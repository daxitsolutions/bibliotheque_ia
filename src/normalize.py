"""Passe 10 — Normalisation + chunking.

Parcourt récursivement data/sources, convertit chaque fichier lisible en Markdown,
avec une conversion Office -> Markdown préalable via LibreOffice/OpenOffice,
détecte titre/date/type, puis découpe en chunks structurés par sections.
Incrémental : un fichier inchangé (même sha256) n'est pas reconverti.

Sorties :
    work/manifest.jsonl          un enregistrement par document
    work/markdown/{doc_id}.md    texte normalisé
    work/chunks/{doc_id}.jsonl   chunks {chunk_id, ordre, chemin_titres, texte}
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

from common import (MAX_CHUNK, SOURCES, WORK, avertir, ecrire_jsonl,
                    executer_passe, lire_jsonl, log, sha256_fichier)

CHECKPOINT_TOUS_LES = 50  # docs : fréquence d'écriture du manifest partiel

EXT_TEXTE = {".md", ".markdown", ".txt"}
EXT_OFFICE = {
    ".doc", ".docx", ".docm", ".dot", ".dotx", ".dotm",
    ".odt", ".ott", ".fodt", ".rtf", ".wpd", ".wps",
    ".xls", ".xlsx", ".xlsm", ".xlsb", ".xlt", ".xltx", ".xltm",
    ".ods", ".ots", ".fods", ".csv", ".tsv",
    ".ppt", ".pptx", ".pptm", ".pps", ".ppsx", ".ppsm",
    ".pot", ".potx", ".potm", ".odp", ".otp", ".fodp",
}
EXT_CONVERTIR = EXT_OFFICE | {".pdf", ".html", ".htm"}
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
COMMANDE_OFFICE_TESTEE = False
COMMANDE_OFFICE_OK = None
AVERTI_OFFICE_INDISPONIBLE = False


class HtmlVersMarkdown(HTMLParser):
    """Conversion HTML -> Markdown volontairement simple et robuste."""
    TITRES = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.morceaux = []
        self.lien = None
        self.dans_ligne_table = False

    def texte(self) -> str:
        texte = "".join(self.morceaux)
        texte = re.sub(r"[ \t]+\n", "\n", texte)
        texte = re.sub(r"\n{3,}", "\n\n", texte)
        return texte.strip()

    def _ajouter(self, texte: str) -> None:
        if texte:
            self.morceaux.append(texte)

    def _bloc(self) -> None:
        if self.morceaux and not "".join(self.morceaux[-2:]).endswith("\n\n"):
            self.morceaux.append("\n\n")

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in self.TITRES:
            self._bloc()
            self._ajouter(self.TITRES[tag] + " ")
        elif tag in {"p", "div", "section", "article", "br"}:
            self._bloc()
        elif tag in {"li"}:
            self._bloc()
            self._ajouter("- ")
        elif tag in {"th", "td"}:
            self._ajouter(" | " if self.dans_ligne_table else "")
            self.dans_ligne_table = True
        elif tag == "tr":
            self._bloc()
            self.dans_ligne_table = False
        elif tag == "a":
            self.lien = attrs.get("href")

    def handle_endtag(self, tag):
        if tag in self.TITRES or tag in {"p", "div", "section", "article", "li", "tr"}:
            self._bloc()
        elif tag == "a":
            self.lien = None

    def handle_data(self, data):
        texte = re.sub(r"\s+", " ", data)
        if not texte.strip():
            return
        if self.lien:
            self._ajouter(f"{texte.strip()} ({self.lien})")
        else:
            self._ajouter(texte)


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


def commande_office() -> str | None:
    global AVERTI_OFFICE_INDISPONIBLE, COMMANDE_OFFICE_OK, COMMANDE_OFFICE_TESTEE
    if COMMANDE_OFFICE_TESTEE:
        return COMMANDE_OFFICE_OK
    COMMANDE_OFFICE_TESTEE = True

    for exe in (shutil.which("libreoffice"), shutil.which("soffice"), shutil.which("openoffice")):
        if not exe:
            continue
        try:
            version = subprocess.run(
                [exe, "--version"], text=True, capture_output=True, timeout=20, check=False
            )
        except Exception as e:
            if not AVERTI_OFFICE_INDISPONIBLE:
                avertir(f"LibreOffice/OpenOffice trouvé mais inutilisable ({exe}: {e})")
                AVERTI_OFFICE_INDISPONIBLE = True
            continue
        if version.returncode != 0:
            if not AVERTI_OFFICE_INDISPONIBLE:
                detail = (version.stderr or version.stdout or f"code retour {version.returncode}").strip()
                avertir(f"LibreOffice/OpenOffice trouvé mais inutilisable ({exe}: {detail})")
                AVERTI_OFFICE_INDISPONIBLE = True
            continue

        dossier = WORK / "office_health"
        dossier.mkdir(parents=True, exist_ok=True)
        sonde = dossier / "sonde.rtf"
        sonde.write_text(r"{\rtf1\ansi Test LibreOffice\par}", encoding="utf-8")
        test = subprocess.run(
            [exe, "--headless", "--convert-to", "html", "--outdir", str(dossier), str(sonde)],
            text=True, capture_output=True, timeout=60, check=False,
        )
        if test.returncode == 0 and (dossier / "sonde.html").exists():
            COMMANDE_OFFICE_OK = exe
            return exe
        if not AVERTI_OFFICE_INDISPONIBLE:
            detail = (test.stderr or test.stdout or f"code retour {test.returncode}").strip()
            avertir(f"LibreOffice/OpenOffice trouvé mais conversion headless inutilisable ({exe}: {detail})")
            AVERTI_OFFICE_INDISPONIBLE = True

    COMMANDE_OFFICE_OK = None
    return None


def html_vers_markdown(html: str) -> str:
    parser = HtmlVersMarkdown()
    parser.feed(html)
    return parser.texte()


def convertir_office_en_markdown(chemin: Path, doc_id: str) -> str:
    global COMMANDE_OFFICE_OK
    exe = commande_office()
    if not exe:
        raise RuntimeError("LibreOffice/OpenOffice indisponible ou conversion headless inutilisable")

    dossier = WORK / "office_convert" / doc_id
    dossier.mkdir(parents=True, exist_ok=True)
    for ancien in dossier.iterdir():
        if ancien.is_file():
            ancien.unlink()

    commande = [exe, "--headless", "--convert-to", "html", "--outdir", str(dossier), str(chemin)]
    resultat = subprocess.run(commande, text=True, capture_output=True, timeout=300, check=False)
    if resultat.returncode != 0:
        COMMANDE_OFFICE_OK = None
        raise RuntimeError((resultat.stderr or resultat.stdout or "conversion Office échouée").strip())

    htmls = sorted(dossier.glob("*.html")) + sorted(dossier.glob("*.htm"))
    if not htmls:
        raise RuntimeError("conversion Office sans fichier HTML produit")
    html = htmls[0].read_text(encoding="utf-8", errors="replace")
    md = html_vers_markdown(html)
    if not md.strip():
        raise RuntimeError("conversion Office vide")

    sortie = WORK / "office_md" / f"{doc_id}.md"
    sortie.parent.mkdir(parents=True, exist_ok=True)
    sortie.write_text(md, encoding="utf-8")
    return md


def convertir_via_markitdown_ou_texte(chemin: Path) -> str:
    try:
        from markitdown import MarkItDown
        resultat = MarkItDown().convert(str(chemin))
        if resultat.text_content and resultat.text_content.strip():
            return resultat.text_content
    except Exception as e:
        avertir(f"Fallback markitdown impossible pour {chemin.name} ({e}) ; tentative texte brut")

    donnees = chemin.read_bytes()
    if b"\x00" in donnees[:4096]:
        raise ValueError("fichier probablement binaire non textuel")
    return donnees.decode("utf-8", errors="replace")


def ecrire_markdown_office(doc_id: str, md: str) -> None:
    sortie = WORK / "office_md" / f"{doc_id}.md"
    sortie.parent.mkdir(parents=True, exist_ok=True)
    sortie.write_text(md, encoding="utf-8")


def convertir_en_markdown(chemin: Path, doc_id: str) -> str:
    if chemin.suffix.lower() in EXT_TEXTE:
        return chemin.read_text(encoding="utf-8", errors="replace")
    if chemin.suffix.lower() in EXT_OFFICE:
        try:
            return convertir_office_en_markdown(chemin, doc_id)
        except Exception as e:
            avertir(f"Conversion Office via LibreOffice/OpenOffice impossible pour {chemin.name} ({e}) ; fallback markitdown")
            md = convertir_via_markitdown_ou_texte(chemin)
            ecrire_markdown_office(doc_id, md)
            return md
    if chemin.suffix.lower() in EXT_CONVERTIR:
        return convertir_via_markitdown_ou_texte(chemin)
    return convertir_via_markitdown_ou_texte(chemin)


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


def traiter_fichier(chemin, doc_id, sha, relatif, passe):
    """Convertit + découpe un fichier. Renvoie l'entrée manifest, ou None si ignoré."""
    chemin_md = WORK / "markdown" / f"{doc_id}.md"
    md = convertir_en_markdown(chemin, doc_id)
    if not md.strip():
        passe.avertissement(f"Document vide après conversion : {relatif}")
        return None

    chemin_md.write_text(md, encoding="utf-8")
    m_titre = re.search(r"^#\s+(.+)$", md, re.M)
    titre = m_titre.group(1).strip() if m_titre else chemin.stem.replace("_", " ")
    chunks = [
        {"chunk_id": f"{doc_id}#{i:03d}", "ordre": i,
         "chemin_titres": chemin_titres, "texte": texte}
        for i, (chemin_titres, texte) in enumerate(decouper(md, MAX_CHUNK))
    ]
    ecrire_jsonl(WORK / "chunks" / f"{doc_id}.jsonl", chunks)
    log(f"  [OK] {relatif} -> {doc_id} ({len(chunks)} chunks)")
    return {
        "doc_id": doc_id,
        "chemin_source": relatif,
        "titre": titre,
        "type_document": detecter_type(chemin.name, md),
        "date_document": detecter_date(chemin.name, md),
        "sha256": sha,
        "nb_chunks": len(chunks),
    }


def principal(passe) -> None:
    if not SOURCES.exists():
        raise SystemExit(f"Répertoire sources absent : {SOURCES}")
    precedent = {m["doc_id"]: m for m in lire_jsonl(WORK / "manifest.jsonl")}
    (WORK / "markdown").mkdir(parents=True, exist_ok=True)
    (WORK / "chunks").mkdir(parents=True, exist_ok=True)

    manifest = []
    fichiers = sorted(
        p for p in SOURCES.rglob("*")
        if p.is_file() and p.name not in EXT_IGNORE and not p.name.startswith(".")
    )
    passe.compter("fichiers_trouves", len(fichiers))

    for chemin in fichiers:
        relatif = str(chemin.relative_to(SOURCES))
        doc_id = "DOC-" + hashlib.sha1(relatif.encode("utf-8")).hexdigest()[:10]
        try:
            sha = sha256_fichier(chemin)
        except OSError as e:
            passe.erreur(f"Fichier illisible : {relatif}", str(e))
            continue
        chemin_md = WORK / "markdown" / f"{doc_id}.md"

        if doc_id in precedent and precedent[doc_id]["sha256"] == sha and chemin_md.exists():
            manifest.append(precedent[doc_id])
            passe.compter("inchanges")
            continue

        try:
            entree = traiter_fichier(chemin, doc_id, sha, relatif, passe)
        except Exception as e:
            # Isolation totale : un fichier piégé ne fait jamais tomber le corpus.
            passe.erreur(f"Conversion impossible : {relatif}", str(e))
            passe.compter("ignores")
            continue
        if entree is None:
            passe.compter("ignores")
            continue
        manifest.append(entree)
        passe.compter("convertis")
        passe.compter("chunks", entree["nb_chunks"])

        # Checkpoint incrémental : un kill après des heures ne perd pas tout.
        if passe.compteurs["convertis"] % CHECKPOINT_TOUS_LES == 0:
            ecrire_jsonl(WORK / "manifest.jsonl", manifest)

    ecrire_jsonl(WORK / "manifest.jsonl", manifest)
    elaguer_orphelins({m["doc_id"] for m in manifest}, passe)
    log(f"Normalisation : {passe.compteurs['convertis']} converti(s), "
        f"{passe.compteurs['inchanges']} inchangé(s), {passe.compteurs['ignores']} ignoré(s), "
        f"{passe.compteurs['artefacts_elagues']} orphelin(s) supprimé(s) "
        f"— {len(manifest)} documents au total")


def elaguer_orphelins(ids_valides: set, passe) -> None:
    """Supprime les artefacts des documents disparus (sources supprimées/renommées).

    Sans cela, à l'échelle de milliers de documents qui évoluent, les extractions
    orphelines pollueraient la canonisation (canonize lit TOUT extract/) et les
    chunks orphelins déclencheraient des embeddings inutiles.
    """
    for dossier, motif in (
        (WORK / "markdown", "DOC-*.md"),
        (WORK / "chunks", "DOC-*.jsonl"),
        (WORK / "extract", "DOC-*.json"),
        (WORK / "office_md", "DOC-*.md"),
    ):
        if not dossier.exists():
            continue
        for fichier in dossier.glob(motif):
            if fichier.stem not in ids_valides:
                try:
                    fichier.unlink()
                    passe.compter("artefacts_elagues")
                except OSError as e:
                    passe.avertissement(f"Orphelin non supprimé : {fichier.name}", str(e))


if __name__ == "__main__":
    executer_passe("10_normalize", principal)
