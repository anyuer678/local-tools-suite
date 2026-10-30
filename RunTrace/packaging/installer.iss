[Setup]
AppId={{B1A0F3A2-7C2E-4D1B-9E5A-3C8F2D6A1B44}
AppName=RunTrace
AppVersion=0.1.0
AppPublisher=Local Tools
DefaultDirName={localappdata}\Programs\RunTrace
DefaultGroupName=RunTrace
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
SourceDir={#SourcePath}\..
OutputDir={#SourcePath}\..\..\release\installers
OutputBaseFilename=RunTrace-0.1.0-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\RunTrace.exe
SetupIconFile=assets\app.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "dist\RunTrace\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\RunTrace"; Filename: "{app}\RunTrace.exe"
Name: "{autodesktop}\RunTrace"; Filename: "{app}\RunTrace.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\RunTrace.exe"; Description: "Launch RunTrace"; Flags: nowait postinstall skipifsilent
