"""Genere de petits artefacts sans LLM pour tester load/query localement."""
import json
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
WORK = RACINE / "data" / "work"


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")


def main():
    write_jsonl(WORK / "manifest.jsonl", [{
        "doc_id": "DOC-smoke",
        "chemin_source": "exemple_compte_rendu.md",
        "titre": "Compte rendu COPIL",
        "type_document": "compte_rendu",
        "date_document": "2026-05-12",
        "sha256": "smoke",
        "nb_chunks": 1,
    }])
    write_jsonl(WORK / "chunks" / "DOC-smoke.jsonl", [{
        "chunk_id": "DOC-smoke#000",
        "ordre": 0,
        "chemin_titres": "Compte rendu COPIL",
        "texte": "La decision de reprendre les donnees fournisseurs via le module Import a ete actee.",
    }])
    write_jsonl(WORK / "canon" / "nodes.jsonl", [
        {"node_id": "DEC-smoke01", "type": "decision", "nom": "Reprise des donnees fournisseurs",
         "attributs": {}},
        {"node_id": "MOD-smoke01", "type": "module", "nom": "Import", "attributs": {}},
    ])
    write_jsonl(WORK / "canon" / "aliases.jsonl", [])
    write_jsonl(WORK / "canon" / "mentions.jsonl", [
        {"node_id": "DEC-smoke01", "chunk_id": "DOC-smoke#000",
         "citation": "reprendre les donnees fournisseurs"},
        {"node_id": "MOD-smoke01", "chunk_id": "DOC-smoke#000", "citation": "module Import"},
    ])
    write_jsonl(WORK / "canon" / "edges.jsonl", [
        {"source_id": "DEC-smoke01", "type": "concerne", "cible_id": "MOD-smoke01",
         "chunk_id": "DOC-smoke#000", "citation": "via le module Import"},
    ])
    write_jsonl(WORK / "enrich" / "fiches.jsonl", [
        {"node_id": "DEC-smoke01", "sha": "smoke",
         "fiche": "**Résumé** Reprise des donnees fournisseurs via le module Import."},
        {"node_id": "MOD-smoke01", "sha": "smoke",
         "fiche": "**Résumé** Module Import concerne par la reprise des donnees fournisseurs."},
    ])


if __name__ == "__main__":
    main()
