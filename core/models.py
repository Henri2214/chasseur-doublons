"""Gestionnaire de modèles ONNX : téléchargement à la demande, vérification, sessions partagées.

- téléchargement dans un fichier .part puis renommage, vérification de la taille ;
- progression consultable par l'interface ;
- les modèles InsightFace (recherche non commerciale) exigent une confirmation explicite de licence ;
- sessions onnxruntime partagées, threads intra-op limités, fournisseurs CUDA > DirectML > CPU.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
import zipfile

from .config import MODELS_DIR, WORKERS

try:
    import onnxruntime as ort
except ImportError:
    ort = None

INSIGHTFACE_ZIP = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
HF_CLIP = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/"

LICENSE_INSIGHTFACE = ("Modèles InsightFace (buffalo_l) : usage limité à la recherche NON COMMERCIALE. "
                       "Ils ne sont jamais inclus dans le dépôt ; vous les téléchargez vous-même.")

# name: fichiers produits, source, taille attendue (octets) des fichiers finaux, licence
REGISTRY = {
    "insightface": {
        "label": "Visages précis (InsightFace SCRFD + ArcFace)",
        "files": {"det_10g.onnx": 16923827, "w600k_r50.onnx": 174383860, "2d106det.onnx": 5030888},
        "zip": INSIGHTFACE_ZIP, "zip_size": 288621354,
        "license": "Recherche non commerciale uniquement (InsightFace)", "confirm": True,
    },
    "opencv_faces": {
        "label": "Visages licence libre (OpenCV YuNet + SFace)",
        "files": {"face_detection_yunet_2023mar.onnx": 232589, "face_recognition_sface_2021dec.onnx": 38696353},
        "urls": {"face_detection_yunet_2023mar.onnx":
                 "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx",
                 "face_recognition_sface_2021dec.onnx":
                 "https://huggingface.co/opencv/face_recognition_sface/resolve/main/face_recognition_sface_2021dec.onnx"},
        "license": "MIT (YuNet) / Apache-2.0 (SFace)", "confirm": False,
    },
    "yolox_s": {
        "label": "Objets (YOLOX-S, 80 classes)",
        "files": {"yolox_s.onnx": 35858002},
        "urls": {"yolox_s.onnx": "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx"},
        "license": "Apache-2.0", "confirm": False,
    },
    "yolox_nano": {
        "label": "Objets léger (YOLOX-Nano)",
        "files": {"yolox_nano.onnx": 3659407},
        "urls": {"yolox_nano.onnx": "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.onnx"},
        "license": "Apache-2.0", "confirm": False,
    },
    "clip": {
        "label": "Recherche en langage naturel et scènes (CLIP ViT-B/32, quantifié)",
        "files": {"clip/vision_model_quantized.onnx": 89117001, "clip/text_model_quantized.onnx": 64504507,
                  "clip/tokenizer.json": 2224119},
        "urls": {"clip/vision_model_quantized.onnx": HF_CLIP + "onnx/vision_model_quantized.onnx",
                 "clip/text_model_quantized.onnx": HF_CLIP + "onnx/text_model_quantized.onnx",
                 "clip/tokenizer.json": HF_CLIP + "tokenizer.json"},
        "license": "MIT (OpenAI CLIP)", "confirm": False,
    },
}


# Modèles téléchargés automatiquement au premier lancement (licences libres uniquement).
RECOMMENDED = ("opencv_faces", "yolox_s", "clip")


def providers():
    """Fournisseurs onnxruntime disponibles, dans l'ordre CUDA, DirectML, CPU."""
    if ort is None:
        return []
    avail = ort.get_available_providers()
    return [p for p in ("CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider") if p in avail]


GPU_PROVIDERS = ("CUDAExecutionProvider", "DmlExecutionProvider")
# Seuls ces modèles vont sur le GPU : taille d'entrée fixe, précision flottante.
# Les modèles CLIP « quantized » (entiers 8 bits) sont conçus pour le processeur : sur DirectML ils
# n'apportent rien et provoquent une violation d'accès (plantage constaté en 2.3.1).
GPU_MODELS = {"yolox_s.onnx", "yolox_nano.onnx", "det_10g.onnx", "w600k_r50.onnx", "2d106det.onnx"}
_GPU_LOCK = threading.Lock()     # une seule exécution GPU à la fois, toutes sessions confondues

# Essai du GPU dans un processus séparé : un pilote graphique défaillant fait planter le processus entier
# (aucune exception Python), il ne faut donc jamais faire le premier essai dans l'application elle-même.
_PROBE = r"""
import sys, json, numpy as np, onnxruntime as ort
path, provs = sys.argv[1], json.loads(sys.argv[2])
so = ort.SessionOptions(); so.log_severity_level = 3
if "DmlExecutionProvider" in provs:
    so.enable_mem_pattern = False
    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
s = ort.InferenceSession(path, sess_options=so, providers=provs)
if s.get_providers()[0] == "CPUExecutionProvider":
    sys.exit(3)
feed = {}
for i in s.get_inputs():
    shp = [d if isinstance(d, int) and d > 0 else None for d in i.shape]
    if len(shp) == 4:
        shp = [shp[0] or 1, shp[1] or 3, shp[2] or 640, shp[3] or 640]
    else:
        shp = [d or (1 if k == 0 else 16) for k, d in enumerate(shp)]
    feed[i.name] = np.zeros(shp, np.int64 if "int64" in i.type else np.int32 if "int32" in i.type else np.float32)
for _ in range(2):
    s.run(None, feed)
"""


class LockedSession:
    """Session GPU : DirectML n'accepte pas plusieurs run() simultanés sur une même session (plantage
    du processus). Les appels sont donc mis en file ; le GPU traite de toute façon une image à la fois."""

    gpu = True

    def __init__(self, sess):
        self._s = sess
        self._l = _GPU_LOCK

    def run(self, *a, **k):
        with self._l:
            return self._s.run(*a, **k)

    def __getattr__(self, name):
        return getattr(self._s, name)


class ModelManager:
    def __init__(self, root=MODELS_DIR, db=None):
        self.root = str(root)
        self.db = db
        self.progress = {}          # name -> {done, total, status, error}
        self._sessions = {}
        self._lock = threading.Lock()
        self.gpu_note = ""          # explication affichée dans Réglages quand le GPU n'est pas utilisé

    # ------------------------------------------------------------ GPU
    def use_gpu(self):
        if os.environ.get("CD_FORCE_CPU"):
            return False
        return not self.db or str(self.db.get_setting("use_gpu", "1")) not in ("0", "False", "false")

    def gpu_providers(self):
        return [p for p in providers() if p in GPU_PROVIDERS] if self.use_gpu() else []

    def _gpu_safe(self, path, provs):
        """Vrai si le modèle tourne sur le GPU dans un processus d'essai (résultat mémorisé)."""
        key = "gpu_probe:%s:%s:%s:%s" % (os.path.basename(path), os.path.getsize(path),
                                         getattr(ort, "__version__", "?"), provs[0])
        if self.db:
            v = self.db.get_setting(key)
            if v is not None:
                if not v:
                    self.gpu_note = "GPU écarté après un essai en échec (%s) : calcul sur le processeur." % provs[0]
                return bool(v)
        try:
            r = subprocess.run([sys.executable, "-c", _PROBE, path, json.dumps(provs)], capture_output=True,
                               timeout=180)
            ok = r.returncode == 0
        except Exception:
            ok = False
        if not ok:
            self.gpu_note = "GPU écarté après un essai en échec (%s) : calcul sur le processeur." % provs[0]
        if self.db:
            self.db.set_setting(key, bool(ok))
        return ok

    # ------------------------------------------------------------ état
    def path(self, rel):
        return os.path.join(self.root, *rel.split("/"))

    def available(self, name):
        spec = REGISTRY[name]
        for rel, size in spec["files"].items():
            p = self.path(rel)
            if not os.path.isfile(p) or (size and os.path.getsize(p) != size):
                return False
        return True

    def license_ok(self, name):
        if not REGISTRY[name].get("confirm"):
            return True
        return bool(self.db and self.db.get_setting("license_ok_" + name))

    def confirm_license(self, name):
        if self.db:
            self.db.set_setting("license_ok_" + name, True)

    def status(self):
        out = {}
        for name, spec in REGISTRY.items():
            out[name] = {"label": spec["label"], "license": spec["license"], "confirm": spec.get("confirm", False),
                         "license_ok": self.license_ok(name), "available": self.available(name),
                         "size": sum(s or 0 for s in spec["files"].values()),
                         "progress": self.progress.get(name)}
        return out

    # ------------------------------------------------------------ téléchargement
    def auto_download(self, names=None):
        """Lance en arrière-plan le téléchargement des modèles conseillés qui manquent.

        InsightFace en est exclu : sa licence (recherche non commerciale) exige un accord explicite."""
        started = []
        for name in (names or RECOMMENDED):
            try:
                if self.available(name) or not self.license_ok(name):
                    continue
                cur = self.progress.get(name)
                if cur and cur.get("status") == "running":
                    continue
                self.download(name)
                started.append(name)
            except Exception:       # un modèle indisponible ne doit jamais empêcher le démarrage
                continue
        return started

    def download(self, name, background=True):
        if name not in REGISTRY:
            raise KeyError(name)
        if not self.license_ok(name):
            raise PermissionError("Confirmez d'abord la licence de ce modèle.")
        cur = self.progress.get(name)
        if cur and cur.get("status") == "running":
            return
        self.progress[name] = {"done": 0, "total": 0, "status": "running", "error": None}
        if background:
            threading.Thread(target=self._download, args=(name,), daemon=True).start()
        else:
            self._download(name)

    def _fetch(self, url, dest, expected, prog):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        part = dest + ".part"
        req = urllib.request.Request(url, headers={"User-Agent": "chasseur-doublons"})
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as fh:
            total = int(r.headers.get("Content-Length") or 0) or (expected or 0)
            prog["total"] += total
            got = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                prog["done"] += len(chunk)
        if expected and got != expected:
            os.remove(part)
            raise IOError("taille inattendue pour %s (%d au lieu de %d octets)" % (os.path.basename(dest), got, expected))
        if total and got != total:
            os.remove(part)
            raise IOError("téléchargement incomplet : %s" % os.path.basename(dest))
        os.replace(part, dest)

    def _download(self, name):
        spec, prog = REGISTRY[name], self.progress[name]
        try:
            if "zip" in spec:
                tmp = self.path(name + ".zip")
                self._fetch(spec["zip"], tmp, spec.get("zip_size"), prog)
                try:
                    with zipfile.ZipFile(tmp) as z:
                        members = {os.path.basename(m): m for m in z.namelist()}
                        for rel, size in spec["files"].items():
                            m = members.get(os.path.basename(rel))
                            if not m:
                                raise IOError("%s absent de l'archive" % rel)
                            dest = self.path(rel)
                            with z.open(m) as src, open(dest + ".part", "wb") as out:
                                shutil.copyfileobj(src, out, 1 << 20)
                            if size and os.path.getsize(dest + ".part") != size:
                                os.remove(dest + ".part")
                                raise IOError("taille inattendue pour %s" % rel)
                            os.replace(dest + ".part", dest)
                finally:
                    if os.path.exists(tmp):
                        os.remove(tmp)
            else:
                for rel, url in spec["urls"].items():
                    if os.path.isfile(self.path(rel)) and (not spec["files"][rel] or
                                                           os.path.getsize(self.path(rel)) == spec["files"][rel]):
                        continue
                    self._fetch(url, self.path(rel), spec["files"][rel], prog)
            prog["status"] = "done"
            with self._lock:   # forcer le rechargement des sessions
                for k in list(self._sessions):
                    if k.startswith(name):
                        del self._sessions[k]
        except Exception as e:  # noqa
            prog["status"] = "error"
            prog["error"] = str(e)

    def delete(self, name):
        for rel in REGISTRY[name]["files"]:
            p = self.path(rel)
            if os.path.isfile(p):
                os.remove(p)
        with self._lock:
            for k in list(self._sessions):
                if k.startswith(name):
                    del self._sessions[k]

    # ------------------------------------------------------------ sessions
    def session(self, name, rel):
        """Session onnxruntime partagée (thread-safe pour run())."""
        key = name + ":" + rel
        with self._lock:
            s = self._sessions.get(key)
            if s is not None:
                return s
            if ort is None:
                raise RuntimeError("onnxruntime n'est pas installé")
            if not self.available(name):
                raise FileNotFoundError("Modèle « %s » absent : téléchargez-le dans l'onglet Réglages." %
                                        REGISTRY[name]["label"])
            so = ort.SessionOptions()
            so.intra_op_num_threads = max(1, (os.cpu_count() or 2) // WORKERS)
            so.inter_op_num_threads = 1
            # pas d'arène mémoire : sinon chaque exécution concurrente conserve ses tampons (RAM x threads)
            so.enable_cpu_mem_arena = False
            so.log_severity_level = 3
            s = None
            gpu = self.gpu_providers() if os.path.basename(rel) in GPU_MODELS else []
            if gpu and self._gpu_safe(self.path(rel), gpu + ["CPUExecutionProvider"]):
                try:
                    if "DmlExecutionProvider" in gpu:      # exigences de DirectML
                        so.enable_mem_pattern = False
                        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                    g = ort.InferenceSession(self.path(rel), sess_options=so, providers=gpu + ["CPUExecutionProvider"])
                    if g.get_providers()[0] in GPU_PROVIDERS:
                        s = LockedSession(g)
                    else:
                        s = g
                except Exception as e:  # noqa
                    self.gpu_note = "GPU indisponible pour %s (%s) : calcul sur le processeur." % (rel, e)
                    s = None
            if s is None:
                so = ort.SessionOptions()
                so.intra_op_num_threads = max(1, (os.cpu_count() or 2) // WORKERS)
                so.inter_op_num_threads = 1
                so.enable_cpu_mem_arena = False
                so.log_severity_level = 3
                s = ort.InferenceSession(self.path(rel), sess_options=so, providers=["CPUExecutionProvider"])
            self._sessions[key] = s
            return s

    def reset_sessions(self):
        with self._lock:
            self._sessions.clear()
