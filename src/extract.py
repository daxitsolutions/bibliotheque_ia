"""Passe 20 - Extraction : entites et relations depuis les chunks.

Lit les chunks normalises, construit un prompt a partir de l'ontologie, puis
demande au LLM d'extraire des entites et relations typpees avec provenance.
Les resultats sont caches par empreinte du chunk.
"""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from common import (CONCURRENCE, MODELE_EXTRACTION, WORK, appel_llm,
                    charger_ontologie, executer_passe, lire_jsonl,
                    llm_disponible, log, sha_texte)

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
    if not isinstance(donnees, dict):
        donnees = {}
    for e in donnees.get("entites", []) or []:
        if not isinstance(e, dict):  # le modèle renvoie parfois des chaînes
            continue
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
        if not isinstance(r, dict):
            continue
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


CHECKPOINT_TOUS_LES = 50  # chunks : flush du cache document en cours de route


def _charger_cache(sortie) -> dict:
    """Cache d'extraction d'un document, tolérant aux fichiers corrompus."""
    if not sortie.exists():
        return {}
    try:
        return json.loads(sortie.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, OSError):
        return {}


def _ecrire_doc(sortie, doc_id, chunks_sortie) -> None:
    entites = [e for r in chunks_sortie for e in r["entites"]]
    relations = [r for bloc in chunks_sortie for r in bloc["relations"]]
    sortie.write_text(json.dumps({
        "doc_id": doc_id,
        "chunks": chunks_sortie,
        "entites": entites,
        "relations": relations,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(entites), len(relations)


def principal(passe) -> None:
    if not llm_disponible():
        raise SystemExit("LLM injoignable : impossible d'extraire les entites.")
    onto = charger_ontologie()
    systeme = prompt_systeme(onto)
    manifest = lire_jsonl(WORK / "manifest.jsonl")
    if not manifest:
        raise SystemExit("Manifest absent : lancez d'abord scripts/10_normalize.sh")
    DOSSIER.mkdir(parents=True, exist_ok=True)
    # Empreinte de la logique d'extraction : contenu + prompt (qui encode
    # l'ontologie) + modèle. Modifier le prompt, l'ontologie ou le modèle
    # invalide automatiquement le cache et ré-extrait ce qui est concerné.
    version_logique = sha_texte(systeme + "\x00" + MODELE_EXTRACTION)

    for doc in manifest:
        chunks = lire_jsonl(WORK / "chunks" / f"{doc['doc_id']}.jsonl")
        sortie = DOSSIER / f"{doc['doc_id']}.json"
        cache = _charger_cache(sortie)
        resultats = {r["chunk_id"]: r for r in cache.get("chunks", [])}

        # 1) Séparer les chunks déjà en cache (aucun appel LLM) de ceux à
        #    (ré)extraire. On mémorise l'empreinte pour ne cacher qu'en succès.
        par_id = {}  # chunk_id -> extrait, reconstruit dans l'ordre d'origine ensuite
        a_extraire = []
        for chunk in chunks:
            empreinte = sha_texte(chunk["texte"] + "\x00" + version_logique)
            en_cache = resultats.get(chunk["chunk_id"])
            if en_cache and en_cache.get("sha") == empreinte:
                par_id[chunk["chunk_id"]] = en_cache
                passe.compter("chunks_caches")
            else:
                a_extraire.append((chunk, empreinte))

        # 2) Extraire les chunks restants, jusqu'à CONCURRENCE appels LLM de
        #    front. Seul l'appel LLM est parallèle : la consommation des
        #    résultats (compteurs, journal, écriture) reste dans ce thread, donc
        #    sans verrou. Un échec reste non caché → réessayé au prochain run.
        def _extraire(item):
            chunk, empreinte = item
            extrait = extraire_chunk(chunk, systeme, onto)
            extrait["sha"] = empreinte  # caché UNIQUEMENT en cas de succès
            return chunk["chunk_id"], extrait

        depuis_flush = 0
        with ThreadPoolExecutor(max_workers=CONCURRENCE) as executor:
            futurs = {executor.submit(_extraire, it): it for it in a_extraire}
            for futur in as_completed(futurs):
                cid = futurs[futur][0]["chunk_id"]
                try:
                    _, extrait = futur.result()
                    passe.compter("chunks_extraits")
                except Exception as e:
                    passe.erreur(f"Extraction en échec : {cid}", str(e))
                    extrait = {"chunk_id": cid, "entites": [],
                               "relations": [], "echec": True}
                    passe.compter("chunks_en_echec")
                par_id[cid] = extrait
                depuis_flush += 1
                # Checkpoint : un kill au milieu d'un gros document ne reperd pas
                # les appels LLM déjà effectués. On écrit dans l'ordre d'origine.
                if depuis_flush >= CHECKPOINT_TOUS_LES:
                    partiel = [par_id[c["chunk_id"]] for c in chunks
                               if c["chunk_id"] in par_id]
                    _ecrire_doc(sortie, doc["doc_id"], partiel)
                    depuis_flush = 0

        # 3) Reconstruire la sortie dans l'ordre des chunks d'origine.
        chunks_sortie = [par_id[c["chunk_id"]] for c in chunks
                         if c["chunk_id"] in par_id]

        try:
            nb_entites, nb_relations = _ecrire_doc(sortie, doc["doc_id"], chunks_sortie)
        except OSError as e:
            passe.erreur(f"Écriture extraction impossible : {doc['doc_id']}", str(e))
            continue
        passe.compter("documents")
        passe.compter("entites", nb_entites)
        passe.compter("relations", nb_relations)
        log(f"  [OK] {doc['doc_id']} : {nb_entites} entite(s), {nb_relations} relation(s)")

    log(f"Extraction terminée : {passe.compteurs['chunks_extraits']} chunk(s) traité(s), "
        f"{passe.compteurs['chunks_caches']} en cache, "
        f"{passe.compteurs['chunks_en_echec']} en échec")


if __name__ == "__main__":
    executer_passe("20_extract", principal)
