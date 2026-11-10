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


def create_person(db, engine, name):
    """Crée une personne (nommée) sans visage ; sert à « Nouvelle personne » dans la recherche."""
    name = (name or "").strip()
    cur = db.x("INSERT INTO persons(name, engine, created) VALUES (?, ?, ?)", (name or None, engine, time.time()))
    return cur.lastrowid if hasattr(cur, "lastrowid") else db.one("SELECT MAX(id) m FROM persons")["m"]


# ------------------------------------------------------------------ recherche par visage de référence
class FaceMatrix:
    """Matrice des visages indexés d'un moteur (float32 normalisée), rechargée si l'index a changé."""

    def __init__(self):
        self.key = None
        self.engine = None
        self.ids = self.file_ids = self.person_ids = self.elig = self.E = None

    def get(self, db, engine):
        n = db.one("SELECT COUNT(*) n, COALESCE(MAX(id),0) m, COALESCE(SUM(person_id),0) s, COALESCE(SUM(locked),0) l "
                   "FROM faces WHERE engine=?", (engine,))
        key = (engine, n["n"], n["m"], n["s"], n["l"])
        if key != self.key:
            rows = db.q("SELECT id, file_id, person_id, eligible, emb FROM faces WHERE engine=?", (engine,))
            self.ids = np.array([r["id"] for r in rows], dtype=np.int64)
            self.file_ids = np.array([r["file_id"] for r in rows], dtype=np.int64)
            self.person_ids = np.array([r["person_id"] if r["person_id"] is not None else -1 for r in rows], dtype=np.int64)
            self.elig = np.array([bool(r["eligible"]) for r in rows])
            if rows:
                E = np.stack([from_f16(r["emb"]) for r in rows]).astype(np.float32)
                E /= np.linalg.norm(E, axis=1, keepdims=True) + 1e-9
            else:
                E = np.zeros((0, 512), np.float32)
            self.E, self.key = E, key
        return self


FACE_MATRIX = FaceMatrix()


def person_reference(db, pid):
    """Vecteur de référence d'une personne : moyenne de ses visages (confirmés d'abord, sinon éligibles)."""
    rows = db.q("SELECT emb, locked FROM faces WHERE person_id=? AND (locked=1 OR eligible=1)", (pid,))
    if not rows:
        rows = db.q("SELECT emb, locked FROM faces WHERE person_id=?", (pid,))
    if not rows:
        return None
    lk = [r for r in rows if r["locked"]]
    use = lk if len(lk) >= 3 else rows
    v = np.stack([from_f16(r["emb"]) for r in use]).astype(np.float32).mean(0)
    return v / (np.linalg.norm(v) + 1e-9)


def face_reference(db, face_id):
    r = db.one("SELECT emb, engine FROM faces WHERE id=?", (face_id,))
    if not r:
        return None, None
    v = from_f16(r["emb"]).astype(np.float32)
    return v / (np.linalg.norm(v) + 1e-9), r["engine"]


def match_faces(db, engine, ref, thr, include_small=False, exclude_person=None):
    """Tous les visages dont la similarité cosinus avec `ref` est >= thr.
    Retourne une liste de (face_id, file_id, person_id|None, similarité) triée par similarité décroissante."""
    M = FACE_MATRIX.get(db, engine)
    if not len(M.ids):
        return []
    sim = M.E @ np.asarray(ref, np.float32)
    ok = sim >= thr
    if not include_small:
        ok &= M.elig | (M.person_ids >= 0)
    if exclude_person is not None:
        ok &= M.person_ids != exclude_person
    idx = np.nonzero(ok)[0]
    idx = idx[np.argsort(-sim[idx])]
    return [(int(M.ids[i]), int(M.file_ids[i]), int(M.person_ids[i]) if M.person_ids[i] >= 0 else None,
             float(sim[i])) for i in idx]


def best_per_file(matches):
    """Un résultat par photo : le visage le plus ressemblant. -> liste (file_id, face_id, person_id, sim)."""
    seen, out = set(), []
    for fid, file_id, pid, s in matches:
        if file_id in seen:
            continue
        seen.add(file_id)
        out.append((file_id, fid, pid, s))
    return out


def assign_faces(db, face_ids, pid):
    """Affectation (et verrouillage) en lot : un seul recalcul des centroïdes. Retourne le nombre de visages affectés."""
    p = db.one("SELECT engine FROM persons WHERE id=?", (pid,))
    if not p:
        raise ValueError("Personne introuvable")
    ids = [int(i) for i in face_ids]
    n = 0
    with db.wlock:
        c = db.conn()
        for i in range(0, len(ids), 800):
            ch = ids[i:i + 800]
            cur = c.execute("UPDATE faces SET person_id=?, locked=1 WHERE engine=? AND id IN (%s)" % ",".join("?" * len(ch)),
                            (pid, p["engine"], *ch))
            n += cur.rowcount
        c.commit()
    refresh_persons(db, p["engine"])
    return n
