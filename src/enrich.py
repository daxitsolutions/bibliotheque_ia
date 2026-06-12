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

from common import (WORK, appel_llm, charger_ontologie, ecrire_jsonl,
                    embeddings, embeddings_disponibles, executer_passe,
                    lire_jsonl, llm_disponible, log, sha_texte, vec_vers_b64)

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


def principal(passe) -> None:
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
            passe.erreur(f"Fiche en échec : {noeud['node_id']}", str(e))
            passe.compter("fiches_en_echec")
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
    passe.compter("fiches_redigees", redigees)
    passe.compter("fiches_en_cache", len(fiches) - redigees)
    log(f"Fiches : {redigees} rédigée(s), {len(fiches) - redigees} en cache")

    # --- 2. Embeddings des chunks (cache par empreinte du texte) -----------------------
    if not embeddings_disponibles():
        passe.avertissement("Embeddings ignorés : Ollama injoignable",
                            "base fonctionnelle en mode plein texte + graphe")
        # Préserver d'éventuels embeddings déjà calculés plutôt que de les écraser à vide.
        if not (DOSSIER / "emb_chunks.jsonl").exists():
            ecrire_jsonl(DOSSIER / "emb_chunks.jsonl", [])
        if not (DOSSIER / "emb_nodes.jsonl").exists():
            ecrire_jsonl(DOSSIER / "emb_nodes.jsonl", [])
        return

    emb_chunks = _vectoriser(
        passe, "chunks", DOSSIER / "emb_chunks.jsonl",
        [(cid, sha_texte(c["texte"]), f"{c['chemin_titres']}\n{c['texte']}")
         for cid, c in chunks_par_id.items()],
        cle="chunk_id")
    emb_noeuds = _vectoriser(
        passe, "nœuds", DOSSIER / "emb_nodes.jsonl",
        [(n["node_id"],
          sha_texte(f"{n['type']} : {n['nom']}\n{fiches_par_noeud.get(n['node_id'], '')[:1200]}"),
          f"{n['type']} : {n['nom']}\n{fiches_par_noeud.get(n['node_id'], '')[:1200]}")
         for n in noeuds],
        cle="node_id")
    passe.compter("chunks_vectorises", len(emb_chunks))
    passe.compter("noeuds_vectorises", len(emb_noeuds))
    log(f"Enrichissement terminé : {len(emb_chunks)} chunks et {len(emb_noeuds)} nœuds vectorisés")


def _vectoriser(passe, libelle, chemin_sortie, items, cle):
    """Calcule les embeddings par lots, en réutilisant le cache et en isolant chaque lot.

    Un lot qui échoue (timeout, surcharge) n'interrompt plus toute la passe : les
    éléments du lot restent à recalculer au prochain run, les autres sont conservés.
    """
    cache = {e[cle]: e for e in lire_jsonl(chemin_sortie)}
    resultats, a_calculer = [], []
    for identifiant, empreinte, texte in items:
        en_cache = cache.get(identifiant)
        if en_cache and en_cache.get("sha") == empreinte:
            resultats.append(en_cache)
        else:
            a_calculer.append((identifiant, empreinte, texte))

    for debut in range(0, len(a_calculer), 64):
        lot = a_calculer[debut:debut + 64]
        try:
            vecteurs = embeddings([t for _, _, t in lot])
        except Exception as e:
            passe.erreur(f"Lot d'embeddings {libelle} en échec", str(e))
            passe.compter(f"lots_embeddings_{libelle}_en_echec")
            continue
        resultats.extend(
            {cle: ident, "sha": emp, "vec": vec_vers_b64(v)}
            for (ident, emp, _), v in zip(lot, vecteurs)
        )
        log(f"  embeddings {libelle} : {min(debut + 64, len(a_calculer))}/{len(a_calculer)} nouveaux")
        # Sauvegarde de progression : un crash ultérieur ne reperd pas les lots déjà calculés.
        ecrire_jsonl(chemin_sortie, resultats)
    ecrire_jsonl(chemin_sortie, resultats)
    return resultats


if __name__ == "__main__":
    executer_passe("40_enrich", principal)
