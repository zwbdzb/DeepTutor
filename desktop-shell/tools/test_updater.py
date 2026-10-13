"""updater.py 全链路本地验证（fake HTTP server，不碰真实服务器）。

运行：
    python tools/test_updater.py

覆盖：版本比较 / check_latest（正常·404·非法清单） / 下载+SHA256（通过·失败）/
清理 / installed_version（注册表探测，读到读不到都不算失败）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desktop import updater  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def serve(update_dir: Path):
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass
    # directory= 参数固定文档根，绝不用 os.chdir——chdir 会把进程中临时目录锁定，
    # Windows 下 TemporaryDirectory 退出清理必挂（WinError 32/5）。
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(Handler, directory=str(update_dir))
    )
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return f"http://127.0.0.1:{server.server_address[1]}", server


def make_payload(version="1.6.14.10121554", sha="0" * 64, size=123, url=None):
    return {
        "app": "ThinkBuddy",
        "version": version,
        "min_version": "0.1.0",
        "url": url,
        "sha256": sha,
        "size": size,
        "published_at": "2026-10-09T10:00:00+08:00",
        "changelog": "line1\nline2",
    }


def main() -> None:
    print("== 1. 版本比较 ==")
    cases = [
        # 正常引擎升级
        (("1.6.14.10081400", "1.6.13.10030900"), True),
        # 引擎不变，小改动重打包 → 时间戳更新触发更新
        (("1.6.14.10130920", "1.6.14.10081400"), True),
        # 已经是最新
        (("1.6.14.10130920", "1.6.14.10130920"), False),
        # 本地读不到 → 视为有更新
        (("1.6.14.10121554", None), True),
        # 空版本 → 不提示
        (("", "1.6.14.10121554"), False),
        # 兼容旧格式 0.2.0+dt1.6.14（注册表残留）
        (("1.6.14.10121554", "0.2.0+dt1.6.14"), True),
    ]
    for (remote, local), want in cases:
        got = updater.is_newer(remote, local)
        check(f"is_newer({remote!r}, {local!r}) == {want} (got {got})", got == want)

    check("version_key('1.6.14.10121554')", updater.version_key("1.6.14.10121554") == (1, 6, 14, 10121554))
    check("version_key('1.6.14')", updater.version_key("1.6.14") == (1, 6, 14))
    check("version_key('')", updater.version_key("") == (0,))
    check("forced_by_min_version: below threshold", updater.forced_by_min_version("1.6.14", "1.6.13"))
    check("forced_by_min_version: empty -> no force", not updater.forced_by_min_version("", "0.0.1"))
    check("forced_by_min_version: local unknown -> force", updater.forced_by_min_version("0.1.0", None))

    print("== 2. check_latest / download（fake server）==")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        update_dir = root / "update"          # 文档根 root 下的 update/ 子目录
        update_dir.mkdir()
        ds = b"fake-installer-bytes" * 1024
        digest = hashlib.sha256(ds).hexdigest()
        (update_dir / "ThinkBuddySetup-dt1.6.14_10121554.exe").write_bytes(ds)

        (update_dir / "latest.json").write_text(
            json.dumps(make_payload(
                url="http://127.0.0.1:1/IGNORED",  # 占位，check_latest 只看结构
            ), ensure_ascii=False), encoding="utf-8")

        base, server = serve(root)             # 暴露 /update/...
        try:
            mani = updater.check_latest(f"{base}/update")
            check("check_latest ok: version parsed",
                  mani["version"] == "1.6.14.10121554", str(mani))
            check("check_latest ok: sha256 echo field", mani["sha256"] == "0" * 64)

            # --- build 产物形态：URL 指向同目录下的具体安装包 ---
            url = f"{base}/update/ThinkBuddySetup-dt1.6.14_10121554.exe"
            dest = root / "dl" / "Setup.exe"
            progress = []
            updater.download(url, dest, expected_sha256=digest,
                             on_progress=lambda r, t: progress.append((r, t)))
            check("download ok: file written", dest.exists())
            check("download ok: content match", dest.read_bytes() == ds)
            check("download ok: progress reported", len(progress) >= 1)

            # --- 错误 sha256 → 拒绝 ---
            bad = root / "dl" / "Bad.exe"
            try:
                updater.download(url, bad, expected_sha256="f" * 64)
                check("download bad-sha rejected", False)
            except updater.UpdateError:
                check("download bad-sha rejected", True)
            check("download bad-sha cleans .part", not (Path(str(bad) + ".part")).exists())

            # --- 404 ---
            try:
                updater.download(f"{base}/update/nope.exe", root / "dl" / "x.exe")
                check("download 404 raises", False)
            except updater.UpdateError:
                check("download 404 raises", True)

            # --- check_latest 非法（缺 sha256）→ 抛错而非静默已最新 ---
            bad_dir = root / "bad"
            bad_dir.mkdir()
            (bad_dir / "latest.json").write_text(
                json.dumps(make_payload(sha="", url="http://x")), encoding="utf-8")
            bad_base, bad_server = serve(bad_dir)
            try:
                updater.check_latest(bad_base)
                check("check_latest missing-sha raises", False)
            except updater.UpdateError:
                check("check_latest missing-sha raises", True)
            bad_server.shutdown()

            # --- check_latest 404 ---
            try:
                updater.check_latest(f"{base}/no-such-dir")
                check("check_latest 404 raises", False)
            except updater.UpdateError:
                check("check_latest 404 raises", True)

            # --- cleanup_updates_dir ---
            updater.cleanup_updates_dir(root / "dl", keep=dest)
            check("cleanup keeps current, removes others", dest.exists() and not bad.exists())
        finally:
            server.shutdown()

    print("== 3. installed_version（注册表探测，可能为 None）==")
    ver = updater.installed_version()
    print(f"  info  installed_version -> {ver!r}")
    check("installed_version returns str or None", ver is None or isinstance(ver, str))

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
