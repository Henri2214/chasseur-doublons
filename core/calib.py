"""Outil d'étalonnage : « ces deux photos sont-elles des doublons ? » / « même personne ? » -> ajuste les seuils.

Doublons : on propose des paires candidates (pHash proche, rigueur « large ») dont les mesures sont proches
des seuils actuels. Les réponses servent à calculer, pour chaque mesure, le seuil qui accepte le plus de
vrais doublons sans accepter de faux (une fausse acceptation pèse 3 fois plus qu'un refus à tort).
Le résultat est enregistré comme rigueur « étalonnée ».
"""
import json
import random
import time

import numpy as np

from .config import RIGOR
from .db import from_f16
from .dupes import candidate_pairs, meta_check, pair_metrics
from .scanner import load_fine
from .util import to_u64

LOWER_IS_BETTER = ("b2", "bmax", "col", "ph")
HIGHER_IS_BETTER = ("ncc",)
FP_WEIGHT = 3.0


def _answered(db, kind):
    return {(r["a"], r["b"]) for r in db.q("SELECT a, b FROM calib WHERE kind=?", (kind,))}


def next_dup_pair(db, sample=4000):
    rows = db.q("SELECT id, phash, w, h, taken, cam FROM files WHERE kind='image' AND phash IS NOT NULL "
                "ORDER BY RANDOM() LIMIT ?", (sample,))
    if len(rows) < 2:
        return None
    H = np.array([to_u64(r["phash"]) for r in rows], dtype=np.uint64)
    ci, cj, cd = candidate_pairs(H, RIGOR["large"]["ph"])
    done = _answered(db, "dup")
    P = RIGOR["large"]
    cands = []
    for i, j, d in zip(ci.tolist(), cj.tolist(), cd.tolist()):
        a, b = rows[i], rows[j]
        key = (min(a["id"], b["id"]), max(a["id"], b["id"]))
        if key in done:
            continue
        ok, _ = meta_check(a, b, P, True)
        if ok:
            cands.append((key, d))
    if not cands:
        return None
    random.shuffle(cands)
    strict = RIGOR["strict"]
    best = None
    for key, d in cands[:300]:
        fe = load_fine(db, key)
        if len(fe) < 2:
            continue
        m = pair_metrics(fe[key[0]], fe[key[1]])
        m["ph"] = int(d)
        # proximité de la frontière de décision (mesure fine b2 surtout)
        closeness = abs(m["b2"] - strict["b2"]) / strict["b2"]
        if best is None or closeness < best[0]:
            best = (closeness, key, m)
    if not best:
        return None
    return {"kind": "dup", "a": best[1][0], "b": best[1][1], "metrics": best[2]}


def next_face_pair(db, engine):
    rows = db.q("SELECT id, emb, file_id FROM faces WHERE engine=? AND eligible=1 ORDER BY RANDOM() LIMIT 2000", (engine,))
    if len(rows) < 2:
        return None
    E = np.stack([from_f16(r["emb"]) for r in rows])
    E /= np.linalg.norm(E, axis=1, keepdims=True) + 1e-9
    S = E @ E.T
    np.fill_diagonal(S, -1)
    done = _answered(db, "face")
    from .persons import threshold
    thr = threshold(db, engine)
    ii, jj = np.nonzero(np.abs(S - thr) < 0.12)
    order = np.random.permutation(len(ii))
    for k in order[:5000]:
        a, b = rows[ii[k]], rows[jj[k]]
        if a["file_id"] == b["file_id"]:
            continue
        key = (min(a["id"], b["id"]), max(a["id"], b["id"]))
        if key not in done:
            return {"kind": "face", "a": key[0], "b": key[1], "metrics": {"cos": float(S[ii[k], jj[k]])},
                    "engine": engine}
    return None


def answer(db, kind, a, b, metrics, yes):
    db.x("INSERT INTO calib(kind, a, b, metrics, answer, ts) VALUES (?,?,?,?,?,?)",
         (kind, int(a), int(b), json.dumps(metrics), 1 if yes else 0, time.time()))


def best_threshold(pos, neg, lower_is_better=True):
    """Seuil qui maximise (vrais doublons acceptés) - FP_WEIGHT x (faux acceptés)."""
    vals = sorted(set(pos) | set(neg))
    if not vals:
        return None
    best, best_t = None, None
    for t in vals:
        if lower_is_better:
            tp = sum(v <= t for v in pos)
            fp = sum(v <= t for v in neg)
        else:
            tp = sum(v >= t for v in pos)
            fp = sum(v >= t for v in neg)
        s = tp - FP_WEIGHT * fp
        if best is None or s > best:
            best, best_t = s, t
    return best_t


def stats(db):
    out = {}
    for kind in ("dup", "face"):
        r = db.one("SELECT COUNT(*) n, SUM(answer) y FROM calib WHERE kind=?", (kind,))
        out[kind] = {"n": r["n"], "oui": r["y"] or 0, "non": (r["n"] or 0) - (r["y"] or 0)}
    out["rigor_etalonne"] = db.get_setting("rigor_etalonne")
    return out


def apply_dup(db, min_answers=10):
    rows = db.q("SELECT metrics, answer FROM calib WHERE kind='dup'")
    if len(rows) < min_answers:
        raise ValueError("Au moins %d réponses sont nécessaires (actuellement %d)." % (min_answers, len(rows)))
    data = [(json.loads(r["metrics"]), r["answer"]) for r in rows]
    if not any(a for _, a in data) or all(a for _, a in data):
        raise ValueError("Il faut des réponses « oui » ET « non ».")
    params = {}
    for k in LOWER_IS_BETTER + HIGHER_IS_BETTER:
        pos = [m[k] for m, a in data if a and k in m]
        neg = [m[k] for m, a in data if not a and k in m]
        t = best_threshold(pos, neg, k in LOWER_IS_BETTER)
        if t is not None:
            params[k] = float(t)
    if "ph" in params:
        params["ph"] = int(params["ph"])
    db.set_setting("rigor_etalonne", params)
    return params


def apply_face(db, engine, min_answers=10):
    rows = db.q("SELECT metrics, answer FROM calib WHERE kind='face'")
    if len(rows) < min_answers:
        raise ValueError("Au moins %d réponses sont nécessaires." % min_answers)
    pos = [json.loads(r["metrics"])["cos"] for r in rows if r["answer"]]
    neg = [json.loads(r["metrics"])["cos"] for r in rows if not r["answer"]]
    if not pos or not neg:
        raise ValueError("Il faut des réponses « oui » ET « non ».")
    t = best_threshold(pos, neg, lower_is_better=False)
    db.set_setting("person_thr_" + engine, float(t))
    return t
