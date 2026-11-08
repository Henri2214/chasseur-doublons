"""Gestionnaire de modèles : .part + renommage, vérification de taille, extraction zip, licence obligatoire.
Serveur HTTP local (aucun accès Internet)."""
import http.server
import os
import threading
import zipfile

import pytest

from core import models
from core.db import Database


@pytest.fixture
def server(tmp_path):
    srv_dir = tmp_path / "srv"
    srv_dir.mkdir()
    (srv_dir / "a.onnx").write_bytes(b"x" * 1000)
    with zipfile.ZipFile(srv_dir / "pack.zip", "w") as z:
        z.writestr("pack/m1.onnx", b"1" * 300)
        z.writestr("pack/m2.onnx", b"2" * 500)
        z.writestr("pack/inutile.onnx", b"3" * 50)

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(srv_dir), **k)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d/" % httpd.server_address[1], srv_dir
    httpd.shutdown()


def test_telechargement_et_verification(tmp_path, server, monkeypatch):
    url, srv = server
    reg = {
        "simple": {"label": "s", "files": {"a.onnx": 1000}, "urls": {"a.onnx": url + "a.onnx"}, "license": "MIT"},
        "faux": {"label": "f", "files": {"b.onnx": 999}, "urls": {"b.onnx": url + "a.onnx"}, "license": "MIT"},
        "zip": {"label": "z", "files": {"m1.onnx": 300, "m2.onnx": 500}, "zip": url + "pack.zip",
                "license": "recherche", "confirm": True},
    }
    monkeypatch.setattr(models, "REGISTRY", reg)
    db = Database(str(tmp_path / "d.sqlite3"))
    mm = models.ModelManager(str(tmp_path / "models"), db)
    assert not mm.available("simple")
    mm.download("simple", background=False)
    assert mm.progress["simple"]["status"] == "done" and mm.available("simple")
    assert not os.path.exists(mm.path("a.onnx") + ".part")
    # taille incorrecte : refusé, pas de fichier final ni .part
    mm.download("faux", background=False)
    assert mm.progress["faux"]["status"] == "error" and "taille" in mm.progress["faux"]["error"]
    assert not os.path.exists(mm.path("b.onnx")) and not os.path.exists(mm.path("b.onnx") + ".part")
    # licence non confirmée : refus
    with pytest.raises(PermissionError):
        mm.download("zip", background=False)
    mm.confirm_license("zip")
    mm.download("zip", background=False)
    assert mm.available("zip")
    assert not os.path.exists(mm.path("inutile.onnx"))          # seuls les fichiers utiles sont extraits
    assert not os.path.exists(mm.path("zip.zip"))                # archive supprimée
    st = mm.status()
    assert st["zip"]["license_ok"] and st["simple"]["available"]
    mm.delete("simple")
    assert not mm.available("simple")


def test_modele_absent_message_clair(tmp_path):
    mm = models.ModelManager(str(tmp_path / "vide"))
    with pytest.raises(FileNotFoundError) as e:
        mm.session("yolox_s", "yolox_s.onnx")
    assert "Réglages" in str(e.value)
