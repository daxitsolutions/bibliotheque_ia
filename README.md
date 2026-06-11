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
- Decoupe les documents en passages indexes.
- Utilise un LLM local pour extraire les personnes, decisions, actions, risques,
  tests, procedures, modules, reunions, etc.
- Fusionne les doublons comme `H. Dupont`, `Helene Dupont` et `Mme Dupont`.
- Cree un graphe de connaissances dans SQLite : `nodes` + `edges`.
- Ajoute une recherche plein texte FTS5.
- Ajoute des fiches de synthese par noeud.
- Ajoute des embeddings si Ollama et `sqlite-vec` sont disponibles.
- Expose la base par CLI et par serveur MCP pour une IA.

## Pourquoi

Une IA n'a pas besoin d'une belle arborescence de fichiers. Elle a besoin de bons
points d'entree dans la connaissance :

- `schema` pour comprendre ce qu'il y a dans la base ;
- `recherche` pour trouver les passages et les concepts pertinents ;
- `fiche` pour lire une synthese courte d'un objet ;
- `voisins` pour explorer ce qui est lie ;
- `chemin` pour expliquer le lien entre deux objets ;
- `requete_sql` pour les questions precises ou analytiques.

La base reste locale et reconstructible. Les fichiers originaux restent la source
de verite ; `data/kb.sqlite` est un index enrichi que l'on peut regenerer.

## Installation rapide

Par defaut, le LLM utilise est LM Studio avec le modele :

```text
google/gemma-4-e4b
```

L'installation demande l'URL de l'API LM Studio et un token optionnel.

```bash
./scripts/00_install.sh
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

Si vous voulez aussi Ollama pour les embeddings :

```bash
./scripts/00_install.sh --avec-ollama
```

Ollama est optionnel. Sans embeddings, la base fonctionne quand meme en mode
plein texte + graphe.

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

Les sous-dossiers sont autorises.

Lancez ensuite le pipeline complet :

```bash
./scripts/run_all.sh
```

La base finale est creee ici :

```text
data/kb.sqlite
```

Le rapport qualite est cree ici :

```text
data/rapport_validation.md
```

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

- `10` convertit et decoupe les documents.
- `20` extrait les entites et relations avec le LLM.
- `30` fusionne les doublons et stabilise les identifiants.
- `40` redige les fiches et calcule les embeddings si possible.
- `50` reconstruit `data/kb.sqlite`.
- `60` controle la qualite.

## Interroger la base en CLI

Quelques exemples :

```bash
./scripts/90_query.sh schema
./scripts/90_query.sh recherche "reprise des donnees fournisseurs"
./scripts/90_query.sh fiche DEC-xxxxxxxx
./scripts/90_query.sh voisins DEC-xxxxxxxx --profondeur 2
./scripts/90_query.sh chemin DEC-xxxxxxxx TST-yyyyyyyy
```

Les resultats sont du JSON borne, avec provenance quand elle existe.

## Comment utiliser la DB construite avec mon IA préférée après que les documents aient été absorbés

Une fois `data/kb.sqlite` construit, le plus confortable est d'utiliser le serveur
MCP fourni par le projet. Il expose des outils propres a l'IA, au lieu de lui
donner seulement un fichier SQLite brut.

Demarrez le serveur MCP :

```bash
./mcp/run_mcp.sh
```

Dans votre client IA compatible MCP, ajoutez un serveur de ce type :

```json
{
  "mcpServers": {
    "bibliotheque-ia": {
      "command": "/chemin/absolu/vers/bibliotheque_ia/mcp/run_mcp.sh"
    }
  }
}
```

Remplacez `/chemin/absolu/vers/bibliotheque_ia` par le chemin reel du projet.
Sur cette machine, c'est par exemple :

```text
/Users/Dax/Documents/GitHub/bibliotheque_ia
```

L'IA aura alors acces a ces outils :

- `schema` : comprendre l'ontologie et la volumetrie.
- `recherche` : trouver les passages et noeuds pertinents.
- `fiche` : lire tout ce que la base sait d'un noeud.
- `voisins` : explorer le graphe autour d'un noeud.
- `chemin` : expliquer le lien entre deux noeuds.
- `requete_sql` : lancer une requete SQL en lecture seule.

Exemples de questions a poser a votre IA apres branchement MCP :

- "Commence par appeler `schema`, puis cherche les decisions liees a la reprise
  des donnees fournisseurs."
- "Trouve les risques qui bloquent un jalon et cite les sources."
- "Pour la decision DEC-xxxx, donne-moi le contexte, les actions liees et les
  tests qui la valident."
- "Quel est le chemin entre cette procedure et ce module ?"

Si votre IA ne supporte pas MCP, utilisez la CLI comme passerelle : demandez-lui
quelle commande lancer, executez-la, puis collez-lui le JSON obtenu.

Exemple :

```bash
./scripts/90_query.sh recherche "risques recette" --k 10
```

Pour une IA ou un outil qui sait lire directement SQLite, ouvrez :

```text
data/kb.sqlite
```

Tables principales :

- `documents` : fichiers sources absorbes.
- `chunks` + `chunks_fts` : passages et index plein texte.
- `nodes` : entites canoniques.
- `edges` : relations typees entre entites.
- `mentions` : provenance des noeuds dans les passages.
- `aliases` : variantes fusionnees.

Mais pour un usage IA, MCP reste preferable : les resultats sont bornes,
structures et orientes raisonnement.

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
- `data/kb.sqlite` : base construite.
- `mcp/server.py` : serveur MCP.
- `scripts/90_query.sh` : interrogation CLI.

## Qualite et limites

`data/rapport_validation.md` signale les points a verifier :

- types hors ontologie ;
- aretes pendantes ;
- noeuds isoles ;
- documents sans extraction ;
- couverture vectorielle ;
- echantillons de fiches a relire.

Le LLM peut mal extraire ou mal fusionner certaines informations. Le rapport de
validation sert justement a relire un echantillon, ajuster `config/ontologie.yaml`
ou ajouter des equivalences de canonisation, puis reconstruire la base.
