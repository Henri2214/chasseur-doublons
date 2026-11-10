"""Formats HEIC/AVIF/RAW, « Tout sélectionner » (identifiants complets) et recherche de personne par visage."""
import io
import os

import numpy as np
import pytest

from conftest import photo_like, save_jpeg
from core import imaging, persons
from core.config import IMG_EXT, RAW_EXT
from core.db import f16
from core.web import create_app
from test_api import client, post, wait_scan  # noqa: F401


def test_extensions_etendues():
    for e in (".heic", ".heif", ".hif", ".avif", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf", ".tiff"):
        assert e in IMG_EXT
    assert RAW_EXT <= IMG_EXT


@pytest.mark.skipif(not imaging.HEIF, reason="pillow-heif absent")
@pytest.mark.parametrize("fmt,ext", [("HEIF", ".heic"), ("AVIF", ".avif")])
def test_heic_avif_decode_et_signature(tmp_path, fmt, ext):
    im = photo_like(5, 800, 600)
    p = tmp_path / ("img" + ext)
    try:
        im.save(p, fmt, quality=90)
    except Exception as e:  # encodeur absent dans cette build
        pytest.skip("encodeur %s indisponible : %s" % (fmt, e))
    d = imaging.decode(str(p))
    assert (d.w, d.h) == (800, 600)
    # la signature visuelle d'un HEIC doit rester proche de celle du même contenu en JPEG
    ref = tmp_path / "ref.jpg"
    save_jpeg(im, ref, 95)
    h1, h2 = imaging.phash(d.sig), imaging.phash(imaging.decode(str(ref)).sig)
    assert bin(h1 ^ h2).count("1") <= 6


def test_heic_sans_plugin_message_clair(tmp_path, monkeypatch):
    monkeypatch.setattr(imaging, "HEIF", False)
    p = tmp_path / "x.heic"
    p.write_bytes(b"not an image")
    with pytest.raises(RuntimeError) as e:
        imaging.decode(str(p))
    assert "pillow-heif" in str(e.value)


@pytest.mark.skipif(not imaging.RAW, reason="rawpy absent")
def test_raw_sans_fichier_valide_leve_erreur(tmp_path):
    p = tmp_path / "x.cr2"
    p.write_bytes(b"pas un raw")
    with pytest.raises(Exception):
        imaging.decode(str(p))


@pytest.mark.skipif(not imaging.HEIF, reason="pillow-heif absent")
def test_scan_heic_et_conversion_pour_le_navigateur(client, state, tmp_path):
    root = tmp_path / "ph"
    root.mkdir()
    im = photo_like(7, 640, 480)
    try:
        im.save(root / "a.heic", "HEIF", quality=90)
    except Exception:
        pytest.skip("encodeur HEIF indisponible")
    save_jpeg(im, root / "a_copie.jpg", 92)
    assert post(client, state, "/api/scan", {"path": str(root), "options": {"faces": False}}).status_code == 200
    s = wait_scan(client)
    assert s["status"] == "done", s
    fid = state.db.one("SELECT id FROM files WHERE name='a.heic'")["id"]
    r = client.get("/api/file?id=%d" % fid)
    assert r.status_code == 200 and r.mimetype == "image/jpeg"          # converti, pas du HEIC brut
    r = client.get("/api/thumb?id=%d" % fid)
    assert r.status_code == 200 and r.mimetype == "image/jpeg"
    # le HEIC et son équivalent JPEG sont reconnus comme doublons visuels
    post(client, state, "/api/dupes", {"options": {"rigor": "normal", "exif": False}})
    g = client.get("/api/groups").get_json()
    assert any(len(x["files"]) == 2 for x in g["groups"])


def test_tout_selectionner_doublons_et_explorer(client, state, photos):
    post(client, state, "/api/scan", {"path": str(photos)})
    assert wait_scan(client)["status"] == "done"
    post(client, state, "/api/dupes", {"options": {"rigor": "normal"}})
    g = client.get("/api/group_ids").get_json()["groups"]
    n = sum(len(x["members"]) for x in g)
    assert g and n >= 2
    assert client.get("/api/group_ids?kind=video").get_json()["groups"] == []
    ids = client.get("/api/search_ids").get_json()
    assert ids["total"] == 4 == len(ids["ids"])            # toutes les photos, pas une page
    ids = client.get("/api/search_ids?q=base").get_json()
    assert ids["total"] >= 1


# ------------------------------------------------------------------ recherche de personne
class FakeEngine:
    """Visage = vecteur dérivé de la graine ; retourne toujours le même visage pour une même image."""
    def analyse(self, bgr):
        h, w = bgr.shape[:2]
        v = np.random.default_rng(int(bgr[:8, :8].mean() * 1000) % 7).standard_normal(512).astype("float32")
        return [{"box": (w * .3, h * .3, w * .7, h * .7), "kps": None, "score": 0.99, "eyes": None, "blur": 100.0,
                 "emb": v / np.linalg.norm(v)}]


def _vec(seed, noise=0.0):
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(512).astype("float32")
    v /= np.linalg.norm(v)
    if noise:
        v = v + noise * rng.standard_normal(512).astype("float32") / np.sqrt(512)
        v /= np.linalg.norm(v)
    return v


@pytest.fixture
def indexed(client, state, photos):
    post(client, state, "/api/scan", {"path": str(photos), "options": {"faces": False}})
    assert wait_scan(client)["status"] == "done"
    files = [r["id"] for r in state.db.q("SELECT id FROM files ORDER BY id")]
    A, B = _vec(1), _vec(2)
    # fichiers 0,1,2 : personne A (variations) ; fichier 3 : personne B
    vecs = [A, _vec(1, 0.4), _vec(1, 0.6), B]
    for fid, v in zip(files, vecs):
        state.db.x("INSERT INTO faces(file_id, engine, emb, x1, y1, x2, y2, px, score, blur, eyes, eligible) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,1)", (fid, "opencv", f16(v), .2, .2, .6, .6, 120, .99, 90., None))
    return files, vecs


def test_recherche_par_personne_et_selection_totale(client, state, indexed):
    files, vecs = indexed
    persons.recluster(state.db, "opencv")
    pid = state.db.one("SELECT person_id FROM faces WHERE file_id=?", (files[0],))["person_id"]
    assert pid is not None
    r = post(client, state, "/api/psearch/run", {"person": pid, "thr": 0.4}).get_json()
    got = {f["id"] for f in r["files"]}
    assert files[0] in got and files[3] not in got
    assert all(f["sim"] >= 0.4 for f in r["files"])
    ids = post(client, state, "/api/psearch/run", {"person": pid, "thr": 0.4, "ids_only": True}).get_json()
    assert set(ids["ids"]) == got


def test_recherche_par_visage_indexe_et_seuil(client, state, indexed):
    files, vecs = indexed
    fid = state.db.one("SELECT id FROM faces WHERE file_id=?", (files[0],))["id"]
    strict = post(client, state, "/api/psearch/run", {"face": fid, "thr": 0.95}).get_json()
    loose = post(client, state, "/api/psearch/run", {"face": fid, "thr": 0.3}).get_json()
    assert [f["id"] for f in strict["files"]] == [files[0]]
    assert len(loose["files"]) >= len(strict["files"]) and files[3] not in {f["id"] for f in loose["files"]}


def test_photo_de_reference_envoyee(client, state, indexed):
    state._face_engines["opencv"] = FakeEngine()
    buf = io.BytesIO()
    photo_like(1, 400, 300).save(buf, "JPEG")
    buf.seek(0)
    r = client.post("/api/psearch/detect", data={"photo": (buf, "moi.jpg"), "engine": "opencv"},
                    headers={"X-Token": state.token}, content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    j = r.get_json()
    assert j["faces"] and j["faces"][0]["thumb"].startswith("data:image/jpeg")
    r = post(client, state, "/api/psearch/run", {"ref": j["ref"], "idx": 0, "thr": -1.0}).get_json()
    assert r["total"] == 4                                   # seuil minimal : tous les visages indexés
    assert post(client, state, "/api/psearch/run", {"ref": "inconnu", "idx": 0}).status_code == 400


def test_detect_refuse_format_et_sans_jeton(client, state):
    buf = io.BytesIO(b"x")
    assert client.post("/api/psearch/detect", data={"photo": (buf, "a.txt")}, content_type="multipart/form-data").status_code == 403
    r = client.post("/api/psearch/detect", data={"photo": (io.BytesIO(b"x"), "a.txt")}, headers={"X-Token": state.token},
                    content_type="multipart/form-data")
    assert r.status_code == 400


def test_affecter_les_visages_trouves(client, state, indexed):
    files, vecs = indexed
    fid = state.db.one("SELECT id FROM faces WHERE file_id=?", (files[0],))["id"]
    r = post(client, state, "/api/psearch/assign", {"face": fid, "thr": 0.3, "new_name": "Alice"}).get_json()
    assert r["assigned"] == 3
    rows = state.db.q("SELECT person_id, locked FROM faces WHERE file_id IN (?,?,?)", files[:3])
    assert {x["person_id"] for x in rows} == {r["person"]} and all(x["locked"] for x in rows)
    assert state.db.one("SELECT name FROM persons WHERE id=?", (r["person"],))["name"] == "Alice"
    other = state.db.one("SELECT person_id FROM faces WHERE file_id=?", (files[3],))["person_id"]
    assert other != r["person"]


def test_hors_perimetre_exclu(client, state, indexed):
    files, vecs = indexed
    state.scope.reset([os.path.join(os.path.dirname(state.data_dir), "ailleurs")])
    fid = state.db.one("SELECT id FROM faces WHERE file_id=?", (files[0],))["id"]
    assert post(client, state, "/api/psearch/run", {"face": fid, "thr": 0.0}).get_json()["total"] == 0


# ------------------------------------------------------------------ v2.3 : pagination, modèles, étalonnage
def test_pagination_jusqu_a_1000(client, state, photos):
    post(client, state, "/api/scan", {"path": str(photos), "options": {"faces": False}})
    assert wait_scan(client)["status"] == "done"
    r = client.get("/api/search?per=500").get_json()
    assert r["per"] == 500 and r["total"] == 4
    assert client.get("/api/search?per=99999").get_json()["per"] == 1000
    post(client, state, "/api/dupes", {"options": {"rigor": "normal"}})
    assert client.get("/api/groups?per=200").get_json()["per"] == 200


def test_reglage_telechargement_auto(client, state):
    assert client.get("/api/caps").get_json()["caps"]["auto_models"] is True
    assert post(client, state, "/api/models/auto", {"enabled": False}).get_json()["auto_models"] is False
    assert client.get("/api/caps").get_json()["caps"]["auto_models"] is False
    r = post(client, state, "/api/models/auto", {"enabled": True}).get_json()
    assert r["auto_models"] is True and isinstance(r["started"], list)


def test_etalonnage_reste_dans_le_perimetre(client, state, photos, tmp_path):
    """Les photos d'une analyse précédente (autre dossier) ne doivent plus être proposées : vignettes 404."""
    autre = tmp_path / "ailleurs"
    autre.mkdir()
    from conftest import photo_like, save_jpeg
    im = photo_like(21)
    save_jpeg(im, autre / "a.jpg", 95)
    save_jpeg(im, autre / "b.jpg", 94)
    post(client, state, "/api/scan", {"path": str(autre), "options": {"faces": False}})
    assert wait_scan(client)["status"] == "done"
    post(client, state, "/api/scan", {"path": str(photos), "options": {"faces": False}})
    assert wait_scan(client)["status"] == "done"
    dehors = {r["id"] for r in state.db.q("SELECT id FROM files WHERE path LIKE ?", (str(autre) + "%",))}
    for _ in range(6):
        p = client.get("/api/calib/next?kind=dup").get_json()["pair"]
        if not p:
            break
        assert p["a"] not in dehors and p["b"] not in dehors
        assert client.get("/api/thumb?id=%d" % p["a"]).mimetype == "image/jpeg"
        post(client, state, "/api/calib/answer",
             {"kind": "dup", "a": p["a"], "b": p["b"], "metrics": p["metrics"], "yes": True})
