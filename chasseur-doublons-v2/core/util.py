"""Utilitaires système : chemins longs Windows, fichiers verrouillés, OneDrive, périmètre autorisé."""
import errno
import os
import threading

import numpy as np

from .config import IMG_EXT, VID_EXT

IS_WIN = os.name == "nt"

# Attributs Windows (st_file_attributes)
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000


def lp(path):
    r"""Chemin utilisable par l'OS. Sous Windows, préfixe \\?\ au-delà de 240 caractères (limite MAX_PATH 260)."""
    if not IS_WIN or not path:
        return path
    p = os.path.abspath(path)
    if len(p) < 240 or p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def unlp(path):
    """Retire le préfixe \\\\?\\ pour l'affichage et la base."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def norm(p):
    return os.path.normcase(os.path.realpath(unlp(p)))


def kind_of(name):
    ext = os.path.splitext(name)[1].lower()
    return "image" if ext in IMG_EXT else "video" if ext in VID_EXT else "other"


def is_cloud_placeholder(st):
    """Fichier OneDrive / cloud non téléchargé localement (le lire déclencherait un téléchargement)."""
    attrs = getattr(st, "st_file_attributes", 0) or 0
    return bool(attrs & (_FILE_ATTRIBUTE_OFFLINE | _FILE_ATTRIBUTE_RECALL_ON_OPEN |
                         _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS))


def explain_oserror(e):
    """Message clair en français pour une erreur d'accès fichier."""
    win = getattr(e, "winerror", None)
    if win in (32, 33):
        return "fichier verrouillé (ouvert par un autre programme)"
    if win == 206 or getattr(e, "errno", None) == errno.ENAMETOOLONG:
        return "chemin trop long"
    if win == 362 or win == 389:
        return "fichier OneDrive non disponible hors ligne"
    if isinstance(e, PermissionError):
        return "accès refusé"
    if isinstance(e, FileNotFoundError):
        return "fichier introuvable"
    return "%s: %s" % (type(e).__name__, e)


def popcount64(a):
    """Nombre de bits à 1 d'un tableau uint64 (vectorisé)."""
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(a)
    lut = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
    b = np.ascontiguousarray(a).view(np.uint8).reshape(a.shape + (8,))
    return lut[b].sum(axis=-1, dtype=np.uint8)


def to_u64(v):
    """Entier signé SQLite -> uint64."""
    return np.uint64(v & 0xFFFFFFFFFFFFFFFF)


def to_i64(v):
    """uint64 -> entier signé pour SQLite."""
    v = int(v)
    return v - (1 << 64) if v >= (1 << 63) else v


class Scope:
    """Périmètre des dossiers autorisés pour les actions. Remis à zéro à chaque session (nouvelle analyse)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.roots = set()
        self.dests = set()

    def reset(self, roots=()):
        with self._lock:
            self.roots = {norm(r) for r in roots}
            self.dests = set()

    def add_dest(self, d):
        with self._lock:
            self.dests.add(norm(d))

    def contains(self, p):
        rp = norm(p)
        with self._lock:
            allr = self.roots | self.dests
        return any(rp == r or rp.startswith(r.rstrip(os.sep) + os.sep) for r in allr)

    def near(self, p):
        """Dossier consultable dans le bandeau : dans, au-dessus ou à côté d'un dossier analysé."""
        def chk(q):
            rq = norm(q)
            with self._lock:
                allr = self.roots | self.dests
            return any(rq == r or rq.startswith(r.rstrip(os.sep) + os.sep) or
                       r.startswith(rq.rstrip(os.sep) + os.sep) for r in allr)
        return chk(p) or chk(os.path.dirname(os.path.abspath(p)))
