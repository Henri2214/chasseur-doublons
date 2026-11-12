# Chasseur de doublons v2

Page web locale (Flask) pour trouver, comparer et trier les doublons de photos, vidéos et fichiers,
reconnaître les personnes, retrouver une photo par son contenu, et garder la meilleure version d'une série.
**Tout reste sur votre PC** : aucune photo, aucun visage, aucun texte n'est envoyé sur le réseau
(seul le téléchargement des modèles, à votre demande, utilise Internet).

Dépôt : une seule version, à la racine. La procédure suivie pour chaque grosse modification (sauvegarde
`.bak`, branche, tests, purge) est décrite dans [MAINTENANCE.md](MAINTENANCE.md). La v1.1 est archivée
(étiquette Git `v1.1-finale`).

## Application autonome (sans installer Python)

Dans les **Releases** du dépôt GitHub, deux fichiers au choix :

| Fichier | Usage |
|---|---|
| `ChasseurDoublons-v…-Setup.exe` | **Installateur, à privilégier.** Un seul fichier qui contient tout (programme, bibliothèques, modèles d'IA libres). Installe sans droits d'administrateur, crée les raccourcis, démarre vite, se désinstalle proprement. |
| `ChasseurDoublons-v…-portable.exe` | Un seul fichier, sans installation, à poser n'importe où. Il se décompresse à chaque lancement (démarrage plus lent) et télécharge les modèles la première fois. |

Les données (index, miniatures, journaux, modèles téléchargés) sont dans `%LOCALAPPDATA%\ChasseurDoublons` :
déplacer ou mettre à jour l'exécutable ne fait rien perdre. Pour tout garder sur une clé USB, créez un dossier
`data` à côté de l'exécutable portable.

Pour construire vous-même (Inno Setup 6 requis pour l'installateur : `winget install JRSoftware.InnoSetup`) :

```powershell
.\tools\construire.ps1                      # utilise les modèles déjà présents dans models\
.\tools\construire.ps1 -TelechargerModeles  # les télécharge d'abord
```

Le résultat est dans `dist\`. Le dossier `dist\ChasseurDoublons\` sert à fabriquer l'installateur : son
`ChasseurDoublons.exe` **ne fonctionne pas sorti de ce dossier** (il a besoin de `_internal\`). Sur GitHub,
pousser une étiquette (`git tag v2.9.0 ; git push origin v2.9.0`) lance la construction, installe réellement
l'installateur, lance la version portable seule dans un dossier vide, puis publie les deux fichiers.
Les exécutables ne sont pas signés : Windows SmartScreen peut afficher « Windows a protégé votre ordinateur »
au premier lancement (Informations complémentaires → Exécuter quand même).

## Installation et lancement

Prérequis : Python 3.10 ou plus récent ([python.org](https://www.python.org/downloads/), cocher « Add to PATH »).
**Python 3.12 est recommandé** : l'OCR (`rapidocr-onnxruntime`) n'est pas encore publié pour 3.13 et 3.14. Tout
le reste fonctionne avec les versions plus récentes. Aucune dépendance à compiler, pas de PyTorch.

- **Windows** : double-clic sur `lancer.bat`. L'environnement `.venv` est créé s'il manque, les dépendances de
  base sont vérifiées **à chaque lancement** (et installées si besoin), puis les modules optionnels sont
  installés un par un : si l'un n'existe pas pour votre version de Python, les autres le sont quand même.
- **Linux / macOS** : `./lancer.sh`
- Options : `python app.py --port 8765 --no-browser --data D:\ChasseurData --models D:\ChasseurModeles`

Tout passe par `.venv` : pour les tests et l'autotest, utilisez le Python de cet environnement
(`.venv\Scripts\python.exe` sous Windows, `.venv/bin/python` ailleurs) et non le `python` du système, sinon les
modules installés par `lancer.bat` ne sont pas visibles. Pour forcer une autre version de Python, supprimez
`.venv` puis relancez `lancer.bat` (par exemple après `py -3.12 -m venv .venv`, qui est aussi accepté).

Puis, dans la page : choisir un dossier, cocher les modules, cliquer sur « Analyser ».
Au premier lancement, les modèles conseillés (visages OpenCV, objets YOLOX-S, contenu CLIP) se téléchargent
**automatiquement** en arrière-plan : la progression s'affiche dans le bandeau de la page d'accueil et les
options s'activent toutes seules à la fin. Pour désactiver : case dans Réglages, ou `python app.py
--sans-modeles`. InsightFace reste à part : sa licence (recherche non commerciale) demande votre accord.

Le GPU est activé automatiquement : `lancer.bat` installe `onnxruntime-directml` au premier lancement si
aucune accélération n'est disponible (Windows, toute carte DirectX 12 — Intel, AMD, NVIDIA), `lancer.sh`
installe `onnxruntime-gpu` si une carte NVIDIA est présente. `lancer.bat --cpu` reste sur le processeur ;
supprimez `.venv\gpu.txt` pour refaire la détection. Réglages indique le fournisseur réellement utilisé.
Le GPU accélère les visages, les objets et CLIP ; la lecture des images reste faite par le processeur.

Les modèles se gèrent aussi à la main dans l'onglet **Réglages**, avec une barre de progression — ou directement
depuis le bandeau jaune de la page d'accueil, bouton « Télécharger les modèles conseillés » (visages OpenCV
à licence libre, objets YOLOX-S, contenu CLIP ; InsightFace reste à part à cause de sa licence). Tant qu'un
modèle manque, l'option correspondante (Visages, Objets, IA CLIP) est grisée : cliquez dessus ou sur son
bouton « ⤓ modèle » pour arriver directement sur la ligne à télécharger. Sans aucun modèle, l'application
fonctionne quand même : doublons exacts, doublons visuels, qualité et photos similaires par l'empreinte
visuelle.

Vérification de l'installation sur votre PC (tous les modules, GPU compris) :

```
.venv\Scripts\python.exe tools/selftest.py --portrait une_photo_de_vous.jpg --autre une_photo_de_quelqu_un_d_autre.jpg
```

### Accélération GPU (optionnelle)
Remplacez `onnxruntime` par `onnxruntime-directml` (Windows, toute carte DirectX 12) ou `onnxruntime-gpu`
(NVIDIA avec CUDA 12 et cuDNN). Les fournisseurs sont choisis automatiquement dans l'ordre CUDA, DirectML, CPU.
Voir les commentaires de `requirements-optional.txt`.

## Modules (activables séparément)

| Module | Rôle | Modèle | Licence du modèle |
|---|---|---|---|
| Exacts | taille, puis hash partiel, puis SHA-256 (tous types de fichiers) | - | - |
| Visuel | photos redimensionnées, recompressées ou retouchées en luminosité ; vidéos (6/12/24 images clés) | - | - |
| Qualité | netteté (variance du Laplacien), « garder la meilleure » | - | - |
| Visages (précis) | détection SCRFD, reconnaissance ArcFace 512 dimensions, points de repère 2d106 (yeux fermés) | InsightFace buffalo_l | **recherche non commerciale** |
| Visages (licence libre) | détection YuNet, reconnaissance SFace 128 dimensions | OpenCV Zoo | MIT / Apache-2.0 |
| Objets | 80 classes COCO, noms traduits en français | YOLOX-S (ou Nano) | Apache-2.0 |
| Texte (OCR) | aucun / captures et documents / toutes les images ; texte indexé (FTS5) | rapidocr-onnxruntime | Apache-2.0 |
| IA CLIP | recherche en langage naturel, ~40 scènes, doublons « sémantiques » | CLIP ViT-B/32 quantifié (Xenova) | MIT |

### Doublons visuels : comment ça marche
1. Le pHash (64 bits) sert seulement à trouver des **candidats**. La recherche est vectorisée : index multiple
   sur les octets pour les rigueurs stricte et très stricte, force brute par blocs numpy sinon.
2. Chaque paire candidate est ensuite **vérifiée** :
   - date de prise de vue EXIF et appareil identiques ;
   - mêmes proportions (écart < 2 %) ;
   - corrélation à 48 px et couleurs ;
   - comparaison fine à 128 px bloc par bloc : une personne déplacée, une expression ou un objet différent
     suffit à rejeter la paire ;
   - si les deux photos ont des visages : même nombre de visages, boîtes au même endroit (IoU ≥ 0,6 en
     rigueur stricte, 0,4 en large) et mêmes personnes (cosinus ≥ 0,5).
3. **Pas de date EXIF** : le contrôle de date est ignoré (aucun repli sur la date de modification du
   fichier) et le groupe porte le badge « date non vérifiée ».
4. Les groupes se forment autour d'une **photo de référence** (badge « réf. »), sans chaînage A~B~C :
   chaque fichier affiche son % de similarité avec la référence.
5. Quatre niveaux de rigueur (très stricte / stricte / normale / large), plus une rigueur « étalonnée ».
   Changer de rigueur recalcule depuis l'index **sans relancer l'analyse**.

### Index persistant
Tout est stocké dans `data/index.sqlite3` (SQLite en mode WAL) : fichiers, empreintes, EXIF, netteté,
visages, personnes, objets, texte OCR (FTS5), embeddings CLIP, scènes, journal des actions, réponses
d'étalonnage. Chaque image n'est **décodée qu'une fois** (mode draft de Pillow pour les JPEG, rotation EXIF),
puis toutes les étapes partent de cette image. Une nouvelle analyse ne traite que les fichiers nouveaux ou
modifiés (taille + date de modification) et purge les fichiers disparus.

## Interface

- **Doublons** :
  - groupes paginés côté serveur, badges « réf. », score, « ★ meilleure », « yeux fermés » ;
  - date EXIF affichée sans décalage de fuseau ;
  - aperçu plein écran et lecture vidéo ;
  - sélection automatique (« garder la meilleure », la plus ancienne, la meilleure résolution…) ;
  - **« Tout sélectionner »** : coche TOUTES les photos de tous les groupes (toutes les pages, référence incluse) ;
    « Tout désélectionner » ; dans chaque groupe, « Tout cocher » en plus de « Tout cocher sauf la réf. ».
    La suppression demande une confirmation si elle viderait un groupe ;
  - bandeau latéral des dossiers avec miniatures : glisser-déposer d'une miniature vers un dossier, avec
    bouton « Annuler ».
- **Personnes** :
  - grille de visages-types ; renommer, fusionner deux personnes, retirer ou confirmer un visage ;
  - glisser un visage sur une personne pour l'y affecter ;
  - « Voir toutes ses photos » ouvre l'Explorer filtré ;
  - bouton **« Effacer tous les visages et personnes »**.
- **Similaires** :
  - bouton « Similaires » sous chaque photo et dans l'aperçu plein écran : toutes les photos qui ressemblent
    à celle-là, même si ce ne sont pas des doublons (recadrage, retouche, même scène, mêmes personnes) ;
  - quatre sources combinables : empreinte visuelle (fonctionne sans aucun modèle), mêmes personnes
    (visages regroupés), mêmes objets (YOLOX), même contenu (CLIP) ; chaque résultat indique sa source et
    son pourcentage ;
  - curseur de ressemblance minimale, « Tout sélectionner », déplacement et suppression groupés ;
  - **recherche par zone** : dans l'aperçu plein écran, « Chercher une zone », puis tracer un rectangle
    autour d'un objet, d'un visage ou d'un détail ; les photos contenant la même chose ressortent (objets
    YOLOX de la zone, visage de la zone, contenu CLIP de la zone). La partie CLIP n'a pas pu être testée ici.
- **Recherche de personne** (identification faciale) :
  - choisir une personne déjà regroupée, ou **envoyer une photo de référence** (JPEG, PNG, HEIC, AVIF, RAW…) ;
    si la photo contient plusieurs visages, on clique sur celui à chercher ; depuis l'onglet Personnes,
    « Retrouver partout » et « Chercher avec le visage sélectionné » lancent la même recherche ;
  - tous les visages indexés ressemblant à la référence sont retrouvés, même non rangés dans une personne ;
    curseur de ressemblance minimale (cosinus ArcFace/SFace), option « petits visages / flous » ;
  - résultats avec pourcentage de ressemblance, « Tout sélectionner » (toutes les pages), déplacer, supprimer,
    et « Affecter les visages trouvés à… » (personne existante ou nouvelle) pour compléter les regroupements ;
  - nécessite d'avoir analysé le dossier avec le module Visages, avec le même moteur (InsightFace ou OpenCV).
- **Nombre de miniatures par page** : 50, 100, 200, 500 ou 1000 (10 à 200 groupes dans Doublons) — pratique
  pour tout sélectionner d'un coup. Le réglage est commun à Explorer, Similaires et Recherche de personne.
- **Explorer** :
  - une seule zone de recherche qui combine CLIP, objets, texte OCR, noms de personnes, scènes et noms de
    fichiers ;
  - filtres : type, personne, objet, scène, flou, dossier, dates ;
  - résultats paginés et actions groupées (déplacement en masse par glisser-déposer) ;
  - **« Tout sélectionner »** : coche tous les résultats de la recherche, sur toutes les pages.
  - Sans CLIP, la recherche fonctionne quand même avec les objets, l'OCR et les personnes.
  - Dans l'aperçu plein écran, les personnes, objets et scènes reconnus sont des **puces cliquables** : un
    clic affiche toutes les photos qui contiennent la même chose (voiture, oiseau, ordinateur…).
  - Les objets reconnus par YOLOX sont les 80 catégories COCO (personne, voiture, oiseau, ordinateur
    portable, chien, vélo…). Pour une notion absente de cette liste (« bras », « coucher de soleil »,
    « facture »), tapez-la dans la zone de recherche : c'est CLIP qui répond, en langage naturel.
- **Étalonnage** : « ces deux photos sont-elles des doublons ? » et « est-ce la même personne ? ». Vos réponses
  ajustent les seuils ; une fausse détection pèse trois fois plus qu'un doublon manqué.
- **Rapport** : fichiers ignorés avec leur cause, images/seconde par étape, durée de chaque phase.
- **Journal** : toutes les suppressions, tous les déplacements et renommages, avec annulation des déplacements
  et renommages.

## Formats d'images pris en charge

JPEG, PNG, GIF, BMP, WebP, TIFF, JPEG 2000, TGA, **HEIC/HEIF/HIF** et **AVIF** (bibliothèque `pillow-heif`,
installée par défaut), et **RAW** d'appareils photo : CR2, CR3, CRW, NEF, NRW, ARW, SR2, ORF, RW2, RAF, PEF,
SRW, DNG, 3FR, IIQ, RWL, X3F… (bibliothèque optionnelle `rawpy` : aperçu JPEG intégré, sinon développement en
demi-résolution). Chaque photo est décodée une fois ; les empreintes visuelles (pHash, 48 et 128 px), les
visages, les objets et l'OCR sont donc calculés de la même façon pour tous les formats : un HEIC et son JPEG
sont reconnus comme doublons. Les formats que le navigateur ne sait pas afficher (HEIC, AVIF, RAW, TIFF…) sont
convertis en JPEG (mis en cache) pour l'aperçu plein écran. Si `pillow-heif` ou `rawpy` manque, le rapport
indique la cause exacte du fichier ignoré.

## Documents, courriels et autres fichiers

Tous les fichiers peuvent être dédoublonnés. Cochez « Documents » et « Autres » (cochés par défaut) :

| Badge | Signification | Fiabilité |
|---|---|---|
| IDENTIQUES | mêmes octets (SHA-256), n'importe quel type de fichier | certaine |
| MÊME CONTENU | même texte dans des fichiers différents : document ré-enregistré, .docx exporté en .pdf / .odt | texte identique, mise en forme et images non comparées |
| MÊME COURRIEL | même identifiant de message (.eml / .msg, exports multiples) | certaine pour le message, pièces jointes non comparées |
| VERSIONS PROCHES | part du texte commune ≥ seuil (80 % par défaut, réglable) | à relire avant de supprimer |

Formats dont le contenu est lu : Word (.docx, .docm, .dotx, .doc), OpenDocument (.odt, .ods, .odp), RTF, Excel
(.xlsx, .xlsm, .xls), PowerPoint (.pptx, .ppsx, .ppt approximatif), PDF (OCR des PDF scannés si « Texte » n'est
pas sur « aucun »), texte (.txt, .md, .csv, .log, .html), courriels (.eml, .msg). Tous les autres fichiers
(archives, .pst, .mbox, musique, programmes…) sont comparés bit à bit.

Le texte est indexé : l'onglet **Rechercher** retrouve un document ou un courriel par un mot de son contenu.
Un clic sur un document affiche son texte ; « Similaires » liste les documents au texte voisin.
« MÊME CONTENU » compare le texte seul : deux documents au même texte mais aux images différentes sont
regroupés. Vérifiez avant de supprimer.

## Lancement silencieux

Aucune fenêtre noire ne reste ouverte : `lancer.bat` installe ce qui manque (fenêtre visible seulement à ce
moment-là), démarre l'application en arrière-plan et se referme ; la page s'ouvre dans le navigateur.
`tools\raccourci.ps1` crée un raccourci sur le Bureau et dans le menu Démarrer. L'exécutable
(`ChasseurDoublons.exe`) est lui aussi sans console.

- Relancer l'application alors qu'elle tourne déjà rouvre simplement la page.
- Les messages sont écrits dans `data\journal.log`. Pour voir la fenêtre de messages : `lancer.bat --console`
  ou `ChasseurDoublons-diagnostic.exe`.
- Sans page ouverte depuis 10 minutes, l'application s'arrête seule (jamais pendant une analyse) ;
  `--arret-auto 0` désactive cet arrêt, `--arret-auto 30` le règle à 30 minutes.

L'interface est claire par défaut ; le bouton « Sombre / Clair » en haut à droite change de thème.

## Quitter

Le bouton **⏻ Quitter** (en haut à droite) arrête complètement l'application : l'analyse en cours est
interrompue, le serveur local et tous ses processus s'arrêtent. Fermer l'onglet du navigateur ne suffit pas.

## Sécurité des actions
- La suppression est **définitive**, sans corbeille (choix assumé).
- Juste avant de supprimer, la taille et la date de modification sont revérifiées. Si le fichier a changé,
  la suppression est refusée.
- Supprimer le **dernier exemplaire** d'un groupe est refusé sans une confirmation explicite.
- Les actions ne portent que sur des fichiers indexés et situés dans le **périmètre autorisé** : les dossiers
  que vous avez analysés avec l'outil (celui en cours et ceux des analyses précédentes) et les destinations de
  déplacement. Rien d'autre sur le disque n'est accessible depuis la page.
- Le serveur écoute sur 127.0.0.1 uniquement. Un jeton propre à chaque lancement est exigé pour toute requête
  d'action, et l'en-tête `Host` est contrôlé (protection contre le DNS rebinding).
- Fichiers Windows :
  - chemins longs (> 260 caractères) gérés via le préfixe `\\?\` ;
  - fichiers verrouillés et fichiers OneDrive non téléchargés signalés clairement. Les seconds sont ignorés
    pour ne pas déclencher leur téléchargement.

## Licences des modèles — à lire
- **InsightFace (buffalo_l : det_10g, w600k_r50, 2d106det)** : usage limité à la **recherche non
  commerciale**. Ces modèles ne sont **jamais** inclus dans le dépôt. Leur téléchargement (archive officielle
  `github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip`, dont seuls les trois fichiers
  utiles sont extraits) demande une confirmation explicite de la licence dans l'interface.
- **Moteur de repli à licence permissive** : OpenCV YuNet (MIT) + SFace (Apache-2.0), à choisir dans
  Réglages > « Moteur de visages ».
  - Écart de précision mesuré sur deux images d'exemple : même visage recadré, cosinus 0,98 (ArcFace) contre
    0,64 à 0,84 (SFace) ; deux personnes différentes, 0,03 contre 0,13.
  - Conséquence : SFace sépare moins bien les personnes qui se ressemblent et tolère moins les profils et les
    petits visages ; son seuil par défaut est plus bas (0,40 contre 0,45).
  - Les yeux fermés ne sont détectés qu'avec InsightFace, car YuNet ne fournit que 5 points de repère.
- **YOLOX** : Apache-2.0. YOLOv8 n'est volontairement pas utilisé : sa licence AGPL pose problème pour un
  dépôt public.
- **CLIP** (OpenAI, conversion ONNX Xenova) : MIT. **rapidocr** : Apache-2.0.
- Le dossier `models/` est ignoré par git.

## Données biométriques
Les empreintes de visages (vecteurs de 512 ou 128 nombres) sont des données biométriques.
- Elles restent dans `data/index.sqlite3`, sur ce PC.
- Elles ne transitent jamais sur le réseau et ne vont jamais dans git (`data/`, `*.sqlite3` et les miniatures
  de visages sont ignorés).
- Le bouton « Effacer tous les visages et personnes » supprime visages, personnes, noms et réponses
  d'étalonnage des visages, puis compacte la base (VACUUM).

## Performances mesurées (machine de test : 4 cœurs, CPU seul, Linux)

| Mesure | Résultat |
|---|---|
| 15 000 images synthétiques 800×600, modules exacts + visuel + qualité | 95 s (158 images/s), RAM max **161 Mo** |
| Seconde analyse des mêmes 15 000 images, sans changement | **1,1 s** |
| Calcul des doublons depuis l'index (15 000 images) | 0,25 s |
| Recherche de candidats pHash sur 100 000 empreintes (seuil 7) | 2,3 s, 80 Mo (36 s en force brute) |
| Tous modules (InsightFace précis, YOLOX-S, OCR toutes images, CLIP), 240 photos ~1300×900 | ~1,7 image/s, RAM max **1,3 Go**, stable d'une analyse à l'autre |

Débits par étape avec tous les modules, en images/s pour 4 threads :
- visages (SCRFD 640 + ArcFace) : ~4 ;
- objets : ~12 ;
- OCR : ~2 (sérialisé) ;
- CLIP : ~20 ;
- décodage : ~110 ;
- empreintes : ~390.

Sur CPU, une première analyse de 15 000 photos avec **tous** les modules prend donc plusieurs heures. Les
analyses suivantes ne traitent que les nouveautés. Un GPU (DirectML ou CUDA) accélère visages, objets et CLIP.
Les débits de votre machine s'affichent dans l'onglet Rapport, ou avec `python tools/bench.py --n 2000`.

## Tests

```
pip install -r requirements-optional.txt
python -m pytest                       # 32 tests sans modèle (images synthétiques générées à la volée)
CD_MODELS_DIR=models CD_TEST_IMAGES=dossier_portraits python -m pytest tests/test_models_real.py
```

- `test_models_real.py` attend `personne_a.jpg` et `personne_b.jpg`, deux portraits de deux personnes
  différentes que vous fournissez (aucune photo n'est dans le dépôt).
- Ce que couvrent les tests :
  - base et analyse incrémentale ;
  - pHash et vérification. Doublons attendus : redimensionnement, JPEG, luminosité. Non-doublons attendus :
    zone déplacée, zone remplacée, décalage, zoom ;
  - regroupement sans chaînage ;
  - suppression revérifiée, dernier exemplaire, journal et annulation ;
  - API (jeton, Host, parcours complet) ;
  - gestionnaire de modèles (`.part`, vérification de taille, zip, licence) ;
  - regroupement des visages avec verrouillage.

## Ce qui a été testé, et ce qui ne l'a pas été

**Testé** (Linux, Python 3.13, CPU, sans GPU) :
- les 32 tests automatiques ;
- les vrais modèles sur deux images d'exemple publiques (échantillons d'OpenCV) :
  - InsightFace : détection, reconnaissance, points de repère des yeux ouverts ;
  - SFace ;
  - YOLOX (personne, ballon) ;
  - OCR d'une capture générée ;
  - CLIP (couleurs, recherche « facture », « sport ») ;
- une analyse complète avec tous les modules ;
- l'interface dans Chromium sans affichage (onglets, recalcul de rigueur, glisser-déposer, aucune erreur
  JavaScript) ;
- un vrai téléchargement de modèle via le gestionnaire.

**Non testé — à vérifier avec `tools/selftest.py` sur votre PC** :
- **Windows** : lancer.bat, chemins longs, fichiers verrouillés, fichiers OneDrive, lecteurs réseau. Le code
  existe mais n'a jamais tourné sous Windows.
- **GPU** (CUDA, DirectML) : seul le fournisseur CPU a été exécuté.
- **Seuils sur une vraie photothèque** : rigueurs, seuil « même personne » (0,45 / 0,40), yeux fermés (0,20),
  flou (60). Ils ne sont calibrés que sur des images synthétiques et deux images d'exemple : utilisez l'onglet
  **Étalonnage**.
  - En test, une copie éclaircie de 10 % atteint b2 = 0,25 (la v1.1 annonçait ≤ 0,18). Elle reste acceptée
    en rigueur stricte (seuil 0,28).
  - Le niveau « large » a accepté une « zone déplacée » sur un des trois jeux de test.
- **Yeux fermés** : jamais testés sur une photo aux yeux fermés.
- **Fichiers RAW réels** : le décodage RAW (rawpy) n'a pas pu être essayé sur de vrais fichiers d'appareil
  (seul le comportement sur fichier invalide est testé). HEIC et AVIF ont été testés (encodage puis analyse,
  doublon HEIC/JPEG, recherche de personne avec une référence HEIC et le vrai modèle InsightFace) ; les HEIC
  provenant d'un iPhone (HDR, photos « live », profils Display P3) n'ont pas été essayés.
- **Recherche de personne** avec le moteur OpenCV (YuNet + SFace) : non testée ici (modèles inaccessibles) ;
  lancez `python tools/selftest.py` sur votre PC.
- **Vidéos réelles** (MOV/MP4 H.264/H.265) : seule une vidéo MJPEG synthétique a
  été testée ; le support dépend des codecs de la version d'OpenCV.
- **Analyse complète de 100 000 images** : seule la recherche de candidats a été mesurée à cette échelle.
- **Version non quantifiée de CLIP** : non prévue ; seuls les modèles `_quantized` sont gérés.

## Limites connues
- Documents : seul le texte est comparé (pas la mise en forme ni les images). Le contenu des archives
  (.zip, .pst, .mbox) n'est pas ouvert. Lecture des .msg, des anciens .xls et des PDF testée sur peu de
  fichiers réels ; anciens .ppt : texte approximatif.
- Les vidéos n'ont que la signature visuelle : pas de visages, d'objets ni de CLIP sur les vidéos.
- CLIP ne comprend que l'anglais. Les mots français courants (objets, scènes, glossaire d'une centaine de
  mots) sont traduits ; une requête plus libre en français donne des résultats moins pertinents.
- L'OCR (modèles chinois/anglais de rapidocr) peut perdre les accents. En mode « captures et documents »,
  toutes les images sans EXIF d'appareil sont lues : images WhatsApp, téléchargements…
- Le périmètre autorisé (affichage et actions) est l'ensemble des dossiers déjà analysés avec l'outil et
  toujours présents sur le disque ; il est rétabli à chaque lancement.
- Le calcul des doublons est synchrone : quelques secondes pour 100 000 images, pendant lesquelles la page
  attend la réponse.
- Les personnes sans nom peuvent être renumérotées lors d'un nouveau regroupement. Nommez une personne pour la
  figer.

## Structure
```
app.py                 point d'entrée (arguments, navigateur)
core/                  config, db (SQLite), imaging (décodage, EXIF, empreintes), scanner (analyse
                       incrémentale), dupes, faces, persons, objects, ocr, clip, search, calib, actions,
                       models (téléchargements, sessions ONNX), web (API Flask)
static/index.html      interface
tests/                 pytest
tools/selftest.py      autotest de chaque module ; tools/bench.py : mesures de performance
```
