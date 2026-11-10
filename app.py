#!/usr/bin/env python3
"""Chasseur de doublons v2 - page web locale.

Doublons exacts et visuels, visages et personnes, objets, texte (OCR), recherche en langage naturel (CLIP),
qualité des photos. Index persistant SQLite : une seconde analyse ne traite que les fichiers nouveaux ou
modifiés.
"""
import argparse
import os
import sys
import threading
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import __version__  # noqa: E402
from core.web import State, create_app  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chasseur de doublons %s (page web locale)" % __version__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--data", help="dossier des données (base SQLite, miniatures)")
    ap.add_argument("--models", help="dossier des modèles ONNX")
    ap.add_argument("--cpu", action="store_true", help="géré par lancer.bat / lancer.sh : n'installe pas le GPU")
    ap.add_argument("--sans-modeles", action="store_true", help="ne pas télécharger automatiquement les modèles")
    a = ap.parse_args(argv)
    kw = {}
    if a.data:
        kw["data_dir"] = a.data
    if a.models:
        kw["models_dir"] = a.models
    state = State(**kw)
    if not getattr(a, "sans_modeles", False) and state.auto_models():
        lances = state.mm.auto_download()
        if lances:
            print("Téléchargement automatique des modèles : %s (en arrière-plan, suivi dans Réglages)"
                  % ", ".join(lances))
    app = create_app(state)
    if a.host != "127.0.0.1":
        print("ATTENTION : l'interface permet de supprimer des fichiers. Exposez-la uniquement sur un réseau de confiance.")
        app.config["ALLOW_ANY_HOST"] = True
    url = "http://127.0.0.1:%d" % a.port
    print("Chasseur de doublons %s -> %s" % (__version__, url))
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=a.host, port=a.port, threaded=True)


if __name__ == "__main__":
    main()
