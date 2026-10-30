[Setup]
AppId={{E4D3F6A5-AB5C-4A4E-C18D-6FBC5A9D4E77}
AppName=RecallFS
AppVersion=0.1.0
AppPublisher=Local Tools
DefaultDirName={localappdata}\Programs\RecallFS
DefaultGroupName=RecallFS
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
SourceDir={#SourcePath}\..
OutputDir={#SourcePath}\..\..\release\installers
OutputBaseFilename=RecallFS-0.1.0-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\RecallFS.exe
SetupIconFile=assets\app.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "dist\RecallFS\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\RecallFS"; Filename: "{app}\RecallFS.exe"
Name: "{autodesktop}\RecallFS"; Filename: "{app}\RecallFS.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\RecallFS.exe"; Description: "Launch RecallFS"; Flags: nowait postinstall skipifsilent
