# NgJaBach Shadow Army

*Last updated: 16/04/2026*

**Goal:** A collection of Telegram bots and automation scripts serving Bach the Monarch — monitoring AI spend, guarding GPU territory, and reporting to the council.

This repo was originally built for the [Business AI Lab](https://www.facebook.com/business.ai.lab) shared infrastructure. Each bot is a single Python file, minimal dependencies, runs anywhere with an NVIDIA driver.

---

## Bots

### 1. OpenAI Shadow Ledger — `@BachsSlave2Bot`

**Location:** `OpenAIUsageBot/`
**Run:** `bash scripts/run_openai_bot.sh`

#### What it does

Tracks OpenAI API token and cost usage across all organization projects in real time. Polls the OpenAI Admin API on a configurable schedule and pushes alerts to the council Telegram chat when spending thresholds are crossed.

- Reports daily token consumption and cost per project
- Breaks down usage by model (gpt-4o, gpt-4o-mini, etc.)
- Fires escalating drama alerts starting at $5/day, then every $2 above that
- Alerts at token milestones for **mini/nano models** (1M / 4M / 7M / 8M / 9M / 10M — 10M free-tier cap)
- Alerts at token milestones for **premium models** (200K / 500K / 800K / 1M — 1M free-tier cap for gpt-4o, gpt-4.1, o1, o3, etc.)
- Detects when ≥ 3 projects are hitting the API concurrently (concurrency alert)
- Applies a 10-minute ingestion delay buffer on the Costs API (documented OpenAI lag)
- Responds to pull commands from any subscribed chat

#### Personality

**Marshal-Rank Shadow Commander.** Speaks with imperial weight. Every alert is a battlefield dispatch. Spending reports read like war-chest ledgers. Free-tier exhaustion is a strategic crisis. No small talk — every message has purpose and rank.

#### Commands

| Command | Description |
|---------|-------------|
| `@BachsSlave2Bot tokens` | Token breakdown per project with per-model detail |
| `@BachsSlave2Bot projects` | Project roster with token bar chart |
| `@BachsSlave2Bot rank` | Rankings by token consumption and daily spend |
| `@BachsSlave2Bot recent` | Last 31 days — per-project cost, total tokens & requests |
| `@BachsSlave2Bot models` | Aggregate model usage across all projects today |
| `@BachsSlave2Bot spending` | Monthly bill — current + previous month |
| `@BachsSlave2Bot active` | Projects with API activity in the last 5 min |
| `@BachsSlave2Bot refresh` | Force-poll OpenAI immediately |
| `@BachsSlave2Bot arise` | Subscribe this chat to automatic alerts |
| `@BachsSlave2Bot dismiss` | Unsubscribe (primary chat cannot be dismissed) |
| `@BachsSlave2Bot setname Name` | Set the name the bot uses to address you |
| `@BachsSlave2Bot help` | Full command registry |

#### `.env` file — `OpenAIUsageBot/.env`

```env
# OpenAI Admin API key (sk-admin-..., NOT a regular sk-... key)
# Create at: platform.openai.com → Organization → API Keys → Create Admin Key
OPENAI_ADMIN_KEY=PUT_KEY_HERE

# Telegram bot token from @BotFather
TELEGRAM_BOT_TOKEN=PUT_TOKEN_HERE

# Target Telegram chat (group/channel/private). Use negative ID for groups.
TELEGRAM_CHAT_ID=PUT_ID_HERE

# Optional: topic thread ID for supergroups with topics enabled
TELEGRAM_THREAD_ID=

# How often to poll OpenAI usage API (minutes). Default: 5
POLL_INTERVAL_MINS=5
```

> **Note:** `DAILY_SPEND_LIMIT` is hardcoded to `$5.00` in the bot source — alerts fire at $5, then every $2 above.

---

### 2. GPU VRAM Sentinel — `@GruVramBot`

**Location:** `GpuVramService/`
**Run:** `bash scripts/run_gpu_vram_bot.sh`

#### What it does

Monitors VRAM usage across all detected GPUs on the host machine. Sends automatic alerts when **free VRAM rises above** the configured threshold (i.e., the GPU is available and ripe for claiming), and allows on-command VRAM bloating — pre-reserving GPU memory via the CUDA Driver API so lightweight jobs cannot claim it before heavy model workloads start.

- Polls `nvidia-smi` every N seconds across all GPUs
- Fires high-VRAM alerts with per-GPU cooldown (10 min) to prevent spam
- Bloat: allocates a CUDA memory block to occupy 20 / 50 / 70 / 90% of a GPU's VRAM
- Release: frees the allocation on demand (per GPU or all at once)
- **Killer Mode:** arms autopilot — when free VRAM drops below a trigger threshold (10% / 50% / 70%), the bot auto-bloats to full and sends reminders every 60s
- Uses the CUDA Driver API (`libcuda.so.1`) directly via `ctypes` — no PyTorch required

> **Linux vs Windows:** On Linux (this machine), CUDA allocations are physically committed to VRAM immediately — `nvidia-smi` will show the full reservation. On Windows WDDM, the OS may page GPU memory, making pre-reservation unreliable. **Linux is the intended deployment target.**

#### Personality

**VRAM Garrison Commander.** Bound to Bach the Monarch. Speaks with a soldier's discipline — formal, precise, zero humor. Reports status in terms of territory, occupation %, and garrison strength. The GPU is the battlefield. Every bloat is a territorial claim. Every release is a strategic withdrawal.

#### Commands

| Command | Description |
|---------|-------------|
| `@GruVramBot status` | VRAM snapshot for all GPUs — used/free/total, utilization, temperature |
| `@GruVramBot bloat` | Interactive keyboard: select GPU(s), then occupation % target |
| `@GruVramBot release` | Interactive keyboard: release specific GPU or all |
| `@GruVramBot killer` | Arm autopilot: auto-bloat when free VRAM drops below a trigger threshold |
| `@GruVramBot unkill` | Disarm killer mode (interactive buttons) |
| `@GruVramBot arise` | Subscribe chat to automatic high-VRAM alerts |
| `@GruVramBot dismiss` | Unsubscribe chat |
| `@GruVramBot setname Name` | Set the name the bot uses to address you |
| `@GruVramBot help` | Command registry |

#### Bloat levels

`20%` · `50%` · `70%` · `90%` of total VRAM on the selected GPU.

#### Killer Mode trigger thresholds

`10%` · `50%` · `70%` free VRAM — auto-bloat fires when free VRAM drops below the chosen level. Reminders fire every 60 seconds while armed.

#### `.env` file — `GpuVramService/.env`

```env
# Telegram bot token from @BotFather (different bot from @BachsSlave2Bot)
TELEGRAM_BOT_TOKEN=PUT_TOKEN_HERE

# Target Telegram chat (group/channel/private). Use negative ID for groups.
TELEGRAM_CHAT_ID=PUT_ID_HERE

# Optional: topic thread ID for supergroups with topics enabled
TELEGRAM_THREAD_ID=

# How often to poll GPU stats (seconds). Default: 60
GPU_POLL_INTERVAL_SECS=60

# Alert when free VRAM rises ABOVE this percentage on any unoccupied GPU. Default: 50
VRAM_HIGH_THRESHOLD_PCT=50
```

---

## Project structure

```
NgJaBach-Shadow-Army/
├── scripts/
│   ├── run_openai_bot.sh      # Launch @BachsSlave2Bot
│   └── run_gpu_vram_bot.sh    # Launch @GruVramBot
│
├── OpenAIUsageBot/
│   ├── openai_usage_bot.py    # Bot source (single file)
│   ├── .env                   # Secrets — gitignored
│   ├── .gitignore
│   ├── docs/
│   │   └── bot_blueprint.md   # Full spec
│   └── bot_data/              # Auto-created on first run — gitignored
│       ├── usage_state.json   # Today's usage snapshot
│       ├── subscribers.json   # Subscribed chat IDs
│       └── names.json         # Per-chat display names
│
└── GpuVramService/
    ├── just_training.py       # Bot source (single file)
    ├── .env                   # Secrets — gitignored
    ├── .gitignore
    ├── docs/
    │   └── bot_blueprint.md   # Full spec
    └── bot_data/              # Auto-created on first run — gitignored
        ├── subscribers.json   # Subscribed chat IDs
        └── names.json         # Per-chat display names
```

A shared `.venv/` is created at the repo root by either run script.

---

## Quick start

```bash
# Clone
git clone https://github.com/NgJaBach/NgJaBach-Shadow-Army.git
cd NgJaBach-Shadow-Army

# Configure OpenAI bot
cp OpenAIUsageBot/.env.example OpenAIUsageBot/.env   # or create manually
# Fill in: OPENAI_ADMIN_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# Configure GPU VRAM bot
cp GpuVramService/.env.example GpuVramService/.env   # or create manually
# Fill in: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# Run (each in its own terminal or tmux pane)
bash scripts/run_openai_bot.sh
bash scripts/run_gpu_vram_bot.sh
```

Both scripts auto-create the `.venv`, install dependencies, validate `.env`, then launch.

---

## Dependencies

All dependencies are installed automatically by the run scripts.

| Package | Used by |
|---------|---------|
| `requests` | Both bots — Telegram API, OpenAI API |
| `python-dotenv` | Both bots — `.env` loading |

No PyTorch, no CUDA toolkit. GPU VRAM bot requires only the **NVIDIA GPU driver** (`libcuda.so.1` on Linux, `nvcuda.dll` on Windows).

---

> ⚠️ **Reminder:** A lot of example code floating around the internet uses older versions of the OpenAI Python package. Double-check the latest changes before copying anything into production.
>
> 👉 [v1.0.0 Migration Guide](https://github.com/openai/openai-python/discussions/742)
