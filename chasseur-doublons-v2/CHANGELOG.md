# Changelog

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
