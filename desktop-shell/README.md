# ThinkBuddy

把港大开源的学习工作区 **DeepTutor**（[deeptutor.info](https://deeptutor.info/zh-cn/)）一键装成桌面客户端 **ThinkBuddy**。
以前要 `pip install -U deeptutor && deeptutor init && deeptutor start` 再开浏览器；
现在双击 `ThinkBuddyDesktop.exe`，弹出原生窗口，本地服务自动静默拉起；关窗后收进系统托盘（Windows 通知区域 / macOS 菜单栏），选「退出」才真正结束。

## 它做了什么

- 原生 **WebView2 窗口**（1280×860）承载 DeepTutor 前端 `127.0.0.1:3782`
- **登录门控**（WorkBuddy 同款交互）：启动时若未登录，窗口停留在登录页
  （居中吉祥物 + 「懂了的那一下，很爽。」+ 黑色登录按钮）；点击用系统浏览器打开
  Tokengine 平台注册/登录 → 客户端自动取回**域名 / 业务令牌 / 可用模型**写进
  DeepTutor 并进入应用。**退出登录即回到登录页，软件功能不可用**，重新登录后恢复
  （详见 [`docs/tokengine-integration.md`](docs/tokengine-integration.md) 与
  [`docs/adr/ADR-003-login-gate.md`](docs/adr/ADR-003-login-gate.md)）
- `deeptutor start` 以**隐藏子进程**运行；关窗后后台服务继续运行（Windows 收进通知区域，macOS 收进菜单栏），点托盘图标 / 菜单「打开」/ 点 Dock 图标恢复窗口；托盘或文件菜单「退出」（macOS 也可 Cmd+Q）才停止服务。托盘不可用时关闭窗口直接退出。
- **单实例**（再次启动不重复开窗，而是把已打开的窗口带到前台）
- 工作区数据在 `~/ThinkBuddy`, 日志落盘 `%LOCALAPPDATA%\ThinkBuddy\logs\app.log`、

## Tokengine 登录（三步）

```
登录门控页「登录」按钮（未登录启动时整页显示；登录后也可从**标题栏右上角账号区**发起）
   → 系统浏览器打开 <平台>/oauth/authorize（PKCE S256 + 本机回环回调 127.0.0.1:<随机端口>）
   → 手机号/账密登录 →「确认授权」→ 平台 302 回本机回环带 code
   → 客户端换取 OAuth 凭证，再从 userinfo 取得业务令牌（`ai_token`）；模型名单
     则从中继 `GET /v1/models` 拉取（每项自带 model_type），
     按 model_type 分流写入 ~/ThinkBuddy/data/user/settings/model_catalog.json
     （对话→llm、向量→embedding、图像/视频/语音各归其位），随后自动进入应用
```

- **必须登录才能使用**：服务就绪后检查登录态，未登录停在登录页；登录成功自动进入
  应用；应用内退出登录（标题栏账号菜单）吊销令牌、摘除模型配置并回到登录页。
  离线/开发旁路：环境变量 `DEEPTUTOR_DESKTOP_SKIP_LOGIN=1`。
- 标题栏右上角账号区文案随登录态变化：`登录` → `等待浏览器…` → 用户名（点击弹原生账号菜单）。
  登录后的菜单提供“签到加积分”，不再往页面注入任何悬浮控件（ADR-004）。
- **签到直连 thinkbuddy-website**：客户端从 OAuth `userinfo` 的 `sub` 读取稳定用户 ID，
  直接 `POST /api/v1/checkin`，请求体只包含 `provider_user_id`。签到暂不鉴权，因此不会
  向 thinkbuddy-website 发送 OAuth `access_token`、手机号、积分或 quota，也不经过
  DeepTutor 本地后端代理；额度由 thinkbuddy-website 后台异步发放。
- **域名**三层解析，内置默认（生产
  `tokengine.hanyoai.com`）→ `endpoints.json` / 环境变量显式覆盖（联调指向，
  回环地址按本地派生 `/v1`）→ userinfo 下发的中继地址。行为完全可预测。
- 只有**业务令牌**（`sk-Tok...`）会写进 DeepTutor；`access_token`/`refresh_token` 仅用于换码与吊销，
  DPAPI 加密落盘，**绝不进入对话请求**。
- 登录按钮默认就绪，无需额外开关；界面调试可用 `DEEPTUTOR_DESKTOP_DEBUG=1` 打开登录页的技术面板。

## 站点与联调配置（endpoints.json）

客户端连接两个正式站点，两者都用**同一个覆盖文件**指向联调环境，装好的包**无需重新打包**：

| 站点 | 生产地址 | 角色 |
|---|---|---|
| **ThinkBuddy 主站** | `https://thinkbuddy.hanyoai.com` | 产品主站：官网跳转、签到、邀请页、更新源、遥测 |
| **Tokengine 平台** | `https://tokengine.hanyoai.com` | 登录认证（OAuth）+ token 服务（模型中继） |

**文件位置**：`%LOCALAPPDATA%\ThinkBuddy\endpoints.json`
（即 `C:\Users\<用户名>\AppData\Local\ThinkBuddy\endpoints.json`；macOS 为
`~/Library/Application Support/ThinkBuddy/endpoints.json`）。文件缺失或损坏时
静默回退到内置生产默认；改完保存后**重启应用生效**（启动时 toast 会提示
「API 地址已按 endpoints.json 对齐」）。

```json
{
  "api_base": "http://127.0.0.1:8000",
  "oauth_base": "http://127.0.0.1:3000"
}
```

可覆写的全部键（只写需要的键，没写的键继续用生产默认）：

| 键 | 站点 | 作用 | 缺省值 |
|---|---|---|---|
| `api_base` | ThinkBuddy 主站 | 主站根地址；更新源/遥测/邀请页**硬编码派生**自它 | `https://thinkbuddy.hanyoai.com` |
| `oauth_base` | Tokengine | 认证+token 服务根地址，派生全部 OAuth 端点与中继 | `https://tokengine.hanyoai.com` |
| `authorize_url` / `token_url` / `userinfo_url` / `revoke_url` | Tokengine | 单独覆盖某个 OAuth 端点（少用） | `<oauth_base>/oauth/<name>` |
| `relay_base` | Tokengine | OpenAI 兼容模型中继地址 | `<oauth_base>/v1` |

> 更新源固定为 `<api_base>/update`、遥测固定为 `<api_base>/v1/events`——**不占
> 环境变量和 endpoints.json 键位**，跟着主站走（联调主站时更新检查自动指向本地，
> 不会误弹生产更新）。

**优先级（高 → 低）**：环境变量 > `endpoints.json` > 内置生产默认。对应环境变量：
主站为 `THINKBUDDY_API_BASE`；Tokengine 键为 `TOKENGINE_<键名大写>`
（如 `TOKENGINE_OAUTH_BASE`）。环境变量适合临时联调（不改文件），
`endpoints.json` 适合长期指向——**两个站点用同一种方式调试**。

> ⚠️ 2026-10-22 键值语义迁移：旧键 `api_base`（原指 Tokengine）已由
> `oauth_base` 取代，`api_base` 现指 ThinkBuddy 主站；旧键
> `thinkbuddy_website_url`/`update_url`/`telemetry_url` 已废弃。旧文件中的废弃键
> 会被忽略并在日志提示；若你的 endpoints.json 仍用旧语义写了 `api_base`，
> 请手工改为 `oauth_base`。

联调细节（OAuth 回调、签到链路、更新源自测）见
[`docs/tokengine-integration.md`](docs/tokengine-integration.md)；
端点解析实现见 [`desktop/auth/config.py`](desktop/auth/config.py)。

## 三种运行形态

| 形态 | 谁用 | 需要什么 | 默认产出 |
|---|---|---|---|
| `ThinkBuddyDesktop.exe`（单文件） | 开发者 / 已有 deeptutor 的机器 | 系统里有 Python+Node+`deeptutor` | ✅ |
| `ThinkBuddySetup-dt<版本>.<时间戳>.exe`（Inno 安装器） | 分发 | 安装向导 + 开始菜单 + 卸载器，自带运行时 | ✅ |
| `ThinkBuddyPortable.zip`（内置运行时） | 免安装场景 | **什么都不用装**，解压即用 | 加 `-MakePortable` |

## 快速开始（本仓库开发态）

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -U pip pywebview pystray pillow pyinstaller
.\.venv\Scripts\python -m desktop.main        # 开发跑起来
```

> 开发态复用系统 PATH 里的 `deeptutor` 与 `node`（Node 20+）；缺失时启动画面会
> 报错提示手动安装（`pip install -U deeptutor`）。打包版不受影响——安装器内置完整运行时。

## 构建

**默认只出两个包**：`ThinkBuddyDesktop.exe` + `ThinkBuddySetup-dt<版本>.<时间戳>.exe`。
便携 zip 默认不制作（它只是同一份运行时的另一种分发形态，每次重压 ~500MB/2.1 万文件、约 8 分钟），
需要时显式加 `-MakePortable`。

**日常与首次构建都是同一条命令**——整条链路增量：

- **运行时**：源码指纹（size+mtime）未变 → 只跑版本门禁（秒级）；改了代码 →
  `pip --no-deps` 只重装自家 3 个包（~20 秒）；`pyproject.toml` 依赖变了 →
  全量重装一次，之后恢复快速路径。版本门禁不可跳过（曾因跳过把 1.6.9 旧运行时
  打进新包，见 `docs/adr/ADR-005`）。
- **web**：源码未变时跳过 next build；`web → deeptutor_web` 拷贝按指纹增量跳过。
- **壳**：PyInstaller 不带 `--clean`（复用分析缓存），UPX 关闭。
- **安装包**：`-FastInstaller` 用 `lzma2/fast`（快，体积 +10~15%）；不加则
  `lzma2` 最小体积（压缩 8~15 分钟）。

```powershell
# 日常迭代（推荐）：全增量 + 快速压缩，改代码后整包 ~1-3 分钟
powershell -ExecutionPolicy Bypass -File build\build.ps1 -FastInstaller

# 正式发布：严格内容指纹核验源码 + 最小体积安装包
powershell -ExecutionPolicy Bypass -File build\build.ps1 -StrictRuntime

powershell -ExecutionPolicy Bypass -File build\build.ps1 -SkipInstaller    # 只要壳 exe
powershell -ExecutionPolicy Bypass -File build\build.ps1 -MakePortable     # 额外做便携 zip（~8 分钟）
```

两个开关正交：`-FastInstaller` 管压缩速度，`-StrictRuntime` 管源码校验强度，
可同时使用。每阶段耗时都会打印（`[阶段名] done in mm:ss` + 总耗时）。

打包机制科普（runtime / PyInstaller / 安装位置 / 自检清单）见
[`docs/packaging-guide.md`](docs/packaging-guide.md)。

也可以分步手动执行（`build.ps1` 做的就是这几步）：

```powershell
.\.venv\Scripts\python tools\build_runtime.py --no-zip                              # 1) 离线运行时 + 版本门禁
.\.venv\Scripts\python tools\rebrand.py                                             # 2) staging 改品牌（幂等）
.\.venv\Scripts\python -m PyInstaller --noconfirm build\ThinkBuddyDesktop.spec      # 3) 桌面壳 exe
& "C:\Program Files\Inno Setup 7\ISCC.exe" /DMyAppVersion=1.6.14.10121554 build\installer.iss   # 4) 安装向导（版本 = 引擎版本.时间戳；裸跑会得到占位版本）
.\.venv\Scripts\python tools\make_portable.py                                       # 5) 便携包（可选）
```

产物都在 `dist/`：

| 文件 | 说明 | 默认 |
|---|---|---|
| `ThinkBuddyDesktop.exe` | 桌面壳单文件（~14MB），机器上需已有 Python+Node+`deeptutor` | ✅ |
| `ThinkBuddySetup-dt1.6.14.10131621.exe` | 安装向导（文件名 = `dt` + AppVersion，全点分隔，Inno 直接输出、无中间副本），内置运行时装进 `{app}\runtime`（~190MB，lzma2）；装完自动启动，卸载保留 `~/ThinkBuddy` 学习数据 | ✅ |
| `ThinkBuddyPortable.zip` | 解压即用的离线包，**默认不制作** | 加 `-MakePortable` |

> 安装器直接从 `dist\ThinkBuddyDesktop.exe` + `runtime-build\staging` 打包，**不依赖便携 zip**，
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
  （图标更新：品牌资产事实源在仓库根 brand/logo/，
    同步 cp brand/logo/thinkbuddy-icon.ico → assets/icon.ico）
build/
  ThinkBuddyDesktop.spec   PyInstaller 配置
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

ThinkBuddy 只是 DeepTutor 的桌面壳，DeepTutor 本体遵循其仓库
[HKUDS/DeepTutor](https://github.com/HKUDS/DeepTutor) 的开源协议。
