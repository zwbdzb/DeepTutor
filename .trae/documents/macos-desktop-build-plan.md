# macOS 桌面打包流水线实施方案

## Context

DeepTutor 现有桌面打包流水线（`.github/workflows/build-desktop.yml`）只支持 Windows：在 `windows-latest` runner 上跑三步——前端 standalone 构建、`build_runtime.py` 生成 embeddable Python+Node 运行时、PyInstaller+Inno Setup 产出 `ThinkBuddyDesktop.exe` 与 `ThinkBuddySetup.exe`。

用户希望新增一条 macOS 流水线：push 到 main/develop 自动触发，产出 macOS arm64 zip 压缩包（含 `.app` bundle），手动勾选 `release` 时上传到 GitHub Release。架构仅 arm64（M1/M2/M3）。壳体验暂时用 macOS 系统标准标题栏（不重写 Windows 那套自绘 WinForms 标题栏），后续再迭代。

## 关键事实（已通过代码核查确认）

- `desktop/native_menu_backend.py` 顶层 L96 `_user32 = ctypes.windll.user32` 在 mac 上 import 阶段即抛 `AttributeError`，必须延迟绑定
- `install_windows_shell_menu()`（L1293-L1302）已有 `if os.name != "nt": return False` 守卫，mac 上是 no-op
- `configure_account_chip` / `account_chip` / `set_account_chip_hidden`（L514-L537）是平台无关纯函数，mac 上安全调用
- `menubar.build_native_menu(api)` 走 `webview.menu.Menu` 通道，pywebview Cocoa 后端原生支持
- `auth/store.py` 的 DPAPI 失败有 try-except 兜底（L109/L129），mac 上能启动但 token 明文存储（MVP 接受，后续补 Keychain）
- `process.py` 已有 `if os.name != "nt"` 平台分支，无需改
- `main.py` 启动链路在 mac 上能跑（前提是 `native_menu_backend` 顶层不炸）

## 实施步骤

### 步骤 1：修复 `native_menu_backend.py` 顶层 ctypes 绑定

文件：`desktop-shell/desktop/native_menu_backend.py`

把 L95-L97 附近的 `_user32 = ctypes.windll.user32` 及紧随其后的 `restype`/`argtypes` 设置，从模块顶层移到 `_bind_user32()` 函数内。顶层仅声明 `_user32 = None`。在 `install_windows_shell_menu()` 函数体开头（`if os.name != "nt": return False` 之后）调用 `_bind_user32()`。这样 mac 上 import 该模块不再触发 `ctypes.windll` 访问，Windows 行为零变化。

### 步骤 2：平台化 `runtime.py` 路径与可执行名

文件：`desktop-shell/desktop/runtime.py`

加平台辅助函数（文件顶部）：

```python
def _is_mac() -> bool: return sys.platform == "darwin"
def _py_bin_rel() -> tuple: return ("bin", "python3") if _is_mac() else ("python.exe",)
def _py_bin_name() -> str: return "python3" if _is_mac() else "python.exe"
def _node_bin_name() -> str: return "node" if _is_mac() else "node.exe"
def _venv_scripts() -> str: return "bin" if _is_mac() else "Scripts"
```

替换以下硬编码（用 Grep 定位全部 `python.exe`/`node.exe`/`Scripts`）：
- L29 `LOCALAPPDATA`：mac 用 `Path.home() / "Library" / "Application Support"` 替代（变量名保留以避免大改）
- L143 `(cand / "python" / "python.exe")` → `cand / "python" / _py_bin_rel()`
- L178-183 `node.exe` → `_node_bin_name()`
- L207 `MANAGED_VENV / "Scripts" / name` → `MANAGED_VENV / _venv_scripts() / name`
- L231 `base / "python" / "python.exe"` → `base / "python" / _py_bin_rel()`
- L308 `RUNTIME.joinpath("python", "python.exe")` → `RUNTIME.joinpath("python", _py_bin_rel())`
- L346-L378 `provision_with_system_python` 内 `Scripts`/`python.exe` 同样平台化

### 步骤 3：平台化 `build_runtime.py` 的下载与 staging 逻辑

文件：`desktop-shell/tools/build_runtime.py`

加平台常量（在 L66-L74 附近）：

```python
IS_MAC = sys.platform == "darwin"
if IS_MAC:
    PY_VER = "3.12.7"
    PY_TAG = f"cpython-{PY_VER}+20241016-aarch64-apple-darwin-pgo-full"
    PY_URL = f"https://github.com/indygreg/python-build-standalone/releases/download/20241016/{PY_TAG}.tar.gz"
    PY_ZIP = CACHE / f"{PY_TAG}.tar.gz"
    NODE_VER = "v22.22.2"
    NODE_URL = f"https://nodejs.org/dist/{NODE_VER}/node-{NODE_VER}-darwin-arm64.tar.gz"
    NODE_ZIP = CACHE / f"node-{NODE_VER}-darwin-arm64.tar.gz"
else:
    # 原 Windows 常量保持不动
```

需要改的函数：
- `extract()`：加 tar.gz 分支（用 `tarfile` 模块）。python-build-standalone 解包后是 `python/bin/python3`、`python/lib/python3.12/...` 结构，直接展平到 `staging/python/`
- `enable_site()`：mac 上 `return` 早退（python-build-standalone 已启用 site 模块，无 `_pth` 文件）
- `_prepared()`：marker 改为 `python3`/`bin/python3`（mac）或 `python.exe`（win）
- `install_deeptutor()` 的 `dest = target / "Lib" / "site-packages"`：mac 上改为 `target / "lib" / "python3.12" / "site-packages"`
- `smoke_test()` / `version_gate()` 的 `py_exe` 路径：mac 用 `STAGING_PY / "bin" / "python3"`

`PRUNE_GLOBS()` mac 上仍 prune `litellm`/`boto3`/`hf_xet`，去掉 `PyWin32.chm`（mac 不存在）。`bin/` 在 mac 上是可执行入口，**不要** prune。

### 步骤 4：平台化 `dialogs.py` 与 `clipboard.py`

文件：`desktop-shell/desktop/dialogs.py` 与 `desktop-shell/desktop/clipboard.py`

`dialogs.message_box()`：加 `if os.name == "nt":` 分支保留 Win32 MessageBoxW；mac 分支用 `tkinter.messagebox.showinfo`（Python 自带，PyInstaller 安全）

`clipboard.set_text()`：加 `if os.name == "nt":` 分支保留 Win32 剪贴板 API；mac 分支用 `subprocess.run(["pbcopy"], input=text.encode("utf-8"))`（macOS 自带命令，无需额外依赖）

### 步骤 5：新建 `ThinkBuddyDesktop-mac.spec`

文件：`desktop-shell/build/ThinkBuddyDesktop-mac.spec`

```python
# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path

ROOT = Path(SPECPATH)
PROJECT = ROOT.parent
ASSETS = PROJECT / "assets"
DIST = PROJECT / "dist"

datas = [
    (str(ASSETS / "icon.png"), "assets"),
    (str(ASSETS / "icon.icns"), "assets"),
]
runtime_zip = DIST / "runtime.zip"
if runtime_zip.exists():
    datas.append((str(runtime_zip), "."))

a = Analysis(
    [str(PROJECT / "desktop" / "main.py")],
    pathex=[str(PROJECT)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "webview",
        "webview.platforms.cocoa",
    ],
    excludes=["tkinter", "PyQt5", "PySide2", "PySide6", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    name="ThinkBuddyDesktop",
    debug=False,
    strip=False,
    upx=False,  # mac 上 upx 破坏 dylib 签名
    console=False,
    icon=str(ASSETS / "icon.icns"),
)

app = BUNDLE(
    exe, a.binaries, a.datas,
    name="ThinkBuddyDesktop.app",
    icon=str(ASSETS / "icon.icns"),
    bundle_identifier="com.thinkbuddy.desktop",
    info_plist={
        "CFBundleName": "ThinkBuddy",
        "CFBundleDisplayName": "ThinkBuddy",
        "CFBundleShortVersionString": "1.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
    },
)
```

要点：
- 用 onedir + BUNDLE 产出 `.app`（mac 必需，BUNDLE 不支持 onefile）
- `upx=False`：upx 在 mac 上破坏 dylib 签名导致启动崩溃
- `runtime.zip` 仍内嵌（mac 上 build_runtime.py **不带** `--no-zip`，必须生成 zip 让 shell 首启自解压）
- shell 的 `APP_DIR = sys._MEIPASS` 在 mac 上指向 `Contents/Resources`，能自动找到 runtime.zip

### 步骤 6：新建 `.github/workflows/build-desktop-mac.yml`

完整 workflow 文件，关键点：
- `runs-on: macos-14`（arm64 原生 runner）
- setup-python step 加 `id: py312` 以便用 `${{ steps.py312.outputs.python-path }}` 注入 `BUILD_PY`
- 缓存 key：`thinkbuddy-runtime-mac-py3127-node2222`（区分 Windows）
- inline 生成 `.icns`（用 macOS 自带的 `sips` + `iconutil`），**不**签入仓库
- `build_runtime.py` **不带** `--no-zip`（生成 runtime.zip 内嵌进 .app）
- PyInstaller 用 `ThinkBuddyDesktop-mac.spec`
- 用 `ditto -c -k --keepParent` 打成 zip（保留 mac 元数据）
- `workflow_dispatch` release 时上传到 GitHub Release（tag `desktop-mac-N`）
- **不**做 codesigning（MVP 阶段；release 说明里写"首次打开：右键 → 打开 → 仍要打开"绕过 Gatekeeper）

触发 paths 与 Windows workflow 一致，避免改 iss 等无关文件时触发 mac 构建（小问题，可后续优化）。

### 步骤 7：验证

- **CI 验证**：push 到 develop 分支，观察 `build-desktop-mac.yml` 是否触发并产出 `ThinkBuddyDesktop-mac-arm64.zip` artifact
- **本地验证**（如有 mac）：下载 artifact，解压得到 `ThinkBuddyDesktop.app`，右键 → 打开 → 仍要打开，验证：
  - 壳窗口能起来，splash 页能显示
  - deeptutor 后端能启动（首启会自解压 runtime.zip 到 `~/Library/Application Support/ThinkBuddy/runtime/`）
  - 127.0.0.1:3782 前端能加载
  - 登录门控页能显示，点击登录能用系统浏览器打开 Tokengine
- **release 验证**：手动触发 workflow 勾选 release，检查 GitHub Release `desktop-mac-N` 下是否含 zip 附件

## 不改动的文件

- `desktop/main.py`：mac 上启动链路天然兼容（`install_windows_shell_menu` 返回 False，`create_window` + `webview.start` 走 Cocoa 后端）
- `desktop/menubar.py`：`build_native_menu` 走 `webview.menu.Menu` 通道，Cocoa 后端原生渲染
- `desktop/titlebar_account.py`：纯逻辑模块
- `desktop/process.py`：已有平台分支
- `desktop/auth/store.py`：DPAPI 失败有 try-except 兜底（mac 上 token 明文存储，MVP 接受）
- `scripts/prepare_web_package.py`：平台无关
- `desktop-shell/tools/rebrand.py`：平台无关

## 已知限制（MVP 接受，后续迭代）

1. **无 codesigning**：用户首次打开需右键 → 打开绕过 Gatekeeper
2. **token 明文存储**：DPAPI 在 mac 上失败降级为明文（后续接 Keychain 或 Fernet 加密）
3. **无自绘标题栏**：mac 用系统标准红黄绿按钮 + pywebview Cocoa 菜单条（账号区不显示，登录入口走页面内 HTML）
4. **仅 arm64**：Intel mac 用户需 Rosetta 2 或后续补 x86_64 矩阵
5. **install_deeptutor 的 mac dest 路径**需在执行时验证 python-build-standalone 实际布局（`lib/python3.12/site-packages` 是预期，但 release 间可能有差异）

## 代码改动总量预估

- `native_menu_backend.py`：~20 行（延迟绑定）
- `runtime.py`：~30 行（平台函数 + 替换硬编码）
- `build_runtime.py`：~60 行（平台常量 + tar.gz 分支 + dest 路径）
- `dialogs.py`：~15 行（mac 分支）
- `clipboard.py`：~10 行（mac 分支）
- 新建 `ThinkBuddyDesktop-mac.spec`：~60 行
- 新建 `build-desktop-mac.yml`：~80 行

总计约 5 个文件修改 + 2 个新文件，diff 约 280 行。`main.py` 零改动。
