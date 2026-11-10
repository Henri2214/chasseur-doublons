"""Photos similaires : empreinte visuelle, mêmes personnes, mêmes objets, seuil et sélection totale."""
import numpy as np
import pytest
from PIL import Image

from conftest import photo_like, save_jpeg
from core import similar
from core.db import f16
from test_api import client, post, wait_scan  # noqa: F401


@pytest.fixture
def jeu(client, state, tmp_path):
    """base, recadrage, version plus claire, redimensionnée, et deux photos sans rapport."""
    root = tmp_path / "sim"
    root.mkdir()
    base = photo_like(11, 1024, 768)
    save_jpeg(base, root / "base.jpg", 95, "2026:01:01 10:00:00", ("Canon", "EOS"))
    save_jpeg(base.crop((40, 30, 984, 738)).resize((1024, 768), Image.LANCZOS), root / "recadree.jpg", 90)
    save_jpeg(Image.eval(base, lambda v: min(255, int(v * 1.12))), root / "plus_claire.jpg", 90)
    save_jpeg(base.resize((512, 384), Image.LANCZOS), root / "reduite.jpg", 85)
    save_jpeg(photo_like(12, 1024, 768), root / "autre1.jpg", 90)
    save_jpeg(photo_like(13, 1024, 768), root / "autre2.jpg", 90)
    assert post(client, state, "/api/scan", {"path": str(root), "options": {"faces": False}}).status_code == 200
    assert wait_scan(client)["status"] == "done"
    ids = {r["name"]: r["id"] for r in state.db.q("SELECT id, name FROM files")}
    return ids


def test_similaires_visuel(state, jeu):
    res = similar.similar(state.db, jeu["base.jpg"], thr=0.55)
    trouve = {i: s for i, s, _ in res}
    assert jeu["base.jpg"] not in trouve                       # jamais la photo de départ
    for n in ("recadree.jpg", "plus_claire.jpg", "reduite.jpg"):
        assert jeu[n] in trouve, (n, res)
    assert jeu["autre1.jpg"] not in trouve and jeu["autre2.jpg"] not in trouve
    assert all("visuel" in w for _, _, w in res)
    # ordre : la copie réduite (quasi identique) passe devant le recadrage
    assert trouve[jeu["reduite.jpg"]] > trouve[jeu["recadree.jpg"]]
    assert [s for _, s, _ in res] == sorted([s for _, s, _ in res], reverse=True)


def test_seuil(state, jeu):
    large = similar.similar(state.db, jeu["base.jpg"], thr=0.30)
    etroit = similar.similar(state.db, jeu["base.jpg"], thr=0.90)
    assert len(large) >= len(etroit)
    assert all(s >= 0.90 for _, s, _ in etroit)


def test_personnes_et_objets(state, jeu):
    """Deux photos sans ressemblance visuelle ressortent si elles partagent une personne ou des objets."""
    a, b = jeu["autre1.jpg"], jeu["autre2.jpg"]
    pid = state.db.x("INSERT INTO persons(name, engine, created) VALUES ('Alice','opencv',0)").lastrowid
    v = np.ones(512, np.float32) / np.sqrt(512)
    for fid in (a, b):
        state.db.x("INSERT INTO faces(file_id, engine, emb, x1, y1, x2, y2, px, score, blur, eyes, eligible, person_id)"
                   " VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)", (fid, "opencv", f16(v), .2, .2, .6, .6, 120, .9, 90., None, pid))
    res = {i: w for i, s, w in similar.similar(state.db, a, thr=0.55)}
    assert b in res and "personne" in res[b]
    # sans la source « personne », b disparaît
    assert b not in {i for i, _, _ in similar.similar(state.db, a, thr=0.55, sources=["visuel"])}

    for fid in (a, b):
        for lab in ("chien", "ballon", "arbre"):
            state.db.x("INSERT INTO objects(file_id, label, score, x1, y1, x2, y2) VALUES (?,?,?,0,0,1,1)",
                       (fid, lab, 0.9))
    res = {i: w for i, s, w in similar.similar(state.db, a, thr=0.55, sources=["objet"])}
    assert b in res and res[b] == ["objet"]


def test_api_similar(client, state, jeu):
    r = client.get("/api/similar?id=%d&thr=0.55" % jeu["base.jpg"]).get_json()
    assert r["ref"]["name"] == "base.jpg" and r["total"] >= 3
    f = r["files"][0]
    assert 0 < f["sim"] <= 1 and f["why"] and f["id"] != jeu["base.jpg"]
    ids = client.get("/api/similar_ids?id=%d&thr=0.55" % jeu["base.jpg"]).get_json()
    assert ids["total"] == r["total"] == len(ids["ids"])
    assert client.get("/api/similar?id=999999").status_code == 400


def test_api_similar_hors_perimetre(client, state, jeu, tmp_path):
    state.scope.reset([str(tmp_path / "ailleurs")])
    assert client.get("/api/similar?id=%d&thr=0.3" % jeu["base.jpg"]).get_json()["total"] == 0
