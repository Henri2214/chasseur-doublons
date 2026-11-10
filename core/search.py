"""Explorer : recherche unique combinant CLIP, objets, texte OCR, personnes, scènes et noms de fichiers.

Si CLIP est indisponible, la recherche fonctionne quand même avec les objets, l'OCR et les personnes.
CLIP ViT-B/32 ne comprend que l'anglais : les mots français connus (objets, scènes, petit glossaire)
sont traduits avant l'encodage.
"""
import os
import re
import threading
import unicodedata

import numpy as np

from .clip import SCENES
from .config import BLUR_GLOBAL_MIN, DOC_FAMILIES
from .objects import COCO_EN, COCO_FR

GLOSSARY = {
    "enfant": "child", "enfants": "children", "bebe": "baby", "femme": "woman", "homme": "man", "fille": "girl",
    "garcon": "boy", "famille": "family", "amis": "friends", "mer": "sea", "plage": "beach", "montagne": "mountain",
    "neige": "snow", "foret": "forest", "ville": "city", "nuit": "night", "soleil": "sun", "coucher": "sunset",
    "lac": "lake", "riviere": "river", "fleur": "flower", "fleurs": "flowers", "arbre": "tree", "noel": "christmas",
    "sapin": "christmas tree", "anniversaire": "birthday", "mariage": "wedding", "fete": "party", "repas": "meal",
    "plat": "dish", "vin": "wine", "biere": "beer", "maison": "house", "jardin": "garden", "piscine": "swimming pool",
    "velo": "bicycle", "voiture": "car", "chien": "dog", "chat": "cat", "cheval": "horse", "oiseau": "bird",
    "rouge": "red", "bleu": "blue", "vert": "green", "jaune": "yellow", "noir": "black", "blanc": "white",
    "sur": "on", "dans": "in", "avec": "with", "sous": "under", "devant": "in front of", "et": "and",
    "photo": "photo", "selfie": "selfie", "groupe": "group", "visage": "face", "sourire": "smiling",
    "document": "document", "facture": "receipt", "ecran": "screen", "capture": "screenshot", "texte": "text",
    "chateau": "castle", "eglise": "church", "pont": "bridge", "route": "road", "bateau": "boat", "avion": "airplane",
    "ski": "skiing", "randonnee": "hiking", "tour eiffel": "eiffel tower", "chapeau": "hat",
    "lunettes": "glasses", "robe": "dress", "costume": "suit", "gateau": "cake", "bougies": "candles",
    "vacances": "holidays", "voyage": "travel", "paysage": "landscape", "ciel": "sky", "nuage": "cloud",
    "nuages": "clouds", "pluie": "rain", "herbe": "grass", "sable": "sand", "vague": "wave", "vagues": "waves",
    "rue": "street", "magasin": "shop", "marche": "market", "concert": "concert", "scene": "stage",
    "joueur": "player", "joueurs": "players", "basket": "basketball", "foot": "football", "football": "soccer",
}
for fr, en in zip(COCO_FR, COCO_EN):
    GLOSSARY.setdefault(fr, en)


def fold(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return s


GLOSS_FOLD = {fold(k): v for k, v in GLOSSARY.items()}
_STOP = {"le", "la", "les", "un", "une", "des", "de", "du", "d", "l", "a", "au", "aux", "en"}
CLIP_MIN = 0.235   # similarité texte-image minimale pour un résultat trouvé par CLIP seul


def to_english(q):
    words = re.findall(r"[\w'-]+", fold(q))
    out, i = [], 0
    while i < len(words):
        two = " ".join(words[i:i + 2])
        if two in GLOSS_FOLD:
            out.append(GLOSS_FOLD[two])
            i += 2
            continue
        w = words[i]
        if w in GLOSS_FOLD:
            out.append(GLOSS_FOLD[w])
        elif w not in _STOP:
            out.append(w)
        i += 1
    return "a photo of " + " ".join(out) if out else q


class ClipIndex:
    """Matrice des embeddings CLIP en mémoire (float16), rechargée si l'index a changé."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ids = None
        self.E = None
        self.count = -1

    def get(self, db):
        n = db.one("SELECT COUNT(*) n, MAX(file_id) m FROM clip")
        key = (n["n"], n["m"])
        with self.lock:
            if self.count != key:
                rows = db.q("SELECT file_id, emb FROM clip")
                self.ids = np.array([r["file_id"] for r in rows], dtype=np.int64)
                self.E = np.stack([np.frombuffer(r["emb"], np.float16) for r in rows]) if rows else None
                self.count = key
            return self.ids, self.E


CLIP_INDEX = ClipIndex()


def search(db, q="", filters=None, page=1, per=60, clip_model=None, ids_only=False):
    f = filters or {}
    where, args = ["f.status='ok'"], []
    if f.get("kind") in ("image", "video", "doc", "other"):
        where.append("f.kind=?")
        args.append(f["kind"])
    elif f.get("kind") in DOC_FAMILIES:               # famille de documents : pdf, word, tableur, mail...
        ex = sorted(DOC_FAMILIES[f["kind"]])
        where.append("f.ext IN (%s)" % ",".join("?" * len(ex)))
        args += ex
    if f.get("folder"):
        r = os.path.abspath(f["folder"]).rstrip("\\/") + os.sep
        where.append("f.path >= ? AND f.path < ?")
        args += [r, r + "\U0010ffff"]
    if f.get("person"):
        where.append("EXISTS (SELECT 1 FROM faces x WHERE x.file_id=f.id AND x.person_id=?)")
        args.append(int(f["person"]))
    if f.get("object"):
        where.append("EXISTS (SELECT 1 FROM objects x WHERE x.file_id=f.id AND x.label=?)")
        args.append(f["object"])
    if f.get("scene"):
        where.append("EXISTS (SELECT 1 FROM scenes x WHERE x.file_id=f.id AND x.label=?)")
        args.append(f["scene"])
    if f.get("blur") == "flou":
        where.append("f.blur IS NOT NULL AND f.blur < ?")
        args.append(float(db.get_setting("blur_min", BLUR_GLOBAL_MIN)))
    elif f.get("blur") == "net":
        where.append("f.blur >= ?")
        args.append(float(db.get_setting("blur_min", BLUR_GLOBAL_MIN)))
    if f.get("date_from"):
        where.append("COALESCE(f.taken, f.mtime) >= ?")
        args.append(float(f["date_from"]))
    if f.get("date_to"):
        where.append("COALESCE(f.taken, f.mtime) <= ?")
        args.append(float(f["date_to"]))
    W = " AND ".join(where)

    q = (q or "").strip()
    used = []
    if not q:
        if ids_only:
            return {"ids": [r["id"] for r in db.q("SELECT f.id FROM files f WHERE " + W +
                                                  " ORDER BY COALESCE(f.taken, f.mtime) DESC", args)]}
        total = db.one("SELECT COUNT(*) n FROM files f WHERE " + W, args)["n"]
        rows = db.q("SELECT f.id FROM files f WHERE " + W + " ORDER BY COALESCE(f.taken, f.mtime) DESC LIMIT ? OFFSET ?",
                    (*args, per, (page - 1) * per))
        return _hydrate(db, [r["id"] for r in rows], total, page, per, {}, used)

    allowed = {r["id"] for r in db.q("SELECT f.id FROM files f WHERE " + W, args)}
    score = {}

    def bump(ids, s, why):
        n = 0
        for i in ids:
            if i in allowed:
                score[i] = max(score.get(i, 0), s)
                n += 1
        if n:
            used.append(why)

    qf = fold(q)
    words = [w for w in re.findall(r"[\w'-]+", qf) if w not in _STOP and len(w) > 1]
    # personnes
    pids = [r["id"] for r in db.q("SELECT id, name FROM persons WHERE name IS NOT NULL")
            if fold(r["name"]) in qf or any(w in fold(r["name"]).split() for w in words)]
    if pids:
        bump([r["file_id"] for r in db.q("SELECT DISTINCT file_id FROM faces WHERE person_id IN (%s)"
                                         % ",".join("?" * len(pids)), pids)], 1.0, "personnes")
    # objets
    labels = [lab for lab in COCO_FR if fold(lab) in qf or any(w == fold(lab) or w.rstrip("s") == fold(lab) for w in words)]
    if labels:
        bump([r["file_id"] for r in db.q("SELECT DISTINCT file_id FROM objects WHERE label IN (%s)"
                                         % ",".join("?" * len(labels)), labels)], 0.85, "objets")
    # scènes
    sl = [lab for lab, _ in SCENES if fold(lab) in qf]
    if sl:
        bump([r["file_id"] for r in db.q("SELECT DISTINCT file_id FROM scenes WHERE label IN (%s)"
                                         % ",".join("?" * len(sl)), sl)], 0.7, "scènes")
    # OCR
    if words:
        if db.fts:
            expr = " ".join('"%s"*' % w.replace('"', "") for w in words)
            try:
                bump([r["rowid"] for r in db.q("SELECT rowid FROM ocr_fts WHERE ocr_fts MATCH ? LIMIT 5000", (expr,))],
                     0.95, "texte")
            except Exception:
                pass
        else:
            cond = " AND ".join("text LIKE ?" for _ in words)
            bump([r["file_id"] for r in db.q("SELECT file_id FROM ocr WHERE " + cond, ["%" + w + "%" for w in words])],
                 0.95, "texte")
        # contenu des documents (Word, PDF, courriels...)
        if db.fts:
            try:
                bump([r["rowid"] for r in db.q("SELECT rowid FROM doc_fts WHERE doc_fts MATCH ? LIMIT 20000", (expr,))],
                     0.97, "contenu des documents")
            except Exception:
                pass
        else:
            cond = " AND ".join("text LIKE ?" for _ in words)
            bump([r["file_id"] for r in db.q("SELECT file_id FROM doctext WHERE " + cond,
                                             ["%" + w + "%" for w in words])], 0.97, "contenu des documents")
        # noms de fichiers
        cond = " AND ".join("f.name LIKE ?" for _ in words)
        bump([r["id"] for r in db.q("SELECT f.id FROM files f WHERE " + W + " AND " + cond,
                                    (*args, *["%" + w + "%" for w in words]))], 0.6, "noms")
    # CLIP
    if clip_model is not None:
        ids, E = CLIP_INDEX.get(db)
        if E is not None and len(ids):
            t = clip_model.text([to_english(q)])[0].astype(np.float16)
            sim = (E @ t).astype(np.float32)
            top = np.argsort(-sim)[:2000]
            if len(top):
                best = float(sim[top[0]])
                # CLIP ViT-B/32 : image correspondante ~0,25-0,32 ; sans rapport ~0,15-0,22
                for k in top:
                    s = float(sim[k])
                    i = int(ids[k])
                    if i in score and s >= 0.18:          # déjà trouvé par mot-clé : CLIP sert à départager
                        score[i] += 2.0 * (s - 0.18)
                        continue
                    if s < max(CLIP_MIN, best - 0.04):
                        continue
                    if i in allowed:
                        score[i] = 0.3 + 4.0 * (s - 0.18)
                used.append("CLIP")
    order = sorted(score, key=lambda i: -score[i])
    total = len(order)
    if ids_only:
        return {"ids": order}
    sl_ids = order[(page - 1) * per: page * per]
    return _hydrate(db, sl_ids, total, page, per, score, used, words)


def _hydrate(db, ids, total, page, per, score, used, words=()):
    rows = {}
    if ids:
        for r in db.q("SELECT id, path, name, size, mtime, taken, kind, w, h, dur, blur FROM files WHERE id IN (%s)"
                      % ",".join("?" * len(ids)), ids):
            rows[r["id"]] = dict(r)
    if ids:
        from .docs import snippet
        for r in db.q("SELECT file_id, substr(text, 1, 200000) t, words, method, title, sender, mdate, pages "
                      "FROM doctext WHERE file_id IN (%s)" % ",".join("?" * len(ids)), ids):
            if r["file_id"] in rows:
                rows[r["file_id"]].update(snippet=snippet(r["t"] or "", words), words=r["words"], method=r["method"],
                                          title=r["title"], sender=r["sender"], mdate=r["mdate"], pages=r["pages"])
    out = []
    for i in ids:
        if i in rows:
            d = rows[i]
            d["folder"] = os.path.dirname(d["path"])
            d["score"] = round(score.get(i, 0), 3) if score else None
            out.append(d)
    return {"total": total, "page": page, "per": per, "files": out, "sources": used}


def facets(db):
    return {
        "objects": [dict(r) for r in db.q("SELECT label, COUNT(DISTINCT file_id) n FROM objects GROUP BY label ORDER BY n DESC")],
        "scenes": [dict(r) for r in db.q("SELECT label, COUNT(*) n FROM scenes GROUP BY label ORDER BY n DESC")],
        "persons": [dict(r) for r in db.q("SELECT id, name FROM persons WHERE name IS NOT NULL ORDER BY name")],
    }
