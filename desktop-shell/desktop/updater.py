"""ThinkBuddy 更新器：服务器检查 → 下载 → SHA256 校验 → 静默安装。

纯标准库（urllib / winreg / hashlib），无 GUI 依赖，便于脚本与单测直接验证。

服务器契约（见 docs/adr/ADR-006-server-driven-updates.md）：更新源是一段静态目录，
内含 ``latest.json`` 与带版本名的安装包：

    https://<host>/update/latest.json
    https://<host>/update/ThinkBuddySetup-<safeVersion>.exe

``latest.json`` 字段：
    app        "ThinkBuddy"          （校验用，防御放错目录）
    version    "1.6.14.10121554"     （与 build.ps1 注入安装器的 AppVersion 同源）
    min_version "0.1.0"              （低于该版本视为强制更新；可缺省）
    url        安装包完整下载地址
    sha256     安装包 SHA256（小写十六进制）
    size       字节数
    published_at / changelog         展示用，缺省无害

版本语义：
  * ``version`` 格式为 ``主版本.次版本.引擎版本.MMDDHHmm``，末段是打包时间戳。
    比较时按 ``.`` 分段逐段数字比较——引擎版本不变时时间戳自然区分先后。
  * 「本机已装版本」以 Inno 卸载注册表 DisplayVersion 为准；
    读不到（开发态/便携）时由调用方兜底。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
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


def installed_version() -> str | None:
    """读取本机「已装版本」：Inno 卸载注册表的 DisplayVersion。

    per-user 安装写 HKCU；以管理员跑过安装的旧机器可能在 HKLM。
    读不到返回 None（开发态 / 便携版 / 未安装）。
    """
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


def _get_json(url: str, timeout: float) -> dict:
    try:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=timeout) as resp:
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
    """
    base = str(update_base or "").strip().rstrip("/")
    if not base:
        raise UpdateError("未配置更新源")
    payload = _get_json(f"{base}/{MANIFEST_NAME}", timeout)

    version = payload.get("version")
    url = payload.get("url")
    if not (isinstance(version, str) and version.strip() and isinstance(url, str) and url.strip()):
        raise UpdateError("更新清单无效（缺少版本或下载地址）")
    if payload.get("app") not in (None, "ThinkBuddy"):
        raise UpdateError("更新清单不属于 ThinkBuddy")
    sha256 = payload.get("sha256")
    if not (isinstance(sha256, str) and re.fullmatch(r"[0-9a-fA-F]{64}", sha256 or "")):
        raise UpdateError("更新清单缺少有效的 SHA256 校验值")
    size = payload.get("size")
    return {
        "version": version.strip(),
        "min_version": str(payload.get("min_version") or "").strip(),
        "url": url.strip(),
        "sha256": sha256.lower(),
        "size": int(size) if isinstance(size, int) and not isinstance(size, bool) and size > 0 else None,
        "published_at": str(payload.get("published_at") or "").strip(),
        "changelog": str(payload.get("changelog") or "").strip(),
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
        with urllib.request.urlopen(request, timeout=timeout) as resp:
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


def run_installer(setup_path: str | os.PathLike) -> None:
    """静默启动 Inno 安装器（per-user 安装无需 UAC 提权）。

    参数：/VERYSILENT /SUPPRESSMSGBOXES /NORESTART，外加 /LOG 写到安装包
    同目录（ThinkBuddySetup-<版本>.install.log）——静默安装一旦中止没有任何
    可见反馈，这份日志是唯一的事后取证渠道。
    安装器带 `CloseApplications=yes`：正常情况下 [Run] 段（nowait
    skipifnotsilent）装完会自动拉起新版本实例。调用方应随后**自行优雅退出**
    （见 menubar._graceful_exit_for_update）——不能指望 Restart Manager 来关
    本进程：RM 对 GUI 壳的关闭请求经常失败（事件 10006），Inno 静默模式下
    会把被抑制的「无法关闭应用」消息框按「取消」处理，直接中止安装。
    本函数只把安装器进程放出去，不等它完成（等不及——本进程马上退出）。
    """
    setup = str(setup_path)
    if not os.path.exists(setup):
        raise UpdateError("更新包不存在，无法安装")
    setup_path_obj = Path(setup)
    install_log = setup_path_obj.parent / (setup_path_obj.stem + ".install.log")
    cmd = [
        setup,
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        f"/LOG={install_log}",
    ]
    try:
        subprocess.Popen(
            cmd,
            creationflags=0x08000000,  # CREATE_NO_WINDOW：静默不弹黑窗
        )
    except OSError as exc:
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
