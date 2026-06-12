# Bibliotheque IA

Bibliotheque IA transforme un dossier de documents en base de connaissances
locale, interrogeable par une IA.

Au lieu de donner a l'IA une pile de fichiers a relire a chaque question, le
pipeline absorbe le corpus une fois, extrait les entites importantes, cree des
liens entre elles, garde les passages sources, puis construit une base SQLite.
L'IA peut ensuite chercher, citer ses sources, ouvrir une fiche, suivre les liens
du graphe et executer des requetes SQL en lecture seule.

## Ce que ca fait

- Convertit les documents de `data/sources/` en texte exploitable.
- Convertit les fichiers Office en Markdown avant traitement.
- Decoupe les documents en passages indexes.
- Utilise un LLM local pour extraire les personnes, decisions, actions, risques,
  tests, procedures, modules, reunions, etc.
- Fusionne les doublons comme `H. Dupont`, `Helene Dupont` et `Mme Dupont`.
- Cree un graphe de connaissances dans SQLite : `nodes` + `edges`.
- Ajoute une recherche plein texte FTS5.
- Ajoute des fiches de synthese par noeud.
- Ajoute une recherche semantique (embeddings via LM Studio ou Ollama + `sqlite-vec`).
- Expose la base par CLI et par serveur MCP pour une IA.

## Pourquoi

Une IA n'a pas besoin d'une belle arborescence de fichiers. Elle a besoin de bons
points d'entree dans la connaissance :

- `schema` pour comprendre ce qu'il y a dans la base ;
- `recherche` pour trouver les passages et les concepts pertinents ;
- `dossier` pour rassembler en un appel TOUS les documents et passages lies a un
  sujet (directs et indirects), avec le document d'origine et la chronologie ;
- `fiche` pour lire une synthese courte d'un objet ;
- `voisins` pour explorer ce qui est lie ;
- `chemin` pour expliquer le lien entre deux objets ;
- `requete_sql` pour les questions precises ou analytiques.

La base reste locale et reconstructible. Les fichiers originaux restent la source
de verite ; `data/kb.sqlite` est un index enrichi que l'on peut regenerer.

## Installation rapide

Prerequis : Python >= 3.10 (les paquets `markitdown` et `mcp` l'exigent).

Par defaut, le LLM utilise est LM Studio avec le modele :

```text
google/gemma-4-e4b
```

L'installation demande l'URL de l'API LM Studio et un token optionnel.

```bash
./scripts/00_install.sh
```

`00_install.sh` cible Ubuntu (paquets via `apt`). Sur macOS ou sans `apt`,
installez les dependances a la main (en remplacant `python3.12` par votre
interpreteur >= 3.10) :

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
# LibreOffice (conversion Office) : optionnel, ex. `brew install --cask libreoffice`
# puis creez config/local_settings.sh avec votre URL LM Studio (voir ci-dessous).
```

Valeurs par defaut :

```text
LMSTUDIO_URL=http://localhost:1234/v1
LMSTUDIO_API_KEY=
KB_LLM_PROVIDER=lmstudio
KB_MODELE_EXTRACTION=google/gemma-4-e4b
```

Le choix est enregistre dans `config/local_settings.sh`, ignore par Git et cree
avec des permissions restrictives.

### Embeddings

Les embeddings (recherche semantique) peuvent etre fournis par le meme serveur
LM Studio, via son endpoint `/embeddings` OpenAI-compatible. Chargez un modele
d'embeddings dans LM Studio et configurez :

```text
KB_EMBEDDING_PROVIDER=lmstudio
KB_MODELE_EMBEDDING=text-embedding-nomic-embed-text-v1.5
KB_DIM_EMBEDDING=768          # doit correspondre au modele choisi
```

Alternative : Ollama (`KB_EMBEDDING_PROVIDER=ollama`, modele `bge-m3`, 1024 dim) :

```bash
./scripts/00_install.sh --avec-ollama
```

Les embeddings sont optionnels. S'ils sont indisponibles, la base fonctionne
quand meme en mode plein texte + graphe (signale dans le rapport).

## Preparer LM Studio

1. Ouvrez LM Studio.
2. Chargez le modele `google/gemma-4-e4b`.
3. Demarrez le serveur local OpenAI-compatible.
4. Verifiez que l'URL correspond a celle configuree, par defaut :

```text
http://localhost:1234/v1
```

Si votre serveur demande un token, renseignez-le pendant l'installation ou
surchargez-le ponctuellement :

```bash
LMSTUDIO_API_KEY=sk-local-optionnel ./scripts/run_all.sh
```

## Absorber les documents

Deposez vos fichiers dans :

```text
data/sources/
```

Les sous-dossiers sont autorises, meme avec une arborescence profonde ou complexe.
La passe de normalisation parcourt recursivement tout `data/sources/` et tente
d'absorber chaque fichier lisible. Les fichiers binaires non textuels ou
illisibles sont signales puis ignores.

Les fichiers Office sont convertis avant traitement :

```text
Office -> data/work/office_md/*.md -> chunks -> extraction
```

La conversion Office utilise LibreOffice/OpenOffice en mode headless quand la
commande `libreoffice`, `soffice` ou `openoffice` est disponible. Formats pris en
charge : Word/Writer (`.doc`, `.docx`, `.odt`, `.rtf`...), Excel/Calc (`.xls`,
`.xlsx`, `.ods`, `.csv`...), PowerPoint/Impress (`.ppt`, `.pptx`, `.odp`...).
Le pipeline verifie d'abord que LibreOffice/OpenOffice repond correctement. Si
l'installation locale est absente ou plante, il le signale une fois puis tente un
fallback via `markitdown`, puis une lecture texte brute quand c'est possible.

Lancez ensuite le pipeline complet :

```bash
./scripts/run_all.sh
```

La base finale est creee ici :

```text
data/kb.sqlite
```

Le rapport d'execution consolide est cree ici :

```text
data/rapport.md
```

Il agrege les journaux de toutes les passes (`data/work/journal/*.json`) et l'etat
de la base : statut global, deroule de chaque passe, problemes a examiner,
volumetrie et qualite. Il est genere meme si une passe echoue, pour rester
analysable. Un echec de passe interrompt le pipeline mais ne detruit jamais la
base existante : le chargement construit une base temporaire puis la permute de
facon atomique.

## Reprendre une etape

Le pipeline est decoupe en passes :

```bash
./scripts/10_normalize.sh
./scripts/20_extract.sh
./scripts/30_canonize.sh
./scripts/40_enrich.sh
./scripts/50_load.sh
./scripts/60_validate.sh
```

Pour reprendre depuis une etape :

```bash
./scripts/run_all.sh 30
```

Repere simple :

- `10` convertit les fichiers Office en Markdown, decoupe les documents et elague
  les artefacts orphelins.
- `20` extrait les entites et relations avec le LLM.
- `30` fusionne les doublons et stabilise les identifiants.
- `40` redige les fiches et calcule les embeddings si possible.
- `50` reconstruit `data/kb.sqlite` (build temporaire + permutation atomique).
- `60` agrege les journaux et produit `data/rapport.md`.

## Interroger la base en CLI

Quelques exemples :

```bash
./scripts/90_query.sh schema
./scripts/90_query.sh recherche "reprise des donnees fournisseurs"
./scripts/90_query.sh dossier "regle de conversion d'un chiffre en texte" --profondeur 2
./scripts/90_query.sh fiche DEC-xxxxxxxx
./scripts/90_query.sh voisins DEC-xxxxxxxx --profondeur 2
./scripts/90_query.sh chemin DEC-xxxxxxxx TST-yyyyyyyy
```

Les resultats sont du JSON borne, avec provenance quand elle existe.

`dossier` est le point d'entree pour « trouve-moi tout ce qui concerne X » : il
part du sujet, remonte au document qui le definit en premier (le plus ancien
date), puis collecte tous les documents lies directement ou indirectement dans le
graphe (par exemple un PV de comite qui valide une regle), chacun avec ses
passages et, pour les liens indirects, le chemin de relations qui le justifie.

## Comment utiliser la DB construite avec mon IA préférée après que les documents aient été absorbés

Donnez a votre agent IA le fichier d'instructions suivant :

[docs/agent_instructions.md](docs/agent_instructions.md)

Ce fichier est redige comme un prompt d'exploitation pour agent. Il contient :

- le chemin exact du projet ;
- la configuration MCP a utiliser ;
- les commandes CLI exactes ;
- le protocole de recherche obligatoire ;
- les requetes SQLite utiles ;
- les regles de reponse avec sources ;
- les consignes de prudence pour ne pas inventer.

Instruction simple a donner a votre IA :

```text
Lis docs/agent_instructions.md et applique ces consignes pour repondre a mes
questions a partir de la base Bibliotheque IA.
```

## Changer de fournisseur LLM

LM Studio est le defaut. Pour revenir ponctuellement a Ollama :

```bash
KB_LLM_PROVIDER=ollama \
KB_MODELE_EXTRACTION=qwen3:14b \
./scripts/run_all.sh
```

Pour changer l'URL LM Studio ou le token sans relancer l'installation :

```bash
LMSTUDIO_URL=http://localhost:1234/v1 \
LMSTUDIO_API_KEY=sk-local-optionnel \
./scripts/run_all.sh
```

## Fichiers importants

- `config/ontologie.yaml` : vocabulaire ferme de la base.
- `config/settings.sh` : configuration par defaut.
- `config/local_settings.sh` : configuration locale creee par l'installation.
- `data/sources/` : documents originaux.
- `data/work/` : artefacts intermediaires.
- `data/work/office_md/` : Markdown issu des conversions Office.
- `data/kb.sqlite` : base construite.
- `docs/agent_instructions.md` : consignes completes a donner a une IA.
- `mcp/server.py` : serveur MCP.
- `scripts/90_query.sh` : interrogation CLI.

## Qualite et limites

`data/rapport.md` signale les points a verifier :

- statut global et statut de chaque passe (succes / avertissements / echec) ;
- compteurs par passe (documents, chunks, entites, fusions, rejets...) ;
- problemes a examiner, regroupes par passe (conversions impossibles, extractions
  en echec, lignes rejetees...) ;
- types hors ontologie, aretes pendantes, noeuds isoles, documents sans
  extraction, couverture vectorielle ;
- echantillons de fiches a relire.

Robustesse a l'echelle de milliers de documents :

- chaque fichier est isole : un document piege (binaire, corrompu, conversion qui
  plante) est signale puis ignore, jamais fatal pour le corpus ;
- la normalisation ecrit un manifest incremental : un arret apres des heures ne
  reperd pas tout, et elle elague les artefacts des documents disparus
  (sources supprimees/renommees) pour ne pas polluer la base ;
- detection de changement par metadonnees (mtime + taille) avant de re-hasher un
  fichier : a l'echelle de milliers de gros documents, les fichiers inchanges ne
  sont pas relus inutilement ;
- le cache d'extraction, de fiches et d'embeddings est invalide non seulement par
  le contenu mais aussi par la **logique** (prompt, ontologie, modele LLM, modele
  d'embedding) : changer un prompt ou un modele recalcule automatiquement, et
  uniquement, ce qui est concerne ;
- un appel LLM en echec n'est jamais cache comme un resultat vide : le chunk
  concerne est reessaye au run suivant, sans bloquer les chunks reussis ;
- les fichiers JSONL intermediaires tolerent les lignes corrompues ;
- le chargement est atomique : la base existante n'est jamais detruite si le
  chargement echoue, et les lignes incoherentes sont ignorees et comptees ;
- toute passe ecrit son journal `data/work/journal/<passe>.json` meme en cas de
  plantage, et le rapport est produit dans tous les cas.

Le LLM peut mal extraire ou mal fusionner certaines informations. Le rapport sert
justement a relire un echantillon, ajuster `config/ontologie.yaml` ou ajouter des
equivalences de canonisation, puis reconstruire la base.
