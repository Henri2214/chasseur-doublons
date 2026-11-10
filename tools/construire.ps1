<#
    Construit l'application autonome Windows : dist\ChasseurDoublons\ChasseurDoublons.exe
    et l'archive dist\ChasseurDoublons-v<version>-win64.zip (aucun Python requis sur le PC de destination).

        .\tools\construire.ps1                       # modèles déjà présents dans models\ copiés dans le paquet
        .\tools\construire.ps1 -TelechargerModeles   # télécharge d'abord les modèles conseillés
        .\tools\construire.ps1 -SansModeles          # paquet léger : modèles téléchargés au premier lancement
        .\tools\construire.ps1 -Cpu                  # sans accélération GPU DirectML

    Les modèles InsightFace (licence « recherche non commerciale ») ne sont JAMAIS mis dans le paquet :
    l'utilisateur les télécharge lui-même dans Réglages après avoir accepté la licence.
#>
[CmdletBinding()]
param([switch]$TelechargerModeles, [switch]$SansModeles, [switch]$Cpu, [string]$Python = "")

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
        $LASTEXITCODE = 0
        Write-Host "   modeles copies : $((Get-ChildItem $dest -Recurse -File | Measure-Object Length -Sum).Sum / 1MB -as [int]) Mo (InsightFace exclu)"
    } else {
        Write-Host "   pas de dossier models\ : les modeles seront telecharges au premier lancement" -ForegroundColor DarkYellow
    }
}

# ---- notice et archive
@"
Chasseur de doublons $version - application autonome
====================================================

Lancement : double-clic sur ChasseurDoublons.exe. Une fenetre noire s'ouvre (le moteur) et la page
s'affiche dans votre navigateur (http://127.0.0.1:8765). Fermer la fenetre noire arrete l'application.

Tout reste sur ce PC : aucune photo n'est envoyee sur Internet.

Dossiers crees a cote de l'executable :
  data\    index de vos analyses, miniatures, journal (plantage.log en cas de probleme)
  models\  modeles d'IA (visages OpenCV, objets YOLOX, contenu CLIP)

Si le dossier n'est pas accessible en ecriture (Program Files), ces dossiers sont crees dans
%LOCALAPPDATA%\ChasseurDoublons.

Modeles InsightFace (visages plus precis) : non fournis, licence "recherche non commerciale".
A telecharger dans l'onglet Reglages apres acceptation de la licence.

Options : ChasseurDoublons.exe --port 8765 --no-browser --data D:\Donnees --models D:\Modeles --sans-modeles
"@ | Set-Content -Encoding UTF8 (Join-Path $app "LISEZMOI.txt")

$zip = Join-Path $racine "dist\ChasseurDoublons-v$version-win64.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Write-Host "== Archive" -ForegroundColor Cyan
Compress-Archive -Path $app -DestinationPath $zip -CompressionLevel Optimal
$mo = [math]::Round((Get-Item $zip).Length / 1MB)
Write-Host ""
Write-Host "Termine : $zip ($mo Mo)" -ForegroundColor Green
Write-Host "Essai   : dist\ChasseurDoublons\ChasseurDoublons.exe"
