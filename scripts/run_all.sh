#!/usr/bin/env bash
set -euo pipefail
ICI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DEPUIS="${1:-10}"
case "$DEPUIS" in
  10) ETAPES=(10_normalize 20_extract 30_canonize 40_enrich 50_load 60_validate) ;;
  20) ETAPES=(20_extract 30_canonize 40_enrich 50_load 60_validate) ;;
  30) ETAPES=(30_canonize 40_enrich 50_load 60_validate) ;;
  40) ETAPES=(40_enrich 50_load 60_validate) ;;
  50) ETAPES=(50_load 60_validate) ;;
  60) ETAPES=(60_validate) ;;
  *) echo "Usage: $0 [10|20|30|40|50|60]" >&2; exit 2 ;;
esac

for etape in "${ETAPES[@]}"; do
  echo "== $etape =="
  "$ICI/$etape.sh"
done
