#!/usr/bin/env bash
# =============================================================================
# 00_install.sh — Installation complète sur Ubuntu (24.04 ou supérieur)
#
# Usage :
#   ./scripts/00_install.sh                # configure LM Studio + tout sauf Ollama
#   ./scripts/00_install.sh --avec-ollama  # installe aussi Ollama pour les embeddings
#
# 100 % open-source : Python, SQLite, sqlite-vec, FTS5, markitdown, Ollama.
# =============================================================================
set -euo pipefail
ICI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RACINE="$(dirname "$ICI")"

echo "== [1/4] Paquets système =="
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3 python3-venv python3-pip sqlite3 jq curl
else
    echo "[!] apt-get introuvable : installez python3, python3-venv, sqlite3, jq manuellement."
fi

echo "== [2/4] Environnement Python (.venv) =="
python3 -m venv "$RACINE/.venv"
"$RACINE/.venv/bin/pip" install --quiet --upgrade pip
"$RACINE/.venv/bin/pip" install --quiet -r "$RACINE/requirements.txt"

echo "== [3/4] Répertoires de travail =="
mkdir -p "$RACINE/data/sources" "$RACINE/data/work"

echo "== [4/4] LM Studio, Ollama et modèles =="
LMSTUDIO_URL_DEFAUT="${LMSTUDIO_URL:-http://localhost:1234/v1}"
if [[ -t 0 ]]; then
    read -r -p "URL API LM Studio [$LMSTUDIO_URL_DEFAUT] : " LMSTUDIO_URL_SAISIE
    LMSTUDIO_URL_CHOISIE="${LMSTUDIO_URL_SAISIE:-$LMSTUDIO_URL_DEFAUT}"
else
    LMSTUDIO_URL_CHOISIE="$LMSTUDIO_URL_DEFAUT"
    echo "   Mode non interactif : URL LM Studio = $LMSTUDIO_URL_CHOISIE"
fi
cat > "$RACINE/config/local_settings.sh" <<EOF
# Généré par scripts/00_install.sh
export KB_LLM_PROVIDER="\${KB_LLM_PROVIDER:-lmstudio}"
export LMSTUDIO_URL="\${LMSTUDIO_URL:-$LMSTUDIO_URL_CHOISIE}"
export KB_MODELE_EXTRACTION="\${KB_MODELE_EXTRACTION:-google/gemma-4-e4b}"
EOF
echo "   LM Studio configuré : $LMSTUDIO_URL_CHOISIE"
echo "   Modèle LLM par défaut : google/gemma-4-e4b"

if ! command -v ollama >/dev/null 2>&1; then
    if [[ "${1:-}" == "--avec-ollama" ]]; then
        curl -fsSL https://ollama.com/install.sh | sh
    else
        echo "[!] Ollama n'est pas installé. Pour l'installer :"
        echo "      curl -fsSL https://ollama.com/install.sh | sh"
        echo "    ou relancez : ./scripts/00_install.sh --avec-ollama"
    fi
fi
if command -v ollama >/dev/null 2>&1; then
    # shellcheck source=../config/settings.sh
    source "$RACINE/config/settings.sh"
    echo "   Téléchargement des modèles Ollama (peut être long la première fois)..."
    if [[ "$KB_LLM_PROVIDER" == "ollama" ]]; then
        ollama pull "$KB_MODELE_EXTRACTION" || echo "[!] Échec du pull de $KB_MODELE_EXTRACTION"
    else
        echo "   LLM configuré via LM Studio : $KB_MODELE_EXTRACTION (pull à faire dans LM Studio)"
    fi
    ollama pull "$KB_MODELE_EMBEDDING"  || echo "[!] Échec du pull de $KB_MODELE_EMBEDDING"
fi

echo ""
echo "== Installation terminée =="
echo "Étapes suivantes :"
echo "  0. Dans LM Studio, démarrez le serveur local et chargez google/gemma-4-e4b"
echo "  1. Déposez vos fichiers dans data/sources/ (sous-dossiers autorisés)"
echo "  2. Lancez le pipeline complet : ./scripts/run_all.sh"
echo "  3. Testez : ./scripts/90_query.sh recherche \"votre question\""
