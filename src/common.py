"""Fonctions partagées du pipeline : configuration, ontologie, clients LLM, base SQLite."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import struct
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

try:
    import requests
except ImportError:  # Les commandes SQLite/FTS doivent rester utilisables sans Ollama.
    requests = None

# --- Configuration (héritée de config/settings.sh via l'environnement) ----------
RACINE = Path(os.environ.get("KB_RACINE", Path(__file__).resolve().parents[1]))
SOURCES = Path(os.environ.get("KB_SOURCES", RACINE / "data" / "sources"))
WORK = Path(os.environ.get("KB_WORK", RACINE / "data" / "work"))
DB_PATH = Path(os.environ.get("KB_DB", RACINE / "data" / "kb.sqlite"))
ONTOLOGIE_PATH = Path(os.environ.get("KB_ONTOLOGIE", RACINE / "config" / "ontologie.yaml"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
LMSTUDIO_URL = os.environ.get("LMSTUDIO_URL", "http://localhost:1234/v1").rstrip("/")
LMSTUDIO_API_KEY = os.environ.get("LMSTUDIO_API_KEY", "")
LLM_PROVIDER = os.environ.get("KB_LLM_PROVIDER", "ollama").lower()
# Fournisseur d'embeddings : par défaut le même que le LLM (LM Studio expose aussi
# un endpoint /embeddings OpenAI-compatible). Surchargeable pour garder Ollama.
EMBEDDING_PROVIDER = os.environ.get("KB_EMBEDDING_PROVIDER", LLM_PROVIDER).lower()
MODELE_EXTRACTION = os.environ.get("KB_MODELE_EXTRACTION", "qwen3:14b")
MODELE_EMBEDDING = os.environ.get("KB_MODELE_EMBEDDING", "bge-m3")
DIM_EMBEDDING = int(os.environ.get("KB_DIM_EMBEDDING", "1024"))
NUM_CTX = int(os.environ.get("KB_NUM_CTX", "8192"))
MAX_TOKENS_SORTIE = int(os.environ.get("KB_MAX_TOKENS_SORTIE", "4096"))  # plafond de génération
MAX_CHUNK = int(os.environ.get("KB_MAX_CHUNK", "2500"))
SEUIL_FUSION = int(os.environ.get("KB_SEUIL_FUSION", "92"))
SEUIL_ARBITRAGE = int(os.environ.get("KB_SEUIL_ARBITRAGE", "80"))
ARBITRAGE_LLM = os.environ.get("KB_ARBITRAGE_LLM", "1") == "1"


def log(msg: str) -> None:
    print(msg, flush=True)


def avertir(msg: str) -> None:
    print(f"[!] {msg}", file=sys.stderr, flush=True)


# --- Ontologie -------------------------------------------------------------------
def _valeur_yaml_simple(valeur: str):
    valeur = valeur.strip()
    if valeur == "[]":
        return []
    if valeur in ("true", "false"):
        return valeur == "true"
    if valeur.isdigit():
        return int(valeur)
    if valeur.startswith('"') and valeur.endswith('"'):
        return valeur[1:-1]
    if valeur.startswith("'") and valeur.endswith("'"):
        return valeur[1:-1]
    if valeur.startswith("[") and valeur.endswith("]"):
        dedans = valeur[1:-1].strip()
        if not dedans:
            return []
        return [_valeur_yaml_simple(x.strip()) for x in dedans.split(",")]
    return valeur


def _charger_yaml_simple(chemin: Path) -> dict:
    racine = {}
    pile = [(-1, racine)]
    for brute in chemin.read_text(encoding="utf-8").splitlines():
        ligne = brute.split("#", 1)[0].rstrip()
        if not ligne.strip():
            continue
        indent = len(ligne) - len(ligne.lstrip(" "))
        texte = ligne.strip()
        if ":" not in texte:
            continue
        cle, valeur = texte.split(":", 1)
        cle = cle.strip().strip('"').strip("'")
        while pile and indent <= pile[-1][0]:
            pile.pop()
        parent = pile[-1][1]
        if valeur.strip():
            parent[cle] = _valeur_yaml_simple(valeur)
        else:
            parent[cle] = {}
            pile.append((indent, parent[cle]))
    return racine


def charger_ontologie() -> dict:
    if yaml is not None:
        with open(ONTOLOGIE_PATH, encoding="utf-8") as f:
            onto = yaml.safe_load(f)
    else:
        onto = _charger_yaml_simple(ONTOLOGIE_PATH)
    if not onto or "noeuds" not in onto or "relations" not in onto:
        raise SystemExit(f"Ontologie invalide : {ONTOLOGIE_PATH}")
    return onto


# --- Normalisation et identifiants -------------------------------------------------
def normaliser(nom: str, titres: tuple = ()) -> str:
    """Forme canonique d'un nom : minuscules, sans accents ni ponctuation ni titres."""
    s = unicodedata.normalize("NFKD", nom)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^\w\s-]", " ", s)
    mots = [m for m in s.split() if m not in titres]
    return " ".join(mots)


def node_id(prefixe: str, cle: str) -> str:
    """Identifiant stable et citable : PREFIXE-xxxxxxxx, dérivé de la clé canonique."""
    return f"{prefixe}-{hashlib.sha1(cle.encode('utf-8')).hexdigest()[:8]}"


def sha256_fichier(chemin: Path) -> str:
    h = hashlib.sha256()
    with open(chemin, "rb") as f:
        for bloc in iter(lambda: f.read(1 << 16), b""):
            h.update(bloc)
    return h.hexdigest()


def sha_texte(texte: str) -> str:
    return hashlib.sha256(texte.encode("utf-8")).hexdigest()[:16]


# --- Clients LLM -----------------------------------------------------------------
def _lmstudio_headers() -> dict:
    if not LMSTUDIO_API_KEY:
        return {}
    return {"Authorization": f"Bearer {LMSTUDIO_API_KEY}"}


def _http_disponible(url: str, headers=None) -> bool:
    if requests is None:
        return False
    try:
        return requests.get(url, headers=headers or {}, timeout=3).ok
    except Exception:
        return False


def llm_disponible() -> bool:
    """Indique si le fournisseur LLM configure repond."""
    if LLM_PROVIDER == "lmstudio":
        return _http_disponible(f"{LMSTUDIO_URL}/models", headers=_lmstudio_headers())
    return _http_disponible(f"{OLLAMA_URL}/api/tags")


def embeddings_disponibles() -> bool:
    """Le fournisseur d'embeddings configuré (LM Studio ou Ollama) répond-il ?"""
    if EMBEDDING_PROVIDER == "lmstudio":
        return _http_disponible(f"{LMSTUDIO_URL}/models", headers=_lmstudio_headers())
    return _http_disponible(f"{OLLAMA_URL}/api/tags")


def ollama_disponible() -> bool:
    """Compatibilite historique : vrai si Ollama repond pour les embeddings."""
    return embeddings_disponibles()


def _reparer_json(texte: str) -> str:
    """Répare un JSON tronqué ou légèrement malformé.

    Cas visés : sortie LLM coupée par le plafond de tokens (chaîne ou structure
    laissée ouverte) ou délimiteur manquant en fin. On rééquilibre les
    accolades/crochets et on ferme une chaîne en suspens pour récupérer au
    moins une extraction partielle plutôt que de perdre tout le chunk.
    """
    out, pile = [], []
    dans_chaine = echappe = False
    for c in texte:
        if dans_chaine:
            out.append(c)
            if echappe:
                echappe = False
            elif c == "\\":
                echappe = True
            elif c == '"':
                dans_chaine = False
            continue
        if c == '"':
            dans_chaine = True
        elif c in "{[":
            pile.append(c)
        elif c in "}]" and pile:
            pile.pop()
        out.append(c)
    res = "".join(out)
    if dans_chaine:  # chaîne coupée en plein milieu : on la termine
        res += '"'
    # retire les fragments de fin invalides laissés par la troncature :
    # virgule/deux-points pendants, ou une clé en position de clé sans valeur
    # (« {"clef": », « ,"clef" »). On itère jusqu'à stabilité.
    while True:
        precedent = res
        res = re.sub(r'[,:]\s*$', "", res.rstrip())
        res = re.sub(r'([{\[,])\s*"(?:[^"\\]|\\.)*"\s*$', r"\1", res.rstrip())
        if res == precedent:
            break
    for c in reversed(pile):  # ferme les structures restées ouvertes
        res += "}" if c == "{" else "]"
    return res


def extraire_json(texte: str) -> dict:
    """Extrait le premier objet JSON d'une réponse, même entourée de bruit.

    Le modèle local produit régulièrement du JSON invalide : retours à la ligne
    bruts ou guillemets non échappés dans les citations, délimiteurs manquants,
    sortie tronquée au plafond de tokens. On tente le décodage strict, puis une
    réparation tolérante (`json_repair`), puis un rééquilibrage structurel en
    dernier recours, afin de récupérer au moins une extraction partielle.
    """
    texte = re.sub(r"```(?:json)?", "", texte).strip()
    debut = texte.find("{")
    if debut < 0:
        raise ValueError(f"Aucun JSON dans la réponse : {texte[:200]!r}")
    fragment = texte[debut:]
    try:
        obj, _ = json.JSONDecoder().raw_decode(fragment)
        return obj
    except json.JSONDecodeError:
        pass
    try:
        from json_repair import repair_json
        obj = repair_json(fragment, return_objects=True)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    obj, _ = json.JSONDecoder().raw_decode(_reparer_json(fragment))
    return obj


def appel_llm(messages: list, json_attendu: bool = True,
              temperature: float = 0.0, essais: int = 3):
    """Appel chat au fournisseur configure, deterministe par defaut."""
    if requests is None:
        raise RuntimeError("Le paquet Python 'requests' est requis pour appeler le LLM.")
    derniere = None
    for i in range(essais):
        try:
            if LLM_PROVIDER == "lmstudio":
                corps = {
                    "model": MODELE_EXTRACTION,
                    "messages": messages,
                    "stream": False,
                    "temperature": temperature,
                    "max_tokens": MAX_TOKENS_SORTIE,
                }
                if json_attendu:
                    # LM Studio attend 'json_schema' (le mode 'json_object' d'OpenAI
                    # n'est pas supporté par toutes les versions). Un schéma permissif
                    # force une sortie JSON valide via grammaire, sans contraindre la forme.
                    corps["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {"name": "sortie", "strict": False,
                                        "schema": {"type": "object"}},
                    }
                r = requests.post(
                    f"{LMSTUDIO_URL}/chat/completions",
                    headers=_lmstudio_headers(),
                    json=corps,
                    timeout=900,
                )
                r.raise_for_status()
                contenu = r.json()["choices"][0]["message"]["content"]
            else:
                corps = {
                    "model": MODELE_EXTRACTION,
                    "messages": messages,
                    "stream": False,
                    "think": False,
                    "options": {"temperature": temperature, "num_ctx": NUM_CTX},
                }
                if json_attendu:
                    corps["format"] = "json"
                r = requests.post(f"{OLLAMA_URL}/api/chat", json=corps, timeout=900)
                r.raise_for_status()
                contenu = r.json()["message"]["content"]
            return extraire_json(contenu) if json_attendu else contenu.strip()
        except Exception as e:  # réseau, JSON malformé, surcharge...
            derniere = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"Échec LLM ({LLM_PROVIDER}) après {essais} essais : {derniere}")


def embeddings(textes: list, lot: int = 32) -> list:
    """Embeddings par lots, via LM Studio (/embeddings) ou Ollama (/api/embed)."""
    if requests is None:
        raise RuntimeError("Le paquet Python 'requests' est requis pour calculer les embeddings.")
    vecteurs = []
    for i in range(0, len(textes), lot):
        bloc = textes[i:i + lot]
        if EMBEDDING_PROVIDER == "lmstudio":
            r = requests.post(
                f"{LMSTUDIO_URL}/embeddings",
                headers=_lmstudio_headers(),
                json={"model": MODELE_EMBEDDING, "input": bloc},
                timeout=900,
            )
            r.raise_for_status()
            # Format OpenAI : data triée par 'index'.
            donnees = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
            vecteurs.extend(d["embedding"] for d in donnees)
        else:
            r = requests.post(
                f"{OLLAMA_URL}/api/embed",
                json={"model": MODELE_EMBEDDING, "input": bloc},
                timeout=900,
            )
            r.raise_for_status()
            vecteurs.extend(r.json()["embeddings"])
    return vecteurs


# --- Vecteurs : sérialisation float32 <-> blob/base64 ---------------------------------
def vec_vers_blob(vec: list) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def vec_vers_b64(vec: list) -> str:
    return base64.b64encode(vec_vers_blob(vec)).decode("ascii")


def b64_vers_blob(s: str) -> bytes:
    return base64.b64decode(s)


# --- Base SQLite ------------------------------------------------------------------------
def ouvrir_db(lecture_seule: bool = False) -> sqlite3.Connection:
    if lecture_seule:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception as e:
        avertir(f"sqlite-vec indisponible ({e}) — recherche vectorielle désactivée")
    return conn


def table_existe(conn: sqlite3.Connection, nom: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ?", (nom,)
    ).fetchone() is not None


# --- JSONL --------------------------------------------------------------------------------
def lire_jsonl(chemin: Path) -> list:
    """Lit un JSONL en tolérant les lignes corrompues (ignorées et signalées).

    À l'échelle de milliers de documents, une seule ligne tronquée (kill pendant
    une écriture, disque plein...) ne doit jamais faire planter une passe entière.
    """
    chemin = Path(chemin)
    if not chemin.exists():
        return []
    lignes, corrompues = [], 0
    with open(chemin, encoding="utf-8", errors="replace") as f:
        for numero, brute in enumerate(f, 1):
            if not brute.strip():
                continue
            try:
                lignes.append(json.loads(brute))
            except (json.JSONDecodeError, ValueError):
                corrompues += 1
                if corrompues <= 3:
                    avertir(f"Ligne illisible ignorée dans {chemin.name}:{numero}")
    if corrompues:
        avertir(f"{chemin.name} : {corrompues} ligne(s) corrompue(s) ignorée(s)")
    return lignes


def _ecrire_atomique(chemin: Path, contenu: str) -> None:
    """Écrit via un fichier temporaire puis remplace : jamais de fichier à moitié écrit."""
    chemin = Path(chemin)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    temp = chemin.with_name(chemin.name + ".tmp")
    with open(temp, "w", encoding="utf-8") as f:
        f.write(contenu)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, chemin)


def ecrire_jsonl(chemin: Path, lignes: list) -> None:
    _ecrire_atomique(
        chemin, "".join(json.dumps(ligne, ensure_ascii=False) + "\n" for ligne in lignes)
    )


def ecrire_texte(chemin: Path, texte: str) -> None:
    """Écriture texte atomique (rapports, fiches consolidées...)."""
    _ecrire_atomique(chemin, texte)


# --- Journal d'exécution : socle du rapport consolidé -------------------------------------
JOURNAL = WORK / "journal"
MAX_INCIDENTS_STOCKES = 500  # bornage : compteur exhaustif, mais on n'archive pas 100k détails


class Passe:
    """Journal structuré d'une passe : compteurs + incidents, persisté en JSON.

    Chaque passe écrit `data/work/journal/<nom>.json`, lu ensuite par le rapport
    consolidé. Le fichier est écrit même en cas de plantage (voir `executer_passe`),
    pour qu'un échec reste analysable.
    """

    def __init__(self, nom: str):
        self.nom = nom
        self.debut = time.time()
        self.compteurs = Counter()
        self.incidents = []  # [{niveau, sujet, detail}]
        self._tronques = 0
        self.statut = "ok"

    def compter(self, cle: str, n: int = 1) -> None:
        self.compteurs[cle] += n

    def _incident(self, niveau: str, sujet: str, detail: str = "") -> None:
        self.compteurs[f"_{niveau}"] += 1
        if len(self.incidents) < MAX_INCIDENTS_STOCKES:
            self.incidents.append({"niveau": niveau, "sujet": sujet, "detail": str(detail)[:500]})
        else:
            self._tronques += 1

    def avertissement(self, sujet: str, detail: str = "") -> None:
        self._incident("avertissement", sujet, detail)
        avertir(f"{sujet}" + (f" — {detail}" if detail else ""))

    def erreur(self, sujet: str, detail: str = "") -> None:
        self._incident("erreur", sujet, detail)
        avertir(f"{sujet}" + (f" — {detail}" if detail else ""))

    def fermer(self, statut: str | None = None) -> dict:
        if statut:
            self.statut = statut
        elif self.compteurs.get("_erreur"):
            self.statut = "avertissements"  # erreurs non fatales isolées
        donnees = {
            "passe": self.nom,
            "statut": self.statut,
            "fin": datetime_iso(),
            "duree_s": round(time.time() - self.debut, 1),
            "compteurs": dict(self.compteurs),
            "incidents": self.incidents,
            "incidents_tronques": self._tronques,
        }
        try:
            _ecrire_atomique(JOURNAL / f"{self.nom}.json",
                             json.dumps(donnees, ensure_ascii=False, indent=2))
        except Exception as e:  # le journal ne doit jamais faire échouer la passe
            avertir(f"Journal non écrit pour {self.nom} ({e})")
        return donnees


def datetime_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def executer_passe(nom: str, fonction) -> None:
    """Exécute une passe en garantissant l'écriture du journal, même sur plantage.

    `fonction(passe)` reçoit le journal. Toute exception est enregistrée comme
    échec fatal puis ré-émise (code retour non nul pour run_all), mais le journal
    est écrit dans tous les cas : un crash reste visible dans le rapport.
    """
    passe = Passe(nom)
    try:
        fonction(passe)
    except SystemExit as e:
        # Préconditions non remplies (LLM injoignable, manifest absent...).
        if e.code not in (0, None):
            passe.erreur("Passe interrompue", str(e.code or e))
            passe.fermer("echec")
        else:
            passe.fermer()
        raise
    except KeyboardInterrupt:
        passe.fermer("interrompu")
        raise
    except Exception as e:
        passe.erreur("Plantage inattendu", f"{type(e).__name__}: {e}")
        passe.fermer("echec")
        raise
    else:
        passe.fermer()
