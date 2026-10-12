#!/usr/bin/env bash
set -euo pipefail
ROOT="$(dirname "$(readlink -f "$0")")"
WHEELHOUSE="${1:-/data/mia-build-wheels-aliyun}"
"$ROOT/.venv/bin/python" -c 'import sys, platform; assert sys.version_info[:2] == (3, 11); assert platform.machine() == "x86_64"'
exec "$ROOT/.venv/bin/python" "$ROOT/prepare_api_wheels.py" "$WHEELHOUSE" \
  --index "${MIA_PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
