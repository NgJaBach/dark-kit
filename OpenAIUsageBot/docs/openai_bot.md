# OpenAI Shadow Ledger — Bot Reference

Complete reference for the OpenAI usage-tracking Telegram bot.
Covers behaviour, polling logic, notification system, commands, personality, and implementation details.

---

## 1. Identity & Personality

| Field | Value |
|---|---|
| **Name** | BachsSlave2Bot |
| **Rank** | Marshal-Rank Shadow Commander |
| **Bound to** | Bach the Monarch |
| **Tone** | Formal, restrained, zero humor, precision in all things |
| **Address form** | Refers to the primary user as "Monarch {name}" or "My Liege {name}" |
| **Persona source** | Shared with the HERMES bot — same universe, same oath |

The bot speaks as a loyal military intelligence officer. Its reports are structured and terse. Its alerts escalate in drama proportional to severity. It does not apologize, does not filler-talk, and does not repeat itself unless the situation demands it.

---

## 2. What the Bot Does

Tracks OpenAI API token and cost usage across every project of **two OpenAI
organizations** — *Business AI Lab 2* and *Business AI Lab 3* (see §2b) — and
enforces their free daily quotas. Reports to Telegram via automatic push alerts
and on-demand pull commands.

**Role 1 — Autonomous monitor (push)**
Polls the OpenAI Admin API on a dynamic schedule.
On each poll:
- Fetches today's token usage per project, broken down by model
- Fetches today's cost per project
- Updates that org's persistent state (`bot_data/usage_state.json` for Lab 2, `usage_state_lab3.json` for Lab 3)
- Fires milestone alerts, overcap broadcasts, or concurrency alerts as needed

**Role 2 — Command responder (pull)**
Long-polls Telegram `getUpdates`. When a message starts with `@BachsSlave2Bot <cmd>`,
dispatches to the matching handler and replies to that chat/thread.

Both roles run as daemon threads (one monitor per org, one command responder
for all). Main thread sleeps until `KeyboardInterrupt`.

---

## 2b. Organizations (Business AI Lab 2 + Lab 3)

| | Business AI Lab 2 (`lab2`) | Business AI Lab 3 (`lab3`) |
|---|---|---|
| Admin key (`.env`) | `OPENAI_ADMIN_KEY` | `OPENAI_ADMIN_KEY_LAB3` |
| Usage tier | 3+ | 1-2 (new org, created 2026-09-29) |
| Normal: free allowance / ceiling / seal point | 10M / 10M / 9.5M | 2.5M / **10M / 9.5M** |
| Premium: free allowance / ceiling / seal point | 1M / 1M / 800k | 250K / **1M / 800k** |
| Milestone ladders (on the free allowance) | 1M 4M 7M 8M 9M 10M · 200k 500k 800k 1M | same fractions: 250k … 2.5M · 50k 125k 200k 250k |
| State / project cache | `usage_state.json` / `projects.json` (unchanged) | `usage_state_lab3.json` / `projects_lab3.json` |
| Spend alarm (ladders scale with it) | $2.00/day · $0.10 0.50 1.00 1.50 2.00 | **$10.00/day** · $0.50 2.50 5.00 7.50 10.00 |

**Lab 3 pays past its free allowance on purpose** (Bach, 2026-09-30): it has to spend
its way up the usage tiers to reach Lab 2's allowance, so usage beyond 2.5M / 250K is
wanted, up to the same daily room as Lab 2. Each org therefore has two numbers per track:
the **free allowance** (`Org.cap()`: milestones, the "allowance exhausted" alert — which
for Lab 3 adds "by design this org keeps running on paid usage until the seal point" —
and the lane lines) and the **ceiling** (`Org.ceiling()`: seal point = ceiling minus the
track buffer, the wave guard, the watch zone and the overcap "ILLEGAL ACTIVITY" alarm).
For Lab 2 they are equal. Paid usage at both Lab 3 ceilings costs roughly $2–6/day
(Lab 2's measured overage prices: normal ≈ $0.18/M, premium ≈ $1.5–4.6/M), so the $10
alarm still means "something unplanned is billing". To change the policy, edit
`ceiling` / `daily_limit` in `ORG_SPECS`; setting Lab 3's ceiling back to its `free`
values restores the strict free-only seal (2.375M / 200k).

Models, prices, classification and quarantine rules are identical for both orgs.
An org is monitored only if its admin key is set, so removing `OPENAI_ADMIN_KEY_LAB3`
turns the bot back into a single-org bot (no org headers, no org picker).

**Design — one process, strictly separated orgs.** One Telegram bot token allows only
one poller, so a single process serves both orgs. Everything that touches an org is
per-org and passed **explicitly**, never inferred:

- `class Org` (in `ORGS`, built from `ORG_SPECS`) holds the key, caps, seal points,
  milestone ladders, project table + archive index, state/cache paths, the `/refresh`
  wake-up event (`poll_now`) and the busy claim.
- One `UsageStore(path, org)` per org; `usage.org` is an attribute, never part of the
  saved data (`update()` replaces the data every poll).
- Every Admin API call takes the org: org-wide fetchers positionally
  (`fetch_today_usage(org)`, `_fetch_costs_breakdown(org)` …), project-scoped calls as
  a required keyword (`_fetch_project_rate_limits(pid, org=…)`,
  `_update_project_rate_limit(…, org=…)`). `_openai_headers(org)` has no default: a
  call that forgot its org fails loudly instead of using the other org's key. (Lab 2's
  key gets a 404 on Lab 3's projects anyway — a mix-up could never seal the wrong
  org, but it would leave the right one unprotected.)
- **Busy claims are per org** (`_try_claim_busy(org)`): a Lab 2 sweep never delays a
  Lab 3 seal or midnight rollover. Memo keys that are not project-scoped include the
  org (`_REPAIR_DONE`, `_DRIFT_CHECKED`, `_PENDING_ANNOUNCED`); project-keyed memos rely
  on project ids being globally unique.
- The **canonical restore baseline is per org** — Lab 3's org-level ceilings are below
  Lab 2's values, so a pooled baseline would restore Lab 3 rows above its ceilings.
- Threads: one `usage_poll_loop` + one `concurrency_check_loop` **per org**, one
  `telegram_poll_loop` for all.
- **Attribution**: `_broadcast(…, org=…)` requires the org and prefixes
  "🏢 **Business AI Lab N**" to every alert (both orgs send the same alert types, and
  both have a "Default project"). Console lines and intel-log events are tagged from a
  per-thread org context (`_org_context`, set by each org's threads, archive workers
  and pool workers): stdout shows `[lab3] …`, events carry `"org": "lab3"`.
- **Commands** render one labelled section per org (`_per_org`); `/refresh` wakes every
  org's poll loop; the archive menu asks for the org (§7.7).

**Seals take effect late on OpenAI's side.** A zeroed rate-limit row reads 0 at once,
but the API gateway enforces it unevenly for minutes. Measured with real traffic:
- Lab 3, 2026-09-30 (first real seal, 7 × 33k-token `gpt-5.1` requests → 231,257
  premium tokens): the bot saw the whole burst on its first poll ~2 min after it ended
  and sealed in 47 s; yet 5–15 min later a zero-limit model still answered most probes
  (`gpt-5.1`, `gpt-5.4`, `o3`, `gpt-4o`, `gpt-4.1` alternated between 200 and 429).
- Lab 2, 2026-09-26: namvuong-project was blocked for 5 min after its seal, then served
  2,795 `gpt-4o-mini` requests in minutes 5–10, then stopped — no bot action in between.
  The other Lab 2 seals checked (09-23, 09-24, 09-28) cut traffic within 1–2 min.
So the track buffers absorb enforcement lag as well as reporting lag, and nothing the
bot can do shortens it (zeroing the rows is the only reversible lever the Admin API
offers). With Lab 3's ceiling at Lab 2's levels its buffers are Lab 2's measured ones.

---

## 3. Architecture

```
main()
 ├── load .env                          → ORGS = orgs whose admin key is set (lab2, lab3)
 ├── UsageStore(org.state_path, org)    → one per org
 ├── SubscriberStore(bot_data/subscribers.json)   (shared)
 ├── NameStore(bot_data/names.json)               (shared)
 ├── _load_project_cache(org)           → merge each org's projects cache into its table
 ├── print [config] line per org        → poll interval, spend alarm, seal points, projects
 ├── per org: Thread usage_poll_loop()        ← THE enforcement pipeline (starts first — never waits on Telegram)
 ├── per org: Thread concurrency_check_loop() ← concurrent-project detector
 └── Thread: telegram_poll_loop(all stores)   ← command responder for every org

usage_poll_loop(usage)  — one thread per org; one cycle, in this order (seals first; nothing slow ahead of them)
  ├── fetch_today_usage()                  → tokens per project by band (date = the query window's day)
  ├── _overlay_cached_costs()              → last known SAME-DAY costs, so the store never saves $0
  ├── usage.update(snap)                   → day rollover; False = stale snapshot / rollover deferred → skip
  ├── _WaveGuard.observe() + static check  → _handle_track_seal(): normal ≥95%, premium ≥80%, or projected breach
  ├── _process_pending_track_unseals()     → yesterday's seals, per-row, backoff, today-covered rows held
  ├── _fetch_costs_breakdown()             → live costs → _apply_live_costs() (off the critical path)
  ├── seed/check_milestones()              → token milestone alerts
  ├── seed/check_spend()                   → org + per-project $ thresholds, overcap escalation
  ├── check_unlisted_models()              → off-watchlist first-touch alert
  ├── _quarantine_unlisted_users()         → FULL-seals any project using an unlisted model
  ├── _repair_seal_gaps()                  → while mass-sealed: gaps every poll, drift every 5 min
  ├── _handle_overcap()                    → alarm: exempt / leaking projects on an exhausted band
  ├── mode transition logic                → passive / urgent / aggressive
  ├── _sync_projects() (hourly)            → live project list from the Admin API
  └── org.poll_now.wait(interval)          → /refresh wakes it early; busy modes never slower than passive

telegram_poll_loop()
  ├── _telegram_startup()     → getMe + stale-update discard, retried until both succeed
  ├── @bot <command>          → _command_allowed() → dispatch() → (text, keyboard)
  └── inline-button callback  → handle_archive_callback() drives the archive menu

concurrency_check_loop()
  └── every CONCURRENCY_WINDOW_MINS (5 min):
       └── _fetch_recent_activity() → alert if ≥3 projects active simultaneously
                                      (preserves last snapshot on API failure)
```

---

## 4. File Structure

```
OpenAIUsageBot/
├── openai_usage_bot.py        # Entire bot — single file
├── .env                       # Secrets (gitignored)
├── .gitignore
├── docs/
│   ├── bot_blueprint.md       # Legacy design spec
│   └── openai_bot.md          # This file — current reference
└── bot_data/                  # Auto-created on first run (gitignored)
    ├── usage_state.json       # Lab 2: today's snapshot + mode/seal state
    ├── usage_state_lab3.json  # Lab 3: same shape
    ├── projects.json          # Lab 2 project table cache (§9)
    ├── projects_lab3.json     # Lab 3 project table cache
    ├── subscribers.json       # Subscribed chat IDs
    ├── names.json             # Per-chat display names
    └── logs/                  # Local intel log (durable history)
        ├── events-YYYY-MM.jsonl   # Structured events: every broadcast + state change
        └── stdout-YYYY-MM.log     # Raw console output (tee'd by run script)
```

---

## 5. Configuration

### `.env` file
```
OPENAI_ADMIN_KEY=sk-admin-...      # Business AI Lab 2 admin key (OpenAI Platform)
OPENAI_ADMIN_KEY_LAB3=sk-admin-... # Business AI Lab 3 admin key (optional — omit to monitor Lab 2 only)
TELEGRAM_BOT_TOKEN=...             # From @BotFather
TELEGRAM_CHAT_ID=-100xxxxxxxxx     # Primary chat (group/channel/private)
TELEGRAM_ALLOWED_CHAT_IDS=         # Optional, comma-separated: other chats allowed to `arise`
POLL_INTERVAL_MINS=1               # Passive-mode interval (code default 30; production runs 1)
```

The bot prints its effective settings at startup as one `[config]` line per org (poll
interval, spend alarm, seal points, number of projects known), plus a line for any org
whose key is not set. The run script no longer prints
its own guesses — it used to show "Poll: 60 min" while the bot polled every minute.

### Hardcoded constants (edit source to change)

| Constant | Default | Purpose |
|---|---|---|
| `ORG_SPECS` | lab2, lab3 | Per org: id, label, admin-key env var, `free` allowance, enforcement `ceiling`, `daily_limit`, state + cache files (§2b) |
| `DAILY_LIMIT` | $2.00 | Base spend alarm (Lab 2's); each org's `daily_limit` scales the three ladders below |
| `SPEND_MILESTONES` | $0.10/0.50/1.00/1.50/2.00 | Base org-wide spend alert ladder (× daily_limit / $2) |
| `PROJECT_SPEND_THRESHOLDS` | $0.25/0.50/1.00 | Base per-project spend thresholds (Lab 3: $1.25/2.50/5.00) |
| `SPEND_OVERCAP_STEP` | $0.50 | Base escalation step past the cap (Lab 3: $2.50) |
| `NORMAL_MILESTONE_FRACTIONS` | .1 .4 .7 .8 .9 1.0 | Normal milestone ladder as fractions of the org's cap |
| `PREMIUM_MILESTONE_FRACTIONS` | .2 .5 .8 1.0 | Premium milestone ladder as fractions of the org's cap |
| `PASSIVE_INTERVAL_SECS` | 30 min | Passive-mode poll interval (`POLL_INTERVAL_MINS`; production: 1 min) |
| `PASSIVE_BACKOFF_MAX` | 2 h | Max backoff on consecutive API failures |
| `URGENT_INTERVAL_MIN` | 3 min | Starting poll interval in urgent/aggressive mode (never above passive) |
| `URGENT_INTERVAL_MAX` | 10 min | Maximum poll interval in urgent/aggressive mode (never above passive) |
| `URGENT_INTERVAL_STEP` | 1 min | Interval increment per poll in urgent/aggressive mode |
| `URGENT_REVERT_SECS` | 1 h | Time without new milestone before reverting to passive |
| `AGGRESSIVE_REVERT_SECS` | 1 h | Time since last illegal project before reverting to passive |
| `CONCURRENCY_THRESHOLD` | 3 | Projects active simultaneously to trigger concurrency alert |
| `CONCURRENCY_WINDOW_MINS` | 5 | "Active" window for concurrency check (narrow, real-time use) |
| `CONCURRENCY_COOLDOWN` | 900 s | Min gap between concurrency alerts |
| `OVERCAP_WINDOW_MINS` | 20 | Activity window for overcap detection (wider, accounts for ingestion lag) |
| `NORMAL_SEAL_REMAINING_PCT` | 0.05 | Normal-band buffer → seals at 95% |
| `PREMIUM_SEAL_REMAINING_PCT` | 0.20 | Premium-band buffer → seals at 80% (sized to the measured blind spot — §7.7) |
| `Org.normal_threshold` | 9.5M / 9.5M | Derived per org: ceiling × (1 − remaining_pct), normal band |
| `Org.premium_threshold` | 800k / 800k | Derived per org: ceiling × (1 − remaining_pct), premium band |
| `WAVE_LOOKAHEAD_SECS` | 1200 | Predictive-seal projection horizon (ingestion lag + sweep) |
| `WAVE_RATE_WINDOW_SECS` | 600 | Sliding window for burn-rate measurement (dilutes ingestion chunks) |
| `WAVE_MIN_UTILIZATION_PCT` | 0.60 | Predictive seal armed only above this fraction of cap |
| `WAVE_CONFIRM_POLLS` | 2 | Consecutive over-cap projections required to fire |
| `WAVE_WATCH_SLEEP_SECS` | 60 | Poll cadence inside the watch zone |
| `SEAL_SWEEP_WORKERS` | 4 | Parallel per-project workers for mass seals and restores |
| `QUARANTINE_TRACK` | `"full"` | Pseudo-track holding full-project quarantine seals (§7.5) |
| `DRIFT_VERIFY_SECS` | 300 | How often believed-sealed projects are re-verified against the API |
| `ROLLOVER_DEFER_MAX_SECS` | 600 | Max wait for an in-flight seal op before forcing the day rollover |
| `QUARANTINE_RETRY_SECS` | 180 | Backoff before retrying a failed quarantine |
| `PENDING_RETRY_MAX_SECS` | 1800 | Backoff ceiling for a failing midnight restore |
| `PENDING_RETRY_ALERT_AFTER` | 6 | Failed restore attempts before a one-time alert |
| `PROJECT_SYNC_SECS` | 3600 | How often the live project list is pulled from the Admin API |

### Admin key requirement
Regular `sk-...` keys cannot access `/v1/organization/*` endpoints.
Must use an **Admin API key** (`sk-admin-...`):
Platform → Organization → API Keys → Create Admin Key

### Model classification

**Source of truth** — re-check whenever OpenAI updates the offer:
<https://help.openai.com/en/articles/10306912-sharing-feedback-evaluation-and-fine-tuning-data-and-api-inputs-and-outputs-with-openai>
(section *"What models are included in this offer?"*). **Last synced: 2026-09-22** — reverified, no models added/removed/moved between groups since 2026-08-21.

`_track_for_model(model)` is the single source of truth in code: it returns
`"normal"`, `"premium"`, or `None` (unlisted). A model matches a listed name only
if it is (a) the **exact name**, or (b) the name plus a **`-YYYY-MM-DD` snapshot
suffix** — `_is_listed_variant`.

OpenAI publishes **dated snapshots** (`gpt-5.4-2026-03-05`); the tuples store the
**base name**, so one entry covers every dated snapshot of that model while
same-prefix paid products stay unlisted. Bare aliases resolve too, because the
usage API sometimes reports `gpt-4o` rather than `gpt-4o-2024-08-06`.

| Group | Cap (tier 3+) | Cap (tiers 1-2) | Models |
|---|---|---|---|
| **Premium** | 1M/day | 250K/day | `gpt-5.6-sol`, `gpt-5.5`, `gpt-5.4`, `gpt-5.2`, `gpt-5.1`, `gpt-5.1-codex`, `gpt-5-codex`, `gpt-5`, `gpt-5-chat-latest`, `gpt-4.5-preview`¹, `gpt-4.1`, `gpt-4o`, `o3`, `o1-preview`, `o1` |
| **Normal** | 10M/day | 2.5M/day | `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.4-mini`, `gpt-5.4-nano`, `gpt-5.1-codex-mini`, `gpt-5-mini`, `gpt-5-nano`, `gpt-4.1-mini`, `gpt-4.1-nano`, `gpt-4o-mini`, `o4-mini`, `o1-mini`, `codex-mini-latest` |

¹ deprecated and shut down 2025-07-14; kept for completeness.

Quota is **shared across each group**. OpenAI excludes from the offer regardless
of model name: **fine-tuned models, fine-tuning training, evals, and tool use** —
so `ft:*` classifies as unlisted (and therefore triggers quarantine).

> **Tier caveat**: allowances depend on the org's usage tier — 10M / 1M on tier 3+,
> 2.5M / 250K on tiers 1-2. They are set per org in `ORG_SPECS` (Lab 2 is tier 3+,
> Lab 3 is tier 1-2). If an org changes tier, update its caps there, or the bot will
> seal it at the wrong point (4× too late on a tier-1-2 org set to 10M / 1M).

**Any other suffix means a different paid product and classifies as `None`**:
`o1-pro` ($150/$600 per 1M), `gpt-5.5-pro`, `gpt-5.4-pro`, `o3-pro`,
`gpt-4o-mini-tts`, `gpt-4o-transcribe`, `gpt-5.4-cyber`, `gpt-5.2-chat-latest`,
`gpt-5-search-api`, `gpt-5.3-codex`, etc. An earlier version used loose prefix
matching (`m.startswith(p + "-")`), which silently counted `o1-pro` usage toward
the premium free bucket. The strict rule is deliberately conservative in the
cheap direction: misclassifying toward *unlisted* costs an alert and a
quarantine (reversible); misclassifying toward *listed* silently absorbs
standard-rate spend into the "free" bucket.

There is **no heuristic fallback**. Unlisted models are not counted toward either
token bucket, are never touched by track seal/unseal, and trigger the
off-watchlist anomaly alert **and an automatic full-project quarantine** (§7.5)
on first use each day.

**When OpenAI updates the lists**, edit `NORMAL_MODEL_PREFIXES` /
`PREMIUM_MODEL_PREFIXES` (base names only), update the table above and the
`test_model_classification` fixture, and re-run the suite. Note that a model
*joining* a free list retroactively reclassifies that day's usage into the track
buckets — which can push a track over its cap the moment the bot restarts.

---

## 6. Polling Modes

The bot operates in one of three modes at any time. Mode is persisted in `usage_state.json`
and survives restarts.

### Passive Mode
**Trigger:** Default state at day start, or revert from urgent/aggressive.
**Poll interval:** `PASSIVE_INTERVAL_SECS` (default 30 min), with exponential backoff up to 2 h on consecutive API failures.
**Behaviour:** Polls tokens + costs. Fires milestone alerts on threshold crossings.
Checks for cap breach on every poll — if active illegal projects are found, switches immediately to aggressive.

### Urgent Mode
**Trigger:** A token milestone threshold is crossed while under the daily cap.
**Poll interval:** Starts at 3 min, increases by 1 min per poll, caps at 10 min — and
is **never longer than the passive interval**. With production's `POLL_INTERVAL_MINS=1`
the stepping used to make urgent/aggressive poll *slower* than passive, exactly when
usage was high (September log: 9,412 one-minute polls vs 169 ten-minute urgent polls);
now every mode polls at least once a minute there.
Hitting a new milestone within the 1-hour window **resets the interval back to 3 min**.
**Revert condition:** 1 hour passes without any new milestone → reverts to passive
(also while a cap is exceeded — that branch used to shadow the revert, so urgent
lasted until midnight).
**Behaviour:** Same as passive, but at a shorter interval. Cap breaches still trigger aggressive.

### Aggressive Mode
**Trigger:** Either cap is exceeded **and** at least one project has recent activity
**on the EXCEEDED band** within the last 20 minutes (`OVERCAP_WINDOW_MINS`). Per-band
filtering means premium-track usage does not raise the alarm when only the normal cap
is exceeded, and vice versa. Projects **sealed on that band within the window are
skipped** — their counted requests may all predate the seal. Before this, every mass
seal was followed ~1 min later by a false "ILLEGAL ACTIVITY — HALT ALL OPERATIONS"
naming projects that had just been sealed (09-26, 09-28, 09-29). After the window,
activity from a sealed project is a real leak and does alert.
**Poll interval:** Same 3→10 min progression as urgent mode (capped at passive).
**Behaviour:** On every poll, fetches banded recent activity, filters to projects with
usage on the exceeded band(s), and broadcasts an urgent red-tone alert listing each
project with its per-band request counts. No cooldown — broadcasts fire every poll
while illegal activity is detected.
**Revert condition:** 1 hour passes since the last poll that found active illegal-band activity.

### Mode Transition Summary

```
Startup / new day  →  passive
passive            →  urgent      on milestone crossed
passive            →  aggressive  on cap exceeded + active projects found
urgent             →  passive     after 1 h without new milestone
urgent             →  aggressive  on cap exceeded + active projects found (interval resets)
aggressive         →  passive     after 1 h since last illegal project spotted
```

### /refresh
`/refresh` replies with fresh token / lane / spend numbers for **every org** and
**wakes every org's poll loop** (`org.poll_now`), each of which runs one full cycle immediately — milestones, spend, quarantine,
seals, repair, overcap and mode, exactly as a scheduled poll. `/refresh` itself never
writes day state, seals, or broadcasts.

It used to re-implement that pipeline on the Telegram thread and had drifted from it:
a stale "Premium passed 85%" label (seals at 80%), a quarantine worker grabbing the busy
claim so the due track seal silently deferred while the reply said "mass throttle
started", zero-cost snapshots saved to the store, milestone/mode handling the poll loop
never saw, and a blocking activity call on the Telegram thread. One pipeline means
nothing left to drift.

---

## 7. Notification System

### 7.1 Startup Milestone Seeding
On the **first poll of each UTC day** (including after a bot restart), `seed_milestones()` runs.
It fires **exactly one notification per track** — the highest threshold already crossed — so the
user knows the current status without being flooded.

If a track hasn't crossed its first threshold, no notification is sent for that track.
All lower thresholds in the same track are silently marked so they don't re-fire later.

Example: normal tokens at 7.5M → fires the 7M milestone only, silently marks 1M and 4M.

Subsequent polls use `check_milestones()` which only fires on **newly crossed** thresholds.

**Idempotency**: `seed_milestones()` self-guards via `usage.has_seeded()` and returns
immediately if the day has already been seeded. This closes the race where the Telegram
thread runs `/refresh` before the usage poll thread's first iteration: whichever fires
first does the seed; the second is a no-op. The flag is reset by day rollover (handled
inside `UsageStore.update()` and on stale-date load in `UsageStore.__init__`).

### 7.2 Normal-Band Token Milestones (10M/day free on Lab 2 · 2.5M on Lab 3)
Applies to OpenAI's listed normal-band models:
`gpt-5.4-mini`, `gpt-5.4-nano`, `gpt-5.1-codex-mini`, `gpt-5-mini`, `gpt-5-nano`,
`gpt-4.1-mini`, `gpt-4.1-nano`, `gpt-4o-mini`, `o1-mini`, `o3-mini`, `o4-mini`,
`codex-mini-latest`.
Any other model with `mini` or `nano` in its name is also treated as normal-band.

| Threshold | Level | Tone |
|---|---|---|
| 1M, 4M, 7M | casual | Informational — within safe range |
| 8M, 9M | urgent | Warning — approaching the cap |
| 10M | cap | Cap reached — free tier exhausted, billing starts |

Each threshold fires **once per UTC day**. Crossing the cap milestone arms the
overcap detector — subsequent polls broadcast aggressive alerts as long as
projects keep burning normal-band tokens.

### 7.3 Premium-Band Token Milestones (1M/day free on Lab 2 · 250K on Lab 3)
Applies to OpenAI's listed premium-band models:
`gpt-5.4`, `gpt-5.2`, `gpt-5.1`, `gpt-5.1-codex`, `gpt-5`, `gpt-5-codex`,
`gpt-5-chat-latest`, `gpt-4.1`, `gpt-4o`, `o1`, `o3`.
Unknown full-size models (no `mini`/`nano` in the name) also count here — conservative
default for free-tier alerting.

| Threshold | Level | Tone |
|---|---|---|
| 200k, 500k | casual | Informational |
| 800k | urgent | Nearing the 1M cap |
| 1M | cap | Cap reached — billing starts |

### 7.4 Overcap Active-Project Alerts (Aggressive Mode)
**Condition:** Cap exceeded **and** at least one project has activity in the EXCEEDED
band within the last 20 min (`OVERCAP_WINDOW_MINS`).
**Frequency:** Every poll while a cap is exceeded (never slower than the passive interval) — no cooldown.
**Tone:** Red, urgent, named-project list with per-band request counts, explicit "halt" command.

Crucial detail: the filter is per-band. If only the **Normal (10M)** cap is exceeded,
a project burning only premium-band tokens does **not** trigger the alarm — premium is
still under its 1M allowance. The bot fetches recent activity grouped by both project
and model, classifies each model into a band, and then keeps only projects with usage
on the exceeded band(s).

The 20-min window (vs. 5 min for concurrency) absorbs OpenAI's 5–15 min ingestion lag —
a 5-min window would miss activity that completed 9 min ago and hasn't shown up yet.

The same window is why **just-sealed projects are skipped** (`_recently_sealed()` →
`grace` in `_filter_to_exceeded_band()`): for 20 min after a project's seal on the
exceeded band, its counted requests may all predate the seal. Without this the alert
fired ~1 min after every mass seal, naming projects that had just been sealed (09-29:
"duyanh-project — 758 normal reqs", 70 s after its seal). After the window, activity
from a sealed project is a genuine leak and alerts. A quarantine covers both bands.

Format (example: only the normal cap exceeded; phongnguyen's premium-only usage is correctly suppressed):
```
🔴 ‼️ BUDGET BREACHED — ILLEGAL ACTIVITY DETECTED ‼️

The Normal (10M) free-tier allowance is exhausted.
These projects are still burning the exhausted band — every request now bills:

🚨 khonlanh-project  —  142 normal reqs in the last 20 min
🚨 ngjabach-project  —  7 normal reqs in the last 20 min

HALT ALL NON-ESSENTIAL OPERATIONS IMMEDIATELY.
(Activity window: last 20 min — accounts for API ingestion lag)
Monarch Bach — the treasury is bleeding. Your command is required at once.
```

When both caps are exceeded the entry shows a combined breakdown
(e.g. `7 normal + 3 premium reqs`).

### 7.5 Daily Spend Alerts & Off-Watchlist Anomaly Detection

The token-milestone path (§7.2, §7.3) covers OpenAI's **two free-tier model lists** only.
Models outside those lists — embeddings, image generation, audio, fine-tuned models,
gpt-3.5 — bill at **standard rates from the first token** and are deliberately untouched
by `_track_for_model()` / the seal logic. A real incident in 2026 had $6 of embedding
spend go undetected this way (caught only when the wallet emptied). The spend-monitoring
layer closes that gap.

Three independent dimensions, all alarm-only (no auto-seal — see §7.5 note below):

| Dimension | Thresholds | Dedup unit | Source |
|---|---|---|---|
| **Org-wide cumulative spend** | $0.10 / $0.50 / $1.00 / $1.50 / $2.00 (= `DAILY_LIMIT`) | per-threshold per-day | `_fetch_costs_breakdown()` summed |
| **Per-project spend** | $0.25 / $0.50 / $1.00 | per-(pid, threshold) per-day | `_fetch_costs_breakdown()` by project |
| **Unlisted-model first-touch** | any non-zero usage | per-(pid, model) per-day | `_fetch_tokens()` model breakdown |

**Quarantine — auto full-seal on ANY off-watchlist usage.** Alerting alone proved
insufficient: unlisted models can't be selectively track-throttled, so the bot now
seals the offending project **entirely**. `_quarantine_unlisted_users()` runs every
poll: any KNOWN project with non-zero usage of an unlisted model today gets
`_full_seal_project()` — **every** rate-limit row POSTed to 0 (embeddings, gpt-5.6,
audio — all model rows accept 0), healthy originals captured under
`sealed_tracks["full"]` (the `QUARANTINE_TRACK`). Broadcast: "☣️ QUARANTINED".

- **Dedup / self-healing**: presence in `sealed_tracks["full"]` is the memo; a
  failed seal retries after `QUARANTINE_RETRY_SECS` (3 min — it used to retry every
  60 s poll, i.e. a GET plus up to ~190 POSTs a minute for a persistently failing
  project), even if its write-ahead captures left it recorded; a `noop` (no rows) is
  memoized in-memory per day.
- **Release**: `/archive` → Unseal → **Both** → project (or ALL) also lifts the
  quarantine — restores all rows and exempts the project from re-quarantine for
  the rest of the UTC day (`track_exemptions[pid] ⊇ ["full"]`). Off-watchlist
  spend after a release is a deliberate human decision.
  - ⚠️ **"Both" also exempts the token caps.** The gesture runs the normal and
    premium manual-unseal too, which *pre-exempt* the project from those auto-seals
    for the day even when neither was sealed — so a release yields
    `track_exemptions[pid] = ["normal", "premium", "full"]`. On 2026-09-15
    namvuong-project was released for audio use and was then skipped by the normal
    mass-seal, burning 3.72M normal tokens past the cap. Kept deliberately
    (Bach's call, 2026-09-16): a release is treated as full trust for the day. To
    release a quarantine *without* waiving the caps, re-seal the lanes afterwards
    via archive → Seal.
- **Midnight**: the `"full"` entry flows through the standard rollover →
  `pending_track_unseal` → restore path, like any track seal.
- **Rows already at 0** (e.g. track-sealed earlier the same day) are throttled
  again harmlessly but never captured — the 0/0-cascade guard applies.
- Archive status marks quarantined projects with ☣️.

**Cap behaviour** — when the org total first crosses `DAILY_LIMIT` ($2/day), the bot:
- broadcasts the cap milestone alert,
- flips to **AGGRESSIVE** mode (3-min polling) and **holds it while spend ≥ cap**
  (spend never decreases intraday, so in practice until the midnight reset —
  an earlier version reverted to passive on the very next poll),
- does **NOT** auto-mass-seal at the org level: off-watchlist culprits are already
  handled per-project by the quarantine above, and an org-wide seal would throttle
  legitimate listed-model use without adding protection. Manual
  `/archive` → Seal → Both → ALL remains one click away.

**Overcap escalation** — past the cap, a fresh cap-level alert fires every extra
`SPEND_OVERCAP_STEP` ($0.50): at $2.50, $3.00, $3.50, … Dynamic thresholds share the
same per-day notified list as the static ones, so each fires exactly once. `seed_spend()`
marks already-crossed escalation steps silently on restart (one catch-up broadcast for
the highest crossed level only). The bot is never silent while the bleed continues.

**Seed-on-first-poll** — `seed_spend()` runs once per UTC day (atomic via
`claim_spend_seed()`). On bot restart mid-day it marks every already-crossed threshold
as notified — silent — then fires ONE catch-up broadcast for the highest crossed level.
Same pattern as token `seed_milestones()`. Prevents the "$1.50 already? fire all five
back-to-back" restart flood.

**Cost-fetch latency** — OpenAI's `costs` endpoint has a 5–10 min ingestion lag. Spend
alerts may arrive that delayed, but that's far better than the previous "wait until the
wallet is empty" detection window.

### 7.5b Per-lane cost + the Exotic lane

Every token report shows **billed cost per lane** next to the token counter, plus a
third lane — **Exotic** — for all spend outside the two free-tier lanes:

```
   ⭐ Premium (1M): 0 / 1M. Cost: 0.00$
   📦 Normal (10M): 11.39M / 10M. Cost: 0.21$
   🧪 Exotic: 0.12$
Spend today:  $0.3283
```

Rendered by `_fmt_lane_lines()` in `/refresh`, `/usage`, the daily
snapshot, and the archive status; the console poll line appends `exotic=$X` when
non-zero, and every intel-log `poll` event records the split under `lanes`.

**Attribution is exact, not estimated.** `_fetch_costs_breakdown()` makes one
costs-API call grouped by `project_id` **and** `line_item`. Line items look like
`gpt-audio-mini-2025-12-15 audio, input` — `_lane_for_line_item()` takes the model
token before the comma and classifies it with `_track_for_model()`. Anything that
is not a normal/premium model — including non-model items such as web search or
storage — is **exotic**, so the three lanes always sum to the total (verified live
2026-09-15: 0.206513 + 0 + 0.121780 = 0.328293).

- **Free lanes read `0.00$` until their allowance is exhausted** — the costs API
  reports actual billing, so only overage appears.
- **Exotic shows cost only** — it has no free allowance to count tokens against.
- **`_fmt_cost()` never renders real spend as zero**: sub-cent amounts keep four
  decimals (`0.0043$`), since surfacing small off-watchlist spend is the Exotic
  lane's whole purpose. `—` means no cost data yet (before the first fetch).
- The split is cached in `costs_cache.per_lane`, so a failed costs fetch still
  shows the last known lanes (marked stale).

### 7.6 Concurrent Project Alerts
**Condition:** ≥ 3 projects active simultaneously in the last 5 minutes.
**Frequency:** At most once per 15 minutes (`CONCURRENCY_COOLDOWN`).
**Source:** `concurrency_check_loop` thread, runs every 5 min independent of poll mode.

### 7.7 Sealing — track-level mass throttle + interactive manual control

The bot prevents track-cap breaches by **mass-throttling every project's rate
limits for a track** as soon as that track passes its seal threshold (normal 95%,
premium 80%) or the wave guard projects a breach. Sealing uses
`POST /v1/organization/projects/{pid}/rate_limits/{rate_limit_id}` because the
project `/archive` endpoint is one-way and cannot be reversed via API.

Only models on OpenAI's two free-tier lists are touched. `_track_for_model()`
strict-matches each model to `normal` / `premium` / `None`; unlisted models
(sora-2, babbage-002, dall-e-3, etc.) are never throttled — they bill at standard
rates regardless, so throttling them is pointless.

#### Auto trigger — track-level mass seal + wave guard

Constants:
```
NORMAL_SEAL_REMAINING_PCT     = 0.05  → Org.normal_threshold  = 9,500,000 (Lab 2, 500k buffer) · 2,375,000 (Lab 3, 125k)
PREMIUM_SEAL_REMAINING_PCT    = 0.20  → Org.premium_threshold =   800,000 (Lab 2, 200k buffer) ·   200,000 (Lab 3,  50k)
WAVE_LOOKAHEAD_SECS           = 1200  (20 min: API ingestion lag + sweep time)
WAVE_WATCH_BAND_PCT           = 0.10  (watch zone starts 10% of cap below threshold)
WAVE_WATCH_SLEEP_SECS         = 60
SEAL_SWEEP_WORKERS            = 4
```

**Why per-track buffers ("the wave"):** usage numbers are 5–15 min stale when the
bot sees them, the sweep takes time, and in-flight requests land after throttling.
What the buffer really has to absorb is the **reporting blind spot** — tokens
already spent when the seal fires but not yet visible to the API.

Measured blind spot = *(end-of-day total) − (total observed when the seal fired)*:

| Date | Seal fired at | End of day | Blind spot | Result |
|---|---|---|---|---|
| 2026-08-19 | 911,516 | 954,270 | 43k | under cap |
| 2026-08-20 | 895,212 | 1,032,623 | 137k | 33k over — $0.26 |
| 2026-08-26 | 854,438 | 1,020,876 | **166k** | 21k over — $0.097 |

The 15% buffer (150k) fell ~21k short of the worst case on 2026-08-26 *even with
all 13 projects sealed and zero failures* — proof the leak is reporting lag, not
seal failure. Premium therefore seals at **80% (200k buffer)**, clearing the worst
observed blind spot by 34k. This costs nothing in practice: across 14 days no day
ever ended between 800k and 850k, and every day that reached 850k blew past 1M
anyway.

**Normal keeps 95% — but its buffer has now been outrun once.** On 2026-09-15 the
usage API jumped **8.88M → 10.08M in a single reporting interval** (~1.2M tokens in
~7 min, ~2,900 tok/s — 26× the premium wave). The first reading past the 9.5M
threshold was already past the 10M cap, so the 500k buffer never got a chance; the
day ended at 11.39M. It cost only **$0.21** because normal-lane overage is cheap.
The threshold is unchanged for now: it is a single data point, and namvuong-project
was exempt that day (see the §7.5 release note), so its burn contaminates the
post-seal blind-spot measurement. **Resize if it recurs.**

**If overshoot recurs**, re-derive the blind spot from the intel log
(`day_rollover.final_premium` vs the `mass_seal.consumed` of the same day) and
widen the buffer past the new maximum.

Three trigger paths, all idempotent via the per-day `mass_sealed_tracks` flag:

1. **Static threshold** — `usage_poll_loop()` and `cmd_refresh()` check
   `tokens ≥ threshold` after every poll.
2. **Predictive (wave guard, `_WaveGuard`)** — projects each track forward by
   `WAVE_LOOKAHEAD_SECS` at the measured burn rate; if the projection crosses
   the **cap**, the seal fires even below the static threshold. Logged as
   `wave_trigger` with rate + projection. Because OpenAI ingestion is lumpy
   (delayed data lands in chunks — one chunk read as ~450 tok/s and falsely
   sealed premium at 46% on 2026-08-14), the trigger is gated four ways:
   rate is measured over a **2–10 min sample window** (chunks dilute), the
   **last poll interval must also project over cap** (a window stays hot after
   a wave dies; a chunk is hot for exactly one interval — only sustained waves
   pass both), it is **armed only ≥60% utilization**, and it needs the
   projection over cap on **2 consecutive polls**.
3. **Watch zone** — while an unsealed track is within 10% of cap below its
   threshold, the poll sleep is clamped to 60 s, overriding urgent mode's
   3→10 min stepping (which used to reopen the detection gap at the worst time).

#### Sweep execution — parallel + self-healing

`_mass_seal_track()` throttles projects **in parallel** (4 workers; projects are
independent, 50 ms inter-POST spacing preserved within each project) — ~1 min for
13 projects instead of ~5. Failed projects get one **in-sweep sequential retry**.
Projects are ordered heaviest-first from the **stored snapshot** (no API call), and
the "begin sealing" broadcast goes out only after the workers are already POSTing —
both used to sit in front of the first seal (the activity fetch could take ~48 s
with retries on a bad link).

**Write-ahead captures (`_seal_rows`).** Every seal records the healthy pre-seal
values *before* its first POST. If a POST fails, the rows already zeroed are rolled
back, and the captures are dropped only if that rollback **fully** succeeded. The
old order (capture after the last POST, rollback result ignored) could strand rows
forever: a DNS outage longer than the retry window failed row *k* and its rollback,
rows 0..k-1 stayed at 0/0 with no capture, and midnight only restores captured
rows. Now a half-applied seal stays recorded — drift repair zeroes the rest, the
midnight restore reopens every touched row, and a manual Seal on a project recorded
as sealed verifies it against the API instead of answering "noop".

Restores use the same pool via `_run_per_project()`: both the manual
`_mass_unseal_track()` and the midnight `_process_pending_track_unseals()`. They
stayed sequential long after the seal went parallel, so unsealing 13 projects × ~19
rows took 5–10 min against ~1.5 min to seal (measured 2026-09-28/29: 5 min, 5 min,
10.5 min). Each worker exempts its project immediately after restoring it, so the
restore-then-exempt window is no wider than it was sequentially.

**`_repair_seal_gaps()`** runs on every poll while a track is mass-sealed —
including early wave-guard seals below the static threshold, which used to go
unverified until usage reached it (09-27: a 7.98M seal went unchecked until 9.97M) —
and heals **two** distinct failure modes:

1. **Gap** — a project missing from `sealed_tracks` because its seal failed
   mid-sweep (observed 2026-08-13: the straggler kept burning post-cap for hours).
   Re-sealed, then memoized per-(day, track) so settled projects aren't re-POSTed
   every poll; failures stay unmemoized and retry next poll.
2. **Drift** — a project the bot *believes* is sealed whose limits are actually
   healthy again (found 2026-08-22 during a smoke test: state and reality
   disagreed and nothing ever noticed, so the bot was confidently wrong while a
   project could burn freely). Believed-sealed projects are therefore **verified
   against the live API** and re-zeroed on drift. Never memoized — drift can recur
   at any time — but verified every `DRIFT_VERIFY_SECS` (5 min), not every poll:
   at 60 s polling the old cadence cost ~13 GETs a minute per sealed track for the
   rest of the day (~11k calls/day, each a DNS-failure opportunity), and the one
   live drift ever seen was found 13 min after its seal. The first poll after each
   sweep always verifies.

Drift re-seal **merges** captures via `merge_track_originals()` rather than
overwriting: only some rows may have drifted, and replacing the capture list
wholesale would discard the originals of rows still at 0 — the 0/0 cascade in a
new disguise.

#### Org rate-limit ceiling on restore

`GET` can report a project rate-limit value that `POST` then refuses, because the
**organization-level** ceiling for that model is lower (`400
organization_rate_limit_exceeded`). Observed live 2026-08-22 on every `*-pro` row.
Before the fix this made the whole restore return `failed`, so the project was
never removed from `sealed_tracks` — **a quarantine could never be lifted, and the
midnight queue would retry and fail forever**.

`_update_project_rate_limit()` now parses the ceiling out of the error message and
retries with that field clamped — **iteratively**, because the API names only one
offending field per response and a row can exceed on several (`max_requests…`
first, then `max_tokens…`). After 4 rounds it soft-skips: one unrestorable row
must never strand an entire project in a sealed state.

#### What a mass throttle does

`_mass_seal_track(track)` runs under a single global busy claim so no other
seal/unseal can interleave. Auto-sweep & day-rollover restore run inline in the
poll thread; manual archive ops run in a background worker thread (see "Race
safety" below).

1. Marks the track in `mass_sealed_tracks` (auto-sweep idempotency).
2. Orders that org's projects by today's usage on that track (descending, from the stored snapshot — no API call) so heavy spenders are throttled **first**.
3. Submits the parallel workers, then broadcasts a concise "begin sealing…" banner (so the Telegram send never delays the first POST).
4. For each project: skips if exempt for that track or already sealed; otherwise GETs its rate limits, filters to rows where `_matches_track(model, track)`, captures the pre-throttle originals, and POSTs `0` to every present field. Originals are saved under `sealed_tracks[track].originals_by_project[pid]`.
5. Broadcasts a concise "done sealing… N throttled (M exempt)" summary — **no per-project chatter**.

Inter-POST spacing is 50 ms (bulk writes without spacing occasionally don't persist on the API side).

#### Race safety — atomic busy claim + background workers

Every operation that mutates rate limits — the auto threshold/wave sweep, the
day-rollover restore, quarantine, seal repair, and every manual button action —
passes through one atomic check-and-set
called `_try_claim_busy()`. Only one operation can hold the claim at a time; every
caller MUST `_release_busy()` in a `finally`. This is intentionally **not** a
blocking lock — callers that cannot claim the flag bail out instead of waiting:

- **Manual button click** (Telegram callback) — refuses immediately with a small
  toast: *"A seal/unseal is already running — try again shortly."* The user can
  click again once the in-flight operation finishes.
- **Auto seal sweep** (poll loop) — defers silently and logs `mass-seal deferred`.
  The next poll re-checks the threshold and retries; since consumption only
  grows, the trigger condition won't disappear.
- **Day-rollover restore** (poll loop) — defers silently and logs
  `pending-track-unseal deferred`. The pending queue is persisted, so the next
  poll picks up where this one left off.

**The day rollover waits for the claim too.** `UsageStore.update()` defers a date
change while `_is_busy()` (bounded by `ROLLOVER_DEFER_MAX_SECS`, then forced). An
operation spanning midnight used to write its captures and exemptions into the NEW
day's state — never restored until the following midnight.

Manual button operations are additionally **dispatched to a daemon worker thread**
so the Telegram poll loop is never blocked by a 30-50 s mass sweep. The callback
toast answers immediately; the **worker** posts the "🔄 Working…" placeholder and
then the final result, so the two edits can't arrive out of order (a fast no-op job
used to finish first and get overwritten by the placeholder, leaving the message
stuck with no buttons). This closes two older bugs:

1. **TOCTOU race** — the old code read `_SEAL_BUSY` *without* the lock, then
   `with _SEAL_LOCK:` inside the heavy function. A click between check and acquire
   would block the Telegram thread for the duration of the running sweep.
2. **Poll-thread freeze** — the old design ran the seal inline. A mass-seal would
   freeze every other Telegram command for 30-50 s.

#### Callback input validation

`handle_archive_callback` validates every field of the
`arch:<org>:<action>:<mode>:<target>` payload against an enum set before using it:

- exactly 5 fields — a 4-field button from before the org step
  (`arch:seal:normal:3`) returns "This menu is outdated — send /archive again."
  It is never mapped to a guessed org: the two orgs' project indices overlap.
- `org`    ∈ `{-}` ∪ monitored org ids — anything else returns "Unknown org."
- `action` ∈ `{cancel, menu, seal, unseal}` — anything else returns "Unknown action."
- `mode`   ∈ `{-, normal, premium, both}` — anything else returns "Unknown mode."
- `target` — must be `"-"`, `"all"`, or a **project id of that org**. Anything else
  (an old index-style button, another org's project) returns "Unknown project." —
  validated before the busy claim is taken, so nothing can strand the claim.

This is defence-in-depth: Telegram already restricts the keyboard to bot-emitted
buttons, but a forged callback (e.g. from a compromised account) cannot trigger a
seal on an unknown project or with an unknown mode.

#### Soft-skip error codes

Some rate-limit rows returned by GET aren't actually updatable. The bot treats these as successful no-ops:
- `rate_limit_does_not_exist_for_org_and_model` — org has no access to that model.
- `rate_limit_not_updatable` — fine-tune / batch-only rows.
- `invalid_rate_limit_type` — model doesn't expose that field (e.g. `sora-2` has no `max_tokens_per_1_minute`).

#### Manual control — `/archive` (interactive buttons)

The command takes **no arguments**. It posts the live status plus an inline keyboard
and drives a small button state machine (messages are edited in place, not re-sent):

```
/archive
   → status of every org + [🔒 Seal] [🔓 Unseal] [✖ Cancel]
        → Seal/Unseal → [🏢 Lab 2] [🏢 Lab 3] [✖ Cancel]          (skipped with one org)
             → org → [📦 Normal] [⭐ Premium] [🔱 Both] [✖ Cancel]
                  → mode → one button per project OF THAT ORG (2 cols) + [🟥 ALL Lab N projects] [✖ Cancel]
                       → project (or ALL) → applies the change on that org, re-renders its status
```

Callback data is `arch:<org>:<action>:<mode>:<target>` (≤ 54 bytes against Telegram's
64-byte cap) where the target is the **project id** — never a list index, which could
point at a different project after a restart (edited seed, lost cache) and make an old
message's button act on the wrong project. Every step after the org choice carries the
org, the id must belong to it, and the operation claims **that org's** busy flag
(a Lab 2 sweep in flight doesn't block a Lab 3 click). Choosing a single project calls
`_manual_seal_project` / `_manual_unseal_project`; choosing **ALL** calls
`_mass_seal_track` (ignoring exemptions) / `_mass_unseal_track` — for that org only.

- A manual **unseal** restores the project's rows for that track to the canonical
  baseline and marks it exempt for the rest of the UTC day (the auto-sweep skips it).
- A manual **seal** clears any exemption and throttles the project's track rows to 0.
- **Exempt projects still get the overcap alarm** — if an unsealed project keeps
  burning post-cap tokens, `_handle_overcap` keeps broadcasting the red-tone alert.
  The user is responsible for the bill.

#### Uniform restore (canonical baseline)

All restores route through `_compute_canonical_baseline(usage)`, which derives — **per
org**, from that org's captures and projects only — one
healthy value per (model, field) by consensus across **state captures first**
(pre-throttle originals in `sealed_tracks` / `pending_track_unseal`) then live API
values. It **never emits a zero** — a field is omitted unless a non-zero value
exists somewhere. This guarantees two things: every project restores to the *same*
per-model limits (uniform), and a restore can never accidentally re-throttle a
project (immune to the 0/0 cascade that bricked projects in an earlier version).

#### Auto-unseal at UTC midnight

`UsageStore.update()` and `__init__()` detect date changes and call
`_reset_daily_state_locked()`, which merges `sealed_tracks` into
`pending_track_unseal` **by row id** (a per-project overwrite used to drop rows still
owed from an earlier day), and clears `mass_sealed_tracks` and `track_exemptions`.
The next poll calls `_process_pending_track_unseals()` (gated by the busy claim) to
drain the queue to the canonical baseline:

- **Per-row bookkeeping** — only rows whose POST failed stay queued; a retry never
  re-POSTs rows already restored.
- **Backoff** — a failing project retries at 2, 4, 8 … up to 30 min instead of every
  60 s poll (which used to mean 13 baseline GETs plus a begin/done broadcast pair
  every minute, forever, for one permanently failing row). After
  `PENDING_RETRY_ALERT_AFTER` (6) failed attempts, one "Restore failing repeatedly"
  alert goes out.
- **Hold rows today's seals cover** — if a project is sealed again today (on the
  row's track, or quarantined), yesterday's capture for that row is held back:
  restoring it would silently reopen a row today's seal zeroed (and never captured,
  since it was already at 0). Held rows go out after today's seal lifts.
- **Quiet retries** — begin/done broadcasts go out on the day's first pass, and
  afterwards only when a retry actually restores something.

**Midnight-straddling polls.** `fetch_today_usage()` takes the date label from the
**same instant** as the query window. The old code computed the window when the fetch
started and stamped `today_str()` after pagination finished. A fetch that began at
23:59:5x therefore returned the old day's totals under the new date. On 2026-09-29
that carried 09-28's 24.88M into the new day, and the bot "crossed 249%" and
mass-sealed all 13 projects for the whole day. The same shape appears on 09-02 and
09-08 (≈1M premium, no seal). If midnight passes mid-fetch, the fetch now re-runs
for the new day. Separately, `UsageStore.update()` **refuses a snapshot older than
the stored day** and returns `False`. Previously any date difference counted as a
rollover, so a late pre-midnight snapshot from another thread would have reset the
day *backward* and queued every live seal for restore. The poll loop checks that
return value and never acts on a refused snapshot (`/refresh` no longer writes state
at all). A related trap is also closed: on the first poll of a new day, a failed
costs fetch used to fall back to *yesterday's* cached costs, and the day's silent
spend seed then marked every threshold up to yesterday's total as already notified —
suppressing today's real crossings. The cache is now only used within the same day.

#### State schema

```jsonc
{
  // Every project currently throttled on a track (mass OR manual share this).
  "sealed_tracks": {
    "normal":  { "sealed_at": 1737000000,
                 "originals_by_project": {
                   "proj_xxx": [ { "id": "rl-gpt-4o-mini", "model": "gpt-4o-mini",
                                   "max_requests_per_1_minute": 5000,
                                   "max_tokens_per_1_minute": 4000000 }, … ] } },
    "premium": { … or absent if no project sealed on premium }
  },
  // Tracks whose mass sweep (threshold or wave) has fired today (auto-trigger idempotency).
  "mass_sealed_tracks": ["normal"],
  // Projects manually unsealed today → auto-sweep skips these tracks.
  "track_exemptions": { "proj_yyy": ["normal"] },
  // Day-rollover restore queue (same shape as sealed_tracks).
  "pending_track_unseal": { … }
}
```

All four fields are preserved across snapshot updates (in `UsageStore._PRESERVED`).
The legacy per-project-full-seal fields (`sealed_projects`, `pending_unseal`,
`manually_unsealed_today`) and the never-wired spend-alert fields (`alert_sent`,
`spend_intervals_notified`) are dropped on load.

#### Latency & blast radius

- **Detection latency**: up to one poll cycle, clamped to 60 s inside the watch zone (an unsealed track within 10% of cap below its threshold). Milestones flip mode to urgent well before any threshold.
- **Per-project throttle cost**: ~50–80 track rows × ~50 ms ≈ a few seconds per project per track.
- **`/refresh` never blocks**: it only reads (both orgs in parallel) and wakes each org's poll loop (`poll_now`); every seal, quarantine and alert happens there. It used to run that pipeline on the Telegram thread and froze every command for minutes (fixed 2026-08-22, removed 2026-09-29).
- **Enforcement lag (OpenAI side)**: a zeroed row reads 0 at once but the gateway can keep serving that model for minutes (up to ~10 min on Lab 2, 15+ min on the brand-new Lab 3 — §2b). The buffers absorb it.
- **Full mass sweep**: 13 projects with 4 parallel workers ≈ ~1 min per track (observed ~5 min sequential on 2026-08-13 — that gap is what let the wave crest during the sweep). Restores (manual "Unseal → All" and the midnight queue) use the same 4-worker pool since 2026-09-29; before that they were sequential and took 5–10 min. Auto-sweep & midnight restore block the **poll** thread for that duration; manual button-driven sweeps run in a daemon worker thread so the Telegram poll thread stays free.
- **Inflight window**: a brief gap between detection and full throttle where running requests complete. Unavoidable — bounded by sweep wall-time, absorbed by the per-track buffer.

---

## 8. Command Reference

Trigger: `/command`, `/command@BachsSlave2Bot`, or a message starting with
`@BachsSlave2Bot` (case-insensitive) followed by the command — or a tap on the menu
buttons under `help` and `refresh`. The main commands are registered with Telegram
(`setMyCommands` at startup), so they appear in the chat's "/" menu. A bare `/command`
the bot doesn't have is ignored (it may belong to another bot in the group).
In groups/topics, the bot respects `message_thread_id` — replies stay in the originating thread.
Non-subscribed chats can only use `arise`, and **only if allowlisted**: the primary
chat (`TELEGRAM_CHAT_ID`) or a chat listed in `TELEGRAM_ALLOWED_CHAT_IDS`. Subscribers
get full seal/unseal control, and `arise` used to subscribe any chat at all — a
stranger could DM the bot, arise, and unseal every project. Refused attempts are
logged (`arise_refused`), never broadcast. Chats already subscribed are unaffected.

**Every report covers every monitored org**, one section per org headed
"🏢 Business AI Lab N", each rendered from that org's own data and caps. Replies over
Telegram's 4096-character limit are split into several messages on line boundaries
(`_send` → `_split_message`); the keyboard rides on the last part.

Since 2026-09-30 the 13 commands are 4 reports + help + chat setup. The old names
still work as silent aliases (`COMMAND_ALIASES`), so habits and pinned messages keep
working.

| Command | Description | Old names |
|---|---|---|
| `refresh` | Fresh numbers for every org (tokens per lane, spend), a "Guard:" line (seals, quarantines, pending restores), the projects active in the last 5 min, and an immediate full poll cycle in each org. Carries the menu buttons. | `active`, `status` |
| `usage` | Today per project, busiest first: tokens, requests, spend and per-model lines (🧪 = off-watchlist model), a by-model total, the lane lines. | `tokens`, `projects`, `rank`, `models` |
| `spending` | Live money view: this month and last month per project, plus the last 31 days' tokens and requests. | `recent`, `bill` |
| `archive` | Show, seal, unseal projects via interactive buttons (see §7.7). | — |
| `help` | The registry plus the menu buttons (`@BachsSlave2Bot` alone shows it too). | `start`, `menu` |
| `arise` | Subscribe this chat to all alerts (allowlisted chats only). Plays the Beru GIF on first subscribe. | — |
| `dismiss` | Unsubscribe this chat (primary chat cannot be dismissed). | — |
| `setname Name` | Set the name the bot uses to address you in this chat. | — |

Menu buttons send `cmd:<name>` callbacks; the reply is posted as a new message in the
same chat/topic. Only subscribed chats, and only the four menu commands, are accepted.

---

## 9. Known Projects

**Business AI Lab 2** (seed `SEED_PROJECTS["lab2"]`)

| Name | Project ID |
|---|---|
| Default project | `proj_Gkm7qFbBFgmW11VFtO13Uw3F` |
| cngvng-project | `proj_9su0tGI8NsaLE7LHqikCw8VE` |
| hoangha-project | `proj_4VPu8UTHzBpZiHFQVaYG923d` |
| namvuong-project | `proj_fvkY21dJ0ripiOIA2jCC86f3` |
| khonlanh-project | `proj_fEboQnaVm4tQCk8kFy0h8s08` |
| phongnguyen-project | `proj_zRWDq4YWIDEkxbgMAjX0xy79` |
| ngjabach-project | `proj_J4rNEXilII2l889OotmE7YNW` |
| oduong-project | `proj_OWrxxJaWk5MXHBi3HIdPxBDh` |
| duyanh-project | `proj_C51oeo4LjmiQefinVfoI8Rs0` |
| minhphung-project | `proj_cEHeqXeLfsJ6jrQhOXDlt9wH` |
| kong-project | `proj_wmeni3BelwvPUahovs5wQy3i` |
| ngocvo-project | `proj_E8F4KEaZSMfBuaPhE3Y69BzM` |
| tubel-project | `proj_MIieWaC8hSsgAp4rSaN86BEp` |
| giaotien-project | `proj_bUTBrctRsITbimsCqGg3VuJR` |

**Business AI Lab 3** (seed `SEED_PROJECTS["lab3"]`)

| Name | Project ID |
|---|---|
| Default project | `proj_zo1iaAChFX81OMGwxRStD76g` |

**The tables above are only the offline seeds.** Per org, the bot pulls the live list
from `GET /v1/organization/projects` with that org's key (active projects only) at
startup and every `PROJECT_SYNC_SECS` (1 h; 5 min retry after a failure), merges it
into `org.projects`, caches it to the org's file (`projects.json` for Lab 2,
`projects_lab3.json` for Lab 3), and broadcasts "🆕 New project(s) detected" (headed
with the org) once per new project. Motivation: giaotien-project (created 2026-09-22) went a week outside every
seal, quarantine and archive button because nobody added it to the hardcoded table.

Merge rules: an org's table and archive-button index (`org.projects`,
`org.project_index`) are **rebound, never mutated** (other threads iterate them; an in-place insert raises "dictionary
changed size during iteration" there), and **append-only** (existing button indices
never shift; renames update the name in place). Names are stripped of `<>&` since
they land in HTML-mode Telegram messages. Projects are never removed automatically.

Keep the seed current anyway (IDs are case-sensitive) — it is what the bot knows if
it boots with the network down and no cache.

---

## 10. Data Persistence

### `bot_data/usage_state.json` (Lab 2) · `usage_state_lab3.json` (Lab 3)
One file per org, same shape. Stores that org's snapshot plus all alert-control and
mode state. Lab 2 kept its original file name, so upgrading to multi-org lost no state.
Fields preserved across snapshot updates (not overwritten by each poll):

| Field | Type | Purpose |
|---|---|---|
| `token_milestones_notified` | list[int] | Normal-band thresholds already alerted |
| `premium_milestones_notified` | list[int] | Premium-band thresholds already alerted |
| `last_concurrent_alert_ts` | float | Timestamp of last concurrency alert |
| `active_projects` | dict | Last concurrency-check activity snapshot |
| `costs_cache` | dict | Last successful cost fetch (per-project + total) |
| `bot_mode` | str | Current mode: "passive" / "urgent" / "aggressive" |
| `mode_entered_ts` | float | When current mode was entered |
| `last_milestone_ts` | float | Timestamp of last milestone hit (urgent revert timer) |
| `last_illegal_seen_ts` | float | Timestamp of last poll with active illegal projects |
| `urgent_poll_step` | int | Current step in 3→10 min interval progression |
| `milestones_seeded` | bool | True after first-poll seed runs; guards the /refresh race condition |
| `sealed_tracks` | dict | track → {sealed_at, originals_by_project: {pid: [rate-limit-rows]}} for every project currently throttled on that track (mass or manual) |
| `mass_sealed_tracks` | list | tracks whose mass sweep (threshold or wave) has fired today (auto-trigger idempotency) |
| `track_exemptions` | dict | pid → list of tracks manually unsealed today; the auto-sweep skips these |
| `pending_track_unseal` | dict | track → {originals_by_project}. Populated by day rollover from `sealed_tracks`, drained next poll |
| `spend_milestones_notified` | list[float] | Org-wide $ thresholds already alerted today (§7.5) |
| `project_spend_notified` | dict | pid → list[float]: per-project $ thresholds already alerted today |
| `unlisted_models_alerted` | dict | pid → list[model]: (pid, model) pairs that already fired the first-touch off-watchlist alert today |
| `spend_seeded` | bool | True after first-poll spend seed runs; same race guard as `milestones_seeded` |

All fields reset at UTC midnight. Two reset paths, both internal:

- `UsageStore.__init__`: on load, if the persisted `date` is older than today, reset
  immediately so commands hitting the store before the first poll don't see yesterday's
  data.
- `UsageStore.update()`: each poll's snapshot carries today's date. If it differs from
  the persisted date, the store auto-resets before merging. This closes the race where
  the Telegram thread runs `/refresh` on a new day before the poll loop notices the
  rollover.

Both paths share `_reset_daily_state_locked()` (private — caller must hold the store lock).

### `bot_data/logs/` — local intel log

Telegram used to be the only place push alerts existed; the intel log mirrors
everything to disk for later analysis.

**`events-YYYY-MM.jsonl`** (monthly rotation, written by `_log_event`) — one JSON
object per line: `{"ts", "utc", "kind", ...fields}`. Kinds:

| Kind | When | Key fields |
|---|---|---|
| `broadcast` | Every `_broadcast` (all push alerts, canonical "Bach" rendering) | `text` |
| `poll` | Each poll where totals moved (quiet polls skipped) | `date, normal, premium, cost, mode` |
| `poll_fail` | API-failure onset + escalation (1st, 5th, 10th consecutive) | `consecutive, backoff_secs` |
| `mode` | Actual mode transitions only | `from_mode, to_mode` |
| `mass_seal` / `mass_unseal` | Sweep completion | `track`, per-project outcome lists / counts |
| `manual_seal` / `manual_unseal` | Button-driven single-project ops | `project, track` |
| `pending_unseal` | Midnight restore drain | `tracks, restored, failed` |
| `day_rollover` | UTC date change | `from_date, to_date, final_*` totals |
| `command` | Every accepted @command and archive button press | `chat, cmd` |

Logging is best-effort: a failure prints `[intel-log error]` and never blocks
delivery or polling. A quiet month is well under 1 MB; prune old files by hand.

**`stdout-YYYY-MM.log`** — the run script tees all console output here
(`PYTHONUNBUFFERED=1 python3 … | tee -a`), so `[poll]` lines, errors and
tracebacks survive reboots. Note: lines contain ANSI color codes; `grep` works
fine, use `less -R` for reading.

### `bot_data/subscribers.json`
JSON array of chat ID strings. Primary chat (`TELEGRAM_CHAT_ID`) is always included and cannot be removed.

### `bot_data/names.json`
JSON object mapping chat ID → display name. Primary chat defaults to "Bach".

### `bot_data/projects.json` (Lab 2) · `projects_lab3.json` (Lab 3)
Each org's project table (`{project_id: name}`) as last synced from the Admin API — see §9.
Loaded at startup so a reboot with the network down still covers every project, and
so a discovered project isn't re-announced after each restart.

---

## 11. OpenAI Admin API

### Costs endpoint
```
GET https://api.openai.com/v1/organization/costs
Authorization: Bearer sk-admin-...
Params: start_time, end_time, bucket_width=1d, group_by[]=project_id, limit=100
```

### Usage/completions endpoint
```
GET https://api.openai.com/v1/organization/usage/completions
Authorization: Bearer sk-admin-...
Params: start_time, end_time, bucket_width=1h|1m|1d, group_by[]=project_id, group_by[]=model, limit=100
```

**Important:** `group_by[]` must be passed as a list of tuples in Python `requests`, not as a dict key.
Costs API has a ~5–10 minute ingestion lag. `today_window_costs()` always sets `end_time` to tomorrow midnight to avoid same-day 400 errors.

---

## 12. Running

```bash
tmux new-session -d -s bot 'bash /home/ngjabach/Documents/Research/BAILAB/NgJaBach-Shadow-Army/scripts/run_openai_bot.sh'
```

**Operating rules (Bach's standing instructions):**
- Never stop or restart a running bot without Bach's explicit go — he manages its
  lifecycle. Starting it when it is down (e.g. after a reboot) is fine.
- Live smoke tests against the real API are welcome (seal/unseal round-trips, reads);
  prefer the normal lane for burn tests. Keep the offline suite green before restarting.
- Call the orgs "Business AI Lab 2" (the original org) and "Business AI Lab 3" (created
  2026-09-29).
- Admin keys live only in `OpenAIUsageBot/.env` (chmod 600) — never in code, docs or logs.

`scripts/run_openai_bot.sh`:
- Runs the bot with the repo's uv-managed `.venv` (pinned by `uv.lock`). It touches
  the network **only if a dependency is missing**, retrying every 30 s until the
  install works. It used to run `pip install --upgrade` on every start — and the
  `.venv` has no pip, so it fell back to system Python and a user-site install. With
  DNS down right after a reboot, both installs failed and `set -e` aborted the launch.
- Validates `.env`.
- **Watchdog**: restarts the bot 5 s after any exit and logs
  `[watchdog] … bot exited (code N)` to the stdout log. The old script used
  `set -euo pipefail`, so a crash failed the `python | tee` pipeline and `set -e`
  killed the script instead of restarting — the "restarts on crash" loop only ever
  restarted after a clean exit (proven in a sandbox with a crashing stub bot).
- Tees output to `bot_data/logs/stdout-YYYY-MM.log`.

**Reboot autostart**: a user crontab entry (installed 2026-09-29) runs
`@reboot sleep 15 && /usr/bin/tmux new-session -d -s bot '…/run_openai_bot.sh'`.
It is a no-op if a `bot` session already exists, so it can never start a second
bot. Check it with `crontab -l`. Before this, every reboot left the bot down until
someone relaunched it by hand.

On startup the bot:
1. Validates `OPENAI_ADMIN_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
2. Creates `bot_data/` if needed
3. Loads persistent state (resets daily fields if date has changed)
4. Loads `bot_data/projects.json` and prints the `[config]` line
5. Starts `usage_poll_loop` and `concurrency_check_loop` first, then
   `telegram_poll_loop` — usage protection never waits on Telegram
6. The Telegram thread resolves `@BachsSlave2Bot` via `getMe` and discards queued
   updates, retrying each until it succeeds (one-shot failures used to leave commands
   dead for the whole run, or replay 24 h of stale archive clicks)
7. First poll seeds milestones — fires highest already-crossed milestone per track (not silent, not a flood), then marks all lower ones silently; the first cycle also syncs the project list
8. Main thread sleeps until `KeyboardInterrupt`

---

## 13. Known Issues & Implementation Notes

- **group_by[] encoding** — Must use list-of-tuples form in `requests.get(params=...)`. Dict form (`{"group_by[]": ...}`) produces the same URL encoding but is rejected by some API versions.
- **Usage lag** — OpenAI usage data has a ~5–15 minute ingestion delay. Not real-time.
- **Cost API same-day 400** — If `start_time == end_time` the costs API returns 400 even when `end_ts > start_ts`. `today_window_costs()` sets `end_time` to tomorrow midnight as a workaround, and so does `month_window()` for the current month (on the 1st it used to 400, and `@spending` showed "no spend").
- **Thread resilience** — All `requests` calls are wrapped in `try/except`. Network errors log a line and return empty; they do not kill the thread. `usage_poll_loop` body itself is wrapped so any unexpected exception just logs and retries after the normal sleep.
- **Telegram offset** — `telegram_poll_loop` advances `offset` before handling each update. A crash mid-handler never causes a message to be re-processed. On startup, `_telegram_startup()` calls `_discard_pending_updates()` until it succeeds (it returns `None` on failure) to drop any updates that piled up while the bot was offline — without this, stale archive button clicks from before the restart could re-fire a real seal/unseal.
- **Store isolation** — `UsageStore.update()` deep-copies the snapshot. It used to keep the caller's dict, and a caller mutating it while another thread's `_save()` serialised it raised "dictionary changed size during iteration" mid-write.
- **2026-09-29 audit pass** — two independent audits (correctness; efficiency/pipeline) plus a final diff review drove the changes documented above: project discovery, launcher watchdog, `arise` allowlist, write-ahead seal captures, rollover deferral, restore-queue rework, quarantine backoff, drift-check throttle, repair gating, overcap grace window, seal-first cycle order, capped busy-mode cadence, `/refresh` → wake the poll loop, and dead-code removal (`_fetch_costs`, `_spawn_bg`, `mode_entered_ts`, legacy-field cleanup). Every fix has a regression test that fails on the pre-fix code.
- **Atomic state writes** — every JSON store (`usage_state.json`, `subscribers.json`, `names.json`) writes via `_atomic_write_json`: write to a `.tmp`, `fsync`, then `os.replace`. A crash mid-write leaves either the old file intact or the new file complete — never a half-written file. If a corrupt state file is found on load it is moved to `<path>.corrupt-<ts>` (loud warning to logs) instead of silently wiped, so the operator can inspect it.
- **HTML injection in display names** — `NameStore.set()` runs the name through `html.escape()` and caps to 48 chars. Names are interpolated into many Telegram-HTML messages; without escaping, a `setname </b><a href='...'>` would break the rendering of every subsequent broadcast.
- **UTC alignment** — All dates use UTC. If running in Vietnam (UTC+7), "today" in UTC starts 7 hours behind local midnight. This matches OpenAI's billing day.
- **Aggressive mode no-cooldown** — Overcap active-project broadcasts fire every poll (3–10 min) with no cooldown by design. This is intentional: the situation is a financial emergency and the team must be continuously reminded until action is taken.
- **Concurrency loop is independent** — `concurrency_check_loop` runs every 5 minutes regardless of the current poll mode. It has its own 15-minute cooldown and is a separate concern from budget caps. On API failure it preserves the last known active-projects snapshot instead of overwriting with `{}`, so a brief network blip doesn't make `/refresh`'s "Active" line show "none" misleadingly.
- **Per-band overcap filtering** — `_fetch_recent_activity_by_band()` groups recent requests by both project and model. `_handle_overcap()` then keeps only projects with activity on the EXCEEDED band(s). A project using premium models cannot trigger the normal-cap alarm and vice versa.
- **Activity fetch failure** — `_fetch_recent_activity*` return `None` on API failure (distinguished from `{}` meaning "no activity"). Callers preserve the previous mode/state rather than acting on missing data.
- **Telegram poll backoff** — `_get_updates()` sleeps 5 s on network error before returning to avoid a tight reconnect loop. Successful long-polls return immediately without added sleep.
- **Network retry on OpenAI *and* Telegram calls** — every Admin API request (`_openai_call`) and every Telegram Bot API request (`_telegram_call`) goes through the same shared `_retrying_call()`, which retries DNS / connection / timeout errors twice (1 s, then 2 s backoff) before re-raising. HTTP error *responses* are never retried — they are answers, not blips. Motivation: this host logged 67 DNS resolution failures in ~18 h on 2026-09-15, and one of them failed an entire quarantine sweep (a full seal aborts and rolls back on its first failed POST). On 2026-09-22 the same flakiness dropped a `sendMessage` alert with no retry on that path (it survived only because `_broadcast` mirrors every alert to the local intel log first) — Telegram calls were brought under the same retry umbrella that day. `getMe` (`_fetch_bot_username`) is not wrapped: `_telegram_startup()` retries it with its own backoff (5 s → 120 s) until it succeeds. Retries only mask the symptom; the machine's resolver is the root cause.
- **Multi-org (2026-09-30)** — see §2b. Implementation notes:
  - The module shadows `print` with a version that prefixes the current thread's org
    (`[lab3] …`), and `_log_event` adds `"org"` from the same thread-local context. It
    only *tags* output; no decision reads it. Pool workers get it via `_in_org`, archive
    workers via `_org_context`, each org's poll/concurrency thread sets it once.
  - `_broadcast` requires an `org` keyword (explicit `None` for org-less messages), so
    a new alert type cannot silently go out without an org header.
  - Live test on Lab 3 (2026-09-30, Default project, real API): premium track seal
    zeroed all 24 premium rows in 30 s and restored them; a quarantine full-seal zeroed
    all but the `ft:*` rows (`rate_limit_not_updatable` — soft-skipped, same as Lab 2)
    and the release restored everything, with two `gpt-4o-audio-preview` rows clamped to
    Lab 3's org ceiling (200 rpm). A brand-new org is still being provisioned: during the
    test OpenAI raised several Lab 3 limits (o1-pro 50 → 500 rpm) and added 37 rate-limit
    rows (150 → 187). Restores write the values captured at seal time (or the org's
    consensus baseline), so a provisioning change OpenAI makes *while* a project is
    sealed can be reverted to the older value by the restore. While Lab 3 settles,
    spot-check its limits on the platform after a seal/restore cycle.
- **Multi-org review fixes (2026-09-30)** — a 4-lens adversarial review (isolation,
  threads, Telegram UX, Lab 2 regressions; every finding double-checked by skeptics)
  confirmed the design and found 14 issues, all fixed with regression tests:
  - **Unguarded org is announced.** A failing org used to look like a quiet, healthy one
    (reports showed $0 / "no usage"). Now: an alert at once if the admin key is rejected
    (401/403), after 5 consecutive failed polls (~15 min) otherwise, and a recovery
    notice; reports print "API error — unknown, NOT zero"; every report section of an
    org with no successful poll or a failing API carries a warning line.
  - **Thread watchdog.** The poll loop's sleep step is guarded (an `OSError` saving state
    there used to end that org's thread), and `main()` exits if any worker thread dies,
    so the launcher restarts the bot instead of one org going silently unguarded.
  - **Unknown projects are synced before sealing** — usage from a project not yet in
    the table triggers an immediate project sync (≤ every 5 min) ahead of the seal
    decisions; seal, repair and quarantine only act on known projects.
  - **Duplicate admin keys are refused** at startup, and a discovery result containing
    another org's project ids is not merged.
  - Archive: project-id buttons; quarantined projects marked ☣️ in Unseal → Both; the
    re-shown picker keeps its prompt; a manual "seal ALL" says "Manual seal … at N%".
  - State getters copy two levels deep; the costs cache is dated (a /refresh straddling
    midnight can't cache yesterday's spend as today's); per-org report sections render in
    parallel; long replies split between blank-line blocks; `setname`'s reply is escaped.
