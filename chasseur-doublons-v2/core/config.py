"""Constantes et emplacements.

Les données (base SQLite, miniatures) et les modèles sont stockés à côté de l'application par défaut,
dans des dossiers ignorés par git. Variables d'environnement :
  CD_DATA_DIR   : dossier des données (base + caches)
  CD_MODELS_DIR : dossier des modèles ONNX
"""
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CD_DATA_DIR") or (BASE / "data"))
MODELS_DIR = Path(os.environ.get("CD_MODELS_DIR") or (BASE / "models"))
DB_NAME = "index.sqlite3"

IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".heif", ".jfif"}
VID_EXT = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg",
           ".3gp", ".mts", ".m2ts", ".ts"}

# Taille de décodage unique d'une image (côté max). Toutes les étapes partent de cette image.
DECODE_MAX = 1280
# Image réduite pour les empreintes visuelles (identique au pipeline calibré de la v1.1).
SIG_MAX = 256

WORKERS = max(2, min(8, os.cpu_count() or 2))

# Niveaux de rigueur. Le pHash ne sert qu'à trouver des CANDIDATS ; chaque paire est ensuite vérifiée.
#  ph  : distance pHash max pour être candidat     ncc/bmax/col : test 48 px (corrélation, bloc max, couleurs)
#  b2  : test fin 128 px bloc par bloc (copie <= 0,18 ; petite modification locale >= 0,28)
#  date: écart max (s) entre dates EXIF de prise de vue   vph/vdur : vidéos   iou : visages
RIGOR = {
    "tres_strict": dict(ph=4, ncc=0.985, bmax=0.45, col=12, b2=0.22, date=0.001, vph=4, vdur=0.02, iou=0.7),
    "strict":      dict(ph=7, ncc=0.96,  bmax=0.70, col=18, b2=0.28, date=0.5,   vph=5, vdur=0.03, iou=0.6),
    "normal":      dict(ph=8, ncc=0.92,  bmax=1.00, col=26, b2=0.45, date=2.0,   vph=6, vdur=0.03, iou=0.5),
    "large":       dict(ph=12, ncc=0.85, bmax=1.50, col=40, b2=0.80, date=10.0,  vph=9, vdur=0.05, iou=0.4),
}
RIGOR_LABELS = {"tres_strict": "très stricte", "strict": "stricte", "normal": "normale",
                "large": "large", "etalonne": "étalonnée"}

# Visages
FACE_MIN_PX = 40          # visage plus petit (côté, en pixels de l'original) : non assigné
FACE_BLUR_MIN = 25.0      # variance du Laplacien du visage en dessous : trop flou, non assigné
FACE_SAME_COS = 0.5       # aide aux doublons : même personne si cosinus >= 0,5
PERSON_THR = {"insightface": 0.45, "opencv": 0.40}   # seuils de regroupement (à étalonner)
EYE_CLOSED_RATIO = 0.20   # hauteur/largeur des points de l'oeil en dessous : oeil fermé (à étalonner)
BLUR_GLOBAL_MIN = 60.0    # variance du Laplacien globale en dessous : photo floue (à étalonner)

OCR_MODES = ("aucun", "auto", "toutes")
