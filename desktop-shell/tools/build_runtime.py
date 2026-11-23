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

v1.7.2 起上游把 deeptutor 拆成「核心 + 每模块一个 distribution」。离线 runtime 因此
不再做单一 `pip install <源>`（那样只会得到 core，没有 deeptutor-server /
deeptutor-web 等模块元数据，用户装完 `deeptutor start` 第一行
require_module("server") 就判死），改为：

    build_runtime -> 随包模块集合(pyproject[full] + PIP_EXTRAS -> 模块清单)
                  -> 本地 wheelhouse（上游 scripts/build_modules.py 切代码）
                  -> pip install --target <全量随包 wheel>
                  -> module_gate（每个随包模块必须 installed/enabled/版本一致）

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
import re
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
# 本地 wheelhouse：随包模块 wheel 的构建产物，install_deeptutor 用 --find-links
# 指向它。只做安装中间产物，装完即删（见 build() 里的 _drop_wheelhouse）。
WHEELHOUSE = STAGE / "wheelhouse"
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

# 随包模块集合的两块来源（见 _shipped_selection）：
#   * pyproject [tool.deeptutor.optional-dependencies]["full"] —— 官方安装档，
#     当前 19 个 deeptutor-* distribution；
#   * PIP_EXTRAS —— 额外的 feature 档（math-animator → deeptutor-renderer-manim）。
# 两者里的 deeptutor-* distribution 按 manifest 映射成模块 id，再补齐
# manifest.requires 闭包；`core` 永远随包。模块数不硬编码，一律以 pyproject +
# 清单文件为准（新增模块只要能进 full 就自动随包，不必改本文件）。
FULL_PROFILE = "full"

# deeptutor_web 是 Next.js standalone 产物（数千个文件），自写 data wheel 打包。
WEB_MODULE = "web"

# 每个随包模块之外的启动面依赖：`deeptutor start` 真正 import 的服务端栈。
# module_gate 用 find_spec 轻量探测（不 import，避免无显示/无网络环境的变数）。
DESKTOP_SMOKE_PACKAGES: tuple[str, ...] = ("fastapi", "uvicorn", "starlette")

# wheel 内 zip 条目的固定时间戳：同一份源码打出的 wheel 字节可复现。
_ZIP_DATE = (2020, 1, 1, 0, 0, 0)

# 每个 extra 装进 runtime 后必须可 find_spec 的模块（smoke_test 门禁用）。
# 键必须与 PIP_EXTRAS 一一对应：新增 extra 而不配门禁模块会在构建期直接报错，
# 而不是静默漏装（scipy/av 是 manim>=0.19 的传递依赖，已核实 PyPI 元数据）。
EXTRA_SMOKE_MODULES: dict[str, tuple[str, ...]] = {
    "math-animator": ("manim", "scipy", "av"),
}


def _read_source_version(source_root: Path) -> str:
    """读本地源 deeptutor/__version__.py 的 __version__（版本门禁的基准值）。"""
    vf = source_root / "deeptutor" / "__version__.py"
    if not vf.exists():
        raise SystemExit(f"deeptutor source not found at {source_root} "
                         f"(missing {vf}); pass --deeptutor-source <path>")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)', vf.read_text(encoding="utf-8"), re.M)
    if not m:
        raise SystemExit(f"cannot parse __version__ from {vf}")
    return m.group(1)


def _distinfo_metadata_version(dist: Path) -> str | None:
    """读 dist-info/METADATA 的 Version 字段；读不到或解析失败返回 None。"""
    meta = dist / "METADATA"
    if not meta.is_file():
        return None
    try:
        for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Version:"):
                value = line.split(":", 1)[1].strip()
                return value or None
    except OSError:
        return None
    return None


MANIFEST_DIR = "deeptutor/modules/manifests"
# 需求行的包名 = 开头的 distribution 名（去掉 extras / 版本 / 环境标记）。
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _load_manifests(source_root: Path) -> dict[str, dict]:
    """Read every module manifest in the source tree, keyed by module id.

    Manifests are the single source of truth for what a module owns
    (``paths``), which distributions it depends on (``requires``) and which
    third-party packages it needs (``requirements``) — nothing here is
    hard-coded, so a module added upstream ships without touching this file.
    """
    directory = source_root / MANIFEST_DIR
    if not directory.is_dir():
        raise SystemExit(
            f"module manifests not found at {directory}; --deeptutor-source must "
            f"point at a 1.7.2+ source tree that ships the modular runtime"
        )
    result: dict[str, dict] = {}
    for path in sorted(directory.glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        result[row["id"]] = row
    if "core" not in result:
        raise SystemExit(f"module manifests in {directory} declare no core module")
    return result


def _optional_dependencies(source_root: Path, version: str) -> dict[str, list[str]]:
    """Resolve ``[tool.deeptutor.optional-dependencies]`` with ``{version}`` filled.

    The table is dynamic in pyproject (``dynamic = ["version",
    "optional-dependencies"]``), i.e. rows carry a ``{version}`` placeholder
    that the upstream build substitutes. Note it lives under
    ``[tool.deeptutor]``, not ``[project]`` — reading the wrong table is how an
    extras-driven dependency change would silently keep the fast path.
    """
    try:
        import tomllib
    except ImportError as exc:  # pragma: no cover - build python is 3.11+
        raise SystemExit("build_runtime.py needs tomllib (Python 3.11+)") from exc
    pyproject = source_root / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"cannot read {pyproject}: {exc}") from exc
    except ValueError as exc:
        raise SystemExit(f"cannot parse {pyproject}: {exc}") from exc
    table = ((data.get("tool") or {}).get("deeptutor") or {}).get(
        "optional-dependencies"
    ) or {}
    return {
        str(name): [str(raw).format(version=version) for raw in rows or []]
        for name, rows in table.items()
    }


def _requirement_name(raw: str) -> str:
    """Bare distribution name of a requirement line (no extras, version or marker)."""
    match = _REQUIREMENT_NAME.match(raw)
    return match.group(1) if match else ""


def _shipped_selection(source_root: Path) -> tuple[list[str], list[str]]:
    """Return ``(module ids in dependency order, extra pip requirements)``.

    The shipped surface is pyproject's ``full`` profile plus ``PIP_EXTRAS``,
    mapped to module ids through each manifest's ``distribution``. Every
    ``requires`` edge is closed over so no shipped module can reference a
    module that is not shipped (``deeptutor start`` gates on
    ``require_module("server")`` and ``("web")``, whose closures must be whole).
    """
    version = _read_source_version(source_root)
    manifests = _load_manifests(source_root)
    extras = _optional_dependencies(source_root, version)
    by_distribution = {row["distribution"]: name for name, row in manifests.items()}
    missing_extras = [extra for extra in ACTIVE_EXTRAS if extra not in extras]
    if missing_extras:
        raise SystemExit(
            f"ACTIVE_EXTRAS declares {missing_extras} but pyproject defines no such "
            f"extra; fix PIP_EXTRAS or the pyproject table."
        )
    requested = list(extras.get(FULL_PROFILE, []))
    for extra in ACTIVE_EXTRAS:
        requested.extend(extras[extra])
    if not requested:
        raise SystemExit(
            f"pyproject defines no deeptutor[{FULL_PROFILE}] profile and PIP_EXTRAS "
            f"is empty: refusing to build a runtime with no feature modules."
        )
    modules: list[str] = []
    requirements: list[str] = []
    for raw in requested:
        name = _requirement_name(raw)
        module = by_distribution.get(name)
        if module is None:
            if name.startswith("deeptutor"):
                raise SystemExit(
                    f"pyproject ships {name!r} but no module manifest declares that "
                    f"distribution; the runtime cannot own it."
                )
            requirements.append(raw)  # a plain pip dependency, not a module
        elif module not in modules:
            modules.append(module)

    ordered: list[str] = []

    def visit(name: str) -> None:
        if name in ordered:
            return
        for dependency in manifests[name]["requires"]:
            visit(dependency)
        ordered.append(name)

    for name in modules:
        visit(name)
    # core 永远随包（它是 `deeptutor` 本身）。full 档通常不写它，个别 extra 会
    # 写成 `deeptutor[xxx]`，所以先摘掉再统一置顶，避免重复。
    if "core" in ordered:
        ordered.remove("core")
    return ["core", *ordered], list(dict.fromkeys(requirements))


def shipped_modules(source_root: Path) -> list[str]:
    """Module ids that ship in the desktop runtime, in dependency order."""
    return _shipped_selection(source_root)[0]


def _wheel_path(manifest: dict, version: str) -> Path:
    """Where a module's ``py3-none-any`` wheel lands inside the wheelhouse."""
    return WHEELHOUSE / f"{manifest['distribution'].replace('-', '_')}-{version}-py3-none-any.whl"


def _build_data_wheel(source_root: Path, manifests: dict[str, dict],
                      name: str, version: str) -> Path:
    """Write the web module wheel by hand: pure data, stored, no setuptools.

    ``deeptutor_web`` is the Next.js standalone payload — thousands of small
    files, ~90 MB. setuptools has to walk them as package-data through a temp
    staging tree (measured at 59 minutes); a wheel is just a ZIP with a
    dist-info, so writing it directly with ``ZIP_STORED`` takes ~10 s on a build
    machine (≈3500 files, 88 MB stored) and installs identically through pip.
    """
    manifest = manifests[name]
    root = source_root / "deeptutor_web"
    if not (root / "server.js").is_file():
        raise SystemExit(
            f"frontend not built: {root} lacks server.js.\n"
            f"run:  cd web && npm ci && npm run build && "
            f"python scripts/prepare_web_package.py"
        )
    files = sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix.lower() not in {".pyc", ".pyo"}
        ),
        key=lambda item: item.as_posix(),
    )
    if not files:
        raise SystemExit(f"frontend payload {root} is empty")

    # Requires-Dist 顺序与 scripts/build_modules.py 保持一致：本模块三方需求，
    # 然后是核心，然后是 requires 里每个模块的 distribution（同版本锁定）。
    dependencies = list(manifest["requirements"])
    dependencies.append(f"deeptutor=={version}")
    dependencies.extend(
        f"{manifests[dependency]['distribution']}=={version}"
        for dependency in manifest["requires"]
    )
    dist_info = f"{manifest['distribution'].replace('-', '_')}-{version}.dist-info"
    metadata = "\n".join([
        "Metadata-Version: 2.1",
        f"Name: {manifest['distribution']}",
        f"Version: {version}",
        f"Summary: DeepTutor {name} module",
        "Requires-Python: >=3.11,<3.15",
        *(f"Requires-Dist: {raw}" for raw in dependencies),
    ]) + "\n"
    wheel_metadata = "\n".join([
        "Wheel-Version: 1.0",
        "Generator: deeptutor-desktop build_runtime",
        "Root-Is-Purelib: true",
        "Tag: py3-none-any",
    ]) + "\n"

    wheel = _wheel_path(manifest, version)
    wheel.unlink(missing_ok=True)
    written: list[str] = []
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_STORED) as archive:
        for path in files:
            arcname = path.relative_to(source_root).as_posix()
            archive.writestr(zipfile.ZipInfo(arcname, date_time=_ZIP_DATE),
                             path.read_bytes())
            written.append(arcname)
        for arcname, text in (
            (f"{dist_info}/METADATA", metadata),
            (f"{dist_info}/WHEEL", wheel_metadata),
            (f"{dist_info}/top_level.txt", "deeptutor_web\n"),
        ):
            archive.writestr(zipfile.ZipInfo(arcname, date_time=_ZIP_DATE),
                             text.encode("utf-8"))
            written.append(arcname)
        # RECORD 必须列全（含自己）；hash/size 留空是 wheel 规范允许的形式，
        # pip 安装时会按实际文件重建 site-packages 里的 RECORD。
        record = "".join(f"{arcname},,\n" for arcname in written)
        record += f"{dist_info}/RECORD,,\n"
        archive.writestr(zipfile.ZipInfo(f"{dist_info}/RECORD", date_time=_ZIP_DATE),
                         record.encode("utf-8"))
    log(f"built data wheel {wheel.name} ({len(files)} files, "
        f"{wheel.stat().st_size / 1e6:.1f} MB)")
    return wheel


def build_wheelhouse(source_root: Path, modules: list[str]) -> dict[str, Path]:
    """Build one wheel per shipped module into ``WHEELHOUSE`` → ``{module: wheel}``.

    Code splitting is upstream's job: ``scripts/build_modules.py`` derives file
    ownership from the manifests, builds disjoint wheels and fails on overlap
    (one installed file must have exactly one owner). Only ``web`` is built
    here, as a data wheel — see ``_build_data_wheel``.
    """
    version = _read_source_version(source_root)
    manifests = _load_manifests(source_root)
    builder = source_root / "scripts" / "build_modules.py"
    if not builder.is_file():
        raise SystemExit(f"module wheel builder missing: {builder}")
    if BUILD_PY is None or not BUILD_PY.exists():
        raise SystemExit("no cp312 build interpreter found (set BUILD_PY=...py)")

    # 每次重建：wheelhouse 里绝不允许出现「上一版源码」的 wheel —— CI 缓存
    # 恢复出旧 wheelhouse 时，陈旧的模块代码会被静默装进新运行时。
    if WHEELHOUSE.exists():
        shutil.rmtree(WHEELHOUSE)
    WHEELHOUSE.mkdir(parents=True, exist_ok=True)

    native = [name for name in modules if name != WEB_MODULE]
    command = [str(BUILD_PY), str(builder), "--output", str(WHEELHOUSE)]
    for name in native:
        command += ["--module", name]
    log(f"building {len(native)} module wheels into {WHEELHOUSE} ...")
    result = subprocess.run(command, capture_output=True, text=True, timeout=3600)
    if result.returncode != 0:
        log(result.stdout[-3000:])
        log(result.stderr[-3000:])
        raise SystemExit(f"module wheel build failed (rc={result.returncode})")
    wheels: dict[str, Path] = {}
    for name in modules:
        if name == WEB_MODULE:
            wheels[name] = _build_data_wheel(source_root, manifests, name, version)
            continue
        wheel = _wheel_path(manifests[name], version)
        if not wheel.is_file():
            raise SystemExit(f"expected module wheel missing after build: {wheel.name}")
        wheels[name] = wheel
    total = sum(path.stat().st_size for path in wheels.values()) / 1e6
    log(f"wheelhouse ready: {len(wheels)} wheels, {total:.1f} MB")
    return wheels


# 跨架构（android-arm64）安装的补丁 wheel：(发行名, 上游版本, 本地版本)。
# pip 带 --only-binary=:all: 时不能源码编译，而 PyStemmer 2.x 只发 sdist
# （没有 aarch64 轮子），llama-index-retrievers-bm25 的每个版本又都钉
# pystemmer<3 —— 解析必然失败。PyStemmer 3.1.0 有 cp312 aarch64 轮子，顶层
# 模块（Stemmer）和 API 与 2.x 相同，因此重新贴标成 2.2.0.1+android：
# 本地版本号仍满足上游约束（>=2.2.0.1,<3），装进运行时的是 3.1.0 的二进制。
ANDROID_COMPAT_WHEELS: tuple[tuple[str, str, str], ...] = (
    ("pystemmer", "3.1.0", "2.2.0.1+android"),
)


def _pip_platform_args() -> list[str]:
    """跨架构安装的 pip 平台参数（手机运行时是 aarch64/cp312）。"""
    return [
        "--platform", "manylinux2014_aarch64",
        "--platform", "manylinux_2_17_aarch64",
        "--python-version", PY_VER.rsplit(".", 1)[0],
        "--implementation", "cp",
        "--abi", "cp312",
        "--only-binary=:all:",
    ]


def _relabel_wheel(src: Path, dest: Path, *, dist: str,
                   old_version: str, new_version: str) -> None:
    """把 wheel 的 dist-info 版本改名（内容不变），用于本地兼容贴标。"""
    old_info = f"{dist}-{old_version}.dist-info"
    new_info = f"{dist}-{new_version}.dist-info"
    dest.unlink(missing_ok=True)
    moved: list[str] = []
    with zipfile.ZipFile(src) as zin, \
            zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            name = info.filename
            if name.startswith(f"{old_info}/"):
                name = new_info + name[len(old_info):]
                if name.endswith("/METADATA"):
                    data = data.decode("utf-8").replace(
                        f"Version: {old_version}", f"Version: {new_version}", 1,
                    ).encode("utf-8")
                elif name.endswith("/RECORD"):
                    continue  # 最后重建：改名后原哈希已失效
            if not name.endswith("/"):
                moved.append(name)
            zout.writestr(name, data)
        # RECORD 必须列全自身；hash/size 留空是规范允许的形式。
        record = "".join(f"{name},,\n" for name in moved) + f"{new_info}/RECORD,,\n"
        zout.writestr(f"{new_info}/RECORD", record.encode("utf-8"))


def vendor_android_compat_wheels() -> Path | None:
    """生成 ``ANDROID_COMPAT_WHEELS`` 里的兼容 wheel，返回 find-links 目录。

    只在跨架构（IS_LINUX）全量安装时调用；产物缓存在 ``runtime-build/cache``
    里，重复构建直接复用。
    """
    if not IS_LINUX:
        return None
    if BUILD_PY is None or not BUILD_PY.exists():
        raise SystemExit("no cp312 build interpreter found (set BUILD_PY=...py)")
    dest = CACHE / "compat-wheels"
    dest.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.setdefault("PIP_INDEX_URL", DEFAULT_PIP_INDEX_URL)
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")

    for name, upstream, local in ANDROID_COMPAT_WHEELS:
        cached = sorted(dest.glob(f"{name}-{local}-*.whl"))
        if cached:
            log(f"compat wheel reused: {cached[0].name}")
            continue
        scratch = CACHE / "compat-wheels" / "_download"
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True, exist_ok=True)
        cmd = [str(BUILD_PY), "-m", "pip", "download", "--no-deps",
               "--no-cache-dir", "--dest", str(scratch),
               *_pip_platform_args(), f"{name}=={upstream}"]
        res = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if res.returncode != 0:
            log(res.stdout[-2000:])
            log(res.stderr[-2000:])
            raise SystemExit(
                f"pip download {name}=={upstream} (aarch64) failed; "
                f"update ANDROID_COMPAT_WHEELS"
            )
        src = next(iter(scratch.glob(f"{name}-{upstream}-*.whl")), None)
        if src is None:
            raise SystemExit(f"pip download produced no wheel for {name}=={upstream}")
        out = dest / src.name.replace(f"-{upstream}-", f"-{local}-", 1)
        _relabel_wheel(src, out, dist=name, old_version=upstream, new_version=local)
        shutil.rmtree(scratch, ignore_errors=True)
        log(f"compat wheel built: {out.name} (upstream {name} {upstream})")
    return dest


def _drop_wheelhouse() -> None:
    """Delete the install-only wheelhouse (the web wheel alone is ~90 MB).

    It lives under ``runtime-build/``, which CI caches wholesale, so keeping it
    would bloat the cache for no gain: every reinstall rebuilds it from source
    anyway. Called only after a build that actually installed.
    """
    if not WHEELHOUSE.exists():
        return
    shutil.rmtree(WHEELHOUSE, ignore_errors=True)
    log("wheelhouse removed (install artefact only)")


# Relocatable runtime components (edit to bump versions).
# Windows 用 python.org embeddable + node win-x64 zip；
# macOS 用 python-build-standalone（indygreg）+ node darwin tarball——
# python.org 没有 mac 等价 embeddable 发行版，python-build-standalone 是
# PyInstaller/Relink 生态公认的可重定位 mac Python 来源。
# Mac 通过 --arch 选择 arm64 / x86_64，默认跟随当前构建机架构。
#
# android-arm64（手机端）：Linux/aarch64 的 python-build-standalone + node
# linux-arm64。Android 是 bionic libc，glibc 二进制不能直接执行，所以这份
# 运行时跑在 PRoot 里的 Debian arm64 rootfs 中（见 build_android_bundle.py）；
# 本文件只负责产出与桌面端同 schema 的 python/ + node/ 两棵树。
IS_MAC = sys.platform == "darwin"
PY_VER = "3.12.7"
NODE_VER = "v22.22.2"

TARGETS = ("native", "windows-x64", "macos-arm64", "macos-x86_64", "android-arm64")


def _cli_value(name: str) -> str:
    """读 argv 里的 ``--name value`` / ``--name=value``。

    URL 是模块级常量、下载缓存文件名由它决定，所以目标平台必须在 import
    期确定，不能等 main() 里 argparse 的结果。
    """
    for index, arg in enumerate(sys.argv):
        if arg == f"--{name}" and index + 1 < len(sys.argv):
            return sys.argv[index + 1]
        if arg.startswith(f"--{name}="):
            return arg.split("=", 1)[1]
    return ""


_target = _cli_value("target") or "native"
if _target not in TARGETS:
    raise SystemExit(f"unknown --target {_target!r}; choose from {', '.join(TARGETS)}")
_arch = _cli_value("arch") or "native"

IS_LINUX = _target == "android-arm64"
IS_WIN = _target == "windows-x64" or (_target == "native" and sys.platform == "win32")
IS_MAC_TARGET = _target.startswith("macos") or (_target == "native" and IS_MAC)
# 布局判定：macOS 与 Linux 都是 POSIX 布局（bin/python3 + lib/python3.12）。
IS_POSIX = not IS_WIN

if IS_MAC_TARGET:
    if _target == "macos-x86_64":
        _arch = "x86_64"
    elif _target == "macos-arm64":
        _arch = "arm64"
    elif _arch == "native":
        _arch = "arm64" if platform.machine().lower() in ("aarch64", "arm64") else "x86_64"
    _PY_TAG = (f"cpython-{PY_VER}+20241016-"
               f"{'x86_64' if _arch == 'x86_64' else 'aarch64'}-apple-darwin-install_only")
    _PY_OSNODE = "darwin-x64" if _arch == "x86_64" else "darwin-arm64"
elif IS_LINUX:
    _arch = "aarch64"
    _PY_TAG = f"cpython-{PY_VER}+20241016-aarch64-unknown-linux-gnu-install_only"
    _PY_OSNODE = "linux-arm64"
else:
    _arch = "x86_64"
    _PY_OSNODE = ""

if IS_POSIX:
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

# 跨架构构建（x86 runner 上产 aarch64 运行时）无法 spawn staging 解释器：
# 所有「跑一下证明它真的能导入」的门禁退化为读 dist-info 的静态校验。
HOST_ARM = platform.machine().lower() in ("aarch64", "arm64")
HOST_ARCH = "aarch64" if HOST_ARM else "x86_64"
TARGET_OS = "windows" if IS_WIN else ("macos" if IS_MAC_TARGET else "linux")
HOST_OS = "windows" if sys.platform == "win32" else ("macos" if IS_MAC else "linux")
CAN_EXECUTE_STAGING = TARGET_OS == HOST_OS and _arch == HOST_ARCH

# 手机运行时（android-arm64）不随包 manim：路线 4 里 math_animator /
# video_lesson 这类重能力交给电脑引擎（SSH 远程模式）。这同时避开了 manim
# 生态里若干只发 x86_64 manylinux 轮子的传递依赖（手机上装不上）。
ACTIVE_EXTRAS: tuple[str, ...] = () if IS_LINUX else PIP_EXTRAS

# A system cp312 interpreter used only to resolve cp312 wheels into the
# embeddable distribution during the *build* (not needed at run time).
def _find_build_py() -> Path | None:
    env = os.environ.get("BUILD_PY")
    if env:
        return Path(env)
    candidates = list(Path.home().glob(
        "AppData/Roaming/uv/python/cpython-3.1[12]*/python.exe"))
    candidates += list(Path.home().glob(
        ".local/share/uv/python/cpython-3.1[12]*/python.exe"))
    candidates += list(Path.home().glob(
        ".local/share/uv/python/cpython-3.1[12]*/bin/python3.12"))
    candidates += [Path("/usr/bin/python3.12"), Path("/usr/local/bin/python3.12")]
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
    Linux（Android 运行时）同理。
    """
    if IS_POSIX:
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
    Returning [] (e.g. building on macOS/Linux) is harmless — UCRT is assumed
    present there.
    """
    if IS_POSIX:
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
    if IS_POSIX:
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
    if IS_POSIX:
        return target / "lib" / f"python{PY_VER.rsplit('.', 1)[0]}" / "site-packages"
    return target / "Lib" / "site-packages"


def _staging_py_exe(target: Path) -> Path:
    """staging python 树内解释器路径（用于版本门禁/冒烟测试）。"""
    if IS_POSIX:
        return target / "bin" / "python3"
    return target / "python.exe"


def install_deeptutor(target: Path, source_root: Path, *,
                      with_deps: bool = True,
                      modules: list[str] | None = None,
                      requirements: list[str] | None = None) -> None:
    """pip install --target 把【本地源构建的随包 wheel】装进 embeddable 的 site-packages。

    1.7.2 起 deeptutor 被拆成「核心 + 每模块一个 distribution」。只装核心会得到
    一个没有 deeptutor-server / deeptutor-web 元数据的运行时：`deeptutor start`
    第一行 require_module("server") 就直接判死。所以这里装的是
    build_wheelhouse() 产出的【全量随包 wheel】，`--find-links` 指向本地
    wheelhouse，第三方依赖仍由各 wheel 的 Requires-Dist 从索引解析。

    with_deps=False（快速路径）：依赖签名未变时三方树原样复用，只重装我们自己
    （核心 + 全部模块）的 wheel —— 但仍是【全部】，不能只装核心，否则模块元数据
    会消失。快速路径下 plain requirements（PIP_EXTRAS 里的非模块依赖）不参与：
    --no-deps 的语义就是不碰依赖树，它们在全量安装时已就位。
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
    if modules is None:
        modules, requirements = _shipped_selection(source_root)
    requirements = list(requirements or [])
    dest = _py_site_packages(target)
    dest.mkdir(parents=True, exist_ok=True)
    desc = "shipped module wheels + full deps" if with_deps \
        else "shipped module wheels only (--no-deps fast path)"
    log(f"pip installing {desc} from LOCAL SOURCE {source_root} "
        f"({len(modules)} modules: {', '.join(modules)}) ...")
    env = dict(os.environ)
    # setdefault：用户已设置镜像/代理时尊重用户，没有才落默认国内镜像。
    env.setdefault("PIP_INDEX_URL", DEFAULT_PIP_INDEX_URL)
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    env.setdefault("PIP_DEFAULT_TIMEOUT", "60")
    log(f"pip index: {env['PIP_INDEX_URL']}")

    wheels = build_wheelhouse(source_root, modules)
    # 模块顺序即依赖顺序（core 在前），pip 一次解析整组本地 wheel：
    # deeptutor-server==<v> 这类 Requires-Dist 由 --find-links 里的 wheel 满足。
    cmd = [str(BUILD_PY), "-m", "pip", "install", "--upgrade", "--no-compile",
           "--target", str(dest), "--find-links", str(WHEELHOUSE)]
    if IS_LINUX:
        # 手机运行时是 aarch64，构建机通常是 x86：必须显式指定目标平台，
        # 否则 pip 解析出 x86_64 的 manylinux wheel，装进 rootfs 里跑不起来。
        # 二进制都来自 Debian rootfs 的 glibc，所以 manylinux 轮子可用
        # （Android 自身的 bionic 在这里不参与，PRoot 交出的是 rootfs 的 libc）。
        cmd.extend(_pip_platform_args())
        if with_deps:
            compat = vendor_android_compat_wheels()
            if compat is not None:
                cmd.extend(["--find-links", str(compat)])
    if not with_deps:
        cmd.append("--no-deps")
    cmd.extend(str(wheels[name]) for name in modules)
    if with_deps:
        cmd.extend(requirements)
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if res.returncode != 0:
        log(res.stdout[-3000:])
        log(res.stderr[-3000:])
        raise SystemExit(f"pip install deeptutor modules failed (rc={res.returncode})")
    log(f"deeptutor (local source) + {len(modules)} module wheels installed "
        f"into embeddable runtime")


def version_gate(target: Path, expected: str) -> str:
    """版本门禁：staging 里实际装上的 deeptutor 版本必须等于本地源版本。

    防止「以为打了新版、其实静默回退到旧版」的事故再次发生。
    """
    if not CAN_EXECUTE_STAGING:
        return version_gate_static(target, expected)
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


def _site_packages_for_exe(py_exe: Path) -> Path:
    """POSIX staging 布局下由解释器路径反推 site-packages。"""
    return py_exe.parents[1] / "lib" / f"python{PY_VER.rsplit('.', 1)[0]}" / "site-packages"


def _module_issues_static(site_packages: Path, modules: list[str],
                          source_root: Path | None = None) -> list[str]:
    """跨架构构建的模块门禁：只读 dist-info，不 spawn 目标解释器。

    x86 runner 上产 aarch64 运行时无法执行 staging 里的 python（需要 qemu），
    因此把「跑一下确认」换成读元数据：每个随包 distribution 的 dist-info
    必须在、且版本等于源码版本；启动面依赖的包目录必须在。
    """
    root = source_root or MONOREPO_SOURCE
    manifests = _load_manifests(root)
    expected = _read_source_version(root)
    issues: list[str] = []
    for name in modules:
        distribution = manifests[name]["distribution"]
        prefix = distribution.replace("-", "_")
        versions = [version for path in site_packages.glob(f"{prefix}-*.dist-info")
                    if (version := _distinfo_metadata_version(path))]
        if not versions:
            issues.append(f"missing module distribution {distribution} ({name})")
        elif expected not in versions:
            issues.append(f"module {name} is {versions[0]} but core is {expected}")
    for package in DESKTOP_SMOKE_PACKAGES:
        if not (site_packages / package).is_dir():
            issues.append(f"missing start-up package {package}")
    return issues


def version_gate_static(target: Path, expected: str) -> str:
    """version_gate 的静态版本（跨架构构建）。"""
    sp = _py_site_packages(target)
    found = sorted(version for path in sp.glob("deeptutor-*.dist-info")
                   if (version := _distinfo_metadata_version(path)))
    if expected not in found:
        raise SystemExit(
            f"VERSION GATE FAILED (static): staging dist-info has "
            f"{found or 'no deeptutor dist-info'} but source is {expected!r}."
        )
    log(f"VERSION GATE OK (static): deeptutor {expected} == source")
    return expected


def smoke_test(py_exe: Path) -> None:
    """staging 冒烟测试：自家包可 import + PIP_EXTRAS 关键模块存在。

    extras 门禁用 importlib.util.find_spec 轻量探测（不真正 import manim——
    它导入重且在无显示环境可能引入变数），缺任一声明模块即 SystemExit，
    防止「extras 没装上但整条构建链路静默通过」。PIP_EXTRAS 为空时门禁
    自然跳过（与安装逻辑联动）。
    """
    if not CAN_EXECUTE_STAGING:
        # 跨架构：不能跑 staging 解释器，退化为目录存在性检查。
        sp = _site_packages_for_exe(py_exe)
        gated = [mod for extra in ACTIVE_EXTRAS
                 for mod in EXTRA_SMOKE_MODULES.get(extra, ())]
        missing = [mod for mod in gated if not (sp / mod).is_dir()]
        if missing:
            raise SystemExit(
                f"EXTRAS GATE FAILED: modules missing from runtime: {missing}.\n"
                f"extras {list(ACTIVE_EXTRAS)} 未装进 runtime；用 --clean 触发全量重装。"
            )
        log(f"extras gate OK (static): {gated} present")
        return
    code = (
        "import deeptutor_cli, deeptutor, deeptutor_web; "
        "print('runtime imports OK:', deeptutor_cli.__file__)"
    )
    res = subprocess.run([str(py_exe), "-c", code], capture_output=True, text=True, timeout=120)
    log("import check: " + (res.stdout.strip() or res.stderr.strip()))

    if not ACTIVE_EXTRAS:
        return
    # {模块: 所属 extra} 映射；extra 缺门禁声明属于配置错误，宁可构建失败，
    # 不可静默跳过校验（与「防静默漏装」同一原则）。
    mapping: dict[str, str] = {}
    for extra in ACTIVE_EXTRAS:
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


def _module_probe_code(modules: list[str], packages: tuple[str, ...]) -> str:
    """Build the staging-python probe source (module state + start-up packages).

    Runs inside the packaged runtime, so it must only use the standard library
    plus the deeptutor package itself: it reads the manifests, asks
    ``module_enabled`` (installed + enabled + version match + requires closure)
    and find_specs the server stack. Nothing heavy is imported.
    """
    return (
        "import importlib.util, json\n"
        "from importlib import metadata\n"
        "from deeptutor.__version__ import __version__\n"
        "from deeptutor.modules.catalog import manifests, module_enabled\n"
        f"modules = {list(modules)!r}\n"
        f"packages = {list(packages)!r}\n"
        "report = {'version': __version__, 'modules': {}, 'packages': {}}\n"
        "for name in modules:\n"
        "    distribution = manifests()[name]['distribution']\n"
        "    row = {'distribution': distribution, 'version': None, 'enabled': False}\n"
        "    try:\n"
        "        row['version'] = metadata.version(distribution)\n"
        "    except metadata.PackageNotFoundError:\n"
        "        pass\n"
        "    row['enabled'] = module_enabled(name)\n"
        "    report['modules'][name] = row\n"
        "for package in packages:\n"
        "    report['packages'][package] = importlib.util.find_spec(package) is not None\n"
        "print(json.dumps(report))\n"
    )


def _probe_env() -> dict[str, str]:
    """Environment for probes: keep the builder's module selection out of the way.

    ``catalog.selection()`` reads ``$DEEPTUTOR_HOME/data/system/modules.json``
    and falls back to the current directory. Pointing the probe at a scratch
    home means the build machine's own choices can never disable a shipped
    module (or, worse, silently hide a missing one).
    """
    env = dict(os.environ)
    home = STAGE / "gate-home"
    home.mkdir(parents=True, exist_ok=True)
    env["DEEPTUTOR_HOME"] = str(home)
    env.pop("PYTHONPATH", None)
    return env


def module_report(py_exe: Path, modules: list[str]) -> dict:
    """Ask the staging runtime which shipped modules are actually usable."""
    try:
        res = subprocess.run(
            [str(py_exe), "-c", _module_probe_code(modules, DESKTOP_SMOKE_PACKAGES)],
            capture_output=True, text=True, env=_probe_env(), timeout=300,
        )
    except OSError as exc:
        raise SystemExit(
            f"MODULE PROBE FAILED: cannot run staging interpreter {py_exe}: {exc}"
        ) from exc
    if res.returncode != 0:
        raise SystemExit(
            "MODULE PROBE FAILED: staging python cannot read the module catalog.\n"
            f"{(res.stderr or '').strip()[-2000:]}"
        )
    try:
        return json.loads((res.stdout or "").strip() or "{}")
    except ValueError:
        raise SystemExit(
            f"MODULE PROBE FAILED: cannot parse probe output {res.stdout!r}.\n"
            f"{(res.stderr or '').strip()[-2000:]}"
        )


def module_issues(report: dict) -> list[str]:
    """Human-readable defects in a module report (empty list == all good)."""
    issues: list[str] = []
    version = str(report.get("version") or "")
    for name, row in sorted((report.get("modules") or {}).items()):
        installed_version = row.get("version")
        if installed_version is None:
            issues.append(f"missing module distribution {row.get('distribution')} ({name})")
        elif installed_version != version:
            issues.append(
                f"module {name} is {installed_version} but core is {version}"
            )
        elif not row.get("enabled"):
            issues.append(f"module {name} is installed but not enabled")
    for package, present in sorted((report.get("packages") or {}).items()):
        if not present:
            issues.append(f"missing start-up package {package}")
    return issues


def module_gate(py_exe: Path, modules: list[str]) -> None:
    """Fail the build unless every shipped module is installed, current and enabled.

    ``deeptutor start`` gates on ``require_module("server")`` / ``("web")``
    before doing anything else, so a runtime that lacks one shipped module is a
    "click install, see a crash banner" product. That makes this gate a build
    invariant, not an after-install check: it runs on every build (including
    builds that skipped pip) and again after pruning.
    """
    if CAN_EXECUTE_STAGING:
        issues = module_issues(module_report(py_exe, modules))
    else:
        issues = _module_issues_static(_site_packages_for_exe(py_exe), modules)
    if issues:
        detail = "\n  ".join(issues)
        raise SystemExit(
            f"MODULE GATE FAILED: {len(issues)} defect(s) in the packaged runtime:\n"
            f"  {detail}\n"
            f"随包模块（{len(modules)}）必须全部 installed/enabled/版本一致；"
            f"用 --clean 或 --force-deeptutor 触发全量重装。"
        )
    log(f"MODULE GATE OK: {len(modules)} shipped modules ready "
        f"+ {', '.join(DESKTOP_SMOKE_PACKAGES)} present")


def module_precheck(py_exe: Path, modules: list[str]) -> list[str]:
    """Best-effort module defects, used to decide whether a reinstall is needed.

    Deliberately tolerant: a staging tree left by an older release has no
    ``deeptutor.modules.catalog`` at all (the modular runtime is new in 1.7.2),
    and CI restores exactly such trees from cache. That must mean "reinstall",
    never "abort the build" — the strict verdict belongs to ``module_gate``.
    """
    if not CAN_EXECUTE_STAGING:
        return _module_issues_static(_site_packages_for_exe(py_exe), modules)
    try:
        return module_issues(module_report(py_exe, modules))
    except SystemExit as exc:
        first_line = str(exc).strip().splitlines()
        return [first_line[0] if first_line else "module probe failed"]


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
    if IS_POSIX:
        # POSIX 树上 bin/ 是 python-build-standalone 的可执行入口（python3），
        # prune 掉会导致运行时找不到解释器；PyWin32.chm 只存在于 Windows。
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


def _compile_py_exe(target: Path) -> Path | None:
    """预编译用的解释器：跨架构构建时用宿主机的同版本 python。

    .pyc 与 CPU 架构无关，只要求 magic number 一致；构建机与 staging 都是
    cp312，所以宿主解释器编译出的 pyc 在手机 aarch64 上同样有效。
    """
    if CAN_EXECUTE_STAGING:
        return _staging_py_exe(target)
    return BUILD_PY if BUILD_PY and BUILD_PY.exists() else None


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
    py_exe = _compile_py_exe(target)
    if py_exe is None:
        log("precompile skipped: no host interpreter available for cross-build")
        return
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
    if IS_POSIX:
        # python-build-standalone：bin/python3；node posix tarball：bin/node
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


def _module_dependency_payload(source_root: Path) -> dict[str, dict]:
    """安装面视图：每个清单的 distribution / requirements / requires / optional。

    只取这四段而不是整份清单：路由、导航、capabilities 等改动不影响 pip 安装
    面，纳入哈希会让「只加一条路由」误触发全量重装。
    """
    return {
        name: {
            "distribution": row["distribution"],
            "requirements": sorted(row["requirements"]),
            "requires": list(row["requires"]),
            "optional": list(row["optional"]),
        }
        for name, row in sorted(_load_manifests(source_root).items())
    }


def _dependency_relevant_payload(source_root: Path) -> bytes | None:
    """提取 pyproject.toml 中真正影响依赖安装面的字段（canonical JSON 编码）。

    整文件哈希的教训（2026-10-24 CI 实证，macOS x86_64 job 11m11s）：pyproject
    里的 ruff/black/pytest/mypy/bandit 工具配置、description、scripts、
    entry-points 与依赖安装毫无关系，但任何一个字符的改动都会让 deps_sig 变化
    → CI 缓存 restore-keys 恢复旧 staging 后 site-packages 被整体清空 + 全量
    重装（Intel runner 上多花 5-7 分钟）。改为只哈希真正决定安装面的内容：

      [build-system].requires / .build-backend   — 隔离构建环境的内容
      [project].requires-python                  — 环境标记求值基准
      [project].dependencies                     — 主依赖集（core wheel 的 Requires-Dist）
      [tool.deeptutor.optional-dependencies]      — full 档 + PIP_EXTRAS 的声明
      deeptutor/modules/manifests/*.json          — 各模块的 requirements/requires/optional
      随包模块集合                                — 少一个模块 = 少一整套 wheel

    1.7.2 之前这里读的是 ``[project].optional-dependencies``（本项目从来不存在
    这张表，extras 声明在 ``[tool.deeptutor]`` 下且带 ``{version}`` 占位），
    结果是 extras 段恒为空、模块依赖变更完全不在签名里 —— 那正是 --no-deps
    快路径会静默漏装模块依赖的路径，一并修掉。

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
    version = _read_source_version(source_root)
    optional = _optional_dependencies(source_root, version)
    payload = {
        "build_backend": build_system.get("build-backend", ""),
        "build_requires": sorted(build_system.get("requires", []) or []),
        "requires_python": project.get("requires-python", ""),
        "dependencies": sorted(project.get("dependencies", []) or []),
        # PIP_EXTRAS 里声明了但 pyproject 没定义的 extra 也计入：这种情况
        # 构建立刻会失败，先让签名变化触发重装，好过静默漏装。
        "extras": {extra: sorted(optional.get(extra, [])) for extra in PIP_EXTRAS},
        "missing_extras": sorted(e for e in PIP_EXTRAS if e not in optional),
        "full": sorted(optional.get(FULL_PROFILE, [])),
        "modules": _module_dependency_payload(source_root),
        "shipped": shipped_modules(source_root),
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
        一次（安全方向，一次性全量重装后归位）。deps-only-v2 起纳入模块清单
        与随包模块集合（v1 的签名不含模块依赖面）。
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
    digest.update(b"\0extras=" + ",".join(ACTIVE_EXTRAS).encode("utf-8"))
    digest.update(b"\0algo=deps-only-v2")
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
        if IS_POSIX:
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
    modules, module_requirements = _shipped_selection(source_root)
    log(f"shipped modules ({len(modules)}): {', '.join(modules)}")
    if module_requirements:
        log(f"plain extras requirements: {', '.join(module_requirements)}")
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
        # 模块状态是「已装好」的一部分，而不是装完之后才知道的事：缺
        # deeptutor-server / 版本错位 / 被 disable 时，必须走重装而不是跳过 pip
        # 然后在门禁里炸掉（CI 缓存恢复出的老 staging 正是这种形态）。
        module_defects = (
            module_precheck(py_exe, modules) if res.returncode == 0
            else ["staging deeptutor is not importable"]
        )
        installed_ok = (
            res.returncode == 0 and current == expected
            and not state_changed and not deps_changed and not module_defects
        )
        if module_defects and not state_changed and not deps_changed:
            log("module gate pre-check found defects; forcing reinstall: "
                + "; ".join(module_defects[:6]))
        # 清理自家 dist-info 的残留元数据（历史事故：pip --target 覆盖式安装
        # 只增不删，旧版本 deeptutor-<旧>.dist-info 会随运行时一起发给用户，
        # 显示层若取到旧元数据就出现「1.6.14 遮蔽 1.6.15」的版本倒退）。
        # 【关键】只删版本与源码（expected）不一致的残留，**保留正确那份**：
        # 快路径（已装且版本/指纹未变 → 跳过下方安装，见 installed_ok）不会
        # 重建 dist-info，若把唯一一份也删掉，打出的运行时只有代码没有元数据，
        # 关于页引擎会显示「未知」、select_runtime_base 的版本择优也会失明。
        # 1.7.2 起每个模块都是一个 distribution（deeptutor-server / -web / ...），
        # 所以用 deeptutor* 通配覆盖全部，只匹配 deeptutor-* 会漏掉模块元数据。
        for entry in sp.glob("deeptutor*.dist-info"):
            if _distinfo_metadata_version(entry) == expected:
                continue
            try:
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
                log(f"removed stale dist-info residue: {entry.name}")
            except OSError as exc:
                raise SystemExit(
                    f"cannot clean stale {entry.name}: {exc}\n"
                    f"likely held by a running ThinkBuddy/deeptutor "
                    f"process or antivirus — close the app and retry."
                ) from exc
        if installed_ok and not force_deeptutor:
            log(f"deeptutor {current} already installed & matches source; skipping pip")
        else:
            log(f"reinstalling deeptutor: staging={current!r} source={expected!r} "
                f"force={force_deeptutor} deps_changed={deps_changed} "
                f"modules_ok={not module_defects}")
            # 挖掉全部自家代码树 + 全部 deeptutor* dist-info（含各模块的）：
            # pip --target 只增不删，旧版本残留会和 new 版混合成一个运行时，
            # 且能骗过版本门禁。
            for pat in ("deeptutor", "deeptutor_cli", "deeptutor_web",
                        "deeptutor*.dist-info"):
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
            # --no-deps 快速路径（重装全部自家 wheel，第三方树原样保留）。
            install_deeptutor(STAGING_PY, source_root, with_deps=deps_changed,
                              modules=modules, requirements=module_requirements)
            did_install = True
    else:
        install_deeptutor(STAGING_PY, source_root, with_deps=True,
                          modules=modules, requirements=module_requirements)
        did_install = True

    # 冒烟测试（重量级 import，embeddable 下 ~10-40s）只在真装过东西后跑；
    # 版本门禁与模块门禁都轻量，每次构建都跑——它们是 staging 存活与
    # 「装完能 start」的证明，不是安装的副产品。
    if did_install:
        smoke_test(_staging_py_exe(STAGING_PY))
    version_gate(STAGING_PY, expected)   # 版本门禁：不过这里直接构建失败
    module_gate(_staging_py_exe(STAGING_PY), modules)  # 随包模块全可用才算成功
    save_source_state(source_root, fp, deps_sig, mode)

    if did_install:
        sp = _py_site_packages(STAGING_PY)
        if (sp / "litellm").exists() or (sp / "boto3").exists():
            prune_runtime(sp)
            smoke_test(_staging_py_exe(STAGING_PY))  # re-verify after pruning
            # prune 的 glob 可能误伤模块依赖（例如某模块 requirements 里加了
            # hf_xet）：删完必须重新证明全部门禁仍然成立。
            module_gate(_staging_py_exe(STAGING_PY), modules)
        else:
            log("runtime already pruned; skipping")

    # wheelhouse 只是安装中间产物（web wheel 就 ~90MB），装完即删：它躺在
    # CI 整体缓存的 runtime-build/ 里，留着只涨缓存。
    if did_install:
        _drop_wheelhouse()

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
    compile_py = _compile_py_exe(STAGING_PY)
    if not CAN_EXECUTE_STAGING and compile_py is not None:
        # rebrand 改写过 .py 后要重编译 pyc；跨架构时用宿主的同版本解释器
        # （.pyc 与 CPU 架构无关，只要求 cp312 magic 一致）。
        os.environ["REBRAND_COMPILE_PY"] = str(compile_py)
    _rebrand_staging(_py_site_packages(STAGING_PY))
    # zip 路径兜底：rebrand_root 末尾已预编译，这里幂等重跑（增量，秒级），
    # 保证打进 runtime.zip 的树 pyc 齐全——zip 是 mac .app 的运行时事实源。
    precompile_site_packages(STAGING_PY)

    out_zip = DIST / ("runtime-android-arm64.zip" if IS_LINUX else "runtime.zip")
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
                "target": _target,
                "arch": _arch,
                "deeptutor_version": expected,
                "source_fingerprint": fp,
                "source_fingerprint_mode": mode,
            }),
        )
        for root_ in (STAGING_PY, STAGING_NODE):
            base = "python" if root_ is STAGING_PY else "node"
            for f in sorted(root_.rglob("*")):
                if f.is_symlink() and IS_POSIX:
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
                    if IS_POSIX:
                        # 只存权限位，不存 S_IFREG：运行端用 attr & 0o120000
                        # 检测符号链接，S_IFREG (0o100000) 与 S_IFLNK (0o120000)
                        # 有位重叠，加了 S_IFREG 的普通文件会被误判为符号链接。
                        info.external_attr = (stat.S_IMODE(f.stat().st_mode) << 16)
                    with open(f, "rb") as fh:
                        zf.writestr(info, fh.read())
    size = out_zip.stat().st_size / 1e6
    log(f"built {out_zip} ({size:.1f} MB)")
    if IS_POSIX and CAN_EXECUTE_STAGING:
        # POSIX 打包段自我验证：把 runtime.zip 按最终用户路径完整走一遍
        # （extract -> 权限/符号链接修复 -> 真实 spawn python3/node）。
        # 这段检测的是「zip 元数据是否正确编码了 unix 模式位/符号链接」——
        # python3 修复链上所有历史 bug（无执行位、python3.12 真身漏修、
        # S_IFLNK 放错位导致 Exec format error）都会在这里直接失败。
        # 跨架构（x86 上产 aarch64）无法 spawn，跳过——手机端由 runtime
        # 自检（Android 壳首次启动的 smoke 输出）兜底。
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
    ap.add_argument("--target", choices=TARGETS, default="native",
                    help="目标平台；android-arm64 产出手机端 Linux/aarch64 运行时")
    if IS_MAC_TARGET:
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
