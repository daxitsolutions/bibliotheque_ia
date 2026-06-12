"""Couche de requête de la base de connaissances.

Fonctions partagées par la CLI (scripts/90_query.sh) et le serveur MCP :
  - schema()                     : ontologie + volumétrie (auto-description)
  - recherche(question, ...)     : hybride vecteurs + plein texte, fusion RRF
  - dossier(sujet, ...)          : tous les documents/passages liés à un sujet
                                   (directs + indirects), avec document d'origine
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

from common import (avertir, charger_ontologie, embeddings,
                    embeddings_disponibles, ouvrir_db, table_existe,
                    vec_vers_blob)

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
    if table_existe(conn, "chunks_vec") and embeddings_disponibles():
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
            "suite": "Approfondissez avec fiche(node_id), voisins(node_id) ou chemin(a, b). "
                     "Pour TOUS les documents liés à un sujet, utilisez dossier(sujet)."}


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


# --- Dossier complet : tout ce qui est lié à un sujet -------------------------------------
# Plafonds : exhaustif mais borné. Toute troncature est signalée dans "limites".
DOSSIER_MAX_NOEUDS = 600        # taille max du sous-graphe exploré
DOSSIER_MAX_DOCUMENTS = 200     # documents restitués
DOSSIER_MAX_PASSAGES_DOC = 15   # passages détaillés par document (le total reste compté)


def _amorces(conn, sujet: str, k: int) -> tuple[dict, list]:
    """Nœuds-graines du sujet (résolution directe + recherche hybride) et passages directs."""
    seeds = {}  # node_id -> score
    direct = _resoudre_node_id(conn, sujet)
    if direct:
        seeds[direct["node_id"]] = 1.0

    vecteur = None
    if table_existe(conn, "chunks_vec") and embeddings_disponibles():
        try:
            vecteur = embeddings([sujet])[0]
        except Exception as e:
            avertir(f"Embedding du sujet impossible : {e}")

    listes_noeuds, listes_chunks = [], [_fts(conn, sujet, k * 6)]
    if vecteur:
        listes_chunks.append(_vec(conn, "chunks_vec", vecteur, k * 6))
        listes_noeuds.append(_vec(conn, "nodes_vec", vecteur, k * 4))
    motif = f"%{sujet.strip()[:60]}%"
    lexicaux = conn.execute(
        "SELECT rowid FROM nodes WHERE nom LIKE ? "
        "UNION SELECT n.rowid FROM nodes n JOIN aliases a ON a.node_id = n.node_id "
        "WHERE a.alias LIKE ? LIMIT ?", (motif, motif, k)).fetchall()
    listes_noeuds.append([r["rowid"] for r in lexicaux])

    for rowid, score in sorted(_rrf(listes_noeuds).items(), key=lambda kv: -kv[1])[:k]:
        r = conn.execute("SELECT node_id FROM nodes WHERE rowid = ?", (rowid,)).fetchone()
        if r:
            seeds.setdefault(r["node_id"], round(score, 4))

    chunks_directs = []
    for rowid, _ in sorted(_rrf(listes_chunks).items(), key=lambda kv: -kv[1])[:k * 2]:
        r = conn.execute("SELECT chunk_id FROM chunks WHERE rowid = ?", (rowid,)).fetchone()
        if r:
            chunks_directs.append(r["chunk_id"])
    return seeds, chunks_directs


def _expansion(conn, seeds: set, profondeur: int) -> tuple[dict, dict, bool]:
    """BFS depuis les graines. Renvoie distances, parents (pour expliquer les liens) et troncature."""
    distance = {s: 0 for s in seeds}
    parent = {s: None for s in seeds}
    frontiere = set(seeds)
    tronque = False
    for niveau in range(1, profondeur + 1):
        if not frontiere or len(distance) >= DOSSIER_MAX_NOEUDS:
            break
        suivante = set()
        for a in _aretes_de(conn, frontiere):
            for courant, voisin, sens in (
                (a["source_id"], a["cible_id"], "->"),
                (a["cible_id"], a["source_id"], "<-")):
                if courant in frontiere and voisin not in distance:
                    if len(distance) >= DOSSIER_MAX_NOEUDS:
                        tronque = True
                        break
                    distance[voisin] = niveau
                    parent[voisin] = (courant, a["type"], sens)
                    suivante.add(voisin)
        frontiere = suivante
    return distance, parent, tronque


def _chemin_lien(parent, noms, node_id) -> list:
    """Remonte la chaîne de relations d'une graine jusqu'à node_id (pourquoi c'est lié)."""
    etapes, courant = [], node_id
    garde = 0
    while parent.get(courant) and garde < 12:
        prec, rel, sens = parent[courant]
        etapes.append({"de": noms.get(prec, prec), "relation": rel,
                       "sens": sens, "vers": noms.get(courant, courant)})
        courant = prec
        garde += 1
    etapes.reverse()
    return etapes


def dossier(sujet: str, profondeur: int = 2, k: int = 6) -> dict:
    """Dossier complet d'un sujet : document d'origine, TOUS les documents liés
    (directs et indirects) et tous les passages, avec la raison de chaque lien.

    `sujet` peut être une question libre (« règle qui convertit un chiffre en
    texte »), un nom exact ou un identifiant de nœud.
    """
    profondeur = max(1, min(int(profondeur), 3))
    conn = ouvrir_db(lecture_seule=True)
    limites = []

    seeds, chunks_directs = _amorces(conn, sujet, k)
    if not seeds and not chunks_directs:
        conn.close()
        return {"sujet": sujet, "amorces": [], "documents": [],
                "message": "Aucun nœud ni passage trouvé pour ce sujet. "
                           "Reformulez ou utilisez recherche()."}

    distance, parent, tronque_graphe = _expansion(conn, set(seeds), profondeur)
    if tronque_graphe:
        limites.append(f"Sous-graphe borné à {DOSSIER_MAX_NOEUDS} nœuds : "
                       "réduisez la profondeur pour un périmètre plus précis.")

    # Noms des nœuds atteints (affichage + explication des liens)
    ids = list(distance)
    noms = {}
    for debut in range(0, len(ids), 400):
        lot = ids[debut:debut + 400]
        marqueurs = ",".join("?" * len(lot))
        for r in conn.execute(
                f"SELECT node_id, type, nom FROM nodes WHERE node_id IN ({marqueurs})", lot):
            noms[r["node_id"]] = f"[{r['type']}] {r['nom']}"

    # Tous les passages mentionnant un nœud du sous-graphe, + les passages directs du texte
    docs = {}  # doc_id -> agrégat
    passages_total = 0

    def ajouter_passage(chunk_id, node_id, dist):
        nonlocal passages_total
        r = conn.execute(
            "SELECT c.chunk_id, c.chemin_titres, c.texte, d.doc_id, d.titre, "
            "d.type_document, d.date_document, d.chemin_source FROM chunks c "
            "JOIN documents d ON d.doc_id = c.doc_id WHERE c.chunk_id = ?", (chunk_id,)).fetchone()
        if not r:
            return
        doc = docs.setdefault(r["doc_id"], {
            "doc_id": r["doc_id"], "titre": r["titre"], "type": r["type_document"],
            "date": r["date_document"], "source": r["chemin_source"],
            "distance": dist, "passages": [], "_chunks": set(), "_noeuds": set()})
        doc["distance"] = min(doc["distance"], dist)
        if node_id:
            doc["_noeuds"].add(node_id)
        if r["chunk_id"] not in doc["_chunks"]:
            doc["_chunks"].add(r["chunk_id"])
            passages_total += 1
            doc["passages"].append({
                "chunk_id": r["chunk_id"], "section": r["chemin_titres"],
                "extrait": r["texte"][:500] + ("…" if len(r["texte"]) > 500 else "")})

    for debut in range(0, len(ids), 400):
        lot = ids[debut:debut + 400]
        marqueurs = ",".join("?" * len(lot))
        for m in conn.execute(
                f"SELECT node_id, chunk_id FROM mentions WHERE node_id IN ({marqueurs})", lot):
            ajouter_passage(m["chunk_id"], m["node_id"], distance[m["node_id"]])
    for chunk_id in chunks_directs:
        ajouter_passage(chunk_id, None, 0)

    # Document d'origine : le plus ancien parmi ceux qui mentionnent une graine
    dates = [(d["date"], d) for d in docs.values()
             if d["distance"] == 0 and d["date"]]
    origine = min(dates, key=lambda kv: kv[0])[1] if dates else None
    if not origine and any(d["distance"] == 0 for d in docs.values()):
        limites.append("Aucune date détectée sur les documents définissant le sujet : "
                       "document d'origine indéterminé (voir la chronologie).")

    # Mise en forme des documents : rôle + raison du lien (chemin de relations)
    sortie_docs = []
    for d in sorted(docs.values(), key=lambda x: (x["distance"], x["date"] or "9999")):
        noeud_pivot = min(d["_noeuds"], key=lambda n: distance.get(n, 9),
                          default=None) if d["_noeuds"] else None
        role = ("définit / mentionne directement le sujet" if d["distance"] == 0
                else f"lié indirectement (distance {d['distance']})")
        entree = {
            "doc_id": d["doc_id"], "titre": d["titre"], "type": d["type"],
            "date": d["date"], "source": d["source"], "distance": d["distance"],
            "role": role,
            "nb_passages": len(d["passages"]),
            "passages": d["passages"][:DOSSIER_MAX_PASSAGES_DOC],
        }
        if d["distance"] > 0 and noeud_pivot:
            entree["lien"] = {
                "via_noeud": noms.get(noeud_pivot, noeud_pivot),
                "chemin": _chemin_lien(parent, noms, noeud_pivot),
            }
        if len(d["passages"]) > DOSSIER_MAX_PASSAGES_DOC:
            entree["passages_tronques"] = len(d["passages"]) - DOSSIER_MAX_PASSAGES_DOC
        sortie_docs.append(entree)

    if len(sortie_docs) > DOSSIER_MAX_DOCUMENTS:
        limites.append(f"{len(sortie_docs)} documents trouvés, "
                       f"{DOSSIER_MAX_DOCUMENTS} restitués (les plus proches du sujet).")
        sortie_docs = sortie_docs[:DOSSIER_MAX_DOCUMENTS]

    amorces = [{"node_id": s, "libelle": noms.get(s, s), "score": sc}
               for s, sc in sorted(seeds.items(), key=lambda kv: -kv[1])]
    chronologie = [{"date": d["date"], "titre": d["titre"], "doc_id": d["doc_id"],
                    "role": ("origine" if origine and d["doc_id"] == origine["doc_id"]
                             else ("définit" if d["distance"] == 0 else "lié"))}
                   for d in sorted(docs.values(), key=lambda x: (x["date"] or "9999"))
                   if d["date"]]
    conn.close()

    return {
        "sujet": sujet,
        "profondeur": profondeur,
        "amorces": amorces,
        "document_origine": ({"doc_id": origine["doc_id"], "titre": origine["titre"],
                              "date": origine["date"], "source": origine["source"],
                              "pourquoi": "document daté le plus ancien définissant le sujet"}
                             if origine else None),
        "chronologie": chronologie,
        "couverture": {"documents": len(sortie_docs), "passages": passages_total,
                       "noeuds_du_sujet": len(distance), "profondeur": profondeur},
        "documents": sortie_docs,
        "limites": limites or ["Aucune (résultat complet dans les bornes par défaut)."],
        "suite": "Détaillez un nœud avec fiche(node_id) ou un lien avec chemin(a, b).",
    }


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
    pd = sous.add_parser("dossier")
    pd.add_argument("sujet")
    pd.add_argument("--profondeur", type=int, default=2)
    pd.add_argument("--k", type=int, default=6)
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
    elif args.commande == "dossier":
        resultat = dossier(args.sujet, args.profondeur, args.k)
    else:
        resultat = schema()
    print(json.dumps(resultat, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    principal()
