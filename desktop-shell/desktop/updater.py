"""ThinkBuddy 更新器：服务器检查 → 下载 → SHA256 校验 → 静默安装。

纯标准库（urllib / winreg / plistlib / hashlib），无 Python GUI 依赖（安装期
的「正在更新」进度小窗由系统 PowerShell + WinForms 承载，见 run_installer），
便于脚本与单测直接验证。

服务器契约（见 docs/adr/ADR-006-server-driven-updates.md）：更新源是一段静态目录，
内含 ``latest.json`` 与带版本名的安装包：

    https://<host>/update/latest.json
    https://<host>/update/ThinkBuddySetup-<safeVersion>.exe          (Windows)
    https://<host>/update/ThinkBuddyDesktop-mac-<arch>-<safeVersion>.dmg  (macOS)

``latest.json`` 字段（双平台：顶层 = Windows，向后兼容老客户端；
``mac`` 子对象按架构区分，macOS 客户端读取）：
    app        "ThinkBuddy"          （校验用，防御放错目录）
    version    "1.6.14.10121554"     （Windows 包版本，与 build.ps1 注入安装器的 AppVersion 同源）
    min_version "0.1.0"              （低于该版本视为强制更新；可缺省，对全平台生效）
    url        Windows 安装包完整下载地址
    sha256     Windows 安装包 SHA256（小写十六进制）
    size       字节数
    published_at / changelog         展示用，缺省无害
    mac        {"arm64": {...}, "x86_64": {...}}  （可选；每项含 version/url/sha256/size）
               兼容形态：清单本身就是单架构 mac 片段（无 mac 键）时直接按顶层读取。

版本语义：
  * ``version`` 格式为 ``主版本.次版本.引擎版本.MMDDHHmm``，末段是打包时间戳。
    比较时按 ``.`` 分段逐段数字比较——引擎版本不变时时间戳自然区分先后。
  * 「本机已装版本」Windows 以 Inno 卸载注册表 DisplayVersion 为准；
    macOS 读自身 .app 的 Info.plist CFBundleShortVersionString；
    读不到（开发态/便携）时由调用方兜底。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

log = logging.getLogger("dt.updater")

# 与 build/installer.iss 的 AppId={{3B4FC0AE-...} 一致（Inno 的 {{ 是转义，实际 AppId 单花括号）。
INSTALLER_APP_ID = "{3B4FC0AE-E3E3-491F-B3B6-59BBBF1D6305}"
MANIFEST_NAME = "latest.json"

# 检查 manifest 的请求超时：短——更新失败绝不能拖住用户。
CHECK_TIMEOUT = 8.0
# 下载单次读超时（秒，两次数据之间无响应视为断流）。
DOWNLOAD_READ_TIMEOUT = 60.0
# 下载进度回调节流：至少相隔这么多秒才再回调一次（避免 toast 刷屏）。
PROGRESS_MIN_INTERVAL = 1.2

_USER_AGENT = "ThinkBuddyDesktop/updater"


def _num_key(text: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", text or "")
    return tuple(int(p) for p in parts) or (0,)


def version_key(version: str) -> tuple[int, ...]:
    """把版本串解析成可比较的数字元组。

    - "1.6.14.10121554" → (1, 6, 14, 10121554)
    - "1.6.14"           → (1, 6, 14)
    - "0.1.0"            → (0, 1, 0)
    - "" / None          → (0,)
    """
    return _num_key(str(version or ""))


def is_newer(remote_version: str, local_version: str | None) -> bool:
    """remote 是否严格高于 local。local 读不到(None)视为旧版 → 提示更新。"""
    if not remote_version:
        return False
    if not local_version:
        return True
    return version_key(remote_version) > version_key(local_version)


class UpdateError(RuntimeError):
    """更新链路失败（网络 / 契约无效 / 校验失败），message 面向用户友好提示。"""


def _mac_arch() -> str:
    """当前 mac 架构名（latest.json 里 mac 子对象的键）。"""
    return "arm64" if platform.machine() == "arm64" else "x86_64"


def installed_version() -> str | None:
    """读取本机「已装版本」。

    Windows：Inno 卸载注册表的 DisplayVersion（per-user 写 HKCU；
    以管理员跑过安装的旧机器可能在 HKLM）。
    macOS：自身 .app 的 Info.plist CFBundleShortVersionString（spec 在
    打包时注入壳版本 1.6.14.MMddHHmm）。
    读不到返回 None（开发态 / 便携版 / 未安装）。
    """
    if sys.platform == "darwin":
        try:
            import plistlib

            exe = Path(sys.executable).resolve()  # .../Contents/MacOS/ThinkBuddyDesktop
            plist_path = exe.parent.parent / "Info.plist"
            with open(plist_path, "rb") as fh:
                value = plistlib.load(fh).get("CFBundleShortVersionString")
            return value.strip() if isinstance(value, str) and value.strip() else None
        except Exception:  # noqa: BLE001  开发态/结构变化 → 调用方兜底
            return None
    uninstall = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    sub_key = f"{uninstall}\\{INSTALLER_APP_ID}_is1"
    try:
        import winreg
    except ImportError:  # 非 Windows（脚本测试环境）
        return None
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, sub_key) as key:
                value, _ = winreg.QueryValueEx(key, "DisplayVersion")
            if isinstance(value, str) and value.strip():
                return value.strip()
        except OSError:
            continue
    return None


_SENTINEL = object()
_SYSTEM_SSL_CTX: object = _SENTINEL


def _system_ssl_context():
    """macOS：用系统钥匙串的信任链校验 HTTPS（对齐 Windows/WKWebView 行为）。

    urllib 默认用 OpenSSL 自带 CA bundle；用户系统装了自签根证书的代理
    （企业网关 / Clash mitm 等）时会 CERTIFICATE_VERIFY_FAILED（2026-10-16
    真机更新检查必现），而 Windows 的 WinHTTP 与 WKWebView 走系统信任库
    无此问题。这里把系统钥匙串证书导出为 PEM 灌进 SSL context。导出失败
    退回 None（urlopen 默认行为）。非 macOS 返回 None。
    """
    global _SYSTEM_SSL_CTX
    if sys.platform != "darwin":
        return None
    if _SYSTEM_SSL_CTX is not _SENTINEL:
        return _SYSTEM_SSL_CTX
    try:
        import ssl

        ctx = ssl.create_default_context()
        pems = []
        for keychain in (
            "/Library/Keychains/System.keychain",
            "/System/Library/Keychains/SystemRootCertificates.keychain",
            os.path.expanduser("~/Library/Keychains/login.keychain-db"),
        ):
            try:
                result = subprocess.run(
                    ["security", "find-certificate", "-a", "-p", keychain],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                if result.returncode == 0 and result.stdout.strip():
                    pems.append(result.stdout)
            except Exception:  # noqa: BLE001  单个钥匙串导出失败不致命
                log.debug("keychain export failed: %s", keychain)
        bundle = "\n".join(pems).strip()
        if bundle:
            fh = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
            fh.write(bundle + "\n")
            fh.close()
            ctx.load_verify_locations(fh.name)
            _SYSTEM_SSL_CTX = ctx
            log.info("更新检查启用系统钥匙串信任链（%d 字节 PEM）", len(bundle))
            return ctx
    except Exception:  # noqa: BLE001  构建信任链失败 → 退回默认校验
        log.exception("system ssl context build failed")
    _SYSTEM_SSL_CTX = None
    return None


def _get_json(url: str, timeout: float) -> dict:
    try:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
        )
        with urllib.request.urlopen(
            request, timeout=timeout, context=_system_ssl_context()
        ) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            exc.read()
        finally:
            exc.close()
        raise UpdateError(f"更新服务器返回异常（HTTP {exc.code}）") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise UpdateError(f"无法连接更新服务器：{reason}") from exc
    try:
        payload = json.loads(raw.decode("utf-8-sig"))  # 兼容服务器端带 BOM 的 JSON
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpdateError("更新服务器返回了无法解析的数据") from exc
    if not isinstance(payload, dict):
        raise UpdateError("更新服务器返回了无法解析的数据")
    return payload


def check_latest(update_base: str, timeout: float = CHECK_TIMEOUT) -> dict:
    """拉取并校验 latest.json，返回规范化 manifest（dict）。

    必要字段（version / url / sha256）缺失或类型不对 → 抛 UpdateError，
    避免「服务器误配一份空清单就让所有客户端静默认为已最新」。

    双平台读取规则：
      * Windows：直接读顶层字段（老格式，完全兼容）。
      * macOS：优先读 ``mac.<当前架构>`` 子对象（统一清单）；清单本身是
        单架构 mac 片段（没有 mac 键）时退回顶层字段。
    """
    base = str(update_base or "").strip().rstrip("/")
    if not base:
        raise UpdateError("未配置更新源")
    payload = _get_json(f"{base}/{MANIFEST_NAME}", timeout)

    if payload.get("app") not in (None, "ThinkBuddy"):
        raise UpdateError("更新清单不属于 ThinkBuddy")

    # macOS：定位本架构的更新条目（统一清单的 mac.<arch> 或独立 mac 片段）
    entry = payload
    if sys.platform == "darwin":
        mac_section = payload.get("mac")
        if isinstance(mac_section, dict):
            arch = _mac_arch()
            entry = mac_section.get(arch)
            if not isinstance(entry, dict):
                raise UpdateError(f"更新清单缺少 macOS {arch} 更新信息")

    version = entry.get("version")
    url = entry.get("url")
    if not (isinstance(version, str) and version.strip() and isinstance(url, str) and url.strip()):
        raise UpdateError("更新清单无效（缺少版本或下载地址）")
    sha256 = entry.get("sha256")
    if not (isinstance(sha256, str) and re.fullmatch(r"[0-9a-fA-F]{64}", sha256 or "")):
        raise UpdateError("更新清单缺少有效的 SHA256 校验值")
    size = entry.get("size")
    # min_version 对全平台生效，放在顶层；changelog/published_at 优先用本条目
    min_version = str(payload.get("min_version") or "").strip()
    published_at = entry.get("published_at") or payload.get("published_at") or ""
    changelog = entry.get("changelog") or payload.get("changelog") or ""
    return {
        "version": version.strip(),
        "min_version": min_version,
        "url": url.strip(),
        "sha256": sha256.lower(),
        "size": int(size) if isinstance(size, int) and not isinstance(size, bool) and size > 0 else None,
        "published_at": str(published_at).strip(),
        "changelog": str(changelog).strip(),
    }


def forced_by_min_version(min_version: str, local_version: str | None) -> bool:
    """本机版本是否低于 min_version（强制更新）。缺 min_version 不强制。"""
    if not min_version:
        return False
    if not local_version:
        return True
    return version_key(min_version) > version_key(local_version)


def download(
    url: str,
    dest_path: str | os.PathLike,
    expected_sha256: str | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    timeout: float = DOWNLOAD_READ_TIMEOUT,
) -> Path:
    """流式下载到临时文件，SHA256 校验通过后原子替换为 dest_path。

    on_progress(received, total)：total 可能为 0（服务器无 Content-Length）。
    任何失败都会清理临时文件并抛 UpdateError。
    """
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.unlink(missing_ok=True)

    hasher = hashlib.sha256()
    received = 0
    try:
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        last_report = [0.0]
        with urllib.request.urlopen(
            request, timeout=timeout, context=_system_ssl_context()
        ) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
                    hasher.update(chunk)
                    received += len(chunk)
                    now = time.monotonic()
                    if on_progress and now - last_report[0] >= PROGRESS_MIN_INTERVAL:
                        last_report[0] = now
                        on_progress(received, total)
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise UpdateError(f"下载失败：{exc}") from exc

    if received == 0:
        tmp.unlink(missing_ok=True)
        raise UpdateError("下载失败：没有收到数据")
    if total and received != total:
        tmp.unlink(missing_ok=True)
        raise UpdateError("下载不完整（长度不符），请重试")
    if expected_sha256:
        got = hasher.hexdigest()
        if got != expected_sha256.lower():
            tmp.unlink(missing_ok=True)
            raise UpdateError("更新包校验失败（SHA256 不符），更新已中止")
    os.replace(tmp, dest)
    if on_progress:
        on_progress(received, received)
    return dest


def _run_installer_darwin(dmg_path: str) -> None:
    """macOS 静默更新：detached bash 脚本编排（本进程随后自行优雅退出）。

    时序：等旧进程退场 → 挂载 dmg → ditto 覆盖 /Applications/ThinkBuddy.app
    → 卸载 → 拉起新版并**验证真的起来了**。关键节点写 <dmg名>.update.log。

    【2026-10-27 改：把两处固定 sleep 换成活跃探测，与 Windows 侧
    「ping 15s → runtime 就绪门」同一套思路】
      1) `sleep 3`（等旧壳退场）→ 轮询 `kill -0 <旧壳 pid>`（调用方把自己的
         pid 传进来）：进程一消失立刻往下走，通常 0.3-1s；上限 30×0.1s=3s，
         与旧的固定等待同量级，**永远不会更慢**。没有 pid 可用（旧版调用）
         时退回固定 3s。
      2) `sleep 8`（落盘后缓冲再 open）→ **启动后验证**：`open` 之后轮询
         `pgrep` 确认进程真的出现（首次最多 6s，通常 1-2s），没出现就重试一次
         （`open` 对「正在启动」的应用是激活而非二次实例，重试安全）。
    为什么 macOS 可以用「验证」替代「等待」而 Windows 不行：Windows 侧
    LoadLibrary 抢跑是**硬失败**（ERROR 126 + 弹框，进程活着但没起来，只能靠
    事前等待规避）；macOS 上 exec 一个新落盘的 Mach-O 只是慢（XProtect 同步
    扫描），不会失败，所以事后验证 + 重试是对的解法，且省掉 8s 白等。

    dmg 由 urllib 下载、hdiutil 挂载，均不经过 LaunchServices，不会带
    com.apple.quarantine 隔离属性——覆盖安装后不会被 Gatekeeper 拦。
    """
    dmg = Path(dmg_path).resolve()
    update_log = dmg.parent / (dmg.stem + ".update.log")
    app_name = "ThinkBuddy.app"
    exe_name = "ThinkBuddy"          # Info.plist 的 CFBundleExecutable（mac.spec:95）
    script = "\n".join([
        "set -u",
        'DMG="$1"; LOG="$2"; OLD_PID="${3:-0}"',
        f'APP_NAME="{app_name}"',
        f'EXE_NAME="{exe_name}"',
        'DEST="/Applications/$APP_NAME"',
        'BIN="$DEST/Contents/MacOS/$EXE_NAME"',
        'log() { echo "[$(date +%T)] $*" >> "$LOG"; }',
        'log "update start: $DMG (old pid=$OLD_PID)"',
        # ---- 1) 等旧壳真正退场（活跃探测，替代固定 sleep 3） --------------
        "old_gone=0",
        'if [ "$OLD_PID" -gt 0 ] 2>/dev/null; then',
        "  i=0",
        '  while [ "$i" -lt 30 ]; do',
        '    if ! kill -0 "$OLD_PID" 2>/dev/null; then old_gone=1; break; fi',
        "    sleep 0.1",
        "    i=$((i + 1))",
        "  done",
        '  log "old process wait: ticks=$i gone=$old_gone"',
        "else",
        "  sleep 3",
        "  old_gone=1",
        '  log "old process wait: fixed 3s (no pid)"',
        "fi",
        'hdiutil attach "$DMG" -nobrowse -quiet || { log "attach failed"; exit 1; }',
        'SRC="$(ls -d /Volumes/ThinkBuddy*/"$APP_NAME" 2>/dev/null | head -1)"',
        'if [ -z "$SRC" ]; then log "app not found in dmg"; hdiutil detach /Volumes/ThinkBuddy* -quiet 2>/dev/null; exit 1; fi',
        'rm -rf "$DEST"',
        'ditto "$SRC" "$DEST" || { log "copy failed"; hdiutil detach "$(dirname "$SRC")" -quiet 2>/dev/null; exit 1; }',
        'hdiutil detach "$(dirname "$SRC")" -quiet 2>/dev/null || true',
        # 复制期间再复核一次旧进程：ditto 覆盖几百 MB 的 bundle 本身要几秒，
        # 前 3s 没退干净的旧壳（优雅退出最多给 15s）到这时通常已经没了 ——
        # 补这一步能把「跳过验证」压成少数真·卡死场景，而不是常态。
        'if [ "$old_gone" -eq 0 ]; then',
        '  if [ "$OLD_PID" -gt 0 ] && ! kill -0 "$OLD_PID" 2>/dev/null; then',
        '    old_gone=1; log "old process gone during copy"',
        "  fi",
        "fi",
        # ---- 2) 落盘校验 + 拉起并验证（替代固定 sleep 8） ----------------
        'if [ ! -x "$BIN" ]; then log "bundle executable missing or not exec: $BIN"; fi',
        # app_up：只认「本 bundle 路径下的 Mach-O」或进程名恰为 EXE_NAME，
        # 避免把系统里同名的别的东西当成启动成功。
        "app_up() {",
        '  pgrep -f "$DEST/Contents/MacOS/" >/dev/null 2>&1 && return 0',
        '  pgrep -x "$EXE_NAME" >/dev/null 2>&1 && return 0',
        "  return 1",
        "}",
        "launch_and_verify() {",
        '  open "$DEST" >>"$LOG" 2>&1 || log "open failed"',
        "  k=0",
        '  while [ "$k" -lt "$1" ]; do',
        '    if app_up; then return 0; fi',
        "    sleep 0.1",
        "    k=$((k + 1))",
        "  done",
        "  return 1",
        "}",
        'if [ "$old_gone" -eq 0 ]; then',
        # 旧实例没死透时 pgrep 会假阳性（匹配到旧的），验证没有意义：直接
        # open，交给应用自身的单实例逻辑（mac 侧是 PID 文件 + 激活既有实例）。
        '  log "old instance still alive; open without verification"',
        '  open "$DEST" >>"$LOG" 2>&1 || log "open failed"',
        "else",
        "  if launch_and_verify 60; then",
        '    log "relaunched (verified)"',
        "  else",
        '    log "first launch not detected, retry once"',
        "    sleep 0.5",
        "    if launch_and_verify 40; then",
        '      log "relaunched on retry (verified)"',
        "    else",
        '      log "app did not start; please launch ThinkBuddy manually"',
        "    fi",
        "  fi",
        "fi",
    ])
    script_path = Path(tempfile.gettempdir()) / f"thinkbuddy_update_{os.getpid()}.sh"
    # newline="\n" 必写：write_text 默认会把 \n 翻成 os.linesep，在 macOS 上恰好
    # 还是 \n，但换到任何非 POSIX 宿主（本仓在 Windows 上做脚本验证）就会落成
    # CRLF，bash 会把它当成 `set -u\r` 这类非法 token，整段脚本直接不工作。
    script_path.write_text(script + "\n", encoding="utf-8", newline="\n")
    try:
        subprocess.Popen(
            # 第三个参数是本进程 pid：脚本据此做「旧壳是否真的退了」的活跃探测。
            ["/bin/bash", str(script_path), str(dmg), str(update_log), str(os.getpid())],
            start_new_session=True,  # 脱离父会话：壳退出后脚本继续跑完
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        script_path.unlink(missing_ok=True)
        raise UpdateError(f"无法启动更新脚本：{exc}") from exc


# 「正在更新」进度小窗脚本（PowerShell WinForms）模板，模块级常量、零插值
# （窗口参数一律经命令行传入，避免字符串拼接与转义）。选型理由：这个窗必须
# 能活过壳自杀（Restart Manager 关不掉 GUI 壳，/VERYSILENT 会把「无法关闭」
# 按取消处理直接中止安装，壳必须先退）、bat 的 taskkill、以及 Inno
# /FORCECLOSEAPPLICATIONS 的 RM 关闭——只有系统自带 powershell.exe（不在
# 安装目录、不持有安装目录文件）天然免疫；壳本身无 tkinter，PyInstaller
# onedir 也不能单拷一个 exe，PowerShell 是零新增二进制的唯一方案。
#
# ⚠ 落盘必须 encoding="utf-8-sig"：Windows PowerShell 5.1 对无 BOM 的 .ps1
# 按 ANSI(GBK) 解码，下面所有中文文案都会变乱码。脚本逻辑纯 ASCII，中文只
# 出现在字符串字面量里（BOM 兜底）。
_UPDATE_UI_PS1 = r"""# =====================================================================
# ThinkBuddy update progress window (Windows PowerShell + WinForms).
# =====================================================================
# Runs as an INDEPENDENT powershell.exe process so the window survives
# everything that happens to the app during a silent update:
#   * the old shell exits itself before Inno Setup starts;
#   * the update bat taskkills ThinkBuddyDesktop.exe;
#   * Inno /FORCECLOSEAPPLICATIONS closes apps holding install-dir files.
# powershell.exe is a system binary: it does not live in the install
# directory and holds no file there, so none of the above can kill it.
#
# FILE ENCODING CONTRACT: this file MUST be written with a UTF-8 BOM
# (Python side uses encoding="utf-8-sig"). Windows PowerShell 5.1 decodes
# BOM-less files as ANSI and every Chinese string below would become
# mojibake. Logic code is pure ASCII; Chinese lives only in literals.
# =====================================================================
param(
    [string]$LogPath = '',
    [string]$NewVersion = '',
    [string]$AppExe = 'ThinkBuddyDesktop.exe'
)

$ErrorActionPreference = 'SilentlyContinue'

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

# ---- window: small borderless top-most dialog, centered on screen ----------
$form = New-Object System.Windows.Forms.Form
$form.Text = 'ThinkBuddy Update'
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.TopMost = $true
$form.BackColor = [System.Drawing.Color]::White
$form.ClientSize = New-Object System.Drawing.Size(380, 190)
try {
    $wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
    $form.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
    $form.Location = New-Object System.Drawing.Point(
        ($wa.Left + [int](($wa.Width - $form.Width) / 2)),
        ($wa.Top + [int](($wa.Height - $form.Height) / 2)))
} catch {
    # Headless / exotic session: fall back to whatever the system decides.
    $form.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
}

# ---- borderless drag support ------------------------------------------------
# A None-border form has no caption to drag by (user feedback 2026-10-27).
# Standard fix: on left MouseDown anywhere on the form or its child controls,
# release mouse capture and send WM_NCLBUTTONDOWN(0xA1)/HTCAPTION(2) so
# Windows itself runs the native move loop. TopMost stays: an updater window
# is expected to stay visible above app windows being installed. Cosmetic
# only -- any Add-Type failure must not take the update window down.
try {
    Add-Type -Name U32 -Namespace W -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool ReleaseCapture();
[DllImport("user32.dll")] public static extern int SendMessage(IntPtr h, int m, IntPtr w, IntPtr l);
'@
    $dragHandler = {
        if ($_.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
            [void][W.U32]::ReleaseCapture()
            [void][W.U32]::SendMessage($form.Handle, 0xA1, [IntPtr]2, [IntPtr]0)
        }
    }
    $form.Add_MouseDown($dragHandler)
    foreach ($ctl in $form.Controls) { $ctl.Add_MouseDown($dragHandler) }
} catch { }

$lblTitle = New-Object System.Windows.Forms.Label
$title = '正在更新 ThinkBuddy'
if ($NewVersion -and $NewVersion.Trim().Length -gt 0) {
    $title = '正在更新 ThinkBuddy v' + $NewVersion.Trim()
}
$lblTitle.Text = $title
$lblTitle.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 11.75, [System.Drawing.FontStyle]::Bold)
$lblTitle.ForeColor = [System.Drawing.Color]::FromArgb(32, 33, 36)
$lblTitle.TextAlign = [System.Drawing.ContentAlignment]::MiddleCenter
$lblTitle.Size = New-Object System.Drawing.Size(360, 36)
$lblTitle.Location = New-Object System.Drawing.Point(10, 24)
$form.Controls.Add($lblTitle)

$bar = New-Object System.Windows.Forms.ProgressBar
$bar.Style = [System.Windows.Forms.ProgressBarStyle]::Marquee
$bar.MarqueeAnimationSpeed = 30
$bar.Size = New-Object System.Drawing.Size(300, 10)
$bar.Location = New-Object System.Drawing.Point(40, 72)
$form.Controls.Add($bar)

$lblStage = New-Object System.Windows.Forms.Label
$lblStage.Text = '正在准备安装…'
$lblStage.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9.75)
$lblStage.ForeColor = [System.Drawing.Color]::FromArgb(32, 33, 36)
$lblStage.TextAlign = [System.Drawing.ContentAlignment]::MiddleCenter
$lblStage.Size = New-Object System.Drawing.Size(360, 60)
$lblStage.Location = New-Object System.Drawing.Point(10, 92)
$form.Controls.Add($lblStage)

$lblHint = New-Object System.Windows.Forms.Label
$lblHint.Text = '预计 1 分钟左右（取决于磁盘与杀毒软件），完成后将自动打开新版本'
$lblHint.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 8.25)
$lblHint.ForeColor = [System.Drawing.Color]::Gray
$lblHint.TextAlign = [System.Drawing.ContentAlignment]::MiddleCenter
$lblHint.Size = New-Object System.Drawing.Size(360, 22)
$lblHint.Location = New-Object System.Drawing.Point(10, 158)
$form.Controls.Add($lblHint)

# ---- 右上角最小化按钮（原版窗体上唯一新增的控件，其余保持不变）---------------
$btnMin = New-Object System.Windows.Forms.Label
$btnMin.Text = '–'
$btnMin.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 11.0)
$btnMin.ForeColor = [System.Drawing.Color]::FromArgb(95, 99, 104)
$btnMin.BackColor = [System.Drawing.Color]::White
$btnMin.TextAlign = [System.Drawing.ContentAlignment]::MiddleCenter
$btnMin.Size = New-Object System.Drawing.Size(32, 24)
$btnMin.Location = New-Object System.Drawing.Point(348, 0)
$btnMin.Cursor = [System.Windows.Forms.Cursors]::Hand
$btnMin.Add_Click({ $form.WindowState = [System.Windows.Forms.FormWindowState]::Minimized })
$form.Controls.Add($btnMin)
$btnMin.BringToFront()


# ---- helpers ----------------------------------------------------------------
function Read-UpdateLog {
    # Read the whole update log. The bat appends to it concurrently, so the
    # file must be opened with FileShare.ReadWrite to avoid sharing clashes;
    # a missing / momentarily locked log simply yields empty text.
    if (-not $LogPath) { return '' }
    try {
        $fs = [System.IO.FileStream]::new($LogPath,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::ReadWrite)
        try {
            $reader = New-Object System.IO.StreamReader($fs)
            $text = $reader.ReadToEnd()
            $reader.Close()
        } finally {
            $fs.Close()
        }
        return [string]$text
    } catch {
        return ''
    }
}

function Show-ErrorStage {
    # Switch the stage line to an error message (red), freeze the marquee and
    # schedule the window to close itself 120 seconds from now.
    param([string]$Message)
    $lblStage.Text = $Message
    $lblStage.ForeColor = [System.Drawing.Color]::Firebrick
    $bar.MarqueeAnimationSpeed = 0
    $script:errorMode = $true
    $script:errorUntilTick = $script:tickCount + 120
}

# ---- polling state -----------------------------------------------------------
$script:tickCount = 0
$script:errorMode = $false
$script:errorUntilTick = -1
$script:launchSeenTick = -1
$procName = ''
if ($AppExe) { $procName = [System.IO.Path]::GetFileNameWithoutExtension($AppExe) }

# Stage mapping (from the update bat's log lines):
#   no log / only start lines            -> '正在准备安装…'
#   runtime children swept, no setup exit -> '正在安装新版本…'
#   setup exit 0                          -> '正在完成收尾…'
#   setup exit 8                          -> runtime 解压失败（安装器自定义码，见 installer.iss）
#   setup exit <non-zero>                 -> red failure text, close in 120s
#   InstallLocation missing               -> red failure text, close in 120s
#   runtime probe waiting                 -> '正在校验运行环境…'（就绪门等待中）
#   deploy enter/extract/swap/warmup      -> '正在部署/解压/启用/预热运行环境…'
#                                            （installer.iss ssPostInstall 追加，约 45s）
#   launching + new process alive         -> '更新完成，正在打开新版本…', 3s
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1000
$timer.Add_Tick({
    $script:tickCount++
    # Hard stop: no matter what the log says, never stay up over 720 s.
    if ($script:tickCount -ge 720) { $form.Close(); return }
    if ($script:errorMode) {
        if ($script:tickCount -ge $script:errorUntilTick) { $form.Close() }
        return
    }
    $text = Read-UpdateLog
    if ($text -match 'InstallLocation missing, abort launch') {
        Show-ErrorStage -Message '更新安装失败：未找到安装目录，请手动重新安装。'
        return
    }
    if ($text -match 'setup exit (\d+)') {
        $code = [int]$Matches[1]
        if ($code -eq 0) {
            # 就绪门等待期（runtime probe waiting 已落盘、ok/timeout 还没落）：
            # 给一句比「正在完成收尾…」更准确的话，用户不会以为卡住。
            if (($text -match 'runtime probe waiting') -and ($text -notmatch 'runtime probe (ok|timeout)')) {
                $lblStage.Text = '正在校验运行环境…'
            } else {
                $lblStage.Text = '正在完成收尾…'
            }
        } elseif ($code -eq 8) {
            Show-ErrorStage -Message '运行环境解压失败（可能被杀毒软件拦截）。旧版本未被破坏，请手动运行下载的安装包完成安装。'
        } else {
            Show-ErrorStage -Message ('更新安装失败（错误码 ' + $code + '）。当前版本未被破坏，可稍后在应用内重试，或手动运行下载的安装包。')
        }
    } elseif ($text -match 'runtime children swept') {
        $lblStage.Text = '正在安装新版本…'
    } else {
        # Log not created yet, or only the early kill/sweep lines landed.
        $lblStage.Text = '正在准备安装…'
    }
    # runtime 部署阶段（installer.iss 的 ssPostInstall 经 /TBLOG 追加 deploy 行）。
    # 这一段约 45 秒（删旧 5s + tar 解压 30s + 预热 13s）且 Inno 自己不提供任何
    # 进度，不映射的话窗口会一直停在「正在完成收尾…」像是死了。
    # 只在 setup 退出前生效：setup exit 落地后日志里仍留着这些行，不能让它们
    # 盖掉后面的「正在完成收尾…」/失败态。
    if ($text -notmatch 'setup exit') {
        if ($text -match 'deploy warmup') {
            $lblStage.Text = '正在预热运行环境…'
        } elseif ($text -match 'deploy swap') {
            $lblStage.Text = '正在启用新运行环境…'
        } elseif ($text -match 'deploy extract') {
            $lblStage.Text = '正在解压运行环境…'
        } elseif ($text -match 'deploy enter') {
            $lblStage.Text = '正在部署运行环境…'
        }
    }
    if ($text -match 'launching') {
        $running = $null
        if ($procName) {
            $running = Get-Process -Name $procName -ErrorAction SilentlyContinue
        }
        if ($running) {
            $lblStage.Text = '更新完成，正在打开新版本…'
            if ($script:launchSeenTick -lt 0) {
                $script:launchSeenTick = $script:tickCount
            } elseif (($script:tickCount - $script:launchSeenTick) -ge 3) {
                $form.Close()
                return
            }
        }
    }
})

# User may close the window (Alt+F4) at any time: harmless to the install.
[void]$timer.Start()
[void]$form.ShowDialog()

# 脚本自身用完即删：否则每次更新都会在 %TEMP% 留一个 thinkbuddy_update_ui_*.ps1
# （实测残留，脚本已全部载入内存，删除不影响正在运行的内容）。
try { Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue } catch { }
"""  # ← _UPDATE_UI_PS1 收尾（必须与开头的 r""" 配对）


def _spawn_update_ui(
    update_log: Path, new_version: str, app_exe: str
) -> "subprocess.Popen | None":
    """拉起「正在更新」进度小窗（独立 PowerShell 进程，best-effort）。

    UI 的任何失败都绝不允许阻断更新主链路：这里整段 try/except，失败只记
    warning 并返回 None，调用方照常放出安装 bat。成功时返回进程句柄，供
    调用方在 bat 拉起失败时 terminate（防孤儿窗口）。
    """
    try:
        ps1_path = Path(tempfile.gettempdir()) / f"thinkbuddy_update_ui_{os.getpid()}.ps1"
        # utf-8-sig：必须带 BOM，PowerShell 5.1 才能按 UTF-8 读对中文文案。
        ps1_path.write_text(_UPDATE_UI_PS1, encoding="utf-8-sig")
        # 版本号只用于进度窗标题，做一次防御性清洗（正常是 1.2.3.4 形态）。
        safe_version = re.sub(r"[^\w.\-+]", "", str(new_version or ""))[:64]
        return subprocess.Popen(
            [
                "powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
                "-WindowStyle", "Hidden", "-File", str(ps1_path),
                "-LogPath", str(update_log),
                "-NewVersion", safe_version,
                "-AppExe", app_exe,
            ],
            creationflags=0x08000000,  # CREATE_NO_WINDOW：不闪 PowerShell 控制台
        )
    except Exception:  # noqa: BLE001  进度窗失败不阻断更新
        log.warning("update progress window spawn failed", exc_info=True)
        return None


def run_installer(setup_path: str | os.PathLike, new_version: str | None = None) -> None:
    """静默安装更新，并在安装完成后**延迟**拉起新版本。

    Windows：先 best-effort 拉起独立 PowerShell 进程渲染「正在更新」进度
    小窗（任何 UI 失败都不阻断更新），再放出临时 bat 编排时序——
        1) taskkill /T 兜底清理残留旧实例（连树杀，防文件占用中止静默
           安装），并按安装目录精确清扫 runtime python/node 孤儿（换文件
           窗口期撞半成品运行时的崩溃循环，2026-10-23 用户机实证）；
        2) 阻塞运行 /VERYSILENT 安装（bat 顺序执行天然等待完成）。新运行时
           的冷启动预热由 installer.iss 的 CurStepChanged(ssPostInstall)
           在复制文件后完成，bat 不再重复做；
        3) 就绪门 + 启动：探测 runtime 的 python.exe 能否起来（等价于
           LoadLibrary(python312.dll) 是否被杀软放行，PyInstaller bootloader
           早于 main 执行的正是这一步，抢跑会拿到 ERROR 126，2026-10-14
           实测），通过即启动，失败按 1s 间隔重试、上限 15 拍（与原先
           固定 15s 延迟同量级，正常路径 ~0.1s）；
        4) 从注册表 InstallLocation 读安装目录，拉起新壳。
    bat 与进度窗全程 CREATE_NO_WINDOW / -WindowStyle Hidden，不弹黑窗；小窗
    存活在系统 powershell.exe 进程里，不受壳自退、taskkill 与 Inno Restart
    Manager 关闭影响，整个安装期用户都有可见反馈。bat 顺序写 update.log，
    小窗按日志行映射阶段文案。

    macOS：临时 sh detached 执行（见 _run_installer_darwin）；``new_version``
    仅 Windows 使用，macOS 分支忽略。

    ``new_version``：目标版本号（可选），仅用于进度窗标题「正在更新
    ThinkBuddy vX.Y.Z」。
    两个平台的调用方都在脚本放出后**自行优雅退出**（menubar._graceful_exit_for_update）。
    """
    setup = str(setup_path)
    if not os.path.exists(setup):
        raise UpdateError("更新包不存在，无法安装")
    if sys.platform == "darwin":
        _run_installer_darwin(setup)
        return
    setup_path_obj = Path(setup)
    install_log = setup_path_obj.parent / (setup_path_obj.stem + ".install.log")
    update_log = setup_path_obj.parent / (setup_path_obj.stem + ".update.log")
    app_exe = "ThinkBuddyDesktop.exe"
    uninstall_key = f"HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{INSTALLER_APP_ID}_is1"
    # 换文件窗口期的运行时孤儿清扫（2026-10-24）：优雅退出链断裂（硬退路径）
    # 时 detached 的 python/node 孤儿可能活到 Inno 换文件窗口，被
    # /FORCECLOSEAPPLICATIONS 强杀后又被壳/launcher 反复拉起，撞上半成品
    # 运行时 → ModuleNotFoundError 崩溃循环（2026-10-23 用户机 1.6.15 更新
    # 事故实证：先后缺 security_guard / voice.adapters.gemini 连崩 8 次）。
    # Inno 启动前按「可执行文件路径位于安装目录内」精确清场，不误伤用户
    # 自己的 python/node。安装器退出后 bat 再从注册表读 InstallLocation
    # 拉起新壳（同一目录）。
    ps_sweep = (
        "$d=$env:TB_APPDIR; if($d){"
        "Get-Process python,node -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Path -and $_.Path.ToLower().StartsWith(($d.TrimEnd('\\') + '\\').ToLower()) } | "
        "Stop-Process -Force }"
    )
    # bat 主体保持纯 ASCII（cmd 按 OEM 代码页解析，中文注释会乱码，同
    # build.ps1 的教训），但 setup/install_log/update_log 等嵌入路径位于
    # %LOCALAPPDATA% 下，中文用户名机器上必然含中文，因此写盘**不能**用
    # ascii（会在中文用户名机器上直接炸 UnicodeEncodeError，断掉整条
    # 静默更新链 —— P2-5，2026-10-26 修复）。改用 mbcs（Windows ANSI
    # 代码页，中文系统即 GBK，与 cmd 解析代码页一致）；若内容含 ANSI
    # 代码页之外的字符，降级为 UTF-8 写盘并在 bat 头部 chcp 65001。
    bat_lines = [
        "@echo off",
        "setlocal",
        f"echo [%date% %time%] update-bat started >> \"{update_log}\"",
        # 只杀壳本身，**绝不能带 /T**：本 bat 是壳用 Popen 起的 cmd 子进程，
        # /T 会连壳的整棵子进程树一起杀 —— 包括正在执行本文件的 cmd.exe 自己，
        # bat 当场死掉，安装器一行都执行不到（2026-10-26 用户机实证：update.log
        # 只剩第一行 update-bat started，且不产生 .install.log）。
        # 壳直拉的 launcher python / uvicorn / node 孤儿由下一段按安装目录前缀
        # 精确清扫，不依赖 /T。
        f"taskkill /F /FI \"IMAGENAME eq {app_exe}\" >nul 2>&1",
        f"echo [%date% %time%] old instances killed >> \"{update_log}\"",
        f"for /f \"tokens=2*\" %%A in ('reg query \"{uninstall_key}\" /v InstallLocation 2^>nul ^| findstr InstallLocation') do set \"APPDIR=%%B\"",
        "if not defined APPDIR goto :runtime_swept",
        "if \"%APPDIR:~-1%\"==\"\\\" set \"APPDIR=%APPDIR:~0,-1%\"",
        "set \"TB_APPDIR=%APPDIR%\"",
        f"powershell -NoProfile -Command \"{ps_sweep}\" >nul 2>&1",
        ":runtime_swept",
        f"echo [%date% %time%] runtime children swept >> \"{update_log}\"",
        f"\"{setup}\" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /FORCECLOSEAPPLICATIONS /MERGETASKS=\"!autostart\" /LOG=\"{install_log}\" /TBLOG=\"{update_log}\"",
        # 注意是单 %errorlevel%（bat 文件里 %%errorlevel%% 只会输出字面
        # 「%errorlevel%」，实测 2026-10-25）：进度窗按「setup exit <数字>」
        # 映射「正在完成收尾」与「错误码 N」失败态，日志里必须落真实数字。
        f"echo [%date% %time%] setup exit %errorlevel% >> \"{update_log}\"",
        # 预热已由 installer.iss 的 CurStepChanged(ssPostInstall) 承担（Inno
        # 复制完文件后对全新 runtime 做同样的冷导入，覆盖更新与新装两条路），
        # bat 内这段跑在热缓存上是纯重复，删除。
        #
        # 【2026-10-27 改】这里原先是一句 `ping -n 16`，即**盲等 15 秒**，
        # 赌杀软把刚落地的 DLL 扫完（否则 PyInstaller bootloader 在 main
        # 之前 LoadLibrary(python312.dll) 会拿到 ERROR 126，2026-10-14 本机
        # 静默更新必现）。用户机实测这段固定占更新全程 ~45s 的 25%
        # （10:12:51.60 setup exit 0 → 10:13:06.88 launching，正好 15.28s）。
        # 改成**主动探测**：直接用 runtime 里的 python.exe 跑一句 -c，这一步
        # 必然 LoadLibrary(python312.dll)——正是失败的那个操作；能跑通就说明
        # 杀软已经放行，立刻启动；跑不通就隔 1s 重试。上限 15 tick 与旧的
        # 固定 15s 同量级，**永远不会比旧行为更慢**，但正常路径只需 1 次探测
        # （~0.1s）。日志里落 ticks 数，方便后续按真实数据判断还需不需要它。
        f"for /f \"tokens=2*\" %%A in ('reg query \"{uninstall_key}\" /v InstallLocation 2^>nul ^| findstr InstallLocation') do set \"APPDIR=%%B\"",
        f"if not defined APPDIR (echo [%date% %time%] InstallLocation missing, abort launch >> \"{update_log}\" & exit /b 1)",
        "if \"%APPDIR:~-1%\"==\"\\\" set \"APPDIR=%APPDIR:~0,-1%\"",
        "set \"GATE_TICKS=0\"",
        ":gate_probe",
        # 探测体只做「能不能起来」这一件事：-c pass 不需要任何 import，
        # 返回 0 就说明 python312.dll 与 UCRT 依赖都能加载（实测健康 ~0.1s，
        # 缺 DLL 时 0.05s 返回非 0，且 CREATE_NO_WINDOW 下不弹加载器模态框）。
        "\"%APPDIR%\\runtime\\python\\python.exe\" -c \"pass\" >nul 2>&1",
        # 判据用等值比较 `EQU 0`，不能写 `if errorlevel 1`：DLL 加载失败时
        # 进程退出码是 0xC0000135（实测 cmd 里呈现为**负数** -1073741515），
        # `if errorlevel 1` 对负数恒不成立，会把失败误判成成功。
        "if %errorlevel% EQU 0 goto :gate_ready",
        "set /a GATE_TICKS+=1",
        "if %GATE_TICKS% GEQ 15 goto :gate_timeout",
        # 只记第一拍：进度窗据此显示「正在校验运行环境…」，又不刷爆日志。
        f"if %GATE_TICKS% EQU 1 echo [%date% %time%] runtime probe waiting >> \"{update_log}\"",
        "ping -n 2 127.0.0.1 >nul",
        "goto :gate_probe",
        ":gate_timeout",
        f"echo [%date% %time%] runtime probe timeout ticks=%GATE_TICKS%, launching anyway >> \"{update_log}\"",
        "goto :launch_now",
        ":gate_ready",
        f"echo [%date% %time%] runtime probe ok ticks=%GATE_TICKS% >> \"{update_log}\"",
        ":launch_now",
        f"echo [%date% %time%] launching %APPDIR%\\{app_exe} >> \"{update_log}\"",
        f"start \"\" /D \"%APPDIR%\" \"%APPDIR%\\{app_exe}\"",
    ]
    bat_path = Path(tempfile.gettempdir()) / f"thinkbuddy_update_{os.getpid()}.bat"
    bat_payload = "\r\n".join(bat_lines) + "\r\n"
    # newline="" 必写：payload 已自带 \r\n，而 write_text 默认还会把每个 \n 再翻
    # 成 os.linesep，落盘变成 \r\r\n（2026-10-27 实测）。cmd 目前容忍多余 \r，
    # 但这是双重回车而不是标准 CRLF，属于随时可能咬人的隐式行为。
    try:
        bat_path.write_text(bat_payload, encoding="mbcs", newline="")
    except (LookupError, UnicodeEncodeError):
        # mbcs 不可用（非 Windows 测试环境）或内容含 ANSI 外字符：
        # 降级 UTF-8 写盘 + chcp 65001，让 cmd 按 UTF-8 解析后续行。
        bat_path.write_text(
            "\r\n".join(["@echo off", "chcp 65001 >nul", *bat_lines[1:]]) + "\r\n",
            encoding="utf-8",
            newline="",
        )
    # 先拉进度窗、后放 bat：窗是独立 powershell.exe 进程，bat 随后 taskkill/
    # 安装器 RM 都伤不到它；窗若拉不起来只记 warning，更新照常进行。
    ui_proc = _spawn_update_ui(update_log, new_version or "", app_exe)
    try:
        subprocess.Popen(
            ["cmd", "/c", str(bat_path)],
            creationflags=0x08000000,  # CREATE_NO_WINDOW：全程隐藏，不弹黑窗
        )
    except OSError as exc:
        # bat 没放出去：回收刚拉起的进度窗进程，避免留下孤儿窗口。
        if ui_proc is not None:
            try:
                ui_proc.terminate()
            except Exception:  # noqa: BLE001  回收失败也无妨（窗自身 720s 超时兜底）
                pass
        bat_path.unlink(missing_ok=True)
        raise UpdateError(f"无法启动安装程序：{exc}") from exc


def cleanup_updates_dir(updates_dir: str | os.PathLike, keep: str | os.PathLike | None = None) -> None:
    """清理更新缓存目录，保留本次要用的安装包，其余删除（可能有数百 MB 旧包）。"""
    folder = Path(updates_dir)
    if not folder.is_dir():
        return
    keep_path = Path(keep) if keep else None
    for child in folder.iterdir():
        if child.is_file() and (keep_path is None or child.resolve() != keep_path.resolve()):
            try:
                child.unlink(missing_ok=True)
            except OSError:
                log.debug("cleanup skipped: %s", child)
