"""桌面壳进程内 HTTPS 统一信任链（urllib 请求专用，仅标准库依赖）。

为什么存在（2026-11-18 签到"签到服务连接失败"排查结论）：

壳进程里所有直连外网的 HTTPS 请求（OAuth、更新检查、签到、遥测）都走
urllib。冻结/托管运行时里的 OpenSSL 默认信任库**不可靠**：

* Windows embeddable 与 macOS python-build-standalone 的默认 CA 路径在
  用户机器上通常不存在——``ssl.create_default_context()`` 可能加载 0 个
  受信根，任何 HTTPS 都报 ``CERTIFICATE_VERIFY_FAILED``；
* 站点证书链是 WoTrus DV → USERTrust RSA → AAA Certificate Services
  （实测 *.hanyoai.com），精简/企业管控镜像的系统根库可能没有这条链；
* 用户装了自签根证书的代理（企业网关 / Clash mitm）时，自签根只进
  macOS 钥匙串 / Windows 系统库，OpenSSL 默认上下文不认（2026-10-16
  mac 真机更新检查必现，updater._system_ssl_context 由此而来）。

此前只有 OAuth（auth.client._ssl_context，certifi 方案）与 macOS 更新
检查（updater._system_ssl_context，钥匙串方案）各自修过——签到与遥测
仍用裸 ``urlopen``，于是表现为"登录正常、更新正常、签到连接失败"。

本模块把两套方案合并成唯一入口 :func:`trusted_ssl_context`，逐层叠加
信任源，任何一层失败都降级继续（最坏退回系统默认 = 修复前行为）。
auth/updater 后续可迁移过来；本次不动已真机验证的登录/更新链路。
"""
from __future__ import annotations

import functools
import logging
import os
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("dt.http_ssl")

# 环境变量允许运维显式追加企业 CA（与 auth.client 保持同一组名字）。
_ENV_CA_BUNDLES = ("DT_SSL_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")

# macOS 钥匙串：系统根库 + 系统管理员安装的根 + 用户登录钥匙串。
_MACOS_KEYCHAINS = (
    "/Library/Keychains/System.keychain",
    "/System/Library/Keychains/SystemRootCertificates.keychain",
    os.path.expanduser("~/Library/Keychains/login.keychain-db"),
)


def _process_certifi_path() -> str:
    """开发态/已装环境：certifi 可直接导入时的 CA bundle 路径。"""
    try:
        import certifi  # type: ignore

        return certifi.where()
    except Exception:  # noqa: BLE001  打包环境可能不含 certifi
        return ""


def _runtime_certifi_paths() -> list[str]:
    """内嵌托管运行时自带的 certifi（Windows/mac 布局不同）。

    布局对齐 desktop/runtime.select_runtime_base + auth.client 的探测：
    Windows ``<base>/python/Lib/site-packages/certifi/cacert.pem``；
    macOS ``<base>/python/lib/python3*/site-packages/certifi/cacert.pem``。
    """
    try:
        from desktop import runtime as _rt
    except Exception:  # noqa: BLE001  延迟导入失败不致命
        return []
    try:
        base = _rt.select_runtime_base()
    except Exception:  # noqa: BLE001
        log.debug("select_runtime_base 失败，跳过运行时 certifi", exc_info=True)
        return []
    if not base:
        return []
    py_root = Path(base) / "python"
    candidates = [
        py_root / "Lib" / "site-packages" / "certifi" / "cacert.pem",  # Windows
    ]
    candidates.extend(
        py_root.glob("lib/python3*/site-packages/certifi/cacert.pem")  # macOS
    )
    return [str(p) for p in candidates if p.is_file()]


def _export_macos_keychain_pems() -> str:
    """把 macOS 钥匙串证书导出为一段 PEM 文本（对齐 updater 的方案）。

    单个钥匙串导出失败不致命；无 ``security`` 命令（非 macOS）返回空串。
    """
    if sys.platform != "darwin":
        return ""
    pems: list[str] = []
    for keychain in _MACOS_KEYCHAINS:
        try:
            result = subprocess.run(
                ["security", "find-certificate", "-a", "-p", keychain],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                pems.append(result.stdout)
        except Exception:  # noqa: BLE001
            log.debug("keychain export failed: %s", keychain)
    return "\n".join(pems).strip()


@functools.lru_cache(maxsize=1)
def trusted_ssl_context() -> ssl.SSLContext:
    """构建并缓存壳进程统一的 HTTPS 信任上下文。

    逐层叠加（全部 best-effort，失败仅记日志并继续）：

    1. ``ssl.create_default_context()`` 的系统/平台默认信任库；
    2. 进程内 certifi（Mozilla 根库，覆盖 WoTrus 链缺根的管控机器）；
    3. 内嵌托管运行时自带的 certifi；
    4. 环境变量追加的企业 CA（``DT_SSL_CA_BUNDLE`` / ``SSL_CERT_FILE`` /
       ``REQUESTS_CA_BUNDLE``，覆盖 SSL 拦截型代理的根证书）；
    5. macOS 系统钥匙串导出的 PEM（覆盖 Clash mitm / 企业网关自签根）。

    返回值进程级单例（钥匙串导出有 subprocess 开销，必须缓存）。
    """
    ctx = ssl.create_default_context()
    bundles: list[str] = []

    def _load(path: str, label: str) -> None:
        try:
            ctx.load_verify_locations(path)
            bundles.append(label)
        except (OSError, ssl.SSLError):
            log.debug("CA 加载失败（跳过）：%s=%s", label, path, exc_info=True)

    certifi_path = _process_certifi_path()
    if certifi_path:
        _load(certifi_path, "certifi")

    for path in _runtime_certifi_paths():
        _load(path, f"runtime-certifi:{path}")

    for env_name in _ENV_CA_BUNDLES:
        path = os.environ.get(env_name)
        if path:
            _load(path, env_name)

    keychain_pems = _export_macos_keychain_pems()
    if keychain_pems:
        fh = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
        try:
            fh.write(keychain_pems + "\n")
            fh.close()
            _load(fh.name, "macos-keychain")
        finally:
            try:
                os.unlink(fh.name)  # load_verify_locations 已读入内容，可即删
            except OSError:
                pass

    log.info("HTTPS 信任源：%s", " | ".join(bundles) or "仅系统根库")
    return ctx
