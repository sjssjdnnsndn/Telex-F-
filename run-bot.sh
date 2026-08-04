#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="${ROOT_DIR}/bot-runtime"

if [[ ! -f "${RUNTIME_DIR}/pyproject.toml" || ! -f "${RUNTIME_DIR}/main.py" ]]; then
  echo "ERROR: bot runtime is incomplete at ${RUNTIME_DIR}" >&2
  exit 1
fi

export PYTHONUNBUFFERED=1
export PYTHONPATH="${RUNTIME_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
export PORT="${PORT:?PORT environment variable is required}"

exec uv run \
  --directory "${RUNTIME_DIR}" \
  --locked \
  --python 3.11 \
  python main.py
