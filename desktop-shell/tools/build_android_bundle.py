"""Assemble the Android runtime bundle shipped to the phone app.

The phone cannot execute glibc binaries directly (Android uses bionic), so the
Linux/aarch64 runtime produced by ``build_runtime.py --target android-arm64``
runs inside a PRoot'ed Linux rootfs. This script merges three inputs into one
zip the app downloads on first launch:

    ThinkBuddy-android-runtime-arm64.zip
      runtime-manifest.json   # copied from the runtime zip
      python/                 # python-build-standalone linux-aarch64
      node/                   # node linux-arm64
      rootfs/                 # Linux aarch64 rootfs（Debian bookworm / Ubuntu Base）
      proot                   # static aarch64 PRoot binary (mode 0755)
      android-bundle.json     # phone-side metadata (versions, proot source)

Everything is written straight into the zip: the rootfs tarball is streamed
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
import re
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

# 手机端 rootfs：Debian bookworm arm64。首选 LXC 官方镜像的当日快照——每次构建
# 现读 index 取最新日期目录，而不是把某个时间戳写死（LXC 只保留最近若干天，写死
# 的 URL 过几天就 404）。退路是 Ubuntu Base arm64（cdimage 长期保留，glibc 更新，
# python / node 的 glibc 二进制同样能跑；只是 `/bin/bash` 变成 dash）。
# 注意：不要再指回 debuerreotype/docker-debian-artifacts 的
# `dist-arm64v8/bookworm/rootfs.tar.xz`——该分支已改成 OCI 布局，这个路径 404。
LXC_ROOTFS_INDEX = (
    "https://images.linuxcontainers.org/images/debian/bookworm/arm64/default/"
)
UBUNTU_BASE_INDEXES = (
    "https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/release/",
    "https://cdimage.ubuntu.com/ubuntu-base/releases/22.04/release/",
)
# 静态 aarch64 PRoot：不依赖 Termux 的 libtalloc/libandroid-shmem，解压即用。
# 只钉 v5.3.0：v5.4.0 / v5.4.1 / v5.5.0 的 release 资产只叫 `proot`（按构建机
# 架构出包，拿不到确定是 aarch64 的静态二进制）；v5.3.0 有明确的 aarch64 资产，
# 并用下面的 sha256 固定内容。
PROOT_URLS = (
    "https://github.com/proot-me/proot/releases/download/v5.3.0/proot-v5.3.0-aarch64-static",
)
PROOT_SHA256 = "fa10b1a7818c2f5b1dcb5834450570c368c9ecf66d31521509621b95c4538a45"
ELF_MACHINE_AARCH64 = 0xB7
# GitHub Release 单个资产上限 2 GB（硬限制），超了上传会失败；提前拦住。
GITHUB_ASSET_LIMIT = 2_000_000_000
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


def _fetch_text(url: str, timeout: int = 60) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "ThinkBuddy-build"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def _lxc_rootfs_urls(limit: int = 4) -> list[str]:
    """Newest LXC Debian arm64 rootfs tarballs, newest first."""
    html = _fetch_text(LXC_ROOTFS_INDEX)
    stamps = sorted(set(re.findall(r'href="(\d{8}_\d{2}%3A\d{2})/"', html)), reverse=True)
    if not stamps:
        raise OSError("no dated snapshot directories listed")
    return [f"{LXC_ROOTFS_INDEX}{stamp}/rootfs.tar.xz" for stamp in stamps[:limit]]


def _ubuntu_rootfs_urls() -> list[str]:
    """Newest Ubuntu Base arm64 tarball per supported release (fallback only)."""
    urls: list[str] = []
    for index in UBUNTU_BASE_INDEXES:
        try:
            html = _fetch_text(index)
        except Exception as exc:  # noqa: BLE001 - fallback chain
            log(f"ubuntu-base index unavailable ({index}): {exc}")
            continue
        names = re.findall(r'href="(ubuntu-base-[\d.]+-base-arm64\.tar\.gz)"', html)
        if not names:
            continue
        # 24.04.10 必须排在 24.04.9 之后：按数字段而不是字典序比较。
        names.sort(key=lambda name: [int(part) for part in re.findall(r"\d+", name)],
                   reverse=True)
        urls.append(index + names[0])
    return urls


def _rootfs_candidates() -> list[str]:
    candidates: list[str] = []
    for provider in (_lxc_rootfs_urls, _ubuntu_rootfs_urls):
        try:
            candidates.extend(provider())
        except Exception as exc:  # noqa: BLE001 - try the next provider
            log(f"rootfs provider failed ({provider.__name__}): {exc}")
    return candidates


def _rootfs_probe(archive: Path) -> str | None:
    """Return the guest arch when the tarball is a usable aarch64 rootfs."""
    try:
        with tarfile.open(archive, "r:*") as tar:
            for member in tar:
                name = member.name
                while name.startswith("./"):
                    name = name[2:]
                if name not in ("usr/bin/env", "bin/sh") or not member.isfile():
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    return None
                head = handle.read(20)
                if len(head) < 20 or head[:4] != b"\x7fELF":
                    return None
                if (head[18] | (head[19] << 8)) != ELF_MACHINE_AARCH64:
                    return None
                return "aarch64"
    except (tarfile.TarError, OSError) as exc:
        log(f"rootfs probe failed: {exc}")
    return None


def _download_rootfs(dest: Path) -> Path:
    """Fetch one verified aarch64 rootfs tarball, trying every mirror in order."""
    if dest.is_file() and _rootfs_probe(dest):
        log(f"cached: {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    errors: list[str] = []
    for url in _rootfs_candidates():
        try:
            log(f"downloading rootfs {url} ...")
            request = urllib.request.Request(
                url, headers={"User-Agent": "ThinkBuddy-build"})
            with urllib.request.urlopen(request, timeout=300) as response, \
                    part.open("wb") as handle:
                shutil.copyfileobj(response, handle, 1024 * 1024)
            size = part.stat().st_size
            if size < 5_000_000:
                raise OSError(f"too small ({size} bytes)")
            if not _rootfs_probe(part):
                raise OSError("not an aarch64 rootfs tarball")
            part.replace(dest)
            log(f"downloaded rootfs {size / 1e6:.1f} MB -> {dest.name}")
            return dest
        except Exception as exc:  # noqa: BLE001 - try the next mirror
            errors.append(f"{url}: {exc}")
            part.unlink(missing_ok=True)
    raise SystemExit(
        "no usable arm64 rootfs could be downloaded; tried:\n  "
        + "\n  ".join(errors)
        + "\npass --rootfs <local tar.xz|tar.gz> to build offline."
    )


def _zip_info(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
    # create_system=3（UNIX）让解压端按 external_attr 还原权限/符号链接；
    # 高 16 位放 unix 模式，S_IFLNK 由调用方并进 mode。
    info.create_system = 3
    info.external_attr = (mode & 0xFFFF) << 16
    # writestr(info, ...) 用 ZipInfo 自带的 compress_type，不吃 ZipFile 构造函数
    # 的默认值（ZipInfo 默认 ZIP_STORED）。不显式设置的话 rootfs 会原样存储，
    # 95MB 的 rootfs 膨胀成 ~460MB，整包直接顶到 GitHub 的 2GB 单文件上限。
    info.compress_type = zipfile.ZIP_DEFLATED
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
    """Stream a Linux rootfs tarball (xz or gz) into the zip under ``rootfs/``."""
    count = 0
    with tarfile.open(archive, "r:*") as tar:
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
    rootfs_archive = rootfs or _download_rootfs(CACHE / "linux-aarch64-rootfs.tar")
    if rootfs is not None and not _rootfs_probe(rootfs_archive):
        raise SystemExit(
            f"{rootfs_archive} is not an aarch64 rootfs tarball; pass a Debian or "
            f"Ubuntu arm64 rootfs (tar.xz / tar.gz)."
        )
    proot_binary = proot or _download(
        PROOT_URLS, CACHE / "proot-aarch64-static", 50_000)
    machine = _elf_machine(proot_binary)
    if machine != ELF_MACHINE_AARCH64:
        raise SystemExit(
            f"{proot_binary} is not an aarch64 ELF (e_machine={machine:#x}); "
            f"point --proot at a static aarch64 build."
        )
    proot_sha = hashlib.sha256(proot_binary.read_bytes()).hexdigest()
    if proot is None and PROOT_SHA256 and proot_sha != PROOT_SHA256:
        raise SystemExit(
            f"proot sha256 mismatch: expected {PROOT_SHA256}, got {proot_sha}; "
            f"the mirror served different bytes, refusing to ship it."
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
                "rootfs": "linux-aarch64",
                "rootfs_source": rootfs_archive.name,
                "rootfs_sha256": hashlib.sha256(rootfs_archive.read_bytes()).hexdigest(),
                "rootfs_bytes": rootfs_archive.stat().st_size,
                "proot_sha256": proot_sha,
            }, indent=2),
        )

    digest = hashlib.sha256()
    with out_zip.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    size = out_zip.stat().st_size
    if size > GITHUB_ASSET_LIMIT:
        raise SystemExit(
            f"runtime bundle is {size / 1e6:.0f} MB, over the GitHub Release "
            f"{GITHUB_ASSET_LIMIT / 1e9:.0f} GB per-file limit; trim the shipped "
            f"module set or publish it as split assets."
        )
    log(f"built {out_zip} ({size / 1e6:.1f} MB)")
    log(f"sha256 {digest.hexdigest()}")
    return out_zip


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime-zip", default=str(DIST / "runtime-android-arm64.zip"))
    ap.add_argument("--out", default=str(DIST / "ThinkBuddy-android-runtime-arm64.zip"))
    ap.add_argument("--rootfs", default=None, help="local arm64 rootfs tarball (tar.xz / tar.gz)")
    ap.add_argument("--proot", default=None, help="local static aarch64 proot binary")
    args = ap.parse_args()
    build(
        Path(args.runtime_zip), Path(args.out),
        rootfs=Path(args.rootfs) if args.rootfs else None,
        proot=Path(args.proot) if args.proot else None,
    )


if __name__ == "__main__":
    sys.exit(main())
