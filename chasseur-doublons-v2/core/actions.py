"""Actions sur les fichiers : supprimer (définitif), déplacer, renommer, annuler. Tout est journalisé.

Sécurités :
 - le fichier doit être indexé et dans le périmètre de la session ;
 - suppression : revérification juste avant (taille et date de modification inchangées, sinon refus) ;
 - suppression du dernier exemplaire d'un groupe refusée sans confirmation explicite ;
 - déplacement et renommage annulables depuis le journal.
"""
import json
import os
import shutil

from .util import explain_oserror, lp


class ActionError(Exception):
    pass


def _file(db, fid):
    r = db.one("SELECT * FROM files WHERE id=?", (fid,))
    if not r:
        raise ActionError("fichier inconnu de l'index")
    return r


def _check_scope(scope, path):
    if not scope.contains(path):
        raise ActionError("fichier hors du périmètre de la session")


def _unchanged(r):
    """Revérifie taille et date de modification juste avant une action destructive."""
    try:
        st = os.stat(lp(r["path"]))
    except OSError as e:
        raise ActionError(explain_oserror(e))
    if st.st_size != r["size"] or abs(st.st_mtime - r["mtime"]) > 1e-6:
        raise ActionError("le fichier a changé depuis l'analyse (taille ou date) : relancez l'analyse")


def delete(db, scope, result, ids, allow_last=False):
    """Supprime DÉFINITIVEMENT les fichiers (pas de corbeille)."""
    ids = list(dict.fromkeys(int(i) for i in ids))
    errors, deleted, freed = [], [], 0
    # dernier exemplaire : un groupe dont tous les membres restants seraient supprimés
    if not allow_last and result is not None:
        sel = set(ids)
        for g in result.visible():
            mem = {m["id"] for m in g["members"]}
            if mem and mem <= sel:
                return {"deleted": 0, "freed": 0, "errors": [], "needs_confirm": True,
                        "message": "La sélection supprimerait TOUS les exemplaires d'au moins un groupe. "
                                   "Confirmez explicitement pour continuer."}
    for fid in ids:
        try:
            r = _file(db, fid)
            _check_scope(scope, r["path"])
            _unchanged(r)
            try:
                os.remove(lp(r["path"]))
            except OSError as e:
                raise ActionError(explain_oserror(e))
            db.journal("delete", fid, r["path"], None, r["size"], r["mtime"])
            db.x("DELETE FROM files WHERE id=?", (fid,))
            deleted.append(fid)
            freed += r["size"]
        except ActionError as e:
            errors.append({"id": fid, "error": str(e)})
    if result is not None:
        result.remove(deleted)
    return {"deleted": len(deleted), "freed": freed, "errors": errors, "ids": deleted}


def _unique(dest_dir, name):
    stem, ext = os.path.splitext(name)
    target, n = os.path.join(dest_dir, name), 1
    while os.path.exists(lp(target)):
        target = os.path.join(dest_dir, "%s (%d)%s" % (stem, n, ext))
        n += 1
    return target


def _relocate(db, fid, new_path):
    st = os.stat(lp(new_path))
    db.x("UPDATE files SET path=?, folder=?, name=?, size=?, mtime=? WHERE id=?",
         (new_path, os.path.dirname(new_path), os.path.basename(new_path), st.st_size, st.st_mtime, fid))


def move(db, scope, result, ids, dest):
    if not dest:
        raise ActionError("destination manquante")
    dest = os.path.abspath(os.path.expanduser(dest))
    try:
        os.makedirs(lp(dest), exist_ok=True)
    except OSError as e:
        raise ActionError(explain_oserror(e))
    scope.add_dest(dest)
    moves, errors = [], []
    for fid in dict.fromkeys(int(i) for i in ids):
        try:
            r = _file(db, fid)
            _check_scope(scope, r["path"])
            if os.path.normcase(os.path.dirname(r["path"])) == os.path.normcase(dest):
                raise ActionError("déjà dans ce dossier")
            target = _unique(dest, r["name"])
            try:
                shutil.move(lp(r["path"]), lp(target))
            except OSError as e:
                raise ActionError(explain_oserror(e))
            _relocate(db, fid, target)
            jid = db.journal("move", fid, r["path"], target, r["size"], r["mtime"])
            moves.append({"id": fid, "journal": jid, "from": r["path"], "to": target})
        except ActionError as e:
            errors.append({"id": fid, "error": str(e)})
    if result is not None:
        result.remove([m["id"] for m in moves], remember=True)   # retirés des groupes, annulable
    return {"moved": len(moves), "moves": moves, "errors": errors}


def rename(db, scope, fid, new_name):
    new_name = (new_name or "").strip()
    if not new_name or any(c in new_name for c in '/\\:*?"<>|') or new_name in (".", ".."):
        raise ActionError("nom invalide")
    r = _file(db, fid)
    _check_scope(scope, r["path"])
    target = os.path.join(os.path.dirname(r["path"]), new_name)
    if os.path.exists(lp(target)) and os.path.normcase(target) != os.path.normcase(r["path"]):
        raise ActionError("un fichier porte déjà ce nom")
    try:
        os.rename(lp(r["path"]), lp(target))
    except OSError as e:
        raise ActionError(explain_oserror(e))
    _relocate(db, fid, target)
    jid = db.journal("rename", fid, r["path"], target, r["size"], r["mtime"])
    return {"ok": True, "journal": jid, "path": target}


def undo(db, scope, result, journal_ids):
    """Annule des déplacements / renommages (les suppressions sont définitives)."""
    done, errors = [], []
    for jid in journal_ids:
        j = db.one("SELECT * FROM journal WHERE id=?", (int(jid),))
        try:
            if not j:
                raise ActionError("entrée de journal introuvable")
            if j["action"] not in ("move", "rename"):
                raise ActionError("une suppression ne peut pas être annulée")
            if j["undone"]:
                raise ActionError("déjà annulé")
            if not os.path.exists(lp(j["dst"])):
                raise ActionError("le fichier n'est plus à %s" % j["dst"])
            if os.path.exists(lp(j["src"])):
                raise ActionError("emplacement d'origine occupé")
            os.makedirs(lp(os.path.dirname(j["src"])), exist_ok=True)
            try:
                shutil.move(lp(j["dst"]), lp(j["src"]))
            except OSError as e:
                raise ActionError(explain_oserror(e))
            if j["file_id"] and db.one("SELECT 1 FROM files WHERE id=?", (j["file_id"],)):
                _relocate(db, j["file_id"], j["src"])
            db.x("UPDATE journal SET undone=1 WHERE id=?", (j["id"],))
            db.journal("undo", j["file_id"], j["dst"], j["src"], info={"journal": j["id"]})
            done.append(j["file_id"])
        except ActionError as e:
            errors.append({"journal": jid, "error": str(e)})
    if result is not None:
        result.restore(done)
    return {"undone": len(done), "errors": errors}


def journal_page(db, page=1, per=50):
    total = db.one("SELECT COUNT(*) n FROM journal")["n"]
    rows = db.q("SELECT * FROM journal ORDER BY id DESC LIMIT ? OFFSET ?", (per, (page - 1) * per))
    out = []
    for r in rows:
        d = dict(r)
        if d.get("info"):
            try:
                d["info"] = json.loads(d["info"])
            except ValueError:
                pass
        out.append(d)
    return {"total": total, "entries": out}
