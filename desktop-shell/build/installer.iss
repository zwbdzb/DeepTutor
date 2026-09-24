; EduBuddy — click-to-install package (Inno Setup 7)
; Compile:  "C:\Program Files\Inno Setup 7\ISCC.exe" installer.iss
; Produces: dist\EduBuddySetup.exe  (installer, uninstaller, shortcuts)
;
; The setup ships:
;   EduBuddyDesktop.exe   — the native shell (windowed, no console)
;   runtime\               — offline deeptutor runtime, installed as a plain
;                            tree into {app}\runtime (embeddable python +
;                            deeptutor + portable node). No python/node/pip
;                            needed on the target machine; the shell resolves
;                            {app}\runtime automatically.
;   assets\icon.ico        — shortcuts + uninstaller icon
;
; End users double-click EduBuddySetup.exe -> Next/Next/Install -> the app
; launches on its own. Uninstall keeps ~\EduBuddy workspace (learning data).

#define MyAppName "EduBuddy"
; AppVersion is injected by build.ps1 via /DMyAppVersion=<0.2.0+dt<source version>>,
; so the installer metadata always tracks deeptutor/__version__.py.
; The fallback below only applies to a bare manual `ISCC installer.iss` run
; and is intentionally wrong-looking so a stale build is easy to spot.
#ifndef MyAppVersion
#define MyAppVersion "0.2.0+dtSET-BY-build.ps1"
#endif
#define MyAppPublisher "HKUDS"
#define MyAppExeName "EduBuddyDesktop.exe"

[Setup]
AppId={{8C3A8E6A-5D44-4A67-BD6E-7A1F3C4B0E5F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\EduBuddy
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=EduBuddySetup
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
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; native shell + assets
Source: "..\dist\EduBuddyDesktop.exe"; DestDir: "{app}"; Flags: ignoreversion
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
; 安装新版时清掉本机遗留的托管运行时缓存（%LOCALAPPDATA%\EduBuddy\runtime）。
; 它是旧版 runtime.zip 自解压的历史产物，版本可能停在任意旧版（曾以 1.6.9
; 遮蔽新装的 1.6.10，见 docs/adr/ADR-005）。本安装器自带完整 runtime/ 树，
; 缓存纯属冗余，删除无任何用户数据损失（学习数据在 %USERPROFILE%\EduBuddy）。
Type: filesandordirs; Name: "{localappdata}\EduBuddy\runtime"

[UninstallDelete]
; 卸载时清掉托管缓存 + 应用目录；keep user data (workspace lives in
; %USERPROFILE%\EduBuddy) — only the app dir (incl. the installed runtime
; copy) and the version-locked runtime cache are removed.
Type: filesandordirs; Name: "{localappdata}\EduBuddy\runtime"
Type: filesandordirs; Name: "{app}"
