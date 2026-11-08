#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "Premier lancement : création de l'environnement Python..."
  python3 -m venv .venv || { echo "python3 (3.10+) et le module venv sont nécessaires."; exit 1; }
  . .venv/bin/activate
  python -m pip install --upgrade pip
  pip install -r requirements.txt || { echo "Échec de l'installation des dépendances."; exit 1; }
  pip install -r requirements-optional.txt || echo "Modules optionnels non installés (OCR / CLIP indisponibles)."
else
  . .venv/bin/activate
fi
python app.py "$@"
