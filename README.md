# Bibliotheque IA

Base de connaissances locale pour IA : extraction batch depuis un corpus de
documents, graphe type dans SQLite, index plein texte FTS5, embeddings optionnels
avec `sqlite-vec`, puis exposition par CLI ou serveur MCP.

## Architecture

- `data/sources/` : fichiers bruts a indexer.
- `data/work/` : artefacts intermediaires reconstructibles.
- `data/kb.sqlite` : base finale.
- `config/ontologie.yaml` : types de noeuds, relations et regles de canonisation.
- `src/` : passes du pipeline.
- `scripts/` : wrappers executables.
- `mcp/server.py` : serveur MCP stdio.

Le modele de donnees combine trois entrees utiles pour une IA :

- recherche plein texte dans les passages sources ;
- recherche vectorielle sur passages et fiches de noeuds quand `sqlite-vec` et
  Ollama sont disponibles ;
- graphe `nodes` / `edges` traversable par voisinage et chemin.

## Installation

Sur Ubuntu :

```bash
./scripts/00_install.sh
```

Avec installation d'Ollama :

```bash
./scripts/00_install.sh --avec-ollama
```

Sur macOS ou autre environnement, creez un venv puis installez :

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Les modeles par defaut sont configures dans `config/settings.sh` :

- fournisseur LLM : LM Studio
- extraction/fiches/arbitrage : `google/gemma-4-e4b`
- embeddings : `bge-m3`

Pendant l'installation, `scripts/00_install.sh` demande l'URL API LM Studio et
un token optionnel. Validez la valeur par defaut `http://localhost:1234/v1` ou
saisissez une autre URL. Le choix est enregistre dans `config/local_settings.sh`,
ignore par Git et cree avec des permissions restrictives.

Pour utiliser le comportement par defaut, demarrez le serveur local
OpenAI-compatible dans LM Studio, chargez `google/gemma-4-e4b`, puis lancez :

```bash
./scripts/run_all.sh
```

Vous pouvez toujours surcharger ponctuellement :

```bash
KB_LLM_PROVIDER=lmstudio \
LMSTUDIO_URL=http://localhost:1234/v1 \
LMSTUDIO_API_KEY=sk-local-optionnel \
KB_MODELE_EXTRACTION=google/gemma-4-e4b \
./scripts/run_all.sh
```

Dans ce mode, LM Studio sert a l'extraction, aux fiches et a l'arbitrage de
canonisation. Les embeddings restent fournis par Ollama (`bge-m3`) si disponible ;
sinon la base reste chargeable en mode plein texte + graphe.

Pour revenir a Ollama comme fournisseur LLM :

```bash
KB_LLM_PROVIDER=ollama KB_MODELE_EXTRACTION=qwen3:14b ./scripts/run_all.sh
```

## Utilisation

Deposez les documents dans `data/sources/`, puis lancez :

```bash
./scripts/run_all.sh
```

Pour reprendre a une etape :

```bash
./scripts/run_all.sh 30
```

Etapes disponibles :

- `10_normalize` : conversion Markdown et decoupage en chunks.
- `20_extract` : extraction LLM des entites et relations.
- `30_canonize` : fusion des entites et resolution des relations.
- `40_enrich` : fiches de synthese et embeddings.
- `50_load` : reconstruction SQLite.
- `60_validate` : rapport qualite.

Interroger la base :

```bash
./scripts/90_query.sh schema
./scripts/90_query.sh recherche "reprise des donnees fournisseurs"
./scripts/90_query.sh fiche DEC-xxxxxxxx
./scripts/90_query.sh voisins DEC-xxxxxxxx --profondeur 2
./scripts/90_query.sh chemin DEC-xxxxxxxx TST-yyyyyyyy
```

## MCP

Le serveur MCP expose les outils `schema`, `recherche`, `fiche`, `voisins`,
`chemin` et `requete_sql` en transport stdio :

```bash
./mcp/run_mcp.sh
```

Configuration generique cote client :

```json
{
  "mcpServers": {
    "bibliotheque-ia": {
      "command": "/chemin/vers/bibliotheque_ia/mcp/run_mcp.sh"
    }
  }
}
```

Pour Claude Desktop ou Claude Code, suivez la documentation officielle MCP sur
[docs.claude.com](https://docs.claude.com/).

## Mode texte seul

La base reste exploitable sans embeddings : `load.py` charge FTS5 et ignore
proprement `sqlite-vec` si l'extension n'est pas disponible. Les passes
`20_extract` et la generation des fiches dans `40_enrich` demandent un LLM
joignable, via Ollama ou LM Studio. Les embeddings demandent Ollama, mais ils
sont ignores proprement si indisponibles.

## Qualite

`data/rapport_validation.md` signale notamment :

- types hors ontologie ;
- aretes pendantes ;
- noeuds isoles ;
- documents sans extraction ;
- couverture vectorielle ;
- echantillons de fiches a relire.
