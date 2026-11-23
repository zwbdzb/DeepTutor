"""Assemble the Android runtime bundle shipped to the phone app.

The phone cannot execute glibc binaries directly (Android uses bionic), so the
Linux/aarch64 runtime produced by ``build_runtime.py --target android-arm64``
runs inside a PRoot'ed Debian rootfs. This script merges three inputs into one
zip the app downloads on first launch:

    ThinkBuddy-android-runtime-arm64.zip
      runtime-manifest.json   # copied from the runtime zip
      python/                 # python-build-standalone linux-aarch64
      node/                   # node linux-arm64
      rootfs/                 # Debian bookworm arm64 minirootfs (flattened)
      proot                   # static aarch64 PRoot binary (mode 0755)
      android-bundle.json     # phone-side metadata (versions, proot source)

Everything is written straight into the zip: the rootfs tar.xz is streamed
member by member, so symlinks and mode bits are encoded in ``external_attr``
instead of being materialised on disk (which would need admin rights on
Windows build machines).

Usage:
    python tools/build_android_bundle.py \
        --runtime-zip dist/runtime-android-arm64.zip \
        --out dist/ThinkBuddy-android-runtime-arm64.zip
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
CACHE = ROOT / "runtime-build" / "android-cache"

# 已压缩的 Debian minirootfs（解压后 ~120MB，zip 内再压缩一次）。
ROOTFS_URLS = (
    "https://github.com/debuerreotype/docker-debian-artifacts/raw/"
    "dist-arm64v8/bookworm/rootfs.tar.xz",
)
# 静态 aarch64 PRoot：不依赖 Termux 的 libtalloc/libandroid-shmem，解压即用。
PROOT_URLS = (
    "https://github.com/proot-me/proot/releases/download/v5.4.0/proot-v5.4.0-aarch64-static",
    "https://github.com/proot-me/proot/releases/download/v5.3.0/proot-v5.3.0-aarch64-static",
)
ELF_MACHINE_AARCH64 = 0xB7
_ZIP_DATE = (2020, 1, 1, 0, 0, 0)


def log(message: str) -> None:
    print(message, flush=True)


def _download(urls: tuple[str, ...], dest: Path, min_bytes: int) -> Path:
    if dest.is_file() and dest.stat().st_size >= min_bytes:
        log(f"cached: {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for url in urls:
        try:
            log(f"downloading {url} ...")
            with urllib.request.urlopen(url, timeout=120) as response, \
                    dest.open("wb") as handle:
                shutil.copyfileobj(response, handle)
            if dest.stat().st_size < min_bytes:
                raise OSError(f"too small ({dest.stat().st_size} bytes)")
            log(f"downloaded {dest.stat().st_size / 1e6:.1f} MB")
            return dest
        except Exception as exc:  # noqa: BLE001 - try the next mirror
            errors.append(f"{url}: {exc}")
            dest.unlink(missing_ok=True)
    raise SystemExit("download failed; tried:\n  " + "\n  ".join(errors))


def _elf_machine(path: Path) -> int:
    """e_machine of a little-endian ELF64 header (0xB7 == aarch64)."""
    data = path.read_bytes()[:20]
    if len(data) < 20 or data[:4] != b"\x7fELF":
        return -1
    return data[18] | (data[19] << 8)


def _zip_info(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
    # create_system=3（UNIX）让解压端按 external_attr 还原权限/符号链接；
    # 高 16 位放 unix 模式，S_IFLNK 由调用方并进 mode。
    info.create_system = 3
    info.external_attr = (mode & 0xFFFF) << 16
    return info


def _add_existing_zip(out: zipfile.ZipFile, source: Path) -> str | None:
    """Copy every entry of the runtime zip, keeping its unix mode bits."""
    deeptutor_version: str | None = None
    copied = 0
    with zipfile.ZipFile(source) as src:
        for info in src.infolist():
            if info.is_dir():
                continue
            data = src.read(info)
            if info.filename == "runtime-manifest.json":
                try:
                    deeptutor_version = json.loads(data.decode("utf-8")).get(
                        "deeptutor_version")
                except ValueError:
                    pass
            out.writestr(_zip_info(info.filename, info.external_attr >> 16 or 0o644), data)
            copied += 1
    log(f"copied {copied} runtime entries -> {source.name}")
    return deeptutor_version


def _add_rootfs(out: zipfile.ZipFile, archive: Path) -> int:
    """Stream a Debian rootfs tar.xz into the zip under ``rootfs/``."""
    count = 0
    with tarfile.open(archive, "r:xz") as tar:
        for member in tar:
            name = member.name
            while name.startswith("./"):
                name = name[2:]
            name = name.lstrip("/")
            if not name or name in (".", ".."):
                continue
            arc = f"rootfs/{name}"
            if member.isdir():
                out.writestr(_zip_info(arc + "/", 0o040755), b"")
                continue
            if member.issym():
                # S_IFLNK (0o120000) 与权限位一起放进 external_attr 高 16 位
                info = _zip_info(arc, 0o120777)
                out.writestr(info, member.linkname)
                count += 1
                continue
            if member.islnk():
                # 硬链接：zip 无原生表达，退化为同内容的普通文件。
                extracted = tar.extractfile(member)
                if extracted is not None:
                    out.writestr(_zip_info(arc, member.mode or 0o644), extracted.read())
                    count += 1
                continue
            if not member.isfile():
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            out.writestr(_zip_info(arc, member.mode or 0o644), handle.read())
            count += 1
    log(f"added {count} rootfs entries -> {archive.name}")
    return count


def build(runtime_zip: Path, out_zip: Path, *, rootfs: Path | None = None,
          proot: Path | None = None) -> Path:
    if not runtime_zip.is_file():
        raise SystemExit(
            f"runtime zip not found: {runtime_zip}\n"
            f"run first: python tools/build_runtime.py --target android-arm64"
        )
    rootfs_archive = rootfs or _download(
        ROOTFS_URLS, CACHE / "debian-bookworm-arm64-rootfs.tar.xz", 5_000_000)
    proot_binary = proot or _download(
        PROOT_URLS, CACHE / "proot-aarch64-static", 50_000)
    machine = _elf_machine(proot_binary)
    if machine != ELF_MACHINE_AARCH64:
        raise SystemExit(
            f"{proot_binary} is not an aarch64 ELF (e_machine={machine:#x}); "
            f"point --proot at a static aarch64 build."
        )

    out_zip.parent.mkdir(parents=True, exist_ok=True)
    out_zip.unlink(missing_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as out:
        deeptutor_version = _add_existing_zip(out, runtime_zip)
        _add_rootfs(out, rootfs_archive)
        out.writestr(_zip_info("proot", 0o755), proot_binary.read_bytes())
        out.writestr(
            _zip_info("android-bundle.json", 0o644),
            json.dumps({
                "layout_version": 1,
                "deeptutor_version": deeptutor_version,
                "rootfs": "debian-bookworm-arm64",
                "rootfs_source": rootfs_archive.name,
                "proot_sha256": hashlib.sha256(proot_binary.read_bytes()).hexdigest(),
            }, indent=2),
        )

    digest = hashlib.sha256()
    with out_zip.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    size = out_zip.stat().st_size / 1e6
    log(f"built {out_zip} ({size:.1f} MB)")
    log(f"sha256 {digest.hexdigest()}")
    return out_zip


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime-zip", default=str(DIST / "runtime-android-arm64.zip"))
    ap.add_argument("--out", default=str(DIST / "ThinkBuddy-android-runtime-arm64.zip"))
    ap.add_argument("--rootfs", default=None, help="local Debian arm64 rootfs tar.xz")
    ap.add_argument("--proot", default=None, help="local static aarch64 proot binary")
    args = ap.parse_args()
    build(
        Path(args.runtime_zip), Path(args.out),
        rootfs=Path(args.rootfs) if args.rootfs else None,
        proot=Path(args.proot) if args.proot else None,
    )


if __name__ == "__main__":
    sys.exit(main())
