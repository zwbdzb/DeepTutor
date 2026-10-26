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

# pip 源：默认清华镜像（国内直连 pypi.org 不稳定）；用户 shell 已设置
# PIP_INDEX_URL（或代理）时以用户为准（见 install_deeptutor 的 setdefault）。
DEFAULT_PIP_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"

# 打包时随本地源一起装进离线 runtime 的 pyproject extras。
# math-animator → manim（数学动画/可视化）。刻意不带系统级依赖：
# manim>=0.19 的视频编码走 PyAV（自带 ffmpeg 库，无需系统 ffmpeg）；
# math_animator 的生成 prompt 默认禁用 Tex/MathTex（非 LaTeX 渲染路径），
# 因此运行时无需捆绑 LaTeX 发行版。
# 增删 extras 会改变 dependencies_signature() → 自动触发下次构建全量重装。
PIP_EXTRAS: tuple[str, ...] = ("math-animator",)

# 每个 extra 装进 runtime 后必须可 find_spec 的模块（smoke_test 门禁用）。
# 键必须与 PIP_EXTRAS 一一对应：新增 extra 而不配门禁模块会在构建期直接报错，
# 而不是静默漏装（scipy/av 是 manim>=0.19 的传递依赖，已核实 PyPI 元数据）。
EXTRA_SMOKE_MODULES: dict[str, tuple[str, ...]] = {
    "math-animator": ("manim", "scipy", "av"),
}


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


def _find_ucrt_dlls() -> list[Path]:
    """Locate the Universal C Runtime DLLs on the build machine.

    Returns ucrtbase.dll (implementation) + api-ms-win-crt-*.dll (API-set
    forwarders). python312.dll / python.exe import these forwarders which
    forward to ucrtbase.dll; neither the embeddable distro nor PyInstaller
    ships them, so on a UCRT-less target the runtime python fails to start.
    Returning [] (e.g. building on macOS) is harmless — UCRT is assumed
    present there.
    """
    if IS_MAC:
        return []
    win_dir = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    found: list[Path] = []
    ucrtbase = win_dir / "System32" / "ucrtbase.dll"
    if ucrtbase.is_file():
        found.append(ucrtbase)
    winsxs = win_dir / "WinSxS"
    components = sorted(
        winsxs.glob("amd64_microsoft-windows-m..namespace-downlevel_*"),
        reverse=True,
    )
    seen: set[str] = set()
    for comp in components:
        for dll in comp.glob("api-ms-win-crt-*.dll"):
            if dll.name in seen:
                continue
            seen.add(dll.name)
            found.append(dll)
        if seen:
            break
    return found


def copy_ucrt_into_python(target: Path) -> int:
    """Copy UCRT DLLs next to python.exe so the embeddable runtime is self-contained.

    Without this, runtime/python/python.exe fails on machines lacking the
    Universal C Runtime (same root cause as the shell's "Failed to load
    Python DLL"). Returns the number of DLLs copied.
    """
    if IS_MAC:
        return 0
    dlls = _find_ucrt_dlls()
    copied = 0
    for src in dlls:
        dst = target / src.name
        # Only copy if missing or source is newer; embeddable re-staging is
        # frequent and we don't want to thrash the filesystem.
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dst)
            copied += 1
    if copied:
        log(f"copied {copied} UCRT DLL(s) into {target}")
    else:
        log(f"UCRT DLLs already present in {target}")
    return copied


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


def install_deeptutor(target: Path, source_root: Path, *,
                      with_deps: bool = True,
                      extras: tuple[str, ...] = PIP_EXTRAS) -> None:
    """pip install --target 把【本地源】的 deeptutor 装进 embeddable 的 site-packages。

    注意：刻意安装本地源路径而非 PyPI 包名——PyPI 版本滞后且不含自研修复。
    本地源需先构建前端（web/.next/standalone），否则 wheel 里没有 deeptutor_web 数据。

    with_deps=False（快速路径）：依赖声明未变时只装我们自己的包，跳过 ~500MB
    第三方依赖树的重新解析/下载/复制。
    extras：随本地源一起安装的 pyproject extras（默认 PIP_EXTRAS，即
    math-animator → manim），pip 目标写成 `<source_root>[extra1,extra2]`；
    传 () 则退化为纯本地源安装。--no-deps 快速路径下 extras 照传（--no-deps
    不会装 extras 的依赖树，但两条路径命令保持一致，避免行为漂移）。
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
    desc = "deeptutor + full deps" if with_deps else "deeptutor only (--no-deps fast path)"
    if extras:
        desc += f" + extras[{','.join(extras)}]"
    log(f"pip installing {desc} from LOCAL SOURCE {source_root} ...")
    env = dict(os.environ)
    # setdefault：用户已设置镜像/代理时尊重用户，没有才落默认国内镜像。
    env.setdefault("PIP_INDEX_URL", DEFAULT_PIP_INDEX_URL)
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    env.setdefault("PIP_DEFAULT_TIMEOUT", "60")
    log(f"pip index: {env['PIP_INDEX_URL']}")

    # pip 对本地目录的 extras 语法：D:\path\proj[extra1,extra2]（列表传参，
    # 无 shell，无需引号转义）。
    spec = f"{source_root}[{','.join(extras)}]" if extras else str(source_root)

    def _pip(extra: list[str]) -> subprocess.CompletedProcess:
        cmd = [str(BUILD_PY), "-m", "pip", "install", "--upgrade", "--no-compile",
               "--target", str(dest), *extra, spec]
        return subprocess.run(cmd, capture_output=True, text=True, env=env)

    # --no-build-isolation：BUILD_PY 里已有 setuptools 时跳过每次建临时
    # 构建环境（省 ~30s 且不联网拉 setuptools/wheel）。失败（版本过老等）
    # 自动回退到隔离构建，正确性不受影响。
    if not with_deps:
        res = _pip(["--no-deps", "--no-build-isolation"])
        if res.returncode != 0:
            log("--no-build-isolation failed; retrying isolated build")
            res = _pip(["--no-deps"])
    else:
        res = _pip(["--no-build-isolation"])
        if res.returncode != 0:
            log("--no-build-isolation failed; retrying isolated build")
            res = _pip([])
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


def smoke_test(py_exe: Path) -> None:
    """staging 冒烟测试：自家包可 import + PIP_EXTRAS 关键模块存在。

    extras 门禁用 importlib.util.find_spec 轻量探测（不真正 import manim——
    它导入重且在无显示环境可能引入变数），缺任一声明模块即 SystemExit，
    防止「extras 没装上但整条构建链路静默通过」。PIP_EXTRAS 为空时门禁
    自然跳过（与安装逻辑联动）。
    """
    code = (
        "import deeptutor_cli, deeptutor, deeptutor_web; "
        "print('runtime imports OK:', deeptutor_cli.__file__)"
    )
    res = subprocess.run([str(py_exe), "-c", code], capture_output=True, text=True, timeout=120)
    log("import check: " + (res.stdout.strip() or res.stderr.strip()))

    if not PIP_EXTRAS:
        return
    # {模块: 所属 extra} 映射；extra 缺门禁声明属于配置错误，宁可构建失败，
    # 不可静默跳过校验（与「防静默漏装」同一原则）。
    mapping: dict[str, str] = {}
    for extra in PIP_EXTRAS:
        mods = EXTRA_SMOKE_MODULES.get(extra)
        if not mods:
            raise SystemExit(
                f"EXTRAS GATE CONFIG ERROR: extra {extra!r} has no modules declared "
                f"in EXTRA_SMOKE_MODULES; declare them or drop the extra."
            )
        for mod in mods:
            mapping[mod] = extra
    # find_spec 必须在 staging python 里跑（探测 runtime 的实际安装面）。
    # mapping 以字面量内嵌进 -c 代码，避免命令行传参的引号/转义问题。
    probe = (
        "import importlib.util, json; "
        f"mapping = {mapping!r}; "
        "missing = {m: e for m, e in mapping.items() "
        "if importlib.util.find_spec(m) is None}; "
        "print(json.dumps(missing))"
    )
    res = subprocess.run([str(py_exe), "-c", probe],
                         capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise SystemExit(
            "EXTRAS GATE FAILED: probe script errored in staging python.\n"
            f"{(res.stderr or '').strip()[-2000:]}"
        )
    try:
        missing = json.loads((res.stdout or "").strip() or "{}")
    except ValueError:
        raise SystemExit(
            f"EXTRAS GATE FAILED: cannot parse probe output {res.stdout!r}.\n"
            f"{(res.stderr or '').strip()[-2000:]}"
        )
    if missing:
        detail = "; ".join(f"{mod} (extra: {extra})"
                           for mod, extra in sorted(missing.items()))
        raise SystemExit(
            f"EXTRAS GATE FAILED: modules missing from runtime: {detail}.\n"
            f"extras {list(PIP_EXTRAS)} 未装进 runtime（漏装或被 prune 误删）；"
            f"用 --clean 或 --force-deeptutor 触发全量重装。"
        )
    log(f"extras gate OK: {sorted(mapping)} present for extras {list(PIP_EXTRAS)}")


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


def precompile_site_packages(target: Path) -> None:
    """打包期预编译 staging site-packages 全量 .pyc（2026-10-24 首启提速）。

    背景：pip --no-compile 装出的是纯源码树，原样打进安装包后，用户机每次
    首启（更新后尤甚）都要现场编译全部字节码——用户机 1.6.15 更新事故实证：
    后端导入 4 分钟以上未监听，撞穿 launcher 60s 就绪超时，壳弹「启动失败」
    横幅。改为构建期用 staging 自带解释器（cp312）编译，pyc 随包分发；
    Inno / ditto 均保留文件 mtime，pyc 的 mtime+size 校验跨机依然有效，
    用户机首启不再付编译成本。

    幂等：compileall 对已是最新（mtime+size 匹配）的 pyc 直接跳过，增量
    重跑只编译缺失/失效的文件，rebrand 改写后的二次调用成本可忽略。

    个别文件编译失败只意味着它没有 pyc（运行时回退源码导入，与
    --no-compile 现状等价），记录后继续，不让整个构建失败。
    """
    sp = _py_site_packages(target)
    if not sp.is_dir():
        log("precompile: site-packages missing, skip")
        return
    py_exe = _staging_py_exe(target)
    log(f"precompiling bytecode for {sp} ...")
    res = subprocess.run(
        [str(py_exe), "-m", "compileall", "-q", "-j", "0", str(sp)],
        capture_output=True, text=True, timeout=1800,
    )
    if res.returncode != 0:
        log(f"precompile finished with errors (rc={res.returncode}); "
            f"tail: {(res.stderr or res.stdout or '').strip()[-800:]}")
    else:
        log("precompile OK")


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


def _iter_source_files(source_root: Path) -> list[Path]:
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
    return sorted(files, key=lambda item: item.as_posix())


def source_fingerprint(source_root: Path, *, strict: bool = False) -> str:
    """Fingerprint the Python packages shipped into the offline runtime.

    默认 size+mtime（编辑/git checkout 必更新 mtime，漏检不可达；且有版本门禁
    + 冒烟测试兜底）。strict=True 时逐文件 sha256（--strict-fingerprint，发布前
    人工核验）。模式不一致视为变更 → 触发重装（安全方向）。
    """
    digest = hashlib.sha256()
    digest.update(b"mode=" + (b"strict" if strict else b"fast") + b"\0")
    for path in _iter_source_files(source_root):
        digest.update(path.relative_to(source_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        st = path.stat()
        digest.update(str(st.st_size).encode("utf-8"))
        digest.update(b"\0")
        if strict:
            with path.open("rb") as fh:
                while chunk := fh.read(1024 * 1024):
                    digest.update(chunk)
        else:
            digest.update(str(st.st_mtime_ns).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _dependency_relevant_payload(source_root: Path) -> bytes | None:
    """提取 pyproject.toml 中真正影响依赖安装面的字段（canonical JSON 编码）。

    整文件哈希的教训（2026-10-24 CI 实证，macOS x86_64 job 11m11s）：pyproject
    里的 ruff/black/pytest/mypy/bandit 工具配置、description、scripts、
    entry-points 与依赖安装毫无关系，但任何一个字符的改动都会让 deps_sig 变化
    → CI 缓存 restore-keys 恢复旧 staging 后 site-packages 被整体清空 + 全量
    重装（Intel runner 上多花 5-7 分钟）。改为只哈希 pip 对
    `<source>[PIP_EXTRAS]` 这条安装命令实际解析的字段：

      [build-system].requires / .build-backend   — 隔离构建环境的内容
      [project].requires-python                  — 环境标记求值基准
      [project].dependencies                     — 主依赖集
      [project.optional-dependencies][extra]     — 仅 PIP_EXTRAS 声明的 extra

    其余段落（工具配置等）的改动只影响源码指纹（pyproject.toml 本身在
    _iter_source_files 里），走 --no-deps 快速路径秒装，不再触发全量重装。
    解析失败（tomllib 不可用 / TOML 语法错误）返回 None，调用方回退整文件
    哈希——保守方向多重装，绝不漏装。
    """
    try:
        import tomllib
    except ImportError:
        return None
    pyproject = source_root / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - malformed TOML: fall back to whole file
        return None
    build_system = data.get("build-system", {}) or {}
    project = data.get("project", {}) or {}
    optional = project.get("optional-dependencies", {}) or {}
    payload = {
        "build_backend": build_system.get("build-backend", ""),
        "build_requires": sorted(build_system.get("requires", []) or []),
        "requires_python": project.get("requires-python", ""),
        "dependencies": sorted(project.get("dependencies", []) or []),
        # PIP_EXTRAS 里声明了但 pyproject 没定义的 extra 也计入：这种情况
        # pip 本身会报错，先让签名变化触发重装、尽早失败，好过静默漏装。
        "extras": {extra: sorted(optional[extra] or [])
                   for extra in PIP_EXTRAS if extra in optional},
        "missing_extras": sorted(e for e in PIP_EXTRAS if e not in optional),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")


def dependencies_signature(source_root: Path) -> str:
    """依赖安装面签名——区分「只改了代码」（--no-deps 快速路径）与「依赖
    声明/安装面变了」（全量安装）。

    哈希输入见 _dependency_relevant_payload（2026-10-24 起为依赖段提取，
    不再是整文件字节——工具配置改动不再误伤增量缓存）。尾部固定 marker
    双保险：
      * extras 标识：PIP_EXTRAS 增删 → 签名变 → 旧 .source-state.json 失效
        → 下次构建自动全量重装（否则改了 extras 但签名不变，manim 等会被
        静默漏装）；
      * 算法版本 marker：签名算法升级后新旧签名必然不同 → 旧状态文件失效
        一次（安全方向，一次性全量重装后归位）。
    """
    pyproject = source_root / "pyproject.toml"
    if not pyproject.is_file():
        return ""
    payload = _dependency_relevant_payload(source_root)
    if payload is None:
        # tomllib 不可用或 TOML 解析失败：回退整文件哈希（旧行为，保守方向）。
        payload = pyproject.read_bytes()
    digest = hashlib.sha256()
    digest.update(payload)
    digest.update(b"\0extras=" + ",".join(PIP_EXTRAS).encode("utf-8"))
    digest.update(b"\0algo=deps-only-v1")
    return digest.hexdigest()


def _load_source_state() -> dict:
    try:
        return json.loads(SOURCE_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - first build or damaged state file
        return {}


def save_source_state(source_root: Path, fingerprint: str, deps_sig: str,
                      mode: str) -> None:
    SOURCE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SOURCE_STATE_FILE.write_text(
        json.dumps({"mode": mode, "fingerprint": fingerprint,
                    "deps_sig": deps_sig}),
        encoding="utf-8",
    )


def _rebrand_staging(site_packages: Path) -> None:
    """调用 tools/rebrand.py 对 staging site-packages 做品牌重写 + 安全自检。

    通过文件路径加载，避免对包结构的依赖；self_check 不过会 SystemExit，
    直接让构建失败（宁可失败，不可把未换牌运行时发出去）。
    """
    import importlib.util
    rebrand_py = Path(__file__).resolve().parent / "rebrand.py"
    spec = importlib.util.spec_from_file_location("rebrand", rebrand_py)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    log("rebranding staging before zip...")
    mod.rebrand_root(site_packages)
    mod.self_check(site_packages)


def build(make_zip: bool = False, source_root: Path | None = None,
          force_deeptutor: bool = False, strict_fingerprint: bool = False) -> None:
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

    # 把 UCRT（ucrtbase.dll + api-ms-win-crt-*.dll）拷进 staging/python/，
    # 与 python.exe 同目录。embeddable Python 自带不包含 UCRT，目标机器若缺
    # 少它（Win7/8 无 KB2999226、精简 LTSC 等），runtime/python 会无法启动。
    # 每次构建都跑：copy_ucrt_into_python 内部按存在性+size 去重，无副作用。
    copy_ucrt_into_python(STAGING_PY)

    # pip install 决策：源码指纹变 → 重装（deps 未变走 --no-deps 快速路径）；
    # 依赖签名变 → 全量重装。指纹模式（fast/strict）变化也算变更（安全方向）。
    fp = source_fingerprint(source_root, strict=strict_fingerprint)
    deps_sig = dependencies_signature(source_root)
    mode = "strict" if strict_fingerprint else "fast"
    state = _load_source_state()
    state_changed = (state.get("mode") != mode
                     or state.get("fingerprint") != fp)
    deps_changed = state.get("deps_sig") != deps_sig
    if state_changed:
        log("source package fingerprint changed; reinstalling deeptutor")
    if deps_changed:
        log("dependency signature changed (or first build); full install")

    sp = _py_site_packages(STAGING_PY)

    # 依赖签名变更 → 全量重装。此时先整体清空 site-packages：CI 缓存
    # restore-keys 恢复的旧 staging 树里，被从 pyproject 移除的第三方依赖会残留
    # （pip --target 覆盖式安装只增不删，残包会随运行时一起发到用户机器）。
    # 全量安装本来就要装齐所有包，先删零成本——pip 下载缓存命中，重装远快于
    # 冷装。仅源码变更（state_changed、deps 未变）走 --no-deps 快速路径时
    # 【绝不】清空：旧树的第三方依赖正是快速路径秒装的本钱。
    if deps_changed and sp.exists():
        log("dependency signature changed: wiping stale site-packages "
            "(deps removed from pyproject would otherwise ship as residue)")
        try:
            shutil.rmtree(sp)
        except OSError as exc:
            raise SystemExit(
                f"cannot wipe stale site-packages {sp}: {exc}\n"
                f"likely held by a running ThinkBuddy/deeptutor process or "
                f"antivirus — close the app and retry."
            ) from exc
        # 显式重建空目录（install_deeptutor 内部 mkdir 兜底，这里让语义可见）。
        sp.mkdir(parents=True, exist_ok=True)

    did_install = False
    if (sp / "deeptutor").exists():
        py_exe = _staging_py_exe(STAGING_PY)
        res = subprocess.run([str(py_exe), "-c",
                              "from deeptutor.__version__ import __version__; print(__version__)"],
                             capture_output=True, text=True, timeout=120)
        current = (res.stdout or "").strip()
        installed_ok = (
            res.returncode == 0 and current == expected
            and not state_changed and not deps_changed
        )
        if installed_ok and not force_deeptutor:
            log(f"deeptutor {current} already installed & matches source; skipping pip")
        else:
            log(f"reinstalling deeptutor: staging={current!r} source={expected!r} "
                f"force={force_deeptutor} deps_changed={deps_changed}")
            for pat in ("deeptutor", "deeptutor-*.dist-info", "deeptutor_cli",
                        "deeptutor_cli-*.dist-info", "deeptutor_web",
                        "deeptutor_web-*.dist-info"):
                for entry in sp.glob(pat):
                    # 清理失败必须响：ignore_errors 会静默残留旧文件，产出混合运行时
                    # 且能通过版本门禁。宁可构建失败，不可静默混合。
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
            # deps_changed=True 的场景已在上方清空 site-packages 并转入下方
            # else 分支全量安装；能走到这里的 reinstall 都是 deps 未变的
            # --no-deps 快速路径（只重装自家包，第三方树原样保留）。
            install_deeptutor(STAGING_PY, source_root, with_deps=deps_changed)
            did_install = True
    else:
        install_deeptutor(STAGING_PY, source_root, with_deps=True)
        did_install = True

    # 冒烟测试（重量级 import，embeddable 下 ~10-40s）只在真装过东西后跑；
    # 版本门禁（轻量，只 import __version__）每次都跑，作为 staging 存活证明。
    if did_install:
        smoke_test(_staging_py_exe(STAGING_PY))
    version_gate(STAGING_PY, expected)   # 版本门禁：不过这里直接构建失败
    save_source_state(source_root, fp, deps_sig, mode)

    if did_install:
        sp = _py_site_packages(STAGING_PY)
        if (sp / "litellm").exists() or (sp / "boto3").exists():
            prune_runtime(sp)
            smoke_test(_staging_py_exe(STAGING_PY))  # re-verify after pruning
        else:
            log("runtime already pruned; skipping")

    # 预编译字节码（no-zip 路径在此收口；Windows 的 build.ps1 随后跑
    # rebrand.py 会改写部分 .py 使 pyc 失效，rebrand_root 末尾会增量重编译）。
    precompile_site_packages(STAGING_PY)

    # The portable flow (make_portable.py) packs `runtime-build/staging` as-is,
    # so the zip below is OPT-IN. Deflating thousands of tiny files is slow, so
    # it is skipped unless explicitly requested (e.g. for the onefile/Inno path).
    if not make_zip:
        log("staging ready at " + str(STAGE / "staging") + " (zip skipped)")
        return

    # 打 zip 前强制重刷品牌（rebrand 幂等）。此前 rebrand 在本脚本之后才跑，
    # zip 里装的是换牌前的旧内容——曾致 mac dmg 品牌混杂（2026-10-19）。
    # 必须在 zip 之前执行，失败即中止构建。
    _rebrand_staging(_py_site_packages(STAGING_PY))
    # zip 路径兜底：rebrand_root 末尾已预编译，这里幂等重跑（增量，秒级），
    # 保证打进 runtime.zip 的树 pyc 齐全——zip 是 mac .app 的运行时事实源。
    precompile_site_packages(STAGING_PY)

    out_zip = DIST / "runtime.zip"
    out_zip.unlink(missing_ok=True)
    # compresslevel=1 而非 6：这个 zip 只存在于 mac .app 内部（Windows 的
    # build.ps1 默认 --no-zip、CI 不产 zip），而 DMG 的 hdiutil -format UDZO
    # 会对整个镜像再压一遍——zip 内的高级压缩纯属白费 CPU（双重压缩）。
    # level 1 让 zip 步骤快 2-3 倍、首启解压也更快；最终 dmg 体积基本不变
    # （UDZO 对低级别压缩/未压缩数据照样压得动）。
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        zf.writestr(
            "runtime-manifest.json",
            json.dumps({
                "layout_version": 5,
                "deeptutor_version": expected,
                "source_fingerprint": fp,
                "source_fingerprint_mode": mode,
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
    ap.add_argument("--strict-fingerprint", action="store_true",
                    help="content-hash source files instead of mtime+size "
                         "(slower; use before a release for extra assurance)")
    if IS_MAC:
        ap.add_argument("--arch", choices=["arm64", "x86_64", "native"], default="native",
                        help="macOS 目标架构：下载对应架构的 python-build-standalone + "
                             "node tarball（默认 native 跟随构建机）")
    args = ap.parse_args()
    src = Path(args.deeptutor_source).resolve() if args.deeptutor_source else None
    build(make_zip=not args.no_zip, source_root=src,
          force_deeptutor=args.force_deeptutor,
          strict_fingerprint=args.strict_fingerprint)


if __name__ == "__main__":
    main()
