#!/usr/bin/env python3
"""Chasseur de doublons v2 - page web locale.

Doublons exacts et visuels, visages et personnes, objets, texte (OCR), recherche en langage naturel (CLIP),
qualité des photos. Index persistant SQLite : une seconde analyse ne traite que les fichiers nouveaux ou
modifiés.
"""
import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import __version__  # noqa: E402
from core.web import State, create_app  # noqa: E402


def sans_console():
    """Vrai quand l'application tourne sans fenêtre (exécutable fenêtré ou pythonw)."""
    return sys.stdout is None or sys.stderr is None or getattr(sys, "_cd_silencieux", False)


def journaliser(data_dir):
    """Sans console, tout ce qui aurait été affiché va dans data/journal.log."""
    if not (sys.stdout is None or sys.stderr is None):
        return
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "journal.log")
    try:
        if os.path.getsize(path) > 2 * 1024 * 1024:
            os.replace(path, path + ".1")
    except OSError:
        pass
    fh = open(path, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = fh
    sys._cd_silencieux = True


def alerte(message):
    """Message d'erreur visible même sans console."""
    print(message)
    if os.name == "nt" and sans_console():
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, "Chasseur de doublons", 0x10)
        except Exception:
            pass


def ouvrir_page(url):
    """Ouvre la page dans le navigateur par défaut (deux moyens : certains postes n'en acceptent qu'un)."""
    try:
        if webbrowser.open(url):
            return
    except Exception:
        pass
    try:
        os.startfile(url)                  # Windows uniquement
    except Exception:
        print("Impossible d'ouvrir le navigateur : ouvrez %s à la main." % url)


def deja_lance(port):
    """L'application répond-elle déjà sur ce port ?"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port, timeout=1.5) as r:
            return json.loads(r.read().decode()).get("app") == "chasseur-doublons"
    except Exception:
        return False


def port_libre(port):
    for p in range(port, port + 30):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return port


def surveiller(state, minutes):
    """Arrête l'application quand plus aucune page n'est ouverte depuis `minutes` (hors analyse en cours)."""
    while True:
        time.sleep(20)
        if state.scanner.state["status"] == "running":
            state.last_seen = time.time()
        elif time.time() - state.last_seen > minutes * 60:
            print("Aucune page ouverte depuis %d min : arrêt automatique." % minutes)
            try:
                state.db.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            os._exit(0)


def main(argv=None):
    try:
        return _main(argv)
    except SystemExit:
        raise
    except Exception as e:  # noqa
        import traceback
        traceback.print_exc()
        alerte("Chasseur de doublons n'a pas pu démarrer :\n\n%s: %s" % (type(e).__name__, e))
        return 1


def _main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) >= 2 and args[0] == "--interne":      # tâche d'essai GPU relancée par l'exécutable lui-même
        from core.models import run_internal
        return run_internal(args[1], args[2:])
    ap = argparse.ArgumentParser(description="Chasseur de doublons %s (page web locale)" % __version__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--data", help="dossier des données (base SQLite, miniatures)")
    ap.add_argument("--models", help="dossier des modèles ONNX")
    ap.add_argument("--cpu", action="store_true", help="géré par lancer.bat / lancer.sh : n'installe pas le GPU")
    ap.add_argument("--sans-modeles", action="store_true", help="ne pas télécharger automatiquement les modèles")
    ap.add_argument("--console", action="store_true", help="géré par lancer.bat : garde la fenêtre de commandes")
    ap.add_argument("--arret-auto", type=int, default=10, metavar="MIN",
                    help="filet de sécurité : arrêt après MIN minutes sans page ouverte (défaut 10). "
                         "0 = ne jamais s'arrêter seul, même quand le navigateur est fermé")
    a = ap.parse_args(argv)
    from core.config import DATA_DIR
    journaliser(a.data or str(DATA_DIR))
    if a.host == "127.0.0.1":
        if deja_lance(a.port):          # déjà en route : on rouvre simplement la page
            print("Déjà lancé sur le port %d : ouverture de la page." % a.port)
            try:
                if os.environ.get("_PYI_SPLASH_IPC"):
                    import pyi_splash
                    pyi_splash.close()
            except Exception:
                pass
            if not a.no_browser:
                ouvrir_page("http://127.0.0.1:%d" % a.port)
            return 0
        libre = port_libre(a.port)
        if libre != a.port:
            print("Port %d occupé par un autre programme : utilisation du port %d." % (a.port, libre))
            a.port = libre
    kw = {}
    if a.data:
        kw["data_dir"] = a.data
    if a.models:
        kw["models_dir"] = a.models
    state = State(**kw)
    try:    # en cas de plantage brutal (pilote graphique, bibliothèque native), la trace est gardée ici
        import faulthandler
        _crash = open(os.path.join(state.data_dir, "plantage.log"), "a", encoding="utf-8")
        _crash.write("\n=== démarrage %s ===\n" % __version__)
        _crash.flush()
        faulthandler.enable(_crash)
    except Exception:
        pass
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
    print("Données : %s | Modèles : %s" % (state.data_dir, state.mm.root))
    try:                                   # écran de démarrage de l'exécutable en un seul fichier
        if os.environ.get("_PYI_SPLASH_IPC"):
            import pyi_splash
            pyi_splash.close()
    except Exception:
        pass
    if not a.no_browser:
        threading.Timer(1.0, lambda: ouvrir_page(url)).start()
    # fermer le navigateur arrête l'application (sauf usage sans page : --no-browser, ou --arret-auto 0)
    app.config["ARRET_FERMETURE"] = a.arret_auto > 0 and not a.no_browser
    if a.arret_auto > 0 and not a.no_browser:
        threading.Thread(target=surveiller, args=(state, a.arret_auto), daemon=True).start()
    if sans_console():
        import logging
        logging.getLogger("werkzeug").setLevel(logging.WARNING)      # pas une ligne par miniature dans le journal
    app.run(host=a.host, port=a.port, threaded=True)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
