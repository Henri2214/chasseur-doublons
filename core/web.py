"""Serveur web local (Flask) : API + page unique.

Sécurité : écoute 127.0.0.1 par défaut, jeton de session obligatoire pour toute requête POST (actions),
contrôle de l'en-tête Host (protège contre le DNS rebinding), fichiers servis uniquement s'ils sont
indexés et dans le périmètre de la session.
"""
import hashlib
import importlib.util
import json
import os
import string
import threading
import time

from flask import Flask, Response, abort, jsonify, request, send_file
from PIL import Image, ImageOps

from . import __version__, actions, calib, docs, dupes, persons, search, similar
from .config import DATA_DIR, IMG_EXT, RIGOR_LABELS, WEB_EXT
from .db import Database
from .imaging import HEIF, RAW, cv2, load_small
from .models import LICENSE_INSIGHTFACE, ModelManager, gpu_adapters, providers
from .scanner import Scanner
from .util import Scope, kind_of, lp, norm


class State:
    def __init__(self, data_dir=DATA_DIR, models_dir=None):
        self.data_dir = str(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.db = Database(os.path.join(self.data_dir, "index.sqlite3"))
        self.mm = ModelManager(models_dir, self.db) if models_dir else ModelManager(db=self.db)
        # l'analyse précédente a-t-elle fait tomber le programme alors que le GPU était utilisé ?
        temoin = self.db.get_setting("analyse_en_cours")
        if temoin:
            self.db.x("DELETE FROM settings WHERE k='analyse_en_cours'")
            if temoin.get("gpu"):
                self.db.set_setting("use_gpu", "0")
                self.db.set_setting("gpu_note", "GPU désactivé automatiquement : la dernière analyse s'est arrêtée "
                                                "brutalement alors qu'il était utilisé. Calcul sur le processeur.")
        self.mm.gpu_note = self.db.get_setting("gpu_note", "") if not self.mm.use_gpu() else ""
        self.scanner = Scanner(self.db, self.mm)
        self.scope = Scope()
        self.scope.reset(self.known_roots())     # les dossiers déjà analysés restent consultables
        self.result = dupes.DupeResult()
        self.thumbs = os.path.join(self.data_dir, "miniatures")
        os.makedirs(self.thumbs, exist_ok=True)
        self.token = hashlib.sha256(os.urandom(32)).hexdigest()
        self.dupe_state = {"status": "idle", "error": None, "elapsed": 0}
        self.last_opts = {}
        self._clip = None
        self._clip_lock = threading.Lock()
        self._face_engines = {}
        self._face_lock = threading.Lock()
        self.last_seen = time.time()  # dernière requête reçue (arrêt automatique quand la page est fermée)
        self.pages = {}               # pages ouvertes : identifiant -> heure du dernier signe de vie
        self.zone_cache = {}          # (photo, zone) -> signature de la zone tracée
        self.psearch_refs = {}        # jeton -> {"engine": str, "embs": [vecteurs]} (photos de référence envoyées)

    def known_roots(self):
        """Dossiers déjà analysés avec cet outil et toujours présents : c'est le périmètre autorisé.

        L'index garde les visages, les objets et le texte de tous ces dossiers ; sans eux dans le périmètre,
        l'onglet Personnes afficherait des vignettes vides pour tout ce qui vient d'une analyse précédente."""
        out = []
        try:
            for r in self.db.q("SELECT root, MAX(id) m FROM scans WHERE root IS NOT NULL GROUP BY root ORDER BY m DESC"):
                if r["root"] and os.path.isdir(lp(r["root"])):
                    out.append(r["root"])
        except Exception:  # noqa
            pass
        return out[:200]

    def face_engine(self, name):
        """Moteur de visages (chargé une fois) ; lève RuntimeError si le modèle manque."""
        with self._face_lock:
            if name not in self._face_engines:
                from .faces import make_engine
                if name == "insightface" and not self.mm.license_ok("insightface"):
                    raise RuntimeError("Confirmez la licence InsightFace (Réglages) ou choisissez le moteur OpenCV.")
                st = self.mm.status()
                if not st["insightface" if name == "insightface" else "opencv_faces"]["available"]:
                    raise RuntimeError("Modèle de visages absent : téléchargez-le dans Réglages.")
                self._face_engines[name] = make_engine(self.mm, name)
            return self._face_engines[name]

    def auto_models(self):
        return str(self.db.get_setting("auto_models", "1")) not in ("0", "False", "false")

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
    app.config["MAX_CONTENT_LENGTH"] = 120 * 1024 * 1024
    from .config import RES_DIR
    static_dir = os.path.join(str(RES_DIR), "static")

    allowed_hosts = {"127.0.0.1", "localhost", "[::1]"}

    @app.before_request
    def guard():
        host = (request.host or "").rsplit(":", 1)[0]
        if host not in allowed_hosts and not app.config.get("ALLOW_ANY_HOST"):
            abort(403)
        jeton = request.headers.get("X-Token")
        if request.path == "/api/bye":      # envoyé par le navigateur à la fermeture : pas d'en-tête possible
            jeton = jeton or request.args.get("t")
        if request.method == "POST" and jeton != S.token:
            abort(403)
        S.last_seen = time.time()

    @app.get("/api/ping")
    def api_ping():
        """Signe de vie de la page (toutes les 15 s) ; sert aussi à reconnaître une instance déjà lancée."""
        p = request.args.get("p")
        if p:
            S.pages[p[:40]] = time.time()
        return jsonify(app="chasseur-doublons", version=__version__)

    def arreter(attendre=8):
        """Arrêt complet : analyse interrompue, index refermé proprement, processus terminé."""
        S.scanner.cancel = True
        t0 = time.time()
        while S.scanner.state["status"] == "running" and time.time() - t0 < attendre:
            time.sleep(0.1)
        try:
            S.db.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa
            pass
        time.sleep(0.3)                    # laisse partir la réponse HTTP
        app.config.get("EXIT", os._exit)(0)

    @app.post("/api/bye")
    def api_bye():
        """La page se ferme (onglet ou navigateur). Si aucune autre page n'est ouverte quelques secondes plus
        tard — un simple rechargement en rouvre une aussitôt —, l'application s'arrête complètement."""
        S.pages.pop((request.args.get("p") or "")[:40], None)
        if not app.config.get("ARRET_FERMETURE", True):
            return jsonify(ok=True, arret=False)
        depart = time.time()

        def verifier():
            time.sleep(float(app.config.get("DELAI_FERMETURE", 6)))
            while True:
                now = time.time()
                if any(t > depart for t in S.pages.values()):
                    return                 # une page a donné signe de vie depuis : rechargement ou autre onglet
                # une page en arrière-plan ne donne signe de vie qu'une fois par minute : on lui laisse 150 s
                # (c'est aussi le délai pour un onglet disparu sans prévenir)
                if not any(now - t < 150 for t in S.pages.values()):
                    break
                time.sleep(5)
            print("Navigateur fermé : arrêt de l'application.")
            arreter()
        threading.Thread(target=verifier, daemon=True).start()
        return jsonify(ok=True, arret=True)

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
            "video": cv2 is not None, "heif": HEIF, "raw": RAW, "pdf": docs.PDF, "ole": docs.OLE,
            "faces_insightface": st["insightface"]["available"], "faces_opencv": st["opencv_faces"]["available"],
            "objects": st["yolox_s"]["available"] or st["yolox_nano"]["available"],
            "clip": st["clip"]["available"] and importlib.util.find_spec("tokenizers") is not None,
            "ocr": importlib.util.find_spec("rapidocr_onnxruntime") is not None,
            "onnx": importlib.util.find_spec("onnxruntime") is not None,
            "providers": providers(), "fts": S.db.fts,
            "gpu": (S.mm.gpu_providers() or [""])[0], "gpu_available": next((p for p in providers() if p != "CPUExecutionProvider"), ""),
            "use_gpu": S.mm.use_gpu(), "gpu_note": S.mm.gpu_note,
            "gpu_devices": gpu_adapters(), "gpu_device": S.db.get_setting("gpu_device", "auto"),
            "gpu_device_used": S.mm.gpu_device(),
            "auto_models": S.auto_models(),
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
        if "gpu_device" in d:
            S.db.set_setting("gpu_device", "auto" if d["gpu_device"] == "auto" else int(d["gpu_device"]))
            d.setdefault("use_gpu", S.mm.use_gpu())
        if "use_gpu" in d:
            S.db.set_setting("use_gpu", "1" if d["use_gpu"] else "0")
            if d.get("retry_gpu"):               # oublier les essais en échec (nouveau pilote, autre carte)
                S.db.x("DELETE FROM settings WHERE k LIKE 'gpu_probe:%'")
            S.mm.gpu_note = ""
            S.db.x("DELETE FROM settings WHERE k='gpu_note'")
            S.mm.reset_sessions()
            S.scanner._engines.clear()
            S._face_engines.clear()
            S._clip = None
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

    @app.post("/api/models/auto")
    def api_model_auto():
        """Active/désactive le téléchargement automatique et lance ce qui manque."""
        d = body()
        if "enabled" in d:
            S.db.set_setting("auto_models", "1" if d["enabled"] else "0")
        started = S.mm.auto_download() if S.auto_models() else []
        return jsonify(started=started, auto_models=S.auto_models())

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
            S.scope.reset([root] + S.known_roots())   # le dossier analysé + ceux des analyses précédentes
            S.result.set([], {})
            S.last_opts = dict(opts, root=root)
            S.scanner.start(root, opts)
        except RuntimeError as e:
            return err(str(e), 409)
        return jsonify(ok=True)

    @app.post("/api/quit")
    def api_quit():
        """Arrête complètement l'application (serveur, analyse en cours, processus auxiliaires)."""
        S.scanner.cancel = True
        threading.Thread(target=arreter, daemon=True).start()
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
                       dupes=S.dupe_state, has_results=bool(S.result.groups),
                       summary={k: (st.get("stats") or {}).get(k) for k in
                                ("files", "new", "changed", "unchanged", "removed", "to_analyse", "docs_new",
                                 "docs_text")})

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
        return jsonify(dupes.page(S.db, S.result, int(a.get("page", 1)), min(500, int(a.get("per", 20))),
                                  a.get("sort", "waste"), a.get("kind", "all")))

    @app.get("/api/group_ids")
    def api_group_ids():
        """Identifiants et métadonnées légères de tous les groupes (pour la sélection automatique)."""
        out = []
        kind = request.args.get("kind", "all")
        for g in S.result.visible():
            if kind != "all" and not (g["kind"] == kind or g["media"] == kind):
                continue
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
        ext = os.path.splitext(r["path"])[1].lower()
        if r["kind"] == "image" and ext not in WEB_EXT:
            return converted_jpeg(r, 2560)        # HEIC, AVIF, TIFF, RAW... : le navigateur ne sait pas les afficher
        if r["kind"] in ("image", "video"):
            return send_file(lp(r["path"]), conditional=True)
        if ext == ".pdf":
            return send_file(lp(r["path"]), conditional=True, mimetype="application/pdf")
        # tout autre fichier est proposé en téléchargement : un .html affiché ici pourrait piloter l'interface
        return send_file(lp(r["path"]), as_attachment=True, download_name=os.path.basename(r["path"]),
                         mimetype="application/octet-stream")

    def converted_jpeg(r, s):
        key = hashlib.sha1(("%s|%s|%s|%s" % (norm(r["path"]), r["mtime"], r["size"], s)).encode()).hexdigest()
        cf = os.path.join(S.thumbs, key[:2], key + ".jpg")
        if not os.path.exists(cf):
            im = load_small(r["path"], "image", s)
            if im is None:
                abort(415)
            os.makedirs(os.path.dirname(cf), exist_ok=True)
            im.save(cf + ".tmp", "JPEG", quality=90)
            os.replace(cf + ".tmp", cf)
        return send_file(cf, mimetype="image/jpeg", max_age=86400)

    def pdf_page(path, s):
        """Première page d'un PDF, pour la miniature."""
        try:
            with docs._PDF_LOCK:
                pdf = docs.pdfium.PdfDocument(lp(path))
                try:
                    page = pdf[0]
                    im = page.render(scale=s / max(page.get_width(), page.get_height(), 1.0)).to_pil().convert("RGB")
                    page.close()
                finally:
                    pdf.close()
            return im
        except Exception:  # noqa
            return None

    def doc_page(fid, ext, s):
        d = S.db.one("SELECT substr(text, 1, 6000) t, title FROM doctext WHERE file_id=?", (fid,))
        if not d or not (d["t"] or "").strip():
            return None                        # contenu pas encore lu, ou document sans texte
        try:
            return docs.text_thumbnail(d["t"], ext, s, d["title"])
        except Exception:  # noqa
            return None

    def placeholder(label):
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#d5dce8"/>'
               '<text x="128" y="140" font-family="sans-serif" font-size="44" fill="#5d6a80" text-anchor="middle">%s</text></svg>'
               % label[:6].upper().replace("<", ""))
        return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "max-age=3600"})

    @app.get("/api/thumb")
    def api_thumb():
        r = S.db.one("SELECT id, path, kind, size, mtime FROM files WHERE id=?", (int(request.args.get("id", 0)),))
        if not r or not S.scope.contains(r["path"]) or not os.path.isfile(lp(r["path"])):
            return placeholder("absent")       # jamais de vignette cassée dans la grille
        s = max(64, min(int(request.args.get("s", 320)), 800))
        ext = os.path.splitext(r["path"])[1].lower()
        if r["kind"] == "other":
            return placeholder(ext.lstrip(".") or "fichier")
        key = hashlib.sha1(("%s|%s|%s|%s|v2" % (norm(r["path"]), r["mtime"], r["size"], s)).encode()).hexdigest()
        cf = os.path.join(S.thumbs, key[:2], key + ".jpg")
        if not os.path.exists(cf):
            if r["kind"] == "doc":
                im = pdf_page(r["path"], s) if ext == ".pdf" and docs.PDF else None
                if im is None:                 # Word, Excel, courriel, texte... : aperçu du début du texte
                    im = doc_page(r["id"], ext, s)
            else:
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
        if not f or not S.scope.contains(f["path"]) or not os.path.isfile(lp(f["path"])):
            return placeholder("?")            # photo disparue ou hors des dossiers analysés : pas d'image cassée
        key = hashlib.sha1(("F|%d|%s|%s" % (fid, norm(f["path"]), f["mtime"])).encode()).hexdigest()
        cf = os.path.join(S.thumbs, "visages", key + ".jpg")
        if not os.path.exists(cf):
            im = load_small(f["path"], "image", 1280)
            if im is None:
                return placeholder("?")
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
                       ocr=(S.db.one("SELECT text FROM ocr WHERE file_id=?", (fid,)) or {"text": ""})["text"],
                       doc=doc_info(fid, r["path"]) if r["kind"] == "doc" else None)

    def doc_info(fid, path):
        d = S.db.one("SELECT substr(text, 1, 60000) text, length(text) chars, words, method, pages, title, sender, "
                     "mdate, extra FROM doctext WHERE file_id=?", (fid,))
        fam = docs.FAMILY_LABELS.get(docs.family(os.path.splitext(path)[1]), "Document")
        if not d:
            return {"family": fam, "text": "", "words": 0, "method": None}
        out = dict(d, family=fam)
        try:
            out["extra"] = json.loads(d["extra"]) if d["extra"] else None
        except ValueError:
            out["extra"] = None
        err_ = S.db.one("SELECT error FROM files WHERE id=?", (fid,))
        out["error"] = err_["error"] if err_ else None
        return out

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
        page, per = int(a.get("page", 1)), min(1000, int(a.get("per", 120)))
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
            persons.assign_faces(S.db, d.get("ids", []), int(d["person"]))
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
        filters = {k: a.get(k) for k in ("kind", "folder", "person", "object", "scene", "blur", "date_from", "date_to", "sort")
                   if a.get(k)}
        res = search.search(S.db, a.get("q", ""), filters, int(a.get("page", 1)), min(1000, int(a.get("per", 60))),
                            S.clip() if a.get("q") else None)
        res["files"] = [f for f in res["files"] if S.scope.contains(f["path"])]
        return jsonify(res)

    def in_scope_ids(ids):
        """Ne garde que les fichiers du périmètre de la session (même contrôle que /api/search)."""
        out = []
        for i in range(0, len(ids), 900):
            ch = ids[i:i + 900]
            ok = {r["id"] for r in S.db.q("SELECT id, path FROM files WHERE id IN (%s)" % ",".join("?" * len(ch)), ch)
                  if S.scope.contains(r["path"])}
            out += [x for x in ch if x in ok]
        return out

    @app.get("/api/search_ids")
    def api_search_ids():
        """Identifiants de TOUS les résultats d'une recherche (bouton « Tout sélectionner »), sans pagination."""
        a = request.args
        filters = {k: a.get(k) for k in ("kind", "folder", "person", "object", "scene", "blur", "date_from", "date_to", "sort")
                   if a.get(k)}
        res = search.search(S.db, a.get("q", ""), filters, 1, 1, S.clip() if a.get("q") else None, ids_only=True)
        ids = in_scope_ids(res["ids"])
        return jsonify(ids=ids[:100000], total=len(ids), truncated=len(ids) > 100000)

    @app.get("/api/facets")
    def api_facets():
        return jsonify(search.facets(S.db))

    # ------------------------------------------------------------ photos similaires (clic sur une photo)
    def run_similar(a):
        fid = int(a.get("id", 0))
        thr = float(a.get("thr") or 0.55)
        src = [x for x in (a.get("sources") or "").split(",") if x] or None
        res = similar.similar(S.db, fid, thr, src)
        ok = set(in_scope_ids([r[0] for r in res]))
        return fid, [r for r in res if r[0] in ok]

    @app.get("/api/similar")
    def api_similar():
        a = request.args
        try:
            fid, res = run_similar(a)
        except ValueError as e:
            return err(str(e))
        page, per = max(1, int(a.get("page", 1))), min(1000, int(a.get("per", 60)))
        sl = res[(page - 1) * per: page * per]
        h = search._hydrate(S.db, [r[0] for r in sl], len(res), page, per, {}, [])
        by = {r[0]: r for r in sl}
        for f in h["files"]:
            _, sim_, srcs = by[f["id"]]
            f.update(sim=sim_, why=[similar.LABELS.get(x, x) for x in srcs])
        ref = S.db.one("SELECT id, path, name FROM files WHERE id=?", (fid,))
        h.update(ref={"id": fid, "name": ref["name"] if ref else "", "path": ref["path"] if ref else ""},
                 sources=sorted({x for r in res for x in r[2]}))
        return jsonify(h)

    def zone_sig(fid, box):
        """Signature de la zone (objets, CLIP, visage), mise en cache : la pagination ne recalcule rien."""
        r = file_row(fid)
        if r["kind"] != "image":
            raise ValueError("La recherche par zone ne s'applique qu'aux images.")
        x1, y1, x2, y2 = [min(1.0, max(0.0, float(v))) for v in box]
        if x2 - x1 < 0.02 or y2 - y1 < 0.02:
            raise ValueError("Zone trop petite : tracez un rectangle plus grand.")
        key = (fid, r["mtime"], round(x1, 3), round(y1, 3), round(x2, 3), round(y2, 3), S.db.get_setting("face_engine", "opencv"))
        if key in S.zone_cache:
            return S.zone_cache[key]
        from .imaging import decode
        dec = decode(r["path"], 2000)
        W, H = dec.rgb.size
        crop = dec.rgb.crop((int(x1 * W), int(y1 * H), max(int(x1 * W) + 8, int(x2 * W)), max(int(y1 * H) + 8, int(y2 * H))))
        yolo = None
        for v in ("yolox_s", "yolox_nano"):
            if S.mm.available(v):
                try:
                    from .objects import YoloX
                    yolo = S.scanner._engine(("yolo", v), lambda v=v: YoloX(S.mm, v))
                except Exception:
                    yolo = None
                break
        eng_name = S.db.get_setting("face_engine", "opencv")
        try:
            feng = S.face_engine(eng_name)
        except Exception:
            feng = None
        sig = similar.zone_signature(crop, yolo, S.clip(), feng)
        sig["engine"] = eng_name
        sig["means"] = [n for n, ok in (("objets", yolo is not None), ("contenu (CLIP)", sig["clip"] is not None),
                                         ("visages", feng is not None)) if ok]
        if len(S.zone_cache) > 30:
            S.zone_cache.pop(next(iter(S.zone_cache)))
        S.zone_cache[key] = sig
        return sig

    @app.post("/api/zone_search")
    def api_zone_search():
        d = body()
        try:
            fid = int(d.get("id", 0))
            sig = zone_sig(fid, d.get("box") or [0, 0, 1, 1])
            src = [x for x in (d.get("sources") or "").split(",") if x and x != "visuel"] or None
            res = similar.zone_search(S.db, sig, sig["engine"], float(d.get("thr") or 0.45), src)
        except ValueError as e:
            return err(str(e))
        ok = set(in_scope_ids([r[0] for r in res]))
        res = [r for r in res if r[0] in ok]
        found = {"labels": [l for l, _ in sig["labels"]], "face": sig["face"] is not None,
                 "clip": sig["clip"] is not None, "means": sig["means"]}
        if d.get("ids_only"):
            return jsonify(ids=[r[0] for r in res], total=len(res))
        page, per = max(1, int(d.get("page", 1))), min(1000, int(d.get("per", 60)))
        sl = res[(page - 1) * per: page * per]
        h = search._hydrate(S.db, [r[0] for r in sl], len(res), page, per, {}, [])
        by = {r[0]: r for r in sl}
        for f in h["files"]:
            _, sim_, srcs = by[f["id"]]
            f.update(sim=sim_, why=[similar.LABELS.get(x, x) for x in srcs])
        h.update(found=found, sources=sorted({x for r in res for x in r[2]}))
        return jsonify(h)

    @app.get("/api/similar_ids")
    def api_similar_ids():
        try:
            fid, res = run_similar(request.args)
        except ValueError as e:
            return err(str(e))
        return jsonify(ids=[r[0] for r in res], total=len(res))

    # ------------------------------------------------------------ recherche d'une personne (visage de référence)
    def resolve_ref(d):
        """(vecteur, moteur, personne_de_référence|None) depuis une personne, un visage indexé ou une photo envoyée."""
        if d.get("person") not in (None, ""):
            pid = int(d["person"])
            p = S.db.one("SELECT engine FROM persons WHERE id=?", (pid,))
            v = persons.person_reference(S.db, pid) if p else None
            if v is None:
                raise ValueError("Cette personne n'a aucun visage indexé.")
            return v, p["engine"], pid
        if d.get("face") not in (None, ""):
            v, eng = persons.face_reference(S.db, int(d["face"]))
            if v is None:
                raise ValueError("Visage introuvable.")
            return v, eng, None
        ref = S.psearch_refs.get(d.get("ref"))
        if ref is None:
            raise ValueError("Photo de référence expirée : renvoyez-la.")
        i = int(d.get("idx", 0))
        if not 0 <= i < len(ref["embs"]):
            raise ValueError("Visage de référence inconnu.")
        return ref["embs"][i], ref["engine"], None

    def run_match(d):
        v, eng, ref_pid = resolve_ref(d)
        thr = float(d.get("thr") or persons.threshold(S.db, eng))
        m = persons.match_faces(S.db, eng, v, thr, bool(d.get("include_small")))
        best = persons.best_per_file(m)
        ok = set(in_scope_ids([b[0] for b in best]))
        return [b for b in best if b[0] in ok], eng, ref_pid, thr, len(m)

    @app.post("/api/psearch/detect")
    def api_psearch_detect():
        f = request.files.get("photo")
        if not f or not f.filename:
            return err("Aucune photo envoyée.")
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in IMG_EXT:
            return err("Format non pris en charge : %s" % (ext or "?"))
        eng_name = request.form.get("engine") or S.db.get_setting("face_engine", "opencv")
        try:
            eng = S.face_engine(eng_name)
        except RuntimeError as e:
            return err(str(e), 409)
        import tempfile
        from .imaging import decode
        fd, tmp = tempfile.mkstemp(suffix=ext)
        try:
            with os.fdopen(fd, "wb") as fh:
                f.save(fh)
            dec = decode(tmp, 1600)
            faces = eng.analyse(dec.bgr())
        except Exception as e:  # noqa
            return err("Photo illisible : %s" % e)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        faces = [x for x in faces if x.get("emb") is not None]
        faces.sort(key=lambda x: -(x["box"][2] - x["box"][0]) * (x["box"][3] - x["box"][1]))
        faces = faces[:12]
        if not faces:
            return err("Aucun visage détecté sur cette photo.")
        import base64
        import io
        out = []
        for i, x in enumerate(faces):
            x1, y1, x2, y2 = [int(v) for v in x["box"]]
            m = int(0.25 * max(x2 - x1, y2 - y1))
            crop = dec.rgb.crop((max(0, x1 - m), max(0, y1 - m), min(dec.rgb.size[0], x2 + m), min(dec.rgb.size[1], y2 + m)))
            crop.thumbnail((128, 128))
            buf = io.BytesIO()
            crop.save(buf, "JPEG", quality=85)
            out.append({"idx": i, "px": int(max(x2 - x1, y2 - y1)), "score": round(float(x["score"]), 3),
                        "thumb": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()})
        token = hashlib.sha1(os.urandom(16)).hexdigest()
        if len(S.psearch_refs) > 20:
            S.psearch_refs.pop(next(iter(S.psearch_refs)))
        embs = [(x["emb"] / (float((x["emb"] ** 2).sum()) ** 0.5 + 1e-9)).astype("float32") for x in faces]
        S.psearch_refs[token] = {"engine": eng_name, "embs": embs}
        return jsonify(ref=token, engine=eng_name, faces=out, threshold=persons.threshold(S.db, eng_name))

    @app.post("/api/psearch/run")
    def api_psearch_run():
        d = body()
        try:
            res, eng, ref_pid, thr, nfaces = run_match(d)
        except ValueError as e:
            return err(str(e))
        if d.get("ids_only"):
            return jsonify(ids=[r[0] for r in res], total=len(res))
        page, per = max(1, int(d.get("page", 1))), min(1000, int(d.get("per", 60)))
        sl = res[(page - 1) * per: page * per]
        h = search._hydrate(S.db, [r[0] for r in sl], len(res), page, per, {}, ["visage"])
        names = {r["id"]: r["name"] for r in S.db.q("SELECT id, name FROM persons WHERE engine=?", (eng,))}
        by_file = {r[0]: r for r in sl}
        for f in h["files"]:
            _, face_id, pid, sim = by_file[f["id"]]
            f.update(sim=round(sim, 3), face_id=face_id, person_id=pid,
                     person_name=(names.get(pid) or "Sans nom") if pid else None,
                     known=bool(ref_pid and pid == ref_pid))
        h.update(threshold=thr, engine=eng, matched_faces=nfaces,
                 sims=[round(r[3], 3) for r in res[:3]])
        return jsonify(h)

    @app.post("/api/psearch/assign")
    def api_psearch_assign():
        """Affecte à une personne les visages trouvés (tous, ou ceux des photos données)."""
        d = body()
        try:
            res, eng, ref_pid, thr, _ = run_match(d)
        except ValueError as e:
            return err(str(e))
        target = d.get("to")
        if d.get("new_name") is not None:
            target = persons.create_person(S.db, eng, d["new_name"])
        elif target in (None, ""):
            target = ref_pid
        if target in (None, ""):
            return err("Choisissez la personne à laquelle affecter les visages.")
        target = int(target)
        only = set(int(i) for i in d["file_ids"]) if d.get("file_ids") else None
        try:
            n = persons.assign_faces(S.db, [face_id for file_id, face_id, pid, sim in res
                                            if pid != target and (only is None or file_id in only)], target)
        except ValueError as e:
            return err(str(e))
        return jsonify(assigned=n, person=target)

    @app.post("/api/persons/create")
    def api_person_create():
        d = body()
        eng = d.get("engine") or S.db.get_setting("face_engine", "opencv")
        return jsonify(id=persons.create_person(S.db, eng, d.get("name", "")))

    # ------------------------------------------------------------ étalonnage
    def calib_roots():
        """Dossier de la session d'analyse : l'étalonnage ne propose que des photos visibles ici."""
        r = S.scanner.state.get("root") or S.last_opts.get("root")
        return [r] if r else []

    @app.get("/api/calib/next")
    def api_calib_next():
        kind = request.args.get("kind", "dup")
        if kind == "face":
            p = calib.next_face_pair(S.db, S.db.get_setting("face_engine", "opencv"), calib_roots())
            if p:
                for k in ("a", "b"):
                    p[k + "_file"] = S.db.one("SELECT file_id FROM faces WHERE id=?", (p[k],))["file_id"]
        else:
            p = calib.next_dup_pair(S.db, roots=calib_roots())
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
