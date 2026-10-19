#!/usr/bin/env python3
"""汇总 Windows + macOS 安装包，生成双平台统一更新清单 latest.json。

结构（向后兼容）：
    顶层字段        = Windows 包（老客户端只读顶层，行为不变）
    mac.<arch>      = macOS 各架构包（mac 客户端读 updater.check_latest）
      arch ∈ {"arm64", "x86_64"}

版本号从文件名的 ``dt<主.次.引擎.MMDDHHmm>`` 段解析（构建脚本已保证命名）。

Usage（CI release job / 本地发布）:
    python3 tools/build_update_manifest.py \
        --win-exe "release-artifacts/ThinkBuddySetup-dt*.exe" \
        --mac-dmg "release-artifacts/ThinkBuddyDesktop-mac-arm64-*.dmg" \
        --mac-dmg "release-artifacts/ThinkBuddyDesktop-mac-x86_64-*.dmg" \
        --out release-artifacts/latest.json
选项: --update-base（默认 https://thinkbuddy.hanyoai.com/update）
      --min-version / --changelog
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

DEFAULT_UPDATE_BASE = "https://thinkbuddy.hanyoai.com/update"
DEFAULT_MIN_VERSION = "0.1.0"
_VERSION_RE = re.compile(r"dt(\d+\.\d+\.\d+\.\d+)")

# 北京时间（与 build.ps1 / make_dmg.sh 的打包时间戳同时区）
_BJT = timezone(timedelta(hours=8))


def _expand(pattern: str, kind: str) -> Path:
    hits = sorted(glob.glob(pattern))
    if not hits:
        raise SystemExit(f"{kind} 未找到: {pattern}")
    if len(hits) > 1:
        # 取字典序最大（时间戳命名保证最新者最大）
        print(f"[warn] {kind} 匹配到多个，取最新: {hits[-1]}", file=sys.stderr)
    return Path(hits[-1])


def _parse_version(path: Path) -> str:
    m = _VERSION_RE.search(path.name)
    if not m:
        raise SystemExit(f"文件名缺 dt<版本> 段，无法解析版本: {path.name}")
    return m.group(1)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _entry(path: Path, update_base: str) -> dict:
    return {
        "version": _parse_version(path),
        "url": f"{update_base.rstrip('/')}/{path.name}",
        "sha256": _sha256(path),
        "size": path.stat().st_size,
        "published_at": datetime.now(_BJT).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--win-exe", help="Windows 安装包路径（支持 glob）")
    ap.add_argument("--mac-dmg", action="append", default=[],
                    help="mac dmg 路径（支持 glob，可重复传多个架构）")
    ap.add_argument("--out", default="latest.json")
    ap.add_argument("--update-base", default=DEFAULT_UPDATE_BASE)
    ap.add_argument("--min-version", default=DEFAULT_MIN_VERSION)
    ap.add_argument("--changelog", default="")
    args = ap.parse_args()

    if not args.win_exe and not args.mac_dmg:
        raise SystemExit("至少提供 --win-exe 或 --mac-dmg 之一")

    published_at = datetime.now(_BJT).strftime("%Y-%m-%dT%H:%M:%S+08:00")

    manifest: dict = {
        "app": "ThinkBuddy",
        "min_version": args.min_version,
        "published_at": published_at,
        "changelog": args.changelog,
    }

    if args.win_exe:
        win = _entry(_expand(args.win_exe, "Windows 安装包"), args.update_base)
        # 顶层 = Windows（老客户端契约：version/url/sha256/size 必须在顶层）
        manifest["version"] = win["version"]
        manifest["url"] = win["url"]
        manifest["sha256"] = win["sha256"]
        manifest["size"] = win["size"]

    if args.mac_dmg:
        mac: dict[str, dict] = {}
        for pattern in args.mac_dmg:
            dmg = _expand(pattern, "mac dmg")
            m = re.search(r"-mac-(arm64|x86_64)-", dmg.name)
            if not m:
                raise SystemExit(f"无法从 dmg 文件名识别架构: {dmg.name}")
            mac[m.group(1)] = _entry(dmg, args.update_base)
        manifest["mac"] = mac
        # 顶层 version 需要存在（客户端老逻辑校验用）；mac-only 场景取 mac 最新
        if "version" not in manifest:
            manifest["version"] = max(e["version"] for e in mac.values())

    out = Path(args.out)
    # UTF-8 无 BOM（客户端 json.loads(decode utf-8-sig) 兼容，但保持与 build.ps1 一致）
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"latest.json 生成: {out}")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
