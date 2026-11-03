#!/usr/bin/env python3
"""
Chasseur de doublons - serveur local + page web.

Détection :
  1. Doublons exacts      : taille -> hash partiel -> SHA-256 complet
  2. Doublons visuels     : hash perceptuel (pHash) images + 5 images-clés par vidéo
  3. Doublons "sémantiques": embeddings CLIP (optionnel, sentence-transformers)

Actions en direct depuis la page : supprimer (définitif), déplacer, renommer.
"""
import argparse
import hashlib
import importlib.util
import os
import shutil
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from flask import Flask, Response, abort, jsonify, request, send_file
from PIL import Image, ImageOps

try:
    import cv2
except ImportError:  # vidéos : pas de signature visuelle ni de miniature
    cv2 = None

try:  # HEIC/HEIF (photos iPhone) si pillow-heif est installé
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:
    pass

BASE = Path(__file__).resolve().parent
CACHE = BASE / ".cache_miniatures"
CACHE.mkdir(exist_ok=True)

IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".heif", ".jfif"}
VID_EXT = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".3gp", ".mts", ".m2ts", ".ts"}
KIND_OPT = {"image": "images", "video": "videos", "other": "others"}
FRACS = (0.1, 0.3, 0.5, 0.7, 0.9)
WORKERS = min(8, (os.cpu_count() or 2) * 2)

app = Flask(__name__)
TOKEN = hashlib.sha256(os.urandom(32)).hexdigest()
ROOTS = set()
LOCK = threading.Lock()
_id = 0


def next_id():
    global _id
    _id += 1
    return _id


JOB = {"status": "idle", "phase": "", "done": 0, "total": 0, "error": None,
       "cancel": False, "groups": [], "started": 0, "ended": 0, "scanned": 0}


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- utilitaires
def norm(p):
    return os.path.normcase(os.path.realpath(p))


def inside_roots(p):
    rp = norm(p)
    return any(rp == r or rp.startswith(r + os.sep) for r in ROOTS)


def popcount(a):
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(a)
    lut = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
    b = np.ascontiguousarray(a).view(np.uint8).reshape(a.shape + (8,))
    return lut[b].sum(axis=-1)


def _dct_matrix(n=32):
    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    m = np.cos(np.pi * (2 * i + 1) * k / (2 * n)) * np.sqrt(2.0 / n)
    m[0] /= np.sqrt(2)
    return m


DCT = _dct_matrix()


def phash(im):
    """Hash perceptuel 64 bits (DCT 32x32 -> 8x8 basses fréquences)."""
    g = im.convert("L").resize((32, 32), Image.LANCZOS)
    d = DCT @ np.asarray(g, dtype=np.float64) @ DCT.T
    low = d[:8, :8].flatten()
    bits = low > np.median(low[1:])
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def video_frames(path, fracs=FRACS, maxside=256):
    if cv2 is None:
        return [], {}
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return [], {}
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = cap.get(cv2.CAP_PROP_FPS) or 0
    info = {"n": n, "w": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
            "h": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
            "dur": (n / fps) if fps > 0 and n > 0 else 0}
    frames = []
    for f in fracs:
        if n > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(n * f)))
        ok, fr = cap.read()
        if not ok:
            continue
        hh, ww = fr.shape[:2]
        s = maxside / max(hh, ww)
        if s < 1:
            fr = cv2.resize(fr, (int(ww * s), int(hh * s)), interpolation=cv2.INTER_AREA)
        frames.append(Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)))
    cap.release()
    return frames, info


def load_image(path, kind, maxside, frac=0.5):
    """Retourne une image PIL RGB réduite (image ou image-clé de vidéo), ou None."""
    try:
        if kind == "video":
            frames, _ = video_frames(path, (frac,), maxside)
            return frames[0] if frames else None
        im = Image.open(path)
        try:
            im.draft("RGB", (maxside * 2, maxside * 2))
        except Exception:
            pass
        im = ImageOps.exif_transpose(im)
        im.thumbnail((maxside, maxside))
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            return bg
        return im.convert("RGB")
    except Exception:
        return None


def analyse(f):
    """Signature visuelle + dimensions pour une image ou une vidéo."""
    try:
        if f["kind"] == "image":
            with Image.open(f["path"]) as im:
                w, h = im.size
                orient = im.getexif().get(274, 1)
            if orient in (5, 6, 7, 8):
                w, h = h, w
            im = load_image(f["path"], "image", 256)
            if im is None:
                return
            f["w"], f["h"] = w, h
            f["sig"] = np.array([phash(im)], dtype=np.uint64)
        else:
            vn = JOB.get("vn", 12)
            frames, info = video_frames(f["path"], [(i + 0.5) / vn for i in range(vn)])
            f.update(info)
            # signature fiable seulement si le nombre d'images est connu et toutes les images-clés lues
            if info.get("n", 0) >= vn * 2 and len(frames) == vn:
                f["sig"] = np.array([phash(x) for x in frames], dtype=np.uint64)
                f["col"] = np.stack([np.asarray(x, dtype=np.float32).reshape(-1, 3).mean(0) for x in frames])
    except Exception:
        pass


class UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def hash_file(path, partial=False, size=0):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        if partial:
            h.update(fh.read(65536))
            if size > 131072:
                fh.seek(-65536, os.SEEK_END)
                h.update(fh.read(65536))
            h.update(str(size).encode())
        else:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- analyse
def set_phase(name, total=0):
    if JOB["cancel"]:
        raise Cancelled()
    JOB.update(phase=name, done=0, total=total)


def run_pool(items, fn, name):
    set_phase(name, len(items))

    def wrap(x):
        if JOB["cancel"]:
            return None
        fn(x)
        JOB["done"] += 1

    with ThreadPoolExecutor(WORKERS) as ex:
        list(ex.map(wrap, items))
    if JOB["cancel"]:
        raise Cancelled()


def run_scan(root, opt):
    try:
        JOB["vn"] = max(4, min(48, int(opt.get("vframes", 12))))
        # ---- 1. inventaire
        set_phase("Inventaire des fichiers")
        files = []
        min_size = int(opt.get("min_kb", 0)) * 1024
        for dp, dn, fn in os.walk(root):
            if JOB["cancel"]:
                raise Cancelled()
            dn[:] = [d for d in dn if not d.startswith(".") and not os.path.islink(os.path.join(dp, d))]
            for name in fn:
                p = os.path.join(dp, name)
                ext = os.path.splitext(name)[1].lower()
                kind = "image" if ext in IMG_EXT else "video" if ext in VID_EXT else "other"
                if not opt.get(KIND_OPT[kind]):
                    continue
                try:
                    if os.path.islink(p):
                        continue
                    st = os.stat(p)
                except OSError:
                    continue
                if st.st_size == 0 or st.st_size < min_size:
                    continue
                files.append({"id": next_id(), "path": p, "name": name, "size": st.st_size,
                              "mtime": st.st_mtime, "kind": kind})
            JOB["done"] = len(files)
        JOB["scanned"] = len(files)

        # ---- 2. doublons exacts -> reps = liste de listes de fichiers identiques
        reps = []
        if opt.get("exact", True):
            by_size = defaultdict(list)
            for f in files:
                by_size[f["size"]].append(f)
            cand = [f for g in by_size.values() if len(g) > 1 for f in g]

            def part(f):
                try:
                    f["ph"] = hash_file(f["path"], True, f["size"])
                except OSError:
                    f["ph"] = None
            run_pool(cand, part, "Hash partiel")
            by_part = defaultdict(list)
            for f in cand:
                if f.get("ph"):
                    by_part[(f["size"], f["ph"])].append(f)
            cand2 = [f for g in by_part.values() if len(g) > 1 for f in g]

            def full(f):
                try:
                    f["sha"] = hash_file(f["path"])
                except OSError:
                    f["sha"] = None
            run_pool(cand2, full, "Hash SHA-256 complet")
            by_sha = defaultdict(list)
            seen = set()
            for f in cand2:
                if f.get("sha"):
                    by_sha[f["sha"]].append(f)
            for g in by_sha.values():
                reps.append(g)
                seen.update(x["id"] for x in g)
            for f in files:
                if f["id"] not in seen:
                    reps.append([f])
        else:
            reps = [[f] for f in files]

        uf = UF(len(reps))

        # ---- 3. visuel
        use_ph, use_ia = opt.get("phash"), opt.get("ia")
        if use_ph or use_ia:
            media = [i for i, g in enumerate(reps) if g[0]["kind"] in ("image", "video")]
            run_pool([reps[i][0] for i in media], analyse, "Signatures visuelles (images/vidéos)")
            # propager dimensions aux membres d'un groupe exact
            for g in reps:
                for x in g[1:]:
                    for k in ("w", "h", "dur"):
                        if k in g[0]:
                            x[k] = g[0][k]

            if use_ph:
                thr = float(opt.get("ph_thr", 6))
                for kind in ("image", "video"):
                    idx = [i for i in media if reps[i][0]["kind"] == kind and "sig" in reps[i][0]]
                    if len(idx) < 2:
                        continue
                    sigs = np.stack([reps[i][0]["sig"] for i in idx])
                    if kind == "image":
                        sigs = sigs[:, 0]
                    durs = np.array([reps[i][0].get("dur", 0) for i in idx], dtype=float)
                    cols = np.stack([reps[i][0]["col"] for i in idx]) if kind == "video" else None
                    set_phase("Comparaison visuelle (%s)" % ("images" if kind == "image" else "vidéos"), len(idx))
                    for a in range(len(idx) - 1):
                        if JOB["cancel"]:
                            raise Cancelled()
                        pc = popcount(sigs[a + 1:] ^ sigs[a])
                        d = pc if pc.ndim == 1 else pc.mean(axis=1)
                        mask = d <= thr
                        if kind == "video":
                            dd = durs[a + 1:]
                            mask &= np.abs(dd - durs[a]) <= np.maximum(2.0, 0.03 * np.maximum(dd, durs[a]))
                            # comparaison fine : >= 80 % des images-clés proches, et couleurs moyennes cohérentes
                            mask &= (pc <= thr * 2).mean(axis=1) >= 0.8
                            mask &= np.abs(cols[a + 1:] - cols[a]).mean(axis=(1, 2)) <= 30
                        for j in np.nonzero(mask)[0]:
                            uf.union(idx[a], idx[a + 1 + j])
                        JOB["done"] = a + 1

            if use_ia:
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError:
                    raise RuntimeError("Mode IA : installez 'sentence-transformers' (pip install sentence-transformers).")
                set_phase("Chargement du modèle CLIP (1er lancement : téléchargement ~350 Mo)")
                model = SentenceTransformer("clip-ViT-B-32")
                idx = media
                set_phase("Embeddings IA (CLIP)", len(idx))
                embs, ok_idx = [], []
                B = 64
                with ThreadPoolExecutor(WORKERS) as ex:
                    for s in range(0, len(idx), B):
                        if JOB["cancel"]:
                            raise Cancelled()
                        chunk = idx[s:s + B]
                        ims = list(ex.map(lambda i: load_image(reps[i][0]["path"], reps[i][0]["kind"], 224), chunk))
                        pairs = [(i, im) for i, im in zip(chunk, ims) if im is not None]
                        if pairs:
                            e = model.encode([im for _, im in pairs], batch_size=B, convert_to_numpy=True,
                                             normalize_embeddings=True, show_progress_bar=False)
                            embs.append(e.astype(np.float32))
                            ok_idx += [i for i, _ in pairs]
                        JOB["done"] = min(len(idx), s + B)
                if len(ok_idx) > 1:
                    E = np.concatenate(embs)
                    thr = float(opt.get("ia_thr", 0.92))
                    set_phase("Comparaison IA", len(ok_idx))
                    C = 512
                    for s in range(0, len(ok_idx), C):
                        if JOB["cancel"]:
                            raise Cancelled()
                        sim = E[s:s + C] @ E.T
                        rows, cols = np.nonzero(sim >= thr)
                        for r, c in zip(rows, cols):
                            if c > s + r:
                                uf.union(ok_idx[s + r], ok_idx[c])
                        JOB["done"] = min(len(ok_idx), s + C)

        # ---- 4. construction des groupes
        set_phase("Construction des résultats")
        comp = defaultdict(list)
        for i in range(len(reps)):
            comp[uf.find(i)].append(i)
        groups = []
        for members in comp.values():
            fl = []
            for i in members:
                g = reps[i]
                for f in g:
                    fl.append({"id": f["id"], "path": f["path"], "name": f["name"], "size": f["size"],
                               "mtime": f["mtime"], "kind": f["kind"], "w": f.get("w"), "h": f.get("h"),
                               "dur": f.get("dur"), "sub": i if len(g) > 1 else None})
            if len(fl) < 2:
                continue
            groups.append({"id": len(groups) + 1, "kind": "exact" if len(members) == 1 else "visuel",
                           "media": fl[0]["kind"], "files": fl})
        with LOCK:
            JOB["groups"] = groups
        JOB["status"] = "done"
    except Cancelled:
        JOB["status"] = "cancelled"
    except Exception as e:  # noqa
        JOB["status"] = "error"
        JOB["error"] = str(e)
    finally:
        JOB["ended"] = time.time()


# --------------------------------------------------------------------------- API
def check_token():
    if request.headers.get("X-Token") != TOKEN:
        abort(403)


def caps():
    return {"video": cv2 is not None,
            "ia": importlib.util.find_spec("sentence_transformers") is not None}


@app.get("/")
def index():
    html = (BASE / "static" / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)
    return Response(html, mimetype="text/html")


@app.post("/api/scan")
def api_scan():
    check_token()
    data = request.get_json(force=True)
    root = os.path.abspath(os.path.expanduser(data.get("path", "")))
    if not os.path.isdir(root):
        return jsonify(error="Dossier introuvable : %s" % root), 400
    if JOB["status"] == "running":
        return jsonify(error="Une analyse est déjà en cours"), 409
    ROOTS.add(norm(root))
    JOB.update(status="running", phase="", done=0, total=0, error=None, cancel=False,
               groups=[], started=time.time(), ended=0, scanned=0, root=root)
    threading.Thread(target=run_scan, args=(root, data.get("options", {})), daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/cancel")
def api_cancel():
    check_token()
    JOB["cancel"] = True
    return jsonify(ok=True)


@app.get("/api/status")
def api_status():
    end = JOB["ended"] or time.time()
    return jsonify(status=JOB["status"], phase=JOB["phase"], done=JOB["done"], total=JOB["total"],
                   error=JOB["error"], scanned=JOB["scanned"], root=JOB.get("root", ""),
                   elapsed=round(end - JOB["started"], 1) if JOB["started"] else 0, caps=caps())


@app.get("/api/results")
def api_results():
    return jsonify(groups=JOB["groups"])


@app.get("/api/ls")
def api_ls():
    p = request.args.get("p", "")
    if not p:
        if os.name == "nt":
            import string
            drives = ["%s:\\" % d for d in string.ascii_uppercase if os.path.exists("%s:\\" % d)]
            return jsonify(path="", parent=None, dirs=[{"name": d, "path": d} for d in drives])
        p = os.path.expanduser("~")
    p = os.path.abspath(p)
    if not os.path.isdir(p):
        return jsonify(error="Introuvable"), 400
    dirs = []
    try:
        for e in sorted(os.scandir(p), key=lambda e: e.name.lower()):
            if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                dirs.append({"name": e.name, "path": e.path})
    except OSError:
        pass
    parent = os.path.dirname(p)
    return jsonify(path=p, parent=parent if parent != p else ("" if os.name == "nt" else None), dirs=dirs)


@app.get("/api/file")
def api_file():
    p = request.args.get("p", "")
    if not inside_roots(p) or not os.path.isfile(p):
        abort(404)
    return send_file(p, conditional=True)


def placeholder(label, video=False):
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#2a3040"/>'
           '<text x="128" y="140" font-family="sans-serif" font-size="44" fill="#8b95ab" text-anchor="middle">%s</text></svg>'
           % (label[:6].upper()))
    return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/thumb")
def api_thumb():
    p = request.args.get("p", "")
    s = max(64, min(int(request.args.get("s", 320)), 800))
    if not inside_roots(p) or not os.path.isfile(p):
        abort(404)
    ext = os.path.splitext(p)[1].lower()
    kind = "image" if ext in IMG_EXT else "video" if ext in VID_EXT else "other"
    if kind == "other":
        return placeholder(ext.lstrip(".") or "fichier")
    st = os.stat(p)
    key = hashlib.sha1(("%s|%s|%s|%s" % (norm(p), st.st_mtime, st.st_size, s)).encode()).hexdigest()
    cf = CACHE / (key + ".jpg")
    if not cf.exists():
        im = load_image(p, kind, s, 0.2)
        if im is None:
            return placeholder(ext.lstrip("."))
        im.save(cf, "JPEG", quality=82)
    return send_file(cf, mimetype="image/jpeg", max_age=86400)


def drop_from_groups(removed_ids, updates=None):
    with LOCK:
        new = []
        for g in JOB["groups"]:
            g["files"] = [f for f in g["files"] if f["id"] not in removed_ids]
            if updates:
                for f in g["files"]:
                    if f["id"] in updates:
                        f.update(updates[f["id"]])
            if len(g["files"]) >= 2:
                new.append(g)
        JOB["groups"] = new


def find_file(path):
    for g in JOB["groups"]:
        for f in g["files"]:
            if f["path"] == path:
                return f
    return None


@app.post("/api/delete")
def api_delete():
    check_token()
    paths = request.get_json(force=True).get("paths", [])
    ok_ids, errors, freed = set(), [], 0
    for p in paths:
        f = find_file(p)
        try:
            if not f or not inside_roots(p):
                raise PermissionError("Fichier hors périmètre")
            os.remove(p)
            ok_ids.add(f["id"])
            freed += f["size"]
        except Exception as e:
            errors.append({"path": p, "error": str(e)})
    drop_from_groups(ok_ids)
    return jsonify(deleted=len(ok_ids), freed=freed, errors=errors, groups=JOB["groups"])


@app.post("/api/move")
def api_move():
    check_token()
    data = request.get_json(force=True)
    dest = os.path.abspath(os.path.expanduser(data.get("dest", "")))
    if not data.get("dest"):
        return jsonify(error="Destination manquante"), 400
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as e:
        return jsonify(error=str(e)), 400
    ROOTS.add(norm(dest))
    updates, errors, moves = {}, [], []
    for p in data.get("paths", []):
        f = find_file(p)
        try:
            if not f or not inside_roots(p):
                raise PermissionError("Fichier hors périmètre")
            if norm(os.path.dirname(p)) == norm(dest):
                raise ValueError("déjà dans ce dossier")
            name, target, n = f["name"], os.path.join(dest, f["name"]), 1
            stem, ext = os.path.splitext(name)
            while os.path.exists(target):
                target = os.path.join(dest, "%s (%d)%s" % (stem, n, ext))
                n += 1
            shutil.move(p, target)
            updates[f["id"]] = {"path": target, "name": os.path.basename(target)}
            moves.append({"id": f["id"], "from": p, "to": target})
        except Exception as e:
            errors.append({"path": p, "error": str(e)})
    drop_from_groups(set(), updates)
    return jsonify(moved=len(updates), moves=moves, errors=errors, groups=JOB["groups"])


@app.post("/api/undo")
def api_undo():
    check_token()
    updates, errors = {}, []
    for m in request.get_json(force=True).get("moves", []):
        try:
            if not inside_roots(m["to"]) or os.path.exists(m["from"]):
                raise FileExistsError("Impossible d'annuler (emplacement d'origine occupé)")
            os.makedirs(os.path.dirname(m["from"]), exist_ok=True)
            shutil.move(m["to"], m["from"])
            updates[m["id"]] = {"path": m["from"], "name": os.path.basename(m["from"])}
        except Exception as e:
            errors.append({"path": m.get("to"), "error": str(e)})
    drop_from_groups(set(), updates)
    return jsonify(undone=len(updates), errors=errors, groups=JOB["groups"])


def tree_ok(p):
    """Dossier consultable dans le bandeau : dans un dossier analysé, au-dessus de lui, ou enfant direct d'un ancêtre."""
    def chk(q):
        rq = norm(q)
        return any(rq == r or rq.startswith(r + os.sep) or r.startswith(rq.rstrip(os.sep) + os.sep) for r in ROOTS)
    return chk(p) or chk(os.path.dirname(os.path.abspath(p)))


@app.get("/api/dirs")
def api_dirs():
    p = os.path.abspath(request.args.get("p", ""))
    if not os.path.isdir(p) or not tree_ok(p):
        abort(404)
    out = []
    try:
        entries = sorted(os.scandir(p), key=lambda e: e.name.lower())
    except OSError:
        entries = []
    for e in entries:
        if len(out) >= 400:
            break
        try:
            if not e.is_dir(follow_symlinks=False) or e.name.startswith("."):
                continue
            n, sub = 0, False
            for c in os.scandir(e.path):
                if c.is_dir(follow_symlinks=False):
                    sub = True
                elif c.is_file(follow_symlinks=False):
                    n += 1
            out.append({"name": e.name, "path": e.path, "n": n, "sub": sub})
        except OSError:
            continue
    return jsonify(path=p, dirs=out)


@app.get("/api/dirthumb")
def api_dirthumb():
    p = os.path.abspath(request.args.get("p", ""))
    s = max(48, min(int(request.args.get("s", 96)), 256))
    if not os.path.isdir(p) or not tree_ok(p):
        abort(404)
    st = os.stat(p)
    cf = CACHE / (hashlib.sha1(("D|%s|%s|%s" % (norm(p), st.st_mtime, s)).encode()).hexdigest() + ".jpg")
    if not cf.exists():
        pics = []
        try:
            for e in sorted(os.scandir(p), key=lambda e: e.name.lower()):
                if e.is_file(follow_symlinks=False) and os.path.splitext(e.name)[1].lower() in IMG_EXT:
                    pics.append(e.path)
                    if len(pics) == 4:
                        break
        except OSError:
            pass
        ims = [im for im in (load_image(x, "image", s) for x in pics) if im is not None]
        if not ims:
            svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96"><rect width="96" height="96" fill="#232a38"/>'
                   '<path d="M12 28h26l8 8h38v40H12z" fill="#f5a524"/></svg>')
            return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "max-age=60"})
        mos = Image.new("RGB", (s, s), (20, 24, 32))
        if len(ims) == 1:
            im = ImageOps.fit(ims[0], (s, s))
            mos.paste(im, (0, 0))
        else:
            h = s // 2
            for k, im in enumerate(ims):
                mos.paste(ImageOps.fit(im, (h, h)), ((k % 2) * h, (k // 2) * h))
        mos.save(cf, "JPEG", quality=80)
    return send_file(cf, mimetype="image/jpeg", max_age=30)


@app.post("/api/rename")
def api_rename():
    check_token()
    data = request.get_json(force=True)
    p, new = data.get("path", ""), (data.get("name") or "").strip()
    f = find_file(p)
    if not f or not inside_roots(p):
        return jsonify(error="Fichier introuvable"), 404
    if not new or any(c in new for c in '/\\:*?"<>|') or new in (".", ".."):
        return jsonify(error="Nom invalide"), 400
    target = os.path.join(os.path.dirname(p), new)
    if os.path.exists(target) and norm(target) != norm(p):
        return jsonify(error="Un fichier porte déjà ce nom"), 409
    try:
        os.rename(p, target)
    except OSError as e:
        return jsonify(error=str(e)), 400
    drop_from_groups(set(), {f["id"]: {"path": target, "name": new}})
    return jsonify(ok=True, groups=JOB["groups"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Chasseur de doublons (page web locale)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    if a.host != "127.0.0.1":
        print("ATTENTION : l'interface permet de supprimer des fichiers. Exposez-la uniquement sur un réseau de confiance.")
    url = "http://%s:%d" % ("127.0.0.1" if a.host in ("0.0.0.0", "127.0.0.1") else a.host, a.port)
    print("Chasseur de doublons ->", url)
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=a.host, port=a.port, threaded=True)
