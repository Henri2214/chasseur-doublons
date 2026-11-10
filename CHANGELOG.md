# Changelog

## 2.6.0 - 2026-11-10
### Ajouté
- **Application autonome Windows** : `tools/construire.ps1` produit `dist\ChasseurDoublons\ChasseurDoublons.exe`
  (PyInstaller, dossier autonome) et l'archive `ChasseurDoublons-v<version>-win64.zip`. Aucun Python requis
  sur le PC de destination ; OCR, HEIC, RAW, GPU DirectML et modèles à licence libre inclus (InsightFace
  jamais inclus : licence non commerciale, téléchargement par l'utilisateur).
- Recette `packaging/chasseur.spec` ; construction automatique sur GitHub (`.github/workflows/construire.yml`)
  à chaque étiquette `v*`, avec publication de l'archive dans les Releases.
- En mode exécutable : données et modèles à côté de l'exe (ou dans `%LOCALAPPDATA%\ChasseurDoublons` si le
  dossier est protégé) ; les essais GPU relancent l'exécutable lui-même (`--interne`).

## 2.5.0 - 2026-11-10
### Ajouté
- **Recherche à partir d'une zone d'une photo** : dans l'aperçu plein écran, bouton « Chercher une zone » ;
  on trace un rectangle autour d'un objet, d'un visage ou d'un détail, et toutes les photos qui contiennent
  la même chose ressortent dans l'onglet Similaires. La zone est analysée par trois moyens combinés :
  objets YOLOX reconnus dans la zone, visage éventuel (identification de la personne), empreinte de contenu
  CLIP. L'onglet indique ce qui a été reconnu dans la zone ; seuil, sources, « Tout sélectionner »,
  déplacement et suppression comme pour les photos similaires. Route `/api/zone_search`.

## 2.4.1 - 2026-11-10
### Ajouté
- **Sélection par plage avec Maj** : cochez une photo, puis Maj + clic sur une autre (case ou image) ; toutes
  les photos entre les deux prennent le même état (cochées ou décochées). Disponible dans Doublons,
  Explorer, Similaires et Recherche de personne.

## 2.4.0 - 2026-11-10
### Ajouté
- **Choix de la carte graphique** : sur un PC à plusieurs GPU, la carte la plus puissante (la plus grande
  mémoire vidéo dédiée) est utilisée par défaut, et non plus la carte n° 0 de Windows, souvent la puce
  intégrée au processeur. Réglages liste les cartes avec leur mémoire, indique celle qui est utilisée et
  permet d'en imposer une. La liste est lue via DXGI dans un processus séparé.

## 2.3.2 - 2026-11-10
### Corrigé
- **Plantage pendant l'analyse avec IA CLIP sur GPU** (« Serveur injoignable ») : `data/plantage.log`
  montrait une violation d'accès dans le modèle de texte CLIP exécuté par DirectML. Les modèles CLIP
  « quantized » restent désormais toujours sur le processeur, pour lequel ils sont conçus.
- Le GPU n'est plus utilisé que pour une liste fermée de modèles à entrée fixe (YOLOX, détection et
  reconnaissance de visages InsightFace), une seule exécution GPU à la fois toutes sessions confondues,
  un visage à la fois pour la reconnaissance.
- **Coupe-circuit** : si une analyse fait tomber le programme alors que le GPU est utilisé, il est
  désactivé automatiquement au redémarrage suivant (message dans Réglages, bouton « Réessayer le GPU »).

## 2.3.1 - 2026-11-10
### Corrigé
- **Plantage (fermeture brutale) au lancement d'une analyse avec Objets / Visages / CLIP sur GPU.**
  DirectML n'accepte pas plusieurs exécutions simultanées sur une même session, or l'analyse en lançait une
  par thread. Les appels GPU sont maintenant mis en file (`LockedSession`), avec les options exigées par
  DirectML (pas de motif mémoire, exécution séquentielle).
- **Filet de sécurité GPU** : chaque modèle est d'abord essayé sur le GPU dans un processus séparé ; si le
  pilote plante, le résultat est mémorisé et l'application calcule sur le processeur au lieu de se fermer.
  Réglages : case « Utiliser le GPU », bouton « Réessayer le GPU », message expliquant l'état.
- Les plantages natifs laissent désormais une trace dans `data/plantage.log`.

### Ajouté
- **« Tout réanalyser »** : case qui force le recalcul complet du dossier (par défaut, seuls les fichiers
  nouveaux ou modifiés sont analysés, les autres sont repris de l'index).
- Fin d'analyse explicite : « N fichiers : x nouveaux, y modifiés, z disparus, w inchangés repris de
  l'index — k analysés cette fois ».

## 2.3.0 - 2026-11-10
### Ajouté
- **Téléchargement automatique des modèles** au lancement (visages OpenCV, objets YOLOX-S, contenu CLIP ;
  InsightFace reste sur accord de licence). Progression affichée dans le bandeau d'accueil, interrupteur
  dans Réglages, option `--sans-modeles` pour s'en passer.
- **GPU** : `lancer.bat` installe `onnxruntime-directml` au premier lancement si aucune accélération n'est
  disponible (toute carte DirectX 12 : Intel, AMD, NVIDIA) ; `lancer.sh` installe `onnxruntime-gpu` si une
  carte NVIDIA est détectée. `lancer.bat --cpu` pour rester sur le processeur, suppression de
  `.venv\gpu.txt` pour refaire la détection. Réglages indique si le GPU est actif et, sinon, quoi faire.
- **Nombre de miniatures par page** réglable : 50, 100, 200, 500 ou 1000 photos (Explorer, Similaires,
  Recherche de personne) et 10 à 200 groupes (Doublons). Le choix est partagé par toutes les vues, ce qui
  permet de sélectionner d'un coup une page entière.
- **OCR livré avec l'application** : `rapidocr-onnxruntime` passe dans `requirements.txt` (avec le marqueur
  `python_version < "3.13"`, lu correctement depuis un fichier), donc installé par `lancer.bat`.
- **Puces cliquables dans l'aperçu plein écran** : personnes, objets et scènes reconnus deviennent des
  boutons ; un clic ouvre l'Explorer filtré sur cet objet, cette personne ou cette scène.

### Corrigé
- **Miniatures instables** : une photo supprimée, déplacée ou hors du dossier analysé renvoyait une erreur 404
  et laissait une case vide ; une image de remplacement est renvoyée à la place.
- **Étalonnage** : il proposait des photos d'analyses précédentes, hors du dossier en cours (d'où les
  vignettes manquantes). Il ne propose plus que des fichiers du dossier analysé et encore présents sur le
  disque.
- **Recherche de personne** : après une suppression, le rafraîchissement relançait la recherche avec une
  référence périmée et affichait une erreur en boucle (`POST /api/psearch/run 400`).

## 2.2.0 - 2026-11-10
### Ajouté
- **Modules grisés : on sait enfin pourquoi et quoi faire.** Un bandeau d'avertissement liste les modules
  désactivés faute de modèles, avec un bouton « Télécharger les modèles conseillés » (visages OpenCV à
  licence libre + YOLOX-S + CLIP, en une fois) ; chaque option grisée porte un bouton « ⤓ modèle » qui ouvre
  Réglages sur la ligne du modèle concerné, mise en évidence ; l'option « Texte (OCR) » indique la commande
  pip à lancer. Bouton « Télécharger les modèles conseillés » également dans Réglages.
- **Photos similaires** : bouton « Similaires » sous chaque photo (Doublons, Explorer, Recherche de personne)
  et dans l'aperçu plein écran. Nouvel onglet qui sort toutes les photos ressemblant à celle choisie, même si
  ce ne sont pas des doublons, en combinant quatre sources : empreinte visuelle (pHash + comparaison fine,
  tolérante aux recadrages et retouches, sans aucun modèle d'IA), mêmes personnes (visages regroupés),
  mêmes objets (YOLOX) et même contenu (CLIP). Curseur de ressemblance minimale, cases pour activer ou
  couper chaque source, pourcentage et source affichés sur chaque résultat, « Tout sélectionner » sur toutes
  les pages, déplacement et suppression groupés. Nouveau module `core/similar.py`, routes `/api/similar`
  et `/api/similar_ids`, tests `tests/test_similar.py`.

### Corrigé
- `requirements-optional.txt` : le marqueur `; python_version < "3.13"` de `rapidocr-onnxruntime` faisait
  échouer l'installation sous Windows (cmd.exe perd les guillemets, pip refusait la ligne). Marqueur retiré :
  la ligne est simplement ignorée quand le paquet n'existe pas pour la version de Python utilisée.

## 2.1.1 - 2026-11-10
### Modifié
- **Un seul dossier de travail** : la v2 remonte à la racine du dépôt, le sous-dossier
  `chasseur-doublons-v2/` disparaît et les fichiers de la v1 (un seul `app.py` + sa page web) sont supprimés.
  La v1 reste disponible dans l'historique Git (étiquette `v1.1-finale`) et dans une archive `.bak`.
- Nouvelle procédure de sauvegarde avant chaque grosse modification : `tools/sauvegarde.ps1` et
  `MAINTENANCE.md` (archive `.bak.zip` hors dépôt, 10 dernières conservées, caches et données exclus).

### Corrigé
- `lancer.bat` / `lancer.sh` n'installaient les dépendances qu'à la création de `.venv` : un `.venv` créé à la
  main (par exemple `py -3.12 -m venv .venv`) démarrait sans Flask. Les dépendances de base sont maintenant
  vérifiées à chaque lancement et installées si besoin.
- Les modules optionnels sont installés un par un : `rapidocr-onnxruntime` indisponible pour Python 3.13/3.14
  empêchait aussi l'installation de `tokenizers`, `rawpy`, `psutil` et `pytest`.
- `requirements-optional.txt` : `rapidocr-onnxruntime>=1.2.3` (la 1.3 n'existe pas) et limité à Python <= 3.12.
- README : Python 3.12 recommandé, et utiliser le Python de `.venv` pour `tools/selftest.py` et pytest.

## 2.1.0 - 2026-11-09
### Ajouté
- **Tout sélectionner** : bouton dans Doublons (toutes les photos de tous les groupes, toutes pages, référence
  incluse), bouton « Tout cocher » par groupe, et bouton dans Explorer (tous les résultats, toutes pages) via
  les nouvelles routes `/api/search_ids` et `/api/group_ids?kind=`. « Tout désélectionner » associé.
- **Formats** : HEIC/HEIF/HIF/AVIF (pillow-heif), RAW (rawpy : CR2, CR3, NEF, ARW, DNG, ORF, RW2, RAF… aperçu
  intégré ou demi-résolution), JPEG 2000, TGA, etc. Mêmes empreintes visuelles pour tous les formats.
  Aperçu plein écran des formats non affichables par le navigateur (conversion JPEG mise en cache).
  Message d'erreur explicite quand pillow-heif ou rawpy manque.
- **Recherche de personne** (nouvel onglet) : par personne connue, par photo de référence envoyée (plusieurs
  visages : on choisit) ou par visage de l'onglet Personnes ; seuil de ressemblance réglable ; résultats avec
  pourcentage, sélection totale, déplacement, suppression et affectation des visages trouvés à une personne
  (existante ou nouvelle). Routes `/api/psearch/detect|run|assign`, `/api/persons/create`.
- Affectation de visages en lot (un seul recalcul des centroïdes).
- Tests : `tests/test_formats_and_psearch.py`.

## 2.0.0 - 2026-11-08
Refonte complète autour d'un index persistant et de modules d'IA locaux (ONNX, sans PyTorch).

### Ajouté
- Index SQLite (WAL) persistant : seuls les fichiers nouveaux ou modifiés sont analysés ; les disparus sont purgés.
- Décodage unique de chaque image, qui alimente toutes les étapes ; pool de threads ; mémoire bornée.
- Modules activables séparément : exacts, visuel, visages, objets, texte (OCR), IA CLIP, qualité.
- Visages : InsightFace (SCRFD + ArcFace + 2d106det, recherche non commerciale, confirmation de licence) ou
  OpenCV YuNet + SFace (licence libre) ; regroupement en personnes ; renommer, fusionner, retirer, confirmer ;
  bouton d'effacement des données biométriques.
- Aide aux doublons par les visages (même nombre, même place, mêmes personnes).
- Qualité : netteté, yeux fermés, « garder la meilleure ».
- Objets YOLOX (noms en français), OCR rapidocr (modes aucun / captures / toutes, index FTS5), CLIP ONNX
  (recherche en langage naturel, scènes zéro-shot).
- Onglet Explorer : recherche combinée et filtres, résultats paginés, actions groupées.
- Outil d'étalonnage des seuils (doublons, même personne) et rigueur « étalonnée ».
- Gestionnaire de modèles : téléchargement à la demande avec progression, fichier .part, vérification de taille,
  fonctions grisées si un modèle manque ; fournisseurs CUDA > DirectML > CPU.
- Rapport des fichiers ignorés avec leur cause ; performances par étape.
- Journal de toutes les actions, avec annulation des déplacements et renommages.
- Tests pytest, `tools/selftest.py`, `tools/bench.py`.

### Corrigé (défauts de la v1.1)
- Plus de repli sur la date de modification quand la date EXIF manque : le contrôle est ignoré et signalé.
- Suppression revérifiée juste avant (taille et date inchangées) ; dernier exemplaire protégé.
- Les fichiers déplacés sont retirés de leur groupe (et y reviennent si l'on annule).
- Comparaison vectorisée (index multiple sur les octets du pHash) : 100 000 empreintes en quelques secondes.
- Pagination côté serveur au lieu de renvoyer tous les résultats après chaque action.
- Fichiers illisibles signalés au lieu d'être ignorés en silence.
- Chemins Windows longs, fichiers verrouillés, fichiers OneDrive non téléchargés.
- Périmètre des dossiers autorisés remis à zéro à chaque session.
- Contrôle de l'en-tête Host (DNS rebinding) ; fichiers servis par identifiant uniquement.

## 1.1.0 - 2026-11-08
- Analyse plus fiable : vérification de la date EXIF et de l'appareil, comparaison fine bloc par bloc, groupes
  autour d'une photo de référence avec score de similarité, menu de rigueur (très stricte / stricte / normale /
  large), vidéos à 6/12/24 images clés.

## 1.0.0 - 2026-11-03
- Doublons exacts (SHA-256), détection visuelle pHash et vidéos, IA CLIP optionnelle, page web avec miniatures,
  renommer / supprimer / déplacer, bandeau de dossiers avec glisser-déposer et annulation.
