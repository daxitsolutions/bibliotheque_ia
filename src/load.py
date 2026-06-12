"""Passe 50 - Chargement SQLite.

Reconstruit data/kb.sqlite depuis les artefacts du pipeline : documents, chunks,
noeuds, alias, mentions, aretes, fiches, FTS5 et tables vectorielles sqlite-vec
quand l'extension est disponible.

Sécurité : la base est construite dans un fichier temporaire puis permutée de
façon atomique. Une base de connaissances existante et fonctionnelle n'est JAMAIS
détruite si le chargement échoue. Les lignes incohérentes (arête pendante,
provenance vers un chunk absent...) sont ignorées et comptées, pas fatales.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

from common import (DB_PATH, DIM_EMBEDDING, RACINE, WORK, b64_vers_blob,
                    charger_ontologie, executer_passe, lire_jsonl, log)


def executer_schema(conn: sqlite3.Connection) -> None:
    schema = (RACINE / "config" / "schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)


def charger_vec(conn: sqlite3.Connection, passe) -> bool:
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception as e:
        passe.avertissement("sqlite-vec indisponible : chargement vectoriel ignoré", str(e))
        return False


def creer_tables_vec(conn: sqlite3.Connection, passe) -> bool:
    if not charger_vec(conn, passe):
        return False
    conn.execute(f"CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[{DIM_EMBEDDING}])")
    conn.execute(f"CREATE VIRTUAL TABLE nodes_vec USING vec0(embedding float[{DIM_EMBEDDING}])")
    return True


def inserer(conn, passe, sql, lignes, etiquette):
    """executemany résilient : sur violation, bascule en ligne-à-ligne et compte les rejets.

    Garantit qu'une poignée de lignes incohérentes ne fait pas échouer tout le
    chargement de milliers de documents.
    """
    if not lignes:
        return 0
    try:
        conn.executemany(sql, lignes)
        return len(lignes)
    except sqlite3.Error:
        pass
    ok = 0
    for ligne in lignes:
        try:
            conn.execute(sql, ligne)
            ok += 1
        except sqlite3.Error as e:
            passe.compter(f"rejets_{etiquette}")
            if passe.compteurs.get(f"rejets_{etiquette}", 0) <= 3:
                passe.avertissement(f"Ligne {etiquette} rejetée", str(e))
    return ok


def _nettoyer(*chemins) -> None:
    for chemin in chemins:
        for suffixe in ("", "-wal", "-shm"):
            p = chemin.with_name(chemin.name + suffixe)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass


def principal(passe) -> None:
    manifest = lire_jsonl(WORK / "manifest.jsonl")
    noeuds = lire_jsonl(WORK / "canon" / "nodes.jsonl")
    if not manifest:
        raise SystemExit("Manifest absent : lancez d'abord scripts/10_normalize.sh")
    if not noeuds:
        raise SystemExit("Nœuds absents : lancez d'abord scripts/30_canonize.sh")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db_build = DB_PATH.with_name(DB_PATH.name + ".build")
    _nettoyer(db_build)  # restes d'un build précédent interrompu

    conn = sqlite3.connect(str(db_build))
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        executer_schema(conn)

        # --- Documents + chunks ---------------------------------------------------------
        ids_chunks = set()
        for doc in manifest:
            inserer(conn, passe,
                    "INSERT INTO documents(doc_id, chemin_source, titre, type_document, date_document, sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [(doc["doc_id"], doc["chemin_source"], doc.get("titre"), doc.get("type_document"),
                      doc.get("date_document"), doc["sha256"])], "document")
            chunks = lire_jsonl(WORK / "chunks" / f"{doc['doc_id']}.jsonl")
            inserer(conn, passe,
                    "INSERT INTO chunks(chunk_id, doc_id, ordre, chemin_titres, texte) VALUES (?, ?, ?, ?, ?)",
                    [(c["chunk_id"], doc["doc_id"], c["ordre"], c.get("chemin_titres"), c["texte"])
                     for c in chunks], "chunk")
            ids_chunks.update(c["chunk_id"] for c in chunks)

        # --- Nœuds (référentiel des identifiants valides) -------------------------------
        fiches = {f["node_id"]: f["fiche"] for f in lire_jsonl(WORK / "enrich" / "fiches.jsonl")}
        inserer(conn, passe,
                "INSERT INTO nodes(node_id, type, nom, attributs, fiche) VALUES (?, ?, ?, ?, ?)",
                [(n["node_id"], n["type"], n["nom"],
                  json.dumps(n.get("attributs") or {}, ensure_ascii=False), fiches.get(n["node_id"]))
                 for n in noeuds], "node")
        ids_noeuds = {n["node_id"] for n in noeuds}

        # --- Alias, mentions, arêtes : filtrés sur les identifiants connus --------------
        # On élague en amont les lignes pendantes (FK) pour ne pas dépendre du moteur.
        alias = [a for a in lire_jsonl(WORK / "canon" / "aliases.jsonl") if a["node_id"] in ids_noeuds]
        inserer(conn, passe, "INSERT OR IGNORE INTO aliases(alias, node_id) VALUES (?, ?)",
                [(a["alias"], a["node_id"]) for a in alias], "alias")

        mentions = lire_jsonl(WORK / "canon" / "mentions.jsonl")
        mentions_ok = [m for m in mentions
                       if m["node_id"] in ids_noeuds and m["chunk_id"] in ids_chunks]
        passe.compter("mentions_pendantes", len(mentions) - len(mentions_ok))
        inserer(conn, passe, "INSERT OR IGNORE INTO mentions(node_id, chunk_id, citation) VALUES (?, ?, ?)",
                [(m["node_id"], m["chunk_id"], m.get("citation", "")) for m in mentions_ok], "mention")

        edges = lire_jsonl(WORK / "canon" / "edges.jsonl")
        edges_ok = [e for e in edges
                    if e["source_id"] in ids_noeuds and e["cible_id"] in ids_noeuds
                    and (not e.get("chunk_id") or e["chunk_id"] in ids_chunks)]
        passe.compter("aretes_pendantes", len(edges) - len(edges_ok))
        inserer(conn, passe,
                "INSERT OR IGNORE INTO edges(source_id, type, cible_id, chunk_id, citation) "
                "VALUES (?, ?, ?, ?, ?)",
                [(e["source_id"], e["type"], e["cible_id"], e.get("chunk_id") or None,
                  e.get("citation", "")) for e in edges_ok], "arete")

        # --- Index plein texte + vectoriel ----------------------------------------------
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
        vec_ok = creer_tables_vec(conn, passe)
        if vec_ok:
            chunk_rowids = {r["chunk_id"]: r["rowid"] for r in conn.execute("SELECT rowid, chunk_id FROM chunks")}
            node_rowids = {r["node_id"]: r["rowid"] for r in conn.execute("SELECT rowid, node_id FROM nodes")}
            inserer(conn, passe, "INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                    [(chunk_rowids[e["chunk_id"]], b64_vers_blob(e["vec"]))
                     for e in lire_jsonl(WORK / "enrich" / "emb_chunks.jsonl")
                     if e["chunk_id"] in chunk_rowids], "chunk_vec")
            inserer(conn, passe, "INSERT INTO nodes_vec(rowid, embedding) VALUES (?, ?)",
                    [(node_rowids[e["node_id"]], b64_vers_blob(e["vec"]))
                     for e in lire_jsonl(WORK / "enrich" / "emb_nodes.jsonl")
                     if e["node_id"] in node_rowids], "node_vec")

        onto = charger_ontologie()
        conn.executemany("INSERT INTO meta(cle, valeur) VALUES (?, ?)", [
            ("construit_le", datetime.now(timezone.utc).isoformat(timespec="seconds")),
            ("ontologie_version", str(onto.get("version", ""))),
            ("sqlite_vec", "oui" if vec_ok else "non"),
        ])
        conn.commit()
        stats = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                 for t in ("documents", "chunks", "nodes", "edges", "mentions")}
        # Base finale = fichier unique autonome (pas de sidecars WAL), pour que
        # l'ouverture en lecture seule (CLI, MCP, rapport) fonctionne partout.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode = DELETE")
    except Exception:
        conn.close()
        _nettoyer(db_build)  # base existante préservée intacte
        raise
    conn.close()

    # --- Permutation atomique : la nouvelle base remplace l'ancienne en un seul geste ---
    _nettoyer(DB_PATH)
    os.replace(db_build, DB_PATH)
    # Sidecars WAL éventuels du build (orphelins après le déplacement du fichier principal).
    for suffixe in ("-wal", "-shm"):
        p = db_build.with_name(db_build.name + suffixe)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass

    for cle, valeur in stats.items():
        passe.compter(cle, valeur)
    log(f"Base chargée : {DB_PATH}")
    log(", ".join(f"{k}={v}" for k, v in stats.items()))


if __name__ == "__main__":
    executer_passe("50_load", principal)
