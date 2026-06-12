"""Passe 60 — Rapport consolidé.

Agrège les journaux de toutes les passes (`data/work/journal/*.json`) et l'état de
la base pour produire un rapport unique, structuré et lisible : `data/rapport.md`.

Objectif : qu'un humain puisse, en une lecture, savoir
  - si le pipeline a réussi (résumé exécutif) ;
  - ce qui s'est passé à chaque passe (compteurs) ;
  - quels problèmes examiner (incidents regroupés) ;
  - la volumétrie et la qualité de la base produite.

Le rapport est généré même si la base est absente (échec d'un chargement) : dans
ce cas seuls les journaux d'exécution sont restitués. C'est précisément là qu'un
rapport sert le plus.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone

from common import (DB_PATH, JOURNAL, RACINE, charger_ontologie, ecrire_texte,
                    executer_passe, log, ouvrir_db, table_existe)

RAPPORT = RACINE / "data" / "rapport.md"

# Ordre canonique des passes du pipeline (la passe de rapport elle-même est exclue).
ORDRE_PASSES = ["10_normalize", "20_extract", "30_canonize", "40_enrich", "50_load"]
NOMS_PASSES = {
    "10_normalize": "Normalisation & découpage",
    "20_extract": "Extraction LLM (entités & relations)",
    "30_canonize": "Canonisation (fusion des doublons)",
    "40_enrich": "Enrichissement (fiches & embeddings)",
    "50_load": "Chargement SQLite",
}
ICONES = {"ok": "✅", "avertissements": "⚠️", "echec": "❌",
          "interrompu": "⏹️", "absent": "⬜"}


def charger_journaux() -> dict:
    """Lit les journaux disponibles, indexés par nom de passe."""
    journaux = {}
    if not JOURNAL.exists():
        return journaux
    for chemin in JOURNAL.glob("*.json"):
        try:
            journaux[chemin.stem] = json.loads(chemin.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError, OSError):
            continue
    return journaux


def section_resume(journaux: dict, stats_db: dict | None) -> list:
    statuts = [journaux.get(p, {}).get("statut", "absent") for p in ORDRE_PASSES]
    if "echec" in statuts:
        global_statut, libelle = "echec", "Échec — une passe n'a pas abouti"
    elif "absent" in statuts:
        global_statut, libelle = "avertissements", "Incomplet — certaines passes n'ont pas été exécutées"
    elif "avertissements" in statuts:
        global_statut, libelle = "avertissements", "Succès avec avertissements"
    else:
        global_statut, libelle = "ok", "Succès"

    duree = sum(j.get("duree_s", 0) for j in journaux.values())
    incidents = sum(j.get("compteurs", {}).get("_erreur", 0)
                    + j.get("compteurs", {}).get("_avertissement", 0)
                    for j in journaux.values())

    lignes = ["# Rapport d'exécution — Bibliothèque IA", ""]
    lignes.append(f"Généré le {datetime.now(timezone.utc).isoformat(timespec='seconds')}.")
    lignes.append("")
    lignes.append("## 1. Résumé exécutif")
    lignes.append("")
    lignes.append(f"**Statut global : {ICONES[global_statut]} {libelle}**")
    lignes.append("")
    lignes.append(f"- Durée totale du pipeline : **{duree:.0f} s**")
    lignes.append(f"- Incidents enregistrés : **{incidents}**")
    if stats_db:
        lignes.append(
            f"- Base produite : **{stats_db['documents']}** documents, "
            f"**{stats_db['chunks']}** passages, **{stats_db['nodes']}** nœuds, "
            f"**{stats_db['edges']}** arêtes")
    else:
        lignes.append("- Base produite : **aucune** (chargement non abouti — voir les passes)")
    lignes.append("")
    lignes.append("| Passe | Statut | Durée | Incidents |")
    lignes.append("|---|---|---|---|")
    for p in ORDRE_PASSES:
        j = journaux.get(p)
        if not j:
            lignes.append(f"| {p} — {NOMS_PASSES[p]} | {ICONES['absent']} non exécutée | — | — |")
            continue
        c = j.get("compteurs", {})
        inc = c.get("_erreur", 0) + c.get("_avertissement", 0)
        lignes.append(f"| {p} — {NOMS_PASSES[p]} | {ICONES.get(j['statut'], j['statut'])} "
                      f"{j['statut']} | {j.get('duree_s', 0):.0f} s | {inc} |")
    lignes.append("")
    return lignes


def section_passes(journaux: dict) -> list:
    lignes = ["## 2. Déroulé des passes", ""]
    for p in ORDRE_PASSES:
        j = journaux.get(p)
        titre = f"### {p} — {NOMS_PASSES[p]}"
        if not j:
            lignes += [titre, "", "_Non exécutée._", ""]
            continue
        lignes.append(f"{titre} — {ICONES.get(j['statut'], '')} {j['statut']} "
                      f"({j.get('duree_s', 0):.0f} s)")
        lignes.append("")
        compteurs = {k: v for k, v in j.get("compteurs", {}).items() if not k.startswith("_")}
        if compteurs:
            lignes.append("| Indicateur | Valeur |")
            lignes.append("|---|---|")
            for cle, valeur in compteurs.items():
                lignes.append(f"| {cle.replace('_', ' ')} | {valeur} |")
        else:
            lignes.append("_Aucun compteur._")
        lignes.append("")
    return lignes


def section_problemes(journaux: dict) -> list:
    lignes = ["## 3. Problèmes à examiner", ""]
    total = 0
    for p in ORDRE_PASSES:
        j = journaux.get(p)
        if not j:
            continue
        incidents = j.get("incidents", [])
        erreurs = [i for i in incidents if i["niveau"] == "erreur"]
        averts = [i for i in incidents if i["niveau"] == "avertissement"]
        if not erreurs and not averts:
            continue
        total += len(erreurs) + len(averts)
        lignes.append(f"### {p} — {NOMS_PASSES[p]}")
        lignes.append("")
        for i in erreurs + averts:
            marque = "❌" if i["niveau"] == "erreur" else "⚠️"
            detail = f" — {i['detail']}" if i.get("detail") else ""
            lignes.append(f"- {marque} **{i['sujet']}**{detail}")
        tronques = j.get("incidents_tronques", 0)
        if tronques:
            lignes.append(f"- _… {tronques} incident(s) supplémentaire(s) non détaillé(s) "
                          "(voir les compteurs)._")
        lignes.append("")
    if total == 0:
        lignes.append("Aucun incident enregistré. ✅")
        lignes.append("")
    return lignes


def section_base(conn, onto) -> tuple[list, dict, list]:
    """Volumétrie + intégrité + audit de la base. Renvoie (lignes, stats, alertes)."""
    lignes = ["## 4. Volumétrie & qualité de la base", ""]
    alertes = []

    stats = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
             for t in ("documents", "chunks", "nodes", "edges", "mentions", "aliases")}
    lignes.append("| Table | Lignes |")
    lignes.append("|---|---|")
    lignes += [f"| {t} | {n} |" for t, n in stats.items()]

    lignes.append("\n### Nœuds par type\n\n| Type | Nombre |\n|---|---|")
    for r in conn.execute("SELECT type, COUNT(*) c FROM nodes GROUP BY type ORDER BY c DESC"):
        lignes.append(f"| {r['type']} | {r['c']} |")
        if r["type"] not in onto["noeuds"]:
            alertes.append(f"Type de nœud hors ontologie : {r['type']}")

    lignes.append("\n### Arêtes par type\n\n| Relation | Nombre |\n|---|---|")
    for r in conn.execute("SELECT type, COUNT(*) c FROM edges GROUP BY type ORDER BY c DESC"):
        lignes.append(f"| {r['type']} | {r['c']} |")
        if r["type"] not in onto["relations"]:
            alertes.append(f"Type d'arête hors ontologie : {r['type']}")

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
    sans_fiche = conn.execute("SELECT COUNT(*) c FROM nodes WHERE fiche IS NULL").fetchone()["c"]

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
    if stats["nodes"] and orphelins > stats["nodes"] * 0.4:
        alertes.append(f"{orphelins} nœuds isolés (>40 %) : extraction de relations à améliorer")
    if docs_vides:
        alertes.append(f"{len(docs_vides)} document(s) sans aucune entité : "
                       + ", ".join(t for t in docs_vides[:5] if t))

    lignes.append("\n### Intégrité\n")
    lignes.append(f"- Nœuds isolés (aucune arête) : **{orphelins}**")
    lignes.append(f"- Arêtes pendantes : **{pendantes}**")
    lignes.append(f"- Nœuds sans provenance : **{sans_mention}**")
    lignes.append(f"- Nœuds sans fiche : **{sans_fiche}**")
    lignes.append(f"- Couverture vectorielle : {couverture_vec}")

    lignes.append("\n### Nœuds les plus connectés\n\n| Nœud | Type | Degré |\n|---|---|---|")
    for r in conn.execute(
        "SELECT n.node_id, n.nom, n.type, COUNT(*) deg FROM nodes n "
        "JOIN edges e ON n.node_id IN (e.source_id, e.cible_id) "
        "GROUP BY n.node_id ORDER BY deg DESC LIMIT 10"):
        lignes.append(f"| `{r['node_id']}` {r['nom'][:50]} | {r['type']} | {r['deg']} |")

    lignes.append("\n### Échantillon d'audit (à relire pour valider la qualité)\n")
    candidats = conn.execute(
        "SELECT node_id, type, nom, fiche FROM nodes WHERE fiche IS NOT NULL").fetchall()
    for r in random.sample(candidats, min(5, len(candidats))):
        citation = conn.execute(
            "SELECT citation FROM mentions WHERE node_id = ? AND citation != '' LIMIT 1",
            (r["node_id"],)).fetchone()
        lignes.append(f"#### `{r['node_id']}` — [{r['type']}] {r['nom']}\n")
        if citation:
            lignes.append(f"> Source : « {citation['citation']} »\n")
        lignes.append((r["fiche"] or "").strip() + "\n")

    return lignes, stats, alertes


def principal(passe) -> None:
    journaux = charger_journaux()
    onto = charger_ontologie()

    lignes_base, stats_db, alertes = [], None, []
    if DB_PATH.exists():
        conn = ouvrir_db(lecture_seule=True)
        try:
            lignes_base, stats_db, alertes = section_base(conn, onto)
        finally:
            conn.close()
    else:
        passe.avertissement("Base absente : rapport limité aux journaux d'exécution")

    lignes = section_resume(journaux, stats_db)
    lignes += section_passes(journaux)
    lignes += section_problemes(journaux)
    if lignes_base:
        lignes += lignes_base
    lignes.append("\n## 5. Alertes qualité\n")
    lignes += [f"- ⚠️ {a}" for a in alertes] if alertes else ["- Aucune alerte critique."]

    ecrire_texte(RAPPORT, "\n".join(lignes) + "\n")

    passe.compter("passes_journalisees", len(journaux))
    passe.compter("alertes_qualite", len(alertes))
    log(f"Rapport consolidé : {RAPPORT}")
    for a in alertes:
        log(f"  [!] {a}")


if __name__ == "__main__":
    executer_passe("60_rapport", principal)
