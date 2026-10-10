"""Build the offline runtime bundle shipped by the click-installer.

Produces  dist/runtime.zip  containing two relocatable components:

    runtime/
      python/   # python.org 3.12 embeddable distribution with `deeptutor`
                # installed flat into Lib/site-packages (cp312 wheels)
      node/     # portable Node.js 22 LTS (node.exe + npm)

The shell extracts this zip into %LOCALAPPDATA%\\ThinkBuddy\\runtime on
first launch, then runs deeptutor via:
    python/python.exe -c "from deeptutor_cli.main import main; ..."

Both components are relocatable by design (no absolute paths / no venv links),
so end users get a fully offline click-to-install experience.

deeptutor 安装来源是【本地源码】（默认 monorepo 上级目录，可用 --deeptutor-source
覆盖），不再从 PyPI 装 —— PyPI 版本滞后且不含自研修复。安装后跑版本门禁：
staging 里的 deeptutor.__version__ 必须等于本地源 __version__.py，否则构建失败。

Usage:
    python tools/build_runtime.py            # download + build
    python tools/build_runtime.py --clean    # fresh build
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# monorepo 布局：desktop-shell/ 的上级就是 DeepTutor 源码根。
# （壳工程独立放置时可显式传 --deeptutor-source <path> 覆盖。）
MONOREPO_SOURCE = ROOT.parent
STAGE = ROOT / "runtime-build"
CACHE = STAGE / "cache"
STAGING_PY = STAGE / "staging" / "python"
STAGING_NODE = STAGE / "staging" / "node"
DIST = ROOT / "dist"

# pip 源（2026-09-24）：国内直连 pypi.org 极不稳定——实测本机 12s 无响应，
# pip 侧表现为 SSL: UNEXPECTED_EOF_WHILE_READING（重试 5 次全灭，连 PyYAML
# 都拉不到，构建必死）。默认走清华镜像；用户 shell 里显式设置了 PIP_INDEX_URL
# （或代理）时以用户为准（见 install_deeptutor 的 setdefault）。
DEFAULT_PIP_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"


def _read_source_version(source_root: Path) -> str:
    """读本地源 deeptutor/__version__.py 的 __version__（版本门禁的基准值）。"""
    import re
    vf = source_root / "deeptutor" / "__version__.py"
    if not vf.exists():
        raise SystemExit(f"deeptutor source not found at {source_root} "
                         f"(missing {vf}); pass --deeptutor-source <path>")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)', vf.read_text(encoding="utf-8"), re.M)
    if not m:
        raise SystemExit(f"cannot parse __version__ from {vf}")
    return m.group(1)

# Relocatable runtime components (edit to bump versions).
# Windows 用 python.org embeddable + node win-x64 zip；
# macOS 用 python-build-standalone（indygreg）+ node darwin tarball——
# python.org 没有 mac 等价 embeddable 发行版，python-build-standalone 是
# PyInstaller/Relink 生态公认的可重定位 mac Python 来源。
# Mac 通过 --arch 选择 arm64 / x86_64，默认跟随当前构建机架构。
IS_MAC = sys.platform == "darwin"
PY_VER = "3.12.7"
NODE_VER = "v22.22.2"
# --- 先解析 --arch（若在 main 里按 parse 结果再下载就太晚了：URL 在模块加载期确定）---
_arch = "native"
if IS_MAC:
    for _i, _a in enumerate(sys.argv):
        if _a == "--arch":
            if _i + 1 < len(sys.argv):
                _arch = sys.argv[_i + 1]
        elif _a.startswith("--arch="):
            _arch = _a.split("=", 1)[1]
    if _arch == "native":
        _arch = {"x86_64": "x86_64", "arm64": "arm64"}.get(
            platform.machine().lower().replace("amd64", "x86_64"),
            "arm64" if platform.machine().lower() in ("aarch64", "arm64") else "x86_64")
if IS_MAC:
    if _arch == "x86_64":
        # x64 用 x86_64-apple-darwin 的 python-build-standalone + node x64 tarball
        _PY_TAG = f"cpython-{PY_VER}+20241016-x86_64-apple-darwin-install_only"
        _PY_OSNODE = "darwin-x64"
        _AR = "x86_64"
    else:  # arm64
        _PY_TAG = f"cpython-{PY_VER}+20241016-aarch64-apple-darwin-install_only"
        _PY_OSNODE = "darwin-arm64"
        _AR = "aarch64"
    # install_only flavor 只含运行时（无 doc/test），体积小、解包即可用。
    # 解包后布局：python/bin/python3、python/lib/python3.12/...，展平到
    # staging/python/ 即与 Windows embeddable 同 schema（runtime/python/...）。
    PY_URL = (f"https://github.com/indygreg/python-build-standalone/"
              f"releases/download/20241016/{_PY_TAG}.tar.gz")
    PY_ZIP = CACHE / f"{_PY_TAG}.tar.gz"
    NODE_URL = f"https://nodejs.org/dist/{NODE_VER}/node-{NODE_VER}-{_PY_OSNODE}.tar.gz"
    NODE_ZIP = CACHE / f"node-{NODE_VER}-{_PY_OSNODE}.tar.gz"
else:
    PY_URL = f"https://www.python.org/ftp/python/{PY_VER}/python-{PY_VER}-embed-amd64.zip"
    PY_ZIP = CACHE / f"python-{PY_VER}-embed-amd64.zip"
    NODE_URL = f"https://nodejs.org/dist/{NODE_VER}/node-{NODE_VER}-win-x64.zip"
    NODE_ZIP = CACHE / f"node-{NODE_VER}-win-x64.zip"

# A system cp312 interpreter used only to resolve cp312 wheels into the
# embeddable distribution during the *build* (not needed at run time).
def _find_build_py() -> Path | None:
    env = os.environ.get("BUILD_PY")
    if env:
        return Path(env)
    candidates = list(Path.home().glob(
        "AppData/Roaming/uv/python/cpython-3.1[12]-*/python.exe"))
    candidates += list(Path.home().glob(
        ".local/share/uv/python/cpython-3.1[12]-*/python.exe"))
    for c in candidates:
        if c.exists():
            return c
    return None


BUILD_PY = _find_build_py()


def log(msg: str) -> None:
    print(msg, flush=True)


def download(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 1_000_000:
        log(f"cached: {dest.name}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"downloading {url} ...")
    urllib.request.urlretrieve(url, dest)
    log(f"downloaded {dest.stat().st_size / 1e6:.1f} MB")


def extract(zip_path: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    if zip_path.name.endswith(".tar.gz"):
        # macOS：python-build-standalone 与 node darwin 都是 tar.gz
        import tarfile
        with tarfile.open(zip_path, "r:gz") as tf:
            tf.extractall(target)
    else:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(target)
    # strip a possible single wrapper dir (node zip/tarball has one)
    subs = [p for p in target.iterdir() if p.is_dir()]
    if len(subs) == 1 and not list(target.glob("*.exe")) and not list(target.glob("python*.dll")):
        inner = subs[0]
        for item in list(inner.iterdir()):
            shutil.move(str(item), str(target / item.name))
        inner.rmdir()
    log(f"extracted {zip_path.name} -> {target}")


def enable_site(path: Path) -> None:
    """Uncomment `import site` and ensure Lib\\site-packages is on sys.path.

    Windows embeddable 用 ``python*._pth`` 控制 sys.path，默认注释掉
    ``import site``，这里取消注释并追加 ``Lib\\site-packages``。
    macOS 的 python-build-standalone 已启用 site 模块、无 _pth 文件，直接 return。
    """
    if IS_MAC:
        return
    pth = next(path.glob("python*._pth"))
    lines = [l.rstrip() for l in pth.read_text(encoding="utf-8").splitlines()]
    out: list[str] = []
    has_sp, has_site = False, False
    for line in lines:
        if line.strip() == "#import site":
            out.append("import site")
            has_site = True
        else:
            out.append(line)
        if "site-packages" in line:
            has_sp = True
    if not has_sp:
        out.append("Lib\\site-packages")
    if not has_site:
        out.append("import site")
    pth.write_text("\n".join(out) + "\n", encoding="utf-8")
    log(f"enabled site-packages in {pth.name}")


def _py_site_packages(target: Path) -> Path:
    """staging python 树内 site-packages 路径。

    Windows embeddable 是 ``Lib/site-packages``；python-build-standalone 是
    ``lib/python{ver}/site-packages``（与 CPython 标准布局一致）。
    """
    if IS_MAC:
        return target / "lib" / f"python{PY_VER.rsplit('.', 1)[0]}" / "site-packages"
    return target / "Lib" / "site-packages"


def _staging_py_exe(target: Path) -> Path:
    """staging python 树内解释器路径（用于版本门禁/冒烟测试）。"""
    if IS_MAC:
        return target / "bin" / "python3"
    return target / "python.exe"


def install_deeptutor(target: Path, source_root: Path) -> None:
    """pip install --target 把【本地源】的 deeptutor 装进 embeddable 的 site-packages。

    注意：这里刻意安装本地源路径而不是 PyPI 包名 —— PyPI 版本永远滞后于仓库，
    且自研修复根本不会发布到 PyPI（历史教训：打包出来是 1.6.7 而仓库已是 1.6.8）。
    本地源需先构建前端（web/.next/standalone），否则 wheel 里没有 deeptutor_web 数据。
    """
    if BUILD_PY is None or not BUILD_PY.exists():
        raise SystemExit("no cp312 build interpreter found (set BUILD_PY=...py)")
    if not (source_root / "pyproject.toml").exists():
        raise SystemExit(f"--deeptutor-source invalid: {source_root} has no pyproject.toml")
    if not (source_root / "deeptutor_web" / "server.js").exists():
        raise SystemExit(
            f"frontend not built: {source_root / 'deeptutor_web'} lacks server.js.\n"
            f"run:  cd web && npm ci && npm run build && "
            f"python scripts/prepare_web_package.py"
        )
    dest = _py_site_packages(target)
    dest.mkdir(parents=True, exist_ok=True)
    log(f"pip installing deeptutor from LOCAL SOURCE {source_root} ...")
    env = dict(os.environ)
    # setdefault：用户已设置镜像/代理时尊重用户，没有才落默认国内镜像。
    # build isolation（"Installing build dependencies" 阶段）继承本环境，
    # 所以一个 env 同时覆盖依赖解析与构建隔离两层。
    env.setdefault("PIP_INDEX_URL", DEFAULT_PIP_INDEX_URL)
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    env.setdefault("PIP_DEFAULT_TIMEOUT", "60")
    log(f"pip index: {env['PIP_INDEX_URL']}")
    res = subprocess.run(
        [str(BUILD_PY), "-m", "pip", "install", "--upgrade", "--no-compile",
         "--target", str(dest), str(source_root)],
        capture_output=True, text=True, env=env,
    )
    if res.returncode != 0:
        log(res.stdout[-3000:])
        log(res.stderr[-3000:])
        raise SystemExit(f"pip install deeptutor failed (rc={res.returncode})")
    log("deeptutor (local source) installed into embeddable runtime")


def version_gate(target: Path, expected: str) -> str:
    """版本门禁：staging 里实际装上的 deeptutor 版本必须等于本地源版本。

    防止「以为打了新版、其实静默回退到旧版」的事故再次发生。
    """
    py_exe = _staging_py_exe(target)
    # 注意：deeptutor/__init__.py 不 re-export __version__，必须经子模块取
    code = "from deeptutor.__version__ import __version__; print(__version__)"
    res = subprocess.run([str(py_exe), "-c", code], capture_output=True, text=True, timeout=120)
    actual = (res.stdout or "").strip()
    if res.returncode != 0 or actual != expected:
        raise SystemExit(
            f"VERSION GATE FAILED: staging has deeptutor {actual!r} "
            f"but source is {expected!r}.\n"
            f"可能的残留原因：--target 安装叠加了旧 dist-info；"
            f"用 --clean 或 --force-deeptutor 重装。"
        )
    log(f"VERSION GATE OK: deeptutor {actual} == source {expected}")
    return actual


def smoke_test(py_exe) -> None:
    import importlib.util

    code = (
        "import deeptutor_cli, deeptutor, deeptutor_web; "
        "print('runtime imports OK:', deeptutor_cli.__file__)"
    )
    res = subprocess.run([str(py_exe), "-c", code], capture_output=True, text=True, timeout=120)
    log("import check: " + (res.stdout.strip() or res.stderr.strip()))


# Runtime packages that are safe to drop from a packaged install:
#  - litellm           : deeptutor dropped it in v1.0.0-beta.3 (native SDKs); only
#                        pageindex imports it lazily for niche local-chat features
#  - boto3/botocore/...: AWS; only llama_index.core.utilities.aws_utils (lazy) uses it
#  - hf_xet            : optional huggingface_hub download accelerator
#  - bin/              : pip console-script launchers (app runs via run_deeptutor.py)
#  - PyWin32.chm       : pywin32 help file（Windows only）
def PRUNE_GLOBS() -> list[str]:
    globs = [
        "litellm", "litellm-*.dist-info",
        "boto3", "boto3-*.dist-info", "botocore", "botocore-*.dist-info",
        "s3transfer", "s3transfer-*.dist-info",
        "hf_xet", "hf_xet-*.dist-info",
    ]
    if IS_MAC:
        # mac 上 bin/ 是 python-build-standalone 的可执行入口（python3），
        # prune 掉会导致运行时找不到解释器；PyWin32.chm 在 mac 不存在。
        return globs
    globs.extend(["bin", "PyWin32.chm"])
    return globs


def prune_runtime(site_packages: Path) -> None:
    """Remove unneeded heavy packages; log how much room this frees."""
    import glob

    removed_mb = 0.0
    for pat in PRUNE_GLOBS():
        for entry in glob.glob(str(site_packages / pat)):
            p = Path(entry)
            mb = sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e6 if p.is_dir() \
                else p.stat().st_size / 1e6
            removed_mb += mb
            shutil.rmtree(entry, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
            log(f"pruned: {p.name} ({mb:.1f} MB)")
    log(f"prune done, freed ~{removed_mb:.1f} MB")


def _prepared(py: Path, node: Path) -> bool:
    """True if staging already holds an extractable python + node of the right arch."""
    if IS_MAC:
        # python-build-standalone：bin/python3；node darwin tarball：bin/node
        if not ((py / "bin" / "python3").exists() and (node / "bin" / "node").exists()):
            return False
        # 检查 arch marker：切换架构时（arm64 ↔ x86_64）必须重新下载。
        marker = STAGE / ".staging-arch"
        try:
            existing = marker.read_text(encoding="utf-8").strip()
        except Exception:
            return False
        return existing == _arch

    def check(root: Path, marker: str) -> bool:
        if not (root / marker).exists():
            return False
        return any(root.glob("python*.dll")) if root is py else True

    return check(py, "python.exe") and check(node, "node.exe")


SOURCE_PACKAGE_DIRS = ("deeptutor", "deeptutor_cli", "deeptutor_web")
SOURCE_STATE_FILE = STAGE / ".source-state.json"


def source_fingerprint(source_root: Path) -> str:
    """Hash the Python packages shipped into the offline runtime."""
    digest = hashlib.sha256()
    files: list[Path] = []
    for name in SOURCE_PACKAGE_DIRS:
        root = source_root / name
        if not root.exists():
            continue
        files.extend(
            path for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() not in {".pyc", ".pyo"}
        )
    if (source_root / "pyproject.toml").is_file():
        files.append(source_root / "pyproject.toml")

    for path in sorted(files, key=lambda item: item.as_posix()):
        digest.update(path.relative_to(source_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as fh:
            while chunk := fh.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def source_state_changed(source_root: Path) -> bool:
    """Return True when the source package content differs from the last build."""
    current = source_fingerprint(source_root)
    try:
        state = json.loads(SOURCE_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - first build or damaged state file
        return True
    return state.get("fingerprint") != current


def save_source_state(source_root: Path) -> None:
    SOURCE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SOURCE_STATE_FILE.write_text(
        json.dumps({"fingerprint": source_fingerprint(source_root)}),
        encoding="utf-8",
    )


def build(make_zip: bool = False, source_root: Path | None = None,
          force_deeptutor: bool = False) -> None:
    source_root = source_root or MONOREPO_SOURCE
    expected = _read_source_version(source_root)
    log(f"deeptutor source: {source_root} (version {expected})")
    DIST.mkdir(parents=True, exist_ok=True)

    if _prepared(STAGING_PY, STAGING_NODE):
        log("staging under " + str(STAGE) + " already prepared; reusing.")
    else:
        download(PY_URL, PY_ZIP)
        download(NODE_URL, NODE_ZIP)
        extract(PY_ZIP, STAGING_PY)
        extract(NODE_ZIP, STAGING_NODE)
        enable_site(STAGING_PY)
        if IS_MAC:
            (STAGE / ".staging-arch").write_text(_arch, encoding="utf-8")

    # pip install：本地源不存在、版本不一致、或 --force-deeptutor 时重装。
    # pip --target 不会卸旧版本（会叠加 dist-info），所以先清掉旧的 deeptutor*。
    sp = _py_site_packages(STAGING_PY)
    state_changed = source_state_changed(source_root)
    if state_changed:
        log("source package fingerprint changed; reinstalling deeptutor")
    installed_ok = False
    if (sp / "deeptutor").exists():
        py_exe = _staging_py_exe(STAGING_PY)
        res = subprocess.run([str(py_exe), "-c",
                              "from deeptutor.__version__ import __version__; print(__version__)"],
                             capture_output=True, text=True, timeout=120)
        current = (res.stdout or "").strip()
        installed_ok = (
            res.returncode == 0 and current == expected and not state_changed
        )
        if installed_ok and not force_deeptutor:
            log(f"deeptutor {current} already installed & matches source; skipping pip")
        else:
            log(f"reinstalling deeptutor: staging={current!r} source={expected!r} "
                f"force={force_deeptutor}")
            for pat in ("deeptutor", "deeptutor-*.dist-info", "deeptutor_cli",
                        "deeptutor_cli-*.dist-info", "deeptutor_web",
                        "deeptutor_web-*.dist-info"):
                for entry in sp.glob(pat):
                    # 清理失败必须响：ignore_errors 曾在文件被占用（应用未关/
                    # 杀毒扫描）时静默残留旧文件，产出「1.6.10 版本 + 1.6.9
                    # 前端 chunk」的混合运行时且能通过版本门禁（门禁只查
                    # deeptutor.__version__）。宁可构建失败，不可静默混合。
                    try:
                        if entry.is_dir():
                            shutil.rmtree(entry)
                        else:
                            entry.unlink()
                    except OSError as exc:
                        raise SystemExit(
                            f"cannot clean stale {entry.name}: {exc}\n"
                            f"likely held by a running ThinkBuddy/deeptutor "
                            f"process or antivirus — close the app and retry."
                        ) from exc
            install_deeptutor(STAGING_PY, source_root)
    else:
        install_deeptutor(STAGING_PY, source_root)

    smoke_test(_staging_py_exe(STAGING_PY))
    version_gate(STAGING_PY, expected)   # 版本门禁：不过这里直接构建失败
    save_source_state(source_root)

    sp = _py_site_packages(STAGING_PY)
    if (sp / "litellm").exists() or (sp / "boto3").exists():
        prune_runtime(sp)
    else:
        log("runtime already pruned; skipping")
    smoke_test(_staging_py_exe(STAGING_PY))  # re-verify after pruning

    # The portable flow (make_portable.py) packs `runtime-build/staging` as-is,
    # so the zip below is OPT-IN. Deflating thousands of tiny files is slow, so
    # it is skipped unless explicitly requested (e.g. for the onefile/Inno path).
    if not make_zip:
        log("staging ready at " + str(STAGE / "staging") + " (zip skipped)")
        return
    out_zip = DIST / "runtime.zip"
    out_zip.unlink(missing_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr(
            "runtime-manifest.json",
            json.dumps({
                "layout_version": 5,
                "deeptutor_version": expected,
                "source_fingerprint": source_fingerprint(source_root),
            }),
        )
        for root_ in (STAGING_PY, STAGING_NODE):
            base = "python" if root_ is STAGING_PY else "node"
            for f in sorted(root_.rglob("*")):
                if f.is_symlink() and IS_MAC:
                    # python-build-standalone/node tarball 里 bin/python3、bin/node
                    # 是符号链接（指向同目录真实可执行文件）。zipfile 无跨条目
                    # symlink 表达，这里记录目标字符串 + S_IFLNK，解压端负责重建。
                    target = os.readlink(f)
                    info = zipfile.ZipInfo(
                        f"{base}/{f.relative_to(root_).as_posix()}",
                        date_time=(2020, 1, 1, 0, 0, 0),
                    )
                    # S_IFLNK 和权限位都要放在 external_attr 的高 16 位；
                    # 低 16 位是 DOS 属性，放错位置会导致运行端 >> 16 后丢失。
                    info.external_attr = ((stat.S_IMODE(f.lstat().st_mode)
                                           | 0o120000) << 16)
                    zf.writestr(info, target)
                    continue
                if f.is_file():
                    arc = f"{base}/{f.relative_to(root_).as_posix()}"
                    info = zipfile.ZipInfo(arc, date_time=(2020, 1, 1, 0, 0, 0))
                    if IS_MAC:
                        # 只存权限位，不存 S_IFREG：运行端用 attr & 0o120000
                        # 检测符号链接，S_IFREG (0o100000) 与 S_IFLNK (0o120000)
                        # 有位重叠，加了 S_IFREG 的普通文件会被误判为符号链接。
                        info.external_attr = (stat.S_IMODE(f.stat().st_mode) << 16)
                    with open(f, "rb") as fh:
                        zf.writestr(info, fh.read())
    size = out_zip.stat().st_size / 1e6
    log(f"built {out_zip} ({size:.1f} MB)")
    if IS_MAC:
        # macOS 打包段自我验证：把 runtime.zip 按最终用户路径完整走一遍
        # （extract -> 权限/符号链接修复 -> 真实 spawn python3/node）。
        # 这段检测的是「zip 元数据是否正确编码了 unix 模式位/符号链接」——
        # python3 修复链上所有历史 bug（无执行位、python3.12 真身漏修、
        # S_IFLNK 放错位导致 Exec format error）都会在这里直接失败，
        # 而不是等你下载 dmg 后在 mac 上才暴露。
        _self_check_zip_layout(out_zip)


def _self_check_zip_layout(archive: Path) -> None:
    """Zip-level smoke test: extract -> repair -> spawn, exactly like runtime.py.

    与 desktop/runtime.py 的 _repair_runtime_perms 逻辑保持一致（不得漂移）：
      * external_attr >> 16 取 unix 模式位
      * 0o120000 (S_IFLNK)   = 符号链接 -> 重建（python3 -> python3.12）
      * 其余且 mode & 0o111   = 有执行位的普通文件 -> chmod

    先跑一遍修复再做 spawn 检查，能证明产物在用户 mac 上可直接执行。
    """
    tree = DIST / "__zip_selfcheck__"
    if tree.exists():
        shutil.rmtree(tree)
    tree.mkdir(parents=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(tree)
        for info in zf.infolist():
            if info.is_dir():
                continue
            attr = info.external_attr >> 16
            if not attr:
                continue
            name = info.filename
            dest = tree.joinpath(*name.split("/"))
            if not dest.exists():
                continue
            if attr & 0o120000:  # S_IFLNK：重建符号链接
                link_dest = zf.read(info)
                if isinstance(link_dest, bytes):
                    link_dest = link_dest.decode("utf-8")
                dest.unlink()
                dest.symlink_to(link_dest, target_is_directory=False)
                log(f"selfcheck: symlink {name} -> {link_dest}")
            else:  # 普通文件：恢复执行权限位
                mode = attr & 0o777
                if mode & 0o111:
                    dest.chmod(mode)
    py = tree / "python" / "bin" / "python3"
    node = tree / "node" / "bin" / "node"
    if not py.exists():
        raise SystemExit(f"zip self-check: python3 missing at {py} "
                         f"(layout wrong, see build log)")
    if not node.exists():
        raise SystemExit(f"zip self-check: node missing at {node} "
                         f"(layout wrong, see build log)")
    for prog, args in ((py, ["-c", "import sys; print('selfcheck py OK', sys.version)"]),
                       (node, ["--version"])):
        res = subprocess.run([str(prog), *args], capture_output=True, text=True, timeout=120)
        if res.returncode != 0:
            raise SystemExit(
                f"zip self-check FAILED: {prog} {args!r}\n"
                f"stdout: {res.stdout.strip()}\nstderr: {res.stderr.strip()}\n"
                f"产物在用户 mac 上同样无法启动——请检查 zip 的 external_attr/"
                f"符号链接编码。"
            )
        log(f"selfcheck: {' '.join(args) or '--version'} -> "
            + (res.stdout.strip().splitlines()[0] if res.stdout.strip() else "?"))
    shutil.rmtree(tree)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="rebuild staging from scratch")
    ap.add_argument("--no-zip", action="store_true",
                    help="do NOT build dist/runtime.zip (portable flow uses staging dir)")
    ap.add_argument("--deeptutor-source", default=None,
                    help="path to the DeepTutor source root "
                         "(default: monorepo parent of desktop-shell/)")
    ap.add_argument("--force-deeptutor", action="store_true",
                    help="reinstall deeptutor from source even if version matches")
    if IS_MAC:
        ap.add_argument("--arch", choices=["arm64", "x86_64", "native"], default="native",
                        help="macOS 目标架构：下载对应架构的 python-build-standalone + "
                             "node tarball（默认 native 跟随构建机）")
    args = ap.parse_args()
    src = Path(args.deeptutor_source).resolve() if args.deeptutor_source else None
    build(make_zip=not args.no_zip, source_root=src,
          force_deeptutor=args.force_deeptutor)


if __name__ == "__main__":
    main()
