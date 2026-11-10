"""Index persistant SQLite (WAL).

Chaque fichier est analysé une seule fois : une nouvelle analyse ne traite que les fichiers nouveaux ou
modifiés (taille + date de modification) et purge les fichiers disparus.
"""
import json
import os
import sqlite3
import threading
import time

import numpy as np

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  folder TEXT NOT NULL,
  name TEXT NOT NULL,
  ext TEXT,
  kind TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime REAL NOT NULL,
  seen REAL,
  status TEXT DEFAULT 'new',      -- new / ok / error / skipped
  error TEXT,
  w INTEGER, h INTEGER,
  taken REAL,                     -- date de prise de vue EXIF (secondes, heure locale de l'appareil) ou NULL
  cam TEXT,
  has_exif_cam INTEGER DEFAULT 0,
  phash INTEGER,                  -- 64 bits (signé)
  fine BLOB, colg BLOB, detail BLOB,
  blur REAL,
  dur REAL, nframes INTEGER, vn INTEGER, vsig BLOB, vcol BLOB,
  phash_part TEXT, sha256 TEXT,
  done_visual INTEGER DEFAULT 0, done_faces TEXT, done_objects INTEGER DEFAULT 0,
  done_ocr INTEGER DEFAULT 0, done_clip INTEGER DEFAULT 0, done_quality INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_files_folder ON files(folder);
CREATE INDEX IF NOT EXISTS ix_files_size ON files(size);
CREATE INDEX IF NOT EXISTS ix_files_sha ON files(sha256);

CREATE TABLE IF NOT EXISTS persons (
  id INTEGER PRIMARY KEY,
  name TEXT,
  engine TEXT NOT NULL,
  centroid BLOB,
  cover_face INTEGER,
  created REAL
);

CREATE TABLE IF NOT EXISTS faces (
  id INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  engine TEXT NOT NULL,
  emb BLOB NOT NULL,              -- float16 normalisé (512 InsightFace, 128 SFace)
  x1 REAL, y1 REAL, x2 REAL, y2 REAL,   -- boîte normalisée 0-1
  px INTEGER,                     -- taille du visage en pixels de l'original
  score REAL,
  blur REAL,
  eyes REAL,                      -- rapport hauteur/largeur moyen des yeux (NULL si inconnu)
  person_id INTEGER REFERENCES persons(id) ON DELETE SET NULL,
  locked INTEGER DEFAULT 0,
  eligible INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_faces_file ON faces(file_id);
CREATE INDEX IF NOT EXISTS ix_faces_person ON faces(person_id);

CREATE TABLE IF NOT EXISTS objects (
  id INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  label TEXT NOT NULL, score REAL, x1 REAL, y1 REAL, x2 REAL, y2 REAL
);
CREATE INDEX IF NOT EXISTS ix_objects_file ON objects(file_id);
CREATE INDEX IF NOT EXISTS ix_objects_label ON objects(label);

CREATE TABLE IF NOT EXISTS ocr (
  file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  text TEXT
);

CREATE TABLE IF NOT EXISTS doctext (
  file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  text TEXT,                      -- texte extrait (tronqué à 1 million de caractères)
  chash TEXT,                     -- empreinte du texte normalisé (même contenu, autre fichier)
  sketch BLOB,                    -- échantillon de suites de 3 mots (versions proches)
  words INTEGER,
  method TEXT,                    -- texte / ocr / approx / erreur
  pages INTEGER,
  title TEXT,
  mid TEXT, sender TEXT, mdate REAL,   -- courriels : Message-ID, expéditeur, date d'envoi
  extra TEXT
);
CREATE INDEX IF NOT EXISTS ix_doctext_chash ON doctext(chash);
CREATE INDEX IF NOT EXISTS ix_doctext_mid ON doctext(mid);

CREATE TABLE IF NOT EXISTS clip (
  file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  emb BLOB NOT NULL               -- float16 normalisé (512)
);

CREATE TABLE IF NOT EXISTS scenes (
  file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  label TEXT NOT NULL, score REAL
);
CREATE INDEX IF NOT EXISTS ix_scenes_file ON scenes(file_id);
CREATE INDEX IF NOT EXISTS ix_scenes_label ON scenes(label);

CREATE TABLE IF NOT EXISTS journal (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  action TEXT NOT NULL,           -- delete / move / rename / undo
  file_id INTEGER,
  src TEXT, dst TEXT,
  size INTEGER, mtime REAL,
  undone INTEGER DEFAULT 0,
  info TEXT
);

CREATE TABLE IF NOT EXISTS calib (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,             -- dup / face
  a INTEGER, b INTEGER,
  metrics TEXT,
  answer INTEGER,                 -- 1 = oui (doublon / même personne), 0 = non
  ts REAL
);

CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY,
  root TEXT, started REAL, ended REAL, stats TEXT
);
"""


class Database:
    """Une connexion par thread ; écritures sérialisées par un verrou (SQLite n'a qu'un écrivain)."""

    def __init__(self, path):
        self.path = str(path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._local = threading.local()
        self.wlock = threading.RLock()
        c = self.conn()
        with self.wlock:
            c.executescript(SCHEMA)
            c.execute("INSERT OR IGNORE INTO meta(k, v) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            self._migrate(c)
            c.commit()
        self.fts = self._init_fts()

    def conn(self):
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=60, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA temp_store=MEMORY")
            self._local.c = c
        return c

    @staticmethod
    def _migrate(c):
        """Mise à niveau d'un index créé par une version précédente (aucune donnée perdue)."""
        from .config import DOC_EXT
        cols = {r["name"] for r in c.execute("PRAGMA table_info(files)")}
        if "done_doc" not in cols:
            c.execute("ALTER TABLE files ADD COLUMN done_doc INTEGER DEFAULT 0")
        ext = sorted(DOC_EXT)
        c.execute("UPDATE files SET kind='doc' WHERE kind='other' AND ext IN (%s)" % ",".join("?" * len(ext)), ext)
        c.execute("UPDATE meta SET v=? WHERE k='schema'", (str(SCHEMA_VERSION),))

    def _init_fts(self):
        c = self.conn()
        try:
            with self.wlock:
                c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(text, content='doctext', "
                          "content_rowid='file_id', tokenize='unicode61 remove_diacritics 2')")
                c.executescript("""
                CREATE TRIGGER IF NOT EXISTS doc_ai AFTER INSERT ON doctext BEGIN
                  INSERT INTO doc_fts(rowid, text) VALUES (new.file_id, new.text); END;
                CREATE TRIGGER IF NOT EXISTS doc_ad AFTER DELETE ON doctext BEGIN
                  INSERT INTO doc_fts(doc_fts, rowid, text) VALUES ('delete', old.file_id, old.text); END;
                CREATE TRIGGER IF NOT EXISTS doc_au AFTER UPDATE ON doctext BEGIN
                  INSERT INTO doc_fts(doc_fts, rowid, text) VALUES ('delete', old.file_id, old.text);
                  INSERT INTO doc_fts(rowid, text) VALUES (new.file_id, new.text); END;
                """)
                c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS ocr_fts USING fts5(text, content='ocr', "
                          "content_rowid='file_id', tokenize='unicode61 remove_diacritics 2')")
                c.executescript("""
                CREATE TRIGGER IF NOT EXISTS ocr_ai AFTER INSERT ON ocr BEGIN
                  INSERT INTO ocr_fts(rowid, text) VALUES (new.file_id, new.text); END;
                CREATE TRIGGER IF NOT EXISTS ocr_ad AFTER DELETE ON ocr BEGIN
                  INSERT INTO ocr_fts(ocr_fts, rowid, text) VALUES ('delete', old.file_id, old.text); END;
                CREATE TRIGGER IF NOT EXISTS ocr_au AFTER UPDATE ON ocr BEGIN
                  INSERT INTO ocr_fts(ocr_fts, rowid, text) VALUES ('delete', old.file_id, old.text);
                  INSERT INTO ocr_fts(rowid, text) VALUES (new.file_id, new.text); END;
                """)
                c.commit()
            return True
        except sqlite3.OperationalError:
            return False   # SQLite compilé sans FTS5 : recherche par LIKE

    # ------------------------------------------------------------------ helpers
    def q(self, sql, args=()):
        return self.conn().execute(sql, args).fetchall()

    def one(self, sql, args=()):
        return self.conn().execute(sql, args).fetchone()

    def x(self, sql, args=()):
        with self.wlock:
            c = self.conn()
            cur = c.execute(sql, args)
            c.commit()
            return cur

    def many(self, sql, rows):
        with self.wlock:
            c = self.conn()
            c.executemany(sql, rows)
            c.commit()

    def get_setting(self, k, default=None):
        r = self.one("SELECT v FROM settings WHERE k=?", (k,))
        return json.loads(r["v"]) if r else default

    def set_setting(self, k, v):
        self.x("INSERT INTO settings(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
               (k, json.dumps(v)))

    def journal(self, action, file_id=None, src=None, dst=None, size=None, mtime=None, info=None):
        cur = self.x("INSERT INTO journal(ts, action, file_id, src, dst, size, mtime, info) VALUES (?,?,?,?,?,?,?,?)",
                     (time.time(), action, file_id, src, dst, size, mtime,
                      json.dumps(info, ensure_ascii=False) if info is not None else None))
        return cur.lastrowid

    def clear_biometrics(self):
        """Efface tous les visages et personnes (données biométriques)."""
        with self.wlock:
            c = self.conn()
            c.execute("DELETE FROM faces")
            c.execute("DELETE FROM persons")
            c.execute("DELETE FROM calib WHERE kind='face'")
            c.execute("UPDATE files SET done_faces=NULL")
            c.commit()
            c.execute("VACUUM")


def f16(a):
    return np.asarray(a, dtype=np.float16).tobytes()


def from_f16(b):
    return np.frombuffer(b, dtype=np.float16).astype(np.float32)
