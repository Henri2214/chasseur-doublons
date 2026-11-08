"""Gestionnaire de modèles ONNX : téléchargement à la demande, vérification, sessions partagées.

- téléchargement dans un fichier .part puis renommage, vérification de la taille ;
- progression consultable par l'interface ;
- les modèles InsightFace (recherche non commerciale) exigent une confirmation explicite de licence ;
- sessions onnxruntime partagées, threads intra-op limités, fournisseurs CUDA > DirectML > CPU.
"""
import os
import shutil
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


def providers():
    """Fournisseurs onnxruntime disponibles, dans l'ordre CUDA, DirectML, CPU."""
    if ort is None:
        return []
    avail = ort.get_available_providers()
    return [p for p in ("CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider") if p in avail]


class ModelManager:
    def __init__(self, root=MODELS_DIR, db=None):
        self.root = str(root)
        self.db = db
        self.progress = {}          # name -> {done, total, status, error}
        self._sessions = {}
        self._lock = threading.Lock()

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
            provs = providers()
            try:
                s = ort.InferenceSession(self.path(rel), sess_options=so, providers=provs)
            except Exception:
                s = ort.InferenceSession(self.path(rel), sess_options=so, providers=["CPUExecutionProvider"])
            self._sessions[key] = s
            return s
