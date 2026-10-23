; ThinkBuddy — click-to-install package (Inno Setup 7)
; Compile:  "C:\Program Files\Inno Setup 7\ISCC.exe" installer.iss
; Produces: dist\ThinkBuddySetup.exe  (installer, uninstaller, shortcuts)
;
; The setup ships:
;   ThinkBuddyDesktop.exe — the native shell (windowed, no console)
;   runtime\               — offline deeptutor runtime, installed as a plain
;                            tree into {app}\runtime (embeddable python +
;                            deeptutor + portable node). No python/node/pip
;                            needed on the target machine; the shell resolves
;                            {app}\runtime automatically.
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
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
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
; offline runtime tree (embeddable python + deeptutor + portable node)
; NOTE: compiled from runtime-build\staging so the installed layout matches the
; portable package exactly ({app}\runtime\python\...,  {app}\runtime\node\...)
Source: "..\runtime-build\staging\*";    DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs

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
