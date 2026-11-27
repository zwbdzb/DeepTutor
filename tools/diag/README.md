# tools/diag — Windows 安装瓶颈诊断脚本集

> 用途：只采集数据，**不改动** build.ps1 / build_runtime.py / PyInstaller spec /
> installer.iss / pyproject.toml / CI yaml / 业务源码。
> 报告正文见 [`docs/PACKAGING_DIAGNOSIS.md`](../../docs/PACKAGING_DIAGNOSIS.md)。

## 脚本清单

| 脚本 | 对应模块 | 需要什么 | 是否只读 |
| --- | --- | --- | --- |
| `measure-runtime-payload.ps1` | 模块2 | 构建产物（staging / dist / runtime.tar.xz） | 只读 |
| `measure-inno-install.ps1` | 模块3 | ThinkBuddySetup-*.exe + 干净测试机 | 会执行安装 |
| `measure-tarxz-extract.ps1` | 模块4 | runtime.tar.xz | 只在 %TEMP% 解压 |
| `verify-runtime-payload-duplication.ps1` | 假设1 | onefile exe（可选 setup / tar.xz） | 只读（字节扫描） |
| `measure-defender-impact.ps1` | 模块5 | **管理员** + 安装包 | 会临时增删 Defender 排除项 |
| `probe-firstlaunch-extract.ps1` | 模块6 | 已安装的 ThinkBuddyDesktop.exe | 启动应用 |
| `compare-runtime-archive-formats.ps1` | 优化批次3 | 一棵代表性目录 + Python 3.14 | 只在 %TEMP% 建子集 |
| `compare-archive-formats-resume.ps1` | 优化批次3 | 上一脚本留下的 `base.tar` | 同上（续跑，免重建子集） |

## 优化阶段的脚本（不属于诊断 6 模块）

| 脚本 | 位置 | 作用 |
| --- | --- | --- |
| `audit_staging.py` | `desktop-shell/tools/` | staging 构成审计（只读）：扩展名 / 子树 / 叶子目录 / `.py`-`.pyc` 配对 / 裁剪信号 |
| `prune_staging.py` | `desktop-shell/tools/` | 按 `build/prune-policy.json` 裁剪；**默认 dry-run**，规则未批准时拒绝 `--apply` |
| `iss_codecheck.py` | `desktop-shell/tools/` | Inno `[Code]` 段结构 delta 校验（无 ISCC 环境时用） |

> 决策与落地说明见
> [`desktop-shell/docs/packaging-optimization.md`](../../desktop-shell/docs/packaging-optimization.md)。


## 推荐执行顺序

```powershell
# 0) 在构建机（有 cp312 + Inno Setup 6/7）先出包
powershell -ExecutionPolicy Bypass -File build\build.ps1 -SkipWebBuild -FastInstaller

# 1) 静态量化（构建机即可，几分钟）
powershell -ExecutionPolicy Bypass -File tools\diag\measure-runtime-payload.ps1 -OutJson "$env:TEMP\tbdiag\payload.json"
powershell -ExecutionPolicy Bypass -File tools\diag\verify-runtime-payload-duplication.ps1 `
    -SetupPath "desktop-shell\dist\ThinkBuddySetup-<ver>.exe" -OutJson "$env:TEMP\tbdiag\dup.json"
powershell -ExecutionPolicy Bypass -File tools\diag\measure-tarxz-extract.ps1 -OutJson "$env:TEMP\tbdiag\tarxz.json"

# 2) 安装分阶段计时（干净测试机，建议 3 轮取中位数）
1..3 | ForEach-Object {
    powershell -ExecutionPolicy Bypass -File tools\diag\measure-inno-install.ps1 `
        -SetupPath "<安装包>" -CleanTarget -OutJson "$env:TEMP\tbdiag\install-$_.json"
}

# 3) Defender 对照（管理员）
powershell -ExecutionPolicy Bypass -File tools\diag\measure-defender-impact.ps1 -SetupPath "<安装包>" -Rounds 3

# 4) 首启自解压验证（装完之后，注意与安装耗时分开记）
powershell -ExecutionPolicy Bypass -File tools\diag\probe-firstlaunch-extract.ps1 `
    -ExePath "$env:LOCALAPPDATA\Programs\ThinkBuddy\ThinkBuddyDesktop.exe" -ClearManagedRuntime
```

## 约定

- 所有脚本只用 `Get-ChildItem` / `Measure-Object` / `Group-Object` 聚合，**不打印全量文件列表**。
- 所有脚本支持 `-OutJson`，便于把多个轮次喂给同一张对照表。
- 缺数据时脚本会显式打印 `[缺失]` / `【需人工采集】` 并以非零码退出，不做静默推测。
- PS 5.1 兼容（CI 的 windows-latest 默认 shell 版本）；文件以 UTF-8 BOM 保存，
  与仓库既有 `build.ps1` 一致。
