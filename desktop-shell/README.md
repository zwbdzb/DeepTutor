# EduBuddy

把港大开源的学习工作区 **DeepTutor**（[deeptutor.info](https://deeptutor.info/zh-cn/)）一键装成桌面客户端 **EduBuddy**。
以前要 `pip install -U deeptutor && deeptutor init && deeptutor start` 再开浏览器；
现在双击 `EduBuddyDesktop.exe`，弹出原生窗口，本地服务自动静默拉起，关窗口即停止。

## 它做了什么

- 原生 **WebView2 窗口**（1280×860）承载 DeepTutor 前端 `127.0.0.1:3782`
- **登录门控**（WorkBuddy 同款交互）：启动时若未登录，窗口停留在登录页
  （居中吉祥物 + 「EduBuddy，我帮你」+ 黑色登录按钮）；点击用系统浏览器打开
  Tokengine 平台注册/登录 → 客户端自动取回**域名 / 业务令牌 / 可用模型**写进
  DeepTutor 并进入应用。**退出登录即回到登录页，软件功能不可用**，重新登录后恢复
  （详见 [`docs/tokengine-integration.md`](docs/tokengine-integration.md) 与
  [`docs/adr/ADR-003-login-gate.md`](docs/adr/ADR-003-login-gate.md)）
- `deeptutor start` 以**隐藏子进程**运行；关闭窗口用 `taskkill /T` 连后端(:8001)+前端(:3782)一起停
- **单实例**
- 工作区数据在 `~/EduBuddy`, 日志落盘 `%LOCALAPPDATA%\EduBuddy\logs\app.log`、

## Tokengine 登录（三步）

```
登录门控页「登录」按钮（未登录启动时整页显示；登录后也可从**标题栏右上角账号区**发起）
   → 系统浏览器打开 <平台>/oauth/authorize（PKCE S256 + 本机回环回调 127.0.0.1:<随机端口>）
   → 手机号/账密登录 →「确认授权」→ 平台 302 回本机回环带 code
   → 客户端换取业务令牌，按 model_type 分流写入 ~/EduBuddy/data/user/settings/model_catalog.json
     （对话→llm/task、向量→embedding、图像/视频各归其位），随后自动进入应用
```

- **必须登录才能使用**：服务就绪后检查登录态，未登录停在登录页；登录成功自动进入
  应用；应用内退出登录（标题栏账号菜单）吊销令牌、摘除模型配置并回到登录页。
  离线/开发旁路：环境变量 `DEEPTUTOR_DESKTOP_SKIP_LOGIN=1`。
- 标题栏右上角账号区文案随登录态变化：`登录` → `等待浏览器…` → 用户名（点击弹原生账号菜单）。
  不再往页面注入任何悬浮控件（ADR-004）。
- **域名**三层解析，内置默认（生产
  `tokengine.hanyoai.com`）→ `endpoints.json` / 环境变量显式覆盖（联调指向，
  回环地址按本地派生 `/v1`）→ userinfo 下发的中继地址。行为完全可预测。
- 只有**业务令牌**（`sk-Tok...`）会写进 DeepTutor；`access_token`/`refresh_token` 仅用于换码与吊销，
  DPAPI 加密落盘，**绝不进入对话请求**。
- 联调期指向本地平台：把 `api_base` 写进 `%LOCALAPPDATA%\EduBuddy\endpoints.json`，
  或设 `TOKENGINE_API_BASE=http://127.0.0.1:3000`。**装好的包无需重新打包即可改指向。**
- 登录按钮默认就绪，无需额外开关；界面调试可用 `DEEPTUTOR_DESKTOP_DEBUG=1` 打开登录页的技术面板。

## 三种运行形态

| 形态 | 谁用 | 需要什么 | 默认产出 |
|---|---|---|---|
| `EduBuddyDesktop.exe`（单文件） | 开发者 / 已有 deeptutor 的机器 | 系统里有 Python+Node+`deeptutor` | ✅ |
| `EduBuddySetup.exe`（Inno 安装器） | 分发 | 安装向导 + 开始菜单 + 卸载器，自带运行时 | ✅ |
| `EduBuddyPortable.zip`（内置运行时） | 免安装场景 | **什么都不用装**，解压即用 | 加 `-MakePortable` |

## 快速开始（本仓库开发态）

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -U pip pywebview pillow pyinstaller
.\.venv\Scripts\python -m desktop.main        # 开发跑起来
```

> 开发态复用系统里的 `deeptutor` 与 `node`；没有则首次运行会自动用系统 python 建
> 独立 venv 并 `pip install -U deeptutor`。

## 构建

**默认只出两个包**：`EduBuddyDesktop.exe` + `EduBuddySetup.exe`。
便携 zip 默认不制作（它只是同一份运行时的另一种分发形态，每次重压 ~500MB/2.1 万文件、约 8 分钟），
需要时显式加 `-MakePortable`。

**日常与首次构建都是同一条命令**——`build_runtime.py` 自带增量：staging 运行时与源码
一致且未变化时只跑冒烟测试 + 版本门禁（十几秒），源码变了自动重装。版本门禁不可跳过
（曾因跳过把 1.6.9 旧运行时打进新包，见 `docs/adr/ADR-005`）。

```powershell
powershell -ExecutionPolicy Bypass -File build\build.ps1                    # 日常/首次
powershell -ExecutionPolicy Bypass -File build\build.ps1 -SkipInstaller    # 只要壳 exe，不编安装包
powershell -ExecutionPolicy Bypass -File build\build.ps1 -MakePortable     # 额外做便携 zip（~8 分钟）
```

打包机制科普（runtime / PyInstaller / 安装位置 / 自检清单）见
[`docs/packaging-guide.md`](docs/packaging-guide.md)。

也可以分步手动执行（`build.ps1` 做的就是这几步）：

```powershell
.\.venv\Scripts\python tools\build_runtime.py --no-zip                                   # 1) 离线运行时 + 版本门禁
.\.venv\Scripts\python tools\rebrand.py                                                  # 2) staging 改品牌（幂等）
.\.venv\Scripts\python -m PyInstaller --noconfirm --clean build\EduBuddyDesktop.spec     # 3) 桌面壳 exe
& "C:\Program Files\Inno Setup 7\ISCC.exe" /DMyAppVersion=0.2.0+dt<版本> build\installer.iss   # 4) 安装向导（版本号自动读自 deeptutor/__version__.py；裸跑会得到占位版本）
.\.venv\Scripts\python tools\make_portable.py                                            # 5) 便携包（可选）
```

产物都在 `dist/`：

| 文件 | 说明 | 默认 |
|---|---|---|
| `EduBuddyDesktop.exe` | 桌面壳单文件（~14MB），机器上需已有 Python+Node+`deeptutor` | ✅ |
| `EduBuddySetup.exe` | 安装向导，内置运行时装进 `{app}\runtime`（~170MB，lzma2）；装完自动启动，卸载保留 `~/EduBuddy` 学习数据 | ✅ |
| `EduBuddyPortable.zip` | 解压即用的离线包，**默认不制作** | 加 `-MakePortable` |

> 安装器直接从 `dist\EduBuddyDesktop.exe` + `runtime-build\staging` 打包，**不依赖便携 zip**，
> 所以「两个包」这条路径是自洽的。
>
> 构建耗时提醒：**别把 `make_portable.py` / ISCC 放进智能体沙箱**。沙箱会限流到
> ~110KB/s，同样的活儿在沙箱外 8 分钟就能跑完。

> 瘦身说明：`tools/build_runtime.py` 会把 deeptutor 的依赖闭包装进 embeddable
> Python（faiss/PyMuPDF/llama-index 等本就很大）。为了减小安装包，默认裁剪了
> deeptutor 已弃用的 `litellm`（v1.0.0-beta.3 起改为原生 SDK）、可选加速
> `hf_xet`、AWS `boto3/botocore`（仅 llama_index 的 AWS 工具懒加载用）、pip 生成的
> `bin/` 脚本与帮助文件。裁剪后的运行时通过了 deeptutor 全量导入冒烟测试；
> 若你确实需要其中某些云功能，在 `site-packages` 里放回对应包即可。

## 项目结构

```
desktop/           桌面壳(Python，pywebview)
  main.py          入口：窗口 + 启动画面 + 生命周期
  inject.py        应用页面 toast 浮层注入（登录/账号入口已迁至标题栏，ADR-004）
  titlebar_account.py 标题栏账号区状态模型 + 同步线程
  native_menu_backend.py 自绘标题栏：菜单/窗口按钮/账号区
  process.py       deeptutor 子进程管理 + 端口健康检查
  runtime.py       运行时解析/自动安装/内置运行时
  splash.py        内嵌启动画面
  auth/            Tokengine OAuth(PKCE + 回环回调) + 模型目录写入
tools/
  build_runtime.py 构建离线运行时
  make_portable.py 组装便携包
  make_icon.py     生成图标
build/
  EduBuddyDesktop.spec   PyInstaller 配置
  installer.iss            Inno Setup 安装向导
  build.ps1                一键全流程
assets/             图标、截图
docs/architecture.md 架构说明
```

## 已知边界

- **端口**：默认前端 3782 / 后端 8001。被占用时启动画面会提示，改默认端口请改
  `desktop/process.py` 与 `runtime.py` 使用的常量。
- 首启 `deeptutor` 无 LLM Key 属正常，在界面 **Settings → LLM** 里配模型即可。
- 离线运行时包较大（内置完整 Next.js 前端 + embeddable Python + Node）。

## License

EduBuddy 只是 DeepTutor 的桌面壳，DeepTutor 本体遵循其仓库
[HKUDS/DeepTutor](https://github.com/HKUDS/DeepTutor) 的开源协议。
