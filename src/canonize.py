"""Passe 30 — Canonisation : résolution d'entités sur l'ensemble du corpus.

Fusionne les variantes d'une même entité ("H. Dupont", "Hélène Dupont", "HD") :
  1. équivalences forcées déclarées dans l'ontologie,
  2. correspondance exacte des noms normalisés,
  3. similarité floue (rapidfuzz) : >= KB_SEUIL_FUSION fusion automatique,
     entre KB_SEUIL_ARBITRAGE et KB_SEUIL_FUSION arbitrage par le LLM.
Résout ensuite les relations vers des identifiants stables et applique les
contraintes de types de l'ontologie (violation -> rétrogradée en `liee_a`).

Sorties : work/canon/{nodes,aliases,mentions,edges}.jsonl + journal.log
"""
import json
from collections import Counter, defaultdict
from pathlib import Path

from rapidfuzz import fuzz, process

from common import (ARBITRAGE_LLM, SEUIL_ARBITRAGE, SEUIL_FUSION, WORK,
                    appel_llm, charger_ontologie, ecrire_jsonl, ecrire_texte,
                    executer_passe, llm_disponible, log, node_id, normaliser)

DOSSIER = WORK / "canon"


class UnionFind:
    def __init__(self):
        self.parent = {}

    def trouver(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def unir(self, a, b):
        ra, rb = self.trouver(a), self.trouver(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def arbitrer_llm(paires: list, journal: list) -> list:
    """Demande au LLM, par lots, si chaque paire désigne la même entité."""
    if not paires:
        return []
    if not (ARBITRAGE_LLM and llm_disponible()):
        journal.append(f"Arbitrage LLM désactivé/indisponible : {len(paires)} paires laissées séparées")
        return [False] * len(paires)
    verdicts = []
    for debut in range(0, len(paires), 25):
        lot = paires[debut:debut + 25]
        enonce = "\n".join(
            f"{i+1}. [{t}] «{a}»  <->  «{b}»" for i, (t, a, b) in enumerate(lot)
        )
        prompt = (
            "Dans une base de connaissances projet, indique pour chaque paire si les deux "
            "libellés désignent la MÊME entité (même personne, même module, même décision...). "
            "En cas de doute, réponds false. Réponds UNIQUEMENT en JSON : "
            f'{{"fusions": [true|false, ...]}} avec exactement {len(lot)} valeurs.\n\n{enonce}'
        )
        try:
            reponse = appel_llm([{"role": "user", "content": prompt}])
            fusions = list(reponse.get("fusions", []))
        except Exception as e:
            journal.append(f"Arbitrage LLM en échec ({e}) : lot laissé séparé")
            fusions = []
        fusions = [bool(v) for v in fusions][:len(lot)]
        fusions += [False] * (len(lot) - len(fusions))
        verdicts.extend(fusions)
    return verdicts


def principal(passe) -> None:
    onto = charger_ontologie()
    titres = tuple(onto.get("canonisation", {}).get("titres_a_retirer", []))
    extraits = sorted((WORK / "extract").glob("DOC-*.json"))
    if not extraits:
        raise SystemExit("Aucune extraction : lancez d'abord scripts/20_extract.sh")
    journal: list = []

    # --- 1. Regroupement par clé (type, nom normalisé) ---------------------------
    groupes = {}  # (type, norme) -> {noms Counter, attributs, mentions[]}
    relations_brutes = []
    for chemin in extraits:
        try:
            donnees = json.loads(chemin.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError, OSError) as e:
            # Une extraction corrompue ne doit jamais bloquer tout le corpus.
            passe.erreur(f"Extraction illisible ignorée : {chemin.name}", str(e))
            continue
        passe.compter("documents_lus")
        for e in donnees.get("entites", []):
            norme = normaliser(e["nom"], titres if e["type"] == "personne" else ())
            if not norme:
                continue
            cle = (e["type"], norme)
            g = groupes.setdefault(cle, {"noms": Counter(), "attributs": {}, "mentions": []})
            g["noms"][e["nom"]] += 1
            for k, v in (e.get("attributs") or {}).items():
                g["attributs"].setdefault(str(k), v)
            if e.get("chunk_id"):
                g["mentions"].append((e["chunk_id"], e.get("citation", "")))
        relations_brutes.extend(donnees.get("relations", []))

    # --- 2. Fusions : équivalences forcées, puis similarité floue ------------------
    uf = UnionFind()
    for regle in onto.get("canonisation", {}).get("equivalences", []) or []:
        type_e, canon, alias = regle[0], regle[1], regle[2]
        a = (type_e, normaliser(canon, titres if type_e == "personne" else ()))
        b = (type_e, normaliser(alias, titres if type_e == "personne" else ()))
        if a in groupes and b in groupes:
            uf.unir(a, b)

    par_type = defaultdict(list)
    for (type_e, norme) in groupes:
        par_type[type_e].append(norme)

    a_arbitrer = []
    for type_e, normes in par_type.items():
        normes = sorted(set(normes))
        for i, norme in enumerate(normes):
            candidats = process.extract(
                norme, normes[i + 1:], scorer=fuzz.token_set_ratio,
                score_cutoff=SEUIL_ARBITRAGE, limit=10,
            )
            for autre, score, _ in candidats:
                if score >= SEUIL_FUSION:
                    uf.unir((type_e, norme), (type_e, autre))
                else:
                    a_arbitrer.append((type_e, norme, autre))

    verdicts = arbitrer_llm(a_arbitrer, journal)
    for (type_e, a, b), fusionner in zip(a_arbitrer, verdicts):
        if fusionner:
            uf.unir((type_e, a), (type_e, b))
            journal.append(f"Arbitrage LLM : fusion [{type_e}] «{a}» <- «{b}»")

    # --- 3. Construction des nœuds canoniques -----------------------------------------
    classes = defaultdict(list)  # représentant -> [clés membres]
    for cle in groupes:
        classes[uf.trouver(cle)].append(cle)

    noeuds, alias_sortie, mentions_sortie = {}, [], []
    correspondance = {}  # (type, norme) -> node_id
    for membres in classes.values():
        type_e = membres[0][0]
        representant = min(n for _, n in membres)  # stable d'une exécution à l'autre
        nid = node_id(onto["noeuds"][type_e]["prefixe"], f"{type_e}|{representant}")
        noms, attributs, mentions = Counter(), {}, []
        for cle in membres:
            g = groupes[cle]
            noms.update(g["noms"])
            for k, v in g["attributs"].items():
                attributs.setdefault(k, v)
            mentions.extend(g["mentions"])
            correspondance[cle] = nid
        nom_canonique = max(noms.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
        noeuds[nid] = {"node_id": nid, "type": type_e, "nom": nom_canonique,
                       "attributs": attributs}
        for variante in noms:
            if variante != nom_canonique:
                alias_sortie.append({"alias": variante, "node_id": nid})
        vues = set()
        for chunk_id, citation in mentions:
            cle_m = (nid, chunk_id, citation or "")
            if cle_m not in vues:
                vues.add(cle_m)
                mentions_sortie.append({"node_id": nid, "chunk_id": chunk_id,
                                        "citation": citation or ""})

    # --- 4. Résolution des relations + contraintes de types ------------------------------
    edges, vues = [], set()
    for r in relations_brutes:
        def resoudre(type_e, nom):
            norme = normaliser(nom, titres if type_e == "personne" else ())
            cle = (type_e, norme)
            if cle in correspondance:
                return correspondance[cle]
            nid = node_id(onto["noeuds"][type_e]["prefixe"], f"{type_e}|{norme}")
            correspondance[cle] = nid
            noeuds.setdefault(nid, {"node_id": nid, "type": type_e, "nom": nom,
                                    "attributs": {"implicite": True}})
            return nid

        type_r = r["type"]
        contrainte = onto["relations"].get(type_r)
        if contrainte is None:
            # Type de relation hors ontologie (cache d'une ancienne version...) : ignoré.
            passe.compter("relations_hors_ontologie")
            continue
        sid = resoudre(r["source_type"], r["source_nom"])
        cid = resoudre(r["cible_type"], r["cible_nom"])
        if sid == cid:
            continue
        if ("*" not in contrainte["sources"] and r["source_type"] not in contrainte["sources"]) or \
           ("*" not in contrainte["cibles"] and r["cible_type"] not in contrainte["cibles"]):
            journal.append(f"Contrainte violée : {r['source_type']} -{type_r}-> "
                           f"{r['cible_type']} ; rétrogradée en liee_a "
                           f"({r['source_nom'][:40]} / {r['cible_nom'][:40]})")
            passe.compter("contraintes_violees")
            type_r = "liee_a"
        cle_e = (sid, type_r, cid, r.get("chunk_id", ""))
        if cle_e in vues:
            continue
        vues.add(cle_e)
        edges.append({"source_id": sid, "type": type_r, "cible_id": cid,
                      "chunk_id": r.get("chunk_id", ""), "citation": r.get("citation", "")})

    DOSSIER.mkdir(parents=True, exist_ok=True)
    ecrire_jsonl(DOSSIER / "nodes.jsonl", list(noeuds.values()))
    ecrire_jsonl(DOSSIER / "aliases.jsonl", alias_sortie)
    ecrire_jsonl(DOSSIER / "mentions.jsonl", mentions_sortie)
    ecrire_jsonl(DOSSIER / "edges.jsonl", edges)
    if journal:
        ecrire_texte(DOSSIER / "journal.log", "\n".join(journal))
    noeuds_implicites = sum(1 for n in noeuds.values() if n.get("attributs", {}).get("implicite"))
    passe.compter("noeuds", len(noeuds))
    passe.compter("noeuds_implicites", noeuds_implicites)
    passe.compter("aretes", len(edges))
    passe.compter("alias", len(alias_sortie))
    passe.compter("paires_arbitrees", len(a_arbitrer))
    passe.compter("fusions_llm", sum(1 for v in verdicts if v))
    if noeuds_implicites:
        passe.avertissement(
            f"{noeuds_implicites} nœud(s) implicite(s) créés par des relations "
            "sans entité extraite correspondante")
    log(f"Canonisation : {len(noeuds)} nœuds, {len(edges)} arêtes, "
        f"{len(alias_sortie)} alias, {len(a_arbitrer)} paires arbitrées "
        f"({sum(1 for v in verdicts if v)} fusions LLM)")


if __name__ == "__main__":
    executer_passe("30_canonize", principal)
