# EduBuddy 打包指南：从源码到安装包

> 面向团队成员的打包机制科普 + 操作手册。读完你只需要记住**一条命令**和**四步自检**。
>
> - 一条命令：`powershell -ExecutionPolicy Bypass -File build\build.ps1`
> - 四步自检：见 §6
>
> 相关决策记录：ADR-004（标题栏账号区）、ADR-005（运行时解析顺序）、ADR-003（登录门禁）。

## 1. 总览：三层源码，一条流水线

DeepTutor 仓库里住着三个**可以独立变化**的部分，打包就是把它们组合成两个 exe：

```
┌─────────────┐   ┌──────────────────┐   ┌──────────────────┐
│  web 前端    │   │  deeptutor 后端   │   │ desktop-shell 壳  │
│  (Next.js)  │   │ (Python 包 + CLI) │   │   (pywebview)    │
└──────┬──────┘   └────────┬─────────┘   └────────┬─────────┘
       │                   │                      │
       ▼ ①                 ▼ ②                    ▼ ③
  npm run build      build_runtime.py         PyInstaller
  + prepare_web_     (组装 staging 运行时      (打壳 → dist\
   package.py         + 版本门禁 + rebrand)     EduBuddyDesktop.exe)
       │                   │                      │
       └─► deeptutor_web 随 pip 装入 staging       │
                           │                      │
                           ▼ ④                    │
                     Inno Setup (ISCC) ───────────┘
                     dist\EduBuddySetup.exe
                     (壳 exe + staging 完整运行时树)
```

① → ④ 由 `build\build.ps1` 一条命令串起来；安装包版本号在 ④ 自动读自 `deeptutor/__version__.py`。

## 2. 三个概念

### 2.1 runtime（离线运行时）——"给用户机器发的整套环境"

用户是普通 Windows 用户：机器上没有 Python、没有 Node.js，也可能没网。但 deeptutor
后端是 Python 程序、前端需要 Node 跑服务。所以要把"能跑 deeptutor 的一切"随身带上：

```
runtime/
  python/   # python.org 官方 embeddable 版 Python 3.12（免安装、可移动）
            #   └─ Lib/site-packages 里用 pip 装进了【本地源码】的 deeptutor
  node/     # 便携版 Node.js 22 LTS（node.exe + npm，免安装）
```

三个关键机制（都在 `tools/build_runtime.py` 里）：

| 机制 | 函数 | 作用 |
|---|---|---|
| 本地源安装 | `install_deeptutor()` | `pip install --target ... D:\studio\DeepTutor`——**装的是硬盘上的源码，不是 PyPI**。PyPI 版本滞后且不含自研修复（历史事故：打出 1.6.7 而仓库已 1.6.8） |
| 版本门禁 | `version_gate()` | 装完真实执行一遍 staging 里的 python，读出 `deeptutor.__version__` 与源码版本**逐字对比**，不一致直接构建失败。防"1.6.9 冒充 1.6.10"的闸门 |
| 指纹增量 | `source_fingerprint()` | 对 `deeptutor/`、`deeptutor_cli/`、`deeptutor_web/`、`pyproject.toml` 全部文件算 SHA256。源码没变 → 跳过重装只跑门禁（十几秒）；变了 → 自动重装 |

- **staging 目录** `desktop-shell/runtime-build/staging/` 是"组装车间"：运行时在这里拼装、过门禁。
- 之后两个去向：打成 `dist/runtime.zip`（`-MakeZip` 时，给 onefile 壳内嵌、首启自解压到
  `%LOCALAPPDATA%\EduBuddy\runtime` 托管缓存），或被 Inno Setup **原样拷进安装包**（默认路径）。
- 下载缓存：python/node 安装 zip 存在 `runtime-build/cache/`，只有首次需要联网下载。

### 2.2 PyInstaller——"把壳变成一个 exe"

**壳（shell）**指 `desktop-shell/desktop/` 下的 Python 程序：pywebview 窗口（EduBuddy
窗口、标题栏、账号区）+ 启动后端、寻找运行时的逻辑。它本身很小。

PyInstaller 把 `desktop/main.py` 连同依赖的 Python 库（pywebview 等）编译成**单个 exe**：
`dist/EduBuddyDesktop.exe`（约 13–14MB）。

最容易误解的点：**这个 exe 里没有 deeptutor 本体**。看它的配方 `build/EduBuddyDesktop.spec`：

```python
datas = [
    (assets/icon.ico, "assets"),   # 图标
    (assets/icon.png, "assets"),
]
runtime_zip = DIST / "runtime.zip"
if runtime_zip.exists():            # 只有 zip 存在才内嵌
    datas.append((runtime_zip, "."))
```

`.spec` 就是 PyInstaller 的"菜谱"：入口 py、`hiddenimports`（`webview.platforms.winforms`
等动态导入的模块必须显式列出）、`console=False`（双击不出黑框）。

### 2.3 Inno Setup——"把 exe + runtime 缝成双击安装包"

`build/installer.iss` 是 Inno Setup 脚本，干三件事：

1. 把 `dist/EduBuddyDesktop.exe` 和 **staging 运行时树原样拷贝**
   （`Source: "..\runtime-build\staging\*"; DestDir: "{app}\runtime"`）压成 `EduBuddySetup.exe`；
2. **版本号自动注入**：build.ps1 编译前读 `deeptutor/__version__.py`，经
   `/DMyAppVersion=0.2.0+dt1.6.10` 传给 iss——安装包属性里的版本永远跟随后端，不用手改。
   裸跑 ISCC 会得到占位版本 `0.2.0+dtSET-BY-build.ps1`（故意难看，便于识别无效包）；
3. **升级清扫**：`[InstallDelete]` 装新版时自动删 `%LOCALAPPDATA%\EduBuddy\runtime`
   旧托管缓存（ADR-005 遮蔽事故的修复）。

## 3. 流水线逐步（build.ps1 在做什么）

| 阶段 | 日志 | 脚本 | 输入 → 输出 | 增量行为 |
|---|---|---|---|---|
| ① 前端构建 | `[0/3]` | `web/` 下 `npm run build` + `scripts/prepare_web_package.py --skip-build` | 前端源码 → `deeptutor_web/`（server.js 就绪） | npm 自身缓存 |
| ② 运行时组装 | `[1/3]` | `tools/build_runtime.py --no-zip` | 本地源码 + cache 里的 python/node zip → `runtime-build/staging/` | 版本一致且指纹未变 → 只跑冒烟+门禁（十几秒）；否则自动重装 |
| 2b 改品牌 | `[1b]` | `tools/rebrand.py` | staging → staging | 幂等，秒级 |
| ③ 壳编译 | `[2/3]` | `python -m PyInstaller --noconfirm --clean build\EduBuddyDesktop.spec` | desktop/ → `dist/EduBuddyDesktop.exe` | 每次全量重打 |
| 2b 便携包 | `[2b]` | `tools/make_portable.py`（仅 `-MakePortable`） | staging → `dist/EduBuddyPortable.zip` | 慢：~500MB / 2.1 万文件 / 约 8 分钟 |
| ④ 安装包 | `[3/3]` | `ISCC /DMyAppVersion=... build\installer.iss` | 壳 exe + staging → `dist/EduBuddySetup.exe` | 每次全量重编（lzma2 压缩耗时主要在这） |

前置条件（一次性）：

```powershell
# desktop-shell/.venv：壳的构建环境（PyInstaller 在这里）
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -U pip pywebview pillow pyinstaller
# Inno Setup 7：https://jrsoftware.org/isdl.php（默认安装路径即可被自动找到）
# Node + npm：前端构建用
```

## 4. 安装位置：装完之后东西都在哪

双击 `EduBuddySetup.exe`（`PrivilegesRequired=lowest`，按用户装、不弹 UAC）后：

| 位置 | 内容 | 生命周期 |
|---|---|---|
| `%LOCALAPPDATA%\Programs\EduBuddy\`（iss 里的 `{app}`） | `EduBuddyDesktop.exe` + `assets\` + `runtime\`（完整运行时树） | 随安装/卸载增删 |
| `%LOCALAPPDATA%\EduBuddy\runtime\` | 旧版 runtime.zip 自解压的托管缓存 | **新版安装时 `[InstallDelete]` 自动清除**；卸载时也删 |
| `%USERPROFILE%\EduBuddy\` | 学习数据/工作区：知识库、对话、记忆 | **卸载也保留**，与程序分离 |
| 开始菜单 + 桌面（可选勾选） | 快捷方式 → `{app}\EduBuddyDesktop.exe` | 随安装/卸载增删 |

要点：**程序和数据分离**。重装/升级只动前两行；用户数据在第三行，永不丢。
（可用环境变量 `DEEPTUTOR_DESKTOP_ROOT` / `DEEPTUTOR_DESKTOP_HOME` 重定向，便携场景用。）

壳运行时选哪棵运行时树，由 `desktop/runtime.py` 的 `select_runtime_base()` 统一裁决：
exe 旁自带 > 解包目录 > `%LOCALAPPDATA%` 托管缓存，且**版本严格更高才允许缓存越位**
（ADR-005）——所以"跑起来的版本"和"关于页显示的版本"永远同源。

## 5. 怎么打包

### 5.1 一条命令（日常答案）

无论改了什么，都是同一条命令——`build.ps1` 内部有增量机制，没变的部分自动秒过：

```powershell
cd D:\studio\DeepTutor\desktop-shell
powershell -ExecutionPolicy Bypass -File build\build.ps1
```

### 5.2 改动范围 → 耗时感受

| 你改了什么 | ① 前端 | ② 运行时 | ③ 壳 | ④ 安装包 | 整体感受 |
|---|---|---|---|---|---|
| deeptutor 后端 `.py` | 重跑 | **自动重装** + 门禁 | 重打 | 重编 | 十几分钟 |
| web 前端 | 重跑 | 指纹变了 → 自动重装 | 重打 | 重编 | 十几分钟 |
| 只改壳（`desktop/` 下） | 很快 | **秒过**（版本同+指纹未变） | 重打 | 重编 | 2~3 分钟 |
| 升版本发布 | — | 重装 | 重打 | 重编 | 先改 `deeptutor/__version__.py` |

### 5.3 开关

```powershell
build\build.ps1 -SkipInstaller   # 只要 EduBuddyDesktop.exe，不编安装包（调试壳最快）
build\build.ps1 -MakePortable    # 额外做 EduBuddyPortable.zip（~8 分钟，默认不做）
build\build.ps1 -MakeZip         # 额外产 dist/runtime.zip（onefile 分发路径用）
```

> `-SkipRuntime` 参数仅为兼容保留：**版本门禁现在永远执行，无法跳过**（曾因跳过
> 把 1.6.9 旧运行时打进新包，见 ADR-005）。日常不需要传它。

### 5.4 手动分步（理解流水线的最好方式）

`build.ps1` 逐行做的事，等价于：

```powershell
cd D:\studio\DeepTutor\desktop-shell

# ① 前端构建（产出 deeptutor_web 需要的 .next/standalone）
cd D:\studio\DeepTutor\web ; npm run build
cd D:\studio\DeepTutor
.venv 对应的 python scripts\prepare_web_package.py --skip-build

# ② 运行时组装 + 版本门禁（staging 已就绪且源码未变时只跑门禁，十几秒）
cd D:\studio\DeepTutor\desktop-shell
.\.venv\Scripts\python tools\build_runtime.py --no-zip
.\.venv\Scripts\python tools\rebrand.py          # 2b：staging 里 DeepTutor → EduBuddy

# ③ 壳编译
.\.venv\Scripts\python -m PyInstaller --noconfirm --clean build\EduBuddyDesktop.spec

# ④ 安装包（版本号手动传；build.ps1 跑时自动读）
& "C:\Program Files\Inno Setup 7\ISCC.exe" /DMyAppVersion=0.2.0+dt1.6.10 build\installer.iss
```

## 6. 交付前自检清单（打完 ≠ 完成）

1. **看门禁日志**：构建输出必须有
   `VERSION GATE OK: deeptutor <版本> == source <版本>`。
   出现 `VERSION GATE FAILED: staging has deeptutor X but source is Y` → staging 有残留，
   用 `tools\build_runtime.py --force-deeptutor`（顽固时 `--clean`）重装；
2. **看产物时间戳**：`dir dist\*.exe`，两个 exe 的修改时间应该是刚刚；
3. **查安装包版本**：右键 `EduBuddySetup.exe` → 属性 → 详细信息 → ProductVersion 应为
   `0.2.0+dt<源码版本>`。看到 `dtSET-BY-build.ps1` = 绕过 build.ps1 裸跑的 ISCC，无效包；
4. **装机验证**：安装后侧边栏徽章/关于页应显示新版本（壳读的运行时与显示版本同源，ADR-005）。

## 7. 已知坑

- **直接 `.\build.ps1` 报"在此系统上禁止运行脚本"**：Windows PowerShell 默认执行策略
  是 `Restricted`。推荐按 §5.1 的标准姿势跑（`powershell -ExecutionPolicy Bypass -File
  build\build.ps1`，只对当次进程生效、零系统改动）；想永久 `.\build.ps1` 可执行一次
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`（本地脚本放行、下载脚本仍需签名）；
- **打包前关掉正在运行的 EduBuddy**：staging 里的 python.exe 被占用时，清理旧文件会
  显式报错退出（故意设计：宁可构建失败，不打包混合运行时——曾产出"1.6.10 版本号 +
  1.6.9 前端 chunk"的混合体并通过门禁）。杀毒软件扫描占用也会触发，重试即可；
- **别在 WorkBuddy 等智能体沙箱里跑重活**（npm build / PyInstaller / ISCC）：
  沙箱限流 + 批量删除守卫会干扰。在普通 PowerShell 窗口跑；
- **别手动改 `installer.iss` 里的版本号**：由 build.ps1 自动注入，手改会在下次构建被覆盖；
- **`pip install --target` 不卸旧版本**（会叠加 dist-info）：build_runtime.py 已在重装前
  先清旧 `deeptutor*`，且清理失败会显式报错，不要绕过。

## 8. 参考

| 文件 | 角色 |
|---|---|
| `build/build.ps1` | 一键流水线（本指南 §3 的编排者） |
| `tools/build_runtime.py` | 运行时组装 + 版本门禁 + 指纹增量 |
| `tools/rebrand.py` | staging 品牌改写（幂等） |
| `build/EduBuddyDesktop.spec` | PyInstaller 配方（壳 exe） |
| `build/installer.iss` | Inno Setup 脚本（安装包 + 升级清扫） |
| `desktop/runtime.py` | 运行时解析顺序（exe 旁 > 解包 > 缓存，版本择优） |
| `docs/adr/ADR-004` | 标题栏账号区（登录入口迁移） |
| `docs/adr/ADR-005` | 运行时解析顺序事故与修复 |
