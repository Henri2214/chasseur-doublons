"""Calcul des doublons à la demande, depuis l'index (changer de rigueur ne relance pas l'analyse).

1. Exacts : même SHA-256.
2. Visuels : pHash -> CANDIDATS (recherche vectorisée par blocs numpy), puis vérification de chaque paire :
   date EXIF + appareil, proportions, test 48 px (corrélation, blocs, couleurs), test fin 128 px bloc par
   bloc, et visages (même nombre, même place, mêmes personnes) si les deux photos en ont.
3. Regroupement autour d'une RÉFÉRENCE (pas de chaînage A~B~C), score de similarité par fichier.
"""
import os
import threading
from collections import defaultdict

import numpy as np

from .config import FACE_MIN_PX, FACE_SAME_COS, RIGOR
from .db import from_f16
from .scanner import load_fine, prefix_range
from .util import popcount64, to_u64


def rigor_params(db, level):
    if level == "etalonne":
        p = db.get_setting("rigor_etalonne")
        if p:
            base = dict(RIGOR["strict"])
            base.update(p)
            return base
        level = "strict"
    return dict(RIGOR.get(level, RIGOR["strict"]))


# ------------------------------------------------------------------ vérifications élémentaires
def pair_metrics(fa, fb):
    """Mesures de comparaison entre deux images (empreintes fines). Utilisé aussi par l'étalonnage."""
    ga, ca, da = fa
    gb, cb, db_ = fb
    x = ga.astype(np.float32)
    y = gb.astype(np.float32)
    x = (x - x.mean()) / (x.std() + 1e-6)
    y = (y - y.mean()) / (y.std() + 1e-6)
    ncc = float((x * y).mean())
    bmax = float(np.abs(x - y).reshape(6, 8, 6, 8).mean(axis=(1, 3)).max())
    col = float(np.abs(ca.astype(np.float32) - cb.astype(np.float32)).mean())
    u = da.astype(np.float32)
    v = db_.astype(np.float32)
    u = (u - u.mean()) / (u.std() + 1e-6)
    v = (v - v.mean()) / (v.std() + 1e-6)
    b2 = float(np.abs(u - v).reshape(16, 8, 16, 8).mean(axis=(1, 3)).max())
    return {"ncc": ncc, "bmax": bmax, "col": col, "b2": b2}


def metrics_ok(m, P):
    return m["ncc"] >= P["ncc"] and m["bmax"] <= P["bmax"] and m["col"] <= P["col"] and m["b2"] <= P["b2"]


def meta_check(a, b, P, use_exif=True):
    """(accepté, date vérifiée). Si une date EXIF manque, le contrôle de date est ignoré et signalé."""
    date_checked = False
    if use_exif:
        ta, tb = a["taken"], b["taken"]
        if ta is not None and tb is not None:
            date_checked = True
            if abs(ta - tb) > P["date"]:
                return False, True
        if a["cam"] and b["cam"] and a["cam"] != b["cam"]:
            return False, date_checked
    wa, ha, wb, hb = a["w"], a["h"], b["w"], b["h"]
    if wa and ha and wb and hb and abs(wa / ha - wb / hb) > 0.02 * (wa / ha):
        return False, date_checked
    return True, date_checked


def iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def faces_ok(fa, fb, P):
    """Si les deux photos ont des visages : même nombre, boîtes au même endroit, mêmes personnes."""
    if not fa or not fb:
        return True
    if len(fa) != len(fb):
        return False
    used = set()
    for box_a, emb_a in fa:
        best, bj = -1.0, -1
        for j, (box_b, _) in enumerate(fb):
            if j in used:
                continue
            v = iou(box_a, box_b)
            if v > best:
                best, bj = v, j
        if bj < 0 or best < P["iou"]:
            return False
        used.add(bj)
        if float(emb_a @ fb[bj][1]) < FACE_SAME_COS:
            return False
    return True


# ------------------------------------------------------------------ recherche de candidats
def candidate_pairs(H, thr, block=None):
    """Toutes les paires (i<j) dont la distance de Hamming est <= thr.

    Seuil <= 7 : index multiple sur les 8 octets (principe des tiroirs : deux empreintes à distance <= 7
    ont au moins un octet identique), comparaisons limitées aux seaux. Sinon (ou seaux trop déséquilibrés) :
    force brute vectorisée par blocs."""
    H = np.ascontiguousarray(H, dtype=np.uint64)
    if block is None and thr <= 7 and len(H) > 2000:
        r = _candidate_pairs_mih(H, thr)
        if r is not None:
            return r
    return _candidate_pairs_brute(H, thr, block)


def _candidate_pairs_mih(H, thr):
    N = len(H)
    B = H.view(np.uint8).reshape(N, 8)
    keys = []
    for k in range(8):
        col = B[:, k]
        order = np.argsort(col, kind="stable")
        bounds = np.searchsorted(col[order], np.arange(257))
        if np.diff(bounds).max() > max(4000, N // 6):
            return None                      # seaux trop gros (empreintes peu variées) : force brute
        for v in range(256):
            idx = order[bounds[v]:bounds[v + 1]]
            if len(idx) < 2:
                continue
            idx = np.sort(idx)
            D = popcount64(H[idx, None] ^ H[None, idx])
            ii, jj = np.nonzero(np.triu(D <= thr, 1))
            if len(ii):
                keys.append(idx[ii].astype(np.int64) * N + idx[jj])
    if not keys:
        return np.zeros(0, int), np.zeros(0, int), np.zeros(0, np.uint8)
    u = np.unique(np.concatenate(keys))
    ci, cj = u // N, u % N
    return ci, cj, popcount64(H[ci] ^ H[cj])


def _candidate_pairs_brute(H, thr, block=None):
    """Force brute par blocs vectorisés. Mémoire bornée : ~9 x block x N octets (~150 Mo pour 100 000)."""
    N = len(H)
    if block is None:
        block = int(max(32, min(1024, 16_000_000 // max(1, N))))
    out_i, out_j, out_d = [], [], []
    for s in range(0, N, block):
        e = min(N, s + block)
        D = popcount64(H[s:e, None] ^ H[None, s:])          # (b, N-s)
        ii, jj = np.nonzero(D <= thr)
        keep = jj > ii                                       # triangle supérieur (j absolu > i absolu)
        ii, jj = ii[keep], jj[keep]
        out_i.append(ii + s)
        out_j.append(jj + s)
        out_d.append(D[ii, jj])
    if not out_i:
        return np.zeros(0, int), np.zeros(0, int), np.zeros(0, np.uint8)
    return np.concatenate(out_i), np.concatenate(out_j), np.concatenate(out_d)


# ------------------------------------------------------------------ regroupement autour d'une référence
def reference_groups(adj, weight):
    """adj[i][j] = score. Retourne [(référence, [(membre, score)...])] sans chaînage : chaque membre est
    vérifié directement contre la référence de son groupe."""
    seen, comps = set(), []
    for start in list(adj):
        if start in seen:
            continue
        stack, comp = [start], []
        seen.add(start)
        while stack:
            x = stack.pop()
            comp.append(x)
            for y in adj[x]:
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        comps.append(comp)
    groups = []
    for comp in comps:
        remaining = set(comp)
        while len(remaining) > 1:
            a = max(remaining, key=lambda x: (sum(1 for y in adj[x] if y in remaining), weight(x)))
            mem = [y for y in adj[a] if y in remaining]
            if not mem:
                remaining.discard(a)
                continue
            groups.append((a, sorted(((y, adj[a][y]) for y in mem), key=lambda t: -t[1])))
            remaining -= {a, *mem}
    return groups


# ------------------------------------------------------------------ calcul complet
class DupeResult:
    """Résultat courant des doublons, paginé côté serveur. Les fichiers déplacés/supprimés en sont retirés."""

    def __init__(self):
        self.lock = threading.RLock()
        self.groups = []          # [{id, kind, media, nodate, members:[{id,size,score,ref,sub,best}]}]
        self.removed = {}         # file_id -> (group_id, member) pour annuler un déplacement
        self.params = {}

    def set(self, groups, params):
        with self.lock:
            self.groups = groups
            self.removed = {}
            self.params = params

    def remove(self, ids, remember=False):
        ids = set(ids)
        with self.lock:
            keep = []
            for g in self.groups:
                rest = []
                for m in g["members"]:
                    if m["id"] in ids:
                        if remember:
                            self.removed[m["id"]] = (g["id"], m)
                    else:
                        rest.append(m)
                g["members"] = rest
                keep.append(g)
            self.groups = keep

    def restore(self, ids):
        with self.lock:
            byid = {g["id"]: g for g in self.groups}
            for i in ids:
                t = self.removed.pop(i, None)
                if t and t[0] in byid:
                    byid[t[0]]["members"].append(t[1])

    def visible(self):
        with self.lock:
            return [g for g in self.groups if len(g["members"]) >= 2]

    def group_of(self, fid):
        with self.lock:
            for g in self.groups:
                if any(m["id"] == fid for m in g["members"]):
                    return g
        return None


def compute(db, roots, opts, progress=None):
    """Calcule les groupes de doublons pour les fichiers indexés sous `roots`."""
    P = rigor_params(db, opts.get("rigor", "strict"))
    use_exif = bool(opts.get("exif", True))
    kinds = [k for k, o in (("image", "images"), ("video", "videos"), ("other", "others")) if opts.get(o, k != "other")]
    min_size = max(1, int(opts.get("min_kb", 0)) * 1024)
    rows = []
    for root in roots:
        lo, hi = prefix_range(root)
        if not kinds:
            continue
        rows += db.q("SELECT id, size, kind, sha256, phash, w, h, taken, cam, dur, nframes, vsig, vcol, blur, "
                     "done_faces FROM files WHERE path >= ? AND path < ? AND size >= ? AND kind IN (%s)"
                     % ",".join("?" * len(kinds)), (lo, hi, min_size, *kinds))
    F = {r["id"]: r for r in rows}

    # 1) exacts -> représentants
    by_sha = defaultdict(list)
    for r in rows:
        if r["sha256"]:
            by_sha[r["sha256"]].append(r["id"])
    reps, rep_of = [], {}
    if opts.get("exact", True):
        for ids in by_sha.values():
            reps.append(ids)
            for i in ids:
                rep_of[i] = len(reps) - 1
    for r in rows:
        if r["id"] not in rep_of:
            reps.append([r["id"]])
            rep_of[r["id"]] = len(reps) - 1

    adj = defaultdict(dict)
    nodate = set()

    def link(a, b, s, dated=True):
        adj[a][b] = s
        adj[b][a] = s
        if not dated:
            nodate.update((a, b))

    if opts.get("visual", True):
        # ---- images
        idx = [k for k, g in enumerate(reps) if F[g[0]]["kind"] == "image" and F[g[0]]["phash"] is not None]
        if len(idx) >= 2:
            H = np.array([to_u64(F[reps[k][0]]["phash"]) for k in idx], dtype=np.uint64)
            ci, cj, cd = candidate_pairs(H, P["ph"])
            if progress:
                progress("Vérification de %d paires candidates" % len(ci))
            faces = _load_faces(db, [reps[idx[k]][0] for k in set(ci.tolist()) | set(cj.tolist())])
            cache = {}
            order = np.argsort(ci, kind="stable")
            CH = 20000
            for s in range(0, len(order), CH):
                sel = order[s:s + CH]
                need = {reps[idx[ci[t]]][0] for t in sel} | {reps[idx[cj[t]]][0] for t in sel}
                missing = [i for i in need if i not in cache]
                if len(cache) + len(missing) > 40000:
                    cache = {k: v for k, v in cache.items() if k in need}
                cache.update(load_fine(db, missing))
                for t in sel:
                    ka, kb = idx[ci[t]], idx[cj[t]]
                    a, b = F[reps[ka][0]], F[reps[kb][0]]
                    ok, dated = meta_check(a, b, P, use_exif)
                    if not ok or a["id"] not in cache or b["id"] not in cache:
                        continue
                    m = pair_metrics(cache[a["id"]], cache[b["id"]])
                    if not metrics_ok(m, P):
                        continue
                    if a["done_faces"] and a["done_faces"] == b["done_faces"] and \
                            not faces_ok(faces.get(a["id"]), faces.get(b["id"]), P):
                        continue
                    link(ka, kb, max(0.0, min(100.0, 100.0 * m["ncc"])), dated or not use_exif)

        # ---- vidéos
        vidx = [k for k, g in enumerate(reps) if F[g[0]]["kind"] == "video" and F[g[0]]["vsig"] is not None]
        if len(vidx) >= 2:
            sigs = [np.frombuffer(F[reps[k][0]]["vsig"], np.uint64) for k in vidx]
            n = len(sigs[0])
            ok_v = [i for i, s_ in enumerate(sigs) if len(s_) == n]
            vidx = [vidx[i] for i in ok_v]
            S = np.stack([sigs[i] for i in ok_v])
            C = np.stack([np.frombuffer(F[reps[k][0]]["vcol"], np.float32).reshape(n, 3) for k in vidx])
            durs = np.array([F[reps[k][0]]["dur"] or 0 for k in vidx], dtype=float)
            thr = P["vph"]
            for a in range(len(vidx) - 1):
                pc = popcount64(S[a + 1:] ^ S[a]).astype(np.float32)
                d = pc.mean(axis=1)
                mask = d <= thr
                dd = durs[a + 1:]
                mask &= np.abs(dd - durs[a]) <= np.maximum(2.0, P["vdur"] * np.maximum(dd, durs[a]))
                mask &= (pc <= thr * 2).mean(axis=1) >= 0.8
                mask &= np.abs(C[a + 1:] - C[a]).mean(axis=(1, 2)) <= 30
                for j in np.nonzero(mask)[0]:
                    link(vidx[a], vidx[a + 1 + j], 100.0 * (1 - float(d[j]) / 64.0))

    if opts.get("clip_dupes"):
        _clip_links(db, reps, F, P, use_exif, float(opts.get("clip_thr", 0.96)), link)

    # ---- groupes
    def weight(k):
        f = F[reps[k][0]]
        return ((f["w"] or 0) * (f["h"] or 0), f["size"])

    def member(fid, k, score, ref):
        return {"id": fid, "size": F[fid]["size"], "score": None if score is None else int(round(score)),
                "ref": ref, "sub": k if len(reps[k]) > 1 else None}

    groups, used = [], set()
    for a, mem in reference_groups(adj, weight):
        ml = [member(f, a, 100, n == 0) for n, f in enumerate(reps[a])]
        for y, s in mem:
            ml += [member(f, y, s, False) for f in reps[y]]
        ks = [a] + [y for y, _ in mem]
        groups.append({"id": len(groups) + 1, "kind": "visuel", "media": F[reps[a][0]]["kind"], "members": ml,
                       "nodate": any(k in nodate for k in ks)})
        used.update(ks)
    for k, g in enumerate(reps):
        if len(g) > 1 and k not in used:
            groups.append({"id": len(groups) + 1, "kind": "exact", "media": F[g[0]]["kind"], "nodate": False,
                           "members": [member(f, k, None, False) for f in g]})
    mark_best(db, groups, F)
    return groups


def _load_faces(db, ids):
    out = defaultdict(list)
    ids = list(ids)
    for i in range(0, len(ids), 900):
        ch = ids[i:i + 900]
        for r in db.q("SELECT file_id, emb, x1, y1, x2, y2, px FROM faces WHERE file_id IN (%s)"
                      % ",".join("?" * len(ch)), ch):
            if (r["px"] or 0) < FACE_MIN_PX:
                continue
            e = from_f16(r["emb"])
            out[r["file_id"]].append(((r["x1"], r["y1"], r["x2"], r["y2"]), e / (np.linalg.norm(e) + 1e-9)))
    return out


def _clip_links(db, reps, F, P, use_exif, thr, link):
    idx = [k for k, g in enumerate(reps) if F[g[0]]["kind"] == "image"]
    embs, ok = [], []
    for k in idx:
        r = db.one("SELECT emb FROM clip WHERE file_id=?", (reps[k][0],))
        if r:
            embs.append(from_f16(r["emb"]))
            ok.append(k)
    if len(ok) < 2:
        return
    E = np.stack(embs)
    E /= np.linalg.norm(E, axis=1, keepdims=True) + 1e-9
    for s in range(0, len(ok), 512):
        sim = E[s:s + 512] @ E.T
        rr, cc = np.nonzero(sim >= thr)
        for r, c in zip(rr, cc):
            if c > s + r:
                a, b = F[reps[ok[s + r]][0]], F[reps[ok[c]][0]]
                good, dated = meta_check(a, b, P, use_exif)
                if good:
                    link(ok[s + r], ok[c], 100.0 * float(sim[r, c]), dated or not use_exif)


def quality_scores(db, members, F=None):
    """Score de qualité par fichier : netteté, résolution, yeux ouverts (sourire non requis)."""
    from .config import EYE_CLOSED_RATIO
    ids = [m["id"] for m in members]
    info = {r["id"]: r for r in db.q("SELECT id, w, h, blur FROM files WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)}
    closed = defaultdict(lambda: [0, 0])
    thr = float(db.get_setting("eye_closed_ratio", EYE_CLOSED_RATIO))
    for r in db.q("SELECT file_id, eyes, px FROM faces WHERE file_id IN (%s)" % ",".join("?" * len(ids)), ids):
        if r["eyes"] is not None and (r["px"] or 0) >= 80:
            closed[r["file_id"]][1] += 1
            if r["eyes"] < thr:
                closed[r["file_id"]][0] += 1
    sharp = {i: (info[i]["blur"] or 0) if i in info else 0 for i in ids}
    res = {i: (info[i]["w"] or 0) * (info[i]["h"] or 0) if i in info else 0 for i in ids}
    ms, mr = max(sharp.values()) or 1, max(res.values()) or 1
    out = {}
    for i in ids:
        c, n = closed[i]
        out[i] = 0.5 * sharp[i] / ms + 0.3 * res[i] / mr + 0.2 * (1 - (c / n if n else 0))
    return out, {i: closed[i][0] for i in ids}


def mark_best(db, groups, F=None):
    for g in groups:
        if g["media"] != "image" or len(g["members"]) < 2:
            continue
        try:
            q, closed = quality_scores(db, g["members"])
        except Exception:
            continue
        best = max(q, key=q.get)
        for m in g["members"]:
            m["best"] = m["id"] == best
            m["quality"] = round(q[m["id"]] * 100)
            m["closed_eyes"] = closed[m["id"]]


def waste(g):
    s = [m["size"] for m in g["members"]]
    return sum(s) - max(s) if s else 0


def page(db, result, page_no=1, per=20, sort="waste", kind="all"):
    gs = result.visible()
    if kind != "all":
        gs = [g for g in gs if g["kind"] == kind or g["media"] == kind]
    if sort == "count":
        gs.sort(key=lambda g: -len(g["members"]))
    elif sort == "name":
        gs.sort(key=lambda g: g["id"])
    else:
        gs.sort(key=lambda g: -waste(g))
    total = len(gs)
    sl = gs[(page_no - 1) * per: page_no * per]
    ids = [m["id"] for g in sl for m in g["members"]]
    info = {}
    for i in range(0, len(ids), 900):
        ch = ids[i:i + 900]
        for r in db.q("SELECT id, path, name, size, mtime, taken, kind, w, h, dur, blur FROM files WHERE id IN (%s)"
                      % ",".join("?" * len(ch)), ch):
            info[r["id"]] = dict(r)
    out = []
    for g in sl:
        files = []
        for m in g["members"]:
            f = info.get(m["id"])
            if f:
                f.update(m)
                f["folder"] = os.path.dirname(f["path"])
                files.append(f)
        out.append({"id": g["id"], "kind": g["kind"], "media": g["media"], "nodate": g["nodate"],
                    "waste": waste(g), "files": files})
    all_files = sum(len(g["members"]) for g in result.visible())
    return {"total": total, "page": page_no, "per": per, "groups": out,
            "summary": {"groups": len(result.visible()), "files": all_files,
                        "waste": sum(waste(g) for g in result.visible())}}
