"""Tests avec les VRAIS modèles (ignorés automatiquement s'ils sont absents).

Variables d'environnement :
  CD_MODELS_DIR   dossier des modèles (même structure que models/)
  CD_TEST_IMAGES  dossier contenant personne_a.jpg et personne_b.jpg : deux portraits de deux personnes
                  différentes (aucune photo n'est fournie dans le dépôt)
"""
import os

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from core.models import ModelManager

MD = os.environ.get("CD_MODELS_DIR", "")
IMGS = os.environ.get("CD_TEST_IMAGES", "")
mm = ModelManager(MD) if MD else None


def need(name):
    if not mm or not mm.available(name):
        pytest.skip("modèle %s absent (CD_MODELS_DIR)" % name)


def portrait(name):
    p = os.path.join(IMGS, name)
    if not IMGS or not os.path.isfile(p):
        pytest.skip("portrait %s absent (CD_TEST_IMAGES)" % name)
    import cv2
    return cv2.imread(p)


@pytest.mark.parametrize("engine,model", [("insightface", "insightface"), ("opencv", "opencv_faces")])
def test_visages(engine, model):
    need(model)
    from core.faces import make_engine
    a, b = portrait("personne_a.jpg"), portrait("personne_b.jpg")
    e = make_engine(mm, engine, 640)
    fa = e.analyse(a)
    assert len(fa) >= 1, "aucun visage détecté sur le portrait"
    h, w = a.shape[:2]
    import cv2
    crop1 = a[0:int(h * 0.92), 0:int(w * 0.9)]
    crop2 = cv2.resize(a[int(h * 0.06):, int(w * 0.08):], None, fx=0.7, fy=0.7)
    e1, e2 = e.analyse(crop1), e.analyse(crop2)
    assert e1 and e2
    same = float(e1[0]["emb"] @ e2[0]["emb"])
    fb = e.analyse(b)
    assert fb
    diff = max(float(fa[0]["emb"] @ x["emb"]) for x in fb)
    print(engine, "même visage recadré : %.3f  personnes différentes : %.3f" % (same, diff))
    from core.faces import default_threshold
    thr = default_threshold(engine)
    assert same >= thr + 0.25 and diff < thr - 0.1


def test_yeux_points_de_repere():
    need("insightface")
    from core.faces import make_engine
    e = make_engine(mm, "insightface", 640)
    f = max(e.analyse(portrait("personne_a.jpg")), key=lambda x: x["box"][2] - x["box"][0])
    assert f["eyes"] is not None and 0.2 < f["eyes"] < 0.8      # portrait aux yeux ouverts


def test_yolox_detecte_une_personne():
    need("yolox_s")
    from core.objects import YoloX
    res = YoloX(mm, "yolox_s").detect(portrait("personne_a.jpg"))
    assert any(lab == "personne" and s > 0.5 for lab, s, _ in res), res


def test_ocr_capture_ecran():
    pytest.importorskip("rapidocr_onnxruntime")
    from core.ocr import OCR
    im = Image.new("RGB", (900, 260), "white")
    d = ImageDraw.Draw(im)
    try:
        fnt = ImageFont.truetype("DejaVuSans.ttf", 40)
    except OSError:
        try:
            fnt = ImageFont.truetype("arial.ttf", 40)
        except OSError:
            fnt = ImageFont.load_default(40)
    d.text((20, 40), "Facture 2026-118", fill="black", font=fnt)
    d.text((20, 140), "Total 149,90 EUR", fill="black", font=fnt)
    txt = OCR().read(np.asarray(im)[:, :, ::-1].copy())
    assert "2026-118" in txt and "149,90" in txt, txt


def test_clip_couleurs_et_scenes():
    need("clip")
    pytest.importorskip("tokenizers")
    from core.clip import Clip
    c = Clip(mm)
    red = Image.new("RGB", (256, 256), (220, 20, 20))
    blue = Image.new("RGB", (256, 256), (20, 40, 220))
    E = c.image([red, blue])
    T = c.text(["a plain red square", "a plain blue square"])
    S = E @ T.T
    assert S[0, 0] > S[0, 1] and S[1, 1] > S[1, 0], S
    assert abs(float(np.linalg.norm(E[0])) - 1) < 1e-3
