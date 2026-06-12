#!/usr/bin/env bash
# Orchestrateur du pipeline.
# Robustesse : si une passe échoue, on s'arrête (les passes suivantes en dépendent)
# MAIS on génère toujours le rapport consolidé, pour que l'échec reste analysable.
set -uo pipefail
ICI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DEPUIS="${1:-10}"
case "$DEPUIS" in
  10) ETAPES=(10_normalize 20_extract 30_canonize 40_enrich 50_load) ;;
  20) ETAPES=(20_extract 30_canonize 40_enrich 50_load) ;;
  30) ETAPES=(30_canonize 40_enrich 50_load) ;;
  40) ETAPES=(40_enrich 50_load) ;;
  50) ETAPES=(50_load) ;;
  60) ETAPES=() ;;  # rapport seul
  *) echo "Usage: $0 [10|20|30|40|50|60]" >&2; exit 2 ;;
esac

ECHEC=""
for etape in "${ETAPES[@]}"; do
  echo "== $etape =="
  if ! "$ICI/$etape.sh"; then
    echo "!! Passe $etape en échec — arrêt du pipeline, génération du rapport." >&2
    ECHEC="$etape"
    break
  fi
done

# Le rapport est TOUJOURS produit, succès comme échec.
echo "== 60_validate (rapport) =="
"$ICI/60_validate.sh" || echo "!! Rapport non généré." >&2

if [[ -n "$ECHEC" ]]; then
  echo "Pipeline interrompu à la passe $ECHEC. Voir data/rapport.md." >&2
  exit 1
fi
