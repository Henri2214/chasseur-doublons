"""pHash + vérification fine : copies = doublons ; modifications locales, décalage, zoom = non-doublons."""
import io

import numpy as np
import pytest
from PIL import Image, ImageEnhance

from conftest import photo_like
from core import imaging
from core.config import RIGOR
from core.dupes import metrics_ok, pair_metrics
from core.util import popcount64


def feats(im):
    dec = imaging.Decoded(im.convert("RGB"), im.width, im.height, None, "", False, "PNG")
    s = dec.sig
    fine, colg = imaging.fine_features(s)
    return imaging.phash(s), (fine, colg, imaging.detail_features(s))


def jpeg(im, q):
    b = io.BytesIO()
    im.save(b, "JPEG", quality=q)
    b.seek(0)
    return Image.open(b).convert("RGB")


def ham(a, b):
    return int(popcount64(np.array([a ^ b], dtype=np.uint64))[0])


BASES = [photo_like(s) for s in (11, 12, 13)]


def copies(im):
    return {
        "redimensionnée": im.resize((im.width // 2, im.height // 2), Image.LANCZOS),
        "jpeg q60": jpeg(im, 60),
        "luminosité +10 %": ImageEnhance.Brightness(im).enhance(1.10),
        "réduite + jpeg": jpeg(im.resize((800, 600), Image.LANCZOS), 75),
    }


def modifications(im, seed):
    rng = np.random.default_rng(seed)
    w, h = im.size
    out = {}
    # zone déplacée : un morceau (personne, objet) change de place
    m = im.copy()
    box = (w // 3, h // 3, w // 3 + w // 6, h // 3 + h // 5)
    patch = im.crop(box)
    m.paste(im.crop((box[0] - w // 8, box[1], box[2] - w // 8, box[3])), box)
    m.paste(patch, (box[0] + w // 8, box[1]))
    out["zone déplacée"] = m
    # zone remplacée par un autre contenu
    m = im.copy()
    m.paste(photo_like(int(rng.integers(1000, 9999)), w // 5, h // 5), (w // 2, h // 2))
    out["zone remplacée"] = m
    # décalage du cadrage (rafale : appareil légèrement bougé)
    out["décalage 4 %"] = im.crop((int(w * 0.04), 0, w, h)).resize((w, h), Image.LANCZOS)
    # zoom
    out["zoom 10 %"] = im.crop((int(w * 0.05), int(h * 0.05), int(w * 0.95), int(h * 0.95))).resize((w, h), Image.LANCZOS)
    return out


@pytest.mark.parametrize("bi", range(len(BASES)))
def test_copies_sont_des_doublons(bi):
    base = BASES[bi]
    ha, fa = feats(base)
    for name, v in copies(base).items():
        hb, fb = feats(v)
        assert ham(ha, hb) <= RIGOR["strict"]["ph"], (name, ham(ha, hb))
        m = pair_metrics(fa, fb)
        assert metrics_ok(m, RIGOR["strict"]), (name, m)
        # v1.1 annonçait <= 0,18 ; une hausse de luminosité qui sature des zones claires monte jusqu'à ~0,25
        assert m["b2"] <= 0.25, (name, m["b2"])


@pytest.mark.parametrize("bi", range(len(BASES)))
def test_modifications_ne_sont_pas_des_doublons(bi):
    base = BASES[bi]
    _, fa = feats(base)
    for name, v in modifications(base, bi).items():
        _, fb = feats(v)
        m = pair_metrics(fa, fb)
        assert not metrics_ok(m, RIGOR["strict"]), (name, m)
        assert not metrics_ok(m, RIGOR["normal"]), (name, m)


def test_photos_differentes_ne_sont_pas_candidates():
    ha, _ = feats(BASES[0])
    hb, _ = feats(BASES[1])
    assert ham(ha, hb) > RIGOR["large"]["ph"]


def test_exif_sans_date_de_repli(tmp_path):
    from conftest import save_jpeg
    p = tmp_path / "sans_date.jpg"
    save_jpeg(BASES[0], p)
    d = imaging.decode(str(p))
    assert d.taken is None          # pas de repli sur la date de modification
    p2 = tmp_path / "avec_date.jpg"
    save_jpeg(BASES[0], p2, exif_date="2026:01:02 03:04:05", cam=("Nikon", "Z6"))
    d2 = imaging.decode(str(p2))
    assert d2.taken == 1767323045.0 and d2.cam == "nikon z6"
