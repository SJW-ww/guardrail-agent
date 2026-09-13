#!/usr/bin/env bash
# 从 FastAPI 导出 OpenAPI,作为前端契约的唯一来源(W1 D6-D7 接上 openapi-typescript)。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/packages/contracts/openapi.json"

cd "$ROOT/apps/api"
if [ -x .venv/bin/python ]; then
  PYTHON=.venv/bin/python
else
  PYTHON="uv run python"
fi

$PYTHON -c "
import json, sys
from guardrail_api.main import app
json.dump(app.openapi(), sys.stdout, ensure_ascii=False, indent=2)
" > "$OUT"

echo "written: $OUT"

cd "$ROOT"
npm run --silent contracts:generate
