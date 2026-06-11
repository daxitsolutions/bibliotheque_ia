"""Passe 60 — Validation : contrôles qualité et rapport.

Vérifie l'intégrité de la base avant exposition à l'IA : nœuds orphelins,
arêtes pendantes, documents sans extraction, couverture vectorielle, types hors
ontologie. Produit data/rapport_validation.md avec statistiques et échantillons
à relire pour auditer la qualité d'extraction.
"""
import json
import random
from datetime import datetime, timezone

from common import (DB_PATH, RACINE, charger_ontologie, log, ouvrir_db,
                    table_existe)

RAPPORT = RACINE / "data" / "rapport_validation.md"


def principal() -> None:
    if not DB_PATH.exists():
        raise SystemExit("Base absente : lancez d'abord scripts/50_load.sh")
    onto = charger_ontologie()
    conn = ouvrir_db(lecture_seule=True)
    lignes = [f"# Rapport de validation — base de connaissances",
              f"\nGénéré le {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
              f"depuis `{DB_PATH.name}`.\n"]
    alertes = []

    # --- Statistiques générales -----------------------------------------------------
    stats = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
             for t in ("documents", "chunks", "nodes", "edges", "mentions", "aliases")}
    lignes.append("## Volumétrie\n")
    lignes.append("| Table | Lignes |\n|---|---|")
    lignes += [f"| {t} | {n} |" for t, n in stats.items()]

    lignes.append("\n## Nœuds par type\n\n| Type | Nombre |\n|---|---|")
    for r in conn.execute("SELECT type, COUNT(*) c FROM nodes GROUP BY type ORDER BY c DESC"):
        lignes.append(f"| {r['type']} | {r['c']} |")
        if r["type"] not in onto["noeuds"]:
            alertes.append(f"Type de nœud hors ontologie : {r['type']}")

    lignes.append("\n## Arêtes par type\n\n| Relation | Nombre |\n|---|---|")
    for r in conn.execute("SELECT type, COUNT(*) c FROM edges GROUP BY type ORDER BY c DESC"):
        lignes.append(f"| {r['type']} | {r['c']} |")
        if r["type"] not in onto["relations"]:
            alertes.append(f"Type d'arête hors ontologie : {r['type']}")

    # --- Contrôles d'intégrité ----------------------------------------------------------
    orphelins = conn.execute(
        "SELECT COUNT(*) c FROM nodes n WHERE NOT EXISTS "
        "(SELECT 1 FROM edges e WHERE e.source_id = n.node_id OR e.cible_id = n.node_id)"
    ).fetchone()["c"]
    pendantes = conn.execute(
        "SELECT COUNT(*) c FROM edges e WHERE "
        "NOT EXISTS (SELECT 1 FROM nodes WHERE node_id = e.source_id) OR "
        "NOT EXISTS (SELECT 1 FROM nodes WHERE node_id = e.cible_id)"
    ).fetchone()["c"]
    sans_mention = conn.execute(
        "SELECT COUNT(*) c FROM nodes n WHERE NOT EXISTS "
        "(SELECT 1 FROM mentions m WHERE m.node_id = n.node_id)"
    ).fetchone()["c"]
    docs_vides = [r["titre"] for r in conn.execute(
        "SELECT d.titre FROM documents d WHERE NOT EXISTS "
        "(SELECT 1 FROM chunks c JOIN mentions m ON m.chunk_id = c.chunk_id "
        " WHERE c.doc_id = d.doc_id)")]
    sans_fiche = conn.execute(
        "SELECT COUNT(*) c FROM nodes WHERE fiche IS NULL").fetchone()["c"]

    couverture_vec = "indisponible (sqlite-vec absent ou passe 40 non exécutée)"
    if table_existe(conn, "chunks_vec"):
        nb_vec = conn.execute("SELECT COUNT(*) c FROM chunks_vec").fetchone()["c"]
        pct = 100 * nb_vec / max(stats["chunks"], 1)
        couverture_vec = f"{nb_vec}/{stats['chunks']} chunks ({pct:.0f} %)"
        if pct < 95:
            alertes.append(f"Couverture vectorielle partielle : {couverture_vec}")
    else:
        alertes.append("Recherche vectorielle indisponible")

    if pendantes:
        alertes.append(f"{pendantes} arête(s) pendante(s) — anomalie de chargement")
    if orphelins > stats["nodes"] * 0.4:
        alertes.append(f"{orphelins} nœuds isolés (>40 %) : extraction de relations à améliorer")
    if docs_vides:
        alertes.append(f"{len(docs_vides)} document(s) sans aucune entité : "
                       + ", ".join(docs_vides[:5]))

    lignes.append("\n## Intégrité\n")
    lignes.append(f"- Nœuds isolés (aucune arête) : **{orphelins}**")
    lignes.append(f"- Arêtes pendantes : **{pendantes}**")
    lignes.append(f"- Nœuds sans provenance : **{sans_mention}**")
    lignes.append(f"- Nœuds sans fiche : **{sans_fiche}**")
    lignes.append(f"- Couverture vectorielle : {couverture_vec}")

    # --- Nœuds les plus connectés -----------------------------------------------------------
    lignes.append("\n## Nœuds les plus connectés\n\n| Nœud | Type | Degré |\n|---|---|---|")
    for r in conn.execute(
        "SELECT n.node_id, n.nom, n.type, COUNT(*) deg FROM nodes n "
        "JOIN edges e ON n.node_id IN (e.source_id, e.cible_id) "
        "GROUP BY n.node_id ORDER BY deg DESC LIMIT 10"):
        lignes.append(f"| `{r['node_id']}` {r['nom'][:50]} | {r['type']} | {r['deg']} |")

    # --- Échantillon d'audit ------------------------------------------------------------------
    lignes.append("\n## Échantillon d'audit (à relire pour valider la qualité)\n")
    candidats = conn.execute(
        "SELECT node_id, type, nom, fiche FROM nodes WHERE fiche IS NOT NULL").fetchall()
    for r in random.sample(candidats, min(5, len(candidats))):
        citation = conn.execute(
            "SELECT citation FROM mentions WHERE node_id = ? AND citation != '' LIMIT 1",
            (r["node_id"],)).fetchone()
        lignes.append(f"### `{r['node_id']}` — [{r['type']}] {r['nom']}\n")
        if citation:
            lignes.append(f"> Source : « {citation['citation']} »\n")
        lignes.append((r["fiche"] or "").strip() + "\n")

    lignes.append("\n## Alertes\n")
    lignes += [f"- [!] {a}" for a in alertes] if alertes else ["- Aucune alerte critique."]
    RAPPORT.write_text("\n".join(lignes), encoding="utf-8")

    log(f"Validation : {stats['nodes']} nœuds, {stats['edges']} arêtes, "
        f"{orphelins} isolés, {len(alertes)} alerte(s)")
    for a in alertes:
        log(f"  [!] {a}")
    log(f"Rapport complet : {RAPPORT}")
    conn.close()


if __name__ == "__main__":
    principal()
