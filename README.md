# Chasseur de doublons

Page web locale (Flask) pour trouver, comparer et trier les doublons d'un dossier.

## Lancement
- Windows : double-clic sur `lancer.bat` (crée le venv et installe les dépendances au 1er lancement)
- Linux/macOS : `./lancer.sh`
- Options : `python app.py --port 8765 --no-browser --host 127.0.0.1`

Puis dans la page : saisir (ou « Parcourir ») le chemin, choisir les options, « Analyser ».

## Détection
1. **Identiques** : taille -> hash partiel -> SHA-256 complet (tous types de fichiers).
2. **Visuel (pHash)** : images (redimensionnées, recompressées, converties) et vidéos
   (5 images-clés + durée similaire). Curseur « tolérance » = distance de Hamming (0 = strict, 6 = défaut).
3. **IA (CLIP)** : optionnel, `pip install sentence-transformers` (télécharge ~350 Mo au 1er usage).
   Curseur « similarité » (0.92 par défaut).

## Actions en direct
Miniatures (clic = aperçu plein écran / lecture vidéo), renommer, supprimer **définitivement**,
déplacer (avec sélecteur de dossier), sélection auto (garder le plus ancien / lourd / meilleure résolution…).
Une alerte s'affiche si une sélection supprime tous les exemplaires d'un groupe.

## Sécurité
Écoute sur 127.0.0.1 uniquement par défaut. Les actions sont limitées aux dossiers analysés (et aux
destinations de déplacement), protégées par un jeton propre à chaque lancement.
Le clustering visuel est de type « chaînage » : vérifiez les groupes « SIMILAIRES » avant de supprimer.
