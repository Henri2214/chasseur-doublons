#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1

# ---- environnement Python dédié (.venv) -------------------------------------
if [ ! -x .venv/bin/python ]; then
  echo "Premier lancement : création de l'environnement Python..."
  python3 -m venv .venv || { echo "python3 (3.10+) et le module venv sont nécessaires."; exit 1; }
fi
. .venv/bin/activate

# ---- dépendances de base (vérifiées à chaque lancement) ---------------------
if ! python -c "import flask, PIL, numpy, cv2, onnxruntime, pillow_heif" 2>/dev/null; then
  echo "Installation des dépendances..."
  python -m pip install --upgrade pip
  pip install -r requirements.txt || { echo "Échec de l'installation des dépendances."; exit 1; }
fi

# ---- modules optionnels : un par un, un échec n'empêche pas les autres ------
if [ ! -f .venv/optionnels.txt ]; then
  echo "Modules optionnels (CLIP, RAW, tests)..."
  grep -v '^[[:space:]]*#' requirements-optional.txt | grep -v '^[[:space:]]*$' | while read -r p; do
    pip install "$p" || echo "  -- $p indisponible pour cette version de Python (ignoré)"
  done
  echo installes > .venv/optionnels.txt
fi

# ---- accélération GPU : CUDA si une carte NVIDIA est présente (sinon on reste sur le processeur) ----
if [ ! -f .venv/gpu.txt ]; then
  if [ "$1" != "--cpu" ] && command -v nvidia-smi >/dev/null 2>&1 \
     && ! python -c "import onnxruntime,sys; sys.exit(0 if 'CUDAExecutionProvider' in onnxruntime.get_available_providers() else 1)" 2>/dev/null; then
    echo "Activation du GPU (onnxruntime-gpu)..."
    pip uninstall -y onnxruntime onnxruntime-gpu >/dev/null 2>&1
    pip install onnxruntime-gpu || pip install onnxruntime
  fi
  echo fait > .venv/gpu.txt
fi

python app.py "$@"
