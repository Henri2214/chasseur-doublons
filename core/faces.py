"""Reconnaissance faciale.

Deux moteurs :
 - "insightface" (précis) : détection SCRFD (det_10g), reconnaissance ArcFace (w600k_r50, 512 dimensions),
   points de repère 2d106det. Modèles réservés à la recherche NON COMMERCIALE.
 - "opencv" (licence libre, moins précis) : détection YuNet + reconnaissance SFace (128 dimensions).

Tout reste local : aucune donnée de visage ne quitte la machine.
"""
import threading

import numpy as np

from .config import EYE_CLOSED_RATIO, FACE_BLUR_MIN, FACE_MIN_PX, PERSON_THR
from .imaging import cv2, sharpness

ARCFACE_DST = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                        [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)
DET_SIZES = {"rapide": 320, "equilibre": 480, "precis": 640}


def nms(boxes, scores, thr):
    x1, y1, x2, y2 = boxes.T
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1 + 1) * np.maximum(0.0, yy2 - yy1 + 1)
        ovr = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[np.where(ovr <= thr)[0] + 1]
    return keep


def align(bgr, kps, size=112):
    """Alignement par transformation de similarité sur 5 points (modèle ArcFace 112x112)."""
    M, _ = cv2.estimateAffinePartial2D(kps.astype(np.float32), ARCFACE_DST * (size / 112.0), method=cv2.LMEDS)
    if M is None:
        return None
    return cv2.warpAffine(bgr, M, (size, size), borderValue=0.0)


# Groupes de 10 points de l'oeil dans le modèle 2d106det (vérifiés : ce sont les points les plus proches
# des centres des yeux donnés par le détecteur).
EYE_GROUPS = (np.arange(33, 43), np.arange(87, 97))


def eye_ratio(landmarks, kps):
    """Rapport hauteur/largeur des points de repère les plus proches de chaque oeil (moyenne des deux yeux).
    Oeil ouvert typique ~0,4 ; oeil fermé ~0,1-0,2 (seuil à étalonner)."""
    if landmarks is None or len(landmarks) < 97:
        return None
    axis = kps[1] - kps[0]
    n = np.linalg.norm(axis)
    if n < 1e-3:
        return None
    ax = axis / n
    perp = np.array([-ax[1], ax[0]])
    ratios = []
    for e in (kps[0], kps[1]):
        # groupe de points dont le barycentre est le plus proche de ce centre d'oeil
        g = min(EYE_GROUPS, key=lambda g: np.linalg.norm(landmarks[g].mean(0) - e))
        pts = landmarks[g]
        u, v = pts @ ax, pts @ perp
        wdt = u.max() - u.min()
        if wdt > 1e-3:
            ratios.append(float((v.max() - v.min()) / wdt))
    return float(np.mean(ratios)) if ratios else None


class InsightFaceEngine:
    name = "insightface"
    dim = 512

    def __init__(self, mm, det_size=640):
        self.mm = mm
        self.det_size = det_size
        self.det = mm.session("insightface", "det_10g.onnx")
        self.rec = mm.session("insightface", "w600k_r50.onnx")
        self.lmk = mm.session("insightface", "2d106det.onnx")
        self.det_in = self.det.get_inputs()[0].name
        self.rec_in = self.rec.get_inputs()[0].name
        self.lmk_in = self.lmk.get_inputs()[0].name

    def detect(self, bgr, thr=0.5):
        S = self.det_size
        h, w = bgr.shape[:2]
        if h > w:
            nh, nw = S, max(1, int(S * w / h))
        else:
            nw, nh = S, max(1, int(S * h / w))
        scale = nh / h
        det_img = np.zeros((S, S, 3), dtype=np.uint8)
        det_img[:nh, :nw] = cv2.resize(bgr, (nw, nh))
        blob = cv2.dnn.blobFromImage(det_img, 1.0 / 128, (S, S), (127.5, 127.5, 127.5), swapRB=True)
        outs = self.det.run(None, {self.det_in: blob})
        boxes, scores, kpss = [], [], []
        for i, stride in enumerate((8, 16, 32)):
            sc = outs[i].reshape(-1)
            bb = outs[i + 3].reshape(-1, 4) * stride
            kp = outs[i + 6].reshape(-1, 10) * stride
            hh, ww = S // stride, S // stride
            centers = np.stack(np.mgrid[:hh, :ww][::-1], axis=-1).astype(np.float32).reshape(-1, 2) * stride
            centers = np.repeat(centers, sc.size // centers.shape[0], axis=0)
            pos = np.where(sc >= thr)[0]
            if not pos.size:
                continue
            c = centers[pos]
            b = bb[pos]
            boxes.append(np.stack([c[:, 0] - b[:, 0], c[:, 1] - b[:, 1], c[:, 0] + b[:, 2], c[:, 1] + b[:, 3]], -1))
            k = kp[pos].reshape(-1, 5, 2) + c[:, None, :]
            kpss.append(k)
            scores.append(sc[pos])
        if not boxes:
            return []
        boxes = np.concatenate(boxes) / scale
        kpss = np.concatenate(kpss) / scale
        scores = np.concatenate(scores)
        keep = nms(boxes, scores, 0.4)
        return [(boxes[i], kpss[i], float(scores[i])) for i in keep]

    def landmarks(self, bgr, box):
        x1, y1, x2, y2 = box
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        s = 192.0 / (max(x2 - x1, y2 - y1) * 1.5)
        M = np.array([[s, 0, 96 - cx * s], [0, s, 96 - cy * s]], dtype=np.float32)
        crop = cv2.warpAffine(bgr, M, (192, 192), borderValue=0.0)
        blob = cv2.dnn.blobFromImage(crop, 1.0, (192, 192), (0, 0, 0), swapRB=True)
        pred = self.lmk.run(None, {self.lmk_in: blob})[0].reshape(-1, 2)
        pred = (pred + 1) * 96
        IM = cv2.invertAffineTransform(M)
        return pred @ IM[:, :2].T + IM[:, 2]

    def embed(self, aligned_list):
        blob = cv2.dnn.blobFromImages(aligned_list, 1.0 / 127.5, (112, 112), (127.5, 127.5, 127.5), swapRB=True)
        if getattr(self.rec, "gpu", False):     # GPU : un visage à la fois (taille d'entrée constante)
            e = np.concatenate([self.rec.run(None, {self.rec_in: blob[i:i + 1]})[0] for i in range(len(blob))])
        else:
            e = self.rec.run(None, {self.rec_in: blob})[0]
        return e / (np.linalg.norm(e, axis=1, keepdims=True) + 1e-9)

    def analyse(self, bgr):
        out, crops = [], []
        for box, kps, score in self.detect(bgr):
            al = align(bgr, kps)
            if al is None:
                continue
            try:
                lm = self.landmarks(bgr, box)
            except Exception:
                lm = None
            out.append({"box": box, "kps": kps, "score": score, "eyes": eye_ratio(lm, kps),
                        "blur": sharpness(cv2.cvtColor(al, cv2.COLOR_BGR2GRAY))})
            crops.append(al)
        if crops:
            embs = self.embed(crops)
            for f, e in zip(out, embs):
                f["emb"] = e
        return out


class OpenCVFaceEngine:
    """Repli à licence permissive : YuNet (détection) + SFace (reconnaissance)."""
    name = "opencv"
    dim = 128

    def __init__(self, mm, det_size=640):
        if cv2 is None:
            raise RuntimeError("OpenCV n'est pas installé")
        self.mm = mm
        self.det_size = det_size
        if not mm.available("opencv_faces"):
            raise FileNotFoundError("Modèles YuNet/SFace absents : téléchargez-les dans l'onglet Réglages.")
        self.det_path = mm.path("face_detection_yunet_2023mar.onnx")
        self.rec_path = mm.path("face_recognition_sface_2021dec.onnx")
        self._tl = threading.local()   # les objets OpenCV ne sont pas partageables entre threads

    def _objs(self):
        if getattr(self._tl, "det", None) is None:
            self._tl.det = cv2.FaceDetectorYN.create(self.det_path, "", (320, 320), 0.7, 0.3, 5000)
            self._tl.rec = cv2.FaceRecognizerSF.create(self.rec_path, "")
        return self._tl.det, self._tl.rec

    def analyse(self, bgr):
        det, rec = self._objs()
        h, w = bgr.shape[:2]
        s = min(1.0, self.det_size / max(h, w))
        small = cv2.resize(bgr, (max(1, int(w * s)), max(1, int(h * s)))) if s < 1 else bgr
        det.setInputSize((small.shape[1], small.shape[0]))
        _, faces = det.detect(small)
        out = []
        if faces is None:
            return out
        for row in faces:
            row = row.copy()
            row[:14] /= s
            x, y, fw, fh = row[:4]
            kps = row[4:14].reshape(5, 2)
            al = rec.alignCrop(bgr, row)
            e = rec.feature(al).reshape(-1)
            e = e / (np.linalg.norm(e) + 1e-9)
            out.append({"box": np.array([x, y, x + fw, y + fh]), "kps": kps, "score": float(row[14]),
                        "eyes": None, "emb": e, "blur": sharpness(cv2.cvtColor(al, cv2.COLOR_BGR2GRAY))})
        return out


def make_engine(mm, name, det_size=640):
    if name == "insightface":
        return InsightFaceEngine(mm, det_size)
    return OpenCVFaceEngine(mm, det_size)


def eligible(px, blur):
    return px >= FACE_MIN_PX and (blur is None or blur >= FACE_BLUR_MIN)


def eyes_closed(ratio, thr=EYE_CLOSED_RATIO):
    return ratio is not None and ratio < thr


# ------------------------------------------------------------------ regroupement en personnes
def cluster(embs, locked, locked_pid, fixed_centroids, thr, passes=2, min_size=2, batch=1024):
    """Regroupe les visages en personnes (centroïdes + deux passes de réaffectation, sans chaînage).

    embs           : (N, D) float32 normalisés
    locked         : (N,) bool, visage confirmé par l'utilisateur (il ne bouge plus)
    locked_pid     : (N,) int, personne imposée des visages verrouillés (-1 = « n'appartient à personne »)
    fixed_centroids: {pid: centroid} des personnes existantes (graines ; les identifiants restent stables)
    Retourne un tableau (N,) d'étiquettes : personne existante (>0), nouvelle personne (<= -2) ou -1.
    """
    N = len(embs)
    labels = np.full(N, -1, dtype=np.int64)
    if N == 0:
        return labels
    locked = np.asarray(locked, dtype=bool)
    labels[locked] = locked_pid[locked]
    locked_pid = np.where(locked, 0, -1)   # usage interne : >= 0 = verrouillé
    keys = list(fixed_centroids)
    cents = [np.asarray(fixed_centroids[k], dtype=np.float32) for k in keys]
    sums = [c.copy() for c in cents]
    counts = [1] * len(keys)
    next_new = -2

    def C():
        if not cents:
            return np.zeros((0, embs.shape[1]), dtype=np.float32)
        m = np.stack(cents)
        return m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)

    # 1) passe initiale par lots : centroïde le plus proche, sinon nouvelle personne (meneur glouton dans le lot)
    free = np.where(locked_pid < 0)[0]
    for s in range(0, len(free), batch):
        idx = free[s:s + batch]
        cm = C()
        if len(cm):
            sim = embs[idx] @ cm.T
            best = sim.argmax(1)
            bv = sim[np.arange(len(idx)), best]
        else:
            best = np.zeros(len(idx), dtype=np.int64)
            bv = np.full(len(idx), -1.0)
        for j, i in enumerate(idx):
            k = -1
            if bv[j] >= thr:
                k = int(best[j])
            else:   # comparer aux personnes créées dans ce lot
                n0 = len(cm)
                if len(cents) > n0:
                    extra = np.stack(cents[n0:])
                    extra = extra / (np.linalg.norm(extra, axis=1, keepdims=True) + 1e-9)
                    sv = extra @ embs[i]
                    jj = int(sv.argmax())
                    if sv[jj] >= thr:
                        k = n0 + jj
            if k < 0:
                keys.append(next_new)
                next_new -= 1
                cents.append(embs[i].copy())
                sums.append(embs[i].copy())
                counts.append(1)
                k = len(keys) - 1
            else:
                sums[k] += embs[i]
                counts[k] += 1
                cents[k] = sums[k] / counts[k]
            labels[i] = keys[k]

    # 2) passes de réaffectation sur les centroïdes recalculés
    key_arr = np.array(keys, dtype=np.int64)
    for _ in range(passes):
        cm = []
        for k_i, key in enumerate(keys):
            m = labels == key
            if m.any():
                c = embs[m].mean(0)
            elif key in fixed_centroids:
                c = np.asarray(fixed_centroids[key], dtype=np.float32)
            else:
                c = np.zeros(embs.shape[1], dtype=np.float32)
            cm.append(c / (np.linalg.norm(c) + 1e-9))
        cm = np.stack(cm)
        for s in range(0, len(free), 8192):
            idx = free[s:s + 8192]
            sim = embs[idx] @ cm.T
            best = sim.argmax(1)
            bv = sim[np.arange(len(idx)), best]
            labels[idx] = np.where(bv >= thr, key_arr[best], -1)

    # 3) les nouvelles personnes trop petites redeviennent « non assignées »
    for key in set(labels.tolist()):
        if key <= -2 and (labels == key).sum() < min_size:
            labels[labels == key] = -1
    return labels


def default_threshold(engine):
    return PERSON_THR.get(engine, 0.45)
