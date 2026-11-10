"""Analyse incrémentale : inventaire -> purge -> doublons exacts -> analyse des images (décodage unique).

Seuls les fichiers nouveaux ou modifiés (taille + date de modification) sont traités. Chaque image est
décodée une seule fois puis alimente toutes les étapes activées (empreintes, qualité, visages, objets,
OCR, CLIP). Les résultats sont écrits par lots ; la mémoire reste bornée (fenêtre de tâches limitée).
"""
import hashlib
import json
import os
import threading
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import numpy as np

from . import imaging, ocr as ocr_mod
from .config import DATA_DIR, MODELS_DIR, WORKERS
from .db import f16
from .imaging import cv2
from .util import explain_oserror, is_cloud_placeholder, kind_of, lp, to_i64, unlp

RESET_COLS = ("status='new', error=NULL, w=NULL, h=NULL, taken=NULL, cam=NULL, has_exif_cam=0, phash=NULL, "
              "fine=NULL, colg=NULL, detail=NULL, blur=NULL, dur=NULL, nframes=NULL, vn=NULL, vsig=NULL, vcol=NULL, "
              "phash_part=NULL, sha256=NULL, done_visual=0, done_faces=NULL, done_objects=0, done_ocr=0, "
              "done_clip=0, done_quality=0, done_doc=0")

DEFAULT_OPTS = {
    "images": True, "videos": True, "documents": True, "others": False, "doc_text": True, "min_kb": 0, "vframes": 12, "skip_cloud": True,
    "exact": True, "visual": True, "faces": False, "objects": False, "ocr": "aucun", "clip": False,
    "quality": True, "face_engine": "opencv", "face_det": "equilibre", "yolox": "yolox_s",
}


class Cancelled(Exception):
    pass


def hash_file(path, partial=False, size=0, should_stop=None):
    h = hashlib.sha256()
    with open(lp(path), "rb") as fh:
        if partial:
            h.update(fh.read(65536))
            if size > 131072:
                fh.seek(-65536, os.SEEK_END)
                h.update(fh.read(65536))
            h.update(str(size).encode())
        else:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                if should_stop and should_stop():   # annulation immédiate, même au milieu d'un gros fichier
                    raise Cancelled()
                h.update(chunk)
    return h.hexdigest()


def prefix_range(root):
    """Bornes SQL pour sélectionner les chemins sous `root`."""
    r = root.rstrip("\\/") + os.sep
    return r, r + "\U0010ffff"


class Scanner:
    def __init__(self, db, mm):
        self.db, self.mm = db, mm
        self.state = {"status": "idle", "phase": "", "done": 0, "total": 0, "error": None, "root": "",
                      "started": 0, "ended": 0, "stats": {}, "warnings": []}
        self.cancel = False
        self._engines = {}
        self._elock = threading.Lock()

    # ------------------------------------------------------------ outils
    def _phase(self, name, total=0):
        if self.cancel:
            raise Cancelled()
        self.state.update(phase=name, done=0, total=total)

    def _engine(self, key, factory):
        with self._elock:
            if key not in self._engines:
                self._engines[key] = factory()
            return self._engines[key]

    def start(self, root, opts):
        if self.state["status"] == "running":
            raise RuntimeError("Une analyse est déjà en cours")
        o = dict(DEFAULT_OPTS)
        o.update(opts or {})
        self.cancel = False
        self.state.update(status="running", phase="", done=0, total=0, error=None, root=root,
                          started=time.time(), ended=0, stats={}, warnings=[])
        t = threading.Thread(target=self.run, args=(root, o), daemon=True)
        t.start()
        return t

    def scan_sync(self, root, opts=None):
        """Analyse bloquante (tests, outils). Retourne les statistiques."""
        o = dict(DEFAULT_OPTS)
        o.update(opts or {})
        self.cancel = False
        self.state.update(status="running", root=root, started=time.time(), ended=0, error=None, warnings=[])
        self.run(root, o)
        if self.state["status"] == "error":
            raise RuntimeError(self.state["error"])
        return self.state["stats"]

    # ------------------------------------------------------------ analyse
    def run(self, root, o):
        stats = {"stages": {}, "skipped": [], "new": 0, "changed": 0, "removed": 0, "unchanged": 0,
                 "files": 0}
        t0 = time.time()
        try:
            self._inventory(root, o, stats)
            if o.get("exact"):
                self._exact(root, o, stats)
            # témoin : s'il est encore là au prochain démarrage, c'est que l'analyse a fait tomber le programme
            if o.get("faces") or o.get("objects") or o.get("clip"):
                self.db.set_setting("analyse_en_cours", {"gpu": bool(self.mm.gpu_providers())})
            if o.get("documents") and o.get("doc_text", True):
                self._documents(root, o, stats)
            self._analyse(root, o, stats)
            if o.get("faces") and stats.get("faces_new"):
                self._phase("Regroupement des visages en personnes")
                from .persons import recluster
                tt = time.time()
                recluster(self.db, o.get("face_engine", "opencv"))
                stats["stages"]["regroupement"] = {"n": stats["faces_new"], "s": round(time.time() - tt, 3)}
            self.state["status"] = "done"
        except Cancelled:
            self.state["status"] = "cancelled"
        except Exception as e:  # noqa
            self.state["status"] = "error"
            self.state["error"] = "%s: %s" % (type(e).__name__, e)
        finally:
            try:
                self.db.x("DELETE FROM settings WHERE k='analyse_en_cours'")
            except Exception:
                pass
            stats["total_s"] = round(time.time() - t0, 3)
            self.state["stats"] = stats
            self.state["ended"] = time.time()
            try:
                self.db.x("INSERT INTO scans(root, started, ended, stats) VALUES (?,?,?,?)",
                          (root, t0, time.time(), json.dumps(stats, ensure_ascii=False)[:2_000_000]))
            except Exception:
                pass

    def _inventory(self, root, o, stats):
        self._phase("Inventaire des fichiers")
        tt = time.time()
        own = {os.path.normcase(os.path.abspath(str(DATA_DIR))), os.path.normcase(os.path.abspath(str(MODELS_DIR)))}
        seen, bad_dirs = {}, []
        skipped = stats["skipped"]

        def onerr(e):
            bad_dirs.append(unlp(getattr(e, "filename", "") or ""))
            skipped.append({"path": unlp(getattr(e, "filename", "") or "?"), "cause": "dossier illisible : " +
                            explain_oserror(e)})

        for dp, dn, fn in os.walk(lp(root), onerror=onerr):
            if self.cancel:
                raise Cancelled()
            dpu = unlp(dp)
            dn[:] = [d for d in dn if not d.startswith(".") and not os.path.islink(os.path.join(dp, d))
                     and os.path.normcase(os.path.join(dpu, d)) not in own]
            for name in fn:
                p = os.path.join(dpu, name)
                try:
                    st = os.stat(lp(p), follow_symlinks=False)
                except OSError as e:
                    skipped.append({"path": p, "cause": explain_oserror(e)})
                    continue
                if not os.path.isfile(lp(p)) or os.path.islink(lp(p)):
                    continue
                if o.get("skip_cloud", True) and is_cloud_placeholder(st):
                    skipped.append({"path": p, "cause": "fichier OneDrive non téléchargé (ignoré pour ne pas le télécharger)"})
                    continue
                seen[p] = (st.st_size, st.st_mtime, name, dpu)
            self.state["done"] = len(seen)
        stats["files"] = len(seen)

        lo, hi = prefix_range(root)
        known = {r["path"]: (r["id"], r["size"], r["mtime"])
                 for r in self.db.q("SELECT id, path, size, mtime FROM files WHERE path >= ? AND path < ?", (lo, hi))}
        now = time.time()
        ins, upd_changed, upd_seen = [], [], []
        for p, (size, mtime, name, folder) in seen.items():
            k = known.get(p)
            if k is None:
                ins.append((p, folder, name, os.path.splitext(name)[1].lower(), kind_of(name), size, mtime, now))
            elif k[1] != size or abs(k[2] - mtime) > 1e-6:
                upd_changed.append((size, mtime, now, k[0]))
            else:
                upd_seen.append((now, k[0]))
        bad = [b.rstrip("\\/") + os.sep for b in bad_dirs if b]
        gone = [k[0] for p, k in known.items() if p not in seen and not any(p.startswith(b) for b in bad)]
        with self.db.wlock:
            c = self.db.conn()
            c.executemany("INSERT INTO files(path, folder, name, ext, kind, size, mtime, seen) VALUES (?,?,?,?,?,?,?,?)", ins)
            for size, mtime, seen_t, fid in upd_changed:
                self._reset_file(c, fid)
                c.execute("UPDATE files SET size=?, mtime=?, seen=?, " + RESET_COLS + " WHERE id=?",
                          (size, mtime, seen_t, fid))
            c.executemany("UPDATE files SET seen=? WHERE id=?", upd_seen)
            for i in range(0, len(gone), 500):
                ch = gone[i:i + 500]
                c.execute("DELETE FROM files WHERE id IN (%s)" % ",".join("?" * len(ch)), ch)
            c.commit()
        forced = 0
        if o.get("force"):      # « Tout réanalyser » : on oublie ce que l'index sait des fichiers de ce dossier
            ids = [r["id"] for r in self.db.q("SELECT id FROM files WHERE path >= ? AND path < ?", (lo, hi))]
            with self.db.wlock:
                c = self.db.conn()
                for i in range(0, len(ids), 500):
                    ch = ids[i:i + 500]
                    q = ",".join("?" * len(ch))
                    for t in ("faces", "objects", "ocr", "clip", "scenes", "doctext"):
                        c.execute("DELETE FROM %s WHERE file_id IN (%s)" % (t, q), ch)
                    c.execute("UPDATE files SET " + RESET_COLS + " WHERE id IN (%s)" % q, ch)
                c.commit()
            forced = len(ids)
        stats.update(new=len(ins), changed=len(upd_changed), unchanged=0 if forced else len(upd_seen),
                     removed=len(gone), forced=forced)
        stats["stages"]["inventaire"] = {"n": len(seen), "s": round(time.time() - tt, 3)}

    @staticmethod
    def _reset_file(c, fid):
        for t in ("faces", "objects", "ocr", "clip", "scenes", "doctext"):
            c.execute("DELETE FROM %s WHERE file_id=?" % t, (fid,))

    def _scope_rows(self, root, o, cols):
        lo, hi = prefix_range(root)
        kinds = [k for k, opt in (("image", "images"), ("video", "videos"), ("doc", "documents"), ("other", "others"))
                 if o.get(opt)]
        if not kinds:
            return []
        return self.db.q("SELECT %s FROM files WHERE path >= ? AND path < ? AND size >= ? AND kind IN (%s)"
                         % (cols, ",".join("?" * len(kinds))), (lo, hi, max(1, int(o.get("min_kb", 0)) * 1024), *kinds))

    def _pool(self, items, fn, name, stage, stats):
        """Exécute fn(item) -> (id, valeurs) dans le pool, puis écrit via self._write par lots."""
        self._phase(name, len(items))
        tt = time.time()
        results = []
        with ThreadPoolExecutor(WORKERS) as ex:
            it = iter(items)
            pending = set()
            for _ in range(WORKERS * 4):
                x = next(it, None)
                if x is None:
                    break
                pending.add(ex.submit(fn, x))
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                if self.cancel:
                    for f in pending:
                        f.cancel()                 # les tâches pas encore commencées sont abandonnées
                for fut in done:
                    if fut.cancelled():
                        continue
                    try:
                        results.append(fut.result())
                    except Cancelled:
                        continue
                    self.state["done"] += 1
                    if not self.cancel:
                        x = next(it, None)
                        if x is not None:
                            pending.add(ex.submit(fn, x))
        if self.cancel:
            raise Cancelled()
        stats["stages"][stage] = {"n": len(items), "s": round(time.time() - tt, 3)}
        return results

    def _exact(self, root, o, stats):
        rows = self._scope_rows(root, o, "id, path, size, phash_part, sha256")
        by_size = defaultdict(list)
        for r in rows:
            by_size[r["size"]].append(r)
        cand = [r for g in by_size.values() if len(g) > 1 for r in g]
        need = [r for r in cand if not r["phash_part"]]

        def part(r):
            try:
                return r["id"], hash_file(r["path"], True, r["size"]), None
            except OSError as e:
                return r["id"], None, explain_oserror(e)
        res = self._pool(need, part, "Hash partiel", "hash_partiel", stats)
        self.db.many("UPDATE files SET phash_part=? WHERE id=?", [(h, i) for i, h, _ in res if h])
        for i, h, err in res:
            if err:
                stats["skipped"].append({"path": self._path(i), "cause": err})
        ids = [r["id"] for r in cand]
        parts = {}
        for i in range(0, len(ids), 900):
            ch = ids[i:i + 900]
            for r in self.db.q("SELECT id, path, size, phash_part, sha256 FROM files WHERE id IN (%s)"
                               % ",".join("?" * len(ch)), ch):
                if r["phash_part"]:
                    parts.setdefault((r["size"], r["phash_part"]), []).append(r)
        cand2 = [r for g in parts.values() if len(g) > 1 for r in g if not r["sha256"]]

        def full(r):
            try:
                return r["id"], hash_file(r["path"], should_stop=lambda: self.cancel), None
            except OSError as e:
                return r["id"], None, explain_oserror(e)
        res = self._pool(cand2, full, "Hash SHA-256 complet", "sha256", stats)
        self.db.many("UPDATE files SET sha256=? WHERE id=?", [(h, i) for i, h, _ in res if h])
        for i, h, err in res:
            if err:
                stats["skipped"].append({"path": self._path(i), "cause": err})

    def _path(self, fid):
        r = self.db.one("SELECT path FROM files WHERE id=?", (fid,))
        return r["path"] if r else "?"

    # ------------------------------------------------------------ contenu des documents
    def _documents(self, root, o, stats):
        """Lit le texte des documents nouveaux ou modifiés et calcule leurs empreintes de contenu."""
        from . import docs
        lo, hi = prefix_range(root)
        want_ocr = o.get("ocr", "aucun") != "aucun"
        rows = self.db.q(
            "SELECT f.id, f.path, f.ext, f.size, f.done_doc, d.words, d.method FROM files f "
            "LEFT JOIN doctext d ON d.file_id=f.id WHERE f.path >= ? AND f.path < ? AND f.kind='doc' AND f.size >= ?",
            (lo, hi, max(1, int(o.get("min_kb", 0)) * 1024)))
        todo = [r for r in rows if not r["done_doc"] or
                (want_ocr and r["done_doc"] == 1 and r["ext"] == ".pdf" and (r["words"] or 0) < 20
                 and r["method"] != "erreur")]
        stats["docs_new"] = len(todo)
        if not todo:
            return
        ocrm = None
        if want_ocr and any(r["ext"] == ".pdf" for r in todo):
            try:
                ocrm = self._engine(("ocr",), ocr_mod.OCR)
            except Exception:  # noqa
                ocrm = None
        level = 2 if ocrm is not None else 1

        def one(r):
            if self.cancel:
                raise Cancelled()
            base = {"id": r["id"], "path": r["path"], "text": "", "method": "erreur", "pages": None, "title": None,
                    "mid": None, "sender": None, "mdate": None, "extra": None, "error": None}
            if r["size"] > docs.MAX_BYTES:
                base["error"] = "document trop volumineux pour la lecture du contenu (comparé bit à bit seulement)"
                return base
            try:
                base.update(docs.extract(r["path"], r["ext"], ocr=ocrm))
            except docs.Unsupported as e:
                base["error"] = str(e)
            except OSError as e:
                base["error"] = explain_oserror(e)
            except Exception as e:  # noqa
                base["error"] = "contenu illisible (%s)" % type(e).__name__
            if not base["error"]:
                base["chash"], base["sketch"], base["words"] = docs.fingerprint(base["text"])
            return base
        res = self._pool(todo, one, "Lecture du contenu des documents", "documents", stats)
        n_text = 0
        with self.db.wlock:
            c = self.db.conn()
            for d in res:
                c.execute("DELETE FROM doctext WHERE file_id=?", (d["id"],))
                c.execute("INSERT INTO doctext(file_id, text, chash, sketch, words, method, pages, title, mid, sender, "
                          "mdate, extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (d["id"], d["text"], d.get("chash"), d.get("sketch") or None, d.get("words") or 0,
                           d["method"], d["pages"], d["title"], d["mid"], d["sender"], d["mdate"],
                           json.dumps(d["extra"], ensure_ascii=False) if d["extra"] else None))
                c.execute("UPDATE files SET done_doc=?, status='ok', error=? WHERE id=?", (level, d["error"], d["id"]))
                if d["error"]:
                    stats["skipped"].append({"path": d["path"], "cause": d["error"]})
                elif d.get("words"):
                    n_text += 1
            c.commit()
        stats["docs_text"] = n_text

    # ------------------------------------------------------------ analyse des médias
    def _analyse(self, root, o, stats):
        engine_name = o.get("face_engine", "opencv")
        rows = self._scope_rows(root, o, "id, path, name, kind, status, vn, done_visual, done_faces, "
                                         "done_objects, done_ocr, done_clip, done_quality, has_exif_cam")
        vn = max(4, min(48, int(o.get("vframes", 12))))
        ocr_mode = o.get("ocr", "aucun")
        tasks = []
        for r in rows:
            if r["kind"] in ("other", "doc"):
                continue
            need = set()
            if r["kind"] == "video":
                if (o.get("visual") or o.get("clip")) and (not r["done_visual"] or r["vn"] != vn):
                    need.add("vvisual")
            else:
                if r["status"] == "error":
                    continue   # déjà signalé comme illisible ; sera retenté s'il change
                if not r["done_visual"]:
                    need.add("visual")
                if o.get("quality") and not r["done_quality"]:
                    need.add("quality")
                if o.get("faces") and r["done_faces"] != engine_name:
                    need.add("faces")
                if o.get("objects") and not r["done_objects"]:
                    need.add("objects")
                if ocr_mode == "toutes" and r["done_ocr"] < 2 or ocr_mode == "auto" and r["done_ocr"] == 0:
                    need.add("ocr")
                if o.get("clip") and not r["done_clip"]:
                    need.add("clip")
            if need:
                tasks.append((dict(r), need))
        stats["to_analyse"] = len(tasks) + stats.get("docs_new", 0)
        if not tasks:
            return

        # modèles : chargés une fois, partagés ; dégradation propre si absents
        warn = self.state["warnings"]
        face_eng = yolo = clipm = ocrm = None
        if any("faces" in n for _, n in tasks):
            try:
                from .faces import DET_SIZES, make_engine
                ds = DET_SIZES.get(o.get("face_det", "equilibre"), 480)
                face_eng = self._engine(("faces", engine_name, ds), lambda: make_engine(self.mm, engine_name, ds))
            except Exception as e:  # noqa
                warn.append("Visages désactivés : %s" % e)
        if any("objects" in n for _, n in tasks):
            try:
                from .objects import YoloX
                v = o.get("yolox", "yolox_s")
                yolo = self._engine(("yolo", v), lambda: YoloX(self.mm, v))
            except Exception as e:  # noqa
                warn.append("Objets désactivés : %s" % e)
        if any("clip" in n for _, n in tasks):
            try:
                from .clip import Clip
                clipm = self._engine(("clip",), lambda: Clip(self.mm))
            except Exception as e:  # noqa
                warn.append("CLIP désactivé : %s" % e)
        if any("ocr" in n for _, n in tasks):
            try:
                ocrm = self._engine(("ocr",), ocr_mod.OCR)
            except Exception as e:  # noqa
                warn.append("OCR désactivé : %s (pip install rapidocr-onnxruntime)" % e)

        timing = defaultdict(lambda: [0, 0.0])
        tlock = threading.Lock()

        def tick(stage, t):
            with tlock:
                timing[stage][0] += 1
                timing[stage][1] += time.perf_counter() - t

        def work(task):
            r, need = task
            out = {"id": r["id"], "kind": r["kind"], "need": need}
            if self.cancel:
                out["error"] = None
                out["cancelled"] = True
                return out
            try:
                if r["kind"] == "video":
                    t = time.perf_counter()
                    info, sig, col = imaging.video_signature(r["path"], vn, should_stop=lambda: self.cancel)
                    if self.cancel:            # signature incomplète : ne rien enregistrer, refaite au prochain passage
                        out["cancelled"] = True
                        return out
                    out.update(info=info, vsig=sig, vcol=col)
                    tick("vidéos", t)
                    return out
                t = time.perf_counter()
                dec = imaging.decode(r["path"])
                tick("décodage", t)
                out["dec_meta"] = (dec.w, dec.h, dec.taken, dec.cam, dec.has_cam)
                if "visual" in need:
                    t = time.perf_counter()
                    s = dec.sig
                    fine, colg = imaging.fine_features(s)
                    out.update(phash=imaging.phash(s), fine=fine, colg=colg, detail=imaging.detail_features(s))
                    tick("empreintes", t)
                if "quality" in need:
                    t = time.perf_counter()
                    out["blur"] = imaging.global_blur(dec)
                    tick("qualité", t)
                bgr = None
                if ({"faces", "objects", "ocr"} & need) and cv2 is not None:
                    bgr = dec.bgr()
                if "faces" in need and face_eng is not None and bgr is not None:
                    t = time.perf_counter()
                    sc = dec.w / max(1, dec.rgb.size[0])
                    W, H = dec.rgb.size
                    fl = []
                    for f in face_eng.analyse(bgr):
                        x1, y1, x2, y2 = [float(v) for v in f["box"]]
                        px = int(max(x2 - x1, y2 - y1) * sc)
                        fl.append((f["emb"], max(0, x1 / W), max(0, y1 / H), min(1, x2 / W), min(1, y2 / H), px,
                                   f["score"], f["blur"], f["eyes"]))
                    out["faces"] = fl
                    tick("visages", t)
                if "objects" in need and yolo is not None and bgr is not None:
                    t = time.perf_counter()
                    out["objects"] = yolo.detect(bgr)[:30]
                    tick("objets", t)
                if "ocr" in need and ocrm is not None and bgr is not None:
                    if ocr_mod.wanted(ocr_mode, r["name"], dec.fmt, dec.has_cam):
                        t = time.perf_counter()
                        out["ocr"] = ocrm.read(bgr)
                        tick("ocr", t)
                    else:
                        out["ocr_skip"] = True
                if "clip" in need and clipm is not None:
                    t = time.perf_counter()
                    e = clipm.image([dec.rgb])[0]
                    out["clip"] = e
                    out["scenes"] = clipm.scenes(e)
                    tick("clip", t)
            except (OSError, SyntaxError, ValueError) as e:
                out["error"] = explain_oserror(e) if isinstance(e, OSError) else "image illisible (%s)" % e
            except Exception as e:  # noqa
                out["error"] = "%s: %s" % (type(e).__name__, e)
            return out

        self._phase("Analyse des images et vidéos", len(tasks))
        tt = time.time()
        buf = []
        stats["faces_new"] = 0
        with ThreadPoolExecutor(WORKERS) as ex:
            it = iter(tasks)
            pending = {ex.submit(work, t) for t in [next(it) for _ in range(min(len(tasks), WORKERS * 3))]}
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                if self.cancel:
                    for f in pending:
                        f.cancel()
                for fut in done:
                    if fut.cancelled():
                        continue
                    buf.append(fut.result())
                    self.state["done"] += 1
                    if not self.cancel:
                        x = next(it, None)
                        if x is not None:
                            pending.add(ex.submit(work, x))
                if len(buf) >= 64:
                    self._write(buf, stats, engine_name, vn, face_eng, yolo, clipm, ocrm)
                    buf = []
        self._write(buf, stats, engine_name, vn, face_eng, yolo, clipm, ocrm)
        if self.cancel:
            raise Cancelled()
        wall = time.time() - tt
        stats["stages"]["analyse"] = {"n": len(tasks), "s": round(wall, 3)}
        stats["per_stage"] = {k: {"n": n, "ms_par_image": round(1000 * s / max(1, n), 1),
                                  "images_par_s_1_thread": round(n / s, 1) if s > 0 else None,
                                  "images_par_s": round(n / s * min(WORKERS, len(tasks)), 1) if s > 0 else None}
                              for k, (n, s) in timing.items()}

    def _write(self, buf, stats, engine_name, vn, face_eng, yolo, clipm, ocrm):
        if not buf:
            return
        with self.db.wlock:
            c = self.db.conn()
            for o in buf:
                if o.get("cancelled"):
                    continue
                fid = o["id"]
                if o.get("error"):
                    c.execute("UPDATE files SET status='error', error=? WHERE id=?", (o["error"], fid))
                    stats["skipped"].append({"path": self._path(fid), "cause": o["error"]})
                    continue
                if o["kind"] == "video":
                    info = o.get("info") or {}
                    sig, col = o.get("vsig"), o.get("vcol")
                    c.execute("UPDATE files SET status='ok', w=?, h=?, dur=?, nframes=?, vn=?, vsig=?, vcol=?, "
                              "done_visual=1 WHERE id=?",
                              (info.get("w"), info.get("h"), info.get("dur"), info.get("n"), vn,
                               sig.tobytes() if sig is not None else None,
                               col.tobytes() if col is not None else None, fid))
                    if not info:
                        stats["skipped"].append({"path": self._path(fid), "cause": "vidéo illisible (codec ?)"})
                    continue
                w, h, taken, cam, has_cam = o["dec_meta"]
                c.execute("UPDATE files SET status='ok', error=NULL, w=?, h=?, taken=?, cam=?, has_exif_cam=? WHERE id=?",
                          (w, h, taken, cam, int(has_cam), fid))
                if "phash" in o:
                    c.execute("UPDATE files SET phash=?, fine=?, colg=?, detail=?, done_visual=1 WHERE id=?",
                              (to_i64(o["phash"]), o["fine"].tobytes(), o["colg"].tobytes(),
                               o["detail"].tobytes(), fid))
                if "blur" in o:
                    c.execute("UPDATE files SET blur=?, done_quality=1 WHERE id=?", (o["blur"], fid))
                if "faces" in o:
                    from .faces import eligible
                    c.execute("DELETE FROM faces WHERE file_id=?", (fid,))
                    for e, x1, y1, x2, y2, px, score, blur, eyes in o["faces"]:
                        c.execute("INSERT INTO faces(file_id, engine, emb, x1, y1, x2, y2, px, score, blur, eyes, "
                                  "eligible) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (fid, engine_name, f16(e), x1, y1, x2, y2, px, score, blur, eyes,
                                   int(eligible(px, blur))))
                    stats["faces_new"] += len(o["faces"])
                    c.execute("UPDATE files SET done_faces=? WHERE id=?", (engine_name, fid))
                if "objects" in o:
                    c.execute("DELETE FROM objects WHERE file_id=?", (fid,))
                    c.executemany("INSERT INTO objects(file_id, label, score, x1, y1, x2, y2) VALUES (?,?,?,?,?,?,?)",
                                  [(fid, lab, s, *b) for lab, s, b in o["objects"]])
                    c.execute("UPDATE files SET done_objects=1 WHERE id=?", (fid,))
                if "ocr" in o:
                    c.execute("DELETE FROM ocr WHERE file_id=?", (fid,))
                    if o["ocr"]:
                        c.execute("INSERT INTO ocr(file_id, text) VALUES (?,?)", (fid, o["ocr"]))
                    c.execute("UPDATE files SET done_ocr=2 WHERE id=?", (fid,))
                elif o.get("ocr_skip"):
                    c.execute("UPDATE files SET done_ocr=1 WHERE id=?", (fid,))
                if "clip" in o:
                    c.execute("INSERT OR REPLACE INTO clip(file_id, emb) VALUES (?,?)", (fid, f16(o["clip"])))
                    c.execute("DELETE FROM scenes WHERE file_id=?", (fid,))
                    c.executemany("INSERT INTO scenes(file_id, label, score) VALUES (?,?,?)",
                                  [(fid, lab, s) for lab, s in o["scenes"]])
                    c.execute("UPDATE files SET done_clip=1 WHERE id=?", (fid,))
            c.commit()


def load_fine(db, ids):
    """Charge les empreintes fines {id: (fine48, colg, detail128)}."""
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 900):
        ch = ids[i:i + 900]
        for r in db.q("SELECT id, fine, colg, detail FROM files WHERE id IN (%s)" % ",".join("?" * len(ch)), ch):
            if r["fine"] is None:
                continue
            out[r["id"]] = (np.frombuffer(r["fine"], np.uint8).reshape(48, 48),
                            np.frombuffer(r["colg"], np.uint8).reshape(4, 4, 3),
                            np.frombuffer(r["detail"], np.uint8).reshape(128, 128))
    return out
