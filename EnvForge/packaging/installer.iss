[Setup]
AppId={{5F8C2A1E-7B4D-4E63-9A2C-0D6F1B8E3C77}
AppName=EnvForge
AppVersion=0.1.0
AppPublisher=Local Tools
DefaultDirName={localappdata}\Programs\EnvForge
DefaultGroupName=EnvForge
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
SourceDir={#SourcePath}\..
OutputDir={#SourcePath}\..\..\release\installers
OutputBaseFilename=EnvForge-0.1.0-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\EnvForge.exe
SetupIconFile=assets\app.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "dist\EnvForge\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\EnvForge"; Filename: "{app}\EnvForge.exe"
Name: "{autodesktop}\EnvForge"; Filename: "{app}\EnvForge.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\EnvForge.exe"; Description: "Launch EnvForge"; Flags: nowait postinstall skipifsilent
