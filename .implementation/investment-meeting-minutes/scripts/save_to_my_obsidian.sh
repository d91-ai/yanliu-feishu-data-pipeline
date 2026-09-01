#!/bin/zsh
set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "用法: $0 INPUT_FILE [MEETING_DATE]" >&2
  exit 1
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
INPUT_FILE=$1
MEETING_DATE=${2:-}

PYTHON_BIN=${INVESTMENT_MINUTES_PYTHON:-python3}
CMD=("$PYTHON_BIN" "$SCRIPT_DIR/export_to_obsidian.py" "$INPUT_FILE")

if [ -n "$MEETING_DATE" ]; then
  CMD+=(--meeting-date "$MEETING_DATE")
fi

"${CMD[@]}"
