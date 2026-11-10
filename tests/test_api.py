"""API Flask : jeton, en-tête Host, analyse, doublons paginés, miniatures, actions."""
import time

import pytest

from core.web import create_app


@pytest.fixture
def client(state):
    app = create_app(state)
    app.testing = True
    c = app.test_client()
    c.environ_base["HTTP_HOST"] = "127.0.0.1:8765"
    return c


def post(c, state, url, data, token=True):
    h = {"X-Token": state.token} if token else {}
    return c.post(url, json=data, headers=h)


def wait_scan(c):
    for _ in range(600):
        s = c.get("/api/status").get_json()
        if s["status"] != "running":
            return s
        time.sleep(0.05)
    raise TimeoutError


def test_jeton_obligatoire(client, state, photos):
    assert post(client, state, "/api/scan", {"path": str(photos)}, token=False).status_code == 403
    assert post(client, state, "/api/delete", {"ids": [1]}, token=False).status_code == 403


def test_host_controle(client):
    r = client.get("/api/status", headers={"Host": "attaquant.example:8765"})
    assert r.status_code == 403


def test_parcours_complet(client, state, photos, tmp_path):
    assert "__TOKEN__" not in client.get("/").get_data(as_text=True)
    r = post(client, state, "/api/scan", {"path": str(photos), "options": {"images": True}})
    assert r.status_code == 200
    s = wait_scan(client)
    assert s["status"] == "done", s
    r = post(client, state, "/api/dupes", {"options": {"rigor": "strict"}})
    assert r.get_json()["groups"] == 1
    g = client.get("/api/groups?page=1&per=10").get_json()
    assert g["total"] == 1 and len(g["groups"][0]["files"]) == 3
    assert sum(1 for f in g["groups"][0]["files"] if f["ref"]) == 1
    fid = g["groups"][0]["files"][0]["id"]
    t = client.get("/api/thumb?id=%d&s=128" % fid)
    assert t.status_code == 200 and t.mimetype == "image/jpeg"
    assert client.get("/api/thumb?id=99999").status_code == 404
    # rapport et performances
    rep = client.get("/api/report").get_json()
    assert "stages" in rep["stats"]
    # déplacement puis annulation via l'API
    r = post(client, state, "/api/move", {"ids": [fid], "dest": str(tmp_path / "dest")}).get_json()
    assert r["moved"] == 1
    g2 = client.get("/api/groups").get_json()
    assert all(f["id"] != fid for grp in g2["groups"] for f in grp["files"])
    u = post(client, state, "/api/undo", {"journal": [r["moves"][0]["journal"]]}).get_json()
    assert u["undone"] == 1
    j = client.get("/api/journal").get_json()
    assert [e["action"] for e in j["entries"]][:2] == ["undo", "move"]
    # recherche sans modèle : par nom de fichier
    res = client.get("/api/search?q=reduite").get_json()
    assert any(f["name"] == "reduite.jpg" for f in res["files"])


def test_effacer_biometrie(client, state):
    assert post(client, state, "/api/biometrics/clear", {}).status_code == 400
    assert post(client, state, "/api/biometrics/clear", {"confirm": "EFFACER"}).status_code == 200
