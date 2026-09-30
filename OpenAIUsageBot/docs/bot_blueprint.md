# OpenAI Shadow Ledger — Bot Blueprint

Complete specification of the OpenAI usage-tracking Telegram bot.
Purpose: allow any agent or developer to reproduce, extend, or debug this bot.

---

## 1. What the Bot Does

Tracks OpenAI API token and cost usage across all of Bach's organization projects.
Reports to Telegram via push alerts and pull commands.

**Role 1 — Scheduled monitor (push)**
Polls the OpenAI Admin API every `POLL_INTERVAL_MINS` (default 60) minutes.
On each poll:
- Fetches today's token usage (input/output/total, per model) per project
- Fetches today's cost per project
- Updates `bot_data/usage_state.json`
- If daily spend ≥ `DAILY_SPEND_LIMIT`, fires a one-shot warning to all subscribers

**Role 2 — Command responder (pull)**
Long-polls Telegram `getUpdates`. When a message starts with `@BachsSlave2Bot <cmd>`,
dispatches to the matching handler and replies to that chat.

Both roles run as daemon threads. Main thread sleeps until `KeyboardInterrupt`.

---

## 2. Bot Identity

**Name:** BachsSlave2Bot
**Telegram Bot Token:** stored in `.env` as `TELEGRAM_BOT_TOKEN`
**Primary Chat ID:** `-1003776514928` (stored in `.env` as `TELEGRAM_CHAT_ID`)
**Persona:** Marshal-Rank Shadow Commander, same as the HERMES bot.

---

## 3. Architecture

```
main()
 ├── load .env
 ├── UsageStore(bot_data/usage_state.json)
 ├── SubscriberStore(bot_data/subscribers.json)
 ├── _fetch_bot_username()   → resolves @BachsSlave2Bot
 ├── Thread: telegram_poll_loop()   ← command responder
 ├── Thread: usage_poll_loop()      ← OpenAI poller + alert sender
 └── _send(startup_message)

telegram_poll_loop()
  └── getUpdates (long-poll, timeout=30s)
       └── _match_prefix(@BachsSlave2Bot) → dispatch() → _send(reply, chat_id)

usage_poll_loop()
  └── every POLL_INTERVAL seconds:
       ├── _fetch_costs_breakdown() → /v1/organization/costs
       ├── _fetch_tokens()  → /v1/organization/usage/completions
       ├── UsageStore.update(snapshot)
       └── if total_cost ≥ DAILY_LIMIT and not alert_sent → _send_all(alert)
```

---

## 4. File & Directory Structure

```
OpenAIUsageBot/
├── openai_usage_bot.py    # Entire bot — single file
├── .env                   # Secrets (gitignored)
├── .gitignore
├── docs/
│   └── bot_blueprint.md   # This file
└── bot_data/              # Auto-created on first run (gitignored)
    ├── usage_state.json   # Today's usage snapshot
    └── subscribers.json   # Subscribed chat IDs
```

---

## 5. Configuration

### `.env` file
```
OPENAI_ADMIN_KEY=sk-admin-...      # Organization admin key from OpenAI Platform
TELEGRAM_BOT_TOKEN=...             # From @BotFather
TELEGRAM_CHAT_ID=-1003776514928    # Primary chat (group/channel/private)
DAILY_SPEND_LIMIT=5.00             # USD threshold for daily alert
POLL_INTERVAL_MINS=60              # How often to poll OpenAI API
```

### Admin key requirement
Regular `sk-...` keys cannot access `/v1/organization/*` endpoints.
Must use an **Admin API key** (`sk-admin-...`) created at:
Platform → Organization → API Keys → Create Admin Key

### Known projects (hardcoded in source, IDs from exported CSV)
| Name | Project ID | Notes |
|---|---|---|
| Default project | `proj_Gkm7qFbBFgmW11VFtO13Uw3F` | O not 0 in `VFtO` |
| cngvng-project | `proj_9su0tGI8NsaLE7LHqikCw8VE` | i not 1 in `qik` |
| hoangha-project | `proj_4VPu8UTHzBpZiHFQVaYG923d` | |
| namvuong-project | `proj_fvkY21dJ0ripiOIA2jCC86f3` | |
| khonlanh-project | `proj_fEboQnaVm4tQCk8kFy0h8s08` | |
| phongnguyen-project | `proj_zRWDq4YWIDEkxbgMAjX0xy79` | RW uppercase, j lowercase |
| ngjabach-project | `proj_J4rNEXilII2l889OotmE7YNW` | |

**Important:** Project IDs are case-sensitive. The IDs above were corrected from
the exported CSV. Earlier versions (from screenshot OCR) had wrong characters.
When in doubt, always re-export from Platform → Projects → Export.

---

## 6. OpenAI Admin API

### Costs endpoint
```
GET https://api.openai.com/v1/organization/costs
Headers: Authorization: Bearer sk-admin-...

Params (list of tuples):
  start_time    Unix timestamp (required)
  end_time      Unix timestamp
  bucket_width  "1d"
  group_by[]    "project_id"
  limit         30
```

Response shape:
```json
{
  "data": [
    {
      "results": [
        {
          "project_id": "proj_xxx",
          "amount": {"value": 1.33, "currency": "usd"}
        }
      ]
    }
  ]
}
```

### Usage endpoint
```
GET https://api.openai.com/v1/organization/usage/completions
Headers: Authorization: Bearer sk-admin-...

Params:
  start_time    Unix timestamp (required)
  end_time      Unix timestamp
  bucket_width  "1h"   ← hourly buckets, summed in code for daily total
  group_by[]    "project_id"
  group_by[]    "model"   ← also group by model for per-model breakdown
  limit         100
```

Response shape:
```json
{
  "data": [
    {
      "results": [
        {
          "project_id": "proj_xxx",
          "model": "gpt-4o",
          "input_tokens": 8000,
          "output_tokens": 4000,
          "num_model_requests": 80
        }
      ]
    }
  ]
}
```

## 13. Running

```bash
bash scripts/run_openai_bot.sh
```

The script:
1. Creates `.venv` at repo root if it doesn't exist
2. Activates it (handles both Windows Git Bash and Unix paths)
3. Installs `requests` and `python-dotenv`
4. Validates `.env` exists and has no placeholder values
5. Launches `openai_usage_bot.py` via `exec` (replaces shell process, clean Ctrl-C)

---

### Known issues / lessons learned
- `group_by[]` must be passed as **list of tuples** in Python requests, NOT as a dict key.
  Dict encoding `{"group_by[]": "project_id"}` produces `group_by%5B%5D=project_id` which
  is the same URL-encoding but some API versions reject it — use `[("group_by[]", "value")]`.
- OpenAI usage data has a ~5–15 minute lag. Not real-time.
- All `requests` calls are wrapped in `try/except` — network errors print a log line and return empty, they do NOT kill the thread.
- `usage_poll_loop` body is wrapped in `try/except` — any unexpected error is logged, then the loop sleeps and retries. The thread never dies.
- `telegram_poll_loop` advances `offset` **before** handling each update — a crash mid-handler never causes a message to be re-processed.
- `DAILY_SPEND_LIMIT` alert fires **once per UTC day** — reset at midnight via `reset_day()`.
- `today_window()` uses UTC. If running in Vietnam (UTC+7), "today" in UTC starts 7h behind
  local midnight. This is intentional — matches OpenAI's billing day.

---

## 7. Data Structures

### `bot_data/usage_state.json`
```json
{
  "date": "2026-03-20",
  "total_cost": 2.89,
  "last_polled": 1742380200.0,
  "alert_sent": false,
  "projects": {
    "proj_fEboQnaVm4tQCk8kFy0h8s08": {
      "name": "khonlanh-project",
      "input_tokens": 10000,
      "output_tokens": 5000,
      "total_tokens": 15000,
      "num_requests": 87,
      "cost_usd": 1.33,
      "models": {
        "gpt-4o": {"input": 8000, "output": 4000, "requests": 71},
        "gpt-4o-mini": {"input": 2000, "output": 1000, "requests": 16}
      }
    }
  }
}
```

- `alert_sent` is preserved across snapshot updates (not overwritten by new poll data)
- `models` is nested inside each project — allows per-model token reporting
- `date` is UTC date string — used to detect midnight rollover in `usage_poll_loop`

### `bot_data/subscribers.json`
```json
["-1003776514928"]
```
Plain JSON array of chat ID strings. Primary chat cannot be removed via `dismiss`.

---

## 8. Auto-Alert System

### Token milestones (daily, resets at UTC midnight)

**Why 10M?** OpenAI provides free daily tokens on traffic shared with OpenAI:
- Up to **1M tokens/day free** for: gpt-4.1, gpt-4o, o1, o3, gpt-5.x (premium)
- Up to **10M tokens/day free** for: gpt-4o-mini, o1-mini, o3-mini, o4-mini, gpt-4.1-mini/nano, gpt-5-mini/nano/codex (mini)

The 10M cap alert fires when the mini-model free tier is exhausted — billing starts at standard rates beyond this point.

| Threshold | Level | Behavior |
|---|---|---|
| 1M, 4M, 7M tokens | casual | Informational — approaching or within free tier |
| 8M, 9M tokens | urgent | Nearing 10M free-tier limit for mini models |
| 10M tokens | cap | Free tier for mini models exhausted — billing starts |

Each milestone fires only once per UTC day (`token_milestones_notified` list in state).
After 10M: `spend_intervals_notified` counter tracks how many $2 intervals have alerted.

### Concurrent project alert
Fires when ≥ 3 projects had API calls within the last 5 minutes.
15-minute cooldown (`last_concurrent_alert_ts`) prevents spam.
Checked by `concurrency_check_loop` every 5 minutes via minute-level API buckets.

### Daily spend alerts (hardcoded $5 limit)
`DAILY_LIMIT = 5.00` is hardcoded — not in `.env`.

| Trigger | Alert |
|---|---|
| ≥ $5.00 | Standard limit alert (fires once, `alert_sent` flag) |
| ≥ $7.00 | Level 1 drama — "Expenditure continues" |
| ≥ $9.00 | Level 2 drama — "Sustained Excess" |
| ≥ $11.00 | Level 3 drama — "CRITICAL" |
| ≥ $13.00+ | Level 4 drama — "UNRESTRAINED SPEND / LEDGER IS BLEEDING" |

Each $2 interval above $5 tracked by `spend_intervals_notified` counter.
Resets at UTC midnight via `reset_day()`.

---

## 8. Command System

> **Superseded 2026-09-30.** The live command set is `refresh · usage · spending ·
> archive` + `help` / chat setup, with `/slash` forms and tap-menu buttons; the old
> names below survive only as aliases. Current reference: `docs/openai_bot.md` §8.
> The table below is the original (March 2026) design, kept for history.

Trigger: message must start with `@BachsSlave2Bot` (case-insensitive match on prefix).

| Command | Handler | What it returns |
|---|---|---|
| `@bot today` | `cmd_today` | Full token + cost report, all active projects, sorted by tokens |
| `@bot tokens` | `cmd_tokens` | Token breakdown per project with per-model detail |
| `@bot projects` | `cmd_projects` | Project roster with token bar chart, sorted by consumption |
| `@bot rank` | `cmd_rank` | Rankings by token consumption and daily spend |
| `@bot week` | `cmd_week` | 7-day rolling trend — tokens + spend per day, bar chart |
| `@bot models` | `cmd_models` | Aggregate model usage across all projects today (from snapshot) |
| `@bot spending` | `cmd_spending` | Monthly bill — current month + previous month (live API fetch) |
| `@bot active` | `cmd_active` | Projects with API activity in the last 5 min + concurrency status |
| `@bot refresh` | `cmd_refresh` | Force-poll OpenAI immediately, also triggers milestone check |
| `@bot arise` | `cmd_arise` | Subscribe this chat; patriotic oath speech on first subscribe |
| `@bot dismiss` | `cmd_dismiss` | Unsubscribe (primary chat cannot be dismissed) |
| `@bot help` | `cmd_help` | Full command registry |

### `@bot tokens` output example
```
🔢 Token Report — 2026-03-20

🔹 khonlanh-project  14.2k  (87 reqs)
   ↳ in: 9.5k  /  out: 4.7k
   gpt-4o       8.1k in / 4.2k out  (71 reqs)
   gpt-4o-mini  1.4k in / 0.5k out  (16 reqs)

━━━━━━━━━━━━━━━━━━━━
🔢 Total: 14.2k tokens  •  87 requests
```

---

## 9. Auto-Alerts (Push)

Only one auto-alert type currently:

**Daily spend threshold breach**
Fires when `total_cost >= DAILY_SPEND_LIMIT` and `alert_sent == False`.
Sets `alert_sent = True` after firing. Reset to `False` at UTC midnight.
Sent to all subscribers via `_send_all()`.

```
⚠️ Expenditure threshold breached.
Daily spend has reached $5.12 against your 5.00 USD limit.
Monarch Bach, your war chest demands attention.
```

---

## 10. What Can Be Tracked

| Metric | Available | Notes |
|---|---|---|
| Input tokens per project | ✅ | |
| Output tokens per project | ✅ | |
| Total tokens per project | ✅ | |
| Requests per project | ✅ | |
| Cost per project | ✅ | |
| Per-model breakdown within project | ✅ | gpt-4o vs gpt-4o-mini etc. |
| Per-user within project | 🔜 | Add `group_by[] = user_id` to usage fetch |
| Historical (past days) | 🔜 | Change `start_time` to any past date |
| Token rate / velocity | 🔜 | Compare consecutive polls |
| Per-API-key breakdown | 🔜 | Add `group_by[] = api_key_id` |
| Real-time | ❌ | ~5–15 min lag on OpenAI's side |

---

## 11. Extending This Bot

| What to change | Where |
|---|---|
| Add new project | Nothing to do — each org's projects are discovered from the Admin API hourly; optionally add it to `SEED_PROJECTS` (offline seed) |
| Add per-user tracking | Add `("group_by[]", "user_id")` to `_fetch_tokens` params, update data model |
| Add token alert | Add `DAILY_TOKEN_LIMIT` env var, check in `usage_poll_loop` alongside cost check |
| Historical query | New `cmd_history(date)` — call `_fetch_tokens` with custom `start_time`/`end_time` |
| Hourly summary | Add a scheduled `_send_all(fmt_hourly(...))` inside `usage_poll_loop` |
| Per-model alert | Check `models` dict in snapshot for any model exceeding a threshold |

---

## 12. Running

```bash
pip install requests python-dotenv
cd OpenAIUsageBot
python openai_usage_bot.py
```

On startup the bot:
1. Validates env vars
2. Creates `bot_data/` if needed
3. Loads persistent state
4. Resolves `@BachsSlave2Bot` username via `getMe`
5. Starts Telegram poll thread
6. Starts OpenAI usage poll thread (polls immediately, then every `POLL_INTERVAL`)
7. Sends startup message to `CHAT_ID`
