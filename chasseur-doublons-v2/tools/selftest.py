#!/usr/bin/env python3
"""Autotest à lancer sur votre PC : vérifie chaque module et affiche OK / ÉCHEC / ABSENT avec la cause.

  python tools/selftest.py                       # tests sans photo
  python tools/selftest.py --portrait moi.jpg    # + détection/reconnaissance de visage et objets sur VOTRE photo
  python tools/selftest.py --portrait a.jpg --autre b.jpg   # + deux personnes différentes

Aucune donnée n'est envoyée sur le réseau ; rien n'est écrit en dehors d'un dossier temporaire.
"""
import argparse
import importlib
import os
import sys
import tempfile
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RESULTS = []


def report(name, status, detail=""):
    RESULTS.append((name, status))
    mark = {"OK": "OK     ", "ECHEC": "ÉCHEC  ", "ABSENT": "ABSENT ", "INFO": "INFO   "}[status]
    print("[%s] %-34s %s" % (mark, name, detail))


def check(name, fn):
    t = time.perf_counter()
    try:
        r = fn()
        dt = (time.perf_counter() - t) * 1000
        if r is None:
            return
        status, detail = r
        report(name, status, detail + ("  (%.0f ms)" % dt if status == "OK" else ""))
    except FileNotFoundError as e:
        report(name, "ABSENT", str(e))
    except Exception as e:  # noqa
        report(name, "ECHEC", "%s: %s" % (type(e).__name__, e))
        if os.environ.get("CD_DEBUG"):
            traceback.print_exc()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--portrait", help="photo d'un visage (la vôtre), pour tester visages et objets")
    ap.add_argument("--autre", help="photo d'une AUTRE personne, pour tester la distinction")
    ap.add_argument("--models", help="dossier des modèles (défaut : models/)")
    a = ap.parse_args()

    print("Chasseur de doublons - autotest\nPython %s sur %s\n" % (sys.version.split()[0], sys.platform))

    # ---- dépendances
    for mod, label, required in (("flask", "Flask", True), ("PIL", "Pillow", True), ("numpy", "numpy", True),
                                 ("cv2", "OpenCV (vidéos, visages)", True), ("onnxruntime", "onnxruntime", False),
                                 ("pillow_heif", "pillow-heif (HEIC)", False), ("tokenizers", "tokenizers (CLIP)", False),
                                 ("rapidocr_onnxruntime", "rapidocr (OCR)", False)):
        try:
            importlib.import_module(mod)
            try:
                from importlib.metadata import version
                v = version({"PIL": "pillow", "cv2": "opencv-python-headless", "pillow_heif": "pillow-heif",
                             "rapidocr_onnxruntime": "rapidocr-onnxruntime"}.get(mod, mod))
            except Exception:
                v = ""
            report(label, "OK", v)
        except Exception as e:  # noqa
            report(label, "ECHEC" if required else "ABSENT", str(e))

    from core.models import ModelManager, providers
    from core.config import MODELS_DIR
    mm = ModelManager(a.models or MODELS_DIR)
    provs = providers()
    report("Accélération (GPU)", "OK" if any(p != "CPUExecutionProvider" for p in provs) else "INFO",
           ", ".join(provs) or "onnxruntime absent")

    # ---- base SQLite / FTS5
    def t_db():
        from core.db import Database
        with tempfile.TemporaryDirectory() as d:
            db = Database(os.path.join(d, "t.sqlite3"))
            ok = db.fts
            db.conn().close()
        return "OK", "WAL, recherche plein texte %s" % ("FTS5" if ok else "indisponible (repli LIKE)")
    check("Base SQLite", t_db)

    # ---- pipeline visuel (images synthétiques)
    def t_visual():
        import numpy as np
        from PIL import Image
        from core import imaging
        from core.dupes import metrics_ok, pair_metrics
        from core.config import RIGOR
        rng = np.random.default_rng(0)
        im = Image.fromarray(rng.integers(0, 255, (48, 64, 3), dtype=np.uint8)).resize((800, 600), Image.BICUBIC)
        f = []
        for x in (im, im.resize((400, 300))):
            d = imaging.Decoded(x, x.width, x.height, None, "", False, "PNG")
            g, c = imaging.fine_features(d.sig)
            f.append((g, c, imaging.detail_features(d.sig)))
        m = pair_metrics(*f)
        return ("OK" if metrics_ok(m, RIGOR["strict"]) else "ECHEC"), "copie réduite reconnue (b2=%.3f)" % m["b2"]
    check("Empreintes visuelles", t_visual)

    def t_video():
        import cv2
        import numpy as np
        from core.imaging import video_signature
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.avi")
            w = cv2.VideoWriter(p, cv2.VideoWriter_fourcc(*"MJPG"), 10, (160, 120))
            for i in range(60):
                fr = np.full((120, 160, 3), i * 4, np.uint8)
                cv2.rectangle(fr, (i, 20), (i + 40, 80), (0, 0, 255), -1)
                w.write(fr)
            w.release()
            info, sig, _ = video_signature(p, 12)
        return ("OK" if sig is not None else "ECHEC"), "%d images, %.1f s" % (info.get("n", 0), info.get("dur", 0))
    check("Vidéos (OpenCV)", t_video)

    # ---- visages
    portrait = None
    if a.portrait:
        import cv2
        portrait = cv2.imread(a.portrait)
        if portrait is None:
            report("Portrait", "ECHEC", "image illisible : %s" % a.portrait)
    autre = None
    if a.autre:
        import cv2
        autre = cv2.imread(a.autre)

    for engine, model in (("insightface", "insightface"), ("opencv", "opencv_faces")):
        def t_face(engine=engine, model=model):
            if not mm.available(model):
                raise FileNotFoundError("modèle non téléchargé (onglet Réglages)")
            import numpy as np
            from core.faces import default_threshold, make_engine
            thr = default_threshold(engine)
            e = make_engine(mm, engine, 640)
            if portrait is None:
                e.analyse(np.zeros((480, 640, 3), np.uint8))
                return "INFO", "modèle chargé ; relancez avec --portrait photo.jpg pour tester la détection"
            fs = e.analyse(portrait)
            if not fs:
                return "ECHEC", "aucun visage détecté sur le portrait"
            f = max(fs, key=lambda x: x["box"][2] - x["box"][0])
            h, w = portrait.shape[:2]
            import cv2
            g = e.analyse(cv2.resize(portrait[int(h * .05):, int(w * .05):], None, fx=.7, fy=.7))
            same = max(float(f["emb"] @ x["emb"]) for x in g) if g else float("nan")
            msg = "%d visage(s), même visage recadré : cosinus %.2f" % (len(fs), same)
            if f.get("eyes") is not None:
                msg += ", yeux : %.2f" % f["eyes"]
            ok = same >= thr + 0.1          # seuil propre au moteur (ArcFace ~0,45 ; SFace ~0,40)
            if autre is not None:
                o = e.analyse(autre)
                if o:
                    diff = max(float(f["emb"] @ x["emb"]) for x in o)
                    msg += ", autre personne : %.2f" % diff
                    ok = ok and diff < thr
            return ("OK" if ok else "ECHEC"), msg
        check("Visages (%s)" % engine, t_face)

    # ---- objets
    def t_obj():
        if not mm.available("yolox_s"):
            raise FileNotFoundError("modèle YOLOX-S non téléchargé (onglet Réglages)")
        import numpy as np
        from core.objects import YoloX
        y = YoloX(mm, "yolox_s")
        if portrait is None:
            y.detect(np.full((480, 640, 3), 114, np.uint8))
            return "INFO", "modèle chargé ; --portrait pour vérifier qu'une personne est détectée"
        res = y.detect(portrait)
        ok = any(lab == "personne" for lab, _, _ in res)
        return ("OK" if ok else "ECHEC"), ", ".join("%s %.2f" % (l, s) for l, s, _ in res[:5]) or "rien détecté"
    check("Objets (YOLOX)", t_obj)

    # ---- OCR
    def t_ocr():
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont
        from core.ocr import OCR
        im = Image.new("RGB", (900, 260), "white")
        d = ImageDraw.Draw(im)
        fnt = None
        for name in ("arial.ttf", "DejaVuSans.ttf", "Arial.ttf"):
            try:
                fnt = ImageFont.truetype(name, 40)
                break
            except OSError:
                pass
        fnt = fnt or ImageFont.load_default()
        d.text((20, 40), "Facture 2026-118", fill="black", font=fnt)
        d.text((20, 140), "Total 149,90 EUR", fill="black", font=fnt)
        txt = OCR().read(np.asarray(im)[:, :, ::-1].copy())
        ok = "2026-118" in txt and "149" in txt
        return ("OK" if ok else "ECHEC"), "lu : %r" % txt.replace("\n", " | ")
    check("Texte (OCR)", t_ocr)

    # ---- CLIP
    def t_clip():
        if not mm.available("clip"):
            raise FileNotFoundError("modèle CLIP non téléchargé (onglet Réglages)")
        from PIL import Image
        from core.clip import Clip
        c = Clip(mm)
        E = c.image([Image.new("RGB", (256, 256), (220, 20, 20)), Image.new("RGB", (256, 256), (20, 40, 220))])
        T = c.text(["a plain red square", "a plain blue square"])
        S = E @ T.T
        ok = S[0, 0] > S[0, 1] and S[1, 1] > S[1, 0]
        return ("OK" if ok else "ECHEC"), "rouge/bleu distingués (%.2f/%.2f)" % (S[0, 0], S[1, 1])
    check("CLIP (recherche, scènes)", t_clip)

    n_fail = sum(1 for _, s in RESULTS if s == "ECHEC")
    print("\nRésumé : %d OK, %d ÉCHEC, %d ABSENT" % (sum(1 for _, s in RESULTS if s == "OK"), n_fail,
                                                    sum(1 for _, s in RESULTS if s == "ABSENT")))
    print("Performances complètes : python tools/bench.py --n 2000  (images synthétiques)")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
