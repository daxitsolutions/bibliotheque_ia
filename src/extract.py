"""Passe 20 - Extraction : entites et relations depuis les chunks.

Lit les chunks normalises, construit un prompt a partir de l'ontologie, puis
demande au LLM d'extraire des entites et relations typpees avec provenance.
Les resultats sont caches par empreinte du chunk.
"""
import json

from common import (WORK, appel_llm, avertir, charger_ontologie, ecrire_jsonl,
                    lire_jsonl, log, ollama_disponible, sha_texte)

DOSSIER = WORK / "extract"
MAX_CHARS_PROMPT = 4500


def prompt_systeme(onto: dict) -> str:
    noeuds = "\n".join(
        f"- {nom}: {spec['description']}" for nom, spec in onto["noeuds"].items()
    )
    relations = "\n".join(
        f"- {nom}: {spec['description']} (sources={spec['sources']}, cibles={spec['cibles']})"
        for nom, spec in onto["relations"].items()
    )
    return (
        "Tu extrais une base de connaissances projet depuis un passage de document. "
        "Respecte strictement l'ontologie. N'invente rien. Ignore les entites "
        "trop vagues sans nom exploitable. Les citations doivent etre de courts "
        "extraits exacts du passage.\n\n"
        "Types d'entites autorises:\n"
        f"{noeuds}\n\n"
        "Types de relations autorises:\n"
        f"{relations}\n\n"
        "Reponds uniquement en JSON avec cette forme:\n"
        '{"entites":[{"type":"decision","nom":"...","attributs":{},'
        '"citation":"..."}],"relations":[{"type":"concerne",'
        '"source_type":"decision","source_nom":"...","cible_type":"module",'
        '"cible_nom":"...","citation":"..."}]}'
    )


def nettoyer(donnees: dict, chunk_id: str, onto: dict) -> dict:
    entites, relations = [], []
    types_noeuds = set(onto["noeuds"])
    types_relations = set(onto["relations"])
    for e in donnees.get("entites", []) or []:
        type_e = str(e.get("type", "")).strip()
        nom = str(e.get("nom", "")).strip()
        if type_e not in types_noeuds or not nom:
            continue
        entites.append({
            "type": type_e,
            "nom": nom,
            "attributs": e.get("attributs") if isinstance(e.get("attributs"), dict) else {},
            "chunk_id": chunk_id,
            "citation": str(e.get("citation", "")).strip()[:500],
        })
    for r in donnees.get("relations", []) or []:
        type_r = str(r.get("type", "")).strip()
        source_type = str(r.get("source_type", "")).strip()
        cible_type = str(r.get("cible_type", "")).strip()
        source_nom = str(r.get("source_nom", "")).strip()
        cible_nom = str(r.get("cible_nom", "")).strip()
        if (type_r not in types_relations or source_type not in types_noeuds or
                cible_type not in types_noeuds or not source_nom or not cible_nom):
            continue
        relations.append({
            "type": type_r,
            "source_type": source_type,
            "source_nom": source_nom,
            "cible_type": cible_type,
            "cible_nom": cible_nom,
            "chunk_id": chunk_id,
            "citation": str(r.get("citation", "")).strip()[:500],
        })
    return {"chunk_id": chunk_id, "entites": entites, "relations": relations}


def extraire_chunk(chunk: dict, systeme: str, onto: dict) -> dict:
    texte = chunk["texte"][:MAX_CHARS_PROMPT]
    message = (
        f"chunk_id: {chunk['chunk_id']}\n"
        f"section: {chunk.get('chemin_titres') or ''}\n\n"
        f"{texte}"
    )
    brut = appel_llm([
        {"role": "system", "content": systeme},
        {"role": "user", "content": message},
    ])
    return nettoyer(brut, chunk["chunk_id"], onto)


def principal() -> None:
    if not ollama_disponible():
        raise SystemExit("Ollama injoignable : impossible d'extraire les entites.")
    onto = charger_ontologie()
    systeme = prompt_systeme(onto)
    manifest = lire_jsonl(WORK / "manifest.jsonl")
    if not manifest:
        raise SystemExit("Manifest absent : lancez d'abord scripts/10_normalize.sh")
    DOSSIER.mkdir(parents=True, exist_ok=True)
    total_chunks, nouveaux = 0, 0

    for doc in manifest:
        chunks = lire_jsonl(WORK / "chunks" / f"{doc['doc_id']}.jsonl")
        sortie = DOSSIER / f"{doc['doc_id']}.json"
        cache = json.loads(sortie.read_text(encoding="utf-8")) if sortie.exists() else {}
        resultats = {r["chunk_id"]: r for r in cache.get("chunks", [])}
        chunks_sortie = []

        for chunk in chunks:
            total_chunks += 1
            empreinte = sha_texte(chunk["texte"])
            en_cache = resultats.get(chunk["chunk_id"])
            if en_cache and en_cache.get("sha") == empreinte:
                chunks_sortie.append(en_cache)
                continue
            try:
                extrait = extraire_chunk(chunk, systeme, onto)
            except Exception as e:
                avertir(f"Extraction en echec pour {chunk['chunk_id']} ({e})")
                extrait = {"chunk_id": chunk["chunk_id"], "entites": [], "relations": []}
            extrait["sha"] = empreinte
            chunks_sortie.append(extrait)
            nouveaux += 1

        entites = [e for r in chunks_sortie for e in r["entites"]]
        relations = [r for bloc in chunks_sortie for r in bloc["relations"]]
        sortie.write_text(json.dumps({
            "doc_id": doc["doc_id"],
            "chunks": chunks_sortie,
            "entites": entites,
            "relations": relations,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"  [OK] {doc['doc_id']} : {len(entites)} entite(s), {len(relations)} relation(s)")

    log(f"Extraction terminee : {nouveaux} chunk(s) traite(s), {total_chunks - nouveaux} en cache")


if __name__ == "__main__":
    principal()
