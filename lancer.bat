@echo off
setlocal
cd /d "%~dp0"

rem ---- environnement Python dedie (.venv) -------------------------------------
if not exist ".venv\Scripts\python.exe" (
  echo Premier lancement : creation de l'environnement Python...
  py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul || python -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo Python 3.10+ introuvable. Installez-le depuis https://www.python.org/downloads/ ^(cocher "Add to PATH"^).
    pause
    exit /b 1
  )
)
call .venv\Scripts\activate.bat

rem ---- dependances de base (verifiees a chaque lancement, pas seulement a la creation) ----
python -c "import flask, PIL, numpy, cv2, onnxruntime, pillow_heif" 2>nul
if errorlevel 1 (
  echo Installation des dependances...
  python -m pip install --upgrade pip
  pip install -r requirements.txt || (echo Echec de l'installation des dependances. & pause & exit /b 1)
)

rem ---- modules optionnels : un par un, un echec n'empeche pas les autres ------
if not exist ".venv\optionnels.txt" (
  echo Modules optionnels ^(OCR, CLIP, RAW, tests^)...
  for /f "usebackq eol=# delims=" %%p in ("requirements-optional.txt") do (
    pip install "%%p" || echo   -- %%p indisponible pour cette version de Python ^(ignore^)
  )
  echo installes> ".venv\optionnels.txt"
)

python app.py %*
pause
