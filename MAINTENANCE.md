# Procédure pour chaque grosse modification

Un seul dossier de travail : **`C:\Github\chasseur-doublons`**. L'application est à la racine
(`lancer.bat`, `app.py`, `core/`, `static/`, `tools/`, `tests/`). Aucun dossier de version, aucune copie
« ancienne » gardée à côté : les versions précédentes vivent dans l'historique Git et dans les archives `.bak`.

## 1. Sauvegarder avant de toucher au code

```powershell
cd C:\Github\chasseur-doublons
.\tools\sauvegarde.ps1 -Etiquette "avant-<ce-que-je-vais-changer>"
```

Crée `C:\Github\_sauvegardes\chasseur-doublons\chasseur-doublons_v<version>_<date>_<etiquette>.bak.zip`,
**hors du dépôt** (donc jamais commité, jamais poussé sur GitHub), et ne garde que les 10 archives les plus
récentes. Les dossiers régénérables ne sont pas archivés : `.venv` (recréé par `lancer.bat`), `data`
(index SQLite et miniatures, reconstruits par une analyse), `models` (modèles téléchargeables), les caches.

Pour repartir d'une archive : décompresser le `.bak.zip` dans un dossier temporaire et recopier les fichiers
voulus — ou, plus simple, `git checkout <commit> -- <fichier>`.

## 2. Travailler sur une branche

```powershell
git pull
git switch -c feature/<nom-de-la-modification>
```

## 3. Vérifier avant de publier

Toujours avec le Python de l'environnement du dépôt, jamais celui du système :

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests
.\.venv\Scripts\python.exe tools\selftest.py
```

Un test nouveau accompagne chaque nouvelle fonction. Ce qui n'a pas pu être testé est écrit noir sur blanc
dans la section « Ce qui a été testé, et ce qui ne l'a pas été » du README.

## 4. Publier

```powershell
git add -A
git commit -m "vX.Y.Z : <resume>"
git push -u origin feature/<nom-de-la-modification>
gh pr create --fill --base main
gh pr merge --squash --delete-branch
```

`CHANGELOG.md` et `core/__init__.py` (`__version__`) sont mis à jour dans le même commit.

## 5. Purger dans le même commit

Un fichier remplacé est supprimé, pas renommé en `_old` : l'ancienne version est déjà dans l'historique Git
et dans l'archive `.bak` de l'étape 1. Ne doivent jamais être commités : `.venv`, `data`, `models`,
`__pycache__`, `.pytest_cache`, les `.onnx`, les `.sqlite3` et les miniatures (voir `.gitignore`).

## Historique des versions

- **v1.1** — première version (un seul `app.py` + une page web). Archivée dans
  `C:\Github\_sauvegardes\chasseur-doublons\chasseur-doublons_v1.1_*.bak.zip`, et retrouvable dans Git :
  `git show v1.1-finale:app.py` (étiquette posée sur le dernier commit qui contenait la v1).
- **v2.x** — version actuelle, à la racine du dépôt.
