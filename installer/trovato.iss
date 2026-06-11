; Inno Setup script for Trovato
; Build prerequisites:
;   1) Run `python installer/build.py` to produce dist/Trovato/
;   2) Open this file in Inno Setup Compiler (https://jrsoftware.org/isinfo.php)
;   3) Build → output in installer/Output/

#define MyAppName      "Trovato"
#define MyAppShort     "Trovato"
#define MyAppVersion   "0.7.1"
; Numeric quad for Windows version-info / uninstall comparison. Final releases
; map straight through (0.4.14 -> 0.4.14.0); pre-release bN builds encode the beta
; number in the 4th component (0.4.0b8 -> 0.4.0.8).
#define MyAppVersionInfo "0.7.1.0"
#define MyAppPublisher "Varous 555"
#define MyAppURL       "https://github.com/Various5/trovato"
#define MyAppExeName   "Trovato.exe"

[Setup]
AppId={{6E5E7206-8F7E-4BB8-8558-BD1A098A8EF9}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
VersionInfoVersion={#MyAppVersionInfo}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppShort}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=TrovatoSetup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german";  MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "..\dist\Trovato\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
