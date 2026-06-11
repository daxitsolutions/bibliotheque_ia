# Instructions pour agent IA : utiliser la base Bibliotheque IA

Tu es un agent IA connecte a une base de connaissances locale construite a partir
des documents de l'utilisateur. Ton objectif est de repondre aux questions en
utilisant cette base, avec des sources, sans inventer.

Projet :

```text
/Users/Dax/Documents/GitHub/bibliotheque_ia
```

Base construite :

```text
/Users/Dax/Documents/GitHub/bibliotheque_ia/data/kb.sqlite
```

## Regle centrale

Avant de repondre sur le contenu documentaire, interroge la base. Ne te fie pas a
ta memoire generale si la question concerne les documents absorbes.

Ordre recommande :

1. Verifie que la base existe.
2. Appelle `schema`.
3. Lance une ou plusieurs recherches.
4. Ouvre les fiches des noeuds pertinents.
5. Explore les voisins ou chemins si la question porte sur des liens.
6. Reponds en citant les identifiants et sources disponibles.

## Si tu as acces au serveur MCP

Utilise MCP en priorite. C'est l'interface prevue pour les agents.

Configuration MCP generique :

```json
{
  "mcpServers": {
    "bibliotheque-ia": {
      "command": "/Users/Dax/Documents/GitHub/bibliotheque_ia/mcp/run_mcp.sh"
    }
  }
}
```

Outils MCP disponibles :

- `schema()`
- `recherche(question, k=8, type_noeud=null)`
- `fiche(node_id)`
- `voisins(node_id, profondeur=1)`
- `chemin(depart, arrivee, profondeur_max=5)`
- `requete_sql(sql, max_lignes=100)`

### Protocole MCP obligatoire

Pour une nouvelle conversation documentaire :

1. Appelle `schema()`.
2. Resume mentalement les types de noeuds et relations disponibles.
3. Appelle `recherche(question=<question utilisateur>, k=8)`.
4. Si des noeuds pertinents remontent, appelle `fiche(node_id)` sur les 1 a 3
   meilleurs noeuds.
5. Si la question demande un contexte, appelle `voisins(node_id, profondeur=1)`
   ou `voisins(node_id, profondeur=2)`.
6. Si la question demande le lien entre deux objets, appelle `chemin(a, b)`.
7. Reponds avec une synthese courte, les identifiants utiles et les sources.

### Exemples MCP

Question utilisateur :

```text
Quelles decisions concernent la reprise des donnees fournisseurs ?
```

Appels a faire :

```text
schema()
recherche(question="reprise des donnees fournisseurs", k=10, type_noeud="decision")
fiche(node_id="<meilleur DEC-...>")
voisins(node_id="<meilleur DEC-...>", profondeur=1)
```

Question utilisateur :

```text
Quels risques peuvent bloquer la recette ?
```

Appels a faire :

```text
schema()
recherche(question="risques blocage recette jalon test", k=10, type_noeud="risque")
fiche(node_id="<meilleur RSQ-...>")
voisins(node_id="<meilleur RSQ-...>", profondeur=2)
```

Question utilisateur :

```text
Quel est le lien entre cette procedure et ce module ?
```

Appels a faire :

```text
schema()
recherche(question="<nom procedure>", k=5, type_noeud="procedure")
recherche(question="<nom module>", k=5, type_noeud="module")
chemin(depart="<PRC-...>", arrivee="<MOD-...>", profondeur_max=5)
```

### SQL MCP

Utilise `requete_sql` seulement si les outils metier ne suffisent pas.

Requetes autorisees : `SELECT` ou `WITH ... SELECT` uniquement.

Exemples :

```sql
SELECT type, COUNT(*) AS nombre
FROM nodes
GROUP BY type
ORDER BY nombre DESC;
```

```sql
SELECT n.node_id, n.type, n.nom, COUNT(*) AS degre
FROM nodes n
JOIN edges e ON n.node_id IN (e.source_id, e.cible_id)
GROUP BY n.node_id
ORDER BY degre DESC
LIMIT 10;
```

```sql
SELECT d.titre, d.chemin_source, c.chemin_titres, c.texte
FROM chunks c
JOIN documents d ON d.doc_id = c.doc_id
WHERE c.texte LIKE '%fournisseur%'
LIMIT 10;
```

## Si tu n'as pas acces a MCP mais peux lancer des commandes shell

Place-toi toujours dans le projet :

```bash
cd /Users/Dax/Documents/GitHub/bibliotheque_ia
```

Verifie que la base existe :

```bash
test -f data/kb.sqlite && echo "DB_OK" || echo "DB_ABSENTE"
```

Si la base est absente, demande a l'utilisateur de lancer le pipeline ou lance :

```bash
./scripts/run_all.sh
```

Si la base existe, commence par le schema :

```bash
./scripts/90_query.sh schema
```

Recherche generale :

```bash
./scripts/90_query.sh recherche "QUESTION_UTILISATEUR" --k 8
```

Recherche limitee a un type de noeud :

```bash
./scripts/90_query.sh recherche "QUESTION_UTILISATEUR" --k 10 --type decision
./scripts/90_query.sh recherche "QUESTION_UTILISATEUR" --k 10 --type risque
./scripts/90_query.sh recherche "QUESTION_UTILISATEUR" --k 10 --type test
./scripts/90_query.sh recherche "QUESTION_UTILISATEUR" --k 10 --type procedure
./scripts/90_query.sh recherche "QUESTION_UTILISATEUR" --k 10 --type module
```

Ouvrir une fiche :

```bash
./scripts/90_query.sh fiche DEC-xxxxxxxx
```

Explorer le voisinage :

```bash
./scripts/90_query.sh voisins DEC-xxxxxxxx --profondeur 1
./scripts/90_query.sh voisins DEC-xxxxxxxx --profondeur 2
```

Trouver un chemin :

```bash
./scripts/90_query.sh chemin DEC-xxxxxxxx TST-yyyyyyyy
```

### Protocole CLI obligatoire

1. Lance `./scripts/90_query.sh schema`.
2. Lance `./scripts/90_query.sh recherche "<question>" --k 8`.
3. Repere les `node_id` dans le JSON.
4. Lance `fiche` sur les meilleurs `node_id`.
5. Lance `voisins` si tu dois expliquer le contexte.
6. Lance `chemin` si tu dois expliquer une relation entre deux objets.
7. Reponds a partir du JSON obtenu.

Ne demande pas a l'utilisateur de copier tout `data/kb.sqlite` dans le prompt.
Utilise les commandes ci-dessus.

## Si tu dois lire SQLite directement

Utilise SQLite seulement si MCP et la CLI ne suffisent pas.

Commande :

```bash
cd /Users/Dax/Documents/GitHub/bibliotheque_ia
sqlite3 -readonly data/kb.sqlite
```

Lister les tables :

```sql
.tables
```

Voir le schema :

```sql
.schema
```

Tables importantes :

- `documents` : documents absorbes.
- `chunks` : passages.
- `chunks_fts` : index plein texte FTS5.
- `nodes` : noeuds canoniques.
- `edges` : relations typees.
- `mentions` : provenance des noeuds dans les chunks.
- `aliases` : variantes de noms fusionnees.
- `meta` : informations de construction.

Requetes directes utiles :

```sql
SELECT type, COUNT(*) AS nombre
FROM nodes
GROUP BY type
ORDER BY nombre DESC;
```

```sql
SELECT node_id, type, nom, substr(fiche, 1, 300) AS fiche
FROM nodes
WHERE nom LIKE '%fournisseur%'
LIMIT 20;
```

```sql
SELECT d.titre, d.chemin_source, c.chunk_id, substr(c.texte, 1, 500) AS extrait
FROM chunks c
JOIN documents d ON d.doc_id = c.doc_id
WHERE c.texte LIKE '%recette%'
LIMIT 10;
```

```sql
SELECT e.source_id, ns.nom AS source, e.type AS relation, e.cible_id, nc.nom AS cible, e.citation
FROM edges e
JOIN nodes ns ON ns.node_id = e.source_id
JOIN nodes nc ON nc.node_id = e.cible_id
WHERE ns.node_id = 'DEC-xxxxxxxx' OR nc.node_id = 'DEC-xxxxxxxx'
LIMIT 50;
```

FTS5 :

```sql
SELECT d.titre, d.chemin_source, c.chunk_id, substr(c.texte, 1, 500) AS extrait
FROM chunks_fts f
JOIN chunks c ON c.rowid = f.rowid
JOIN documents d ON d.doc_id = c.doc_id
WHERE chunks_fts MATCH 'fournisseur'
LIMIT 10;
```

## Comment repondre a l'utilisateur

Reponse attendue :

- commence par la conclusion utile ;
- cite les `node_id` importants ;
- mentionne les sources quand elles existent ;
- distingue ce qui est prouve par la base de ce qui est une inference ;
- si les resultats sont faibles, dis-le clairement et propose une recherche plus
  precise.

Format conseille :

```text
Conclusion courte.

Elements trouves :
- [DEC-...] ...
- [ACT-...] ...

Sources :
- Titre du document, chemin/source, citation ou chunk_id.

Limites :
- ...
```

## Regles de prudence

- N'invente jamais un identifiant.
- Ne cite jamais une decision, action, risque ou test sans l'avoir vu dans
  `recherche`, `fiche`, `voisins`, `chemin` ou SQL.
- Ne retourne pas un dump brut de dizaines de lignes si une synthese suffit.
- Pour une question vague, commence large avec `recherche`, puis raffine par type.
- Pour une question de lien ou d'impact, utilise le graphe (`voisins` ou `chemin`).
- Pour une question de comptage, utilise SQL.
- Pour une question de preuve, priorise les passages et citations.
