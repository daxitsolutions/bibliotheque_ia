# =============================================================================
# Configuration centrale de la base de connaissances.
# Sourcé par tous les scripts. Chaque variable peut être surchargée par
# l'environnement avant l'appel (ex.: KB_MODELE_EXTRACTION=mistral ./run_all.sh)
# =============================================================================

# Racine du projet (calculée automatiquement)
export KB_RACINE="${KB_RACINE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Surcharges locales créées par scripts/00_install.sh (non versionnées)
if [[ -f "$KB_RACINE/config/local_settings.sh" ]]; then
  # shellcheck source=/dev/null
  source "$KB_RACINE/config/local_settings.sh"
fi

# Chemins
export KB_SOURCES="${KB_SOURCES:-$KB_RACINE/data/sources}"      # fichiers bruts en entrée
export KB_WORK="${KB_WORK:-$KB_RACINE/data/work}"               # artefacts intermédiaires
export KB_DB="${KB_DB:-$KB_RACINE/data/kb.sqlite}"              # base de connaissances finale
export KB_ONTOLOGIE="${KB_ONTOLOGIE:-$KB_RACINE/config/ontologie.yaml}"

# Fournisseur LLM pour extraction + fiches + arbitrage :
#   ollama   -> OLLAMA_URL/api/chat
#   lmstudio -> LMSTUDIO_URL/chat/completions (API OpenAI-compatible)
export KB_LLM_PROVIDER="${KB_LLM_PROVIDER:-lmstudio}"
export OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
export LMSTUDIO_URL="${LMSTUDIO_URL:-http://localhost:1234/v1}"
if [[ "$KB_LLM_PROVIDER" == "lmstudio" ]]; then
  export KB_MODELE_EXTRACTION="${KB_MODELE_EXTRACTION:-google/gemma-4-e4b}"
else
  export KB_MODELE_EXTRACTION="${KB_MODELE_EXTRACTION:-qwen3:14b}"
fi
export KB_MODELE_EMBEDDING="${KB_MODELE_EMBEDDING:-bge-m3}"        # embeddings multilingues (1024 dim)
export KB_DIM_EMBEDDING="${KB_DIM_EMBEDDING:-1024}"                # doit correspondre au modèle ci-dessus
export KB_NUM_CTX="${KB_NUM_CTX:-8192}"                            # fenêtre de contexte Ollama

# Découpage et canonisation
export KB_MAX_CHUNK="${KB_MAX_CHUNK:-2500}"        # taille max d'un chunk (caractères)
export KB_SEUIL_FUSION="${KB_SEUIL_FUSION:-92}"    # similarité >= : fusion automatique
export KB_SEUIL_ARBITRAGE="${KB_SEUIL_ARBITRAGE:-80}" # similarité dans [arbitrage, fusion[ : arbitrage LLM
export KB_ARBITRAGE_LLM="${KB_ARBITRAGE_LLM:-1}"   # 0 pour désactiver l'arbitrage LLM

# Python du venv si présent, sinon python3 système, sauf surcharge explicite
if [[ -z "${KB_PYTHON:-}" ]]; then
  KB_PY="$KB_RACINE/.venv/bin/python"
  [[ -x "$KB_PY" ]] || KB_PY="$(command -v python3)"
  export KB_PYTHON="$KB_PY"
fi
