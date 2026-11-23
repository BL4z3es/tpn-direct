; Inno Setup 6 script for the tpn Windows service and tray application.
; Normally invoked through packaging/windows/build_installer.py, which supplies the defines:
;   MyAppVersion=0.3.0, Arch=x86_64|arm64, StagedDir=<stage>, OutputDir=<dist>
; The wrapper also provides IsppIncludePath so this script can find its icons.

#ifndef MyAppVersion
  #error "Pass /DMyAppVersion=<version> to ISCC"
#endif
#ifndef Arch
  #error "Pass /DArch=x86_64 or /DArch=arm64 to ISCC"
#endif
#ifndef StagedDir
  #error "Pass /DStagedDir=<staged package directory> to ISCC"
#endif

#define ProjectRoot SourcePath + "\.."
#ifndef IsppIncludePath
  #define IsppIncludePath ProjectRoot
#endif
#ifndef OutputDir
  #define OutputDir ProjectRoot + "\dist"
#endif

#if Arch == "x86_64"
  #define AllowedArchitectures "x64compatible"
#elif Arch == "arm64"
  #define AllowedArchitectures "arm64"
#else
  #error "Arch must be x86_64 or arm64"
#endif

#define MyAppName "tpn-direct VPN"
#define MyAppPublisher "tpn-direct"
#define MyAppURL "https://github.com/BL4z3es/tpn-direct"
; Keep this AppId stable: it lets Inno recognise and upgrade an existing installation.
#define MyAppId "{{E6F3A8B2-4C9D-4A5E-B1D7-8F2C9A3E4B6D}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\tpn
DefaultGroupName=tpn
DisableProgramGroupPage=yes
PrivilegesRequired=admin
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=tpn-{#MyAppVersion}-windows-{#Arch}-setup
UninstallDisplayIcon={app}\tpn-app.exe
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed={#AllowedArchitectures}
ArchitecturesInstallIn64BitMode={#AllowedArchitectures}
; The icon is produced by the app workstream; the build also works before it exists.
#if FileExists(ProjectRoot + "\packaging\icons\tpn.ico")
SetupIconFile=packaging\icons\tpn.ico
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "Launch tpn automatically when any user signs in"; GroupDescription: "Additional tasks:"; Flags: checkedonce

[Files]
Source: "{#StagedDir}\tpn.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#StagedDir}\tpn-app.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#StagedDir}\README.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#StagedDir}\tor\*"; DestDir: "{app}\tor"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\tpn"; Filename: "{app}\tpn-app.exe"; WorkingDir: "{app}"; Comment: "tpn-direct VPN tray application"

[Registry]
; This machine-wide Run value launches the tray app in every user's logon session.
Root: HKLM; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "tpn"; ValueData: """{app}\tpn-app.exe"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
; A normal, non-postinstall Run entry would elevate the tray app. runasoriginaluser avoids that.
Filename: "{app}\tpn-app.exe"; Description: "{cm:LaunchProgram,tpn}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[Code]
const
  TpnServiceKey = 'SYSTEM\CurrentControlSet\Services\tpn';

function RunHidden(const Filename, Parameters: String): Integer;
var
  ResultCode: Integer;
begin
  if Exec(Filename, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Result := ResultCode
  else
    Result := -1;
end;

function PowerShellExe(): String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

function StopServiceCommand(): String;
begin
  { sc.exe stop is asynchronous. PowerShell lets us wait for the daemon's clean teardown,
    which can take about a minute because it may restart the Tailscale service. }
  Result := '-NoProfile -NonInteractive -Command "$ErrorActionPreference = ''Stop''; ' +
    '$service = Get-Service -Name ''tpn'' -ErrorAction SilentlyContinue; ' +
    'if ($null -ne $service) { Stop-Service -Name ''tpn'' -Force; ' +
    '$service.WaitForStatus(''Stopped'', [TimeSpan]::FromSeconds(90)) }"';
end;

function InitializeSetup(): Boolean;
var
  Answer: Integer;
  ErrorCode: Integer;
begin
  Result := True;
  if not RegKeyExists(HKLM, 'SYSTEM\CurrentControlSet\Services\Tailscale') then begin
    Answer := MsgBox(
      'tpn needs the official Tailscale Windows client, but its Tailscale service is not installed.' +
      #13#10#13#10 + 'Install it from https://tailscale.com/download/windows and run this installer again.' +
      #13#10#13#10 + 'Open the Tailscale download page now?',
      mbCriticalError, MB_YESNO);
    if Answer = IDYES then
      ShellExec('open', 'https://tailscale.com/download/windows', '', '', SW_SHOW, ewNoWait, ErrorCode);
    Result := False;
  end;
end;

function PrepareToInstall(): String;
var
  ResultCode: Integer;
begin
  Result := '';
  { An upgrade must stop the old daemon before Inno replaces its locked one-file executable. }
  if RegKeyExists(HKLM, TpnServiceKey) then begin
    ResultCode := RunHidden(PowerShellExe(), StopServiceCommand());
    if ResultCode <> 0 then begin
      Result := 'The existing tpn service could not be stopped (exit code ' +
        IntToStr(ResultCode) + '). Stop it in services.msc and try again.';
      exit;
    end;
    ResultCode := RunHidden(ExpandConstant('{app}\tpn.exe'), 'service-uninstall');
    if ResultCode <> 0 then begin
      Result := 'The existing tpn service could not be removed (exit code ' +
        IntToStr(ResultCode) + ').';
      exit;
    end;
  end;
  { A running tray app can also lock its executable. No matching process is not an error here. }
  RunHidden(ExpandConstant('{sys}\taskkill.exe'), '/IM tpn-app.exe /F');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then begin
    ResultCode := RunHidden(ExpandConstant('{app}\tpn.exe'), 'service-install');
    if ResultCode <> 0 then
      RaiseException('tpn service installation failed (exit code ' + IntToStr(ResultCode) +
        '). See the installer log for details.');
    ResultCode := RunHidden(ExpandConstant('{sys}\sc.exe'), 'start tpn');
    if ResultCode <> 0 then
      RaiseException('The tpn service was installed but could not be started (exit code ' +
        IntToStr(ResultCode) + '). Check that Tailscale is running.');
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then begin
    { These run before {app} is removed. Keep going on failure so the uninstaller can clean up. }
    RunHidden(PowerShellExe(), StopServiceCommand());
    RunHidden(ExpandConstant('{app}\tpn.exe'), 'service-uninstall');
    RunHidden(ExpandConstant('{sys}\taskkill.exe'), '/IM tpn-app.exe /F');
  end;
  if CurUninstallStep = usPostUninstall then begin
    { Preserve state and logs for silent uninstall; interactive users make an explicit choice. }
    if (not UninstallSilent) and DirExists(ExpandConstant('{commonappdata}\tpn')) then begin
      if MsgBox('Delete tpn state, settings and logs in ' +
        ExpandConstant('{commonappdata}\tpn') + '?' + #13#10#13#10 +
        'Choose No to keep them for a future installation or troubleshooting.',
        mbConfirmation, MB_YESNO) = IDYES then
        DelTree(ExpandConstant('{commonappdata}\tpn'), True, True, True);
    end;
  end;
end;
