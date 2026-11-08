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
