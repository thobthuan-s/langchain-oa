#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT="${1:-$PROJECT_DIR/../langchain-oa.tgz}"

REPO_ROOT="$(git -C "$PROJECT_DIR" rev-parse --show-toplevel 2>/dev/null)" || {
  echo "ERROR: A Git checkout is required to package tracked source files safely." >&2
  exit 1
}
if [[ "$REPO_ROOT" != "$PROJECT_DIR" ]]; then
  echo "ERROR: The project directory must be the root of its own Git checkout." >&2
  exit 1
fi

TEMP_OUTPUT="$(mktemp "${OUTPUT}.tmp.XXXXXX")"
trap 'rm -f "$TEMP_OUTPUT"' EXIT
FILE_LIST="$(mktemp)"
trap 'rm -f "$FILE_LIST" "$TEMP_OUTPUT"' EXIT
git -C "$PROJECT_DIR" ls-files --cached -z > "$FILE_LIST"
[[ -s "$FILE_LIST" ]] || { echo "ERROR: No tracked files to package." >&2; exit 1; }

while IFS= read -r -d '' path; do
  case "$path" in
    .env.template|a365.config.template.json) ;;
    *)
      case "${path##*/}" in
        .env|.env.*|a365.config.json|a365.config.*|a365.generated.config*|*.pem|*.key|*.pfx|*.p12|*.zip|*.tgz|*.xlsx|*.sarif|*.bak|*.bak.*|.DS_Store)
          echo "ERROR: Refusing to package sensitive or generated file: $path" >&2
          exit 1
          ;;
      esac
      ;;
  esac
  case "$path" in
    .git/*|.venv/*|.vscode/*|manifest/*|*/__pycache__/*)
      echo "ERROR: Refusing to package local or generated directory: $path" >&2
      exit 1
      ;;
  esac
  if [[ -L "$PROJECT_DIR/$path" || ! -f "$PROJECT_DIR/$path" ]]; then
    echo "ERROR: Tracked file is missing or is a symbolic link: $path" >&2
    exit 1
  fi
done < "$FILE_LIST"

tar -czf "$TEMP_OUTPUT" -C "$PROJECT_DIR" --null -T "$FILE_LIST"
mv -f "$TEMP_OUTPUT" "$OUTPUT"
echo "Created customer package from tracked source files: $OUTPUT"