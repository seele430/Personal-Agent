; Personal Agent 安装包（Inno Setup 6）
;
; 生成：ISCC.exe desktop\installer.iss
; 前提：先跑过 pyinstaller desktop\build.spec，产出 dist\PersonalAgent\
;
; 设计取舍：
;   - 装到 {localappdata} 而不是 Program Files → 不需要管理员权限；
;   - 数据放 %APPDATA%\PersonalAgent（程序自己建），卸载时不删，避免丢聊天记录。

#define AppName "Personal Agent"
#define AppVersion "0.1.0"
#define AppPublisher "seele"
#define AppExeName "PersonalAgent.exe"

[Setup]
AppId={{8F3A2C71-6B4D-4E19-9C2A-5D7E1B8A3F60}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\PersonalAgent
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=PersonalAgent-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExeName}
SetupIconFile=icon.ico
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: checkedonce

[Files]
; 整个 onedir 产物
Source: "..\dist\PersonalAgent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只清理程序目录；%APPDATA%\PersonalAgent（配置与聊天数据）保留
Type: filesandordirs; Name: "{app}"
