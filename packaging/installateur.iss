; Installateur Windows de Chasseur de doublons (Inno Setup 6).
; Construit par tools\construire.ps1 :  ISCC /DVersion=2.9.0 packaging\installateur.iss
; Produit dist\ChasseurDoublons-v<version>-Setup.exe : UN seul fichier, qui contient le programme, toutes ses
; bibliothèques et les modèles d'IA à licence libre. Installation sans droits d'administrateur, dans le
; profil de l'utilisateur ; raccourcis dans le menu Démarrer et sur le Bureau ; désinstallation propre.

#ifndef Version
  #define Version "0.0.0"
#endif

[Setup]
AppId={{6B0E3B5C-52A4-4E0B-9C1D-7A2F4D8E91C3}
AppName=Chasseur de doublons
AppVersion={#Version}
AppVerName=Chasseur de doublons {#Version}
AppPublisher=Henri Jausions
AppPublisherURL=https://github.com/Henri2214/chasseur-doublons
DefaultDirName={autopf}\ChasseurDoublons
DefaultGroupName=Chasseur de doublons
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=ChasseurDoublons-v{#Version}-Setup
Compression=lzma2/normal
SolidCompression=no
WizardStyle=modern
UninstallDisplayName=Chasseur de doublons
UninstallDisplayIcon={app}\ChasseurDoublons.exe
CloseApplications=no
#if FileExists(SourcePath + "\icone.ico")
SetupIconFile=icone.ico
#endif

[Languages]
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "bureau"; Description: "Créer un raccourci sur le Bureau"; GroupDescription: "Raccourcis :"

[InstallDelete]
; mise à jour : on repart de bibliothèques propres (les données de l'utilisateur ne sont pas ici)
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\ChasseurDoublons\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion; Excludes: "data\*,*.log"

[Icons]
Name: "{group}\Chasseur de doublons"; Filename: "{app}\ChasseurDoublons.exe"; WorkingDir: "{app}"
Name: "{group}\Chasseur de doublons (diagnostic)"; Filename: "{app}\ChasseurDoublons-diagnostic.exe"; WorkingDir: "{app}"
Name: "{group}\Désinstaller Chasseur de doublons"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Chasseur de doublons"; Filename: "{app}\ChasseurDoublons.exe"; WorkingDir: "{app}"; Tasks: bureau

[Run]
Filename: "{app}\ChasseurDoublons.exe"; Description: "Lancer Chasseur de doublons"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM ChasseurDoublons.exe"; Flags: runhidden; RunOnceId: "ArretApp"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM ChasseurDoublons-diagnostic.exe"; Flags: runhidden; RunOnceId: "ArretDiag"

[Messages]
fr.FinishedLabel=Chasseur de doublons est installé. L'application s'ouvre dans votre navigateur ; pour l'arrêter, utilisez le bouton « Quitter » de la page.

[Code]
// Une version déjà en route verrouille ses fichiers : on l'arrête avant de copier.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  r: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM ChasseurDoublons.exe', '', SW_HIDE, ewWaitUntilTerminated, r);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM ChasseurDoublons-diagnostic.exe', '', SW_HIDE, ewWaitUntilTerminated, r);
  Sleep(800);
  Result := '';
end;
