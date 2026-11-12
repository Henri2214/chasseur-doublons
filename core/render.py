"""Rendu EXACT des documents bureautiques : conversion en PDF par le logiciel qui sait les afficher.

Dessiner soi-même un .docx ou un .xlsx ne donne qu'une approximation. Pour une miniature et un aperçu fidèles
(mise en page, images, tableaux, polices), on demande la conversion en PDF à un vrai moteur installé sur le PC :

* Microsoft Office (Word, Excel, PowerPoint), piloté sans fenêtre par un script PowerShell ;
* LibreOffice (soffice --headless), sous Windows, Linux ou macOS.

Le PDF obtenu est gardé dans data/apercus/ ; sa première page sert de miniature et il s'affiche tel quel dans
l'aperçu. Les originaux ne sont jamais ouverts directement : on travaille sur une copie temporaire.

Sans moteur installé, seule la vignette enregistrée DANS le fichier par son logiciel (OpenDocument toujours,
PowerPoint le plus souvent, Word/Excel si l'option était cochée) est exacte ; sinon l'application affiche le
texte extrait, clairement marqué comme tel.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from .util import lp, norm

WORD = {".docx", ".docm", ".dotx", ".dotm", ".doc", ".dot", ".rtf", ".odt"}
EXCEL = {".xlsx", ".xlsm", ".xltx", ".xls", ".ods"}
PPT = {".pptx", ".ppsx", ".pptm", ".potx", ".ppt", ".pps", ".odp"}
CONVERTIBLE = WORD | EXCEL | PPT
MAX_BYTES = 80 * 1024 * 1024
BATCH = 12
_NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}
ENGINE_LABELS = {"office": "Microsoft Office", "libreoffice": "LibreOffice", "": "aucun"}

_PS1 = r'''
param([string]$Liste)
$ErrorActionPreference = "Stop"
$jobs = Get-Content -Raw -Encoding UTF8 $Liste | ConvertFrom-Json
$word = $null; $excel = $null; $ppt = $null; $pptAMoi = $false
function Dit($etat, $id, $msg) { [Console]::Out.WriteLine(("{0}`t{1}`t{2}" -f $etat, $id, ($msg -replace "[\r\n\t]", " "))) }
try {
  foreach ($j in $jobs) {
    try {
      if ($j.type -eq "word") {
        if (-not $word) { $word = New-Object -ComObject Word.Application; $word.Visible = $false; $word.DisplayAlerts = 0; try { $word.AutomationSecurity = 3 } catch {} }
        $d = $word.Documents.Open($j.src, $false, $true, $false, "x")
        try { $d.ExportAsFixedFormat($j.dst, 17) } finally { $d.Close($false) }
      } elseif ($j.type -eq "excel") {
        if (-not $excel) { $excel = New-Object -ComObject Excel.Application; $excel.Visible = $false; $excel.DisplayAlerts = $false; try { $excel.AutomationSecurity = 3 } catch {}; try { $excel.AskToUpdateLinks = $false } catch {} }
        $wb = $excel.Workbooks.Open($j.src, 0, $true, [Type]::Missing, "x")
        try { $wb.ExportAsFixedFormat(0, $j.dst, 0, $true, $false, 1, 3) } finally { $wb.Close($false) }
      } else {
        if (-not $ppt) { $ppt = New-Object -ComObject PowerPoint.Application; $pptAMoi = ($ppt.Presentations.Count -eq 0) }
        $p = $ppt.Presentations.Open($j.src, -1, 0, 0)
        try { $p.SaveAs($j.dst, 32) } finally { $p.Close() }
      }
      if (Test-Path -LiteralPath $j.dst) { Dit "OK" $j.id "" } else { Dit "ERR" $j.id "aucun PDF produit" }
    } catch { Dit "ERR" $j.id $_.Exception.Message }
  }
} finally {
  if ($word) { try { $word.Quit(0) } catch {}; [void][Runtime.InteropServices.Marshal]::ReleaseComObject($word) }
  if ($excel) { try { $excel.Quit() } catch {}; [void][Runtime.InteropServices.Marshal]::ReleaseComObject($excel) }
  if ($ppt) { if ($pptAMoi) { try { $ppt.Quit() } catch {} }; [void][Runtime.InteropServices.Marshal]::ReleaseComObject($ppt) }
  [GC]::Collect(); [GC]::WaitForPendingFinalizers()
}
'''


def kind_of(ext):
    ext = (ext or "").lower()
    return "word" if ext in WORD else "excel" if ext in EXCEL else "ppt" if ext in PPT else None


# ------------------------------------------------------------------ moteurs installés
def find_libreoffice():
    for name in ("soffice", "libreoffice", "soffice.exe"):
        p = shutil.which(name)
        if p:
            return p
    cands = []
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA") and os.path.join(os.environ["LOCALAPPDATA"], "Programs")):
        if base:
            cands += [os.path.join(base, "LibreOffice", "program", "soffice.exe"),
                      os.path.join(base, "LibreOffice 5", "program", "soffice.exe")]
    cands += ["/Applications/LibreOffice.app/Contents/MacOS/soffice", "/usr/lib/libreoffice/program/soffice"]
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def office_apps():
    """Applications Microsoft Office pilotables sur ce PC : {"word", "excel", "ppt"}."""
    if os.name != "nt":
        return set()
    out = set()
    try:
        import winreg
        for key, name in (("Word.Application", "word"), ("Excel.Application", "excel"),
                          ("PowerPoint.Application", "ppt")):
            try:
                winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, key + "\\CLSID"))
                out.add(name)
            except OSError:
                pass
    except Exception:  # noqa
        pass
    return out


def engines():
    """Moteurs disponibles, du plus fidèle au moins fidèle pour des fichiers Microsoft Office."""
    out = []
    if office_apps():
        out.append("office")
    if find_libreoffice():
        out.append("libreoffice")
    return out


# ------------------------------------------------------------------ vignette enregistrée dans le fichier
def embedded_thumbnail(path):
    """Vignette de la première page rangée dans un fichier OpenDocument (LibreOffice la refait à chaque
    enregistrement). Celle des fichiers Word/Excel/PowerPoint n'est pas utilisée : elle provient souvent du
    modèle d'origine et ne correspond plus au contenu."""
    if os.path.splitext(path)[1].lower() not in (".odt", ".ods", ".odp"):
        return None
    try:
        from PIL import Image
        with zipfile.ZipFile(lp(path)) as z:
            n = "Thumbnails/thumbnail.png"
            if n in z.namelist() and z.getinfo(n).file_size < 8 * 1024 * 1024:
                im = Image.open(io.BytesIO(z.read(n)))
                im.load()
                if min(im.size) >= 32:
                    bg = Image.new("RGB", im.size, "white")
                    bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[3])
                    return bg
    except Exception:  # noqa
        pass
    return None


# ------------------------------------------------------------------ conversions
def _kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, **_NOWIN)
        else:
            proc.kill()
    except Exception:  # noqa
        pass


def _run(cmd, timeout, cwd=None):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, **_NOWIN)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=10)
        except Exception:  # noqa
            pass
        return -1, "", "délai dépassé"


def _convert_libreoffice(jobs, work, profile, timeout):
    """jobs : [(id, copie source, pdf attendu)]. Retourne {id: erreur ou None}."""
    exe = find_libreoffice()
    if not exe:
        return {j[0]: "LibreOffice introuvable" for j in jobs}
    out = os.path.join(work, "pdf")
    os.makedirs(out, exist_ok=True)
    cmd = [exe, "--headless", "--norestore", "--nolockcheck", "--nodefault", "--nofirststartwizard",
           "-env:UserInstallation=" + Path(profile).resolve().as_uri(),
           "--convert-to", "pdf", "--outdir", out] + [j[1] for j in jobs]
    code, so, se = _run(cmd, timeout, cwd=work)
    res = {}
    for jid, src, dst in jobs:
        made = os.path.join(out, os.path.splitext(os.path.basename(src))[0] + ".pdf")
        if os.path.isfile(made) and os.path.getsize(made) > 0:
            shutil.move(made, dst)
            res[jid] = None
        else:
            res[jid] = "délai dépassé" if code == -1 else "conversion refusée par LibreOffice"
    return res


def _convert_office(jobs, work, timeout, apps):
    res, todo = {}, []
    for jid, src, dst in jobs:
        t = kind_of(os.path.splitext(src)[1])
        if t not in apps:
            res[jid] = "application Office absente pour ce type"
        else:
            todo.append({"id": str(jid), "type": t, "src": src, "dst": dst})
    if not todo:
        return res
    script, liste = os.path.join(work, "convertir.ps1"), os.path.join(work, "liste.json")
    with open(script, "w", encoding="utf-8-sig") as fh:
        fh.write(_PS1)
    with open(liste, "w", encoding="utf-8") as fh:
        json.dump(todo, fh)
    code, so, se = _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                         "-File", script, "-Liste", liste], timeout, cwd=work)
    said = {}
    for line in so.splitlines():
        p = line.split("\t")
        if len(p) >= 2 and p[0] in ("OK", "ERR"):
            said[p[1]] = None if p[0] == "OK" else (p[2] if len(p) > 2 and p[2] else "conversion refusée")
    for j in todo:
        jid = type(jobs[0][0])(j["id"]) if not isinstance(jobs[0][0], str) else j["id"]
        if j["id"] in said and (said[j["id"]] is not None or os.path.isfile(j["dst"])):
            res[jid] = said[j["id"]]
        else:
            res[jid] = "délai dépassé" if code == -1 else ((se.strip().splitlines() or ["conversion interrompue"])[-1])[:200]
    return res


class Previews:
    """Cache des PDF de rendu exact + file d'attente de conversion en arrière-plan."""

    def __init__(self, data_dir, db=None):
        self.dir = os.path.join(str(data_dir), "apercus")
        self.profile = os.path.join(str(data_dir), "libreoffice-profil")
        self.db = db
        self.lock = threading.Lock()          # un seul moteur de conversion à la fois
        self._q, self._qset = [], set()
        self._qlock = threading.Lock()
        self._worker = None
        self.stop = False
        self.last_error = ""

    # ---- configuration
    def available(self):
        return engines()

    def engine(self):
        """Moteur retenu : réglage « doc_preview_engine » (auto / office / libreoffice / aucun)."""
        want = (self.db.get_setting("doc_preview_engine", "auto") if self.db else "auto") or "auto"
        av = self.available()
        if want == "aucun":
            return ""
        if want in av:
            return want
        return av[0] if av else ""

    # ---- cache
    def key(self, path, mtime, size):
        return hashlib.sha1(("%s|%s|%s" % (norm(path), mtime, size)).encode()).hexdigest()

    def pdf_path(self, path, mtime, size):
        k = self.key(path, mtime, size)
        return os.path.join(self.dir, k[:2], k + ".pdf")

    def state(self, path, mtime, size):
        """"exact" (PDF prêt), "echec" (conversion déjà tentée sans succès) ou "" (rien)."""
        p = self.pdf_path(path, mtime, size)
        if os.path.isfile(p):
            return "exact"
        return "echec" if os.path.isfile(p[:-4] + ".err") else ""

    def wanted(self, path, size):
        return os.path.splitext(path)[1].lower() in CONVERTIBLE and 0 < size <= MAX_BYTES

    # ---- conversion
    def convert(self, rows, should_stop=None):
        """rows : [{"id", "path", "mtime", "size"}]. Convertit par lots ; retourne {id: erreur ou None}."""
        eng = self.engine()
        res = {}
        if not eng:
            return {r["id"]: "aucun moteur de rendu (Microsoft Office ou LibreOffice)" for r in rows}
        apps = office_apps() if eng == "office" else set()
        for i in range(0, len(rows), BATCH):
            if (should_stop and should_stop()) or self.stop:
                break
            batch = rows[i:i + BATCH]
            with self.lock:
                work = tempfile.mkdtemp(prefix="cd_apercu_")
                try:
                    jobs = []
                    for n, r in enumerate(batch):
                        ext = os.path.splitext(r["path"])[1].lower()
                        src = os.path.join(work, "d%03d%s" % (n, ext))
                        try:
                            shutil.copyfile(lp(r["path"]), src)
                        except OSError as e:
                            res[r["id"]] = "copie impossible (%s)" % type(e).__name__
                            continue
                        jobs.append((r["id"], src, os.path.join(work, "d%03d.out.pdf" % n)))
                    if jobs:
                        timeout = 60 + 45 * len(jobs)
                        got = (_convert_office(jobs, work, timeout, apps) if eng == "office"
                               else _convert_libreoffice(jobs, work, self.profile, timeout))
                        byid = {r["id"]: r for r in batch}
                        for jid, src, dst in jobs:
                            err = got.get(jid, "conversion interrompue")
                            r = byid[jid]
                            final = self.pdf_path(r["path"], r["mtime"], r["size"])
                            os.makedirs(os.path.dirname(final), exist_ok=True)
                            if err is None and os.path.isfile(dst):
                                shutil.move(dst, final + ".tmp")
                                os.replace(final + ".tmp", final)
                                try:
                                    os.remove(final[:-4] + ".err")
                                except OSError:
                                    pass
                            else:
                                err = err or "aucun PDF produit"
                                with open(final[:-4] + ".err", "w", encoding="utf-8") as fh:
                                    fh.write(err)
                                self.last_error = err
                            res[jid] = err
                finally:
                    shutil.rmtree(work, ignore_errors=True)
        return res

    # ---- arrière-plan (miniatures demandées avant que l'analyse ait fait le rendu)
    def request(self, row):
        if not self.engine() or not self.wanted(row["path"], row["size"]):
            return False
        if self.state(row["path"], row["mtime"], row["size"]):
            return False
        with self._qlock:
            if row["id"] in self._qset:
                return True
            self._qset.add(row["id"])
            self._q.append(dict(id=row["id"], path=row["path"], mtime=row["mtime"], size=row["size"]))
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, daemon=True)
                self._worker.start()
        return True

    def pending(self):
        with self._qlock:
            return len(self._qset)

    def _loop(self):
        while not self.stop:
            with self._qlock:
                batch, self._q = self._q[:BATCH], self._q[BATCH:]
            if not batch:
                return
            try:
                self.convert(batch)
            except Exception as e:  # noqa
                self.last_error = "%s: %s" % (type(e).__name__, e)
            finally:
                with self._qlock:
                    for r in batch:
                        self._qset.discard(r["id"])

    def forget_failures(self):
        n = 0
        for root, _, files in os.walk(self.dir):
            for f in files:
                if f.endswith(".err"):
                    try:
                        os.remove(os.path.join(root, f))
                        n += 1
                    except OSError:
                        pass
        return n

    def self_test(self):
        """Convertit un petit document Word de démonstration ; retourne (réussi, message, secondes)."""
        eng = self.engine()
        if not eng:
            return False, "Aucun moteur de rendu trouvé : installez Microsoft Office ou LibreOffice.", 0
        d = tempfile.mkdtemp(prefix="cd_essai_")
        t = time.time()
        try:
            src = os.path.join(d, "essai.docx")
            with zipfile.ZipFile(src, "w") as z:
                z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.'
                           'openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/'
                           'vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/'
                           'xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
                           'officedocument.wordprocessingml.document.main+xml"/></Types>')
                z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.'
                           'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
                           'openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                           '</Relationships>')
                z.writestr("word/document.xml", '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.'
                           'openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Essai du rendu exact'
                           '</w:t></w:r></w:p></w:body></w:document>')
            st = os.stat(src)
            row = {"id": 0, "path": src, "mtime": st.st_mtime, "size": st.st_size}
            err = self.convert([row]).get(0, "conversion interrompue")
            final = self.pdf_path(src, st.st_mtime, st.st_size)
            for f in (final, final[:-4] + ".err"):
                try:
                    os.remove(f)
                except OSError:
                    pass
            if err:
                return False, "%s : %s" % (ENGINE_LABELS[eng], err), round(time.time() - t, 1)
            return True, "%s : conversion réussie." % ENGINE_LABELS[eng], round(time.time() - t, 1)
        finally:
            shutil.rmtree(d, ignore_errors=True)
