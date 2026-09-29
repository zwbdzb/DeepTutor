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
; Brand migration: the shell renames legacy %LOCALAPPDATA%\EduBuddy /
; %USERPROFILE%\EduBuddy dirs to their ThinkBuddy counterparts on first run.

#define MyAppName "ThinkBuddy"
; AppVersion is injected by build.ps1 via /DMyAppVersion=<0.2.0+dt<source version>>,
; so the installer metadata always tracks deeptutor/__version__.py.
; The fallback below only applies to a bare manual `ISCC installer.iss` run
; and is intentionally wrong-looking so a stale build is easy to spot.
#ifndef MyAppVersion
#define MyAppVersion "0.2.0+dtSET-BY-build.ps1"
#endif
#define MyAppPublisher "HKUDS"
#define MyAppExeName "ThinkBuddyDesktop.exe"

[Setup]
; New AppId for the ThinkBuddy brand (the old EduBuddy entry, if any, stays
; separately uninstallable; internal-test install base ~0, no upgrade path needed).
AppId={{3B4FC0AE-E3E3-491F-B3B6-59BBBF1D6305}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\ThinkBuddy
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=ThinkBuddySetup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Languages]
; 简体中文放第一位：[Languages] 的第一项是 Inno 的 fallback 默认语言。
; 中文 Windows 若因 LanguageID 匹配失败走到 fallback，拿到的也是中文向导，
; 而不是英文。英文系统用户仍按系统语言自动匹配到 english，不受影响。
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

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
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[InstallDelete]
; 安装新版时清掉本机遗留的托管运行时缓存（%LOCALAPPDATA%\ThinkBuddy\runtime，
; 以及品牌迁移前的 %LOCALAPPDATA%\EduBuddy\runtime）。
; 它是旧版 runtime.zip 自解压的历史产物，版本可能停在任意旧版（曾以 1.6.9
; 遮蔽新装的 1.6.10，见 docs/adr/ADR-005）。本安装器自带完整 runtime/ 树，
; 缓存纯属冗余，删除无任何用户数据损失（学习数据在 %USERPROFILE%\ThinkBuddy；
; 壳首次启动会把 %USERPROFILE%\EduBuddy 整体改名迁移过去）。
Type: filesandordirs; Name: "{localappdata}\ThinkBuddy\runtime"
Type: filesandordirs; Name: "{localappdata}\EduBuddy\runtime"

[UninstallDelete]
; 卸载时清掉托管缓存 + 应用目录；keep user data (workspace lives in
; %USERPROFILE%\ThinkBuddy) — only the app dir (incl. the installed runtime
; copy) and the version-locked runtime cache are removed.
Type: filesandordirs; Name: "{localappdata}\ThinkBuddy\runtime"
Type: filesandordirs; Name: "{app}"
