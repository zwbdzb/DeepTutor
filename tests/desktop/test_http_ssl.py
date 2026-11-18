from __future__ import annotations

import ssl
import sys
from pathlib import Path
from unittest.mock import patch

DESKTOP_SHELL = Path(__file__).resolve().parents[2] / "desktop-shell"
sys.path.insert(0, str(DESKTOP_SHELL))

from desktop import http_ssl
from desktop.http_ssl import trusted_ssl_context


def test_trusted_ssl_context_is_cached_single_instance() -> None:
    assert trusted_ssl_context() is trusted_ssl_context()
    assert isinstance(trusted_ssl_context(), ssl.SSLContext)


def test_context_keeps_default_verification_enabled() -> None:
    ctx = trusted_ssl_context()
    # 统一信任链只做"叠加信任源"，绝不放松校验（不能变成不验证书）。
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_runtime_certifi_paths_windows_layout(tmp_path: Path) -> None:
    """Windows embeddable 布局：<base>/python/Lib/site-packages/certifi。"""
    bundle = tmp_path / "python" / "Lib" / "site-packages" / "certifi" / "cacert.pem"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("", encoding="utf-8")
    with patch("desktop.runtime.select_runtime_base", return_value=tmp_path):
        paths = http_ssl._runtime_certifi_paths()
    assert paths == [str(bundle)]


def test_runtime_certifi_paths_macos_layout(tmp_path: Path) -> None:
    """macOS standalone 布局：<base>/python/lib/python3.X/site-packages/certifi。"""
    bundle = (
        tmp_path / "python" / "lib" / "python3.12" / "site-packages"
        / "certifi" / "cacert.pem"
    )
    bundle.parent.mkdir(parents=True)
    bundle.write_text("", encoding="utf-8")
    with patch("desktop.runtime.select_runtime_base", return_value=tmp_path):
        paths = http_ssl._runtime_certifi_paths()
    assert paths == [str(bundle)]


def test_export_keychain_pems_noop_off_macos() -> None:
    if sys.platform == "darwin":
        return  # 真钥匙串导出只在 macOS 上执行
    assert http_ssl._export_macos_keychain_pems() == ""
