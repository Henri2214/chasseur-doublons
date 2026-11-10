@echo off
setlocal
cd /d "%~dp0"

rem ---- environnement Python dedie (.venv) -------------------------------------
if not exist ".venv\Scripts\python.exe" (
  echo Premier lancement : creation de l'environnement Python...
  rem Python 3.12 en priorite : l'OCR (rapidocr) n'existe pas encore pour 3.13 / 3.14
  py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul || python -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo Python 3.10+ introuvable. Installez-le depuis https://www.python.org/downloads/ ^(cocher "Add to PATH"^).
    pause
    exit /b 1
  )
)
call .venv\Scripts\activate.bat

rem ---- dependances de base (verifiees a chaque lancement, pas seulement a la creation) ----
python -c "import flask, PIL, numpy, cv2, onnxruntime, pillow_heif, pypdfium2, olefile" 2>nul
if errorlevel 1 (
  echo Installation des dependances...
  python -m pip install --upgrade pip
  pip install -r requirements.txt || (echo Echec de l'installation des dependances. & pause & exit /b 1)
)

rem ---- modules optionnels : un par un, un echec n'empeche pas les autres ------
if not exist ".venv\optionnels.txt" (
  echo Modules optionnels ^(CLIP, RAW, tests^)...
  for /f "usebackq eol=# delims=" %%p in ("requirements-optional.txt") do (
    pip install "%%p" || echo   -- %%p indisponible pour cette version de Python ^(ignore^)
  )
  echo installes> ".venv\optionnels.txt"
)

rem ---- acceleration GPU : DirectML (toute carte DirectX 12 : Intel, AMD, NVIDIA) ----
rem    Desactiver avec :  lancer.bat --cpu       Refaire la detection : supprimer .venv\gpu.txt
if /i "%~1"=="--cpu" (
  echo installes^(cpu force^)> ".venv\gpu.txt"
  shift
)
if not exist ".venv\gpu.txt" (
  python -c "import onnxruntime,sys; sys.exit(0 if any(p in onnxruntime.get_available_providers() for p in ('CUDAExecutionProvider','DmlExecutionProvider')) else 1)" 2>nul
  if errorlevel 1 (
    echo Activation du GPU ^(onnxruntime-directml^)...
    pip uninstall -y onnxruntime onnxruntime-directml >nul 2>&1
    pip install onnxruntime-directml && echo   GPU DirectML actif. || (echo   GPU indisponible : retour au processeur. & pip install onnxruntime)
  )
  echo fait> ".venv\gpu.txt"
)

rem ---- lancement silencieux : aucune fenetre noire ne reste ouverte -----------------
rem    L'application s'arrete avec le bouton "Quitter" de la page (ou seule, 10 min apres
rem    la fermeture de la page). Pour garder la fenetre et voir les messages :
rem        lancer.bat --console
set "CONSOLE="
for %%a in (%*) do if /i "%%~a"=="--console" set "CONSOLE=1"
if not exist ".venv\Scripts\pythonw.exe" set "CONSOLE=1"
if defined CONSOLE (
  python app.py %*
  pause
) else (
  start "" ".venv\Scripts\pythonw.exe" app.py %*
)
