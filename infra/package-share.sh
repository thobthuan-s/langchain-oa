#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT="${1:-$PROJECT_DIR/../langchain-oa.tgz}"

if git -C "$PROJECT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C "$PROJECT_DIR" archive --format=tar.gz --output="$OUTPUT" HEAD
  echo "Created customer package from committed files: $OUTPUT"
  exit 0
fi

tar \
  --exclude='./.git' \
  --exclude='./.venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.tgz' \
  --exclude='.DS_Store' \
  --exclude='./.pytest_cache' \
  --exclude='./.env' \
  --exclude='./a365.config.json' \
  --exclude='./a365.generated.config.json' \
  --exclude='./manifest' \
  -C "$PROJECT_DIR" -czf "$OUTPUT" .

echo "Created customer package: $OUTPUT"