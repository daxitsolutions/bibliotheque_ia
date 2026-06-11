#!/usr/bin/env bash
set -euo pipefail
ICI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../config/settings.sh
source "$ICI/../config/settings.sh"
exec "$KB_PYTHON" "$KB_RACINE/src/normalize.py" "$@"
