"""Texte dans les images (OCR) avec rapidocr-onnxruntime (modèles inclus dans le paquet).

Modes : "aucun", "auto" (captures et documents : PNG, absence d'EXIF d'appareil, noms contenant capture /
screenshot / scan), "toutes".
"""
import re
import threading

_AUTO_NAME = re.compile(r"(capture|screenshot|screen[ _-]?shot|scan|cam[ _-]?scanner|document|re[çc]u|facture)",
                        re.I)


def wanted(mode, name, fmt, has_cam):
    if mode == "toutes":
        return True
    if mode != "auto":
        return False
    return bool(_AUTO_NAME.search(name)) or (fmt or "").upper() == "PNG" or not has_cam


class OCR:
    _lock = threading.Lock()

    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR   # ImportError si absent : la fonction est grisée
        self.engine = RapidOCR()

    def read(self, bgr, min_score=0.5):
        with self._lock:          # rapidocr n'est pas garanti thread-safe ; limite aussi la mémoire
            res, _ = self.engine(bgr)
        if not res:
            return ""
        lines = [t for _, t, s in res if float(s) >= min_score and t.strip()]
        return "\n".join(lines)


def available():
    try:
        import importlib.util
        return importlib.util.find_spec("rapidocr_onnxruntime") is not None
    except Exception:
        return False
