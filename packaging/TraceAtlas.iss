#define AppVersion "0.1.0"
[Setup]
AppId={{D5514E5F-7797-46C9-9E39-37B88D4383CC}
AppName=TraceAtlas
AppVersion={#AppVersion}
AppPublisher=TraceAtlas
DefaultDirName={localappdata}\Programs\TraceAtlas
DefaultGroupName=TraceAtlas
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19045
OutputDir=..\dist
OutputBaseFilename=TraceAtlas-0.1.0-setup-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\TraceAtlas.exe
CloseApplications=yes
RestartApplications=no
LicenseFile=..\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\TraceAtlas\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\TraceAtlas"; Filename: "{app}\TraceAtlas.exe"
Name: "{autodesktop}\TraceAtlas"; Filename: "{app}\TraceAtlas.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\TraceAtlas.exe"; Description: "Open TraceAtlas"; Flags: nowait postinstall skipifsilent

; Projects are user-selected folders. There are intentionally no project deletion rules.
