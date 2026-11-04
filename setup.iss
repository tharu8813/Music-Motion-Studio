; ============================================================================
; Music Motion Studio 1.4.0.1 Windows 설치 프로그램
; ChatGPT Codex와 Claude Code를 활용해 제작한 Music Motion Studio의 Inno Setup 스크립트입니다.
; ============================================================================

#define MyAppName "Music Motion Studio"
#define MyAppVersion "1.4.0.1"
#define MyAppFileVersion "1.4.0.1"
#define MyAppPublisher "Ji Beak min(tharu8813)"
#define MyAppCopyright "© 2026 Ji Beak min(tharu8813). All rights reserved."
#define MyAppDescription "음악, 가사와 비주얼 요소를 편집해 플레이리스트 영상을 만드는 Windows 데스크톱 편집기"
#define MyAppComments "ChatGPT Codex와 Claude Code를 활용해 제작한 프로그램입니다."
#define MyAppURL "https://github.com/tharu8813/Music-Motion-Studio"
#define MyAppExeName "Music Motion Studio.exe"
#define PreviousAppName "Playlist Canvas"
; 최초 공개 설치본부터 이 GUID를 변경하지 않아야 이후 버전이 정상 업그레이드됩니다.
#define MyAppGUID "{{28b780ab-d9ec-420a-88d8-17d364505722}"

; 선택적 설정 (필요시 수정)
#ifndef AppSourcePath
  #define AppSourcePath "dist\Music Motion Studio"
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
UsePreviousAppDir=yes
UsePreviousGroup=no
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

[CustomMessages]
korean.UpdateInstalling=Music Motion Studio 업데이트 설치
korean.UpdateClosingEditor=작업을 저장하고 편집기가 종료되기를 기다리는 중입니다.
korean.OldFileStillPresent=이전 프로그램 파일을 지우지 못했습니다. Music Motion Studio와 파일을 사용 중인 프로그램을 닫고 다시 설치해 주세요.%n%n%1
english.UpdateInstalling=Installing the Music Motion Studio update
english.UpdateClosingEditor=Saving the workspace and waiting for the editor to close.
english.OldFileStillPresent=An old application file could not be removed. Close Music Motion Studio and any program using the file, then run Setup again.%n%n%1

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

[InstallDelete]
; Replace the shipped application payload before copying the new version.
; Keep the installation directory, user files, and Inno's uninstall records.
Type: filesandordirs; Name: "{app}\_internal"; Check: IsExistingManagedInstallation; AfterInstall: VerifyOldFileRemoved('_internal')
Type: files; Name: "{app}\project_file_icon.ico"; Check: IsExistingManagedInstallation; AfterInstall: VerifyOldFileRemoved('project_file_icon.ico')
Type: files; Name: "{app}\LICENSE.txt"; Check: IsExistingManagedInstallation; AfterInstall: VerifyOldFileRemoved('LICENSE.txt')
Type: files; Name: "{app}\{#MyAppExeName}"; Check: IsExistingManagedInstallation; AfterInstall: VerifyOldFileRemoved('{#MyAppExeName}')
; Remove stale launchers after the product/EXE name change.
Type: files; Name: "{app}\{#PreviousAppName}.exe"; Check: IsExistingManagedInstallation; AfterInstall: VerifyOldFileRemoved('{#PreviousAppName}.exe')
Type: files; Name: "{app}\Playlist.Canvas.exe"; Check: IsExistingManagedInstallation; AfterInstall: VerifyOldFileRemoved('Playlist.Canvas.exe')
Type: files; Name: "{app}\Playlist-Canvas.exe"; Check: IsExistingManagedInstallation; AfterInstall: VerifyOldFileRemoved('Playlist-Canvas.exe')
Type: files; Name: "{app}\PlaylistCanvas.exe"; Check: IsExistingManagedInstallation; AfterInstall: VerifyOldFileRemoved('PlaylistCanvas.exe')
Type: files; Name: "{app}\integrity.json"; Check: IsExistingManagedInstallation; AfterInstall: VerifyOldFileRemoved('integrity.json')
Type: files; Name: "{userdesktop}\{#PreviousAppName}.lnk"
Type: files; Name: "{commondesktop}\{#PreviousAppName}.lnk"
Type: files; Name: "{userprograms}\{#PreviousAppName}\*.lnk"
Type: files; Name: "{commonprograms}\{#PreviousAppName}\*.lnk"
Type: dirifempty; Name: "{userprograms}\{#PreviousAppName}"
Type: dirifempty; Name: "{commonprograms}\{#PreviousAppName}"

[Registry]
; Windows 탐색기에서 .pvsproj를 더블클릭하면 설치된 앱으로 프로젝트를 엽니다.
; Keep this ProgID stable so existing Explorer default-app choices still work.
Root: HKA; Subkey: "Software\Classes\Applications\{#PreviousAppName}.exe"; Flags: deletekey
Root: HKA; Subkey: "Software\Classes\.pvsproj"; ValueType: string; ValueName: ""; ValueData: "PlaylistCanvas.Project"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\PlaylistCanvas.Project"; ValueType: string; ValueName: ""; ValueData: "Music Motion Studio Project"; Flags: uninsdeletekey
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
var
  HadDesktopShortcut: Boolean;
  ManagedInstallationDir: String;
  UpdatePreparationPage: TOutputMarqueeProgressWizardPage;

function InitializeSetup(): Boolean;
begin
  { Capture this before [InstallDelete] removes the previous brand's links. }
  HadDesktopShortcut :=
    FileExists(ExpandConstant('{userdesktop}\{#MyAppName}.lnk')) or
    FileExists(ExpandConstant('{commondesktop}\{#MyAppName}.lnk')) or
    FileExists(ExpandConstant('{userdesktop}\{#PreviousAppName}.lnk')) or
    FileExists(ExpandConstant('{commondesktop}\{#PreviousAppName}.lnk'));
  Result := True;
end;

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

procedure InitializeWizard();
begin
  if IsAutomaticUpdate() then begin
    WizardForm.Caption := CustomMessage('UpdateInstalling');
    UpdatePreparationPage := CreateOutputMarqueeProgressPage(
      CustomMessage('UpdateInstalling'), CustomMessage('UpdateClosingEditor'));
  end;
end;

function IsExistingManagedInstallation(): Boolean;
var
  AppDir, RegisteredDir, UninstallKey: String;
begin
  AppDir := ExpandConstant('{app}');
  UninstallKey := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\' +
    ExpandConstant('{#MyAppGUID}_is1');
  { Keep the result for this directory as InstallDelete removes the markers. }
  Result :=
    (CompareText(AppDir, ManagedInstallationDir) = 0) or
    ((RegQueryStringValue(HKLM64, UninstallKey, 'InstallLocation', RegisteredDir) or
      RegQueryStringValue(HKLM32, UninstallKey, 'InstallLocation', RegisteredDir)) and
     (CompareText(AddBackslash(RegisteredDir), AddBackslash(AppDir)) = 0)) or
    (FileExists(AddBackslash(AppDir) + 'integrity.json') and
     (FileExists(AddBackslash(AppDir) + '{#PreviousAppName}.exe') or
      FileExists(AddBackslash(AppDir) + '{#MyAppExeName}') or
      FileExists(AddBackslash(AppDir) + 'Playlist.Canvas.exe') or
      FileExists(AddBackslash(AppDir) + 'Playlist-Canvas.exe') or
      FileExists(AddBackslash(AppDir) + 'PlaylistCanvas.exe')));
  if Result then
    ManagedInstallationDir := AppDir;
end;

procedure VerifyOldFileRemoved(FileName: String);
var
  Path: String;
begin
  Path := AddBackslash(ExpandConstant('{app}')) + FileName;
  if DirExists(Path) or FileExists(Path) then
    RaiseException(FmtMessage(CustomMessage('OldFileStillPresent'), [Path]));
end;

function UpdateDesktopShortcut(): Boolean;
begin
  Result := HadDesktopShortcut and not WizardIsTaskSelected('desktopicon');
end;

function UpdateLaunchArguments(Param: String): String;
begin
  Result := '--resume-update ' + AddQuotes(ExpandConstant('{param:UPDATERECOVERY|}'));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ParentId, Poll: Integer;
  ParentHandle: THandle;
  ReadyPath: String;
  WaitResult: Cardinal;
begin
  Result := '';
  if not IsAutomaticUpdate() then Exit;
  ParentId := StrToIntDef(ExpandConstant('{param:UPDATEPID|0}'), 0);
  if ParentId <= 0 then begin
    Result := 'The update parent process is missing.';
    Exit;
  end;
  ParentHandle := OpenProcess($00100000, 0, ParentId);
  if ParentHandle = 0 then begin
    { ERROR_INVALID_PARAMETER means the parent has already exited. }
    if DLLGetLastError() <> 87 then
      Result := 'Could not wait for Music Motion Studio to close.';
    Exit;
  end;
  try
    UpdatePreparationPage.Show;
    WizardForm.Show;
    try
      { Older updaters may have no ready signal. }
      ReadyPath := ExpandConstant('{param:UPDATEREADY|}');
      if (ReadyPath <> '') and not SaveStringToFile(ReadyPath, 'ready', False) then begin
        Result := 'Could not notify Music Motion Studio that Setup is ready.';
        Exit;
      end;
      for Poll := 1 to 480 do begin
        WaitResult := WaitForSingleObject(ParentHandle, 250);
        if WaitResult = 0 then Exit;
        if WaitResult <> 258 then Break;
        UpdatePreparationPage.Animate;
      end;
      Result := 'Music Motion Studio did not finish closing. No update was installed.';
    finally
      UpdatePreparationPage.Hide;
    end;
  finally
    CloseHandle(ParentHandle);
  end;
end;
