<#
    Crée un raccourci « Chasseur de doublons » sur le Bureau et dans le menu Démarrer.

        .\tools\raccourci.ps1

    Le raccourci lance l'application sans aucune fenêtre noire : la page s'ouvre directement dans le
    navigateur. Il vise l'exécutable s'il a été construit (dist\ChasseurDoublons), sinon l'environnement
    Python du dépôt (.venv, créé au premier lancement de lancer.bat).
#>
$ErrorActionPreference = "Stop"
$racine = Split-Path -Parent $PSScriptRoot
$exe    = Join-Path $racine "dist\ChasseurDoublons\ChasseurDoublons.exe"
$pyw    = Join-Path $racine ".venv\Scripts\pythonw.exe"
if (Test-Path $exe)      { $cible = $exe; $arguments = "";       $dossier = Split-Path $exe }
elseif (Test-Path $pyw)  { $cible = $pyw; $arguments = "app.py"; $dossier = $racine }
else { throw "Lancez d'abord lancer.bat une fois (il crée l'environnement Python), puis relancez ce script." }

$shell = New-Object -ComObject WScript.Shell
$emplacements = @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))
foreach ($e in $emplacements) {
    $lnk = $shell.CreateShortcut((Join-Path $e "Chasseur de doublons.lnk"))
    $lnk.TargetPath = $cible
    $lnk.Arguments = $arguments
    $lnk.WorkingDirectory = $dossier
    $lnk.Description = "Chasseur de doublons - doublons de photos, documents et fichiers"
    $icone = Join-Path $racine "packaging\icone.ico"
    if (Test-Path $icone) { $lnk.IconLocation = $icone }
    $lnk.Save()
    Write-Host "Raccourci cree : $($lnk.FullName)" -ForegroundColor Green
}
Write-Host "Pour arreter l'application : bouton Quitter de la page."
