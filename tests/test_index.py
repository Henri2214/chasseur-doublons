"""Index persistant : analyse incrémentale, purge, doublons exacts et visuels depuis l'index."""
import os
import time

from conftest import photo_like, save_jpeg
from core import dupes


def groups_by_name(state, root, rigor="strict"):
    g = dupes.compute(state.db, [str(root)], {"rigor": rigor, "images": True, "videos": True})
    names = []
    for grp in g:
        ids = [m["id"] for m in grp["members"]]
        rows = state.db.q("SELECT name FROM files WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
        names.append((grp["kind"], sorted(r["name"] for r in rows)))
    return g, names


def test_analyse_incrementale(state, photos):
    sc = state.scanner
    st1 = sc.scan_sync(str(photos))
    assert st1["new"] == 4 and st1["to_analyse"] == 4
    t = time.time()
    st2 = sc.scan_sync(str(photos))
    elapsed = time.time() - t
    assert st2["new"] == 0 and st2["changed"] == 0 and st2["unchanged"] == 4
    assert st2.get("to_analyse", 0) == 0                 # rien n'est réanalysé
    assert elapsed < 2.0
    # modification d'un fichier -> seul ce fichier est réanalysé
    p = photos / "autre.jpg"
    save_jpeg(photo_like(99), p, 80)
    os.utime(p, (time.time() + 5, time.time() + 5))
    st3 = sc.scan_sync(str(photos))
    assert st3["changed"] == 1 and st3["to_analyse"] == 1
    # suppression -> purgé de l'index
    os.remove(photos / "reduite.jpg")
    st4 = sc.scan_sync(str(photos))
    assert st4["removed"] == 1
    assert state.db.one("SELECT COUNT(*) n FROM files")["n"] == 3


def test_doublons_depuis_index(state, photos):
    state.scanner.scan_sync(str(photos))
    g, names = groups_by_name(state, photos)
    # base + copie identique (exact) + réduite (visuel) forment UN groupe visuel autour d'une référence
    vis = [n for k, n in names if k == "visuel"]
    assert vis == [["base.jpg", "copie_identique.jpg", "reduite.jpg"]]
    grp = [x for x in g if x["kind"] == "visuel"][0]
    refs = [m for m in grp["members"] if m["ref"]]
    assert len(refs) == 1 and all(m["score"] is not None for m in grp["members"])
    assert not grp["nodate"]
    # changer de rigueur ne relance pas l'analyse (recalcul depuis l'index uniquement)
    n_before = state.db.one("SELECT COUNT(*) n FROM scans")["n"]
    groups_by_name(state, photos, "tres_strict")
    assert state.db.one("SELECT COUNT(*) n FROM scans")["n"] == n_before


def test_date_absente_signalee(state, tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    base = photo_like(5)
    save_jpeg(base, root / "a.jpg", 95)                               # pas d'EXIF
    save_jpeg(base.resize((800, 600)), root / "b.jpg", 85)
    state.scanner.scan_sync(str(root))
    g, _ = groups_by_name(state, root)
    assert len(g) == 1 and g[0]["nodate"]


def test_dates_exif_differentes_rejetees(state, tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    base = photo_like(6)
    save_jpeg(base, root / "a.jpg", 95, "2026:01:01 10:00:00")
    save_jpeg(base, root / "b.jpg", 94, "2026:01:01 10:00:05")       # même image, autre instant (rafale)
    state.scanner.scan_sync(str(root))
    g, _ = groups_by_name(state, root)
    assert g == []


def test_fichier_illisible_rapporte(state, tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "casse.jpg").write_bytes(b"pas une image" * 100)
    st = state.scanner.scan_sync(str(root))
    assert any("casse.jpg" in s["path"] for s in st["skipped"])
    assert state.db.one("SELECT status FROM files")["status"] == "error"


def test_annulation_immediate(state, tmp_path):
    """L'annulation interrompt un hash en cours et laisse l'index cohérent (rien de partiel enregistré)."""
    import pytest
    from core.scanner import Cancelled, hash_file
    big = tmp_path / "gros.bin"
    big.write_bytes(b"\0" * (8 << 20))
    with pytest.raises(Cancelled):
        hash_file(str(big), should_stop=lambda: True)
    root = tmp_path / "p"
    root.mkdir()
    for i in range(40):
        save_jpeg(photo_like(100 + i, 640, 480), root / ("i%02d.jpg" % i), 85)
    sc = state.scanner
    sc.cancel = False
    t = sc.start(str(root), {})
    sc.cancel = True
    t.join(30)
    assert not t.is_alive()
    assert sc.state["status"] in ("cancelled", "done")
    # une nouvelle analyse termine le travail normalement
    st = sc.scan_sync(str(root))
    assert state.db.one("SELECT COUNT(*) n FROM files WHERE done_visual=1")["n"] == 40
    assert st["files"] == 40


def test_tout_reanalyser_et_resume(state, photos):
    """2e analyse : tout est repris de l'index ; avec force=True, tout est recalculé."""
    s1 = state.scanner.scan_sync(str(photos), {"faces": False})
    assert s1["new"] == 4 and s1["to_analyse"] == 4
    s2 = state.scanner.scan_sync(str(photos), {"faces": False})
    assert s2["new"] == 0 and s2["unchanged"] == 4 and s2["to_analyse"] == 0
    s3 = state.scanner.scan_sync(str(photos), {"faces": False, "force": True})
    assert s3["forced"] == 4 and s3["to_analyse"] == 4
    assert state.db.one("SELECT COUNT(*) n FROM files WHERE phash IS NOT NULL")["n"] == 4


def test_gpu_en_echec_bascule_sur_le_processeur(state, tmp_path, monkeypatch):
    """Un GPU qui plante pendant l'essai (processus séparé) ne doit jamais faire tomber l'application."""
    import os
    import pytest
    md = os.environ.get("CD_MODELS_DIR", "")
    from core.models import ModelManager
    mm = ModelManager(md, state.db) if md else None
    if not mm or not mm.available("yolox_s"):
        pytest.skip("modèle yolox_s absent (CD_MODELS_DIR)")
    monkeypatch.setattr(mm, "gpu_providers", lambda: ["FournisseurInexistant"])
    s = mm.session("yolox_s", "yolox_s.onnx")
    assert s.get_providers()[0] == "CPUExecutionProvider" and "GPU écarté" in mm.gpu_note
    assert state.db.one("SELECT COUNT(*) n FROM settings WHERE k LIKE 'gpu_probe:%'")["n"] == 1
