; ============================================================================
; Playlist Canvas 1.3.1.5 Windows 설치 프로그램
; ChatGPT Codex와 Claude Code를 활용해 제작한 Playlist Canvas의 Inno Setup 스크립트입니다.
; ============================================================================

#define MyAppName "Playlist Canvas"
#define MyAppVersion "1.3.1.5"
#define MyAppFileVersion "1.3.1.5"
#define MyAppPublisher "Ji Beak min(tharu8813)"
#define MyAppCopyright "© 2026 Ji Beak min(tharu8813). All rights reserved."
#define MyAppDescription "음악, 가사와 비주얼 요소를 편집해 플레이리스트 영상을 만드는 Windows 데스크톱 편집기"
#define MyAppComments "ChatGPT Codex와 Claude Code를 활용해 제작한 프로그램입니다."
#define MyAppURL "https://github.com/tharu8813/Playlist-Canvas"
#define MyAppExeName "Playlist Canvas.exe"
; 최초 공개 설치본부터 이 GUID를 변경하지 않아야 이후 버전이 정상 업그레이드됩니다.
#define MyAppGUID "{{28b780ab-d9ec-420a-88d8-17d364505722}"

; 선택적 설정 (필요시 수정)
#ifndef AppSourcePath
  #define AppSourcePath "dist\Playlist Canvas"
#endif
#define SetupIconPath "app\resources\app_icon.ico"
#define ProjectIconPath "app\resources\project_file_icon.ico"
#define LicenseFilePath "LICENSE.txt"
#define ReadmeFilePath ""   ; Readme 파일 경로 (예: "readme.txt")

; ============================================================================
; 이하 코드는 수정하지 마세요
; ============================================================================

[Setup]
AppId={#MyAppGUID}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
#if MyAppURL != ""
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
#endif
AppCopyright={#MyAppCopyright}
AppComments={#MyAppComments}
DefaultDirName={commonpf}\{#MyAppName}
PrivilegesRequired=admin
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=output-setup
OutputBaseFilename={#MyAppName}-{#MyAppVersion}-setup
#if SetupIconPath != ""
SetupIconFile={#SetupIconPath}
#endif
#if LicenseFilePath != ""
LicenseFile={#LicenseFilePath}
#endif
#if ReadmeFilePath != ""
InfoBeforeFile={#ReadmeFilePath}
#endif
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
Uninstallable=yes
AlwaysRestart=no
DisableDirPage=no
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll
RestartApplications=no
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
VersionInfoVersion={#MyAppFileVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppDescription}
VersionInfoCopyright={#MyAppCopyright}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
ChangesAssociations=yes

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#AppSourcePath}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "*.pdb,*.log,*.tmp"
Source: "{#ProjectIconPath}"; DestDir: "{app}"; DestName: "project_file_icon.ico"; Flags: ignoreversion
Source: "LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Check: UpdateDesktopShortcut

[Registry]
; Windows 탐색기에서 .pvsproj를 더블클릭하면 설치된 앱으로 프로젝트를 엽니다.
Root: HKA; Subkey: "Software\Classes\.pvsproj"; ValueType: string; ValueName: ""; ValueData: "PlaylistCanvas.Project"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\PlaylistCanvas.Project"; ValueType: string; ValueName: ""; ValueData: "Playlist Canvas Project"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\PlaylistCanvas.Project\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\project_file_icon.ico,0"
Root: HKA; Subkey: "Software\Classes\PlaylistCanvas.Project\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: string; ValueName: ".pvsproj"; ValueData: ""; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Flags: uninsdeletekey

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent; Check: not IsAutomaticUpdate
Filename: "{app}\{#MyAppExeName}"; Parameters: "{code:UpdateLaunchArguments}"; Flags: nowait runasoriginaluser; Check: IsAutomaticUpdate

[UninstallDelete]
Type: dirifempty; Name: "{app}"

[Code]
function OpenProcess(Access: Cardinal; Inherit: Integer; ProcessId: Cardinal): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(Handle: THandle; Milliseconds: Cardinal): Cardinal;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

function IsAutomaticUpdate(): Boolean;
begin
  Result := ExpandConstant('{param:AUTOMATICUPDATE|0}') = '1';
end;

function UpdateDesktopShortcut(): Boolean;
begin
  Result := IsAutomaticUpdate() and not WizardIsTaskSelected('desktopicon') and
    (FileExists(ExpandConstant('{userdesktop}\{#MyAppName}.lnk')) or
     FileExists(ExpandConstant('{commondesktop}\{#MyAppName}.lnk')));
end;

function UpdateLaunchArguments(Param: String): String;
begin
  Result := '--resume-update ' + AddQuotes(ExpandConstant('{param:UPDATERECOVERY|}'));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ParentId: Integer;
  ParentHandle: THandle;
  ReadyPath: String;
begin
  Result := '';
  if not IsAutomaticUpdate() then Exit;
  ParentId := StrToIntDef(ExpandConstant('{param:UPDATEPID|0}'), 0);
  if ParentId <= 0 then begin
    Result := 'The update parent process is missing.';
    Exit;
  end;
  ReadyPath := ExpandConstant('{param:UPDATEREADY|}');
  { Older updaters already close before launching Setup and have no ready signal. }
  if (ReadyPath <> '') and not SaveStringToFile(ReadyPath, 'ready', False) then begin
    Result := 'Could not notify Playlist Canvas that Setup is ready.';
    Exit;
  end;
  ParentHandle := OpenProcess($00100000, 0, ParentId);
  if ParentHandle = 0 then begin
    { ERROR_INVALID_PARAMETER means the parent has already exited. }
    if DLLGetLastError() <> 87 then
      Result := 'Could not wait for Playlist Canvas to close.';
    Exit;
  end;
  try
    if WaitForSingleObject(ParentHandle, 120000) <> 0 then
      Result := 'Playlist Canvas did not finish closing. No update was installed.';
  finally
    CloseHandle(ParentHandle);
  end;
end;
