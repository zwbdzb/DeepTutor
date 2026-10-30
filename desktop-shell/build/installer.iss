; ThinkBuddy — click-to-install package (Inno Setup 7)
; Compile:  "C:\Program Files\Inno Setup 7\ISCC.exe" installer.iss
; Produces: dist\ThinkBuddySetup.exe  (installer, uninstaller, shortcuts)
;
; The setup ships:
;   ThinkBuddyDesktop.exe — the native shell (windowed, no console)
;   runtime.tar.xz         — offline deeptutor runtime as a single archive
;                            (embeddable python + deeptutor + portable node),
;                            unpacked by CurStepChanged(ssPostInstall) into
;                            {app}\runtime. No python/node/pip needed on the
;                            target machine; the shell resolves {app}\runtime
;                            automatically. Single-archive delivery exists
;                            because Inno's per-file write path costs ~6.2ms
;                            per file regardless of size (337s for 40k files).
;                            bsdtar unpacks the same tree in 30s.
;   assets\icon.ico        — shortcuts + uninstaller icon
;
; End users double-click ThinkBuddySetup.exe -> Next/Next/Install -> the app
; launches on its own. Uninstall keeps ~\ThinkBuddy workspace (learning data).

#define MyAppName "ThinkBuddy"
; AppVersion is injected by build.ps1 via /DMyAppVersion=<engine_version.timestamp>,
; (e.g. 1.6.14.10121554) so the installer metadata tracks deeptutor/__version__.py + build time.
; The fallback below only applies to a bare manual `ISCC installer.iss` run
; and is intentionally wrong-looking so a stale build is easy to spot.
#ifndef MyAppVersion
#define MyAppVersion "0.0.0.SET-BY-build.ps1"
#endif
#define MyAppPublisher "HKUDS"
#define MyAppExeName "ThinkBuddyDesktop.exe"

[Setup]
; AppId for the ThinkBuddy brand; internal-test install base ~0, no upgrade
; path needed.
AppId={{3B4FC0AE-E3E3-491F-B3B6-59BBBF1D6305}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\ThinkBuddy
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
; 输出文件名：build.ps1 传 /DMyOutputName=dt<版本>_<时间戳>，直接产出带版本名的
; 发布文件（如 ThinkBuddySetup-dt1.6.14_10131700.exe），不再复制中间产物。
; 裸跑 ISCC（无 define）时回退固定名 ThinkBuddySetup.exe。
#ifdef MyOutputName
OutputBaseFilename=ThinkBuddySetup-{#MyOutputName}
#else
OutputBaseFilename=ThinkBuddySetup
#endif
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
; Compression: 默认 lzma2（发布档，体积最小但慢）。
; /DFastCompression 切到 lzma2/fast——开发迭代和 CI 用，压缩时间大幅缩短，
; 体积代价 +10~15%。build.ps1 的 -FastInstaller 会传这个 define。
#ifdef FastCompression
Compression=lzma2/fast
#else
Compression=lzma2
#endif
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; CloseApplications 用 force 而非 yes：
; Restart Manager 能关掉有消息循环的壳（ThinkBuddyDesktop.exe），却关不掉无
; 消息循环的后端子进程（安装目录 runtime\python\python.exe 的 launcher/
; uvicorn、runtime\node\node.exe 的 next standalone）——它们继续占用安装目录
; 文件，yes 模式下 Inno 会弹「无法自动关闭所有应用程序（重试/忽略/取消）」
; 框卡住交互安装（用户截图实证）。force 兜底强杀所有占用安装目录下文件的
; 进程；按「文件占用安装目录」精确匹配，不会误杀系统里其他无关的
; python.exe / node.exe。
; 静默更新链路（desktop/updater.py run_installer）一直用命令行开关
; /FORCECLOSEAPPLICATIONS（语义等价 force）覆盖本指令，此处统一为同一语义，
; 两条链路行为一致。
CloseApplications=force
; 被 RM/force 关闭的应用不自动重启：装后拉起统一归 updater.bat（静默更新，
; 延迟约 15s 等杀软扫描完新 DLL 再拉起，防 PyInstaller bootloader 报 126）
; 与 Finish 页复选框（交互安装）负责，避免双拉起。
RestartApplications=no

[Languages]
; 简体中文放第一位：[Languages] 的第一项是 Inno 的 fallback 默认语言。
; 中文 Windows 若因 LanguageID 匹配失败走到 fallback，拿到的也是中文向导，
; 而不是英文。英文系统用户仍按系统语言自动匹配到 english，不受影响。
; Inno's standard installation omits Simplified Chinese; use the vendored file.
Name: "chinesesimp"; MessagesFile: "languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
; 附加任务页「开机自启动」勾选（中英双语；未定义的语言回退到第一项中文）。
chinesesimp.StartAtBoot=开机时自动启动 ThinkBuddy
english.StartAtBoot=Start ThinkBuddy at boot

[Tasks]
; 「开机自启动」默认勾选（不加 unchecked），排在桌面快捷方式下方；
; desktopicon 保持原有默认（不勾选），升级时按上次选择复用。
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart";   Description: "{cm:StartAtBoot}";       GroupDescription: "{cm:AdditionalIcons}"

[Files]
; native shell + assets
Source: "..\dist\ThinkBuddyDesktop.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\icon.ico";            DestDir: "{app}\assets"; Flags: ignoreversion
Source: "..\assets\icon.png";            DestDir: "{app}\assets"; Flags: ignoreversion
; offline runtime —— 单归档投递（L1 安装提速，2026-10-27 实测驱动）
; 历史做法是把 staging 整棵树（4 万个文件）逐文件交给 Inno 写入，实测
; 40620 个文件耗时 337s，摊到每个文件 6.21ms，且与文件大小无关——瓶颈是
; 安装器的逐文件解压调度 + 卸载记录写入，不是磁盘也不是杀软（同机直写
; 2000 个同尺寸文件仅 0.75s = 0.38ms/文件）。同一批文件由系统 bsdtar 解压
; 实测只要 30.06s（快 11 倍）。
; 因此 [Files] 只投递一个 runtime.tar.xz（由 build.ps1 的 [2c] 生成），
; 解包与就位由 CurStepChanged(ssPostInstall) 调 {sys}\tar.exe 完成。
; nocompression：归档本身已是 xz 压缩体，让 Inno 再套一层只会白耗一次
; 压缩/解压；标记后 setup 内以原样存储，安装时是纯拷贝（实测 318MB/0.3s）。
; ignoreversion：归档没有可比较的版本资源。
Source: "..\build\runtime.tar.xz"; DestDir: "{app}"; Flags: nocompression ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\icon.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\icon.ico"; Tasks: desktopicon

[Run]
; 双击安装：Finish 页显示"启动 ThinkBuddy"复选框（skipifsilent——静默更新不在此拉起，
; updater.bat 负责：装完延迟 ~15s 等杀软扫描完新 DLL 再拉起，避免 PyInstaller
; bootloader 在 main 前 LoadLibrary 报 126，见 desktop/updater.py run_installer）
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Registry]
; 开机自启动：写 HKCU Run 键（PrivilegesRequired=lowest 的 per-user 安装，无需管理员）。
; 与桌面端「文件 → 开机自启动」读写同一键值，两处天然同步；卸载 uninsdeletevalue 清掉。
; 静默升级时 updater.py 传 /MERGETASKS="!autostart"，避免复用旧勾选把用户关掉的自启写回。
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "ThinkBuddy"; \
  ValueData: """{app}\{#MyAppExeName}"""; \
  Flags: uninsdeletevalue; Tasks: autostart

[InstallDelete]
; 前端运行时缓存（<workspace>\data\user\runtime\web）：应用窗口**内部**的图标
; （侧边栏 logo、网页标签 favicon）都从这里加载，不从 exe 读。它是纯派生数据
; —— launcher 判定缓存失效时本就会自行 rmtree 重建，删掉零用户数据损失（学习
; 数据在 <workspace> 的其他子目录）。删了才能保证更新后窗口里必然是新图标，
; 详见 [Code] 段 PurgeWebRuntimeCache 的注释（那里还覆盖 DEEPTUTOR_DESKTOP_HOME
; 改过 workspace 位置的情况）。
Type: filesandordirs; Name: "{%USERPROFILE}\ThinkBuddy\data\user\runtime\web"
; 安装新版时清掉本机遗留的托管运行时缓存（%LOCALAPPDATA%\ThinkBuddy\runtime）。
; 它是旧版 runtime.zip 自解压的历史产物，版本可能停在任意旧版（曾以 1.6.9
; 遮蔽新装的 1.6.10，见 docs/adr/ADR-005）。本安装器自带完整 runtime/ 树，
; 缓存纯属冗余，删除无任何用户数据损失（学习数据在 %USERPROFILE%\ThinkBuddy）。
Type: filesandordirs; Name: "{localappdata}\ThinkBuddy\runtime"

[UninstallDelete]
; 卸载时清掉托管缓存 + 应用目录；keep user data (workspace lives in
; %USERPROFILE%\ThinkBuddy) — only the app dir (incl. the installed runtime
; copy) and the version-locked runtime cache are removed.
Type: filesandordirs; Name: "{localappdata}\ThinkBuddy\runtime"
Type: filesandordirs; Name: "{app}"

[Code]
var
  // ssPostInstall 里 runtime 是否部署失败。失败时由 GetCustomSetupExitCode
  // 把 setup 退出码改成 8——否则 setup 恒返回 0，静默更新链路的进度窗会
  // 显示「更新完成」而实际 runtime 根本没解出来（壳随后起不来）。
  RuntimeDeployFailed: Boolean;

  // 静默更新链路的 update.log 路径（updater.bat 用 /TBLOG="<update.log>"
  // 传进来）。部署阶段往这里追加 deploy 行，进度小窗轮询该文件映射成阶段
  // 文案——否则那 40+ 秒里小窗只会一直显示「正在完成收尾…」。
  TBDeployLog: string;

(* ===== 交互安装前的旧实例抢先清理（InitializeSetup） =====================
   背景：双击 setup.exe 交互安装新版时若旧版还在运行，Inno 的 Restart Manager
   能关掉有消息循环的壳（ThinkBuddyDesktop.exe），却关不掉无消息循环的后端
   子进程（安装目录 runtime\python\python.exe 的 launcher/uvicorn、
   runtime\node\node.exe 的 next standalone）——它们继续占用安装目录文件，
   Inno 会弹「无法自动关闭所有应用程序（重试/忽略/取消）」框卡住安装。
   壳还有 supervise() 自愈（后端死了有界重启）：若 RM 先杀后端后杀壳，壳会把
   后端再拉起来继续占文件——所以必须先杀壳、再清子进程，顺序不能反。

   修复：复刻静默更新链路 desktop/updater.py run_installer（updater.bat）的
   已验证顺序，在 InitializeSetup 抢先清理：
     1) taskkill 先杀壳，终结 supervise 自愈源；
     2) PowerShell 按「可执行文件路径位于旧安装目录下」精确清理 runtime
        子进程——严禁 taskkill /IM python.exe / node.exe（会误杀系统里其他
        无关进程），必须按 ExecutablePath 匹配；
     3) 睡约 1.5s 等文件句柄释放。

   与 updater.bat 的对应关系（顺序一致性）：bat 先
   taskkill /F /FI "IMAGENAME eq ThinkBuddyDesktop.exe"，再跑
   /VERYSILENT /FORCECLOSEAPPLICATIONS 安装——本函数在交互路径做同样的
   先壳后子进程清理；静默链路跑到这里时壳已被 bat 杀掉，第 1 步是幂等
   空操作，第 2 步与 force 语义一致（目标本就要被关），零冲突零破坏。

   分层设计：本函数只清「安装启动时刻已存在的旧实例」；向导空窗期
   （InitializeSetup 返回后到复制文件之间）用户再启动的实例，由 [Setup]
   段 CloseApplications=force 在复制文件阶段兜底强杀。

   幂等与容错：旧版未装/未运行时三步全部静默通过（taskkill 报无匹配、
   注册表读不到走回退、目录不存在跳过 PowerShell）；任何一步失败都不阻断
   安装——InitializeSetup 恒返回 True，复制文件阶段还有 force 兜底。
   ====================================================================== *)

// kernel32.Sleep 导入：精确毫秒级等待，无窗口、无子进程（比 ping -n 干净）。
// Inno 的 DLL 导入语法；setuponly 表示仅安装器进程加载。
procedure SleepMs(Milliseconds: Longint);
  external 'Sleep@kernel32.dll stdcall setuponly';

(* ===== 更新后强制刷新图标（RefreshShellIcons，ssDone 触发） ================
   背景（换标后用户实测反馈）：升级安装完成，桌面/开始菜单快捷方式仍然显示
   **旧图案**。根因是 Windows Shell 的图标缓存以「图标文件路径 + 图标索引」
   为键 —— 升级时 {app}\assets\icon.ico 的内容确实被新版覆盖了，但**路径没
   变**，缓存键也就不变，Explorer 不会重新去读文件，于是继续画旧图。托盘
   图标另有一份独立缓存（TrayNotify 的 IconStreams，按 exe 路径缓存），同
   路径换 exe 后同样残留旧图。

   注意：光重建 .lnk **无效** —— 新快捷方式指向的还是同一个 ico 路径，照样
   命中缓存。必须显式让缓存失效。

   修复（ssDone 触发，交互安装与静默更新两条链路都覆盖）：
     1) SHChangeNotify(SHCNE_ASSOCCHANGED | SHCNF_FLUSH)：通知 Shell「文件
        关联/图标已变更」，Explorer 据此重建图标缓存；SHCNF_FLUSH 让通知
        同步生效，不排到空闲时再处理。
     2) ie4uinit.exe -show：微软官方的图标缓存刷新入口，补上 ASSOCCHANGED
        在部分系统上刷不到桌面层的缺口（等价于让用户手动跑的那条命令）。
     3) 删除 HKCU TrayNotify 的 IconStreams / PastIconsStream：托盘图标流
        缓存。per-user 安装（PrivilegesRequired=lowest），HKCU 无需管理员。

   best-effort：三步全部静默容错 —— 图标陈旧只影响观感，绝不阻断安装。
   静默更新（/VERYSILENT）同样生效：本过程不依赖 WizardForm。
   ====================================================================== *)

const
  SHCNE_ASSOCCHANGED = $08000000;   // 文件关联/图标已变更
  SHCNF_IDLIST       = $0000;       // dwItem1/dwItem2 为 NULL 的全局通知
  SHCNF_FLUSH        = $1000;       // 同步刷新，不排到空闲时再处理
  TB_TRAY_KEY        = 'Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\TrayNotify';

// shell32.SHChangeNotify：通知 Shell 某项已变更（这里用于作废旧图标缓存）。
// setuponly：只在安装器进程加载，卸载器用不到。
procedure TbSHChangeNotify(wEventId: Longint; uFlags: Longint;
  dwItem1: Longint; dwItem2: Longint);
  external 'SHChangeNotify@shell32.dll stdcall setuponly';

(* 升级时强制重建桌面快捷方式：桌面上那个 .lnk 记录的图标来自旧 icon.ico，
   其中 Tasks 段的 desktopicon 若未选中，Icons 段就不会重写它，用户看到的
   仍是旧图案。这里检测到本机已装过旧版（卸载键存在 = 本次是升级而非首装）时
   强制勾上该任务，保证 .lnk 被重写一遍；再配合 ssDone 的 RefreshShellIcons
   清缓存，桌面图标必然换成新图案。

    两个触发条件（任一满足即强制勾选）：
      a) 卸载键存在 —— 本次是升级，.lnk 由本安装器维护，不重写就停在旧版状态；
      b) 桌面上**已存在**同名 .lnk —— 覆盖旧包用别的 AppId 装的、或用户手动
         建的快捷方式。这类 .lnk 本安装器从未记录过，不主动重写就会一直显示
         旧图案，正是「旧包还在电脑上、桌面图标不换」的那一类场景。

   两者都不满足（全新安装且桌面干净）时保持默认不勾选，不擅自给新用户塞桌面
   图标。静默更新没有向导页（InitializeWizard 不触发），由 updater.py 的
   /MERGETASKS="desktopicon,!autostart" 覆盖同一场景。 *)
procedure InitializeWizard();
begin
  try
    // 子键写法与 InitializeSetup 一致：Inno 对 HKCU 会自动前置
    // Software\Microsoft\Windows\CurrentVersion\Uninstall\。
    if RegKeyExists(HKEY_CURRENT_USER,
         '{3B4FC0AE-E3E3-491F-B3B6-59BBBF1D6305}_is1')
       or FileExists(ExpandConstant('{autodesktop}\{#MyAppName}.lnk'))
       or FileExists(ExpandConstant('{userdesktop}\{#MyAppName}.lnk')) then
      WizardSelectTasks('desktopicon');
  except
    // 纯 UI 状态调整，失败只影响图标刷新，不阻断安装。
  end;
end;

procedure RefreshShellIcons;
var
  ErrorCode: Integer;   // Exec 返回码占位：只看成败，不判具体值
  Ie4u: string;         // {sys}\ie4uinit.exe
begin
  // ---- 1) 通知 Shell 图标缓存失效（同步）----
  try
    TbSHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST or SHCNF_FLUSH, 0, 0);
    Log('TB-ICON notify SHCNE_ASSOCCHANGED sent');
  except
    Log('TB-ICON notify failed (ignored)');
  end;

  // ---- 2) 官方刷新入口，覆盖桌面层 ----
  try
    Ie4u := ExpandConstant('{sys}\ie4uinit.exe');
    if FileExists(Ie4u) then
    begin
      if Exec(Ie4u, '-show', '', SW_HIDE, ewWaitUntilTerminated, ErrorCode) then
        Log('TB-ICON ie4uinit -show exit ' + IntToStr(ErrorCode))
      else
        Log('TB-ICON ie4uinit exec failed');
    end
    else
      Log('TB-ICON ie4uinit not found: ' + Ie4u);
  except
    Log('TB-ICON ie4uinit failed (ignored)');
  end;

  // ---- 3) 托盘图标流缓存（旧托盘图标残留）----
  try
    RegDeleteValue(HKEY_CURRENT_USER, TB_TRAY_KEY, 'IconStreams');
    RegDeleteValue(HKEY_CURRENT_USER, TB_TRAY_KEY, 'PastIconsStream');
    Log('TB-ICON tray icon cache cleared');
  except
    Log('TB-ICON tray cache clear failed (ignored)');
  end;
end;

(* ===== 清前端运行时缓存（PurgeWebRuntimeCache，ssDone 里先于刷新调用） =====
   为什么需要：应用窗口**内部**的图标（侧边栏 logo、网页标签 favicon）不是从
   exe 读的，而是从前端运行时缓存读 —— launcher.py 把 site-packages 里的
   deeptutor_web 物化到 <workspace>\data\user\runtime\web（NTFS 硬链接，秒级），
   是否重建由 .deeptutor-web-runtime.json 里 source_mtime_ns（打包源 server.js
   的 mtime）的**全等比较**决定。

   单靠 mtime 比对有两个缺口，任一命中都会让更新后窗口里继续显示旧图标：
     1) 物化用的是硬链接。安装时 {app}\runtime 被 DelTree 重建，但缓存目录里
        的硬链接仍指向旧 inode，旧内容原地保留；
     2) 若新包 server.js 的 mtime 与缓存记录一致（tar 保留 mtime、或构建环境
        归一化 mtime），直接判定「缓存仍有效」而不重建。

   修复：安装收尾直接删掉该目录。它是纯派生数据（launcher 每次判定失效都会
   自行 rmtree 重建），删除无用户数据损失 —— 学习数据在 <workspace> 的其他子
   目录，不受影响；代价只是下次启动多一次物化（硬链接，秒级）。

   路径：默认 %USERPROFILE%\ThinkBuddy；被 DEEPTUTOR_DESKTOP_HOME 覆盖时按该
   环境变量解析（与 desktop/runtime.py 的 WORKSPACE_HOME 同一约定）。
   InstallDelete 段里另有一条默认路径的静态规则作为双保险。
   ====================================================================== *)
procedure PurgeWebRuntimeCache;
var
  Home: string;    // workspace 根目录
  Cache: string;   // <workspace>\data\user\runtime\web
begin
  try
    Home := GetEnv('DEEPTUTOR_DESKTOP_HOME');
    if Home = '' then
      Home := ExpandConstant('{%USERPROFILE}\ThinkBuddy');
    if Home = '' then
      Exit;
    // 去尾反斜杠，避免拼出双反斜杠。
    while (Length(Home) > 0) and (Home[Length(Home)] = '\') do
      SetLength(Home, Length(Home) - 1);
    Cache := Home + '\data\user\runtime\web';
    if DirExists(Cache) then
    begin
      DelTree(Cache, True, True, True);
      Log('TB-ICON web runtime cache purged: ' + Cache);
    end
    else
      Log('TB-ICON web runtime cache absent: ' + Cache);
  except
    Log('TB-ICON web runtime cache purge failed (ignored)');
  end;
end;

function InitializeSetup(): Boolean;
var
  ErrorCode: Integer;  // Exec 返回码占位：全程只看成败，不判具体值
  AppDir: string;      // 旧版安装目录（HKCU InstallLocation，回退默认目录）
  Pattern: string;     // PowerShell -like 匹配模式：安装目录\*
  PsPath: string;      // powershell.exe 全路径
  PsParams: string;    // powershell.exe 参数串
begin
  Result := True;
  try
    // ---- 第 1 步：先杀壳，终结 supervise() 自愈源（顺序不能反） ----
    // 无实例在跑时 taskkill 报「没有找到进程」(128)，属正常，忽略返回码。
    Exec(ExpandConstant('{sys}\taskkill.exe'),
      '/F /FI "IMAGENAME eq ThinkBuddyDesktop.exe"',
      '', SW_HIDE, ewWaitUntilTerminated, ErrorCode);

    // ---- 第 2 步：按安装目录路径精确清理 runtime 子进程 ----
    // per-user 安装（PrivilegesRequired=lowest）的卸载项在 HKCU；键名里的
    // GUID 即 AppId 字面值（iss 值字段的双花括号转义不适用于 Pascal 字符
    // 串，这里直接写单层花括号即可）。读不到则回退默认安装目录。
    AppDir := '';
    if not RegQueryStringValue(HKEY_CURRENT_USER,
         '{3B4FC0AE-E3E3-491F-B3B6-59BBBF1D6305}_is1',
         'InstallLocation', AppDir) then
      AppDir := ExpandConstant('{autopf}\ThinkBuddy');
    // 规范化：注册表 InstallLocation 常带尾反斜杠，去掉，避免拼出双反斜杠。
    if (Length(AppDir) > 0) and (AppDir[Length(AppDir)] = '\') then
      SetLength(AppDir, Length(AppDir) - 1);
    // 目录不存在（旧版从未安装/已被删）直接跳过，省一次 PowerShell 冷启动。
    if (AppDir <> '') and DirExists(AppDir) then
    begin
      Pattern := AppDir + '\*';
      // 罕见边界：路径含单引号（如用户名 O'Brien）时按 PowerShell 单引号
      // 字符串规则翻倍，防止命令被截断。
      StringChange(Pattern, '''', '''''');
      PsPath := ExpandConstant(
        '{sd}\Windows\System32\WindowsPowerShell\v1.0\powershell.exe');
      // 引号层级：Pascal 串里两个连续单引号即一个单引号；-like 模式用
      // PowerShell 单引号串包住（路径含空格也安全）；整条 -Command 再用
      // 双引号包住直接交给 powershell.exe——不经 cmd，无 OEM 代码页乱码
      // 与重定向干扰；SW_HIDE 保证不闪黑框。
      PsParams :=
        '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command ' +
        '"Get-CimInstance Win32_Process | Where-Object { ' +
        '$_.ExecutablePath -like ''' + Pattern + ''' } | ' +
        'ForEach-Object { Stop-Process -Id $_.ProcessId -Force ' +
        '-ErrorAction SilentlyContinue }"';
      if not Exec(PsPath, PsParams, '', SW_HIDE,
           ewWaitUntilTerminated, ErrorCode) then
        // 兜底：全路径不可用时走 PATH 里的 powershell.exe。
        Exec('powershell.exe', PsParams, '', SW_HIDE,
          ewWaitUntilTerminated, ErrorCode);
    end;

    // ---- 第 3 步：等约 1.5s，让被杀进程释放安装目录文件句柄 ----
    SleepMs(1500);
  except
    // 任何一步异常都不阻断安装：复制文件阶段还有 CloseApplications=force 兜底。
    Result := True;
  end;
end;

(* ===== 复制完归档后：解包 runtime + 预热导入（CurStepChanged/ssPostInstall） =
   L1（2026-10-27）：runtime 不再由 Inno 逐文件铺开，而是以单个 runtime.tar.xz
   投递（见 [Files] 段注释），本函数负责解包与就位。三段式，任何一步失败都
   保留旧 runtime 或干净退出，绝不留下半成品目录：

     1) 解压到 {app}\runtime.new —— 不碰现有 runtime，失败可直接丢弃；
     2) 就位判定：出现 runtime.new\python\python.exe 才算解压成功，否则回滚；
     3) 删旧 runtime → 改名 runtime.new → runtime。
        先删旧是必要的：整目录替换顺带清掉旧版遗留的孤儿模块，而 Inno 的
        逐文件覆盖永远做不到这一点（这是历史遗留的一个隐性正确性问题）。

   为什么先解压后删旧（而不是反过来）：解压失败时旧 runtime 完好，新壳配旧
   运行时只是版本滞后，仍然可用；先删就等于在失败时把用户的应用彻底删掉。

   预热（原逻辑保留）：复制/解压完成后、[Run] 拉起新壳之前，用装好的解释器
   静默导入一次完整应用栈。新装运行时的 15000+ 个 pyc 首次被读都要过杀软
   实时扫描（用户机实测：后端导入 29s、首请求惰性初始化 ~13s，首装 ~139s
   才见主界面），这里把扫描成本吸收进安装阶段。这也是预热的**唯一路径**：
   新装与静默更新（/VERYSILENT 覆盖安装）两条路都会走到 ssPostInstall；
   updater.bat 里曾有的同款预热段已删除（跑在热缓存上是纯重复），bat 仅保留
   ping 15s 作杀软扫描缓冲后再拉起新壳。

   best-effort：Exec 失败/非零退出码一律忽略——预热只是优化，任何失败都不
   阻断安装、不弹框惊扰用户；残余成本由壳侧 180s 就绪超时 + 自动重试兜底
   （desktop/process.py、desktop/main.py）。
   ====================================================================== *)
(* ===== 部署阶段的可见化（DeployStage） ==================================
   为什么需要（2026-10-27 用户实测反馈「卡住了 40 多秒」）：L1 之后 Inno 自己
   的复制阶段只要 3-4 秒就结束（单归档 318MB），真正的大头落在 [Code] 的
   ssPostInstall——删旧 runtime ~5s + tar 解压 ~30s + 预热导入 ~13s ≈ 48s。
   而 Inno 的 [Code] 执行期间既不推进画布也不刷新进度条，向导会**整段停在
   「安装程序正在安装…请稍候」**，看起来就是卡死。

   这里做两件事：
     1) 交互安装：直接改进向导的 StatusLabel 文案（把「还有多久、别关窗口」
        写清楚），并用 WizardForm.Update 强制同步重画——[Code] 阻塞期间没有
        消息泵，只有 UpdateWindow 这种同步重画才画得出来；
     2) 静默更新：往 updater.bat 的 update.log 追加 `deploy <tag>` 行，进度
        小窗每 1s 轮询该文件，据此显示「正在解压运行环境…」等阶段。
   两者共用同一份阶段表，避免两套文案漂移。

   顺带把每个阶段的耗时写进 setup 日志（TB-DEPLOY 前缀 + GetTickCount 差值），
   下次再报「卡住」可以直接从 install.log 读出到底哪一段慢，不用猜。
   ====================================================================== *)

// kernel32.GetTickCount：只在 setup 进程里用（与 SleepMs 同一套导入纪律）。
function TbTickCount: Longint;
  external 'GetTickCount@kernel32.dll stdcall setuponly';

procedure DeployStage(const Tag, Msg: string);
begin
  // 交互安装：刷向导状态行。silent 模式下 WizardForm 不可靠，整体跳过。
  try
    if not WizardSilent then
    begin
      WizardForm.FilenameLabel.Caption := '';   // 清掉上一阶段残留的文件名
      WizardForm.StatusLabel.Caption := Msg;
      WizardForm.Update;
    end;
  except
    // 纯 UI 动作，任何失败都不值得影响安装。
  end;
  Log('TB-DEPLOY ' + Tag + ' ' + Msg);
  // 静默更新：追加给进度小窗轮询。失败（路径不可写）静默忽略。
  if TBDeployLog <> '' then
    SaveStringToFile(TBDeployLog,
      '[' + GetDateTimeString('yyyy/mm/dd hh:nn:ss', '-', ':') + '] deploy ' +
      Tag + #13#10, True);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;   // Exec 返回码占位：预热/解压失败均静默放行
  Archive: string;       // {app}\runtime.tar.xz
  StagingDir: string;    // {app}\runtime.new —— 解压暂存目录
  TargetDir: string;     // {app}\runtime —— 最终就位目录
  TarExe: string;        // 系统自带的 bsdtar（Win10 1803+）
  T0: Longint;           // 分段计时起点（写进 setup 日志，供事后定位）
  PhaseTicks: Longint;   // 当前阶段的起始 tick
begin
  // 安装全部完成（含 [Icons] 重写 .lnk）后刷新图标缓存 —— 让桌面/开始菜单
  // 快捷方式与托盘立刻显示新图标（详见 RefreshShellIcons 函数头注释）。
  // 放 ssDone 而非 ssPostInstall：确保 .lnk 已重写完毕再清缓存，否则
  // Explorer 可能在刷新后又把旧缓存写回去。
  if CurStep = ssDone then
  begin
    // 先清前端运行时缓存（窗口内部图标的来源），再刷 Shell 图标缓存
    // （快捷方式 / 托盘）。顺序无关，但两件都必须在 .lnk 重写之后做。
    PurgeWebRuntimeCache;
    RefreshShellIcons;
    Exit;
  end;

  if CurStep <> ssPostInstall then
    Exit;

  Archive    := ExpandConstant('{app}\runtime.tar.xz');
  StagingDir := ExpandConstant('{app}\runtime.new');
  TargetDir  := ExpandConstant('{app}\runtime');
  TarExe     := ExpandConstant('{sys}\tar.exe');
  // /TBLOG 未传（交互安装、或旧版 updater.bat）时为空串，DeployStage 自动跳过。
  TBDeployLog := ExpandConstant('{param:TBLOG|}');
  T0 := TbTickCount;

  // 关键阶段禁止取消：此刻正删旧 runtime / 解压新 runtime，中途取消会留下
  // 没有 runtime 的安装目录（比失败更糟——用户的应用直接消失）。
  try
    if not WizardSilent then
      WizardForm.CancelButton.Enabled := False;
  except
  end;
  DeployStage('enter', '正在部署运行环境（约 45 秒，请勿关闭窗口）…');

  // 归档缺失 = 包结构异常（手工跑 ISCC 且没执行 build.ps1 的 [2c] 归档步骤）。
  // 不打断安装，也不删除既有 runtime：旧版可能还能起来，比删空强。
  // 但必须置失败标记——新壳已落地而 runtime 没更新，属于必须让用户看见的故障。
  if not FileExists(Archive) then
  begin
    RuntimeDeployFailed := True;
    Exit;
  end;

  if not FileExists(TarExe) then
  begin
    RuntimeDeployFailed := True;
    // 静默更新链路上弹框会卡住无人值守流程，只在交互安装时提示。
    if not WizardSilent then
      MsgBox('无法解压运行环境：当前系统缺少 tar.exe（Windows 10 1803 及以上版本自带）。'
             + #13#10 + '请升级 Windows 后重新运行安装程序。', mbError, MB_OK);
    Exit;
  end;

  // 上次失败可能留下半成品，先清干净，保证解压从空目录开始。
  if DirExists(StagingDir) then
    DelTree(StagingDir, True, True, True);
  if not ForceDirectories(StagingDir) then
  begin
    RuntimeDeployFailed := True;
    Exit;
  end;

  DeployStage('extract', '正在解压运行环境（约 30 秒，请勿关闭窗口）…');
  PhaseTicks := TbTickCount;
  Exec(TarExe, '-xf "' + Archive + '" -C "' + StagingDir + '"', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Log('TB-DEPLOY timing extract ' + IntToStr(TbTickCount - PhaseTicks) + 'ms');

  // 就位判定：解压成功必然出现 python\python.exe（约 4 万文件、实测 30s）。
  if not FileExists(StagingDir + '\python\python.exe') then
  begin
    DelTree(StagingDir, True, True, True);   // 回滚，保留旧 runtime
    RuntimeDeployFailed := True;             // 归档保留，便于用户手动重跑安装包
    Exit;
  end;

  DeployStage('swap', '正在启用新运行环境…');
  PhaseTicks := TbTickCount;
  if DirExists(TargetDir) then
    DelTree(TargetDir, True, True, True);

  if not RenameFile(StagingDir, TargetDir) then
  begin
    // 改名失败（目录被占用/杀软句柄未释放）→ 兜底：直接解压进目标目录。
    ForceDirectories(TargetDir);
    Exec(TarExe, '-xf "' + Archive + '" -C "' + TargetDir + '"', '',
         SW_HIDE, ewWaitUntilTerminated, ResultCode);
    DelTree(StagingDir, True, True, True);
  end;

  // 最终校验：无论走改名还是兜底路径，目标目录里必须有解释器。
  if not FileExists(TargetDir + '\python\python.exe') then
  begin
    RuntimeDeployFailed := True;   // 此时旧 runtime 已删，必须让用户看见失败
    Exit;
  end;

  Log('TB-DEPLOY timing swap ' + IntToStr(TbTickCount - PhaseTicks) + 'ms');

  // 归档用完即删：安装期峰值磁盘 = 318MB 归档 + 1.2GB 运行时，删掉回落 1.2GB。
  // 只在部署成功后删——失败时保留归档，用户重跑安装包无需重新下载 300MB。
  DeleteFile(Archive);

  // 预热：用装好的解释器冷导入一次完整应用栈，把杀软首次扫描成本吸收进
  // 安装阶段（详见函数头注释）。实测约 13s，是部署块里第二贵的动作。
  DeployStage('warmup', '正在预热运行环境（约 15 秒）…');
  PhaseTicks := TbTickCount;
  Exec(ExpandConstant('{app}\runtime\python\python.exe'),
       '-c "import deeptutor.api.main"', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  Log('TB-DEPLOY timing warmup ' + IntToStr(TbTickCount - PhaseTicks) + 'ms');
  Log('TB-DEPLOY timing total ' + IntToStr(TbTickCount - T0) + 'ms');
  DeployStage('done', '运行环境部署完成');
end;

// 卸载器侧的 Sleep 导入：external 声明的 setuponly 标志意味着上面的 SleepMs
// 仅在安装器进程可用；卸载器是独立进程，必须单独声明 uninstallonly 版本
// （两个声明指向同一个 kernel32.Sleep，名字错开即可，互不冲突）。
procedure SleepMsUninstall(Milliseconds: Longint);
  external 'Sleep@kernel32.dll stdcall uninstallonly';

(* ===== 卸载开始前的进程抢先清理（CurUninstallStepChanged/usUninstall） ====
   背景（本机实证 + Inno 机制）：Inno 的 Restart Manager 检测/关闭应用只
   存在于安装阶段；卸载器没有 RM 检测，也从不尝试关闭或杀掉占用待删文件
   的进程——它只删除未被占用的文件，被占用的直接跳过。实证：托盘版卸载
   后卸载注册表键与快捷方式删净，但 {app}\ThinkBuddyDesktop.exe 与
   {app}\runtime\ 因进程占用原样残留，壳与后端进程树继续存活，托盘图标
   还在，用户只能手动从托盘退出。托盘功能放大了这个缺口：关窗不再退出
   进程，「窗口收进托盘 → 直接卸载」成了常态路径。

   为什么不能指望消息式关闭兜底（CloseApplications=force 仅对安装生效；
   即便卸载阶段发消息也是死路）：
   1) 壳的托盘设计把一切「关窗」语义吞掉转成隐藏（WM_CLOSE →
      CloseReason.UserClosing → hide + Cancel；WinForms 层面无法区分用户
      点 X 与外部投递的 WM_CLOSE，应用侧无从放行）；
   2) 后端子进程（runtime\python\python.exe 的 launcher/uvicorn、
      runtime\node\node.exe 的 next standalone）没有窗口，消息无处投递。
   所以必须像 InitializeSetup 一样显式硬杀。

   时机：usUninstall = 「卸载即将开始」——用户已在确认框点「是」、任何
   文件尚未删除。挂在这里而非 InitializeUninstall：用户在确认框反悔取消
   卸载时不会误杀正在运行的应用；静默卸载（unins000.exe /SILENT）同样
   触发本回调。

   顺序与 InitializeSetup 一致（先壳后子进程，顺序不能反——壳有
   supervise() 自愈，必须先终结自愈源）：
     1) taskkill 硬杀壳（ThinkBuddyDesktop.exe）；
     2) PowerShell 按「可执行文件路径位于本安装目录下」精确清理 runtime
        子进程与孤儿进程——严禁 taskkill /IM python.exe / node.exe（会误
        杀系统里其他无关进程），必须按 ExecutablePath 匹配；同时排除
        unins*.exe（卸载器自身的可执行文件就在 {app} 下运行，不排除会把
        卸载器自己杀掉、卸载直接中断）；
     3) 睡约 1.5s 等文件句柄释放。
   与安装侧的差异：{app} 在卸载上下文直接可得（卸载器就装在 {app}），
   无需读注册表 InstallLocation。

   幂等与容错：应用未运行时 taskkill 报无匹配（返回码 128），属正常，
   忽略返回码；PowerShell 全路径不可用时回退 PATH；任何一步失败都不阻断
   卸载——最坏退化为修复前的现状（文件残留），不会更糟。
   ====================================================================== *)

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ErrorCode: Integer;  // Exec 返回码占位：全程只看成败，不判具体值
  Pattern: string;     // PowerShell -like 匹配模式：安装目录\*
  PsPath: string;      // powershell.exe 全路径
  PsParams: string;    // powershell.exe 参数串
begin
  if CurUninstallStep <> usUninstall then
    Exit;
  try
    // ---- 第 1 步：先杀壳，终结 supervise() 自愈源（顺序不能反） ----
    Exec(ExpandConstant('{sys}\taskkill.exe'),
      '/F /FI "IMAGENAME eq {#MyAppExeName}"',
      '', SW_HIDE, ewWaitUntilTerminated, ErrorCode);

    // ---- 第 2 步：按安装目录路径精确清理 runtime 子进程 ----
    Pattern := ExpandConstant('{app}') + '\*';
    // 罕见边界：路径含单引号（如用户名 O'Brien）时按 PowerShell 单引号
    // 字符串规则翻倍，防止命令被截断。
    StringChange(Pattern, '''', '''''');
    PsPath := ExpandConstant(
      '{sd}\Windows\System32\WindowsPowerShell\v1.0\powershell.exe');
    // 引号层级：Pascal 串里两个连续单引号即一个单引号；-like 模式用
    // PowerShell 单引号串包住（路径含空格也安全）；整条 -Command 再用
    // 双引号包住直接交给 powershell.exe——不经 cmd，无 OEM 代码页乱码
    // 与重定向干扰；SW_HIDE 保证不闪黑框。
    // unins*.exe 排除见函数头注释：不排除 = 卸载器自杀。
    PsParams :=
      '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command ' +
      '"Get-CimInstance Win32_Process | Where-Object { ' +
      '$_.ExecutablePath -like ''' + Pattern + ''' -and ' +
      '$_.Name -notlike ''unins*.exe'' } | ' +
      'ForEach-Object { Stop-Process -Id $_.ProcessId -Force ' +
      '-ErrorAction SilentlyContinue }"';
    if not Exec(PsPath, PsParams, '', SW_HIDE,
         ewWaitUntilTerminated, ErrorCode) then
      // 兜底：全路径不可用时走 PATH 里的 powershell.exe。
      Exec('powershell.exe', PsParams, '', SW_HIDE,
        ewWaitUntilTerminated, ErrorCode);

    // ---- 第 3 步：等约 1.5s，让被杀进程释放安装目录文件句柄 ----
    SleepMsUninstall(1500);
  except
    // 任何一步异常都不阻断卸载：最坏退化为修复前的现状（文件残留）。
  end;
end;

(* ===== 自定义安装退出码（GetCustomSetupExitCode） =========================
   为什么需要：Inno 的默认退出码只反映安装流程自身成败，而 runtime 解包发生
   在 [Code] 的 ssPostInstall 里——那里失败时 Inno 依旧返回 0。静默更新链路
   靠 setup 退出码判断成败（updater.bat 记录 `setup exit %errorlevel%`，
   进度窗按该数字渲染成功/失败态），返回 0 会让窗口显示「更新完成，正在
   打开新版本…」而实际壳没有 runtime 可启动，用户看到的是"更新成功但打不开"。

   退出码 8 是本安装器自定义的语义：runtime 部署失败（解压、校验、改名任一
   环节没成功）。updater.py 的进度窗对 8 有专门文案（提示杀软拦截 + 手动
   重跑安装包）。其余情况恒返回 0，与历史行为一致。

   与回滚策略的关系：失败时 ssPostInstall 一律保留旧 runtime（先解压后删旧），
   所以「当前版本未被破坏」对用户仍然成立——只是新版壳配旧运行时，应用可能
   起不来或版本不符，必须让用户知道。
   ====================================================================== *)
function GetCustomSetupExitCode: Integer;
begin
  if RuntimeDeployFailed then
    Result := 8
  else
    Result := 0;
end;
