"""Décodage unique des images, EXIF, empreintes visuelles (pHash, 48 px, 128 px), netteté, vidéos."""
import calendar
import time

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from .config import DECODE_MAX, SIG_MAX
from .util import lp

try:
    import cv2
except ImportError:  # vidéos et netteté indisponibles
    cv2 = None

try:  # HEIC/HEIF (photos iPhone) si pillow-heif est installé
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIF = True
except Exception:
    HEIF = False

Image.MAX_IMAGE_PIXELS = 300_000_000   # protège contre les images démesurées (bombes de décompression)


def _dct_matrix(n=32):
    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    m = np.cos(np.pi * (2 * i + 1) * k / (2 * n)) * np.sqrt(2.0 / n)
    m[0] /= np.sqrt(2)
    return m


DCT = _dct_matrix()


class Decoded:
    """Image décodée une seule fois, partagée par toutes les étapes."""
    __slots__ = ("rgb", "w", "h", "taken", "cam", "has_cam", "fmt", "_sig")

    def __init__(self, rgb, w, h, taken, cam, has_cam, fmt):
        self.rgb, self.w, self.h = rgb, w, h
        self.taken, self.cam, self.has_cam, self.fmt = taken, cam, has_cam, fmt
        self._sig = None

    @property
    def sig(self):
        """Image réduite (256 px) pour les empreintes visuelles."""
        if self._sig is None:
            im = self.rgb.copy()
            im.thumbnail((SIG_MAX, SIG_MAX))
            self._sig = im
        return self._sig

    def bgr(self):
        return np.ascontiguousarray(np.asarray(self.rgb)[:, :, ::-1])


def exif_info(im):
    """(date de prise de vue en secondes, appareil, appareil présent). Aucune date de repli."""
    try:
        ex = im.getexif()
        sub = ex.get_ifd(0x8769)
        dt = sub.get(36867) or sub.get(36868) or ex.get(306)
        taken = None
        if isinstance(dt, bytes):
            dt = dt.decode("ascii", "ignore")
        if isinstance(dt, str) and dt.strip() and not dt.startswith("0000"):
            try:
                taken = float(calendar.timegm(time.strptime(dt.strip()[:19], "%Y:%m:%d %H:%M:%S")))
                ss = str(sub.get(37521) or "").strip()
                if ss.isdigit():
                    taken += float("0." + ss)
            except ValueError:
                taken = None
        make, model = str(ex.get(271) or "").strip("\x00 "), str(ex.get(272) or "").strip("\x00 ")
        cam = ("%s %s" % (make, model)).strip().lower()
        return taken, cam, bool(cam)
    except Exception:
        return None, "", False


def flatten(im):
    if im.mode in ("RGBA", "LA", "P", "PA"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        return bg
    return im.convert("RGB")


def decode(path, maxside=DECODE_MAX):
    """Ouvre l'image UNE fois (draft JPEG, rotation EXIF) et lit l'EXIF. Lève une exception si illisible."""
    with Image.open(lp(path)) as im:
        fmt = im.format or ""
        w, h = im.size
        taken, cam, has_cam = exif_info(im)
        try:
            orient = im.getexif().get(274, 1)
        except Exception:
            orient = 1
        if orient in (5, 6, 7, 8):
            w, h = h, w
        try:
            im.draft("RGB", (maxside, maxside))
        except Exception:
            pass
        im = ImageOps.exif_transpose(im)
        im.thumbnail((maxside, maxside))
        rgb = flatten(im)
    return Decoded(rgb, w, h, taken, cam, has_cam, fmt)


def load_small(path, kind, maxside, frac=0.2):
    """Miniature (image ou image clé de vidéo), ou None."""
    try:
        if kind == "video":
            frames, _ = video_frames(path, (frac,), maxside)
            return frames[0] if frames else None
        d = decode(path, maxside)
        return d.rgb
    except Exception:
        return None


# ------------------------------------------------------------------ empreintes
def phash(im):
    """Hash perceptuel 64 bits (DCT 32x32 -> 8x8 basses fréquences)."""
    g = im.convert("L").resize((32, 32), Image.LANCZOS)
    d = DCT @ np.asarray(g, dtype=np.float64) @ DCT.T
    low = d[:8, :8].flatten()
    bits = low > np.median(low[1:])
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def fine_features(im):
    """Image grise 48x48 + grille de couleurs moyennes 4x4."""
    gray = np.asarray(im.convert("L").resize((48, 48), Image.LANCZOS), dtype=np.uint8)
    colg = np.asarray(im.convert("RGB").resize((4, 4), Image.BOX), dtype=np.uint8)
    return gray, colg


def detail_features(im):
    """Image grise 128x128 floutée (uint8) pour la comparaison fine bloc par bloc."""
    g = im.convert("L").resize((128, 128), Image.LANCZOS).filter(ImageFilter.GaussianBlur(1))
    return np.asarray(g, dtype=np.uint8)


def sharpness(gray_u8):
    """Variance du Laplacien (netteté). gray_u8 : tableau 2D uint8."""
    if cv2 is not None:
        return float(cv2.Laplacian(gray_u8, cv2.CV_64F).var())
    g = gray_u8.astype(np.float64)
    lap = -4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:]
    return float(lap.var())


def global_blur(dec):
    """Netteté globale mesurée sur l'image ramenée à 512 px (indépendante de la résolution)."""
    im = dec.rgb.convert("L")
    im.thumbnail((512, 512))
    return sharpness(np.asarray(im, dtype=np.uint8))


# ------------------------------------------------------------------ vidéos
def video_frames(path, fracs, maxside=256, should_stop=None):
    if cv2 is None:
        return [], {}
    cap = cv2.VideoCapture(lp(path))
    if not cap.isOpened():
        return [], {}
    try:
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        info = {"n": n, "w": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
                "h": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
                "dur": (n / fps) if fps > 0 and n > 0 else 0}
        frames = []
        for f in fracs:
            if should_stop and should_stop():
                break
            if n > 0:
                cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(n * f)))
            ok, fr = cap.read()
            if not ok:
                continue
            hh, ww = fr.shape[:2]
            s = maxside / max(hh, ww)
            if s < 1:
                fr = cv2.resize(fr, (int(ww * s), int(hh * s)), interpolation=cv2.INTER_AREA)
            frames.append(Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)))
        return frames, info
    finally:
        cap.release()


def video_signature(path, vn, should_stop=None):
    """Signature vidéo : pHash de vn images clés + couleurs moyennes. None si non fiable."""
    frames, info = video_frames(path, [(i + 0.5) / vn for i in range(vn)], should_stop=should_stop)
    sig = col = None
    # fiable seulement si le nombre d'images est connu et toutes les images clés lues
    if info.get("n", 0) >= vn * 2 and len(frames) == vn:
        sig = np.array([phash(x) for x in frames], dtype=np.uint64)
        col = np.stack([np.asarray(x, dtype=np.float32).reshape(-1, 3).mean(0) for x in frames]).astype(np.float32)
    return info, sig, col
