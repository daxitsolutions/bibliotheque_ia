"""Passe 40 — Enrichissement : fiches de synthèse et embeddings.

  1. Pour chaque nœud (types `fiche: true` de l'ontologie), rédige via le LLM une
     fiche Markdown courte à partir de ses citations et des chunks où il apparaît.
     C'est la dénormalisation pour la lecture : une requête `fiche(node_id)` suffit
     ensuite à l'IA, sans relire douze documents.
  2. Calcule les embeddings de tous les chunks et de tous les nœuds (nom + fiche).

Tout est mis en cache par empreinte de contenu : seuls les éléments nouveaux ou
modifiés provoquent des appels au modèle.

Sorties : work/enrich/fiches.jsonl, emb_chunks.jsonl, emb_nodes.jsonl
"""
from collections import defaultdict

from common import (WORK, appel_llm, avertir, charger_ontologie, ecrire_jsonl,
                    embeddings, embeddings_disponibles, lire_jsonl,
                    llm_disponible, log, sha_texte, vec_vers_b64)

DOSSIER = WORK / "enrich"
MAX_CONTEXTE = 6000   # caractères de contexte fournis au LLM par fiche
MAX_FICHE_NOEUDS = 0  # 0 = pas de limite


def contexte_du_noeud(noeud, mentions, chunks_par_id):
    """Assemble citations + extraits de chunks (bornés) pour rédiger la fiche."""
    morceaux = [f"{noeud['type'].upper()} : {noeud['nom']}"]
    citations = [m["citation"] for m in mentions if m["citation"]]
    if citations:
        morceaux.append("Citations : " + " | ".join(dict.fromkeys(citations))[:1500])
    compte = defaultdict(int)
    for m in mentions:
        compte[m["chunk_id"]] += 1
    taille = sum(len(x) for x in morceaux)
    for chunk_id, _ in sorted(compte.items(), key=lambda kv: -kv[1]):
        chunk = chunks_par_id.get(chunk_id)
        if not chunk:
            continue
        extrait = f"[{chunk_id} | {chunk['chemin_titres']}]\n{chunk['texte'][:1800]}"
        if taille + len(extrait) > MAX_CONTEXTE:
            break
        morceaux.append(extrait)
        taille += len(extrait)
    return "\n\n".join(morceaux)


def rediger_fiche(contexte: str) -> str:
    prompt = (
        "Rédige une fiche de synthèse en Markdown (150 mots maximum) pour l'entité "
        "décrite par les extraits ci-dessous. Structure : une ligne **Résumé**, puis "
        "**Contexte** (2-3 phrases), puis **Points clés** (3 puces maximum). "
        "Ne mentionne que ce qui figure dans les extraits, n'invente rien, "
        "n'ajoute aucun préambule.\n\n" + contexte
    )
    return appel_llm([{"role": "user", "content": prompt}], json_attendu=False)


def principal() -> None:
    if not llm_disponible():
        raise SystemExit("LLM injoignable : impossible de calculer les fiches.")
    onto = charger_ontologie()
    noeuds = lire_jsonl(WORK / "canon" / "nodes.jsonl")
    mentions = lire_jsonl(WORK / "canon" / "mentions.jsonl")
    if not noeuds:
        raise SystemExit("Aucun nœud canonisé : lancez d'abord scripts/30_canonize.sh")
    DOSSIER.mkdir(parents=True, exist_ok=True)

    chunks_par_id = {}
    for fichier in (WORK / "chunks").glob("DOC-*.jsonl"):
        for chunk in lire_jsonl(fichier):
            chunks_par_id[chunk["chunk_id"]] = chunk
    mentions_par_noeud = defaultdict(list)
    for m in mentions:
        mentions_par_noeud[m["node_id"]].append(m)

    # --- 1. Fiches de synthèse (cache par empreinte du contexte) ----------------------
    cache_fiches = {f["node_id"]: f for f in lire_jsonl(DOSSIER / "fiches.jsonl")}
    fiches, redigees = [], 0
    candidats = [n for n in noeuds if onto["noeuds"][n["type"]].get("fiche")]
    for i, noeud in enumerate(candidats, 1):
        contexte = contexte_du_noeud(noeud, mentions_par_noeud.get(noeud["node_id"], []),
                                     chunks_par_id)
        empreinte = sha_texte(contexte)
        en_cache = cache_fiches.get(noeud["node_id"])
        if en_cache and en_cache.get("sha") == empreinte:
            fiches.append(en_cache)
            continue
        try:
            fiche = rediger_fiche(contexte)
        except Exception as e:
            avertir(f"Fiche en échec pour {noeud['node_id']} ({e})")
            continue
        fiches.append({"node_id": noeud["node_id"], "sha": empreinte, "fiche": fiche})
        redigees += 1
        if redigees % 25 == 0:
            log(f"  fiches : {redigees} rédigées ({i}/{len(candidats)} nœuds parcourus)")
            ecrire_jsonl(DOSSIER / "fiches.jsonl", fiches + [
                v for k, v in cache_fiches.items()
                if k not in {f['node_id'] for f in fiches}
            ])
    ecrire_jsonl(DOSSIER / "fiches.jsonl", fiches)
    fiches_par_noeud = {f["node_id"]: f["fiche"] for f in fiches}
    log(f"Fiches : {redigees} rédigée(s), {len(fiches) - redigees} en cache")

    # --- 2. Embeddings des chunks (cache par empreinte du texte) -----------------------
    if not embeddings_disponibles():
        avertir("Ollama embeddings injoignable : fiches produites, embeddings ignores.")
        ecrire_jsonl(DOSSIER / "emb_chunks.jsonl", [])
        ecrire_jsonl(DOSSIER / "emb_nodes.jsonl", [])
        return

    cache_chunks = {e["chunk_id"]: e for e in lire_jsonl(DOSSIER / "emb_chunks.jsonl")}
    emb_chunks, a_calculer = [], []
    for chunk_id, chunk in chunks_par_id.items():
        empreinte = sha_texte(chunk["texte"])
        en_cache = cache_chunks.get(chunk_id)
        if en_cache and en_cache.get("sha") == empreinte:
            emb_chunks.append(en_cache)
        else:
            a_calculer.append((chunk_id, empreinte, chunk))
    for debut in range(0, len(a_calculer), 64):
        lot = a_calculer[debut:debut + 64]
        vecteurs = embeddings([f"{c['chemin_titres']}\n{c['texte']}" for _, _, c in lot])
        emb_chunks.extend(
            {"chunk_id": cid, "sha": emp, "vec": vec_vers_b64(v)}
            for (cid, emp, _), v in zip(lot, vecteurs)
        )
        log(f"  embeddings chunks : {min(debut + 64, len(a_calculer))}/{len(a_calculer)} nouveaux")
    ecrire_jsonl(DOSSIER / "emb_chunks.jsonl", emb_chunks)

    # --- 3. Embeddings des nœuds (nom + type + fiche) -----------------------------------
    cache_noeuds = {e["node_id"]: e for e in lire_jsonl(DOSSIER / "emb_nodes.jsonl")}
    emb_noeuds, a_calculer = [], []
    for noeud in noeuds:
        texte = f"{noeud['type']} : {noeud['nom']}\n{fiches_par_noeud.get(noeud['node_id'], '')[:1200]}"
        empreinte = sha_texte(texte)
        en_cache = cache_noeuds.get(noeud["node_id"])
        if en_cache and en_cache.get("sha") == empreinte:
            emb_noeuds.append(en_cache)
        else:
            a_calculer.append((noeud["node_id"], empreinte, texte))
    for debut in range(0, len(a_calculer), 64):
        lot = a_calculer[debut:debut + 64]
        vecteurs = embeddings([t for _, _, t in lot])
        emb_noeuds.extend(
            {"node_id": nid, "sha": emp, "vec": vec_vers_b64(v)}
            for (nid, emp, _), v in zip(lot, vecteurs)
        )
        log(f"  embeddings nœuds : {min(debut + 64, len(a_calculer))}/{len(a_calculer)} nouveaux")
    ecrire_jsonl(DOSSIER / "emb_nodes.jsonl", emb_noeuds)
    log(f"Enrichissement terminé : {len(emb_chunks)} chunks et {len(emb_noeuds)} nœuds vectorisés")


if __name__ == "__main__":
    principal()
