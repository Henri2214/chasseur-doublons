"""Serveur web local (Flask) : API + page unique.

Sécurité : écoute 127.0.0.1 par défaut, jeton de session obligatoire pour toute requête POST (actions),
contrôle de l'en-tête Host (protège contre le DNS rebinding), fichiers servis uniquement s'ils sont
indexés et dans le périmètre de la session.
"""
import hashlib
import importlib.util
import os
import string
import threading
import time

from flask import Flask, Response, abort, jsonify, request, send_file
from PIL import Image, ImageOps

from . import __version__, actions, calib, dupes, persons, search
from .config import DATA_DIR, IMG_EXT, RIGOR_LABELS
from .db import Database
from .imaging import HEIF, cv2, load_small
from .models import LICENSE_INSIGHTFACE, ModelManager, providers
from .scanner import Scanner
from .util import Scope, kind_of, lp, norm


class State:
    def __init__(self, data_dir=DATA_DIR, models_dir=None):
        self.data_dir = str(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.db = Database(os.path.join(self.data_dir, "index.sqlite3"))
        self.mm = ModelManager(models_dir, self.db) if models_dir else ModelManager(db=self.db)
        self.scanner = Scanner(self.db, self.mm)
        self.scope = Scope()
        self.result = dupes.DupeResult()
        self.thumbs = os.path.join(self.data_dir, "miniatures")
        os.makedirs(self.thumbs, exist_ok=True)
        self.token = hashlib.sha256(os.urandom(32)).hexdigest()
        self.dupe_state = {"status": "idle", "error": None, "elapsed": 0}
        self.last_opts = {}
        self._clip = None
        self._clip_lock = threading.Lock()

    def clip(self):
        with self._clip_lock:
            if self._clip is None and self.mm.available("clip"):
                try:
                    from .clip import Clip
                    self._clip = Clip(self.mm)
                except Exception:
                    self._clip = None
            return self._clip


def create_app(state=None):
    S = state or State()
    app = Flask(__name__, static_folder=None)
    app.config["STATE"] = S
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")

    allowed_hosts = {"127.0.0.1", "localhost", "[::1]"}

    @app.before_request
    def guard():
        host = (request.host or "").rsplit(":", 1)[0]
        if host not in allowed_hosts and not app.config.get("ALLOW_ANY_HOST"):
            abort(403)
        if request.method == "POST" and request.headers.get("X-Token") != S.token:
            abort(403)

    def body():
        return request.get_json(force=True, silent=True) or {}

    def err(msg, code=400):
        return jsonify(error=msg), code

    # ------------------------------------------------------------ page
    @app.get("/")
    def index():
        with open(os.path.join(static_dir, "index.html"), encoding="utf-8") as fh:
            html = fh.read().replace("__TOKEN__", S.token).replace("__VERSION__", __version__)
        return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ capacités / modèles / réglages
    def caps():
        st = S.mm.status()
        return {
            "video": cv2 is not None, "heif": HEIF,
            "faces_insightface": st["insightface"]["available"], "faces_opencv": st["opencv_faces"]["available"],
            "objects": st["yolox_s"]["available"] or st["yolox_nano"]["available"],
            "clip": st["clip"]["available"] and importlib.util.find_spec("tokenizers") is not None,
            "ocr": importlib.util.find_spec("rapidocr_onnxruntime") is not None,
            "onnx": importlib.util.find_spec("onnxruntime") is not None,
            "providers": providers(), "fts": S.db.fts,
        }

    @app.get("/api/caps")
    def api_caps():
        return jsonify(caps=caps(), models=S.mm.status(), license_insightface=LICENSE_INSIGHTFACE,
                       rigors=RIGOR_LABELS, settings=_settings(), version=__version__)

    def _settings():
        from .config import BLUR_GLOBAL_MIN, EYE_CLOSED_RATIO
        return {"person_thr_insightface": persons.threshold(S.db, "insightface"),
                "person_thr_opencv": persons.threshold(S.db, "opencv"),
                "eye_closed_ratio": float(S.db.get_setting("eye_closed_ratio", EYE_CLOSED_RATIO)),
                "blur_min": float(S.db.get_setting("blur_min", BLUR_GLOBAL_MIN)),
                "face_engine": S.db.get_setting("face_engine", "opencv"),
                "has_etalonne": bool(S.db.get_setting("rigor_etalonne"))}

    @app.post("/api/settings")
    def api_settings():
        d = body()
        for k in ("person_thr_insightface", "person_thr_opencv", "eye_closed_ratio", "blur_min"):
            if k in d:
                S.db.set_setting(k, float(d[k]))
        if d.get("face_engine") in ("insightface", "opencv"):
            S.db.set_setting("face_engine", d["face_engine"])
        return jsonify(settings=_settings())

    @app.post("/api/models/download")
    def api_model_download():
        d = body()
        name = d.get("name")
        if d.get("accept_license"):
            S.mm.confirm_license(name)
        try:
            S.mm.download(name)
        except PermissionError as e:
            return err(str(e), 409)
        except KeyError:
            return err("modèle inconnu", 404)
        return jsonify(ok=True)

    @app.post("/api/models/delete")
    def api_model_delete():
        S.mm.delete(body().get("name"))
        return jsonify(ok=True)

    # ------------------------------------------------------------ analyse
    @app.post("/api/scan")
    def api_scan():
        d = body()
        root = os.path.abspath(os.path.expanduser(d.get("path", "")))
        if not os.path.isdir(lp(root)):
            return err("Dossier introuvable : %s" % root)
        opts = d.get("options", {})
        if opts.get("face_engine") == "insightface" and not S.mm.license_ok("insightface"):
            return err("Confirmez la licence InsightFace (onglet Réglages) ou choisissez le moteur OpenCV.", 409)
        try:
            S.scope.reset([root])          # périmètre remis à zéro à chaque session d'analyse
            S.result.set([], {})
            S.last_opts = dict(opts, root=root)
            S.scanner.start(root, opts)
        except RuntimeError as e:
            return err(str(e), 409)
        return jsonify(ok=True)

    @app.post("/api/cancel")
    def api_cancel():
        S.scanner.cancel = True
        return jsonify(ok=True)

    @app.get("/api/status")
    def api_status():
        st = S.scanner.state
        end = st["ended"] or time.time()
        return jsonify(status=st["status"], phase=st["phase"], done=st["done"], total=st["total"],
                       error=st["error"], root=st["root"], warnings=st["warnings"],
                       elapsed=round(end - st["started"], 1) if st["started"] else 0,
                       dupes=S.dupe_state, has_results=bool(S.result.groups))

    @app.get("/api/report")
    def api_report():
        st = S.scanner.state.get("stats") or {}
        page = int(request.args.get("page", 1))
        sk = st.get("skipped", [])
        indexed_err = [dict(r) for r in S.db.q("SELECT path, error AS cause FROM files WHERE status='error' LIMIT 5000")]
        return jsonify(stats={k: v for k, v in st.items() if k != "skipped"}, skipped_total=len(sk),
                       skipped=sk[(page - 1) * 200: page * 200], errors_in_index=indexed_err,
                       last_scans=[dict(r) for r in S.db.q("SELECT id, root, started, ended FROM scans ORDER BY id DESC LIMIT 10")])

    # ------------------------------------------------------------ doublons (recalcul depuis l'index)
    @app.post("/api/dupes")
    def api_dupes():
        d = body()
        root = S.scanner.state.get("root") or d.get("root")
        if not root:
            return err("Lancez d'abord une analyse.")
        if S.scanner.state["status"] == "running":
            return err("Analyse en cours", 409)
        opts = dict(S.last_opts)
        opts.update(d.get("options", {}))
        S.dupe_state.update(status="running", error=None)
        t = time.time()
        try:
            groups = dupes.compute(S.db, [root], opts)
            S.result.set(groups, opts)
            S.dupe_state.update(status="done", elapsed=round(time.time() - t, 2))
        except Exception as e:  # noqa
            S.dupe_state.update(status="error", error=str(e))
            return err(str(e), 500)
        return jsonify(ok=True, groups=len(S.result.visible()), elapsed=S.dupe_state["elapsed"])

    @app.get("/api/groups")
    def api_groups():
        a = request.args
        return jsonify(dupes.page(S.db, S.result, int(a.get("page", 1)), min(100, int(a.get("per", 20))),
                                  a.get("sort", "waste"), a.get("kind", "all")))

    @app.get("/api/group_ids")
    def api_group_ids():
        """Identifiants et métadonnées légères de tous les groupes (pour la sélection automatique)."""
        out = []
        for g in S.result.visible():
            ids = [m["id"] for m in g["members"]]
            info = {r["id"]: dict(r) for r in S.db.q(
                "SELECT id, size, mtime, w, h, length(path) pl, length(name) nl FROM files WHERE id IN (%s)"
                % ",".join("?" * len(ids)), ids)}
            out.append({"id": g["id"], "members": [dict(info.get(m["id"], {}), best=m.get("best", False))
                                                    for m in g["members"] if m["id"] in info]})
        return jsonify(groups=out)

    # ------------------------------------------------------------ actions
    @app.post("/api/delete")
    def api_delete():
        d = body()
        r = actions.delete(S.db, S.scope, S.result, d.get("ids", []), bool(d.get("allow_last")))
        return jsonify(r)

    @app.post("/api/move")
    def api_move():
        d = body()
        try:
            return jsonify(actions.move(S.db, S.scope, S.result, d.get("ids", []), d.get("dest", "")))
        except actions.ActionError as e:
            return err(str(e))

    @app.post("/api/rename")
    def api_rename():
        d = body()
        try:
            return jsonify(actions.rename(S.db, S.scope, int(d.get("id")), d.get("name")))
        except actions.ActionError as e:
            return err(str(e))

    @app.post("/api/undo")
    def api_undo():
        return jsonify(actions.undo(S.db, S.scope, S.result, body().get("journal", [])))

    @app.get("/api/journal")
    def api_journal():
        return jsonify(actions.journal_page(S.db, int(request.args.get("page", 1))))

    # ------------------------------------------------------------ fichiers et miniatures
    def file_row(fid):
        r = S.db.one("SELECT id, path, kind, size, mtime FROM files WHERE id=?", (fid,))
        if not r or not S.scope.contains(r["path"]) or not os.path.isfile(lp(r["path"])):
            abort(404)
        return r

    @app.get("/api/file")
    def api_file():
        r = file_row(int(request.args.get("id", 0)))
        return send_file(lp(r["path"]), conditional=True)

    def placeholder(label):
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#2a3040"/>'
               '<text x="128" y="140" font-family="sans-serif" font-size="44" fill="#8b95ab" text-anchor="middle">%s</text></svg>'
               % label[:6].upper().replace("<", ""))
        return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "max-age=3600"})

    @app.get("/api/thumb")
    def api_thumb():
        r = file_row(int(request.args.get("id", 0)))
        s = max(64, min(int(request.args.get("s", 320)), 800))
        ext = os.path.splitext(r["path"])[1].lower()
        if r["kind"] == "other":
            return placeholder(ext.lstrip(".") or "fichier")
        key = hashlib.sha1(("%s|%s|%s|%s" % (norm(r["path"]), r["mtime"], r["size"], s)).encode()).hexdigest()
        cf = os.path.join(S.thumbs, key[:2], key + ".jpg")
        if not os.path.exists(cf):
            im = load_small(r["path"], r["kind"], s)
            if im is None:
                return placeholder(ext.lstrip("."))
            os.makedirs(os.path.dirname(cf), exist_ok=True)
            im.save(cf + ".tmp", "JPEG", quality=82)
            os.replace(cf + ".tmp", cf)
        return send_file(cf, mimetype="image/jpeg", max_age=86400)

    @app.get("/api/facethumb")
    def api_facethumb():
        fid = int(request.args.get("id", 0))
        f = S.db.one("SELECT f.*, x.path, x.mtime FROM faces f JOIN files x ON x.id=f.file_id WHERE f.id=?", (fid,))
        if not f or not S.scope.contains(f["path"]):
            abort(404)
        key = hashlib.sha1(("F|%d|%s|%s" % (fid, norm(f["path"]), f["mtime"])).encode()).hexdigest()
        cf = os.path.join(S.thumbs, "visages", key + ".jpg")
        if not os.path.exists(cf):
            im = load_small(f["path"], "image", 1280)
            if im is None:
                abort(404)
            W, H = im.size
            x1, y1, x2, y2 = f["x1"] * W, f["y1"] * H, f["x2"] * W, f["y2"] * H
            m = 0.25 * max(x2 - x1, y2 - y1)
            crop = im.crop((int(max(0, x1 - m)), int(max(0, y1 - m)), int(min(W, x2 + m)), int(min(H, y2 + m))))
            crop = ImageOps.fit(crop, (128, 128))
            os.makedirs(os.path.dirname(cf), exist_ok=True)
            crop.save(cf, "JPEG", quality=85)
        return send_file(cf, mimetype="image/jpeg", max_age=3600)

    @app.get("/api/info")
    def api_info():
        fid = int(request.args.get("id", 0))
        r = S.db.one("SELECT id, path, name, size, mtime, taken, cam, w, h, blur, kind FROM files WHERE id=?", (fid,))
        if not r or not S.scope.contains(r["path"]):
            abort(404)
        return jsonify(file=dict(r),
                       faces=[dict(x) for x in S.db.q("SELECT f.id, f.x1, f.y1, f.x2, f.y2, f.px, f.eyes, f.person_id, "
                                                       "p.name FROM faces f LEFT JOIN persons p ON p.id=f.person_id "
                                                       "WHERE f.file_id=?", (fid,))],
                       objects=[dict(x) for x in S.db.q("SELECT label, score FROM objects WHERE file_id=? ORDER BY score DESC", (fid,))],
                       scenes=[dict(x) for x in S.db.q("SELECT label, score FROM scenes WHERE file_id=?", (fid,))],
                       ocr=(S.db.one("SELECT text FROM ocr WHERE file_id=?", (fid,)) or {"text": ""})["text"])

    # ------------------------------------------------------------ navigation dossiers
    @app.get("/api/ls")
    def api_ls():
        p = request.args.get("p", "")
        if not p:
            if os.name == "nt":
                drives = ["%s:\\" % d for d in string.ascii_uppercase if os.path.exists("%s:\\" % d)]
                return jsonify(path="", parent=None, dirs=[{"name": d, "path": d} for d in drives])
            p = os.path.expanduser("~")
        p = os.path.abspath(p)
        if not os.path.isdir(lp(p)):
            return err("Introuvable")
        dirs = []
        try:
            for e in sorted(os.scandir(lp(p)), key=lambda e: e.name.lower()):
                if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                    dirs.append({"name": e.name, "path": os.path.join(p, e.name)})
        except OSError:
            pass
        parent = os.path.dirname(p)
        return jsonify(path=p, parent=parent if parent != p else ("" if os.name == "nt" else None), dirs=dirs)

    @app.get("/api/dirs")
    def api_dirs():
        p = os.path.abspath(request.args.get("p", ""))
        if not os.path.isdir(lp(p)) or not S.scope.near(p):
            abort(404)
        out = []
        try:
            entries = sorted(os.scandir(lp(p)), key=lambda e: e.name.lower())
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
                out.append({"name": e.name, "path": os.path.join(p, e.name), "n": n, "sub": sub})
            except OSError:
                continue
        return jsonify(path=p, dirs=out)

    @app.get("/api/dirthumb")
    def api_dirthumb():
        p = os.path.abspath(request.args.get("p", ""))
        s = max(48, min(int(request.args.get("s", 96)), 256))
        if not os.path.isdir(lp(p)) or not S.scope.near(p):
            abort(404)
        st = os.stat(lp(p))
        cf = os.path.join(S.thumbs, "dossiers", hashlib.sha1(("D|%s|%s|%s" % (norm(p), st.st_mtime, s)).encode()).hexdigest() + ".jpg")
        if not os.path.exists(cf):
            pics = []
            try:
                for e in sorted(os.scandir(lp(p)), key=lambda e: e.name.lower()):
                    if e.is_file(follow_symlinks=False) and os.path.splitext(e.name)[1].lower() in IMG_EXT:
                        pics.append(e.path)
                        if len(pics) == 4:
                            break
            except OSError:
                pass
            ims = [im for im in (load_small(x, "image", s) for x in pics) if im is not None]
            if not ims:
                svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96"><rect width="96" height="96" fill="#232a38"/>'
                       '<path d="M12 28h26l8 8h38v40H12z" fill="#f5a524"/></svg>')
                return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "max-age=60"})
            mos = Image.new("RGB", (s, s), (20, 24, 32))
            if len(ims) == 1:
                mos.paste(ImageOps.fit(ims[0], (s, s)), (0, 0))
            else:
                h = s // 2
                for k, im in enumerate(ims):
                    mos.paste(ImageOps.fit(im, (h, h)), ((k % 2) * h, (k // 2) * h))
            os.makedirs(os.path.dirname(cf), exist_ok=True)
            mos.save(cf, "JPEG", quality=80)
        return send_file(cf, mimetype="image/jpeg", max_age=30)

    # ------------------------------------------------------------ personnes
    @app.get("/api/persons")
    def api_persons():
        a = request.args
        eng = a.get("engine") or S.db.get_setting("face_engine", "opencv")
        return jsonify(persons.list_persons(S.db, eng, int(a.get("page", 1)), min(200, int(a.get("per", 60))),
                                            a.get("q", "")))

    @app.get("/api/person_faces")
    def api_person_faces():
        a = request.args
        pid = a.get("id")
        page, per = int(a.get("page", 1)), min(300, int(a.get("per", 120)))
        if pid == "none":
            eng = a.get("engine") or S.db.get_setting("face_engine", "opencv")
            rows = S.db.q("SELECT id, file_id, locked, eligible, px FROM faces WHERE person_id IS NULL AND engine=? "
                          "ORDER BY px DESC LIMIT ? OFFSET ?", (eng, per, (page - 1) * per))
        else:
            rows = S.db.q("SELECT id, file_id, locked, eligible, px FROM faces WHERE person_id=? "
                          "ORDER BY locked DESC, px * score DESC LIMIT ? OFFSET ?", (int(pid), per, (page - 1) * per))
        return jsonify(faces=[dict(r) for r in rows])

    @app.post("/api/persons/rename")
    def api_person_rename():
        d = body()
        persons.rename(S.db, int(d["id"]), d.get("name", ""))
        return jsonify(ok=True)

    @app.post("/api/persons/merge")
    def api_person_merge():
        d = body()
        try:
            persons.merge(S.db, int(d["src"]), int(d["dst"]))
        except ValueError as e:
            return err(str(e))
        return jsonify(ok=True)

    @app.post("/api/faces/remove")
    def api_face_remove():
        for fid in body().get("ids", []):
            persons.remove_face(S.db, int(fid))
        return jsonify(ok=True)

    @app.post("/api/faces/assign")
    def api_face_assign():
        d = body()
        try:
            for fid in d.get("ids", []):
                persons.assign_face(S.db, int(fid), int(d["person"]))
        except ValueError as e:
            return err(str(e))
        return jsonify(ok=True)

    @app.post("/api/faces/confirm")
    def api_face_confirm():
        for fid in body().get("ids", []):
            persons.confirm_face(S.db, int(fid))
        return jsonify(ok=True)

    @app.post("/api/persons/recluster")
    def api_recluster():
        eng = body().get("engine") or S.db.get_setting("face_engine", "opencv")
        return jsonify(persons.recluster(S.db, eng))

    @app.post("/api/biometrics/clear")
    def api_bio_clear():
        if body().get("confirm") != "EFFACER":
            return err("Confirmation manquante")
        S.db.clear_biometrics()
        import shutil
        shutil.rmtree(os.path.join(S.thumbs, "visages"), ignore_errors=True)
        return jsonify(ok=True)

    # ------------------------------------------------------------ explorer
    @app.get("/api/search")
    def api_search():
        a = request.args
        filters = {k: a.get(k) for k in ("kind", "folder", "person", "object", "scene", "blur", "date_from", "date_to")
                   if a.get(k)}
        res = search.search(S.db, a.get("q", ""), filters, int(a.get("page", 1)), min(200, int(a.get("per", 60))),
                            S.clip() if a.get("q") else None)
        res["files"] = [f for f in res["files"] if S.scope.contains(f["path"])]
        return jsonify(res)

    @app.get("/api/facets")
    def api_facets():
        return jsonify(search.facets(S.db))

    # ------------------------------------------------------------ étalonnage
    @app.get("/api/calib/next")
    def api_calib_next():
        kind = request.args.get("kind", "dup")
        if kind == "face":
            p = calib.next_face_pair(S.db, S.db.get_setting("face_engine", "opencv"))
            if p:
                for k in ("a", "b"):
                    p[k + "_file"] = S.db.one("SELECT file_id FROM faces WHERE id=?", (p[k],))["file_id"]
        else:
            p = calib.next_dup_pair(S.db)
        return jsonify(pair=p, stats=calib.stats(S.db))

    @app.post("/api/calib/answer")
    def api_calib_answer():
        d = body()
        calib.answer(S.db, d["kind"], d["a"], d["b"], d.get("metrics", {}), bool(d.get("yes")))
        return jsonify(stats=calib.stats(S.db))

    @app.post("/api/calib/apply")
    def api_calib_apply():
        d = body()
        try:
            if d.get("kind") == "face":
                eng = S.db.get_setting("face_engine", "opencv")
                return jsonify(threshold=calib.apply_face(S.db, eng))
            return jsonify(params=calib.apply_dup(S.db))
        except ValueError as e:
            return err(str(e))

    return app


def file_kind(path):
    return kind_of(path)
