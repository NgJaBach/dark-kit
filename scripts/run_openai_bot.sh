#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# OpenAI Shadow Ledger — @BachsSlave2Bot startup script
#
# ── QUICK START ───────────────────────────────────────────────
#   tmux new-session -d -s bot 'bash /home/ngjabach/Documents/Research/BAILAB/NgJaBach-Shadow-Army/scripts/run_openai_bot.sh'
#   tmux attach -t bot
#
# ── REBOOT SURVIVAL (paste into crontab -e) ──────────────────
#   @reboot sleep 15 && tmux new-session -d -s bot 'bash /home/ngjabach/Documents/Research/BAILAB/NgJaBach-Shadow-Army/scripts/run_openai_bot.sh'
#
# ── TMUX CHEATSHEET ──────────────────────────────────────────
#   tmux attach -t bot          — reattach to running session
#   Ctrl+B, D                   — detach (leave bot running)
#   tmux kill-session -t bot    — stop bot permanently
#   tmux ls                     — list sessions
# ─────────────────────────────────────────────────────────────

# No `set -e`: with pipefail, a crashed bot makes the `python | tee` pipeline
# fail, and -e would then kill this script instead of letting the watchdog
# restart the bot.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOT_DIR="$REPO_ROOT/OpenAIUsageBot"
VENV_DIR="$REPO_ROOT/.venv"
LOG_DIR="$BOT_DIR/bot_data/logs"
export PATH="$HOME/.local/bin:$PATH"   # uv lives here; tmux/cron shells may not have it

cd "$REPO_ROOT"
mkdir -p "$LOG_DIR"

# ── 1. Python + dependencies ─────────────────────────────────
# Run from the uv-managed .venv (pinned by uv.lock). The network is touched only
# when a dependency is actually missing: right after a reboot DNS is often still
# down, and an unconditional install used to abort the launch entirely.
PYTHON="$VENV_DIR/bin/python"
deps_ok() { [ -x "$PYTHON" ] && "$PYTHON" -c "import requests, dotenv" 2>/dev/null; }

until deps_ok; do
    echo "[setup] Dependencies missing — installing into .venv ..."
    if command -v uv >/dev/null 2>&1; then
        [ -x "$PYTHON" ] || uv venv -q "$VENV_DIR"
        uv pip install -q --python "$PYTHON" requests python-dotenv
    else
        [ -x "$PYTHON" ] || python3 -m venv "$VENV_DIR"
        "$PYTHON" -m pip install -q requests python-dotenv
    fi
    deps_ok || { echo "[setup] Install failed (network?) — retrying in 30 s"; sleep 30; }
done

# ── 2. Env check ─────────────────────────────────────────────
ENV_FILE="$BOT_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "[error] $ENV_FILE not found. Copy .env.example and fill in your secrets."
    exit 1
fi
if grep -q "PUT_TOKEN_HERE\|PUT_KEY_HERE" "$ENV_FILE"; then
    echo "[error] .env still contains placeholder values. Fill in real credentials."
    exit 1
fi

# ── 3. Launch (watchdog loop — restarts on crash) ─────────────
# Output is mirrored to a monthly log so history survives reboots (tmux
# scrollback does not). PYTHONUNBUFFERED keeps prints line-buffered through tee.
# The bot prints its own effective config at startup.
while true; do
    LOG_FILE="$LOG_DIR/stdout-$(date -u +%Y-%m).log"
    PYTHONUNBUFFERED=1 "$PYTHON" "$BOT_DIR/openai_usage_bot.py" 2>&1 | tee -a "$LOG_FILE"
    code=${PIPESTATUS[0]}
    echo "[watchdog] $(date -u '+%F %T') UTC — bot exited (code $code). Restarting in 5 s (tmux kill-session -t bot to stop)" \
        | tee -a "$LOG_FILE"
    sleep 5
done
