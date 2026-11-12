"""Documents : extraction du texte, même contenu, versions proches, courriels, recherche, bouton Quitter."""
import os
import shutil
import time
import zipfile
from email.message import EmailMessage

import pytest

from core import docs, dupes, render, search, similar
from core.web import create_app

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
PHRASES = [
    "Le contrat de maintenance du pare-feu est renouvelé pour une durée de douze mois.",
    "Le prestataire s'engage à intervenir sous quatre heures ouvrées en cas d'incident bloquant.",
    "Les mises à jour de sécurité sont appliquées chaque mois après validation du client.",
    "La facturation est trimestrielle, à terme échu, payable à trente jours fin de mois.",
    "Toute modification du périmètre fera l'objet d'un avenant signé par les deux parties.",
    "Le client met à disposition un accès distant sécurisé par réseau privé virtuel.",
    "Les sauvegardes de configuration sont conservées pendant quatre-vingt-dix jours.",
    "En cas de litige, le tribunal de commerce de Poitiers sera seul compétent.",
    "Le présent contrat prend effet à la date de signature par les deux parties.",
    "Les données personnelles sont traitées conformément au règlement en vigueur.",
] * 3
RECETTE = ["Éplucher six pommes, préparer une pâte brisée puis cuire quarante minutes à four chaud.",
           "Servir tiède avec de la crème fraîche et un peu de cannelle selon les goûts de chacun."] * 6


def _x(s):
    return s.replace("&", "&amp;").replace("<", "&lt;")


def make_docx(path, paras, comment="", donnees=None, entete=None):
    body = "".join("<w:p><w:r><w:t>%s</w:t></w:r></w:p>" % _x(p) for p in paras)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats'
                   '.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-'
                   'package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName='
                   '"/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.'
                   'document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats'
                   '.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                   'officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("docProps/core.xml", "<c>%s</c>" % comment)
        z.writestr("word/document.xml", '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.'
                   'openxmlformats.org/wordprocessingml/2006/main"><w:body>%s</w:body></w:document>' % body)
        if entete:
            z.writestr("word/header1.xml", "<w:hdr><w:p><w:r><w:t>%s</w:t></w:r></w:p></w:hdr>" % _x(entete))
        if donnees:
            z.writestr("customXml/itemProps1.xml", "<ds:datastoreItem/>")
            z.writestr("customXml/item1.xml", "<fiche>%s</fiche>" % "".join(
                "<%s>%s</%s>" % (k, _x(v), k) for k, v in donnees.items()))


def make_odt(path, paras):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml", "<office:document-content><office:body><office:text>%s</office:text></office:body>"
                                  "</office:document-content>" % "".join("<text:p>%s</text:p>" % _x(p) for p in paras))


def make_xlsx(path, rows):
    strings = sorted({c for r in rows for c in r})
    sst = "<sst>%s</sst>" % "".join("<si><t>%s</t></si>" % _x(c) for c in strings)
    sheet = "<worksheet><sheetData>%s</sheetData></worksheet>" % "".join(
        "<row>%s</row>" % "".join('<c t="s"><v>%d</v></c>' % strings.index(c) for c in r) for r in rows)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/sharedStrings.xml", sst)
        z.writestr("xl/worksheets/sheet1.xml", sheet)


def make_pdf(path, lines):
    """PDF minimal et valide (une page, police Helvetica, encodage WinAnsi)."""
    esc = lambda t: t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 9 Tf 30 810 Td 11 TL " + " ".join("(%s) Tj T*" % esc(t) for t in lines) + " ET"
    content = content.encode("cp1252")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    with open(path, "wb") as fh:
        fh.write(bytes(out))


def make_eml(path, mid, body, date="Mon, 02 Nov 2026 10:15:00 +0100", extra=b""):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "Henri <henri@example.fr>", "client@example.com", "Devis pare-feu"
    m["Date"], m["Message-ID"] = date, mid
    m.set_content(body)
    with open(path, "wb") as fh:
        fh.write(extra + m.as_bytes())


MAIL = ("Bonjour,\n\nVeuillez trouver ci-joint le devis pour le remplacement du pare-feu et la migration des règles "
        "de filtrage vers le nouvel équipement. La prestation comprend l'installation sur site, la reprise de la "
        "configuration existante, les tests de bascule ainsi qu'une demi-journée de formation.\n\nCordialement")


@pytest.fixture
def dossier(tmp_path):
    root = tmp_path / "docs"
    (root / "archives").mkdir(parents=True)
    make_docx(root / "contrat.docx", PHRASES)
    shutil.copy(root / "contrat.docx", root / "archives" / "contrat - Copie.docx")     # bit à bit
    make_docx(root / "contrat_reenregistre.docx", PHRASES, comment="autres métadonnées")  # même texte
    make_odt(root / "contrat.odt", PHRASES)                                             # même texte, autre format
    v2 = list(PHRASES)
    v2[3] = "La facturation est mensuelle, payable à quarante-cinq jours."
    make_docx(root / "contrat_v2.docx", v2 + ["Annexe : liste des équipements couverts."])
    make_docx(root / "recette.docx", RECETTE)
    make_eml(root / "devis.eml", "<abc123@example.fr>", MAIL)
    make_eml(root / "archives" / "devis (1).eml", "<abc123@example.fr>", MAIL, extra=b"X-Export: outil\r\n")
    make_eml(root / "relance.eml", "<zzz999@example.fr>", MAIL, date="Mon, 09 Nov 2026 09:00:00 +0100")
    make_xlsx(root / "parc.xlsx", [["nom", "adresse"], ["pare-feu", "10.0.0.1"], ["commutateur", "10.0.0.2"]])
    (root / "notes.rtf").write_text(r"{\rtf1\ansi{\fonttbl{\f0 Arial;}}\f0 Compte rendu de r\'e9union : le serveur "
                                    r"a \'e9t\'e9 migr\'e9.\par Prochaine \'e9tape : sauvegardes.\par}")
    (root / "a.bin").write_bytes(os.urandom(4000))
    shutil.copy(root / "a.bin", root / "b.dat")
    return root


def names(state, group):
    ids = [m["id"] for m in group["members"]]
    return {r["name"] for r in state.db.q("SELECT name FROM files WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)}


def test_extraction_formats(dossier):
    assert "tribunal de commerce de Poitiers" in docs.extract(str(dossier / "contrat.docx"))["text"]
    assert "tribunal de commerce de Poitiers" in docs.extract(str(dossier / "contrat.odt"))["text"]
    assert "réunion : le serveur a été migré" in docs.extract(str(dossier / "notes.rtf"))["text"]
    assert "commutateur 10.0.0.2" in docs.extract(str(dossier / "parc.xlsx"))["text"]
    m = docs.extract(str(dossier / "devis.eml"))
    assert m["mid"] == "abc123@example.fr" and m["sender"] == "henri@example.fr" and m["mdate"]
    assert "demi-journée de formation" in m["text"]


@pytest.mark.skipif(not docs.OLE, reason="olefile absent")
def test_anciens_formats_office():
    t = docs.extract(os.path.join(FIX, "ancien_word.doc"))
    assert t["text"].startswith("Le contrat de maintenance du pare-feu") and "quarante-cinq jours" in t["text"]
    assert "fw1;10.0.0.1" in docs.extract(os.path.join(FIX, "ancien_excel.xls"))["text"]


@pytest.mark.skipif(not docs.PDF, reason="pypdfium2 absent")
def test_pdf_meme_texte_que_word(dossier, state):
    make_pdf(dossier / "contrat.pdf", PHRASES)
    assert "tribunal de commerce de Poitiers" in docs.extract(str(dossier / "contrat.pdf"))["text"]
    state.scanner.scan_sync(str(dossier))
    gs = dupes.compute(state.db, [str(dossier)], {})
    g = next(g for g in gs if "contrat.pdf" in names(state, g))
    assert {"contrat.docx", "contrat.odt"} <= names(state, g)


def test_fichier_abime_ne_bloque_pas(dossier, state):
    (dossier / "casse.docx").write_bytes(b"PK\x03\x04 ceci n'est pas un zip")
    (dossier / "casse.pdf").write_bytes(b"%PDF-1.4 tronque")
    st = state.scanner.scan_sync(str(dossier))
    assert any("casse.docx" in s["path"] for s in st["skipped"])
    assert state.db.one("SELECT COUNT(*) n FROM files WHERE kind='doc' AND done_doc=0")["n"] == 0


def test_groupes_documents(dossier, state):
    st = state.scanner.scan_sync(str(dossier), {"others": True})
    assert st["docs_new"] == 11
    gs = dupes.compute(state.db, [str(dossier)], {"others": True})
    by = {frozenset(names(state, g)): g for g in gs}
    # même texte : UN groupe, quel que soit le nombre de copies
    meme = by[frozenset({"contrat.docx", "contrat - Copie.docx", "contrat_reenregistre.docx", "contrat.odt"})]
    assert meme["kind"] == "contenu" and all(m["score"] == 100 for m in meme["members"])
    # version proche : une PAIRE (la v2 et un seul exemplaire du contrat), jamais un bloc
    paires = [g for g in gs if g["kind"] == "proche"]
    assert len(paires) == 1 and len(paires[0]["members"]) == 2
    assert "contrat_v2.docx" in names(state, paires[0])
    assert sorted(m["score"] for m in paires[0]["members"])[0] in range(80, 100)
    mail = next(g for n, g in by.items() if "devis.eml" in n)
    assert mail["kind"] == "mail" and names(state, mail) == {"devis.eml", "devis (1).eml"}   # la relance est à part
    assert by[frozenset({"a.bin", "b.dat"})]["kind"] == "exact"
    assert not any("recette.docx" in n for n in by)
    # seuil plus strict : plus de paire, le groupe « même contenu » reste
    gs = dupes.compute(state.db, [str(dossier)], {"doc_thr": 0.97})
    assert [g["kind"] for g in gs if "contrat.docx" in names(state, g)] == ["contenu"]
    assert not any("contrat_v2.docx" in names(state, g) for g in gs)
    # module désactivé : il ne reste que les copies bit à bit
    gs = dupes.compute(state.db, [str(dossier)], {"doc_content": False})
    assert [g["kind"] for g in gs] == ["exact"]


def test_paires_jamais_de_bloc(tmp_path, state):
    """v1 ~ v2 ~ v3 : trois comparaisons deux à deux, chacune avec sa note ; pas de groupe de trois."""
    root = tmp_path / "versions"
    root.mkdir()
    v = list(PHRASES)
    make_docx(root / "v1.docx", v)
    v2 = list(v)
    v2[2] = "Phrase entièrement réécrite pour la deuxième version du document de travail."
    make_docx(root / "v2.docx", v2)
    v3 = list(v2)
    v3[5] = "Encore une autre phrase modifiée, cette fois dans la troisième version."
    make_docx(root / "v3.docx", v3)
    state.scanner.scan_sync(str(root))
    gs = dupes.compute(state.db, [str(root)], {"doc_thr": 0.6})
    assert gs and all(g["kind"] == "proche" and len(g["members"]) == 2 for g in gs)
    assert {frozenset(names(state, g)) for g in gs} == {frozenset({"v1.docx", "v2.docx"}),
                                                         frozenset({"v2.docx", "v3.docx"}),
                                                         frozenset({"v1.docx", "v3.docx"})}
    a = state.db.one("SELECT id FROM files WHERE name='v1.docx'")["id"]
    b = state.db.one("SELECT id FROM files WHERE name='v2.docx'")["id"]
    c = docs.compare(state.db, a, b)
    modif = [r for r in c["rows"] if r[0] != "="]
    assert c["diff"] == len(modif) and 1 <= len(modif) <= 4 and any("deuxième version" in (r[2] or "") for r in modif)


def test_modele_rempli_par_donnees_liees(tmp_path, state):
    """Fiches issues d'un même modèle : document.xml identique, valeurs dans customXml ou dans l'en-tête."""
    root = tmp_path / "sav"
    root.mkdir()
    make_docx(root / "SAV1 - DUPONT.docx", PHRASES, donnees={"client": "Dupont SARL", "numero": "SAV100001"})
    make_docx(root / "SAV2 - MARTIN.docx", PHRASES, donnees={"client": "Martin SAS", "numero": "SAV100002"})
    make_docx(root / "SAV1 - DUPONT (1).docx", PHRASES, comment="x",
              donnees={"client": "Dupont SARL", "numero": "SAV100001"})
    make_docx(root / "lettre A.docx", RECETTE, entete="Dossier 2026-001")
    make_docx(root / "lettre B.docx", RECETTE, entete="Dossier 2026-002")
    assert "Martin SAS" in docs.extract(str(root / "SAV2 - MARTIN.docx"))["text"]
    state.scanner.scan_sync(str(root))
    gs = dupes.compute(state.db, [str(root)], {})
    # seules les deux fiches Dupont ont le même contenu
    assert [names(state, g) for g in gs if g["kind"] in ("contenu", "exact")] == \
        [{"SAV1 - DUPONT.docx", "SAV1 - DUPONT (1).docx"}]
    # les autres fiches du modèle ne sont que des paires « versions proches », jamais à 100 %
    paires = [g for g in gs if g["kind"] == "proche"]
    assert paires and all(len(g["members"]) == 2 for g in paires)
    assert any("SAV2 - MARTIN.docx" in names(state, g) for g in paires)
    assert any(names(state, g) == {"lettre A.docx", "lettre B.docx"} for g in paires)
    assert all(m["score"] < 100 for g in paires for m in g["members"] if not m["ref"])


def test_relecture_quand_la_methode_change(dossier, state):
    state.scanner.scan_sync(str(dossier))
    state.db.set_setting("doc_extract_version", 1)
    assert state.scanner.scan_sync(str(dossier))["docs_new"] == 11


def test_incremental_et_modification(dossier, state):
    state.scanner.scan_sync(str(dossier))
    assert state.scanner.scan_sync(str(dossier))["docs_new"] == 0
    time.sleep(0.02)
    make_docx(dossier / "recette.docx", PHRASES)
    os.utime(dossier / "recette.docx", (time.time() + 5, time.time() + 5))
    assert state.scanner.scan_sync(str(dossier))["docs_new"] == 1
    g = next(g for g in dupes.compute(state.db, [str(dossier)], {}) if "contrat.docx" in names(state, g))
    assert "recette.docx" in names(state, g)


def test_migration_ancien_index(tmp_path):
    from core.db import Database
    db = Database(tmp_path / "i.sqlite3")
    db.x("INSERT INTO files(path, folder, name, ext, kind, size, mtime) VALUES ('/x/a.pdf','/x','a.pdf','.pdf','other',1,1)")
    db.x("INSERT INTO files(path, folder, name, ext, kind, size, mtime) VALUES ('/x/a.zip','/x','a.zip','.zip','other',1,1)")
    db2 = Database(tmp_path / "i.sqlite3")
    assert {r["name"]: r["kind"] for r in db2.q("SELECT name, kind FROM files")} == {"a.pdf": "doc", "a.zip": "other"}


def test_recherche_dans_le_contenu(dossier, state):
    state.scanner.scan_sync(str(dossier))
    r = search.search(state.db, "tribunal poitiers")
    assert "contenu des documents" in r["sources"] and len(r["files"]) == 5
    assert all("Poitiers" in f["snippet"] for f in r["files"])
    assert {f["name"] for f in search.search(state.db, "cannelle")["files"]} == {"recette.docx"}
    assert {f["name"] for f in search.search(state.db, "", {"kind": "mail"})["files"]} == \
        {"devis.eml", "devis (1).eml", "relance.eml"}
    fid = state.db.one("SELECT id FROM files WHERE name='contrat_v2.docx'")["id"]
    res = similar.similar(state.db, fid)
    assert len(res) == 4 and all(src == ["contenu"] for _, _, src in res)


def test_api_documents_et_quitter(dossier, state):
    app = create_app(state)
    app.testing = True
    sorties = []
    app.config["EXIT"] = sorties.append
    c = app.test_client()
    c.environ_base["HTTP_HOST"] = "127.0.0.1:8765"
    h = {"X-Token": state.token}
    assert c.post("/api/scan", json={"path": str(dossier), "options": {"others": True}}, headers=h).status_code == 200
    for _ in range(600):
        if c.get("/api/status").get_json()["status"] != "running":
            break
        time.sleep(0.05)
    assert c.post("/api/dupes", json={"options": {"others": True}}, headers=h).get_json()["groups"] == 4
    g = c.get("/api/groups?kind=proche").get_json()
    assert g["total"] == 1 and len(g["groups"][0]["files"]) == 2 and all(f["words"] > 100 for f in g["groups"][0]["files"])
    ida, idb = [f["id"] for f in g["groups"][0]["files"]]
    cmp_ = c.get("/api/compare?a=%d&b=%d" % (ida, idb)).get_json()
    assert cmp_["diff"] >= 1 and cmp_["same"] > 20
    fid = state.db.one("SELECT id FROM files WHERE name='devis.eml'")["id"]
    info = c.get("/api/info?id=%d" % fid).get_json()["doc"]
    assert info["family"] == "Courriel" and "pare-feu" in info["text"] and info["extra"]["a"] == "client@example.com"
    r = c.get("/api/file?id=%d" % fid)            # jamais affiché dans la page : téléchargement
    assert "attachment" in r.headers["Content-Disposition"]
    t = c.get("/api/thumb?id=%d&s=400" % fid)          # miniature : aperçu du début du texte
    assert t.mimetype == "image/jpeg" and len(t.data) > 3000
    autre = state.db.one("SELECT id FROM files WHERE name='a.bin'")["id"]
    assert c.get("/api/thumb?id=%d" % autre).mimetype == "image/svg+xml"   # fichier illisible : simple étiquette
    # supprimer tout un groupe demande confirmation (dernier exemplaire)
    ids = [f["id"] for f in g["groups"][0]["files"]]
    assert c.post("/api/delete", json={"ids": ids}, headers=h).get_json()["needs_confirm"]
    assert c.post("/api/delete", json={"ids": ids[1:]}, headers=h).get_json()["deleted"] == len(ids) - 1
    # quitter : jeton obligatoire, puis arrêt du processus
    assert c.post("/api/quit", json={}).status_code == 403
    assert c.post("/api/quit", json={}, headers=h).get_json()["ok"]
    for _ in range(100):
        if sorties:
            break
        time.sleep(0.05)
    assert sorties == [0]


def test_explorer_tri_facettes_et_autres_fichiers(dossier, state):
    state.scanner.scan_sync(str(dossier), {"others": True})
    tout = search.search(state.db, "", {"sort": "gros"}, per=100)["files"]
    assert {"a.bin", "b.dat"} <= {f["name"] for f in tout}                 # les « autres » fichiers sont listés
    assert [f["size"] for f in tout] == sorted((f["size"] for f in tout), reverse=True)
    noms = [f["name"] for f in search.search(state.db, "", {"sort": "nom"}, per=100)["files"]]
    assert noms == sorted(noms, key=str.lower)
    r = search.search(state.db, "contrat", {"sort": "petit"})["files"]     # recherche + tri demandé
    assert len(r) >= 4 and [f["size"] for f in r] == sorted(f["size"] for f in r)
    fa = search.facets(state.db)
    assert fa["kinds"] == {"doc": 11, "other": 2} and fa["families"]["mail"] == 3 and fa["families"]["word"] == 7


def test_dossiers_deja_analyses_restent_consultables(dossier, tmp_path):
    """Après un redémarrage (ou l'analyse d'un autre dossier), les fichiers déjà indexés restent affichables."""
    from core.web import State
    data = str(tmp_path / "data")
    s1 = State(data_dir=data, models_dir=str(tmp_path / "m"))
    s1.scanner.scan_sync(str(dossier))
    s2 = State(data_dir=data, models_dir=str(tmp_path / "m"))               # nouveau lancement
    assert s2.scope.contains(str(dossier / "contrat.docx"))
    assert not s2.scope.contains(str(tmp_path / "ailleurs" / "x.docx"))
    app = create_app(s2)
    app.testing = True
    c = app.test_client()
    c.environ_base["HTTP_HOST"] = "127.0.0.1:8765"
    assert len(c.get("/api/search?kind=mail").get_json()["files"]) == 3
    assert c.get("/api/facethumb?id=999").mimetype == "image/svg+xml"       # jamais d'image cassée
    autre = tmp_path / "autre"
    autre.mkdir()
    (autre / "note.txt").write_text("bonjour tout le monde")
    assert c.post("/api/scan", json={"path": str(autre)}, headers={"X-Token": s2.token}).status_code == 200
    assert s2.scope.contains(str(dossier / "contrat.docx")) and s2.scope.contains(str(autre / "note.txt"))


def test_miniature_texte():
    im = docs.text_thumbnail("Bonjour,\n\n" + "Un paragraphe assez long pour passer à la ligne. " * 20
                             + "\n" + "x" * 500, ".docx", 400, "Titre du document")
    assert im.size == (304, 400)
    px = im.convert("RGB")
    assert px.getpixel((150, 10)) == (43, 87, 154)             # bandeau bleu « Word »
    assert sum(1 for x in range(20, 280) for y in range(60, 380, 3) if px.getpixel((x, y)) != (255, 255, 255)) > 300
    assert docs.text_thumbnail("", ".xlsx", 64).size == (48, 64)   # jamais d'erreur, même vide et minuscule


def test_fermeture_du_navigateur_arrete_l_application(state):
    app = create_app(state)
    app.testing = True
    sorties = []
    app.config.update(EXIT=sorties.append, DELAI_FERMETURE=0.3)
    c = app.test_client()
    c.environ_base["HTTP_HOST"] = "127.0.0.1:8765"
    assert c.post("/api/bye?p=a").status_code == 403                       # jeton obligatoire
    # deux pages ouvertes : en fermer une ne coupe rien
    c.get("/api/ping?p=a")
    c.get("/api/ping?p=b")
    assert c.post("/api/bye?p=a&t=" + state.token).get_json()["arret"]
    time.sleep(0.5)
    c.get("/api/ping?p=b")                 # l'autre page continue de donner signe de vie
    time.sleep(0.7)
    assert sorties == []
    # rechargement : la page se ferme puis redonne signe de vie aussitôt
    c.post("/api/bye?p=b&t=" + state.token)
    time.sleep(0.1)
    c.get("/api/ping?p=c")
    time.sleep(1.2)
    assert sorties == []
    # dernière page fermée : arrêt
    c.post("/api/bye?p=c&t=" + state.token)
    for _ in range(60):
        if sorties:
            break
        time.sleep(0.1)
    assert sorties == [0]
    # mode sans page (--no-browser) : jamais d'arrêt sur fermeture
    app.config["ARRET_FERMETURE"] = False
    assert c.post("/api/bye?p=z&t=" + state.token).get_json()["arret"] is False


def _client(state):
    app = create_app(state)
    app.testing = True
    c = app.test_client()
    c.environ_base["HTTP_HOST"] = "127.0.0.1:8765"
    return c


def test_sans_moteur_la_miniature_dit_texte_seul(dossier, state):
    """Aucun logiciel de rendu : on montre le texte, sans jamais le faire passer pour le document."""
    state.db.set_setting("doc_preview_engine", "aucun")
    state.scanner.scan_sync(str(dossier), {"doc_preview": True})
    state.scope.reset([str(dossier)])
    c = _client(state)
    fid = state.db.one("SELECT id FROM files WHERE name='contrat.docx'")["id"]
    assert c.get("/api/caps").get_json()["caps"]["doc_engine"] == ""
    t = c.get("/api/thumb?id=%d&s=400" % fid)
    assert t.mimetype == "image/jpeg" and "no-store" not in (t.headers.get("Cache-Control") or "")
    d = c.get("/api/info?id=%d" % fid).get_json()["doc"]
    assert d["convertible"] and d["apercu"] == "" and d["moteur"] == ""
    assert c.get("/api/apercu?id=%d" % fid).status_code == 404
    from PIL import Image
    import io
    px = Image.open(io.BytesIO(t.data)).convert("RGB")
    assert px.getpixel((150, 392))[2] < 235             # bandeau jaune « TEXTE SEUL » en pied d'image


def test_groupes_page_fichier_dans_deux_groupes(dossier, state):
    """Un fichier présent dans un groupe « même contenu » ET dans une paire garde ses valeurs dans chacun."""
    state.scanner.scan_sync(str(dossier))
    state.scope.reset([str(dossier)])
    state.result.set(dupes.compute(state.db, [str(dossier)], {}), {})
    gs = dupes.page(state.db, state.result, per=50)["groups"]
    meme = next(g for g in gs if g["kind"] == "contenu")
    assert sum(1 for f in meme["files"] if f["ref"]) == 1 and all(f["score"] == 100 for f in meme["files"])
    paire = next(g for g in gs if g["kind"] == "proche")
    assert sorted(f["ref"] for f in paire["files"]) == [False, True]


@pytest.mark.skipif(not render.engines(), reason="ni Microsoft Office ni LibreOffice sur cette machine")
def test_rendu_exact_des_documents(dossier, state):
    st = state.scanner.scan_sync(str(dossier), {"doc_preview": True})
    assert st["previews"] >= 5                          # les .docx et le .rtf (les faux .odt / .xlsx du jeu d'essai sont refusés)
    state.scope.reset([str(dossier)])
    c = _client(state)
    f = state.db.one("SELECT id, path, mtime, size FROM files WHERE name='contrat.docx'")
    assert state.previews.state(f["path"], f["mtime"], f["size"]) == "exact"
    pdf = c.get("/api/apercu?id=%d" % f["id"])
    assert pdf.mimetype == "application/pdf" and pdf.data[:5] == b"%PDF-"
    if docs.PDF:                                        # le PDF produit contient bien le texte du document
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
            fh.write(pdf.data)
        assert "tribunal de commerce de Poitiers" in docs.extract(fh.name)["text"]
        os.unlink(fh.name)
    t = c.get("/api/thumb?id=%d&s=400" % f["id"])
    assert t.mimetype == "image/jpeg" and "no-store" not in (t.headers.get("Cache-Control") or "")
    assert c.get("/api/info?id=%d" % f["id"]).get_json()["doc"]["apercu"] == "exact"
    ids = ",".join(str(r["id"]) for r in state.db.q("SELECT id FROM files WHERE kind='doc'"))
    assert f["id"] in c.get("/api/apercus?ids=" + ids).get_json()["ready"]
    assert state.scanner.scan_sync(str(dossier), {"doc_preview": True}).get("previews") is None   # rien à refaire
    ok, msg, _ = state.previews.self_test()
    assert ok, msg


def test_document_illisible_par_le_moteur(tmp_path, state):
    if not render.engines():
        pytest.skip("aucun moteur")
    root = tmp_path / "casse"
    root.mkdir()
    (root / "faux.docx").write_bytes(b"PK\x03\x04 pas un vrai document" * 10)
    state.scanner.scan_sync(str(root), {"doc_preview": True})
    f = state.db.one("SELECT path, mtime, size FROM files")
    assert state.previews.state(f["path"], f["mtime"], f["size"]) in ("echec", "exact")
    assert not state.previews.request(dict(f, id=1))    # pas de nouvelle tentative en boucle
