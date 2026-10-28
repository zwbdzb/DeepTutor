# ADR-006: 服务器驱动的更新链路（检查 → 下载 → 静默安装）

## Status

**Accepted**

- 日期：2026-10-09
- 决策者：ThinkBuddy 桌面端
- 相关：`desktop/updater.py`、`desktop/menubar.py`、`build/build.ps1`、
  `desktop/auth/config.py`（`update_base`）

---

## Context

### 触发现象

桌面壳「文件 → 检查更新」之前是一个**本地占位探测**（`menubar.py` 旧
`_check_updates`）：比较 exe 文件的 mtime 与 ``%LOCALAPPDATA%\ThinkBuddy\
update_baseline.txt`` 里记录的基线，mtime 明显变新（说明 exe 被安装器
重新覆盖过）就 toast「发现新版本已就绪，重启 ThinkBuddy 后生效」。

它**从不访问网络、不下载、不安装**——所以出现了"明明没有实现更新逻辑，
却会提示'发现新版本已就绪'"的错位：那个提示是**本地 mtime 推断**的结果，
不是服务器检查的结果。它也无法区分"升级"与"装了个更旧的"，基线文件被删
又会导致永远不提示。

需要在**不从零发明轮子**的前提下，实现真正的"从服务器检查更新 + 下载更新"。
约束：

1. 壳是 PyInstaller 打包的 onefile exe；更新目标 = 整个安装包
   （ThinkBuddySetup.exe，内含壳 + 离线 runtime：python + deeptutor + node + web）。
2. 已有一套成熟的 Inno Setup 安装器，负责替换文件、清理托管 runtime
   缓存（`[InstallDelete]`）、注册卸载项、建快捷方式。
3. `installer.iss` 的 `PrivilegesRequired=lowest` → **per-user 安装无需 UAC 提权**。
4. 服务器通信先例是纯标准库 `urllib`（`thinkbuddy_website.py`），且拒绝引入
   第三方网络库。
5. 版本体系有三个互不相同的轴（见下），更新判定必须钉死一个锚点，防止误判。

### 版本体系澄清（本项目三轴）

| 版本号 | 定义位置 | 随发布变化？ |
| --- | --- | --- |
| 壳版本 `0.1.3` | `desktop/__init__.py#__version__` | **否**——PyInstaller 编译期硬编码，发布不回写 |
| 安装器版本 `0.2.0+dt1.6.11` | `build.ps1` 读 `deeptutor/__version__.py` 拼出 `"0.2.0+dt" + 引擎版本`，经 `/DMyAppVersion` 注入 `installer.iss` | **是**——每次引擎 bump 都变；Inno 安装时写入卸载注册表 `DisplayVersion` |
| 引擎版本 `1.6.x` | `deeptutor/__version__.py`（唯一事实源） | 是 |

结论：**「本机当前装的是哪一版」只有卸载注册表 `DisplayVersion` 说得清**——
它同时编码壳产品线（0.2.0）与引擎版本（dt 后缀）。壳源码里的 `0.1.3` 因不随
发布回写，**不能**作为更新判定锚点。

---

## Decision

采用 **方案 A：服务器静态清单 + 下载 + Inno 静默安装** 的更新链路：

**发布侧（`build.ps1` 增量）**
1. Inno 出包 `dist\ThinkBuddySetup.exe` 后，复制为带版本名
   `ThinkBuddySetup-0.2.0_dt1.6.11.exe`（版本号里的 `+` 转 `_`，避免 URL 路径
   里的 `+` 被某些服务器当作空格）。
2. 计算 SHA256，生成 `dist\latest.json`（无 BOM UTF-8），与安装包一起上传到
   更新源目录。

**服务器（静态托管，不写后端逻辑）**

```
https://thinkbuddy.hanyoai.com/update/latest.json
https://thinkbuddy.hanyoai.com/update/ThinkBuddySetup-0.2.0_dt1.6.11.exe
```

`latest.json` 契约：见 `desktop/updater.py` 模块 docstring。必要字段
`version` / `url` / `sha256` 缺失即视为无效清单（抛错，不静默当"已最新"）。

**客户端（新增 `desktop/updater.py` + 改 `menubar._check_updates`）**
1. 菜单回调（独立线程）`GET latest.json`，短超时 8s。
2. 版本比对锚点 = 卸载注册表 `DisplayVersion`（读不到则视为旧版 / 开发态）。
3. 有新版本 → 系统 MessageBox 询问（带 changelog 摘要）。
4. 流式下载到 `%LOCALAPPDATA%\ThinkBuddy\updates\`，实时 SHA256，逐 1.2s
   节流回报 toast 进度；校验失败丢弃 `.part` 并中止，绝不静默装坏包。
5. 二次确认后 `ThinkBuddySetup-xxx.exe /VERYSILENT /SUPPRESSMSGBOXES
   /NORESTART` 静默运行。安装器自带 `CloseApplications=yes` 关闭当前实例并
   `[Run] postinstall` 自动拉起**新版**实例 → 用户感知为"点确定 → 关一下 →
   新版自己回来"。
6. 降级路径：**开发态（非 frozen）**、**未配置更新源**、**服务器不可达**时
   回退到原 mtime 探测（`_probe_local_overwrite`），保住旧版「重启生效」提示。

`latest.json` 的 `min_version` 支持强制更新（低于阈值直接要求升级），
`changelog`/`published_at` 为展示字段，未知字段一律忽略（前向兼容）。

---

## Considered Options

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| **A. 静态清单 + Inno 静默安装** | 复用成熟安装器（清缓存/注册卸载/建快捷方式）；per-user 免 UAC；边界清晰、可单测 | 下载整包 ~145MB；安装期间应用需关闭 | ✅ 采纳 |
| B. 下载新 exe 自替换（bat 延迟替换） | 不依赖安装器 | onefile 自替换脆弱；文件占用/回滚/信号处理事故面大 | ❌ |
| C. 增量/差分更新 | 省流量 | 捆绑体（python+node+web）差分极复杂 | ❌ 过度设计 |

---

## Consequences

### 变容易的

- 「检查更新」从占位变成可用的真功能：新版本号在服务器一上文件，所有客户端
  点一下就能拉新。
- 版本判定唯一锚点（DisplayVersion），不再困惑于壳版本/引擎版本哪来的。
- 更新源可覆盖（`THINKBUDDY_UPDATE_URL` / `endpoints.json#update_url` / 默认
  `https://thinkbuddy.hanyoai.com/update`），内网/灰度/本地联调都能指过去。
  **2026-10-22 更新**：随 endpoints 键值语义重定位（`api_base` 归主站、新增
  `oauth_base`），`THINKBUDDY_UPDATE_URL` 与 `endpoints.json#update_url` 已废弃，
  更新源改为**硬编码派生自主站** `<api_base>/update`（联调主站时自动跟随本地，
  不会误弹生产更新）。
- `build.ps1` 一键产出"安装包 + manifest"，上传即发布，无手工步骤。

### 变难 / 需要小心的

- **`latest.json` 与安装包必须一起上传**：manifest 先于安装包上传统会给出
  下载 404。发布流程要保证"先文件后清单"（原子性靠发布脚本顺序保证，目前
  无跨文件事务）。
- **URL 里的 `+` 必须转义**：已用 `_` 替代；若今后直接改 `AppVersion` 里的
  连接符需同步处理文件名/URL 编码。
- **`AppVersion` 前缀 `0.2.0` 是 build.ps1 手写的**：下次壳产品线 bump 时
  需要手动改这里（与注入 installer.iss 是同一处，无分叉）。
- **更新判定拧在"安装包版本"上**：只改了源码没打包发布就不会有更新提示，
  这是预期行为（更新 = 换安装包）。

### 未决 / 后续

- 自动后台检查（启动时静默查一次，仅在"发现新版"时打扰）可作下一步；
  当前只做用户主动点击触发的检查。
- 安装包数字签名（Authenticode）尚未涉及；SHA256 已保证完整性，但防中间人
  篡改依赖 HTTPS 传输，签名可作后续加强。
- 强制更新（低于 `min_version`）的 UI 目前是"提示用户更新"，未做"阻断使用"。

## macOS 行为差异（2026-10-16）

mac 侧复用同一套检查链路（`latest.json` + 版本比对），但**没有安装环节**：

| 环节 | Windows | macOS |
| --- | --- | --- |
| 检查更新 | `urllib` + 系统 SSL | 同左，但 SSL context 用 `_system_ssl_context()`——导出系统钥匙串证书（System/SystemRoot/login）灌入，代理自签根证书（企业网关/Clash mitm）不再 `CERTIFICATE_VERIFY_FAILED`（Windows 的 WinHTTP 走系统信任库无此问题） |
| 版本锚点 | 安装包版本（`__version__` 注入 `<引擎版本>.<MMDDHHmm>`，mac 由 `tools/inject_version.py` 同源注入） | 同左 |
| 发现新版本 | 询问 → 下载 .exe → SHA256 → Inno 静默安装 → 自退重启 | 仅弹「发现新版本，请前往官网下载 macOS 版」——无 Inno/注册表链路，不自动下载安装（`menubar._check_updates` 的 darwin 分支） |
| 已是最新 | toast | toast（同左；服务器版本低于本机构建时也稳定提示最新） |

注意：mac 上发现新版本不会自动更新是**当前决策**（.app 分发 + 签名/公证
链路未建），不是缺陷；后续若上 Sparkle 类框架需另立 ADR。
