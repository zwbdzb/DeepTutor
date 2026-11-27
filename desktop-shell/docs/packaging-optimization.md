# 打包优化决策记录（Windows 安装耗时）

> 状态：**批次 0 / 1 已落地；批次 2 只落「审计 + dry-run 骨架」，裁剪规则未批准；批次 3 未启用。**
> 只读诊断报告见仓库根 `docs/PACKAGING_DIAGNOSIS.md`（该目录被 `.gitignore` 忽略，属一次性证据）。
> 本文只记录**决策**与**已落地的改动**，不重复诊断数据。

---

## 1. 一句话背景

安装耗时的大头是 **66,912 次文件创建**与**归档解码**两段，各占约一半：

| 组成 | 估算 | 依据 |
| --- | --- | --- |
| xz 解码 1.28 GB | ~22 s | 实测 bsdtar 解码 57.8 MB/s（§6.2） |
| 66,912 次文件创建 + 实时防护扫描 | ~23 s | 0.34 ms/文件，与 0.39 ms/文件基线同量级 |

由此得到三条硬结论，后续所有决策都服从它们：

| 结论 | 依据 |
| --- | --- |
| 保留 `runtime.tar.xz` 中间层，**绝不让 Inno 逐文件展开** | Inno 逐文件写入 6.21 ms/个（`installer.iss` 历史实测）vs bsdtar 0.674 ms/个，逐文件展开会把 70 s 变成 ~7 分钟 |
| 两条腿一起走：**削减文件数** + **换更快的解码格式** | 解码与落盘是两个独立可攻的项（§6.4 修正了 v1 的低估） |
| 任何优化必须用 **3 轮中位数**验证 | 同一操作两轮实测相差 ±40%（45.11 s vs 64.34 s） |

---

## 2. 决策总表

| 批次 | 内容 | 状态 | 改动文件 |
| --- | --- | --- | --- |
| 0 | staging 只读审计 | **已落地** | `tools/audit_staging.py`（新增）、CI 2 步 |
| 1 | 预热异步化 | **已落地** | `build/installer.iss`（3 处）、`tools/diag/measure-inno-install.ps1`（标签） |
| 2 | 按审计数据裁剪 | **骨架已落地，规则未批准** | `tools/prune_staging.py`、`build/prune-policy.json`、`build/runtime-budget.json`、CI 3 步 |
| 3 | zstd 替换 xz | **已实现为可选开关（默认关闭）** | `build.ps1`（`-ZstdArchive`）、`installer.iss`（`{#RuntimeArchive}`）、`.gitignore`、诊断脚本 2 支 |
| ✗ | Inno 逐文件展开 | **永久否决** | — |
| ✗ | 增量升级（只写变更） | **否决**（见 §7） | — |
| ✗ | Defender 排除项 | **否决**（见 §7） | — |
| ✗ | 只发 `.pyc`（删 `.py`） | **否决**（见 §7） | — |

---

## 3. 批次 0：staging 只读审计

### 3.1 为什么必须先审计

66,912 个文件的构成此前全是推测。盲砍的典型事故是 `**/tests` 通配符误删
`setuptools` / `urllib3` 里被 `__init__` 引用的 `tests` 子包 → 运行期
`ModuleNotFoundError`，且只在特定功能路径上触发，极难复现。

### 3.2 落地内容

```powershell
cd desktop-shell
.venv\Scripts\python.exe tools\audit_staging.py --top 20 --json "$env:TEMP\audit-staging.json"
```

输出 6 组数据（全部只给汇总与 TopN，不打印全量文件列表）：

| 分组 | 用途 |
| --- | --- |
| `totals` | 文件数 / 目录数 / 总字节 / 平均体积 |
| `by_ext` | 按扩展名聚合（`.py` vs `.pyc` vs 二进制） |
| `by_subtree` | 按「前两段路径」聚合 —— **决定裁剪主战场** |
| `by_leaf_dir` | 按叶子目录聚合 —— 找「单目录数千文件」的具体目标 |
| `py_pyc` | `.py`/`.pyc` 配对统计（`modules` / `paired` / `py_without_pyc`） |
| `signals` | `node/npm`、`node/include`、`*.map`、`.d.ts`、`.dist-info`、`test*/` 内文件数 |

CI 在 `Build runtime + exe + installer` 之后自动跑同一命令，并把结果作为
`staging-audit` artifact 上传（见 `build-desktop.yml` 的 *Audit runtime staging* 步）。

### 3.3 拿到数据后怎么读（判定规则）

| 观察 | 含义 |
| --- | --- |
| `by_subtree` Top1 占比 > 30% | 先看这个子树是什么：自家包 / 第三方 SDK / web 产物，三者对策不同 |
| `py_pyc.paired` 接近 `.py` 总数 | python 段文件数**基本不可压**（`.pyc`-only 已被否决），只能靠删依赖 |
| `signals` 里 `node/npm|npx`、`node/include` 有值 | 高确定性、低风险，可作为首批 approved 规则 |
| `signals` 里 `test*/` 有值 | **不要**用通配符；只能逐包确认后写精确 `path-prefix` |

---

## 4. 批次 1：预热异步化（已落地）

### 4.1 改了什么

`build/installer.iss` 的 `ssPostInstall` 尾部，原来用
`ewWaitUntilTerminated` 阻塞等 `python -c "import deeptutor.api.main"` 跑完
（用户实测 6.26 ~ 13 s 全在关键路径上）。现在改为 `ewNoWait` 立即返回：

```pascal
if not WasInstalled then
begin
  DeployStage('warmup', '正在后台预热运行环境（不影响完成）…');
  PhaseTicks := TbTickCount;
  Exec(ExpandConstant('{app}\runtime\python\python.exe'),
       '-c "import deeptutor.api.main"', '', SW_HIDE, ewNoWait, ResultCode);
  Log('TB-DEPLOY timing warmup ' + IntToStr(TbTickCount - PhaseTicks) + 'ms');
  Log('TB-DEPLOY note warmup async=1 (not blocking)');
end
else
  Log('TB-DEPLOY warmup skipped (upgrade)');
```

三处配套改动：

1. `[Code] var` 新增 `WasInstalled: Boolean`；
2. `InitializeSetup` 开头读 `HKCU\...\{3B4FC0AE-...-59BBBF1D6305}_is1`
   （与 `InitializeWizard` 同源判定），写 `TB-DEPLOY was_installed=0|1`；
3. 函数头注释同步改写，避免文档与代码互相矛盾。


### 4.2 保留 tag 与计时键的用意

`DeployStage('warmup', ...)` 的 tag **故意不变**：

- `desktop/updater.py` 的进度小窗按 `'deploy warmup'` 匹配中文文案，换 tag 会让
  静默更新的阶段文案掉回通用分支；
- `tools/diag/measure-inno-install.ps1` 的正则只认 `extract|swap|warmup|total`，
  换键会让该阶段从报告里静默消失。

因此 `TB-DEPLOY timing warmup` 的语义从「等待耗时」变成「派发耗时」（应 ≈0），
诊断脚本的标签同步改成 `3c. 预热派发（异步，不阻塞）`，并新增
`TB-DEPLOY note warmup async=1` 作为显式标记。

### 4.3 为什么只在首装做

升级时目标目录里的文件早已被实时防护扫过，再预热是纯占 CPU。
判定用卸载键存在与否，**纯读注册表**，无副作用。

### 4.4 验收与回滚

| 项 | 内容 |
| --- | --- |
| 日志判据 | 首装出现 `timing warmup`（应接近 0，**待测量**）+ `note warmup async=1`；升级出现 `warmup skipped (upgrade)` |
| 功能判据 | 装完立刻启动，后端仍能就绪（`%LOCALAPPDATA%\ThinkBuddy\logs\app.log` 出现前端就绪行） |
| 已知副作用 | 首窗打开后头几秒 CPU/磁盘略高（预热与其并行），需在发布说明注明 |
| 结构校验 | `python tools/iss_codecheck.py build/installer.iss --baseline <git show HEAD 版本>` 无 delta 异常 |
| 回滚 | 还原该 `if` 块即可；无格式/结构变更，不涉及产物 |

> ⚠️ 本机未安装 Inno Setup，**ISCC 编译未在本地验证**；`iss_codecheck.py` 只做
> 结构性 delta 校验（begin/end、if/then、try/finally、`;else`）。首次 CI 构建即为编译验证。

---

## 5. 批次 2：裁剪（骨架已落地，规则未批准）

### 5.1 安全契约

`tools/prune_staging.py`：

| 行为 | 说明 |
| --- | --- |
| 默认 dry-run | 不加 `--apply` 不删任何文件 |
| `--apply` 拒绝执行 | 只要有一条 active 规则 `state != "approved"` → exit 3，什么都不删 |
| protected 优先 | 任何规则命中 protected 一律跳过 |
| 可审计 | `--report` 输出删除清单（规则 / 路径 / 字节），随 artifact 存档 |

### 5.2 protected 的语义（v2，关键设计）

**按「代码资产类型」+「自有包目录」保护，而不是整目录保护。**

```json
"protected": [
  "deeptutor", "deeptutor_cli", "run_deeptutor.py",
  "**/__init__.py",
  "**/*.py", "**/*.pyc", "**/*.js", "**/*.mjs", "**/*.cjs", "**/*.json",
  "**/*.html", "**/*.css", "**/*.node", "**/*.exe", "**/*.dll", "**/*.pyd",
  "**/*.so", "**/*.wasm"
]
```

理由（由实测暴露）：整目录保护 `deeptutor_web` 会让「只删 `*.map`」这类规则完全失效；
而按扩展名保护恰好把「误删代码」这类最危险的事故挡在外面，同时仍允许删除
调试产物（`*.map`）与包管理器 CLI（`node/npm` 等无扩展名文件）。

已用合成树验证（17 个文件，命中/保护行为逐一核对）：

| 输入 | 结果 |
| --- | --- |
| `node/npm`、`node/npm.cmd`、`node/npx.cmd` | 删除（无扩展名 / `.cmd` 不在保护表） |
| `node/include/node/v8.h`、`node/CHANGELOG.md` | 删除 |
| `deeptutor_web/static/**/*.map` | 删除（含顶层 `*.map`，靠 `**/` 零级变体匹配） |
| `deeptutor_web/server.js` | **保留**（`.js` 受保护） |
| `deeptutor/plugin.py`、`deeptutor/tests/test_x.py` | **保留**（自有包目录 + `.py`） |
| `urllib3/tests/__init__.py`、`urllib3/__init__.py` | **保留**（`.py` 受保护 —— 这正是 `tests-anywhere` 通配符被否决的技术保障） |
| `node/node.exe` | **保留**（`.exe`） |
| `deeptutor-1.7.3.dist-info/METADATA` | **保留**（关于页版本号唯一来源） |

实测：dry-run 命中 9/17；`--apply` 在规则未批准时 exit 3 且 0 文件被删；
用批准副本执行后恰好删除这 9 个非代码文件、清掉 5 个空目录、代码资产全部保留。


### 5.3 当前策略文件状态

`build/prune-policy.json`：4 条 `candidate` + 2 条 `rejected`（留档写明否决理由，防止后人重复提案）。

| rule id | 类型 | 状态 | 说明 |
| --- | --- | --- | --- |
| `node-npm` | path-prefix | candidate | 运行时只执行 `node.exe` |
| `node-corepack` | path-prefix | candidate | 版本切换器，运行时不使用 |
| `node-headers` | path-prefix | candidate | C 头文件 / README / CHANGELOG |
| `web-sourcemaps` | glob-in-scope | candidate | 需确认未被打进 standalone manifest |
| `tests-anywhere` | glob `**/tests/**` | **rejected** | 跨树通配不可控（且 `.py` 已受保护） |
| `distinfo-except-deeptutor` | glob | **rejected** | 部分库 import 期读自身 METADATA |

> 这 4 条只是**占位候选**，最终清单必须依据批次 0 的审计分布定稿。
> 每条规则的命中量在 CI 的 `prune-dryrun` artifact 里可见，不必本地跑。

### 5.4 批准一条规则前必须过的自检

```powershell
$py = "runtime-build\staging\python\python.exe"
& $py -c "import deeptutor, deeptutor_cli.main, deeptutor.api.main; print('imports OK')"
& $py -c "import pkgutil, deeptutor; print(len(list(pkgutil.iter_modules(deeptutor.__path__))))"
& $py -c "import importlib.metadata as m; print(m.version('deeptutor'))"
& "runtime-build\staging\node\node.exe" --version
```

第三条（`pkgutil.iter_modules` 计数）是**动态插件发现**的回归探针：数量与裁剪前
不一致就说明删到了被发现的模块。前两条任一条失败即回滚该规则（改回 `candidate`）。

### 5.5 预算门禁（enforce=false 起步）

`build/runtime-budget.json` 的 `max_staged_files` / `max_archive_mb` / `max_setup_mb`
当前均为 `null` = **不判定**，且 `enforce: false`。原因：在拿到首次审计实测前，
任何硬编数字都是猜的。收紧流程写在文件 `note` 里（先填实测值 → 观察 1~2 个 PR →
`enforce: true` → 每完成一条裁剪再下调 2%）。

CI 侧对应 `Runtime budget gate` 一步：超预算时 `enforce=false` 只打
`::warning`，不红构建。

---

## 6. 批次 3：zstd（**已实现为可选开关，默认仍走 xz**）

### 6.1 已确认的技术事实

| 事实 | 证据 |
| --- | --- |
| Windows 自带 bsdtar **链接了 libzstd 1.5.7** | `tar --version` → `bsdtar 3.8.9 ... libzstd/1.5.7`（本机 Win11 26300） |
| 因此 `tar -xf runtime.tar.zst` 可单趟解压，不必外挂 `zstd.exe` | 同上 + 实测 `tar -tf` / `tar -xf` 均成功 |
| Python 3.14 内置 `compression.zstd`（PEP 784），可无依赖产出 .zst | `from compression import zstd`，流式压缩实测 51.73MB → 11.22MB |
| 现状 xz 档位是 `-T0 -3`（快档，非体积优先） | `build.ps1` [2c] |

### 6.2 实测数据（本机，8,000 文件 / 51.73 MB 子集）

| 格式 | 体积 MB | 压缩比 | 编码 s（单线程） | 端到端解压落盘 s（3 轮中位） | ms/文件 |
| --- | --- | --- | --- | --- | --- |
| `xz -3`（现状） | 11.84 | 4.37x | 89.8 | **16.25** | 2.031 |
| `zstd -3` | 13.64 | 3.79x | 0.6 | 12.54 | 1.568 |
| **`zstd -19`** | **11.21** | **4.61x** | 38.6 | **11.60** | **1.451** |

纯解码（`tar -xO > NUL`，剔除文件创建开销，3 轮中位）：

| 格式 | 解码 s | 吞吐 MB/s |
| --- | --- | --- |
| `xz -3` | 0.90 | 57.8 |
| `zstd -19` | 0.30 | **171.7** |

> ⚠️ xz 端到端有 1 次冷启动离群值（72.94s），中位数已排除；这台诊断机磁盘极慢
> （打包 51.73MB 的 `base.tar` 花了 283.5s），绝对值不可外推，**只做同机横向比较**。

### 6.3 三条决策门的结果

| 门 | 阈值 | 实测 | 结论 |
| --- | --- | --- | --- |
| setup 体积 | 增幅 < 3% | **−5.3%**（11.21 vs 11.84） | ✅ 通过（反而更小） |
| 解压耗时 | 有下降 | **−28.6%**（11.60 vs 16.25s） | ✅ 通过 |
| 目标机 `.zst` 支持 | bsdtar ≥ 3.3 | 本机 bsdtar 3.8.9 通过；Win10 1803+ 自带版本满足 | ✅ 通过（installer 侧仍留 zstd.exe 管道回退） |

### 6.4 重要修正：解码不是"小头"

v1 文档按「xz 解码 6–10s」估算，**由实测证伪**：
57.8 MB/s 意味着 1.28 GB 归档仅解码就要 **~22 s**，占用户实测 45.11 s 的约一半。
修正后的模型（用户机 45.11 s / 66,912 文件）：

| 组成 | 估算 | 依据 |
| --- | --- | --- |
| xz 解码 | **~22 s** | 57.8 MB/s × 1.28 GB |
| 66,912 次文件创建 + AV 串行扫描 | **~23 s** | 45.11 − 22；折合 0.34 ms/文件，与 0.39 ms/文件基线一致 |

因此换 zstd 的收益不是"几秒"，而是**解码段 ~22s → ~7.5s**（按 171.7 MB/s 折算），
叠加落盘段不变，`extract` 相位预计下降三分之一左右（**待真机 3 轮验证**）。

### 6.5 实现方式（默认关闭，发布链路不变）

| 侧 | 改动 |
| --- | --- |
| `build.ps1` | 新增 `-ZstdArchive` 开关；`[2c]` 按开关选 `runtime.tar.zst`；zstd.exe 优先、Python 3.14 `compression.zstd` 流式回退；产物用 `tar -tf` 自检，读不了就 `throw` 拒发；`/DRuntimeArchive=<文件名>` 传给 ISCC |
| `installer.iss` | `#ifndef RuntimeArchive` 默认 `"runtime.tar.xz"`；`[Files]` 用 `Source: "..\build\{#RuntimeArchive}"`；`[Code]` 用 `ExpandConstant('{app}\{#RuntimeArchive}')`；解压失败时若存在 `tools\zstd.exe` 则退到管道解压 |
| `.gitignore` | 新增 `build/runtime.tar.zst`（318MB 级派生资产，不入库） |

已本地验证：`build.ps1` PS 解析通过；`installer.iss [Code]` 结构 delta 通过
（begin/end +2、if/then +4、else +2，无 `;else`）；嵌入的 Python 流式压缩片段
实跑成功（`tar -tf` 8606 项、`tar -xf` 8000 文件、rc=0）。

**启用方式（确认后再切默认）**：

```powershell
# 1) 装 zstd（或确保有 Python 3.14+）
winget install --id Facebook.Zstandard   # 或 choco install zstd -y
# 2) 出包
powershell -ExecutionPolicy Bypass -File build\build.ps1 -SkipWebBuild -FastInstaller -ZstdArchive
# 3) 与 xz 包对比 3 轮中位数后再决定是否把默认改成 zstd
```

> 尚未做：真机 3 轮安装中位数、`dist` 产物体积复核、以及"用户机 bsdtar 是否带 zstd"
> 的抽样统计。这三项未完成前**不要把 -ZstdArchive 写进 CI**。



---

## 7. 明确否决的项（附重启条件）

| 项 | 否决理由 | 若未来重启，前置条件 |
| --- | --- | --- |
| **Inno 逐文件展开 runtime** | 6.21 ms/file × 66,912 ≈ 7 分钟，比现状慢一个数量级 | 永不 |
| **增量升级（只写变更文件）** | `(size, mtime)` 比对在增量构建下会漏判（mtime 保留但内容已变）→ 用户拿到新旧混合 runtime；且破坏现有「整目录替换 → 清孤儿」的原子性 | 构建期 `tar --mtime` 归一化 **+** 关键产物（`*.pyc`/`*.js`/`*.json`）哈希清单 **+** purge 清单 **+** 失败自动回退全量 |
| **Defender 排除项** | 需 `PrivilegesRequiredOverridesAllowed=dialog` 引入 UAC；给用户可写目录留永久 AV 盲区；企业 GPO 常禁改；收益只覆盖串行化那一小段 | 企业私有部署（MDM 统一下发例外），或独立「开发者模式」包 |
| **只发 `.pyc`（删 `.py`）** | traceback 丢失 + `pkgutil.iter_modules` 动态插件发现失效 | 永不 |

---

## 8. 度量协议（每批同法）

```powershell
# 每批 3 轮、冷缓存、取中位数；日志锚点已内建，无需插桩
1..3 | ForEach-Object {
  powershell -ExecutionPolicy Bypass -File tools\diag\measure-inno-install.ps1 `
    -SetupPath "<该批次产物>" -CleanTarget `
    -OutJson "$env:TEMP\tbdiag\b-$_.json" -LogPath "$env:TEMP\tbdiag\b-$_.install.log"
}
```

| 关注字段 | 含义 |
| --- | --- |
| `TB-DEPLOY timing extract` | tar 解压落盘（主耗时项） |
| `TB-DEPLOY timing swap` | 删旧 + 改名 |
| `TB-DEPLOY timing warmup` | 批次 1 后应为 ≈0（派发） |
| `TB-DEPLOY note warmup async=1` | 批次 1 生效标记 |
| `TB-DEPLOY timing total` | 部署块总耗时 |

> 对照实验只能通过「管理员 + `Add-MpPreference -ExclusionPath`」做，
> **不要**尝试 `-DisableRealtimeMonitoring`（必被防篡改拦截，实测已失败两次）。

---

## 9. 变更文件清单

| 文件 | 类型 | 批次 |
| --- | --- | --- |
| `desktop-shell/tools/audit_staging.py` | 新增（只读审计） | 0 |
| `.github/workflows/build-desktop.yml` | CI 增 4 步：审计、上传、prune dry-run、预算门禁 | 0 / 2 |
| `desktop-shell/build/installer.iss` | 预热异步化（+37/−11 行） | 1 |
| `tools/diag/measure-inno-install.ps1` | 预热阶段标签改为「异步派发」 | 1 |
| `desktop-shell/tools/iss_codecheck.py` | 新增（ISS `[Code]` 结构 delta 校验） | 1 |
| `desktop-shell/tools/prune_staging.py` | 新增（dry-run 默认，未批准拒绝 apply） | 2 |
| `desktop-shell/build/prune-policy.json` | 新增（候选规则，未批准） | 2 |
| `desktop-shell/build/runtime-budget.json` | 新增（门禁配置，enforce=false） | 2 |
| `desktop-shell/build/build.ps1` | 新增 `-ZstdArchive`；`[2c]` 按格式分支；`/DRuntimeArchive` 注入 | 3 |
| `desktop-shell/build/installer.iss` | `{#RuntimeArchive}` 定义 + `[Files]`/`[Code]` 引用 + 解压失败回退 | 3 |
| `desktop-shell/.gitignore` | 新增 `build/runtime.tar.zst` | 3 |
| `tools/diag/compare-runtime-archive-formats.ps1` | 新增（批次 3 决策数据） | 3（诊断） |
| `tools/diag/compare-archive-formats-resume.ps1` | 新增（同上，续跑段，便于调参） | 3（诊断） |

未改动：`build_runtime.py`、PyInstaller spec（`ThinkBuddyDesktop.spec` / `-mac.spec`）、
`installer.iss` 的 `[Setup]` 段（Compression/Privileges 全部原样）、`pyproject.toml`、
CI 的构建命令（默认仍是不带 `-ZstdArchive` 的 xz 路径）、任何业务源码。

---

## 10. 下一步（按序）

1. 跑一次 CI（或本地 `build.ps1`），取 `staging-audit` artifact → 按 §3.3 读分布；
2. 把实测的 `totals.files` / `archive MB` 填进 `runtime-budget.json`；
3. 按审计结果把 §5.3 的候选规则改写成「指向真实路径的精确规则」，
   逐条过 §5.4 自检后再批准（`state` → `approved`）；
4. 批次 3 等 §6.3 实测 + `dist` 体积复核再定；
5. 每批完成后按 §8 跑 3 轮中位数，把数字回填到本文对应章节。

