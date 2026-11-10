<#
    Sauvegarde .bak du dépôt, à lancer AVANT toute grosse modification.

    Exemples :
        .\tools\sauvegarde.ps1
        .\tools\sauvegarde.ps1 -Etiquette "avant-recherche-video"
        .\tools\sauvegarde.ps1 -Garder 20

    Crée C:\Github\_sauvegardes\<depot>\<depot>_v<version>_<date>[_<etiquette>].bak.zip
    (hors du dépôt, donc jamais commité) et ne garde que les N archives les plus récentes.
    Les dossiers régénérables (.venv, data, models, caches, .git) ne sont pas sauvegardés :
    l'historique du code est déjà dans Git, cette archive sert de filet pour le travail en cours.
#>
[CmdletBinding()]
param(
    [string]$Depot       = (Split-Path -Parent $PSScriptRoot),
    [string]$Destination = "C:\Github\_sauvegardes",
    [string]$Etiquette   = "",
    [int]   $Garder      = 10
)

$ErrorActionPreference = "Stop"
$Depot = (Resolve-Path $Depot).Path
$nom   = Split-Path $Depot -Leaf
$dest  = Join-Path $Destination $nom
New-Item -ItemType Directory -Force -Path $dest | Out-Null

# version lue dans core\__init__.py quand elle existe
$version = ""
$init = Join-Path $Depot "core\__init__.py"
if (Test-Path $init) {
    $m = Select-String -Path $init -Pattern '__version__\s*=\s*"([^"]+)"'
    if ($m) { $version = "_v" + $m.Matches[0].Groups[1].Value }
}
$suffixe = if ($Etiquette) { "_" + ($Etiquette -replace '[^\w\-]', '-') } else { "" }
$zip = Join-Path $dest ("{0}{1}_{2}{3}.bak.zip" -f $nom, $version, (Get-Date -Format "yyyyMMdd-HHmm"), $suffixe)

# copie temporaire sans les dossiers régénérables, puis compression
$tmp = Join-Path $env:TEMP ("bak_" + [guid]::NewGuid().ToString("N"))
$exclus = @(".venv", "data", "models", "__pycache__", ".pytest_cache", ".git", "miniatures")
robocopy $Depot $tmp /E /NFL /NDL /NJH /NJS /NP /XD $exclus | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Copie impossible (robocopy $LASTEXITCODE)" }
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $tmp "*") -DestinationPath $zip -CompressionLevel Optimal
Remove-Item -Recurse -Force $tmp

$taille = [math]::Round((Get-Item $zip).Length / 1KB)
Write-Host "Sauvegarde : $zip ($taille Ko)" -ForegroundColor Green

# ménage : on ne garde que les N archives les plus récentes
$vieilles = Get-ChildItem $dest -Filter "*.bak.zip" | Sort-Object LastWriteTime -Descending | Select-Object -Skip $Garder
foreach ($v in $vieilles) { Remove-Item $v.FullName -Force; Write-Host "Ancienne archive supprimee : $($v.Name)" -ForegroundColor DarkGray }

Write-Host ""
Write-Host "Etapes suivantes :" -ForegroundColor Cyan
Write-Host "  git pull"
Write-Host "  git switch -c feature/<nom-de-la-modification>"
Write-Host "  ... modifications, puis .venv\Scripts\python.exe -m pytest -q tests"
