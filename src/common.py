"""Fonctions partagées du pipeline : configuration, ontologie, clients LLM, base SQLite."""
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
MODELE_EXTRACTION = os.environ.get("KB_MODELE_EXTRACTION", "qwen3:14b")
MODELE_EMBEDDING = os.environ.get("KB_MODELE_EMBEDDING", "bge-m3")
DIM_EMBEDDING = int(os.environ.get("KB_DIM_EMBEDDING", "1024"))
NUM_CTX = int(os.environ.get("KB_NUM_CTX", "8192"))
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
    """Les embeddings restent fournis par Ollama, meme si le chat utilise LM Studio."""
    return _http_disponible(f"{OLLAMA_URL}/api/tags")


def ollama_disponible() -> bool:
    """Compatibilite historique : vrai si Ollama repond pour les embeddings."""
    return embeddings_disponibles()


def extraire_json(texte: str) -> dict:
    """Extrait le premier objet JSON d'une réponse, même entourée de bruit."""
    texte = re.sub(r"```(?:json)?", "", texte).strip()
    debut = texte.find("{")
    if debut < 0:
        raise ValueError(f"Aucun JSON dans la réponse : {texte[:200]!r}")
    obj, _ = json.JSONDecoder().raw_decode(texte[debut:])
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
                    "max_tokens": NUM_CTX,
                }
                if json_attendu:
                    corps["response_format"] = {"type": "json_object"}
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
    """Embeddings par lots via Ollama (/api/embed)."""
    if requests is None:
        raise RuntimeError("Le paquet Python 'requests' est requis pour calculer les embeddings.")
    vecteurs = []
    for i in range(0, len(textes), lot):
        r = requests.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": MODELE_EMBEDDING, "input": textes[i:i + lot]},
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
    if not Path(chemin).exists():
        return []
    with open(chemin, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def ecrire_jsonl(chemin: Path, lignes: list) -> None:
    chemin = Path(chemin)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as f:
        for ligne in lignes:
            f.write(json.dumps(ligne, ensure_ascii=False) + "\n")
