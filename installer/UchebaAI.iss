#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef AppSource
  #error AppSource is required
#endif
#ifndef AppOutput
  #error AppOutput is required
#endif

[Setup]
AppId={{9A4F80D8-9AD1-4B56-955B-4F261461413C}
AppName=Учёба AI
AppVersion={#AppVersion}
AppPublisher=Учёба AI
AppPublisherURL=https://github.com/cheden123/ucheba
AppSupportURL=https://github.com/cheden123/ucheba/issues
DefaultDirName={localappdata}\Programs\UchebaAI
DefaultGroupName=Учёба AI
UninstallDisplayIcon={app}\UchebaAI.exe
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.22000
OutputDir={#AppOutput}
OutputBaseFilename=UchebaAI-Setup-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; Flags: unchecked

[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal\ucheba_ai-0.1.0.dist-info"; Check: SafeStaleMetadata('0.1.0')
Type: filesandordirs; Name: "{app}\_internal\ucheba_ai-0.2.0.dist-info"; Check: SafeStaleMetadata('0.2.0')
Type: filesandordirs; Name: "{app}\_internal\ucheba_ai-0.3.0.dist-info"; Check: SafeStaleMetadata('0.3.0')
Type: filesandordirs; Name: "{app}\_internal\ucheba_ai-0.4.0.dist-info"; Check: SafeStaleMetadata('0.4.0')
Type: filesandordirs; Name: "{app}\_internal\ucheba_ai-0.4.1.dist-info"; Check: SafeStaleMetadata('0.4.1')
#ifdef ExternalMedia
Type: files; Name: "{app}\ffmpeg.exe"
Type: files; Name: "{app}\ffprobe.exe"
Type: files; Name: "{app}\_internal\ucheba\models\diarization\embedding.onnx"
#else
Type: files; Name: "{app}\download-ffmpeg.txt"
Type: files; Name: "{app}\_internal\ucheba\models\diarization\embedding-download.txt"
#endif

[Icons]
Name: "{autoprograms}\Учёба AI"; Filename: "{app}\UchebaAI.exe"
Name: "{autodesktop}\Учёба AI"; Filename: "{app}\UchebaAI.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\UchebaAI.exe"; Description: "Запустить Учёба AI"; Flags: nowait postinstall skipifsilent

[Code]
function GetFileAttributes(FileName: string): LongWord;
external 'GetFileAttributesW@kernel32.dll stdcall';

function SafeStaleMetadata(Version: string): Boolean;
var
  Root, Current, Parent: string;
  Attributes: LongWord;
begin
  Result := False;
  Root := AddBackslash(ExpandFileName(ExpandConstant('{app}')));
  Current := ExpandFileName(Root + '_internal\ucheba_ai-' + Version + '.dist-info');
  if CompareText(Copy(Current, 1, Length(Root)), Root) <> 0 then
    Exit;
  repeat
    Attributes := GetFileAttributes(Current);
    if (Attributes = $FFFFFFFF) or ((Attributes and $400) <> 0) then
      Exit;
    Parent := ExtractFileDir(Current);
    if (Length(Parent) = 0) or (Parent = Current) then
      Break;
    Current := Parent;
  until False;
  Result := True;
end;
