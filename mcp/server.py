"""Serveur MCP de la base de connaissances (transport stdio).

Expose la base à un client MCP via sept outils : schema, recherche, dossier,
fiche, voisins, chemin et requete_sql (lecture seule). Toutes les réponses sont
des structures JSON bornées avec provenance, conçues pour être consommées par une IA.

Lancement : mcp/run_mcp.sh (ou .venv/bin/python mcp/server.py)
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

# Rendre src/ importable quel que soit le répertoire de lancement
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp.server.fastmcp import FastMCP

import query as q
from common import DB_PATH, ouvrir_db

mcp = FastMCP("base-connaissances")


@mcp.tool()
def schema() -> dict:
    """Décrit la base de connaissances : types de nœuds, types de relations,
    volumétrie et métadonnées. À appeler en premier pour s'orienter."""
    return q.schema()


@mcp.tool()
def recherche(question: str, k: int = 8, type_noeud: str | None = None) -> dict:
    """Recherche hybride (sémantique + plein texte) dans tout le corpus.
    Renvoie les nœuds du graphe et les passages de documents les plus pertinents,
    avec provenance. `type_noeud` filtre les nœuds (ex. 'decision', 'test').
    Point d'entrée recommandé pour toute question."""
    return q.recherche(question, k=max(1, min(k, 20)), type_noeud=type_noeud)


@mcp.tool()
def fiche(node_id: str) -> dict:
    """Tout ce que la base sait d'un nœud : fiche de synthèse, attributs, alias,
    liens entrants/sortants typés et passages sources. Accepte un identifiant
    (DEC-xxxxxxxx), un nom exact ou un alias."""
    return q.fiche(node_id)


@mcp.tool()
def voisins(node_id: str, profondeur: int = 1) -> dict:
    """Voisinage d'un nœud dans le graphe (profondeur 1 à 3) : nœuds atteints
    avec leur distance et arêtes typées. Utile pour explorer le contexte
    d'une décision, d'un test ou d'une personne."""
    return q.voisins(node_id, profondeur)


@mcp.tool()
def chemin(depart: str, arrivee: str, profondeur_max: int = 5) -> dict:
    """Plus court chemin entre deux nœuds (identifiants, noms ou alias), avec le
    type et le sens de chaque relation traversée. Répond à « quel est le lien
    entre X et Y ? »."""
    return q.chemin(depart, arrivee, profondeur_max=max(1, min(profondeur_max, 6)))


@mcp.tool()
def dossier(sujet: str, profondeur: int = 2, k: int = 6) -> dict:
    """Dossier COMPLET d'un sujet en un seul appel : le document d'origine (le plus
    ancien qui définit le sujet), TOUS les documents liés directement ET
    indirectement (ex. un PV de comité qui valide une règle), et tous les passages,
    avec la raison de chaque lien (chemin de relations dans le graphe).

    À utiliser dès qu'on demande « tout ce qui concerne X », « tous les documents
    liés à X », « la règle qui ... et où elle a été validée/décidée ». `sujet` peut
    être une question libre, un nom exact ou un identifiant de nœud. `profondeur`
    (1 à 3) règle l'ampleur des liens indirects suivis."""
    return q.dossier(sujet, profondeur=max(1, min(profondeur, 3)), k=max(1, min(k, 12)))


@mcp.tool()
def requete_sql(sql: str, max_lignes: int = 100) -> dict:
    """Exécute une requête SQL en LECTURE SEULE sur la base (SELECT/WITH uniquement).
    Tables : documents, chunks, nodes, edges, mentions, aliases, chunks_fts (FTS5).
    Pour les besoins non couverts par les autres outils (agrégations, filtres fins).
    Appeler schema() d'abord pour connaître les types disponibles."""
    if not re.match(r"^\s*(select|with)\b", sql, re.IGNORECASE):
        return {"erreur": "Seules les requêtes SELECT (ou WITH ... SELECT) sont autorisées."}
    max_lignes = max(1, min(int(max_lignes), 200))
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except Exception:
            pass
        conn.execute("PRAGMA query_only = ON")
        lignes = conn.execute(sql).fetchmany(max_lignes + 1)
        tronque = len(lignes) > max_lignes
        resultat = [dict(l) for l in lignes[:max_lignes]]
        conn.close()
        return {"lignes": resultat, "nombre": len(resultat), "tronque": tronque}
    except sqlite3.Error as e:
        return {"erreur": f"SQL invalide : {e}"}


if __name__ == "__main__":
    if not DB_PATH.exists():
        print(f"[!] Base absente ({DB_PATH}) : exécutez d'abord scripts/run_all.sh",
              file=sys.stderr)
    mcp.run()
