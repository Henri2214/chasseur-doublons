"""Jeu d'images synthétiques « type photo » généré à la volée (aucune photo n'est stockée dans le dépôt)."""
import os
import sys

import numpy as np
import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def photo_like(seed, w=1024, h=768):
    """Image texturée multi-échelle + formes colorées (proche des statistiques d'une photo)."""
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w, 3), np.float32)
    for scale, amp in ((4, 90), (16, 45), (64, 25), (256, 12)):
        small = rng.random((max(2, h * scale // 1024), max(2, w * scale // 1024), 3)).astype(np.float32)
        img += amp * np.asarray(Image.fromarray((small * 255).astype(np.uint8)).resize((w, h), Image.BICUBIC),
                                np.float32) / 255
    img += np.linspace(0, 60, w)[None, :, None] * rng.random(3)
    im = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    for _ in range(12):
        x, y = rng.integers(0, w), rng.integers(0, h)
        r = rng.integers(30, 160)
        col = tuple(int(c) for c in rng.integers(0, 255, 3))
        if rng.random() < 0.5:
            d.ellipse([x - r, y - r, x + r, y + r], fill=col)
        else:
            d.rectangle([x - r, y - r // 2, x + r, y + r // 2], fill=col)
    return im


def save_jpeg(im, path, q=92, exif_date=None, cam=None):
    kw = {}
    if exif_date or cam:
        ex = Image.Exif()
        if cam:
            ex[271], ex[272] = cam
        if exif_date:
            ex.get_ifd(0x8769)[36867] = exif_date
        kw["exif"] = ex.tobytes()
    im.save(path, "JPEG", quality=q, **kw)


@pytest.fixture
def state(tmp_path):
    from core.web import State
    models = os.environ.get("CD_MODELS_DIR") or str(tmp_path / "models")
    return State(data_dir=str(tmp_path / "data"), models_dir=models)


@pytest.fixture
def photos(tmp_path):
    """Dossier : une base + variantes doublons + variantes non-doublons + une autre photo."""
    root = tmp_path / "photos"
    (root / "sous").mkdir(parents=True)
    base = photo_like(1)
    date = "2026:07:14 10:00:00"
    cam = ("Canon", "EOS 90D")
    save_jpeg(base, root / "base.jpg", 95, date, cam)
    save_jpeg(base, root / "sous" / "copie_identique.jpg", 95, date, cam)          # même contenu, mêmes octets ?
    save_jpeg(base.resize((640, 480), Image.LANCZOS), root / "reduite.jpg", 85, date, cam)
    other = photo_like(2)
    save_jpeg(other, root / "autre.jpg", 90, "2026:07:15 11:00:00", cam)
    return root
