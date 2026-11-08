@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Premier lancement : creation de l'environnement Python...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo Python 3.10+ introuvable. Installez-le depuis https://www.python.org/downloads/ ^(cocher "Add to PATH"^).
    pause
    exit /b 1
  )
  call .venv\Scripts\activate.bat
  python -m pip install --upgrade pip
  pip install -r requirements.txt || (echo Echec de l'installation des dependances. & pause & exit /b 1)
  pip install -r requirements-optional.txt || echo Modules optionnels non installes ^(OCR / CLIP indisponibles^).
) else (
  call .venv\Scripts\activate.bat
)
python app.py %*
pause
