# -*- coding: utf-8 -*-
"""实测 build_runtime.source_fingerprint 的耗时（复刻同算法，只读）。"""
import hashlib
import sys
import time
from pathlib import Path

ROOT = Path(r"D:\studio\DeepTutor")
DIRS = ("deeptutor", "deeptutor_cli", "deeptutor_web")

t0 = time.perf_counter()
files = []
for name in DIRS:
    root = ROOT / name
    if root.exists():
        files.extend(
            p for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() not in {".pyc", ".pyo"}
        )
if (ROOT / "pyproject.toml").is_file():
    files.append(ROOT / "pyproject.toml")

t1 = time.perf_counter()
digest = hashlib.sha256()
total_bytes = 0
for path in sorted(files, key=lambda i: i.as_posix()):
    digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
    digest.update(b"\0")
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
            total_bytes += len(chunk)
    digest.update(b"\0")
t2 = time.perf_counter()

print(f"files={len(files)}  bytes={total_bytes/1e6:.1f} MB")
print(f"walk={t1-t0:.1f}s  hash={t2-t1:.1f}s  total={t2-t0:.1f}s")
print(f"throughput={total_bytes/1e6/(t2-t1):.0f} MB/s")
print(f"fp={digest.hexdigest()[:16]}... (stored=728936c14b32bb1d...)")
changed = digest.hexdigest() != "728936c14b32bb1d11d67708ddccb78b73e900e44f7a42dcfa61283299f955db"
print(f"changed_vs_stored={changed}")
