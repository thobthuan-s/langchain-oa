#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT="${1:-$PROJECT_DIR/../langchain-oa.tgz}"

tar \
  --exclude='./.venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.DS_Store' \
  --exclude='./.pytest_cache' \
  --exclude='./.env' \
  --exclude='./a365.config.json' \
  --exclude='./a365.generated.config.json' \
  --exclude='./manifest' \
  -C "$PROJECT_DIR" -czf "$OUTPUT" .

echo "Created customer package: $OUTPUT"