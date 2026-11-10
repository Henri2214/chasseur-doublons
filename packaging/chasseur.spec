# -*- mode: python ; coding: utf-8 -*-
"""Recette PyInstaller : dossier autonome ChasseurDoublons/ (exécutable + _internal/).

    pyinstaller --noconfirm packaging/chasseur.spec        (lancé par tools/construire.ps1)

Aucun Python n'est nécessaire sur le PC de destination. Les modèles d'IA ne sont PAS dans l'exécutable :
ils sont copiés à côté (dossier models/) par le script de construction, ou téléchargés au premier lancement.
"""
import os
from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
datas = [(os.path.join(ROOT, "static"), "static")]
datas += [(os.path.join(ROOT, f), ".") for f in ("LICENSE", "README.md", "CHANGELOG.md")
          if os.path.exists(os.path.join(ROOT, f))]
binaries, hidden = [], ["core.clip", "core.ocr", "core.objects", "core.faces", "core.similar", "core.docs", "olefile"]
# bibliothèques natives ou chargées dynamiquement : on embarque tout (DLL, modèles OCR, fichiers de données)
for pkg in ("pypdfium2", "pypdfium2_raw", "onnxruntime", "rapidocr_onnxruntime", "pillow_heif", "rawpy", "tokenizers", "cv2"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hidden += h
    except Exception as e:      # module optionnel absent de l'environnement de construction
        print("  (paquet optionnel non embarqué : %s — %s)" % (pkg, e))

a = Analysis([os.path.join(ROOT, "app.py")], pathex=[ROOT], binaries=binaries, datas=datas,
             hiddenimports=hidden, excludes=["tkinter", "matplotlib", "pytest", "IPython", "torch", "tensorflow"],
             noarchive=False)
pyz = PYZ(a.pure)
icon = os.path.join(SPECPATH, "icone.ico")
# ChasseurDoublons.exe : sans fenêtre noire (les messages vont dans data\\journal.log).
# ChasseurDoublons-diagnostic.exe : le même programme avec sa fenêtre de commandes, pour chercher une panne.
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ChasseurDoublons", console=False,
          icon=icon if os.path.exists(icon) else None, upx=False)
exe_diag = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ChasseurDoublons-diagnostic", console=True,
               icon=icon if os.path.exists(icon) else None, upx=False)
coll = COLLECT(exe, exe_diag, a.binaries, a.datas, name="ChasseurDoublons", upx=False)
