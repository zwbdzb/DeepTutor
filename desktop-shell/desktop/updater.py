"""ThinkBuddy 更新器：服务器检查 → 下载 → SHA256 校验 → 静默安装。

纯标准库（urllib / winreg / plistlib / hashlib），无 GUI 依赖，便于脚本与单测直接验证。

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
    """macOS 静默更新：detached sh 脚本编排（本进程随后自行优雅退出）。

    时序：等旧进程退场 → 挂载 dmg → ditto 覆盖 /Applications/ThinkBuddy.app
    → 卸载 → 延迟拉起新版（给文件系统/安全扫描留缓冲，对齐 Windows
    「安装器不要立刻 Exec 新壳」的教训）。关键节点写 <dmg名>.update.log。
    dmg 由 urllib 下载、hdiutil 挂载，均不经过 LaunchServices，不会带
    com.apple.quarantine 隔离属性——覆盖安装后不会被 Gatekeeper 拦。
    """
    dmg = Path(dmg_path).resolve()
    update_log = dmg.parent / (dmg.stem + ".update.log")
    app_name = "ThinkBuddy.app"
    script = "\n".join([
        "set -u",
        'DMG="$1"; LOG="$2"',
        f'APP_NAME="{app_name}"',
        'DEST="/Applications/$APP_NAME"',
        'echo "[$(date +%T)] update start: $DMG" >> "$LOG"',
        "sleep 3",  # 等旧壳进程完全退场（调用方优雅退出后本脚本才继续）
        'hdiutil attach "$DMG" -nobrowse -quiet || { echo "[$(date +%T)] attach failed" >> "$LOG"; exit 1; }',
        'SRC="$(ls -d /Volumes/ThinkBuddy*/"$APP_NAME" 2>/dev/null | head -1)"',
        'if [ -z "$SRC" ]; then echo "[$(date +%T)] app not found in dmg" >> "$LOG"; hdiutil detach /Volumes/ThinkBuddy* -quiet 2>/dev/null; exit 1; fi',
        'rm -rf "$DEST"',
        'ditto "$SRC" "$DEST" || { echo "[$(date +%T)] copy failed" >> "$LOG"; hdiutil detach "$(dirname "$SRC")" -quiet 2>/dev/null; exit 1; }',
        'hdiutil detach "$(dirname "$SRC")" -quiet 2>/dev/null || true',
        "sleep 8",  # 新二进制落盘后稍作缓冲再启动（对齐 Windows 延迟启动经验）
        'open "$DEST"',
        'echo "[$(date +%T)] relaunched" >> "$LOG"',
    ])
    script_path = Path(tempfile.gettempdir()) / f"thinkbuddy_update_{os.getpid()}.sh"
    script_path.write_text(script + "\n", encoding="utf-8")
    try:
        subprocess.Popen(
            ["/bin/bash", str(script_path), str(dmg), str(update_log)],
            start_new_session=True,  # 脱离父会话：壳退出后脚本继续跑完
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        script_path.unlink(missing_ok=True)
        raise UpdateError(f"无法启动更新脚本：{exc}") from exc


def run_installer(setup_path: str | os.PathLike) -> None:
    """静默安装更新，并在安装完成后**延迟**拉起新版本。

    Windows：临时 bat 编排时序——
        1) taskkill /T 兜底清理残留旧实例（连树杀，防文件占用中止静默
           安装），并按安装目录精确清扫 runtime python/node 孤儿（换文件
           窗口期撞半成品运行时的崩溃循环，2026-10-23 用户机实证）；
        2) 阻塞运行 /VERYSILENT 安装（bat 顺序执行天然等待完成）；
        3) ping 延迟约 15s，给杀软扫描新 DLL 留出时间（PyInstaller
           bootloader 若在安装器 Exec 后立刻 LoadLibrary(python312.dll)
           可能拿到 ERROR 126，手动延迟启动则正常，2026-10-14 实测）；
        4) 从注册表 InstallLocation 读安装目录，拉起新壳。
    bat 全程 CREATE_NO_WINDOW 隐藏运行；安装器带 CloseApplications=force 兜底。
    不能指望 Restart Manager 关本进程：RM 对 GUI 壳常失败（事件 10006），
    Inno 静默模式会把被抑制的「无法关闭应用」框按「取消」处理直接中止。

    macOS：临时 sh detached 执行（见 _run_installer_darwin）。
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
        # /T 连树杀：壳直拉的 launcher python 及孙进程（uvicorn/node）一起退场，
        # 不给换文件窗口留任何会 import 半成品运行时的活进程。
        f"taskkill /F /T /FI \"IMAGENAME eq {app_exe}\" >nul 2>&1",
        f"echo [%date% %time%] old instances killed >> \"{update_log}\"",
        f"for /f \"tokens=2*\" %%A in ('reg query \"{uninstall_key}\" /v InstallLocation 2^>nul ^| findstr InstallLocation') do set \"APPDIR=%%B\"",
        "if not defined APPDIR goto :runtime_swept",
        "if \"%APPDIR:~-1%\"==\"\\\" set \"APPDIR=%APPDIR:~0,-1%\"",
        "set \"TB_APPDIR=%APPDIR%\"",
        f"powershell -NoProfile -Command \"{ps_sweep}\" >nul 2>&1",
        ":runtime_swept",
        f"echo [%date% %time%] runtime children swept >> \"{update_log}\"",
        f"\"{setup}\" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /FORCECLOSEAPPLICATIONS /MERGETASKS=\"!autostart\" /LOG=\"{install_log}\"",
        f"echo [%date% %time%] setup exit %%errorlevel%% >> \"{update_log}\"",
        "ping -n 16 127.0.0.1 >nul",
        f"for /f \"tokens=2*\" %%A in ('reg query \"{uninstall_key}\" /v InstallLocation 2^>nul ^| findstr InstallLocation') do set \"APPDIR=%%B\"",
        f"if not defined APPDIR (echo [%date% %time%] InstallLocation missing, abort launch >> \"{update_log}\" & exit /b 1)",
        "if \"%APPDIR:~-1%\"==\"\\\" set \"APPDIR=%APPDIR:~0,-1%\"",
        # 预热后端导入（2026-10-24）：新装运行时的 15000+ 个 pyc/py 首次被读
        # 时会挨个过杀软实时扫描（后端 uvicorn 导入实测 29s，冷盘更久），全部
        # 落在用户更新后的第一次启动里。这里在 bat 内、拉起新壳之前用安装好的
        # 解释器静默导入一次完整应用栈，把扫描成本吸收进安装阶段（用户此时
        # 本就在等安装器）。失败不阻塞：预热只是优化，任何错误都放行启动。
        # 注意用 goto 而非 if (...) 括号块：块内 %%errorlevel%% 在解析期提前
        # 展开，会记录到进块前的陈旧值，日志失真。
        f"if not exist \"%APPDIR%\\runtime\\python\\python.exe\" goto :prewarm_done",
        f"echo [%date% %time%] prewarming backend imports >> \"{update_log}\"",
        "\"%APPDIR%\\runtime\\python\\python.exe\" -c \"import deeptutor.api.main\" >nul 2>&1",
        f"echo [%date% %time%] prewarm done (exit %%errorlevel%%) >> \"{update_log}\"",
        ":prewarm_done",
        f"echo [%date% %time%] launching %APPDIR%\\{app_exe} >> \"{update_log}\"",
        f"start \"\" /D \"%APPDIR%\" \"%APPDIR%\\{app_exe}\"",
    ]
    bat_path = Path(tempfile.gettempdir()) / f"thinkbuddy_update_{os.getpid()}.bat"
    bat_payload = "\r\n".join(bat_lines) + "\r\n"
    try:
        bat_path.write_text(bat_payload, encoding="mbcs")
    except (LookupError, UnicodeEncodeError):
        # mbcs 不可用（非 Windows 测试环境）或内容含 ANSI 外字符：
        # 降级 UTF-8 写盘 + chcp 65001，让 cmd 按 UTF-8 解析后续行。
        bat_path.write_text(
            "\r\n".join(["@echo off", "chcp 65001 >nul", *bat_lines[1:]]) + "\r\n",
            encoding="utf-8",
        )
    try:
        subprocess.Popen(
            ["cmd", "/c", str(bat_path)],
            creationflags=0x08000000,  # CREATE_NO_WINDOW：全程隐藏，不弹黑窗
        )
    except OSError as exc:
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
