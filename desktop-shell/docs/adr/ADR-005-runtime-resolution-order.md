# ADR-005: 运行时解析顺序——随包分发优先，托管缓存只配当兜底

## Status

**Accepted** — 2026-09-23（事故驱动）。

- 决策者：EduBuddy 桌面端
- 相关：`desktop/runtime.py`（`select_runtime_base`）、`build/installer.iss`、
  `tools/build_runtime.py`、`docs/adr/ADR-003-login-gate.md`（登录门控）

---

## Context

2026-09-23 用户反馈：**刚打出来的安装包装完仍显示 deeptutor 1.6.9**，而源码
已是 1.6.10（`build.ps1` 注释记录过同类事故：1.6.7、1.6.9 各发生一次，当时
归因为「跳过 runtime 构建步骤」，本次在版本门禁已强制执行的前提下复发）。

实测三处运行时并存、版本各异：

| 位置 | 版本 | 角色 |
| --- | --- | --- |
| 源码树 `deeptutor/__version__.py` | 1.6.10 | 构建源头 |
| `%LOCALAPPDATA%\EduBuddy\runtime`（托管缓存） | **1.6.9** | 旧版 runtime.zip 自解压产物 |
| `%LOCALAPPDATA%\Programs\EduBuddy\runtime`（安装器自带） | 1.6.8 | 更旧的一次安装残留 |

根因：`runtime.py` 的候选顺序是 `(RUNTIME, EXE_DIR/runtime, APP_DIR/runtime)`
——**托管缓存排在安装器自带运行时前面**，且 `resolve_deeptutor_cmd` 在第一
个含 `python.exe` 的候选处直接返回。于是新装包自带的 1.6.10 运行时被旧缓存
1.6.9 整个遮蔽：后端跑的是 1.6.9，侧边栏版本徽章（读后端 status）如实显示
v1.6.9。版本门禁防的是「staging ≠ 源码」，防不了「装机后的缓存遮蔽」。

## Decision

1. **顺序铁律**：候选改为 `(EXE_DIR/runtime, APP_DIR/runtime, RUNTIME)`——
   随本 exe 分发的运行时与 exe 是同一次构建产物，天然自洽，永远优先；
   `%LOCALAPPDATA%` 托管缓存只是 runtime.zip 自解压的产物，只配兜底。
2. **版本感知择优**（`select_runtime_base`）：多个候选并存时，仅当后位候选
   的 deeptutor 版本（读 dist-info，不执行代码）**严格更高**才越位；平手时
   保持「exe 旁优先」的确定性。`resolve_deeptutor_cmd` 与
   `resolve_deeptutor_version` 必须走同一个选中树——跑起来的版本和显示的
   版本永远同源。
3. **安装/卸载时清理托管缓存**（`installer.iss` → `[InstallDelete]` /
   `[UninstallDelete]` 删除 `{localappdata}\EduBuddy\runtime`）：本安装器
   自带完整 runtime 树，缓存纯属冗余；清掉后机器上不存在可遮蔽的旧副本。
   用户学习数据（`%USERPROFILE%\EduBuddy`）不受影响。
4. **构建清理失败必须响**（`build_runtime.py`）：重装 deeptutor 前清理旧包
   的 `rmtree` 不再 `ignore_errors`——文件被占用（应用未关/杀毒扫描）时
   显式失败并提示关程序重试，杜绝「1.6.10 版本号 + 1.6.9 前端 chunk」的
   混合运行时静默通过版本门禁（门禁只查 `deeptutor.__version__`）。

## Consequences

**更容易的**：
- 升级安装后，应用必然运行安装器自带的运行时；版本徽章与安装包元数据一致；
- 「关于」对话框的 deeptutor 版本与实际运行的树同源，排查不再错位；
- 机器上残留任意旧缓存都不再影响新包行为（缓存只在严格更新时才被选用）。

**更困难的 / 需要留意**：
- 便携/onefile 场景（exe 旁没有 runtime 树）行为不变，仍走托管缓存或 PATH；
- 若未来安装器不再内置 runtime 树（改回 runtime.zip 自解压），本 ADR 的
  `[InstallDelete]` 需要同步撤销，否则会把刚装的缓存删掉；
- `build_runtime.py` 的显式失败意味着：打包时必须先关掉正在运行的 EduBuddy
  （此前静默跳过，看似成功实则产出混合运行时——宁可失败）。

## Verification

`tools/_verify_fixes.py` 第 7 节固化三个场景：exe 旁压过旧缓存（事故本尊）、
缓存严格更新才越位、平手 exe 旁优先。回归直接跑：
`.venv/Scripts/python tools/_verify_fixes.py`。
