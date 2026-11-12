<#
    Construit l'application autonome Windows (aucun Python requis sur le PC de destination).

    Trois résultats dans dist\ :
      ChasseurDoublons-v<version>-Setup.exe      INSTALLATEUR, un seul fichier : programme + bibliothèques + modèles.
                                                 C'est le fichier à donner. (Nécessite Inno Setup 6 pour être construit.)
      ChasseurDoublons-v<version>-portable.exe   un seul fichier utilisable sans installation, posé n'importe où
                                                 (démarrage plus lent ; modèles téléchargés au premier lancement).
      ChasseurDoublons\                          le dossier complet. ATTENTION : ChasseurDoublons.exe ne fonctionne
                                                 pas sorti de ce dossier (il a besoin de _internal\).

        .\tools\construire.ps1                       # modèles déjà présents dans models\ mis dans l'installateur
        .\tools\construire.ps1 -TelechargerModeles   # télécharge d'abord les modèles conseillés
        .\tools\construire.ps1 -SansModeles          # installateur léger : modèles téléchargés au premier lancement
        .\tools\construire.ps1 -Cpu                  # sans accélération GPU DirectML
        .\tools\construire.ps1 -Zip                  # ajoute l'archive .zip du dossier complet

    Inno Setup 6 (gratuit) :  winget install JRSoftware.InnoSetup

    Les modèles InsightFace (licence « recherche non commerciale ») ne sont JAMAIS mis dans le paquet :
    l'utilisateur les télécharge lui-même dans Réglages après avoir accepté la licence.
#>
[CmdletBinding()]
param([switch]$TelechargerModeles, [switch]$SansModeles, [switch]$Cpu, [switch]$Zip, [string]$Python = "")

$ErrorActionPreference = "Stop"
$racine = Split-Path -Parent $PSScriptRoot
Set-Location $racine

function Executer([string]$quoi, [scriptblock]$bloc) {
    Write-Host "== $quoi" -ForegroundColor Cyan
    & $bloc
    if ($LASTEXITCODE -ne 0) { throw "Echec : $quoi (code $LASTEXITCODE)" }
}

# ---- environnement de construction séparé (Python 3.12 : l'OCR n'existe pas pour les versions suivantes)
$venv = Join-Path $racine ".venv-build"
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "== Creation de l'environnement de construction" -ForegroundColor Cyan
    if ($Python) { & $Python -m venv $venv } else { & py -3.12 -m venv $venv }
    if (-not (Test-Path $py)) { throw "Python 3.12 introuvable (installez-le ou passez -Python C:\chemin\python.exe)" }
}
Executer "Mise a jour de pip" { & $py -m pip install --upgrade pip --quiet }
Executer "Dependances de base" { & $py -m pip install -r requirements.txt --quiet }
foreach ($ligne in Get-Content requirements-optional.txt) {
    $p = $ligne.Trim()
    if ($p -and -not $p.StartsWith("#") -and $p -notmatch "^pytest") {
        & $py -m pip install $p --quiet
        if ($LASTEXITCODE -ne 0) { Write-Host "   (optionnel non installe : $p)" -ForegroundColor DarkYellow }
    }
}
if (-not $Cpu) {
    Write-Host "== Acceleration GPU DirectML" -ForegroundColor Cyan
    & $py -m pip uninstall -y onnxruntime onnxruntime-directml --quiet | Out-Null
    & $py -m pip install onnxruntime-directml --quiet
    if ($LASTEXITCODE -ne 0) { Write-Host "   DirectML indisponible : paquet construit pour le processeur" -ForegroundColor DarkYellow; & $py -m pip install onnxruntime --quiet }
}
Executer "PyInstaller" { & $py -m pip install pyinstaller --quiet }

# ---- construction
$version = (Select-String -Path core\__init__.py -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Executer "Construction de l'executable (version $version)" {
    & $py -m PyInstaller --noconfirm --distpath dist --workpath build packaging\chasseur.spec
}
$app = Join-Path $racine "dist\ChasseurDoublons"
if (-not (Test-Path (Join-Path $app "ChasseurDoublons.exe"))) { throw "Executable absent apres la construction" }

# ---- modèles à licence libre, copiés à côté de l'exécutable
if (-not $SansModeles) {
    if ($TelechargerModeles) {
        Executer "Telechargement des modeles conseilles" {
            & $py -c "from core.models import ModelManager, RECOMMENDED; mm = ModelManager('models'); [mm.download(n, background=False) for n in RECOMMENDED if not mm.available(n)]; import sys; sys.exit(0 if all(mm.available(n) for n in RECOMMENDED) else 1)"
        }
    }
    if (Test-Path models) {
        $interdits = @("det_10g.onnx", "w600k_r50.onnx", "2d106det.onnx", "1k3d68.onnx", "genderage.onnx")   # InsightFace
        $dest = Join-Path $app "models"
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        robocopy models $dest /E /NFL /NDL /NJH /NJS /NP /XF $interdits "*.part" "*.zip" | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "Copie des modeles impossible" }
        $global:LASTEXITCODE = 0      # robocopy renvoie 1 quand il a copié des fichiers : ce n'est pas une erreur
        Write-Host "   modeles copies : $((Get-ChildItem $dest -Recurse -File | Measure-Object Length -Sum).Sum / 1MB -as [int]) Mo (InsightFace exclu)"
    } else {
        Write-Host "   pas de dossier models\ : les modeles seront telecharges au premier lancement" -ForegroundColor DarkYellow
    }
}

# ---- notice
@"
Chasseur de doublons $version
=============================

IMPORTANT : ChasseurDoublons.exe ne fonctionne que DANS ce dossier (il a besoin du dossier _internal).
Pour un fichier unique, utilisez l'installateur ChasseurDoublons-v$version-Setup.exe ou la version
ChasseurDoublons-v$version-portable.exe.

Lancement : double-clic sur ChasseurDoublons.exe. Aucune fenetre ne s'ouvre : la page s'affiche
directement dans votre navigateur (http://127.0.0.1:8765).

Pour arreter : bouton "Quitter" en haut a droite de la page. Si vous fermez seulement l'onglet,
l'application s'arrete seule au bout de 10 minutes (jamais pendant une analyse). Un second
double-clic rouvre simplement la page.

En cas de probleme : lancez ChasseurDoublons-diagnostic.exe (meme programme, avec sa fenetre de
messages) ou lisez journal.log dans le dossier des donnees.

Tout reste sur ce PC : aucune photo n'est envoyee sur Internet.

Donnees (index, miniatures, journaux, modeles telecharges) : %LOCALAPPDATA%\ChasseurDoublons
Mode portable (cle USB) : creez un dossier "data" a cote de l'executable, tout y restera.

Modeles InsightFace (visages plus precis) : non fournis, licence "recherche non commerciale".
A telecharger dans l'onglet Reglages apres acceptation de la licence.

Options : ChasseurDoublons.exe --port 8765 --no-browser --data D:\Donnees --models D:\Modeles --sans-modeles
"@ | Set-Content -Encoding UTF8 (Join-Path $app "LISEZMOI.txt")

$produits = @()

# ---- version en un seul fichier
$brut = Join-Path $racine "dist\ChasseurDoublons-portable.exe"
if (Test-Path $brut) {
    $portable = Join-Path $racine "dist\ChasseurDoublons-v$version-portable.exe"
    Move-Item -Force $brut $portable
    $produits += $portable
}

# ---- installateur (Inno Setup 6)
$iscc = $null
$cmd = Get-Command iscc.exe -ErrorAction SilentlyContinue
if ($cmd) { $iscc = $cmd.Source }
foreach ($c in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
    if (-not $iscc -and $c -and (Test-Path $c)) { $iscc = $c }
}
if ($iscc) {
    Executer "Installateur (Inno Setup)" { & $iscc "/DVersion=$version" "/Qp" (Join-Path $racine "packaging\installateur.iss") }
    $produits += (Join-Path $racine "dist\ChasseurDoublons-v$version-Setup.exe")
} else {
    Write-Host "== Installateur NON construit : Inno Setup 6 est absent." -ForegroundColor Yellow
    Write-Host "   Installez-le (winget install JRSoftware.InnoSetup) puis relancez ce script." -ForegroundColor Yellow
}

# ---- archive du dossier complet (sur demande)
if ($Zip) {
    $zipf = Join-Path $racine "dist\ChasseurDoublons-v$version-win64.zip"
    if (Test-Path $zipf) { Remove-Item $zipf -Force }
    Write-Host "== Archive" -ForegroundColor Cyan
    Compress-Archive -Path $app -DestinationPath $zipf -CompressionLevel Optimal
    $produits += $zipf
}

Write-Host ""
Write-Host "Termine :" -ForegroundColor Green
foreach ($f in $produits) { if (Test-Path $f) { Write-Host ("  {0}  ({1} Mo)" -f $f, [math]::Round((Get-Item $f).Length / 1MB)) -ForegroundColor Green } }
Write-Host "Dossier complet : dist\ChasseurDoublons\  (l'exe n'y fonctionne qu'avec son dossier _internal)"
exit 0
