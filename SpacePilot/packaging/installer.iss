[Setup]
AppId={{C2B1D4E3-8F3A-4E2C-AF6B-4D9A3E7B2C55}
AppName=SpacePilot
AppVersion=0.1.0
AppPublisher=Local Tools
DefaultDirName={localappdata}\Programs\SpacePilot
DefaultGroupName=SpacePilot
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
SourceDir={#SourcePath}\..
OutputDir={#SourcePath}\..\..\release\installers
OutputBaseFilename=SpacePilot-0.1.0-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\SpacePilot.exe
SetupIconFile=assets\app.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "dist\SpacePilot\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\SpacePilot"; Filename: "{app}\SpacePilot.exe"; Parameters: "--gui"
Name: "{autodesktop}\SpacePilot"; Filename: "{app}\SpacePilot.exe"; Parameters: "--gui"; Tasks: desktopicon

[Run]
Filename: "{app}\SpacePilot.exe"; Parameters: "--gui"; Description: "Launch SpacePilot"; Flags: nowait postinstall skipifsilent
