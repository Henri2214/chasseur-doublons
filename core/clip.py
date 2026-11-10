"""CLIP ViT-B/32 en ONNX (sans PyTorch) : embeddings d'images, de texte, et scènes par zéro-shot.

Modèles Xenova/clip-vit-base-patch32 (vision_model / text_model, versions quantifiées) + tokenizer.json lu
avec la bibliothèque `tokenizers`.
"""
import numpy as np
from PIL import Image

MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
PAD_ID = 49407
CTX = 77

# (libellé affiché, invite anglaise) - environ 40 scènes
SCENES = [
    ("intérieur", "a photo taken indoors"), ("extérieur", "a photo taken outdoors"),
    ("plage", "a photo of a beach"), ("mer", "a photo of the sea"), ("montagne", "a photo of mountains"),
    ("neige", "a photo of snow"), ("forêt", "a photo of a forest"), ("campagne", "a photo of the countryside"),
    ("lac ou rivière", "a photo of a lake or river"), ("désert", "a photo of a desert"),
    ("ville", "a photo of a city street"), ("monument", "a photo of a famous monument"),
    ("église", "a photo of a church"), ("musée", "a photo inside a museum"), ("parc ou jardin", "a photo of a park or garden"),
    ("piscine", "a photo of a swimming pool"), ("coucher de soleil", "a photo of a sunset"),
    ("nuit", "a photo taken at night"), ("ciel", "a photo of the sky and clouds"),
    ("repas", "a photo of a meal on a table"), ("restaurant", "a photo inside a restaurant"),
    ("cuisine", "a photo of a kitchen"), ("salon", "a photo of a living room"), ("chambre", "a photo of a bedroom"),
    ("bureau", "a photo of an office"), ("fête", "a photo of a party"), ("mariage", "a photo of a wedding"),
    ("anniversaire", "a photo of a birthday party with a cake"), ("concert", "a photo of a concert"),
    ("sport", "a photo of people playing sports"), ("portrait", "a portrait photo of a person"),
    ("groupe de personnes", "a photo of a group of people"), ("bébé", "a photo of a baby"),
    ("animal", "a photo of an animal"), ("voiture", "a photo of a car"), ("route", "a photo of a road"),
    ("avion ou aéroport", "a photo of an airplane or airport"), ("document", "a scanned document"),
    ("capture d'écran", "a screenshot of a computer or phone screen"), ("texte manuscrit", "a photo of handwritten notes"),
    ("reçu ou facture", "a photo of a receipt"), ("carte ou plan", "a map"),
]


def preprocess(im):
    """Petit côté à 224 (bicubique), recadrage centré 224x224, normalisation CLIP."""
    im = im.convert("RGB")
    w, h = im.size
    s = 224 / min(w, h)
    im = im.resize((max(224, round(w * s)), max(224, round(h * s))), Image.BICUBIC)
    w, h = im.size
    left, top = (w - 224) // 2, (h - 224) // 2
    im = im.crop((left, top, left + 224, top + 224))
    x = (np.asarray(im, dtype=np.float32) / 255.0 - MEAN) / STD
    return x.transpose(2, 0, 1)


class Clip:
    def __init__(self, mm):
        from tokenizers import Tokenizer
        self.vis = mm.session("clip", "clip/vision_model_quantized.onnx")
        self.txt = mm.session("clip", "clip/text_model_quantized.onnx")
        self.tok = Tokenizer.from_file(mm.path("clip/tokenizer.json"))
        self.vis_in = self.vis.get_inputs()[0].name
        self.txt_inputs = [i.name for i in self.txt.get_inputs()]
        self._scenes = None

    def image(self, ims):
        x = np.stack([preprocess(im) for im in ims]).astype(np.float32)
        e = self.vis.run(None, {self.vis_in: x})[0]
        return e / (np.linalg.norm(e, axis=1, keepdims=True) + 1e-9)

    def text(self, texts):
        ids = np.full((len(texts), CTX), PAD_ID, dtype=np.int64)
        mask = np.zeros((len(texts), CTX), dtype=np.int64)
        for i, t in enumerate(texts):
            enc = self.tok.encode(t).ids[:CTX]
            if len(enc) == CTX:
                enc[-1] = PAD_ID   # garder le jeton de fin
            ids[i, :len(enc)] = enc
            mask[i, :len(enc)] = 1
        feed = {"input_ids": ids}
        if "attention_mask" in self.txt_inputs:
            feed["attention_mask"] = mask
        e = self.txt.run(None, feed)[0]
        return e / (np.linalg.norm(e, axis=1, keepdims=True) + 1e-9)

    def scene_matrix(self):
        if self._scenes is None:
            self._scenes = self.text([p for _, p in SCENES])
        return self._scenes

    def scenes(self, emb, top=3, min_p=0.10):
        logits = 100.0 * (self.scene_matrix() @ emb)
        p = np.exp(logits - logits.max())
        p /= p.sum()
        order = np.argsort(-p)[:top]
        return [(SCENES[i][0], float(p[i])) for i in order if p[i] >= min_p]
