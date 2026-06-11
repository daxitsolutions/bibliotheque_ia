"""Passe 50 - Chargement SQLite.

Reconstruit data/kb.sqlite depuis les artefacts du pipeline : documents, chunks,
noeuds, alias, mentions, aretes, fiches, FTS5 et tables vectorielles sqlite-vec
quand l'extension est disponible.
"""
import json
import sqlite3
from datetime import datetime, timezone

from common import (DB_PATH, DIM_EMBEDDING, RACINE, WORK, b64_vers_blob,
                    charger_ontologie, lire_jsonl, log)


def executer_schema(conn: sqlite3.Connection) -> None:
    schema = (RACINE / "config" / "schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)


def charger_vec(conn: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception as e:
        log(f"[!] sqlite-vec indisponible ({e}) : chargement vectoriel ignore")
        return False


def creer_tables_vec(conn: sqlite3.Connection) -> bool:
    if not charger_vec(conn):
        return False
    conn.execute(f"CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[{DIM_EMBEDDING}])")
    conn.execute(f"CREATE VIRTUAL TABLE nodes_vec USING vec0(embedding float[{DIM_EMBEDDING}])")
    return True


def principal() -> None:
    manifest = lire_jsonl(WORK / "manifest.jsonl")
    noeuds = lire_jsonl(WORK / "canon" / "nodes.jsonl")
    if not manifest:
        raise SystemExit("Manifest absent : lancez d'abord scripts/10_normalize.sh")
    if not noeuds:
        raise SystemExit("Nœuds absents : lancez d'abord scripts/30_canonize.sh")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    for suffixe in ("-wal", "-shm"):
        p = DB_PATH.with_name(DB_PATH.name + suffixe)
        if p.exists():
            p.unlink()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    executer_schema(conn)

    for doc in manifest:
        conn.execute(
            "INSERT INTO documents(doc_id, chemin_source, titre, type_document, date_document, sha256) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (doc["doc_id"], doc["chemin_source"], doc.get("titre"), doc.get("type_document"),
             doc.get("date_document"), doc["sha256"]),
        )
        for chunk in lire_jsonl(WORK / "chunks" / f"{doc['doc_id']}.jsonl"):
            conn.execute(
                "INSERT INTO chunks(chunk_id, doc_id, ordre, chemin_titres, texte) VALUES (?, ?, ?, ?, ?)",
                (chunk["chunk_id"], doc["doc_id"], chunk["ordre"],
                 chunk.get("chemin_titres"), chunk["texte"]),
            )

    fiches = {f["node_id"]: f["fiche"] for f in lire_jsonl(WORK / "enrich" / "fiches.jsonl")}
    for n in noeuds:
        conn.execute(
            "INSERT INTO nodes(node_id, type, nom, attributs, fiche) VALUES (?, ?, ?, ?, ?)",
            (n["node_id"], n["type"], n["nom"],
             json.dumps(n.get("attributs") or {}, ensure_ascii=False), fiches.get(n["node_id"])),
        )
    for a in lire_jsonl(WORK / "canon" / "aliases.jsonl"):
        conn.execute("INSERT OR IGNORE INTO aliases(alias, node_id) VALUES (?, ?)",
                     (a["alias"], a["node_id"]))
    for m in lire_jsonl(WORK / "canon" / "mentions.jsonl"):
        conn.execute(
            "INSERT OR IGNORE INTO mentions(node_id, chunk_id, citation) VALUES (?, ?, ?)",
            (m["node_id"], m["chunk_id"], m.get("citation", "")),
        )
    for e in lire_jsonl(WORK / "canon" / "edges.jsonl"):
        conn.execute(
            "INSERT OR IGNORE INTO edges(source_id, type, cible_id, chunk_id, citation) "
            "VALUES (?, ?, ?, ?, ?)",
            (e["source_id"], e["type"], e["cible_id"], e.get("chunk_id") or None,
             e.get("citation", "")),
        )

    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
    vec_ok = creer_tables_vec(conn)
    if vec_ok:
        chunk_rowids = {r["chunk_id"]: r["rowid"] for r in conn.execute("SELECT rowid, chunk_id FROM chunks")}
        node_rowids = {r["node_id"]: r["rowid"] for r in conn.execute("SELECT rowid, node_id FROM nodes")}
        for e in lire_jsonl(WORK / "enrich" / "emb_chunks.jsonl"):
            if e["chunk_id"] in chunk_rowids:
                conn.execute("INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                             (chunk_rowids[e["chunk_id"]], b64_vers_blob(e["vec"])))
        for e in lire_jsonl(WORK / "enrich" / "emb_nodes.jsonl"):
            if e["node_id"] in node_rowids:
                conn.execute("INSERT INTO nodes_vec(rowid, embedding) VALUES (?, ?)",
                             (node_rowids[e["node_id"]], b64_vers_blob(e["vec"])))

    onto = charger_ontologie()
    conn.executemany("INSERT INTO meta(cle, valeur) VALUES (?, ?)", [
        ("construit_le", datetime.now(timezone.utc).isoformat(timespec="seconds")),
        ("ontologie_version", str(onto.get("version", ""))),
        ("sqlite_vec", "oui" if vec_ok else "non"),
    ])
    conn.commit()
    stats = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
             for t in ("documents", "chunks", "nodes", "edges", "mentions")}
    conn.close()
    log(f"Base chargee : {DB_PATH}")
    log(", ".join(f"{k}={v}" for k, v in stats.items()))


if __name__ == "__main__":
    principal()
