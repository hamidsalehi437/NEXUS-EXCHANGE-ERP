#!/usr/bin/env bash
# =============================================================================
# NEXUS EXCHANGE ERP — export the OpenAPI document
# =============================================================================
# Writes docs/api/openapi.json from the running application. The document is a
# build artefact (CI uploads it, the Flutter client is generated from it), never
# hand-edited — the app itself is the source of truth.
#
#   scripts/gen_openapi.sh        # against the app in this checkout
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT="${ROOT_DIR}/docs/api/openapi.json"

cd "${ROOT_DIR}/apps/api"
export PYTHONPATH="${ROOT_DIR}/apps/api"

mkdir -p "$(dirname "${OUTPUT}")"
python3 - "${OUTPUT}" <<'PY'
import json
import pathlib
import sys

from app.main import create_app

document = create_app().openapi()
target = pathlib.Path(sys.argv[1])
target.write_text(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
print(f"wrote {target} ({len(document['paths'])} paths, {len(document['components']['schemas'])} schemas)")
PY
