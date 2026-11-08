"""Personnes : regroupement des visages, renommage, fusion, retrait, verrouillage."""
import time

import numpy as np

from .db import f16, from_f16
from .faces import cluster, default_threshold


def threshold(db, engine):
    return float(db.get_setting("person_thr_" + engine, default_threshold(engine)))


def recluster(db, engine):
    """Regroupe tous les visages du moteur donné. Les personnes nommées et les visages verrouillés ne
    bougent pas ; les visages trop petits ou trop flous restent « non assignés »."""
    rows = db.q("SELECT id, emb, person_id, locked FROM faces WHERE engine=? AND (eligible=1 OR locked=1) "
                "ORDER BY px * score DESC", (engine,))
    persons = {r["id"]: r for r in db.q("SELECT id, name, centroid FROM persons WHERE engine=?", (engine,))}
    if not rows:
        return {"faces": 0, "persons": 0}
    embs = np.stack([from_f16(r["emb"]) for r in rows])
    embs /= np.linalg.norm(embs, axis=1, keepdims=True) + 1e-9
    locked = np.array([bool(r["locked"]) for r in rows])
    lpid = np.array([r["person_id"] if r["person_id"] is not None else -1 for r in rows], dtype=np.int64)
    seeds = {}
    for pid, p in persons.items():
        m = (lpid == pid)
        if m.any():
            seeds[pid] = embs[m].mean(0)
        elif p["centroid"] is not None:
            seeds[pid] = from_f16(p["centroid"])
    labels = cluster(embs, locked, lpid, seeds, threshold(db, engine))

    with db.wlock:
        c = db.conn()
        newmap = {}
        for lab in sorted(set(labels.tolist())):
            if lab <= -2:
                cur = c.execute("INSERT INTO persons(name, engine, created) VALUES (NULL, ?, ?)", (engine, time.time()))
                newmap[lab] = cur.lastrowid
        upd = []
        for r, lab, lk in zip(rows, labels.tolist(), locked.tolist()):
            if lk:
                continue
            pid = newmap.get(lab, lab if lab > 0 else None)
            upd.append((pid, r["id"]))
        c.executemany("UPDATE faces SET person_id=? WHERE id=?", upd)
        # les visages non éligibles et non verrouillés ne sont assignés à personne
        c.execute("UPDATE faces SET person_id=NULL WHERE engine=? AND eligible=0 AND locked=0", (engine,))
        c.commit()
    refresh_persons(db, engine)
    n = db.one("SELECT COUNT(*) n FROM persons WHERE engine=?", (engine,))["n"]
    return {"faces": len(rows), "persons": n}


def refresh_persons(db, engine=None):
    """Recalcule centroïdes et visage-type ; supprime les personnes sans nom devenues vides."""
    with db.wlock:
        c = db.conn()
        q = "SELECT id, name FROM persons" + (" WHERE engine=?" if engine else "")
        for p in c.execute(q, (engine,) if engine else ()).fetchall():
            fr = c.execute("SELECT id, emb FROM faces WHERE person_id=? ORDER BY locked DESC, px * score DESC",
                           (p["id"],)).fetchall()
            if not fr:
                if not p["name"]:
                    c.execute("DELETE FROM persons WHERE id=?", (p["id"],))
                continue
            e = np.stack([from_f16(r["emb"]) for r in fr]).mean(0)
            e /= np.linalg.norm(e) + 1e-9
            c.execute("UPDATE persons SET centroid=?, cover_face=? WHERE id=?", (f16(e), fr[0]["id"], p["id"]))
        c.commit()


def rename(db, pid, name):
    db.x("UPDATE persons SET name=? WHERE id=?", ((name or "").strip() or None, pid))
    # nommer une personne confirme ses visages actuels
    db.x("UPDATE faces SET locked=1 WHERE person_id=?", (pid,))


def merge(db, src, dst):
    """Fusionne la personne src dans dst."""
    if src == dst:
        return
    a = db.one("SELECT engine, name FROM persons WHERE id=?", (src,))
    b = db.one("SELECT engine, name FROM persons WHERE id=?", (dst,))
    if not a or not b or a["engine"] != b["engine"]:
        raise ValueError("Personnes introuvables ou moteurs différents")
    with db.wlock:
        c = db.conn()
        c.execute("UPDATE faces SET person_id=?, locked=1 WHERE person_id=?", (dst, src))
        if not b["name"] and a["name"]:
            c.execute("UPDATE persons SET name=? WHERE id=?", (a["name"], dst))
        c.execute("DELETE FROM persons WHERE id=?", (src,))
        c.commit()
    refresh_persons(db, a["engine"])


def remove_face(db, face_id):
    """Retire un visage de sa personne ; il ne sera plus réaffecté automatiquement."""
    r = db.one("SELECT engine FROM faces WHERE id=?", (face_id,))
    db.x("UPDATE faces SET person_id=NULL, locked=1 WHERE id=?", (face_id,))
    if r:
        refresh_persons(db, r["engine"])


def assign_face(db, face_id, pid):
    """Affecte (et verrouille) un visage à une personne."""
    r = db.one("SELECT engine FROM faces WHERE id=?", (face_id,))
    p = db.one("SELECT engine FROM persons WHERE id=?", (pid,))
    if not r or not p or r["engine"] != p["engine"]:
        raise ValueError("Visage ou personne introuvable")
    db.x("UPDATE faces SET person_id=?, locked=1 WHERE id=?", (pid, face_id))
    refresh_persons(db, r["engine"])


def confirm_face(db, face_id):
    db.x("UPDATE faces SET locked=1 WHERE id=?", (face_id,))


def list_persons(db, engine=None, page=1, per=60, q=""):
    where, args = ["1=1"], []
    if engine:
        where.append("p.engine=?")
        args.append(engine)
    if q:
        where.append("p.name LIKE ?")
        args.append("%" + q + "%")
    total = db.one("SELECT COUNT(*) n FROM persons p WHERE " + " AND ".join(where), args)["n"]
    rows = db.q("SELECT p.id, p.name, p.engine, p.cover_face, (SELECT COUNT(*) FROM faces f WHERE f.person_id=p.id) n, "
                "(SELECT COUNT(DISTINCT file_id) FROM faces f WHERE f.person_id=p.id) photos "
                "FROM persons p WHERE " + " AND ".join(where) +
                " ORDER BY (p.name IS NULL), n DESC LIMIT ? OFFSET ?", (*args, per, (page - 1) * per))
    unassigned = db.one("SELECT COUNT(*) n FROM faces WHERE person_id IS NULL" +
                        (" AND engine=?" if engine else ""), (engine,) if engine else ())["n"]
    return {"total": total, "unassigned": unassigned, "persons": [dict(r) for r in rows]}
