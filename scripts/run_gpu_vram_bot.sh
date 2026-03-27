#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# GPU VRAM Sentinel — @GruVramBot startup script
# Usage: bash scripts/run_gpu_vram_bot.sh
# ─────────────────────────────────────────────────────────────

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOT_DIR="$REPO_ROOT/GpuVramService"
VENV_DIR="$REPO_ROOT/.venv"
PYTHON=python3

cd "$REPO_ROOT"

# ── 1. Virtual environment ────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "[setup] Creating virtual environment at .venv ..."
    python3 -m venv "$VENV_DIR" 2>/dev/null || true
fi

# Activate if pip is available inside the venv (python3.12-venv required)
VENV_PIP=""
if [ -f "$VENV_DIR/bin/pip" ]; then
    VENV_PIP="$VENV_DIR/bin/pip"
    source "$VENV_DIR/bin/activate"
    PYTHON="$VENV_DIR/bin/python3"
elif [ -f "$VENV_DIR/Scripts/pip" ]; then
    VENV_PIP="$VENV_DIR/Scripts/pip"
    source "$VENV_DIR/Scripts/activate"
    PYTHON="$VENV_DIR/Scripts/python3"
fi

# ── 2. Dependencies ───────────────────────────────────────────
echo "[setup] Installing / verifying dependencies ..."
if [ -n "$VENV_PIP" ]; then
    "$VENV_PIP" install -q --upgrade requests python-dotenv
else
    # Fallback: install into user environment (venv had no pip)
    echo "[setup] venv has no pip — installing to user environment ..."
    pip3 install -q --break-system-packages --upgrade requests python-dotenv \
        2>/dev/null || pip3 install -q --upgrade requests python-dotenv
fi

# ── 3. nvidia-smi check ───────────────────────────────────────
if ! command -v nvidia-smi &> /dev/null; then
    echo "[error] nvidia-smi not found. NVIDIA drivers must be installed."
    exit 1
fi

echo "[check] Detected GPUs:"
nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader,nounits \
    | awk -F', ' '{printf "  GPU %s: %s — %s MiB VRAM\n", $1, $2, $3}'

# ── 4. Env check ─────────────────────────────────────────────
ENV_FILE="$BOT_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "[error] $ENV_FILE not found. Copy .env.example and fill in your secrets."
    exit 1
fi

if grep -q "PUT_TOKEN_HERE\|PUT_ID_HERE" "$ENV_FILE"; then
    echo "[error] .env still contains placeholder values. Fill in real credentials."
    exit 1
fi

# ── 5. Launch ─────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  GPU VRAM Sentinel — @GruVramBot"
echo "  Bot dir : $BOT_DIR"
echo "  Poll    : ${GPU_POLL_INTERVAL_SECS:-60}s  |  Alert threshold: ${VRAM_LOW_THRESHOLD_PCT:-10}% free"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

exec "$PYTHON" "$BOT_DIR/cuda_mem_guard.py"
