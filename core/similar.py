"""Photos similaires à une photo donnée : « cliquer sur une photo et faire ressortir toutes les semblables ».

Contrairement aux doublons (qui exigent une vérification stricte de chaque paire), la recherche de
similitudes est volontairement tolérante et combine plusieurs signaux, du plus sûr au plus large :

1. empreintes visuelles (pHash 64 bits puis comparaison fine 48 px / 128 px) : recadrages, retouches,
   compressions, changements de taille — disponible sans aucun modèle d'IA ;
2. personnes (visages déjà regroupés) : les autres photos où se trouvent les mêmes personnes ;
3. objets (YOLOX) : photos partageant les mêmes objets ;
4. CLIP : photos de même contenu / même scène, même cadrage différent.

Chaque source donne une note entre 0 et 1 ; la note retenue est la meilleure, et l'on dit laquelle a parlé.
"""
import numpy as np

from .db import from_f16
from .dupes import pair_metrics
from .scanner import load_fine
from .search import CLIP_INDEX
from .util import popcount64, to_u64

PH_MAX = 26            # distance pHash maximale pour être candidat à la comparaison fine
CANDIDATES_MAX = 900   # nombre de candidats visuels comparés finement
CLIP_LOW, CLIP_HIGH = 0.62, 0.96    # cosinus CLIP image-image : 0,62 = sans rapport, 0,96 = même scène
LABELS = {"visuel": "empreinte visuelle", "personne": "mêmes personnes", "objet": "mêmes objets",
          "clip": "même contenu (IA)"}


def _c01(x):
    return float(min(1.0, max(0.0, x)))


def visual_score(metrics, ph_dist=None):
    """Note visuelle : corrélation d'ensemble, écart maximal par bloc, et distance pHash.

    La corrélation et le pHash pèsent le plus lourd : un recadrage décale tous les blocs et ferait chuter
    une note fondée sur le seul test bloc par bloc (celui-ci sert à distinguer les vrais doublons)."""
    ncc = max(0.0, metrics["ncc"])
    b2 = 1.0 - min(1.0, max(0.0, metrics["b2"]) / 3.0)
    if ph_dist is None:
        return _c01(0.64 * ncc + 0.36 * b2)
    ph = 1.0 - min(1.0, ph_dist / 32.0)
    return _c01(0.45 * ncc + 0.25 * b2 + 0.30 * ph)


def similar(db, file_id, thr=0.55, sources=None, limit=5000):
    """Liste [(id, note, [sources])] triée par note décroissante, sans la photo de départ."""
    ref = db.one("SELECT id, kind, phash FROM files WHERE id=?", (file_id,))
    if not ref:
        raise ValueError("Photo introuvable dans l'index.")
    on = set(sources or ("visuel", "personne", "objet", "clip"))
    comp = {}

    def put(i, src, s):
        if i == file_id or s <= 0:
            return
        d = comp.setdefault(i, {})
        if s > d.get(src, 0):
            d[src] = s

    # ------------------------------------------------------------ 1. empreintes visuelles
    if "visuel" in on and ref["phash"] is not None:
        rows = db.q("SELECT id, phash FROM files WHERE phash IS NOT NULL AND status='ok'")
        if rows:
            ids = np.array([r["id"] for r in rows], dtype=np.int64)
            H = np.array([to_u64(r["phash"]) for r in rows], dtype=np.uint64)
            d = popcount64(H ^ to_u64(ref["phash"])).astype(np.int32)
            keep = np.nonzero((d <= PH_MAX) & (ids != file_id))[0]
            keep = keep[np.argsort(d[keep])][:CANDIDATES_MAX]
            cand = [int(ids[k]) for k in keep]
            fine = load_fine(db, cand + [file_id])
            fr = fine.get(file_id)
            for k in keep:
                i, dist = int(ids[k]), int(d[k])
                f = fine.get(i)
                if fr is not None and f is not None:
                    put(i, "visuel", visual_score(pair_metrics(fr, f), dist))
                else:                      # pas d'empreinte fine (vidéo, erreur) : on s'en tient au pHash
                    put(i, "visuel", _c01(1.0 - dist / 32.0))

    # ------------------------------------------------------------ 2. personnes
    if "personne" in on:
        pids = [r["person_id"] for r in db.q(
            "SELECT DISTINCT person_id FROM faces WHERE file_id=? AND person_id IS NOT NULL", (file_id,))]
        if pids:
            cnt = {}
            for r in db.q("SELECT DISTINCT file_id, person_id FROM faces WHERE person_id IN (%s)"
                          % ",".join("?" * len(pids)), pids):
                cnt[r["file_id"]] = cnt.get(r["file_id"], 0) + 1
            for i, n in cnt.items():
                put(i, "personne", _c01(0.72 + 0.08 * (n - 1)))

    # ------------------------------------------------------------ 3. objets
    if "objet" in on:
        labels = [r["label"] for r in db.q("SELECT DISTINCT label FROM objects WHERE file_id=?", (file_id,))]
        if labels:
            cnt = {}
            for r in db.q("SELECT DISTINCT file_id, label FROM objects WHERE label IN (%s)"
                          % ",".join("?" * len(labels)), labels):
                cnt[r["file_id"]] = cnt.get(r["file_id"], 0) + 1
            for i, n in cnt.items():
                put(i, "objet", _c01(0.30 + 0.45 * n / len(labels)))

    # ------------------------------------------------------------ 4. CLIP (contenu)
    if "clip" in on:
        ids_c, E = CLIP_INDEX.get(db)
        if E is not None and len(ids_c):
            pos = np.nonzero(ids_c == file_id)[0]
            if len(pos):
                e = E[pos[0]].astype(np.float32)
                e /= np.linalg.norm(e) + 1e-9
                sim = (E.astype(np.float32) @ e)
                for k in np.nonzero(sim >= CLIP_LOW)[0]:
                    put(int(ids_c[k]), "clip",
                        _c01((float(sim[k]) - CLIP_LOW) / (CLIP_HIGH - CLIP_LOW)))

    out = []
    for i, d in comp.items():
        best = max(d.values())
        if best < thr:
            continue
        out.append((i, round(best, 3), sorted([s for s, v in d.items() if v >= best - 0.08],
                                              key=lambda s: -d[s])))
    out.sort(key=lambda x: -x[1])
    return out[:limit]
