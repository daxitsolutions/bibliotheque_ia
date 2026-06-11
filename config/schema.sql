-- =============================================================================
-- SCHÉMA DE LA BASE DE CONNAISSANCES
-- Une seule base SQLite : graphe (nodes/edges) + texte (chunks/FTS5) + provenance.
-- Les tables vectorielles (chunks_vec, nodes_vec) sont créées par load.py car
-- leur dimension dépend du modèle d'embedding configuré.
-- =============================================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Fichiers sources ingérés -----------------------------------------------------
CREATE TABLE documents (
    doc_id        TEXT PRIMARY KEY,            -- DOC-xxxxxxxxxx (stable, dérivé du chemin)
    chemin_source TEXT NOT NULL,               -- chemin relatif sous data/sources
    titre         TEXT,
    type_document TEXT,                        -- compte_rendu, cahier_test, procedure, formation...
    date_document TEXT,                        -- AAAA-MM-JJ si détectée
    sha256        TEXT NOT NULL,
    ingere_le     TEXT DEFAULT (datetime('now'))
);

-- Fragments de texte indexés ---------------------------------------------------
CREATE TABLE chunks (
    chunk_id      TEXT PRIMARY KEY,            -- {doc_id}#NNN
    doc_id        TEXT NOT NULL REFERENCES documents(doc_id),
    ordre         INTEGER NOT NULL,
    chemin_titres TEXT,                        -- "Titre 1 > Sous-titre" : contexte hiérarchique
    texte         TEXT NOT NULL
);
CREATE INDEX idx_chunks_doc ON chunks(doc_id);

-- Nœuds du graphe ----------------------------------------------------------------
CREATE TABLE nodes (
    node_id   TEXT PRIMARY KEY,                -- {PREFIXE}-xxxxxxxx (stable, dérivé du nom canonique)
    type      TEXT NOT NULL,                   -- type de l'ontologie
    nom       TEXT NOT NULL,                   -- nom canonique affichable
    attributs TEXT NOT NULL DEFAULT '{}',      -- JSON libre (statut, date, implicite...)
    fiche     TEXT                             -- fiche de synthèse Markdown pré-calculée
);
CREATE INDEX idx_nodes_type ON nodes(type);
CREATE INDEX idx_nodes_nom  ON nodes(nom);

-- Variantes de noms fusionnées lors de la canonisation ---------------------------
CREATE TABLE aliases (
    alias   TEXT NOT NULL,
    node_id TEXT NOT NULL REFERENCES nodes(node_id),
    PRIMARY KEY (alias, node_id)
);

-- Arêtes typées du graphe ---------------------------------------------------------
CREATE TABLE edges (
    edge_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES nodes(node_id),
    type      TEXT NOT NULL,                   -- type de relation de l'ontologie
    cible_id  TEXT NOT NULL REFERENCES nodes(node_id),
    chunk_id  TEXT REFERENCES chunks(chunk_id),-- provenance : le passage qui justifie le lien
    citation  TEXT                             -- extrait court justificatif
);
CREATE INDEX idx_edges_source ON edges(source_id);
CREATE INDEX idx_edges_cible  ON edges(cible_id);
CREATE INDEX idx_edges_type   ON edges(type);
CREATE UNIQUE INDEX idx_edges_unique ON edges(source_id, type, cible_id, IFNULL(chunk_id,''));

-- Provenance des nœuds : où chaque entité est mentionnée ---------------------------
CREATE TABLE mentions (
    node_id  TEXT NOT NULL REFERENCES nodes(node_id),
    chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id),
    citation TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (node_id, chunk_id, citation)
) WITHOUT ROWID;
CREATE INDEX idx_mentions_chunk ON mentions(chunk_id);

-- Index plein texte (tolérant aux accents) -----------------------------------------
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    texte,
    chemin_titres,
    content='chunks',
    content_rowid='rowid',
    tokenize="unicode61 remove_diacritics 2"
);

-- Métadonnées de construction --------------------------------------------------------
CREATE TABLE meta (
    cle    TEXT PRIMARY KEY,
    valeur TEXT
);
