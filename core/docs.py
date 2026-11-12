"""Documents : extraction du texte, empreintes de contenu et comparaison.

Les doublons « bit à bit » (SHA-256) fonctionnent déjà pour n'importe quel fichier. Ce module ajoute la
comparaison par le CONTENU pour les documents, afin de retrouver :

* le même texte enregistré deux fois (fichier ré-enregistré, .docx exporté en .pdf, .eml et .msg du même
  message) : empreinte du texte normalisé identique ;
* les versions proches d'un même document (quelques phrases changées) : indice de Jaccard estimé sur un
  échantillon des suites de 3 mots (« bottom-k sketch ») ;
* le même courriel présent plusieurs fois : même Message-ID.

Formats lus sans logiciel externe : Word (.docx .docm .dotx, .doc), OpenDocument (.odt .ods .odp), RTF,
Excel (.xlsx .xlsm, .xls approximatif), PowerPoint (.pptx .ppsx, .ppt approximatif), PDF (pypdfium2, avec
OCR des pages scannées si le module Texte est actif), texte (.txt .md .csv .log .html), courriels (.eml,
.msg Outlook via olefile).
"""
import hashlib
import html
import io
import os
import re
import struct
import threading
import unicodedata
import zipfile
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from .config import DOC_EXT, DOC_FAMILIES  # noqa: F401
from .util import lp

FAMILIES = DOC_FAMILIES
FAMILY_LABELS = {"word": "Word / texte mis en forme", "pdf": "PDF", "tableur": "Tableur",
                 "presentation": "Présentation", "texte": "Texte", "mail": "Courriel"}
EXT_FAMILY = {e: f for f, s in FAMILIES.items() for e in s}

EXTRACT_VERSION = 2               # à incrémenter quand la lecture change : les documents sont relus
MAX_BYTES = 200 * 1024 * 1024     # au-delà, le document n'est comparé que bit à bit
MAX_TEXT_BYTES = 30 * 1024 * 1024  # fichiers texte / csv
MAX_CHARS = 1_000_000              # texte conservé dans l'index par document
SKETCH_K = 128                    # taille de l'échantillon de suites de mots
MIN_WORDS_NEAR = 30               # en dessous, seules les copies de texte strictement identiques comptent
OCR_PAGES = 15                    # pages au plus passées à l'OCR pour un PDF scanné
PDF_PAGES = 2000

try:
    import pypdfium2 as pdfium
    PDF = True
except Exception:  # noqa
    pdfium = None
    PDF = False
try:
    import olefile
    OLE = True
except Exception:  # noqa
    olefile = None
    OLE = False

_PDF_LOCK = threading.Lock()      # PDFium n'est pas prévu pour plusieurs fils d'exécution


class Unsupported(Exception):
    """Contenu illisible pour une raison connue (chiffré, bibliothèque absente...)."""


def family(ext):
    return EXT_FAMILY.get((ext or "").lower())


# ------------------------------------------------------------------ normalisation et empreintes
_WORD = re.compile(r"[0-9a-z]+")


def fold(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def words(text):
    return _WORD.findall(fold(text))


def _h64(b):
    return int.from_bytes(hashlib.blake2b(b, digest_size=8).digest(), "big")


def fingerprint(text):
    """(empreinte du texte normalisé, échantillon trié de suites de 3 mots, nombre de mots)."""
    w = words(text)
    if not w:
        return None, b"", 0
    chash = hashlib.sha1(" ".join(w).encode()).hexdigest()
    if len(w) < 3:
        sh = {_h64(" ".join(w).encode())}
    else:
        sh = {_h64((w[i] + " " + w[i + 1] + " " + w[i + 2]).encode()) for i in range(len(w) - 2)}
    sk = sorted(sh)[:SKETCH_K]
    return chash, struct.pack("<%dQ" % len(sk), *sk), len(w)


def unpack_sketch(b):
    return struct.unpack("<%dQ" % (len(b) // 8), b) if b else ()


def jaccard(a, b):
    """Indice de Jaccard estimé à partir de deux échantillons triés (les k plus petites valeurs)."""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    union = sorted(sa | sb)[:SKETCH_K]
    return sum(1 for x in union if x in sa and x in sb) / float(len(union))


# ------------------------------------------------------------------ extraction
class _Strip(HTMLParser):
    SKIP = {"script", "style", "head", "title"}
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "td", "th"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_text(s):
    p = _Strip()
    try:
        p.feed(s)
        p.close()
    except Exception:  # noqa
        return re.sub(r"<[^>]+>", " ", s)
    return "".join(p.out)


def decode_bytes(b):
    if b[:3] == b"\xef\xbb\xbf":
        return b[3:].decode("utf-8", "replace")
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16", "replace")
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp1252", "replace")


def _read(path, limit=MAX_TEXT_BYTES):
    with open(lp(path), "rb") as fh:
        return fh.read(limit)


_XML_BREAK = re.compile(r"</(?:w:p|a:p|text:p|text:h|table:table-row|w:tr|row)>|<(?:w:br|w:tab|a:br|text:line-break|text:tab)\b[^>]*/?>")
_XML_TAG = re.compile(r"<[^>]+>")


def xml_text(data):
    s = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    s = _XML_BREAK.sub("\n", s)
    return html.unescape(_XML_TAG.sub("", s))


def _zip_members(z, pattern):
    rx = re.compile(pattern)
    names = [n for n in z.namelist() if rx.match(n)]

    def key(n):
        m = re.search(r"(\d+)\.xml$", n)
        return (int(m.group(1)) if m else 0, n)
    return sorted(names, key=key)


def _zread(z, name, limit=80 * 1024 * 1024):
    info = z.getinfo(name)
    if info.file_size > limit:        # protège de la « bombe zip »
        raise Unsupported("contenu compressé trop volumineux")
    return z.read(name)


def _custom_xml(z):
    """Données liées aux contrôles de contenu (customXml/item*.xml).

    Un modèle Word rempli par un logiciel (fiche d'intervention, bon, courrier type) garde un document.xml
    strictement identique d'un fichier à l'autre : les vraies valeurs (client, numéro, date...) sont ici,
    et Word les affiche à l'ouverture. Les ignorer ferait passer des documents différents pour des copies."""
    out = []
    for n in _zip_members(z, r"customXml/item\d+\.xml$"):
        s = _zread(z, n).decode("utf-8", "replace")
        vals = [html.unescape(v).strip() for v in re.findall(r">([^<>]+)<", s)]
        vals = [v for v in vals if v]
        if vals:
            out.append("\n".join(vals))
    return "\n".join(out)


def _ooxml_word(path):
    with zipfile.ZipFile(lp(path)) as z:
        names = set(z.namelist())
        if "word/document.xml" not in names:
            raise Unsupported("document Word sans contenu lisible")
        parts = (_zip_members(z, r"word/header\d*\.xml$") + ["word/document.xml"] +
                 [n for n in ("word/footnotes.xml", "word/endnotes.xml", "word/comments.xml") if n in names] +
                 _zip_members(z, r"word/footer\d*\.xml$"))
        texts = [xml_text(_zread(z, n)) for n in parts]
        texts.append(_custom_xml(z))
        return "\n".join(t for t in texts if t.strip())


def _ooxml_slides(path):
    with zipfile.ZipFile(lp(path)) as z:
        names = _zip_members(z, r"ppt/slides/slide\d+\.xml$") + _zip_members(z, r"ppt/notesSlides/notesSlide\d+\.xml$")
        return "\n\n".join([xml_text(_zread(z, n)) for n in names] + [_custom_xml(z)])


_CELL = re.compile(r"<c\b([^>]*?)(?:/>|>(.*?)</c>)", re.S)
_VAL = re.compile(r"<v>(.*?)</v>", re.S)
_SI = re.compile(r"<si>(.*?)</si>", re.S)


def _ooxml_sheet(path):
    with zipfile.ZipFile(lp(path)) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for m in _SI.finditer(_zread(z, "xl/sharedStrings.xml").decode("utf-8", "replace")):
                shared.append(html.unescape(_XML_TAG.sub("", re.sub(r"<rPh\b.*?</rPh>", "", m.group(1), flags=re.S))))
        out, total = [], 0
        for n in _zip_members(z, r"xl/worksheets/sheet\d+\.xml$"):
            s = _zread(z, n).decode("utf-8", "replace")
            for row in s.split("</row>"):
                cells = []
                for m in _CELL.finditer(row):
                    attrs, inner = m.group(1), m.group(2) or ""
                    v = _VAL.search(inner)
                    if 't="s"' in attrs and v:
                        try:
                            cells.append(shared[int(v.group(1))])
                        except (ValueError, IndexError):
                            pass
                    elif 't="inlineStr"' in attrs:
                        cells.append(html.unescape(_XML_TAG.sub("", inner)))
                    elif v:
                        cells.append(html.unescape(v.group(1)))
                if cells:
                    line = "\t".join(cells)
                    out.append(line)
                    total += len(line)
                    if total > MAX_CHARS:
                        return "\n".join(out)
        return "\n".join(out)


def _odf(path):
    with zipfile.ZipFile(lp(path)) as z:
        if "content.xml" not in z.namelist():
            raise Unsupported("document OpenDocument sans contenu lisible")
        s = _zread(z, "content.xml").decode("utf-8", "replace")
    s = re.sub(r"</table:table-cell>", "\t", s)
    return xml_text(s)


_RTF_DEST = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "header", "footer", "object", "themedata",
             "datastore", "latentstyles", "generator", "xmlnstbl", "listtable", "listoverridetable", "rsidtbl",
             "filetbl", "revtbl", "fldinst", "mmathPr", "colorschememapping", "wgrffmtfilter", "pgdsctbl"}
_RTF_TOK = re.compile(r"\\([a-zA-Z]{1,32})(-?\d{1,10})?[ ]?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|[\r\n]+|(.)", re.S)
_RTF_CHARS = {"par": "\n", "sect": "\n\n", "page": "\n\n", "line": "\n", "tab": "\t", "emdash": "\u2014",
              "endash": "\u2013", "lquote": "\u2018", "rquote": "\u2019", "ldblquote": "\u201c",
              "rdblquote": "\u201d", "bullet": "\u2022", "cell": "\t", "row": "\n"}


def rtf_text(s):
    stack, ignorable, ucskip, cur = [], False, 1, 0
    out = []
    for m in _RTF_TOK.finditer(s):
        word, arg, hexa, char, brace, tchar = m.groups()
        if brace:
            cur = 0
            if brace == "{":
                stack.append((ucskip, ignorable))
            elif stack:
                ucskip, ignorable = stack.pop()
        elif char:
            cur = 0
            if char == "~":
                if not ignorable:
                    out.append("\xa0")
            elif char in "{}\\":
                if not ignorable:
                    out.append(char)
            elif char == "*":
                ignorable = True
        elif word:
            cur = 0
            if word in _RTF_DEST:
                ignorable = True
            elif ignorable:
                pass
            elif word in _RTF_CHARS:
                out.append(_RTF_CHARS[word])
            elif word == "uc":
                ucskip = int(arg or 1)
            elif word == "u":
                c = int(arg or 0)
                if c < 0:
                    c += 0x10000
                out.append(chr(c) if c < 0x110000 else "?")
                cur = ucskip
        elif hexa:
            if cur > 0:
                cur -= 1
            elif not ignorable:
                out.append(bytes([int(hexa, 16)]).decode("cp1252", "replace"))
        elif tchar:
            if cur > 0:
                cur -= 1
            elif not ignorable:
                out.append(tchar)
    return "".join(out)


_STR16 = re.compile(rb"(?:[\x20-\x7e\xa0-\xff]\x00){4,}")
_STR8 = re.compile(rb"[\x20-\x7e\xc0-\xff\t]{5,}")


def _strings(data):
    """Extraction approximative des chaînes d'un flux binaire (anciens .xls / .ppt)."""
    out = []
    for m in _STR16.finditer(data):
        out.append(m.group().decode("utf-16-le", "ignore"))
    rest = _STR16.sub(b"\n", data)
    for m in _STR8.finditer(rest):
        t = m.group().decode("cp1252", "ignore")
        if sum(c.isalpha() or c == " " for c in t) >= 0.7 * len(t):
            out.append(t)
    return "\n".join(out)


def _need_ole():
    if not OLE:
        raise Unsupported("module olefile absent (pip install olefile)")


def _doc_binary(path):
    """Ancien format Word (.doc) : texte reconstitué à partir de la table des morceaux (piece table)."""
    _need_ole()
    with olefile.OleFileIO(lp(path)) as ole:
        if not ole.exists("WordDocument"):
            raise Unsupported("ancien document Word sans flux principal")
        wd = ole.openstream("WordDocument").read()
        try:
            flags = struct.unpack_from("<H", wd, 0x0A)[0]
            if flags & 0x0100:
                raise Unsupported("document Word protégé par mot de passe")
            tname = "1Table" if flags & 0x0200 else "0Table"
            fc_clx, lcb_clx = struct.unpack_from("<II", wd, 0x01A2)
            tbl = ole.openstream(tname).read()
            clx = tbl[fc_clx: fc_clx + lcb_clx]
            pos = 0
            while pos < len(clx) and clx[pos] == 1:
                pos += 3 + struct.unpack_from("<H", clx, pos + 1)[0]
            if pos >= len(clx) or clx[pos] != 2:
                raise ValueError("table des morceaux absente")
            lcb = struct.unpack_from("<I", clx, pos + 1)[0]
            plc = clx[pos + 5: pos + 5 + lcb]
            n = (len(plc) - 4) // 12
            cps = struct.unpack_from("<%dI" % (n + 1), plc, 0)
            out = []
            for i in range(n):
                fc = struct.unpack_from("<I", plc, 4 * (n + 1) + 8 * i + 2)[0]
                count = cps[i + 1] - cps[i]
                if count <= 0 or count > 50_000_000:
                    continue
                if fc & 0x40000000:
                    off = (fc & 0x3FFFFFFF) // 2
                    out.append(wd[off: off + count].decode("cp1252", "replace"))
                else:
                    out.append(wd[fc: fc + 2 * count].decode("utf-16-le", "replace"))
            t = "".join(out)
        except Unsupported:
            raise
        except Exception:  # noqa
            t = _strings(wd)
    t = t.replace("\r", "\n").replace("\x07", "\t").replace("\x0b", "\n").replace("\x0c", "\n")
    t = re.sub(r"\x13[^\x14\x15]*\x14", "", t)        # codes de champ : on garde le résultat affiché
    return re.sub(r"[\x00-\x08\x0e-\x1f]", "", t)


def _xls_sst(data):
    """Chaînes partagées d'un classeur Excel 97-2003 (enregistrement SST et ses suites)."""
    pos, sst = 0, None
    while pos + 4 <= len(data):
        rid, ln = struct.unpack_from("<HH", data, pos)
        body = data[pos + 4: pos + 4 + ln]
        pos += 4 + ln
        if rid == 0x00FC:
            sst = bytearray(body)
        elif rid == 0x003C and sst is not None:
            sst += body
        elif sst is not None:
            break
    if sst is None:
        return None
    n = struct.unpack_from("<I", sst, 4)[0]
    p, out = 8, []
    for _ in range(min(n, 2_000_000)):
        if p + 3 > len(sst):
            break
        cch, fl = struct.unpack_from("<HB", sst, p)
        p += 3
        runs = ext = 0
        if fl & 0x08:
            runs = struct.unpack_from("<H", sst, p)[0]
            p += 2
        if fl & 0x04:
            ext = struct.unpack_from("<I", sst, p)[0]
            p += 4
        if fl & 0x01:
            out.append(bytes(sst[p: p + 2 * cch]).decode("utf-16-le", "replace"))
            p += 2 * cch
        else:
            out.append(bytes(sst[p: p + cch]).decode("latin-1"))
            p += cch
        p += 4 * runs + ext
    return "\n".join(out)


def _xls(path):
    _need_ole()
    with olefile.OleFileIO(lp(path)) as ole:
        for name in ("Workbook", "Book"):
            if ole.exists(name):
                data = ole.openstream(name).read(MAX_TEXT_BYTES)
                try:
                    t = _xls_sst(data)
                except Exception:  # noqa
                    t = None
                return t if t is not None else _strings(data)
    raise Unsupported("ancien classeur Excel non reconnu")


def _ole_strings(path, streams):
    _need_ole()
    with olefile.OleFileIO(lp(path)) as ole:
        for s in streams:
            if ole.exists(s):
                return _strings(ole.openstream(s).read(MAX_TEXT_BYTES))
    raise Unsupported("ancien format Office non reconnu")


def _pdf(path, ocr=None):
    if not PDF:
        raise Unsupported("module pypdfium2 absent (pip install pypdfium2)")
    texts, method, npages = [], "texte", 0
    with _PDF_LOCK:
        try:
            pdf = pdfium.PdfDocument(lp(path))
        except Exception as e:  # noqa
            msg = str(e)
            raise Unsupported("PDF protégé par mot de passe" if "assword" in msg else "PDF illisible (%s)" % msg[:80])
        try:
            npages = len(pdf)
            total = 0
            for i in range(min(npages, PDF_PAGES)):
                page = pdf[i]
                tp = page.get_textpage()
                try:
                    t = tp.get_text_bounded() if hasattr(tp, "get_text_bounded") else tp.get_text_range()
                finally:
                    tp.close()
                    page.close()
                texts.append(t or "")
                total += len(t or "")
                if total > MAX_CHARS:
                    break
            # PDF scanné : pratiquement aucun texte -> OCR des premières pages
            rendered = None
            if ocr is not None and npages and sum(len(t.strip()) for t in texts) < 40 * max(1, len(texts)):
                import numpy as np
                rendered = []
                for i in range(min(npages, OCR_PAGES)):
                    page = pdf[i]
                    try:
                        w = page.get_width() or 595
                        bmp = page.render(scale=min(2.5, max(1.0, 1600.0 / w)))
                        rendered.append(np.ascontiguousarray(bmp.to_numpy()[:, :, :3]))
                    finally:
                        page.close()
        finally:
            pdf.close()
    if rendered:
        ot = []
        for arr in rendered:
            try:
                ot.append(ocr.read(arr))
            except Exception:  # noqa
                pass
        if any(x.strip() for x in ot):
            texts, method = ot, "ocr"
    return "\n\n".join(texts), method, npages


def _mail_date(v):
    try:
        return parsedate_to_datetime(str(v)).timestamp()
    except Exception:  # noqa
        return None


def _norm_mid(v):
    v = (v or "").strip().strip("<>").strip().lower()
    return v or None


def _addr(v):
    m = re.search(r"[\w.+-]+@[\w.-]+", str(v or ""))
    return m.group().lower() if m else (str(v or "").strip().lower() or None)


def _eml(path):
    with open(lp(path), "rb") as fh:
        msg = BytesParser(policy=policy.default).parse(fh)
    body = ""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            body = part.get_content()
            if part.get_content_type() == "text/html":
                body = html_text(body)
    except Exception:  # noqa
        pl = msg.get_payload(decode=True)
        body = decode_bytes(pl) if isinstance(pl, bytes) else ""
    att = []
    try:
        att = [a.get_filename() for a in msg.iter_attachments() if a.get_filename()]
    except Exception:  # noqa
        pass
    subject = str(msg.get("Subject", "") or "")
    meta = {"mid": _norm_mid(msg.get("Message-ID")), "sender": _addr(msg.get("From")),
            "mdate": _mail_date(msg.get("Date")), "title": subject,
            "extra": {"de": str(msg.get("From", "") or ""), "a": str(msg.get("To", "") or ""), "pieces": att}}
    return subject + "\n\n" + (body or ""), meta


def _msg(path):
    """Message Outlook (.msg) lu directement dans le conteneur OLE (propriétés MAPI)."""
    _need_ole()
    with olefile.OleFileIO(lp(path)) as ole:
        def prop(tag):
            for suffix, enc in (("001F", "utf-16-le"), ("001E", "cp1252")):
                name = "__substg1.0_%s%s" % (tag, suffix)
                if ole.exists(name):
                    return ole.openstream(name).read().decode(enc, "replace").rstrip("\x00")
            return ""
        subject, body = prop("0037"), prop("1000")
        if not body and ole.exists("__substg1.0_10130102"):
            body = html_text(decode_bytes(ole.openstream("__substg1.0_10130102").read()))
        headers = prop("007D")
        sender = prop("5D01") or prop("0C1F") or prop("0065") or prop("0C1A")
        mid = prop("1035")
        mdate = None
        if headers:
            try:
                h = BytesParser(policy=policy.default).parsebytes(headers.encode("utf-8", "replace"), headersonly=True)
                mdate = _mail_date(h.get("Date"))
                mid = mid or str(h.get("Message-ID", "") or "")
                if "@" not in sender:
                    sender = str(h.get("From", "") or sender)
            except Exception:  # noqa
                pass
        if mdate is None and ole.exists("__properties_version1.0"):
            p = ole.openstream("__properties_version1.0").read()
            for off in range(32, len(p) - 15, 16):         # 0x0E060040 remise, 0x00390040 envoi
                tag = struct.unpack_from("<I", p, off)[0]
                if tag in (0x00390040, 0x0E060040):
                    ft = struct.unpack_from("<Q", p, off + 8)[0]
                    if ft:
                        mdate = ft / 1e7 - 11644473600
                        if tag == 0x00390040:
                            break
        att = []
        for entry in ole.listdir(streams=False, storages=True):
            if len(entry) == 1 and entry[0].startswith("__attach_version1.0_"):
                for tag in ("3707", "3704"):
                    for suffix, enc in (("001F", "utf-16-le"), ("001E", "cp1252")):
                        n = "%s/__substg1.0_%s%s" % (entry[0], tag, suffix)
                        if ole.exists(n):
                            att.append(ole.openstream(n).read().decode(enc, "replace").rstrip("\x00"))
                            break
                    else:
                        continue
                    break
        meta = {"mid": _norm_mid(mid), "sender": _addr(sender), "mdate": mdate, "title": subject,
                "extra": {"de": sender, "a": prop("0E04"), "pieces": att}}
        return subject + "\n\n" + body, meta


def extract(path, ext=None, ocr=None):
    """Retourne {"text", "method", "pages", "title", "mid", "sender", "mdate", "extra"}.

    Lève Unsupported (cause claire) ou une autre exception (fichier abîmé)."""
    ext = (ext or os.path.splitext(path)[1]).lower()
    out = {"text": "", "method": "texte", "pages": None, "title": None, "mid": None, "sender": None,
           "mdate": None, "extra": None}
    with open(lp(path), "rb") as fh:
        head = fh.read(8)
    is_zip, is_ole = head[:2] == b"PK", head == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    if ext == ".pdf":
        out["text"], out["method"], out["pages"] = _pdf(path, ocr)
    elif ext in (".docx", ".docm", ".dotx", ".dotm") or (ext in (".doc", ".dot") and is_zip):
        out["text"] = _ooxml_word(path)
    elif ext in (".doc", ".dot"):
        if head[:5] == b"{\\rtf":
            out["text"] = rtf_text(decode_bytes(_read(path)))
        elif is_ole:
            out["text"] = _doc_binary(path)
        else:
            out["text"] = html_text(decode_bytes(_read(path))) if b"<" in head else decode_bytes(_read(path))
    elif ext in (".odt", ".ods", ".odp"):
        out["text"] = _odf(path)
    elif ext == ".rtf":
        out["text"] = rtf_text(decode_bytes(_read(path)))
    elif ext in (".xlsx", ".xlsm", ".xltx") or (ext == ".xls" and is_zip):
        out["text"] = _ooxml_sheet(path)
    elif ext == ".xls":
        if is_ole:
            out["text"], out["method"] = _xls(path), "approx"
        else:                                   # export HTML ou CSV renommé en .xls
            raw = decode_bytes(_read(path))
            out["text"] = html_text(raw) if "<" in raw[:2000] else raw
    elif ext in (".pptx", ".ppsx", ".pptm", ".potx") or (ext in (".ppt", ".pps") and is_zip):
        out["text"] = _ooxml_slides(path)
    elif ext in (".ppt", ".pps"):
        out["text"], out["method"] = _ole_strings(path, ("PowerPoint Document",)), "approx"
    elif ext in (".htm", ".html"):
        out["text"] = html_text(decode_bytes(_read(path)))
    elif ext == ".eml":
        out["text"], meta = _eml(path)
        out.update(meta)
    elif ext == ".msg":
        out["text"], meta = _msg(path)
        out.update(meta)
    else:
        out["text"] = decode_bytes(_read(path))
    t = out["text"] or ""
    t = re.sub(r"[ \t\xa0]+", " ", t.replace("\x00", ""))
    t = re.sub(r" ?\n ?", "\n", t)
    out["text"] = re.sub(r"\n{3,}", "\n\n", t).strip()[:MAX_CHARS]
    return out


# ------------------------------------------------------------------ comparaison
def links(db, ids, thr=0.8, progress=None):
    """Liens de contenu entre documents indexés. Retourne [(a, b, note 0-100, type)], type parmi
    "mail" (même Message-ID), "contenu" (même texte) et "proche" (versions voisines)."""
    rows = []
    for i in range(0, len(ids), 900):
        ch = ids[i:i + 900]
        rows += db.q("SELECT file_id, chash, sketch, words, mid, sender, mdate FROM doctext WHERE file_id IN (%s)"
                     % ",".join("?" * len(ch)), ch)
    out, linked = [], set()

    def add(a, b, s, kind):
        k = (a, b) if a < b else (b, a)
        if k not in linked:
            linked.add(k)
            out.append((k[0], k[1], s, kind))

    def star(groups, kind):
        for g in groups.values():
            for x in g[1:]:
                add(g[0], x, 100.0, kind)

    by_mid, by_hash = {}, {}
    for r in rows:
        if r["mid"]:
            by_mid.setdefault(r["mid"], []).append(r["file_id"])
        if r["chash"] and (r["words"] or 0) >= 3:
            # deux courriels au même texte mais envoyés séparément (relance, notification) ne sont pas des doublons
            mail = (r["mid"] or (r["sender"], round((r["mdate"] or 0) / 300.0))) \
                if (r["mid"] or r["sender"] or r["mdate"] is not None) else None
            by_hash.setdefault((r["chash"], mail), []).append(r["file_id"])
    star(by_mid, "mail")
    star(by_hash, "contenu")

    # versions proches : un représentant par texte identique, index inversé sur l'échantillon
    info, reps = {}, []
    seen_hash = set()
    for r in rows:
        hk = (r["chash"], r["mid"], r["sender"], r["mdate"])
        if (r["words"] or 0) < MIN_WORDS_NEAR or not r["sketch"] or hk in seen_hash:
            continue
        seen_hash.add(hk)
        info[r["file_id"]] = (unpack_sketch(r["sketch"]), r["mid"], r["sender"], r["mdate"])
        reps.append(r["file_id"])
    inv = {}
    for f in reps:
        for h in info[f][0]:
            inv.setdefault(h, []).append(f)
    counts = {}
    for lst in inv.values():
        if len(lst) < 2 or len(lst) > 400:     # formules présentes partout (pieds de page, signatures)
            continue
        for i in range(len(lst) - 1):
            a = lst[i]
            for b in lst[i + 1:]:
                k = (a, b) if a < b else (b, a)
                counts[k] = counts.get(k, 0) + 1
    if progress:
        progress("Comparaison du contenu de %d paires de documents" % len(counts))
    for (a, b), n in counts.items():
        sa, ma, sea, da = info[a]
        sb, mb, seb, db_ = info[b]
        if n < max(1, int(0.3 * thr * min(len(sa), len(sb)))):
            continue
        mail_a = bool(ma or sea or da is not None)
        mail_b = bool(mb or seb or db_ is not None)
        if mail_a and mail_b:               # deux courriels : même message exigé, pas seulement le même texte
            if ma and mb and ma != mb:
                continue
            if sea and seb and sea != seb:
                continue
            if da is not None and db_ is not None and abs(da - db_) > 300:
                continue
        j = jaccard(sa, sb)
        if j >= thr:
            add(a, b, 100.0 * j, "proche")
    return out


PAIRS_PER_FILE = 5      # un même document apparaît au plus dans ses 5 paires les plus ressemblantes
PAIRS_MAX = 5000


def groups(db, ids, thr=0.8, progress=None):
    """Regroupements de documents : [(type, [(id, note, repère), ...])], référence en tête.

    * "mail" / "contenu" : fichiers au contenu strictement identique — un groupe par contenu, quel que soit
      le nombre de copies (ce sont les mêmes documents) ;
    * "proche" : versions voisines, présentées PAR PAIRES. Chaque paire est une comparaison précise entre deux
      documents, avec sa propre note ; on ne fabrique jamais un gros bloc « A ressemble à B qui ressemble à C ».
      Quand un contenu existe en plusieurs copies identiques, une seule d'entre elles figure dans la paire."""
    lk = links(db, ids, thr, progress)
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    near = []
    for a, b, s_, kind in lk:
        if kind == "proche":
            near.append((a, b, s_))
        else:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
    need = set(parent) | {x for a, b, _ in near for x in (a, b)}
    info = {}
    need = list(need)
    for i in range(0, len(need), 900):
        ch = need[i:i + 900]
        for r in db.q("SELECT d.file_id, d.chash, d.words, d.mid, f.size, f.mtime FROM doctext d "
                      "JOIN files f ON f.id=d.file_id WHERE d.file_id IN (%s)" % ",".join("?" * len(ch)), ch):
            info[r["file_id"]] = r

    def rang(m):
        return (info[m]["words"] or 0, info[m]["mtime"], info[m]["size"])
    out = []
    comps = {}
    for x in list(parent):
        if x in info:
            comps.setdefault(find(x), []).append(x)
    for mem in comps.values():
        if len(mem) < 2:
            continue
        mem.sort(key=rang, reverse=True)
        mids = {info[m]["mid"] for m in mem}
        kind = "mail" if len(mids) == 1 and None not in mids else "contenu"
        out.append((kind, [(m, 100.0, None) for m in mem]))
    near.sort(key=lambda t: -t[2])
    count, n = {}, 0
    for a, b, s_ in near:
        if a not in info or b not in info:
            continue
        if count.get(a, 0) >= PAIRS_PER_FILE or count.get(b, 0) >= PAIRS_PER_FILE:
            continue
        count[a] = count.get(a, 0) + 1
        count[b] = count.get(b, 0) + 1
        ref, autre = (a, b) if rang(a) >= rang(b) else (b, a)
        out.append(("proche", [(ref, 100.0, None), (autre, s_, None)]))
        n += 1
        if n >= PAIRS_MAX:
            break
    return out


def compare(db, a, b, max_lines=3000):
    """Comparaison ligne à ligne du texte de deux documents : [(état, texte A, texte B)], état parmi
    "=" (identique), "~" (modifié), "-" (seulement dans A), "+" (seulement dans B)."""
    import difflib

    def lines(fid):
        r = db.one("SELECT text FROM doctext WHERE file_id=?", (fid,))
        return [x.strip() for x in ((r["text"] if r else "") or "").split("\n") if x.strip()][:max_lines]
    la, lb = lines(a), lines(b)
    sm = difflib.SequenceMatcher(None, [fold(x) for x in la], [fold(x) for x in lb], autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out += [("=", la[i], lb[j1 + (i - i1)]) for i in range(i1, i2)]
        elif tag == "replace":
            n = max(i2 - i1, j2 - j1)
            for k in range(n):
                x = la[i1 + k] if i1 + k < i2 else None
                y = lb[j1 + k] if j1 + k < j2 else None
                out.append(("~" if x is not None and y is not None else "-" if y is None else "+", x, y))
        elif tag == "delete":
            out += [("-", la[i], None) for i in range(i1, i2)]
        else:
            out += [("+", None, lb[j]) for j in range(j1, j2)]
    same = sum(1 for t in out if t[0] == "=")
    return {"rows": out, "same": same, "diff": len(out) - same, "ratio": round(sm.ratio(), 3)}


def similar_docs(db, file_id, thr=0.3, limit=2000):
    """Documents dont le contenu ressemble à celui de `file_id` : [(id, note 0-1)]."""
    ref = db.one("SELECT chash, sketch, mid FROM doctext WHERE file_id=?", (file_id,))
    if not ref or not ref["sketch"]:
        return []
    sa = unpack_sketch(ref["sketch"])
    seta = set(sa)
    out = []
    for r in db.q("SELECT file_id, chash, sketch, mid FROM doctext WHERE file_id<>? AND sketch IS NOT NULL", (file_id,)):
        if (ref["mid"] and r["mid"] == ref["mid"]) or r["chash"] == ref["chash"]:
            out.append((r["file_id"], 1.0))
            continue
        sb = unpack_sketch(r["sketch"])
        if not seta.intersection(sb):
            continue
        j = jaccard(sa, sb)
        if j >= thr:
            out.append((r["file_id"], round(j, 3)))
    out.sort(key=lambda t: -t[1])
    return out[:limit]


def snippet(text, query_words, width=220):
    """Extrait du texte autour du premier mot recherché."""
    if not text:
        return ""
    low = fold(text)
    if len(low) != len(text):
        low = text.lower()
    pos = -1
    for w in query_words:
        pos = low.find(fold(w))
        if pos >= 0:
            break
    a = max(0, pos - width // 3)
    s = text[a: a + width].replace("\n", " ")
    return ("… " if a else "") + s + (" …" if a + width < len(text) else "")


# ------------------------------------------------------------------ miniature d'un document (aperçu du texte)
FAMILY_COLORS = {"word": (43, 87, 154), "tableur": (33, 115, 70), "presentation": (210, 71, 38),
                 "mail": (0, 120, 140), "texte": (95, 105, 125), "pdf": (200, 40, 40)}
_FONTS = {}


def _font(size, bold=False):
    key = (size, bold)
    if key not in _FONTS:
        from PIL import ImageFont
        f = None
        names = (("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf") if bold
                 else ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"))
        for n in names + ("/usr/share/fonts/truetype/dejavu/" + names[-1],):
            try:
                f = ImageFont.truetype(n, size)
                break
            except Exception:  # noqa
                continue
        if f is None:
            try:
                f = ImageFont.load_default(size)
            except TypeError:          # Pillow < 10.1 : police fixe
                f = ImageFont.load_default()
        _FONTS[key] = f
    return _FONTS[key]


def text_thumbnail(text, ext, size=400, title=None, note=None):
    """Image « texte seul » : bandeau de couleur du type de document, puis le début du texte extrait.
    `note` est écrite en pied d'image pour qu'on ne la prenne pas pour le rendu exact du document."""
    from PIL import Image, ImageDraw
    H = int(size)
    W = int(H * 0.76)
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    col = FAMILY_COLORS.get(family(ext), (95, 105, 125))
    band = max(16, H // 11)
    d.rectangle([0, 0, W, band], fill=col)
    fb = _font(max(9, int(band * 0.55)), True)
    d.text((max(5, W // 28), band * 0.2), (ext or "").lstrip(".").upper(), fill="white", font=fb)
    fs = max(7, H // 36)
    f, ft = _font(fs), _font(fs + 1, True)
    margin, y = max(5, W // 16), band + max(4, H // 40)
    maxw, lh = W - 2 * margin, int(fs * 1.38)

    def wrap(line, font):
        out, cur = [], ""
        for w in line.split(" "):
            t = (cur + " " + w).strip()
            if d.textlength(t, font=font) <= maxw:
                cur = t
            else:
                if cur:
                    out.append(cur)
                while d.textlength(w, font=font) > maxw and len(w) > 1:     # mot plus large que la page
                    k = max(1, int(len(w) * maxw / d.textlength(w, font=font)))
                    out.append(w[:k])
                    w = w[k:]
                cur = w
        out.append(cur)
        return out
    if title:
        for ln in wrap(title.strip()[:160], ft)[:2]:
            d.text((margin, y), ln, fill=(20, 28, 45), font=ft)
            y += lh + 1
        y += lh // 3
    for raw in (text or "")[:6000].split("\n"):
        if y + lh > H - margin // 2:
            break
        raw = raw.strip()
        if not raw:
            y += lh // 2
            continue
        for ln in wrap(raw, f):
            if y + lh > H - margin // 2:
                break
            d.text((margin, y), ln, fill=(55, 62, 78), font=f)
            y += lh
    if note and H >= 120:
        fn = _font(max(7, H // 40), True)
        hb = int(fn.size * 1.9) if hasattr(fn, "size") else 14
        d.rectangle([0, H - hb, W, H], fill=(255, 244, 214))
        d.text((margin, H - hb + hb * 0.2), note, fill=(150, 90, 0), font=fn)
    d.rectangle([0, 0, W - 1, H - 1], outline=(190, 198, 212))
    return im
