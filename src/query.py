"""Couche de requête de la base de connaissances.

Fonctions partagées par la CLI (scripts/90_query.sh) et le serveur MCP :
  - schema()                     : ontologie + volumétrie (auto-description)
  - recherche(question, ...)     : hybride vecteurs + plein texte, fusion RRF
  - fiche(node_id)               : tout ce que la base sait d'un nœud
  - voisins(node_id, profondeur) : voisinage du graphe
  - chemin(a, b)                 : plus court chemin entre deux nœuds
Tous les résultats sont des structures JSON bornées avec provenance.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict, deque

from common import (avertir, charger_ontologie, embeddings, ollama_disponible,
                    ouvrir_db, table_existe, vec_vers_blob)

RRF_K = 60


# --- Auto-description ----------------------------------------------------------------
def schema() -> dict:
    onto = charger_ontologie()
    conn = ouvrir_db(lecture_seule=True)
    noeuds = {r["type"]: r["c"] for r in
              conn.execute("SELECT type, COUNT(*) c FROM nodes GROUP BY type")}
    aretes = {r["type"]: r["c"] for r in
              conn.execute("SELECT type, COUNT(*) c FROM edges GROUP BY type")}
    meta = {r["cle"]: r["valeur"] for r in conn.execute("SELECT * FROM meta")}
    conn.close()
    return {
        "description": "Base de connaissances projet : graphe typé + recherche "
                       "sémantique + plein texte. Les identifiants (DEC-xxxx, TST-xxxx...) "
                       "sont stables et réutilisables entre les appels.",
        "types_de_noeuds": {t: {"description": d["description"],
                                "nombre": noeuds.get(t, 0)}
                            for t, d in onto["noeuds"].items()},
        "types_de_relations": {t: {"description": d["description"],
                                   "sources": d["sources"], "cibles": d["cibles"],
                                   "nombre": aretes.get(t, 0)}
                               for t, d in onto["relations"].items()},
        "meta": meta,
    }


# --- Recherche hybride ------------------------------------------------------------------
def _fts(conn, question: str, limite: int) -> list:
    mots = re.findall(r"[\w\u00C0-\u017F]{2,}", question)
    if not mots:
        return []
    requete = " ".join(f'"{m}"' for m in mots)
    try:
        rangs = conn.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
            "ORDER BY bm25(chunks_fts) LIMIT ?", (requete, limite)).fetchall()
        if not rangs:  # repli : OR entre les termes
            requete = " OR ".join(f'"{m}"' for m in mots)
            rangs = conn.execute(
                "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
                "ORDER BY bm25(chunks_fts) LIMIT ?", (requete, limite)).fetchall()
        return [r["rowid"] for r in rangs]
    except Exception as e:
        avertir(f"FTS en échec : {e}")
        return []


def _vec(conn, table: str, vecteur, limite: int) -> list:
    if not table_existe(conn, table):
        return []
    try:
        rangs = conn.execute(
            f"SELECT rowid FROM {table} WHERE embedding MATCH ? AND k = ? "
            "ORDER BY distance", (vec_vers_blob(vecteur), limite)).fetchall()
        return [r["rowid"] for r in rangs]
    except Exception as e:
        avertir(f"Recherche vectorielle en échec : {e}")
        return []


def _rrf(listes: list) -> dict:
    scores = defaultdict(float)
    for liste in listes:
        for rang, ident in enumerate(liste):
            scores[ident] += 1.0 / (RRF_K + rang)
    return scores


def recherche(question: str, k: int = 8, type_noeud: str | None = None) -> dict:
    """Recherche hybride : renvoie passages ET nœuds pertinents, avec provenance."""
    conn = ouvrir_db(lecture_seule=True)
    vecteur, mode = None, "plein texte seul"
    if table_existe(conn, "chunks_vec") and ollama_disponible():
        try:
            vecteur = embeddings([question])[0]
            mode = "hybride (sémantique + plein texte)"
        except Exception as e:
            avertir(f"Embedding de la question impossible : {e}")

    listes_chunks = [_fts(conn, question, k * 4)]
    listes_noeuds = []
    if vecteur:
        listes_chunks.append(_vec(conn, "chunks_vec", vecteur, k * 4))
        listes_noeuds.append(_vec(conn, "nodes_vec", vecteur, k * 3))
    # Nœuds dont le nom ou un alias contient la question (entrée lexicale)
    motif = f"%{question.strip()[:60]}%"
    directs = conn.execute(
        "SELECT rowid FROM nodes WHERE nom LIKE ? "
        "UNION SELECT n.rowid FROM nodes n JOIN aliases a ON a.node_id = n.node_id "
        "WHERE a.alias LIKE ? LIMIT ?", (motif, motif, k)).fetchall()
    listes_noeuds.append([r["rowid"] for r in directs])

    # --- Passages ------------------------------------------------------------------
    passages = []
    for rowid, score in sorted(_rrf(listes_chunks).items(), key=lambda kv: -kv[1])[:k]:
        r = conn.execute(
            "SELECT c.chunk_id, c.chemin_titres, c.texte, d.titre, d.type_document, "
            "d.date_document, d.chemin_source FROM chunks c "
            "JOIN documents d ON d.doc_id = c.doc_id WHERE c.rowid = ?", (rowid,)).fetchone()
        if not r:
            continue
        lies = conn.execute(
            "SELECT n.node_id, n.type, n.nom FROM mentions m "
            "JOIN nodes n ON n.node_id = m.node_id WHERE m.chunk_id = ? LIMIT 8",
            (r["chunk_id"],)).fetchall()
        passages.append({
            "chunk_id": r["chunk_id"],
            "document": {"titre": r["titre"], "type": r["type_document"],
                         "date": r["date_document"], "source": r["chemin_source"]},
            "section": r["chemin_titres"],
            "extrait": r["texte"][:400] + ("…" if len(r["texte"]) > 400 else ""),
            "noeuds_lies": [dict(x) for x in lies],
            "score": round(score, 4),
        })

    # --- Nœuds ----------------------------------------------------------------------
    noeuds = []
    for rowid, score in sorted(_rrf(listes_noeuds).items(), key=lambda kv: -kv[1])[:k]:
        r = conn.execute("SELECT node_id, type, nom, fiche FROM nodes WHERE rowid = ?",
                         (rowid,)).fetchone()
        if not r or (type_noeud and r["type"] != type_noeud):
            continue
        noeuds.append({"node_id": r["node_id"], "type": r["type"], "nom": r["nom"],
                       "fiche_extrait": (r["fiche"] or "")[:250],
                       "score": round(score, 4)})
    conn.close()
    return {"question": question, "mode": mode, "noeuds": noeuds, "passages": passages,
            "suite": "Approfondissez avec fiche(node_id), voisins(node_id) ou chemin(a, b)."}


# --- Fiche d'identité d'un nœud ------------------------------------------------------------
def _resoudre_node_id(conn, node_id: str):
    r = conn.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
    if r:
        return r
    return conn.execute(
        "SELECT * FROM nodes WHERE nom = ? COLLATE NOCASE "
        "OR node_id IN (SELECT node_id FROM aliases WHERE alias = ? COLLATE NOCASE) "
        "LIMIT 1", (node_id, node_id)).fetchone()


def fiche(node_id: str) -> dict:
    conn = ouvrir_db(lecture_seule=True)
    n = _resoudre_node_id(conn, node_id)
    if not n:
        conn.close()
        return {"erreur": f"Nœud introuvable : {node_id}. "
                          "Utilisez recherche() pour obtenir un identifiant valide."}
    sortants = [dict(r) for r in conn.execute(
        "SELECT e.type relation, x.node_id, x.type, x.nom, e.citation FROM edges e "
        "JOIN nodes x ON x.node_id = e.cible_id WHERE e.source_id = ? LIMIT 50",
        (n["node_id"],))]
    entrants = [dict(r) for r in conn.execute(
        "SELECT e.type relation, x.node_id, x.type, x.nom, e.citation FROM edges e "
        "JOIN nodes x ON x.node_id = e.source_id WHERE e.cible_id = ? LIMIT 50",
        (n["node_id"],))]
    sources = [dict(r) for r in conn.execute(
        "SELECT m.chunk_id, m.citation, d.titre, d.date_document, d.chemin_source "
        "FROM mentions m JOIN chunks c ON c.chunk_id = m.chunk_id "
        "JOIN documents d ON d.doc_id = c.doc_id WHERE m.node_id = ? LIMIT 20",
        (n["node_id"],))]
    alias = [r["alias"] for r in conn.execute(
        "SELECT alias FROM aliases WHERE node_id = ?", (n["node_id"],))]
    conn.close()
    return {"node_id": n["node_id"], "type": n["type"], "nom": n["nom"],
            "alias": alias, "attributs": json.loads(n["attributs"]),
            "fiche": n["fiche"], "liens_sortants": sortants,
            "liens_entrants": entrants, "sources": sources}


# --- Parcours de graphe ------------------------------------------------------------------------
def _aretes_de(conn, ids: set) -> list:
    marqueurs = ",".join("?" * len(ids))
    return conn.execute(
        f"SELECT source_id, type, cible_id FROM edges "
        f"WHERE source_id IN ({marqueurs}) OR cible_id IN ({marqueurs})",
        list(ids) * 2).fetchall()


def voisins(node_id: str, profondeur: int = 1) -> dict:
    profondeur = max(1, min(int(profondeur), 3))
    conn = ouvrir_db(lecture_seule=True)
    n = _resoudre_node_id(conn, node_id)
    if not n:
        conn.close()
        return {"erreur": f"Nœud introuvable : {node_id}"}
    atteints, frontiere, aretes = {n["node_id"]: 0}, {n["node_id"]}, []
    for niveau in range(1, profondeur + 1):
        if not frontiere:
            break
        nouvelles = _aretes_de(conn, frontiere)
        suivante = set()
        for a in nouvelles:
            aretes.append(dict(a))
            for ident in (a["source_id"], a["cible_id"]):
                if ident not in atteints:
                    atteints[ident] = niveau
                    suivante.add(ident)
        frontiere = suivante
    marqueurs = ",".join("?" * len(atteints))
    infos = {r["node_id"]: r for r in conn.execute(
        f"SELECT node_id, type, nom FROM nodes WHERE node_id IN ({marqueurs})",
        list(atteints))}
    conn.close()
    vues, uniques = set(), []
    for a in aretes:
        cle = (a["source_id"], a["type"], a["cible_id"])
        if cle not in vues:
            vues.add(cle)
            uniques.append(a)
    return {
        "centre": {"node_id": n["node_id"], "type": n["type"], "nom": n["nom"]},
        "noeuds": [{"node_id": i, "type": infos[i]["type"], "nom": infos[i]["nom"],
                    "distance": d} for i, d in sorted(atteints.items(), key=lambda kv: kv[1])
                   if i in infos][:80],
        "aretes": uniques[:150],
    }


def chemin(depart: str, arrivee: str, profondeur_max: int = 5) -> dict:
    conn = ouvrir_db(lecture_seule=True)
    a, b = _resoudre_node_id(conn, depart), _resoudre_node_id(conn, arrivee)
    if not a or not b:
        conn.close()
        return {"erreur": f"Nœud introuvable : {depart if not a else arrivee}"}
    parents = {a["node_id"]: None}
    file = deque([(a["node_id"], 0)])
    trouve = False
    while file and not trouve:
        courant, dist = file.popleft()
        if dist >= profondeur_max:
            continue
        for ar in _aretes_de(conn, {courant}):
            for suivant, sens in ((ar["cible_id"], "->"), (ar["source_id"], "<-")):
                if suivant in parents:
                    continue
                parents[suivant] = (courant, ar["type"], sens)
                if suivant == b["node_id"]:
                    trouve = True
                file.append((suivant, dist + 1))
    if b["node_id"] not in parents:
        conn.close()
        return {"depart": a["nom"], "arrivee": b["nom"],
                "chemin": None,
                "message": f"Aucun chemin en {profondeur_max} sauts ou moins."}
    etapes, courant = [], b["node_id"]
    while parents[courant]:
        precedent, type_r, sens = parents[courant]
        etapes.append({"de": precedent, "relation": type_r, "sens": sens, "vers": courant})
        courant = precedent
    etapes.reverse()
    tous = {a["node_id"], b["node_id"]} | {e["de"] for e in etapes} | {e["vers"] for e in etapes}
    marqueurs = ",".join("?" * len(tous))
    infos = {r["node_id"]: f"[{r['type']}] {r['nom']}" for r in conn.execute(
        f"SELECT node_id, type, nom FROM nodes WHERE node_id IN ({marqueurs})", list(tous))}
    conn.close()
    return {"depart": infos[a["node_id"]], "arrivee": infos[b["node_id"]],
            "longueur": len(etapes),
            "chemin": [{"de": infos[e["de"]], "relation": e["relation"],
                        "sens": e["sens"], "vers": infos[e["vers"]],
                        "ids": {"de": e["de"], "vers": e["vers"]}} for e in etapes]}


# --- CLI ------------------------------------------------------------------------------------------
def principal() -> None:
    p = argparse.ArgumentParser(description="Interroger la base de connaissances")
    sous = p.add_subparsers(dest="commande", required=True)
    pr = sous.add_parser("recherche")
    pr.add_argument("question")
    pr.add_argument("--k", type=int, default=8)
    pr.add_argument("--type", default=None)
    pf = sous.add_parser("fiche")
    pf.add_argument("node_id")
    pv = sous.add_parser("voisins")
    pv.add_argument("node_id")
    pv.add_argument("--profondeur", type=int, default=1)
    pc = sous.add_parser("chemin")
    pc.add_argument("depart")
    pc.add_argument("arrivee")
    sous.add_parser("schema")
    args = p.parse_args()
    if args.commande == "recherche":
        resultat = recherche(args.question, args.k, args.type)
    elif args.commande == "fiche":
        resultat = fiche(args.node_id)
    elif args.commande == "voisins":
        resultat = voisins(args.node_id, args.profondeur)
    elif args.commande == "chemin":
        resultat = chemin(args.depart, args.arrivee)
    else:
        resultat = schema()
    print(json.dumps(resultat, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    principal()
