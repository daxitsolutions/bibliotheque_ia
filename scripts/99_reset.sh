#!/usr/bin/env bash
# =============================================================================
# 99_reset.sh — Réinitialise la base de connaissances.
#
# Trois niveaux, du moins au plus destructeur :
#
#   ./scripts/99_reset.sh            Base seule : supprime data/kb.sqlite + le
#                                    rapport. Le cache de travail (data/work/) est
#                                    conservé → rebuild rapide, sans ré-appel LLM :
#                                        ./scripts/run_all.sh 50
#
#   ./scripts/99_reset.sh --complet  + data/work/ (manifest, chunks, extractions,
#                                    fiches, embeddings, journaux). Rebuild COMPLET,
#                                    avec ré-appels LLM (long) :
#                                        ./scripts/run_all.sh
#
#   ./scripts/99_reset.sh --sources  + data/sources/ : supprime aussi vos DOCUMENTS
#                                    d'origine. IRRÉVERSIBLE (le pipeline ne peut
#                                    PAS les reconstruire ; récupérables seulement
#                                    s'ils sont suivis par git). Implique --complet.
#
# Les sources ne sont JAMAIS touchées sauf avec --sources. config/ jamais touché.
# Options : --oui (sans confirmation), --aide.
# =============================================================================
set -euo pipefail
ICI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../config/settings.sh
source "$ICI/../config/settings.sh"

COMPLET=0
SOURCES=0
SANS_CONFIRMATION=0
for arg in "$@"; do
  case "$arg" in
    --complet|--tout) COMPLET=1 ;;
    --sources)        SOURCES=1; COMPLET=1 ;;  # supprimer les sources implique tout vider
    --oui|--yes|-y)   SANS_CONFIRMATION=1 ;;
    --aide|--help|-h)
      awk 'NR>1 && /^#/{sub(/^# ?/,"");print;next} NR>1{exit}' "${BASH_SOURCE[0]}"
      exit 0 ;;
    *) echo "Option inconnue : $arg (voir --aide)" >&2; exit 2 ;;
  esac
done

# Garde-fou : ne jamais opérer sur des chemins vides ou racine.
RAPPORT="$KB_RACINE/data/rapport.md"
for chemin in "$KB_DB" "$KB_WORK" "$KB_SOURCES" "$KB_RACINE"; do
  if [[ -z "$chemin" || "$chemin" == "/" ]]; then
    echo "[!] Chemin dangereux ($chemin) — abandon." >&2
    exit 1
  fi
done

echo "Réinitialisation de la base de connaissances."
echo "Seront supprimés :"
echo "  - $KB_DB (+ sidecars -wal/-shm, fichier de build)"
echo "  - $RAPPORT"
if [[ "$COMPLET" == "1" ]]; then
  echo "  - $KB_WORK/ (TOUT le cache : manifest, chunks, extractions, fiches, embeddings, journaux)"
  echo "    -> le prochain run relancera l'extraction et les embeddings (appels LLM, long)."
fi
if [[ "$SOURCES" == "1" ]]; then
  echo ""
  echo "  ⚠️  - $KB_SOURCES/ : VOS DOCUMENTS D'ORIGINE."
  echo "  ⚠️    Action IRRÉVERSIBLE : le pipeline ne peut pas les reconstruire."
  echo "  ⚠️    Récupération possible UNIQUEMENT s'ils sont suivis par git."
fi
if [[ "$COMPLET" != "1" ]]; then
  echo "Conservés : $KB_WORK/ (cache de travail) et $KB_SOURCES/ (documents)."
elif [[ "$SOURCES" != "1" ]]; then
  echo "Conservés : $KB_SOURCES/ (documents)."
fi
echo "Jamais touché : config/."

if [[ "$SANS_CONFIRMATION" != "1" ]]; then
  if [[ ! -t 0 ]]; then
    echo "[!] Mode non interactif : relancez avec --oui pour confirmer." >&2
    exit 1
  fi
  read -r -p "Confirmer la suppression ? [oui/N] " reponse
  case "$reponse" in
    oui|o|O|y|Y|yes) ;;
    *) echo "Annulé."; exit 0 ;;
  esac
  # Seconde confirmation explicite pour la suppression irréversible des sources.
  if [[ "$SOURCES" == "1" ]]; then
    echo "Suppression des DOCUMENTS SOURCES demandée (irréversible)."
    read -r -p "Tapez exactement 'supprimer-sources' pour confirmer : " confirmation
    if [[ "$confirmation" != "supprimer-sources" ]]; then
      echo "Annulé (sources conservées)."
      exit 0
    fi
  fi
fi

# Base construite + sidecars + fichier de build éventuel
for suffixe in "" "-wal" "-shm" ".build" ".build-wal" ".build-shm"; do
  cible="$KB_DB$suffixe"
  [[ -e "$cible" ]] && rm -f "$cible" && echo "  supprimé : $cible"
done
[[ -e "$RAPPORT" ]] && rm -f "$RAPPORT" && echo "  supprimé : $RAPPORT"

if [[ "$COMPLET" == "1" && -d "$KB_WORK" ]]; then
  rm -rf "${KB_WORK:?}/"
  echo "  supprimé : $KB_WORK/"
fi

if [[ "$SOURCES" == "1" && -d "$KB_SOURCES" ]]; then
  # On vide le contenu mais on garde le dossier (pour y redéposer des documents).
  rm -rf "${KB_SOURCES:?}"
  mkdir -p "$KB_SOURCES"
  echo "  vidé : $KB_SOURCES/ (documents supprimés, dossier recréé)"
fi

echo "Réinitialisation terminée."
if [[ "$SOURCES" == "1" ]]; then
  echo "Déposez de nouveaux documents dans $KB_SOURCES/ puis : ./scripts/run_all.sh"
elif [[ "$COMPLET" == "1" ]]; then
  echo "Reconstruire (complet) : ./scripts/run_all.sh"
else
  echo "Reconstruire (rapide, cache conservé) : ./scripts/run_all.sh 50"
fi
