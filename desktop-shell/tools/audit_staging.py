"""Audit the offline runtime staging tree (READ-ONLY).

裁剪前先量化：输出总体 / 按扩展名 / 按子树 / 按叶子目录 / .py-.pyc 配对 / 裁剪信号。
只输出汇总与 TopN，绝不打印全量文件列表。

约束（评审约定）：
  * 只读：不删、不改、不写 staging；唯一副作用是 --json 指定的报告文件。
  * 不给裁剪建议：判定由人依据数据做出，避免 **/tests 之类通配误伤运行时依赖。
  * pyc-only 方案已被否决（traceback / pkgutil.iter_modules 失效），此处只做量化。

Usage:
    python tools/audit_staging.py
    python tools/audit_staging.py runtime-build/staging --top 20 --json audit.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

# 目录名命中即计入 "tests 信号"（只统计，不代表可删）
TEST_DIR_NAMES = {"test", "tests", "__tests__", "testing", "testdata"}
# 受保护顶层：动态插件发现 / rebrand / 入口脚本，任何规则都不得触碰
PROTECTED_TOP = ("deeptutor", "deeptutor_cli", "deeptutor_web", "run_deeptutor.py")


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024.0 or unit == "GB":
            return f"{n:,.1f} {unit}"
        n /= 1024.0
    return f"{n:,.1f} GB"


def scan(root: Path) -> dict:
    files = dirs = total = 0
    ext_count: Counter[str] = Counter()
    ext_bytes: Counter[str] = Counter()
    sub_count: Counter[str] = Counter()
    sub_bytes: Counter[str] = Counter()
    dir_count: Counter[str] = Counter()
    dir_bytes: Counter[str] = Counter()
    py_ext: dict[str, dict[str, bool]] = {}
    py_bytes = pyc_bytes = 0
    signals: Counter[str] = Counter()
    signal_bytes: Counter[str] = Counter()
    test_owners: Counter[str] = Counter()
    distinfo: Counter[str] = Counter()

    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        dirs += len(dirnames)
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                size = os.stat(full).st_size
            except OSError:
                continue
            files += 1
            total += size

            rel = PurePosixPath(os.path.relpath(full, root).replace(os.sep, "/"))
            parts = rel.parts
            suffix = rel.suffix.lower() or "(no-ext)"
            ext_count[suffix] += 1
            ext_bytes[suffix] += size

            sub = "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) == 2 else "(root)")
            sub_count[sub] += 1
            sub_bytes[sub] += size

            drel = "/".join(parts[:-1]) or "(root)"
            dir_count[drel] += 1
            dir_bytes[drel] += size

            # ---- .py / .pyc 配对 ----
            if suffix == ".py":
                py_bytes += size
                key = str(rel.with_suffix(""))
                py_ext.setdefault(key, {"py": False, "pyc": False})["py"] = True
            elif suffix == ".pyc":
                pyc_bytes += size
                if rel.parent.name == "__pycache__":
                    stem = rel.stem.split(".", 1)[0]          # mod.cpython-312 -> mod
                    key = str(rel.parent.parent / stem)
                else:
                    key = str(rel.with_suffix(""))
                py_ext.setdefault(key, {"py": False, "pyc": False})["pyc"] = True

            # ---- 信号（只计数，不判定） ----
            low = "/" + "/".join(parts).lower() + "/"
            if "/node/npm" in low or "/node/npx" in low:
                signals["node/npm|npx"] += 1
                signal_bytes["node/npm|npx"] += size
            if "/node/corepack" in low:
                signals["node/corepack"] += 1
                signal_bytes["node/corepack"] += size
            if low.startswith("/node/include/"):
                signals["node/include (C headers)"] += 1
                signal_bytes["node/include (C headers)"] += size
            if suffix == ".map":
                signals["*.map (source maps)"] += 1
                signal_bytes["*.map (source maps)"] += size
            if suffix == ".d.ts":
                signals["*.d.ts (type decls)"] += 1
                signal_bytes["*.d.ts (type decls)"] += size
            if ".dist-info/" in low:
                dist_name = next((p for p in parts if p.endswith(".dist-info")), "?")
                distinfo[dist_name] += 1
                signals[".dist-info files"] += 1
                signal_bytes[".dist-info files"] += size
            if any(seg in TEST_DIR_NAMES for seg in (p.lower() for p in parts[:-1])):
                test_owners["/".join(parts[:3])] += 1
                signals["test*/ dirs (files inside)"] += 1
                signal_bytes["test*/ dirs (files inside)"] += size

    paired = sum(1 for v in py_ext.values() if v["py"] and v["pyc"])
    py_only = sum(1 for v in py_ext.values() if v["py"] and not v["pyc"])
    pyc_only = sum(1 for v in py_ext.values() if v["pyc"] and not v["py"])

    return {
        "root": str(root),
        "totals": {"files": files, "dirs": dirs, "bytes": total},
        "by_ext": [{"ext": k, "files": n, "bytes": ext_bytes[k]} for k, n in ext_count.most_common()],
        "by_subtree": [{"subtree": k, "files": n, "bytes": sub_bytes[k]} for k, n in sub_count.most_common()],
        "by_subtree_bytes": [{"subtree": k, "files": sub_count[k], "bytes": b} for k, b in sub_bytes.most_common()],
        "by_leaf_dir": [{"dir": k, "files": n, "bytes": dir_bytes[k]} for k, n in dir_count.most_common()],
        "py_pyc": {
            "py_files": ext_count.get(".py", 0),
            "pyc_files": ext_count.get(".pyc", 0),
            "py_bytes": py_bytes,
            "pyc_bytes": pyc_bytes,
            "modules": len(py_ext),
            "paired": paired,
            "py_without_pyc": py_only,
            "pyc_without_py": pyc_only,
            "modules_without_pyc_pct": (round(100.0 * py_only / len(py_ext), 1) if py_ext else None),
        },
        "signals": [{"signal": k, "files": n, "bytes": signal_bytes[k]} for k, n in signals.most_common()],
        "dist_info": [{"dist_info": k, "files": n} for k, n in distinfo.most_common(20)],
        "test_dir_owners": [{"owner": k, "files": n} for k, n in test_owners.most_common(20)],
        "protected_top_level": list(PROTECTED_TOP),
    }


def print_report(d: dict, top: int) -> None:
    t = d["totals"]
    print("=" * 78)
    print(" staging audit (READ-ONLY)")
    print(f" root  : {d['root']}")
    print(f" files : {t['files']:,}   dirs: {t['dirs']:,}   size: {human(t['bytes'])}")
    if t["files"]:
        print(f" avg   : {human(t['bytes'] / t['files'])} / file")
    print("=" * 78)

    def table(title: str, rows: list, key: str, fk: str, bk: str) -> None:
        print(f"\n-- {title} --")
        if not rows:
            print("   (empty)")
            return
        print(f"   {key:52s} {fk:>9s} {bk:>12s}")
        for r in rows:
            label = str(r[key])
            if len(label) > 52:
                label = "..." + label[-49:]
            print(f"   {label:52s} {r[fk]:>9,} {human(r[bk]):>12s}")

    table(f"by extension (top {top})", d["by_ext"][:top], "ext", "files", "bytes")
    table(f"by subtree (top {top}, by file count)", d["by_subtree"][:top], "subtree", "files", "bytes")
    table(f"by leaf directory (top {top}, by file count)", d["by_leaf_dir"][:top], "dir", "files", "bytes")

    p = d["py_pyc"]
    print("\n-- .py / .pyc pairing --")
    print(f"   .py  : {p['py_files']:,} ({human(p['py_bytes'])})")
    print(f"   .pyc : {p['pyc_files']:,} ({human(p['pyc_bytes'])})")
    print(f"   modules={p['modules']:,}  paired={p['paired']:,}  "
          f"py_wo_pyc={p['py_without_pyc']:,}  pyc_wo_py={p['pyc_without_py']:,}")
    print("   NOTE: pyc-only was rejected (traceback / pkgutil.iter_modules); quantified only.")

    print("\n-- prune signals (count only) --")
    if not d["signals"]:
        print("   (none)")
    for r in d["signals"]:
        print(f"   {r['signal']:40s} {r['files']:>9,}  {human(r['bytes']):>12s}")

    if d["test_dir_owners"]:
        print("\n-- test*/ dirs: top owning subtrees (files inside) --")
        for r in d["test_dir_owners"]:
            print(f"   {r['owner']:52s} {r['files']:>9,}")

    if d["dist_info"]:
        print("\n-- .dist-info top (deeptutor-*/METADATA must stay) --")
        for r in d["dist_info"]:
            print(f"   {r['dist_info']:52s} {r['files']:>9,}")

    print("\n protected: " + ", ".join(d["protected_top_level"]))
    print("=" * 78)


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit runtime staging tree (read-only).")
    default_stage = Path(__file__).resolve().parents[1] / "runtime-build" / "staging"
    ap.add_argument("path", nargs="?", default=str(default_stage), help="staging tree (default: runtime-build/staging)")
    ap.add_argument("--top", type=int, default=15, help="rows per table (default 15)")
    ap.add_argument("--json", dest="json_out", default=None, help="write full JSON report here")
    args = ap.parse_args()

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"[error] not a directory: {root}", file=sys.stderr)
        print("        build it first: .venv\\Scripts\\python.exe tools\\build_runtime.py --no-zip", file=sys.stderr)
        return 2

    data = scan(root)
    print_report(data, max(1, args.top))

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n[json] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

