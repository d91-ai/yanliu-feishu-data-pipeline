#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_MODEL_CACHE="${HOME}/Documents/Codex/asr-model-cache"

export PYTHONUTF8="${PYTHONUTF8:-1}"
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"
if [[ -z "${SENSEVOICE_MODEL_CACHE:-}" ]]; then
  export SENSEVOICE_MODEL_CACHE="${FUNASR_MODEL_CACHE:-${DEFAULT_MODEL_CACHE}}"
fi
export FUNASR_MODEL_CACHE="${FUNASR_MODEL_CACHE:-${SENSEVOICE_MODEL_CACHE}}"
export MODELSCOPE_CACHE="${MODELSCOPE_CACHE:-${SENSEVOICE_MODEL_CACHE}/modelscope}"
export HF_HOME="${HF_HOME:-${SENSEVOICE_MODEL_CACHE}/huggingface}"
export SENSEVOICE_BRIDGE_AUX_ENGINE="${SENSEVOICE_BRIDGE_AUX_ENGINE:-}"
export SENSEVOICE_MAX_UPLOAD_BYTES="${SENSEVOICE_MAX_UPLOAD_BYTES:-67108864}"
export SENSEVOICE_MAX_CONCURRENT_TRANSCRIPTIONS="${SENSEVOICE_MAX_CONCURRENT_TRANSCRIPTIONS:-1}"
export RESEARCHPIPELINE_SYNC_LOG_DIR="${RESEARCHPIPELINE_SYNC_LOG_DIR:-${HOME}/Library/Logs/researchpipeline-sync}"
export PATH="${HOME}/Documents/会议纪要整理/.transcribe-venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export SENSEVOICE_PYTHON="${SENSEVOICE_PYTHON:-$(command -v python3)}"
LIBREOFFICE_LIBRARY_PATH="/opt/homebrew/opt/little-cms2/lib:/opt/homebrew/lib:/usr/local/opt/little-cms2/lib:/usr/local/lib"
export DYLD_FALLBACK_LIBRARY_PATH="${LIBREOFFICE_LIBRARY_PATH}:${DYLD_FALLBACK_LIBRARY_PATH:-}"
export DYLD_LIBRARY_PATH="${LIBREOFFICE_LIBRARY_PATH}:${DYLD_LIBRARY_PATH:-}"

exec "${SENSEVOICE_PYTHON}" "${SCRIPT_DIR}/sensevoice_transcription_server.py"
