"""Détection d'objets YOLOX (Apache-2.0, 80 classes COCO) via onnxruntime.

Entrée : BGR, valeurs 0-255 sans normalisation, redimensionnement à 640 avec remplissage 114 (en bas/à droite).
Sortie brute décodée avec les grilles et strides 8/16/32, puis NMS par classe.
"""
import numpy as np

from .faces import nms
from .imaging import cv2

COCO_FR = [
    "personne", "vélo", "voiture", "moto", "avion", "bus", "train", "camion", "bateau", "feu de circulation",
    "bouche d'incendie", "panneau stop", "parcmètre", "banc", "oiseau", "chat", "chien", "cheval", "mouton",
    "vache", "éléphant", "ours", "zèbre", "girafe", "sac à dos", "parapluie", "sac à main", "cravate", "valise",
    "frisbee", "skis", "snowboard", "ballon", "cerf-volant", "batte de baseball", "gant de baseball",
    "skateboard", "planche de surf", "raquette de tennis", "bouteille", "verre à vin", "tasse", "fourchette",
    "couteau", "cuillère", "bol", "banane", "pomme", "sandwich", "orange", "brocoli", "carotte", "hot-dog",
    "pizza", "donut", "gâteau", "chaise", "canapé", "plante en pot", "lit", "table", "toilettes", "télévision",
    "ordinateur portable", "souris", "télécommande", "clavier", "téléphone", "micro-ondes", "four", "grille-pain",
    "évier", "réfrigérateur", "livre", "horloge", "vase", "ciseaux", "ours en peluche", "sèche-cheveux",
    "brosse à dents",
]
COCO_EN = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
    "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard",
    "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors",
    "teddy bear", "hair drier", "toothbrush",
]


class YoloX:
    def __init__(self, mm, variant="yolox_s"):
        self.sess = mm.session(variant, variant + ".onnx")
        self.inp = self.sess.get_inputs()[0].name
        shp = self.sess.get_inputs()[0].shape
        self.size = int(shp[2]) if isinstance(shp[2], int) else 640
        grids, strides = [], []
        for s in (8, 16, 32):
            n = self.size // s
            xv, yv = np.meshgrid(np.arange(n), np.arange(n))
            grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
            strides.append(np.full((n * n, 1), s))
        self.grids = np.concatenate(grids).astype(np.float32)
        self.strides = np.concatenate(strides).astype(np.float32)

    def preprocess(self, bgr):
        S = self.size
        h, w = bgr.shape[:2]
        r = min(S / h, S / w)
        img = np.full((S, S, 3), 114, dtype=np.uint8)
        img[:int(h * r), :int(w * r)] = cv2.resize(bgr, (int(w * r), int(h * r)), interpolation=cv2.INTER_LINEAR)
        return img.transpose(2, 0, 1)[None].astype(np.float32), r

    def detect(self, bgr, thr=0.35, iou=0.45):
        """Liste de (libellé français, score, boîte normalisée x1,y1,x2,y2)."""
        blob, r = self.preprocess(bgr)
        out = self.sess.run(None, {self.inp: blob})[0][0]
        out[:, :2] = (out[:, :2] + self.grids) * self.strides
        out[:, 2:4] = np.exp(out[:, 2:4]) * self.strides
        cls = out[:, 5:].argmax(1)
        sc = out[:, 4] * out[np.arange(len(out)), 5 + cls]
        m = sc >= thr
        if not m.any():
            return []
        b, sc, cls = out[m, :4], sc[m], cls[m]
        boxes = np.stack([b[:, 0] - b[:, 2] / 2, b[:, 1] - b[:, 3] / 2,
                          b[:, 0] + b[:, 2] / 2, b[:, 1] + b[:, 3] / 2], 1) / r
        h, w = bgr.shape[:2]
        res = []
        for c in np.unique(cls):
            idx = np.where(cls == c)[0]
            for k in nms(boxes[idx], sc[idx], iou):
                i = idx[k]
                x1, y1, x2, y2 = boxes[i]
                res.append((COCO_FR[c], float(sc[i]),
                            (max(0, x1 / w), max(0, y1 / h), min(1, x2 / w), min(1, y2 / h))))
        res.sort(key=lambda t: -t[1])
        return res
