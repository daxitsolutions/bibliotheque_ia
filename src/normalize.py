"""Passe 10 — Normalisation + chunking.

Parcourt récursivement data/sources, convertit chaque fichier lisible en Markdown
(markitdown pour les formats bureautiques), détecte titre/date/type, puis découpe
en chunks structurés par sections. Incrémental : un fichier inchangé (même sha256)
n'est pas reconverti.

Sorties :
    work/manifest.jsonl          un enregistrement par document
    work/markdown/{doc_id}.md    texte normalisé
    work/chunks/{doc_id}.jsonl   chunks {chunk_id, ordre, chemin_titres, texte}
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from common import (MAX_CHUNK, SOURCES, WORK, avertir, ecrire_jsonl, lire_jsonl,
                    log, sha256_fichier)

EXT_TEXTE = {".md", ".markdown", ".txt"}
EXT_CONVERTIR = {".docx", ".pptx", ".xlsx", ".pdf", ".html", ".htm", ".csv", ".rtf", ".odt", ".doc"}
EXT_IGNORE = {".DS_Store"}

TYPES_DOCUMENT = [
    (r"compte[\s_-]*rendu|\bcr\b|\bpv\b|minutes|copil|coproj", "compte_rendu"),
    (r"recette|\btnr\b|cahier[\s_-]*de[\s_-]*test|campagne|\btest", "cahier_test"),
    (r"formation|support[\s_-]*de[\s_-]*cours|e-?learning", "formation"),
    (r"proc[ée]dure|mode[\s_-]*op[ée]ratoire|consigne|manuel", "procedure"),
    (r"sp[ée]cification|\bspec\b|exigence|\bcdc\b|cahier[\s_-]*des[\s_-]*charges", "specification"),
    (r"d[ée]cision|arbitrage", "registre_decisions"),
    (r"risque", "registre_risques"),
]

RE_DATE = re.compile(r"(20\d{2})[-_./ ]?(0[1-9]|1[0-2])[-_./ ]?([0-2]\d|3[01])")


def detecter_type(nom_fichier: str, debut_texte: str) -> str:
    cible = f"{nom_fichier} {debut_texte[:500]}".lower()
    for motif, type_doc in TYPES_DOCUMENT:
        if re.search(motif, cible):
            return type_doc
    return "document"


def detecter_date(nom_fichier: str, debut_texte: str) -> str | None:
    for source in (nom_fichier, debut_texte[:500]):
        m = RE_DATE.search(source)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def convertir_en_markdown(chemin: Path) -> str:
    if chemin.suffix.lower() in EXT_TEXTE:
        return chemin.read_text(encoding="utf-8", errors="replace")
    if chemin.suffix.lower() in EXT_CONVERTIR:
        from markitdown import MarkItDown  # import paresseux : lourd
        resultat = MarkItDown().convert(str(chemin))
        return resultat.text_content or ""

    try:
        from markitdown import MarkItDown
        resultat = MarkItDown().convert(str(chemin))
        if resultat.text_content and resultat.text_content.strip():
            return resultat.text_content
    except Exception:
        pass

    donnees = chemin.read_bytes()
    if b"\x00" in donnees[:4096]:
        raise ValueError("fichier probablement binaire non textuel")
    return donnees.decode("utf-8", errors="replace")


def sections(md: str) -> list:
    """Découpe le Markdown en sections (chemin de titres, texte)."""
    pile, courant, resultat = [], [], []

    def vider():
        texte = "\n".join(courant).strip()
        if texte:
            resultat.append((" > ".join(pile), texte))
        courant.clear()

    for ligne in md.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)", ligne)
        if m:
            vider()
            niveau = len(m.group(1))
            del pile[niveau - 1:]
            pile.append(m.group(2).strip())
        else:
            courant.append(ligne)
    vider()
    return resultat


def decouper(md: str, maxi: int) -> list:
    """Chunks <= maxi caractères, coupés aux frontières de paragraphes."""
    bruts = []
    for chemin_titres, texte in sections(md):
        tampon = ""
        for para in re.split(r"\n\s*\n", texte):
            if tampon and len(tampon) + len(para) > maxi:
                bruts.append((chemin_titres, tampon.strip()))
                tampon = para
            else:
                tampon = f"{tampon}\n\n{para}" if tampon else para
        if tampon.strip():
            bruts.append((chemin_titres, tampon.strip()))
    # Fusion des fragments trop petits avec le précédent
    chunks = []
    for chemin_titres, texte in bruts:
        if chunks and len(texte) < 200 and len(chunks[-1][1]) + len(texte) <= maxi:
            chunks[-1] = (chunks[-1][0], chunks[-1][1] + "\n\n" + texte)
        else:
            chunks.append((chemin_titres, texte))
    return chunks


def principal() -> None:
    if not SOURCES.exists():
        raise SystemExit(f"Répertoire sources absent : {SOURCES}")
    precedent = {m["doc_id"]: m for m in lire_jsonl(WORK / "manifest.jsonl")}
    (WORK / "markdown").mkdir(parents=True, exist_ok=True)
    (WORK / "chunks").mkdir(parents=True, exist_ok=True)

    manifest, convertis, reutilises, ignores = [], 0, 0, 0
    fichiers = sorted(
        p for p in SOURCES.rglob("*")
        if p.is_file() and p.name not in EXT_IGNORE and not p.name.startswith(".")
    )
    for chemin in fichiers:
        relatif = str(chemin.relative_to(SOURCES))
        doc_id = "DOC-" + hashlib.sha1(relatif.encode("utf-8")).hexdigest()[:10]
        sha = sha256_fichier(chemin)
        chemin_md = WORK / "markdown" / f"{doc_id}.md"

        if doc_id in precedent and precedent[doc_id]["sha256"] == sha and chemin_md.exists():
            manifest.append(precedent[doc_id])
            reutilises += 1
            continue

        try:
            md = convertir_en_markdown(chemin)
        except Exception as e:
            avertir(f"Conversion impossible : {relatif} ({e})")
            continue
        if not md.strip():
            avertir(f"Document vide après conversion : {relatif}")
            continue

        chemin_md.write_text(md, encoding="utf-8")
        m_titre = re.search(r"^#\s+(.+)$", md, re.M)
        titre = m_titre.group(1).strip() if m_titre else chemin.stem.replace("_", " ")
        entree = {
            "doc_id": doc_id,
            "chemin_source": relatif,
            "titre": titre,
            "type_document": detecter_type(chemin.name, md),
            "date_document": detecter_date(chemin.name, md),
            "sha256": sha,
        }
        chunks = [
            {"chunk_id": f"{doc_id}#{i:03d}", "ordre": i,
             "chemin_titres": chemin_titres, "texte": texte}
            for i, (chemin_titres, texte) in enumerate(decouper(md, MAX_CHUNK))
        ]
        ecrire_jsonl(WORK / "chunks" / f"{doc_id}.jsonl", chunks)
        entree["nb_chunks"] = len(chunks)
        manifest.append(entree)
        convertis += 1
        log(f"  [OK] {relatif} -> {doc_id} ({len(chunks)} chunks, {entree['type_document']})")

    ecrire_jsonl(WORK / "manifest.jsonl", manifest)
    log(f"Normalisation : {convertis} converti(s), {reutilises} inchangé(s), "
        f"{ignores} ignoré(s) — {len(manifest)} documents au total")


if __name__ == "__main__":
    principal()
