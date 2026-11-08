"""Suppression revérifiée, dernier exemplaire, déplacement/renommage, journal et annulation."""
import os
import time

from core import actions, dupes


def setup(state, photos):
    state.scanner.scan_sync(str(photos))
    state.scope.reset([str(photos)])
    groups = dupes.compute(state.db, [str(photos)], {"rigor": "strict"})
    state.result.set(groups, {})
    return {r["name"]: r["id"] for r in state.db.q("SELECT id, name FROM files")}


def test_suppression_revérifiee(state, photos):
    ids = setup(state, photos)
    p = photos / "reduite.jpg"
    os.utime(p, (time.time() + 100, time.time() + 100))          # le fichier a changé depuis l'analyse
    r = actions.delete(state.db, state.scope, state.result, [ids["reduite.jpg"]])
    assert r["deleted"] == 0 and "changé" in r["errors"][0]["error"]
    assert p.exists()


def test_dernier_exemplaire_protege(state, photos):
    ids = setup(state, photos)
    grp = ["base.jpg", "copie_identique.jpg", "reduite.jpg"]
    r = actions.delete(state.db, state.scope, state.result, [ids[n] for n in grp])
    assert r.get("needs_confirm") and r["deleted"] == 0
    assert all((photos / n).exists() or (photos / "sous" / n).exists() for n in grp)
    # suppression partielle autorisée, journalisée, retirée des groupes
    r = actions.delete(state.db, state.scope, state.result, [ids["reduite.jpg"]])
    assert r["deleted"] == 1 and not (photos / "reduite.jpg").exists()
    j = state.db.q("SELECT action, src FROM journal")
    assert j[0]["action"] == "delete" and j[0]["src"].endswith("reduite.jpg")
    assert all(m["id"] != ids["reduite.jpg"] for g in state.result.groups for m in g["members"])
    # confirmation explicite
    r = actions.delete(state.db, state.scope, state.result, [ids["base.jpg"], ids["copie_identique.jpg"]], allow_last=True)
    assert r["deleted"] == 2


def test_hors_perimetre(state, photos, tmp_path):
    ids = setup(state, photos)
    state.scope.reset([str(tmp_path / "ailleurs")])                 # nouvelle session : périmètre remis à zéro
    r = actions.delete(state.db, state.scope, state.result, [ids["autre.jpg"]])
    assert r["deleted"] == 0 and (photos / "autre.jpg").exists()


def test_deplacement_et_annulation(state, photos, tmp_path):
    ids = setup(state, photos)
    dest = tmp_path / "tri"
    fid = ids["reduite.jpg"]
    r = actions.move(state.db, state.scope, state.result, [fid], str(dest))
    assert r["moved"] == 1 and (dest / "reduite.jpg").exists() and not (photos / "reduite.jpg").exists()
    # retiré de son groupe
    assert state.result.group_of(fid) is None
    assert state.db.one("SELECT path FROM files WHERE id=?", (fid,))["path"] == str(dest / "reduite.jpg")
    u = actions.undo(state.db, state.scope, state.result, [r["moves"][0]["journal"]])
    assert u["undone"] == 1 and (photos / "reduite.jpg").exists()
    assert state.result.group_of(fid) is not None                     # remis dans son groupe
    # double annulation refusée
    u = actions.undo(state.db, state.scope, state.result, [r["moves"][0]["journal"]])
    assert u["undone"] == 0


def test_renommage_et_annulation(state, photos):
    ids = setup(state, photos)
    r = actions.rename(state.db, state.scope, ids["autre.jpg"], "vacances.jpg")
    assert (photos / "vacances.jpg").exists()
    u = actions.undo(state.db, state.scope, state.result, [r["journal"]])
    assert u["undone"] == 1 and (photos / "autre.jpg").exists()
    try:
        actions.rename(state.db, state.scope, ids["autre.jpg"], "../evasion.jpg")
        assert False
    except actions.ActionError:
        pass
