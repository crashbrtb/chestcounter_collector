; Total Battle Chest Collector - Windows installer (Inno Setup 6).
; Built by build.ps1, which passes AppVersion from Cargo.toml and fills ..\dist first.

#ifndef AppVersion
  #define AppVersion "4.3.0"
#endif

[Setup]
AppId={{6B1D8F0E-3C7A-4E5B-9A2D-7F4C1E8B2A90}
AppName=Total Battle Chest Collector
AppVersion={#AppVersion}
AppVerName=Total Battle Chest Collector {#AppVersion}
AppPublisher=Aircrash
AppPublisherURL=https://github.com/crashbrtb/chestcounter
; Installed per user by default, in a folder the program can write its configuration to.
; The folder can be changed on the "Select Destination Location" page: choosing the folder
; of an earlier installation keeps its config\config.json.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\TB Chest Collector
DisableDirPage=no
UsePreviousAppDir=yes
DefaultGroupName=Total Battle Chest Collector
AllowNoIcons=yes
LicenseFile=..\LICENSE
OutputDir=..\installer
OutputBaseFilename=TBChestCollector-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
UninstallDisplayIcon={app}\tbcollector-config.exe
UninstallDisplayName=Total Battle Chest Collector

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "schedule"; Description: "Run the collector every hour (Windows Task Scheduler)"; GroupDescription: "Scheduling:"; Flags: unchecked

[Dirs]
Name: "{app}\config"
Name: "{app}\execution_logs"

[Files]
Source: "..\dist\tbcollector.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\tbcollector-config.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\change.log"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Configure"; Filename: "{app}\tbcollector-config.exe"; WorkingDir: "{app}"
Name: "{group}\Run now"; Filename: "{app}\tbcollector.exe"; WorkingDir: "{app}"
Name: "{group}\Diagnose"; Filename: "{cmd}"; Parameters: "/k """"{app}\tbcollector.exe"" diagnose"""; WorkingDir: "{app}"
Name: "{group}\Logs folder"; Filename: "{app}\execution_logs"
Name: "{group}\{cm:UninstallProgram,Total Battle Chest Collector}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\TB Chest Collector"; Filename: "{app}\tbcollector-config.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{sys}\schtasks.exe"; Parameters: "/Create /F /SC HOURLY /TN ""TB Chest Collector"" /TR ""\""{app}\tbcollector.exe\"""""; Flags: runhidden; Tasks: schedule
Filename: "{app}\tbcollector-config.exe"; Description: "Open the configuration"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /F /TN ""TB Chest Collector"""; Flags: runhidden; RunOnceId: "DeleteScheduledTask"

; config\ and execution_logs\ are not removed on uninstall: they hold the accounts,
; the databases' credentials and what the modules remember.
