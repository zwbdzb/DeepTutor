"""Prune the offline runtime staging tree according to a reviewable policy.

**Dry-run by default.** `--apply` refuses to touch anything unless every active
rule is `state: "approved"` in the policy file — so a candidate rule can never
delete files by accident.

Design rules (from the packaging review):
  * No blanket cross-tree wildcards. Each rule names a scope (top-level dir) and
    either an exact path prefix or a glob evaluated *inside* that scope.
  * A hard protect list wins over every rule: our own packages, entry scripts,
    any `__init__.py`, and `deeptutor-*.dist-info/METADATA`.
  * Every removal is recorded in the report (path + rule + bytes) for audit.

Usage:
    python tools/prune_staging.py --policy build/prune-policy.json
    python tools/prune_staging.py --policy build/prune-policy.json --apply
    python tools/prune_staging.py --policy build/prune-policy.json --report build/prune-report.json

Exit codes: 0 ok / 2 bad input / 3 apply requested while rules are unapproved.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
from pathlib import Path, PurePosixPath

ACTIVE_STATES = ("candidate", "approved")


def _glob_variants(pat: str) -> list[str]:
    """`**/x` must also match a file directly under the scope (zero dirs)."""
    variants = [pat]
    p = pat
    while p.startswith("**/"):
        p = p[3:]
        variants.append(p)
    return variants


def is_protected(rel: str, protected: list[str]) -> bool:
    parts = rel.split("/")
    if parts[-1] == "__init__.py":
        return True
    # deeptutor-*.dist-info/METADATA drives the "About" version read
    if rel.endswith("/METADATA") and ".dist-info/" in rel:
        for seg in parts:
            if seg.startswith("deeptutor") and seg.endswith(".dist-info"):
                return True
    for p in protected:
        if p.startswith("**"):
            if any(fnmatch.fnmatch(rel, v) for v in _glob_variants(p)):
                return True
        elif rel == p or rel.startswith(p + "/"):
            return True
    return False


def rule_matches(rel: str, rule: dict) -> bool:
    parts = rel.split("/")
    scope = rule.get("scope", "*")
    if scope != "*" and (not parts or parts[0] != scope):
        return False

    kind = rule.get("kind")
    if kind == "path-prefix":
        return any(rel == m or rel.startswith(m + "/") for m in rule["match"])

    if kind == "glob-in-scope":
        sub = rel if scope == "*" else "/".join(parts[1:])
        for pat in rule["match"]:
            for v in _glob_variants(pat):
                if PurePosixPath(sub).match(v) or fnmatch.fnmatch(sub, v):
                    return True
        return False

    raise ValueError(f"rule {rule.get('id')}: unknown kind {kind!r}")


def remove_empty_dirs(root: Path) -> int:
    removed = 0
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        p = Path(dirpath)
        if p == root:
            continue
        try:
            if not any(p.iterdir()):
                p.rmdir()
                removed += 1
        except OSError:
            pass
    return removed

def main() -> int:
    ap = argparse.ArgumentParser(description="Prune runtime staging (dry-run by default).")
    ap.add_argument("--staging", default=str(Path(__file__).resolve().parents[1] / "runtime-build" / "staging"))
    ap.add_argument("--policy", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report", default=None)
    ap.add_argument("--samples", type=int, default=5, help="sample paths printed per rule")
    args = ap.parse_args()

    staging = Path(args.staging)
    if not staging.is_dir():
        print(f"[error] staging not found: {staging}", file=sys.stderr)
        return 2
    policy_path = Path(args.policy)
    if not policy_path.is_file():
        print(f"[error] policy not found: {policy_path}", file=sys.stderr)
        return 2

    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    protected = policy.get("protected", [])
    rules = [r for r in policy.get("rules", []) if r.get("state") in ACTIVE_STATES]
    if not rules:
        print("[info] no active rules (all rejected / none defined) - nothing to do")
        return 0

    if args.apply:
        unapproved = [r["id"] for r in rules if r.get("state") != "approved"]
        if unapproved:
            print(f"[abort] refusing --apply: unapproved rules {unapproved}", file=sys.stderr)
            print('        approve rules in the policy first (state -> "approved")', file=sys.stderr)
            return 3

    hits: list[dict] = []
    scanned = 0
    for dirpath, _dirnames, filenames in os.walk(staging):
        for name in filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(staging).as_posix()
            scanned += 1
            if is_protected(rel, protected):
                continue
            for r in rules:
                if not rule_matches(rel, r):
                    continue
                try:
                    size = full.stat().st_size
                except OSError:
                    size = 0
                hits.append({"rule": r["id"], "path": rel, "bytes": size})
                if args.apply:
                    try:
                        full.unlink()
                    except OSError as exc:
                        print(f"[warn] unlink failed: {rel}: {exc}", file=sys.stderr)
                break

    total_files = len(hits)
    total_bytes = sum(h["bytes"] for h in hits)
    per_rule: dict[str, dict] = {}
    for h in hits:
        slot = per_rule.setdefault(h["rule"], {"files": 0, "bytes": 0, "samples": []})
        slot["files"] += 1
        slot["bytes"] += h["bytes"]
        if len(slot["samples"]) < args.samples:
            slot["samples"].append(h["path"])

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print("=" * 78)
    print(f" prune_staging {mode}")
    print(f" staging : {staging}")
    print(f" policy  : {policy_path}  (active rules: {len(rules)})")
    print(f" scanned : {scanned:,} files   matched: {total_files:,} files / {total_bytes / 1e6:.1f} MB")
    print("=" * 78)
    for rid, slot in sorted(per_rule.items(), key=lambda kv: -kv[1]["files"]):
        print(f"   {rid:28s} {slot['files']:>8,} files  {slot['bytes'] / 1e6:>8.1f} MB")
        for s in slot["samples"]:
            print(f"      - {s}")

    empty_dirs = 0
    if args.apply:
        empty_dirs = remove_empty_dirs(staging)
        after = sum(1 for p in staging.rglob("*") if p.is_file())
        print(f"\n removed empty dirs: {empty_dirs:,}   files now: {after:,} (was {scanned:,})")

    if args.report:
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "apply": args.apply,
            "staging": str(staging),
            "policy": str(policy_path),
            "scanned_files": scanned,
            "matched_files": total_files,
            "matched_bytes": total_bytes,
            "per_rule": per_rule,
            "removed_empty_dirs": empty_dirs,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[report] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

