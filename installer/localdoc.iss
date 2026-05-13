; Inno Setup script for LocalDoc Intelligence
; Build prerequisites:
;   1) Run `python installer/build.py` to produce dist/LocalDocIntelligence/
;   2) Open this file in Inno Setup Compiler (https://jrsoftware.org/isinfo.php)
;   3) Build → output in installer/Output/

#define MyAppName      "LocalDoc Intelligence"
#define MyAppShort     "LocalDocIntelligence"
#define MyAppVersion   "0.1.2"
#define MyAppPublisher "Varous 555"
#define MyAppURL       "https://github.com/varous555/localdoc-intelligence"
#define MyAppExeName   "LocalDocIntelligence.exe"

[Setup]
AppId={{8E8BFA90-7C95-4C28-9A3A-1F2A4A5C9F11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppShort}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=LocalDocIntelligenceSetup-{#MyAppVersion}
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
Source: "..\dist\LocalDocIntelligence\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
