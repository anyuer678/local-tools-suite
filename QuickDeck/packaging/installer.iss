[Setup]
AppId={{D3C2E5F4-9A4B-4F3D-B07C-5EAB4F8C3D66}
AppName=QuickDeck
AppVersion=0.1.0
AppPublisher=Local Tools
DefaultDirName={localappdata}\Programs\QuickDeck
DefaultGroupName=QuickDeck
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
SourceDir={#SourcePath}\..
OutputDir={#SourcePath}\..\..\release\installers
OutputBaseFilename=QuickDeck-0.1.0-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\QuickDeck.exe
SetupIconFile=assets\app.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "dist\QuickDeck\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourcePath}\..\dist\QuickDeckCLI\*"; DestDir: "{app}\cli"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\QuickDeck"; Filename: "{app}\QuickDeck.exe"; Parameters: "gui"
Name: "{group}\QuickDeck CLI"; Filename: "{app}\cli\QuickDeckCLI.exe"
Name: "{autodesktop}\QuickDeck"; Filename: "{app}\QuickDeck.exe"; Parameters: "gui"; Tasks: desktopicon

[Run]
Filename: "{app}\QuickDeck.exe"; Parameters: "gui"; Description: "Launch QuickDeck"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{userappdata}\QuickDeck');
    { 静默卸载不弹窗，默认保留数据；交互卸载由用户明确选择。 }
    if DirExists(DataDir) and not UninstallSilent then
    begin
      if MsgBox('是否同时删除 QuickDeck 的配置、历史记录和插件数据？' + #13#10 +
        DataDir, mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
