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

Tracks OpenAI API token and cost usage across all organization projects.
Reports to Telegram via automatic push alerts and on-demand pull commands.

**Role 1 — Autonomous monitor (push)**
Polls the OpenAI Admin API on a dynamic schedule.
On each poll:
- Fetches today's token usage per project, broken down by model
- Fetches today's cost per project
- Updates persistent state (`bot_data/usage_state.json`)
- Fires milestone alerts, overcap broadcasts, or concurrency alerts as needed

**Role 2 — Command responder (pull)**
Long-polls Telegram `getUpdates`. When a message starts with `@BachsSlave2Bot <cmd>`,
dispatches to the matching handler and replies to that chat/thread.

Both roles run as daemon threads. Main thread sleeps until `KeyboardInterrupt`.

---

## 3. Architecture

```
main()
 ├── load .env
 ├── UsageStore(bot_data/usage_state.json)
 ├── SubscriberStore(bot_data/subscribers.json)
 ├── NameStore(bot_data/names.json)
 ├── _fetch_bot_username()          → resolves @BachsSlave2Bot
 ├── Thread: telegram_poll_loop()   ← command responder
 ├── Thread: usage_poll_loop()      ← OpenAI poller + alert sender
 └── Thread: concurrency_check_loop() ← concurrent-project detector

usage_poll_loop()
  └── dynamic sleep (mode-dependent):
       ├── fetch_today_usage()                       → tokens per project, classified by band
       ├── _fetch_costs()                            → cost per project
       ├── usage.update(snap)                        → auto-resets daily state on date change
       ├── _process_pending_unseals()                → drain legacy per-project queue (day rollover)
       ├── _process_pending_track_unseals()          → drain per-track queue (day rollover)
       ├── seed_milestones() if !seeded              → fires top-1 milestone per track (idempotent)
       ├── check_milestones() otherwise              → fires on newly crossed thresholds
       ├── _handle_track_seal("normal")              → if ≥95% utilisation, mass-throttle every project
       ├── _handle_track_seal("premium")             →   (track-by-track, idempotent per UTC day)
       ├── _handle_overcap()                         → alarm-only: shouts at any project burning post-cap
       │     ├── _fetch_recent_activity_by_band()    → per-project, per-band counts
       │     └── _filter_to_exceeded_band()          → drop projects only using OK band
       └── mode transition logic                     → passive / urgent / aggressive

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
    ├── usage_state.json       # Today's snapshot + mode state
    ├── subscribers.json       # Subscribed chat IDs
    └── names.json             # Per-chat display names
```

---

## 5. Configuration

### `.env` file
```
OPENAI_ADMIN_KEY=sk-admin-...      # Organization admin key from OpenAI Platform
TELEGRAM_BOT_TOKEN=...             # From @BotFather
TELEGRAM_CHAT_ID=-100xxxxxxxxx     # Primary chat (group/channel/private)
POLL_INTERVAL_MINS=30              # Passive-mode baseline (default 30, minimum 30 recommended)
```

### Hardcoded constants (edit source to change)

| Constant | Default | Purpose |
|---|---|---|
| `DAILY_LIMIT` | $5.00 | Daily spend reference threshold |
| `TOKEN_HARD_CAP` | 10,000,000 | Normal-model free-tier ceiling |
| `PREMIUM_TOKEN_HARD_CAP` | 1,000,000 | Premium-model free-tier ceiling |
| `PASSIVE_INTERVAL_SECS` | 30 min | Passive-mode poll interval |
| `PASSIVE_BACKOFF_MAX` | 2 h | Max backoff on consecutive API failures |
| `URGENT_INTERVAL_MIN` | 3 min | Starting poll interval in urgent/aggressive mode |
| `URGENT_INTERVAL_MAX` | 10 min | Maximum poll interval in urgent/aggressive mode |
| `URGENT_INTERVAL_STEP` | 1 min | Interval increment per poll in urgent/aggressive mode |
| `URGENT_REVERT_SECS` | 1 h | Time without new milestone before reverting to passive |
| `AGGRESSIVE_REVERT_SECS` | 1 h | Time since last illegal project before reverting to passive |
| `CONCURRENCY_THRESHOLD` | 3 | Projects active simultaneously to trigger concurrency alert |
| `CONCURRENCY_WINDOW_MINS` | 5 | "Active" window for concurrency check (narrow, real-time use) |
| `CONCURRENCY_COOLDOWN` | 900 s | Min gap between concurrency alerts |
| `OVERCAP_WINDOW_MINS` | 20 | Activity window for overcap detection (wider, accounts for ingestion lag) |
| `TRACK_SEAL_REMAINING_PCT` | 0.05 | Fraction of remaining quota that triggers track-level mass throttle |
| `NORMAL_TRACK_SEAL_THRESHOLD` | 9,500,000 | Derived: cap × (1 − remaining_pct) for the normal band |
| `PREMIUM_TRACK_SEAL_THRESHOLD` | 950,000 | Derived: cap × (1 − remaining_pct) for the premium band |

### Admin key requirement
Regular `sk-...` keys cannot access `/v1/organization/*` endpoints.
Must use an **Admin API key** (`sk-admin-...`):
Platform → Organization → API Keys → Create Admin Key

### Model classification
`_is_premium_model(model)` in source is the single source of truth. Order of checks:

1. **Normal-band prefix match** (listed mini/nano variants) → normal.
2. **`mini`/`nano` substring** anywhere in name → normal (catches unlisted future
   variants like `gpt-5.5-mini`).
3. Anything else → premium.

`PREMIUM_MODEL_PREFIXES` is kept in source as documentation of the listed premium models
but is not consulted at runtime — the order above means premium status is the catch-all
fallback. This is conservative for free-tier alerting (unknown full-size models count
against the lower 1M cap rather than going untracked).

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
**Poll interval:** Starts at 3 min, increases by 1 min per poll, caps at 10 min.
Hitting a new milestone within the 1-hour window **resets the interval back to 3 min**.
**Revert condition:** 1 hour passes without any new milestone → reverts to passive.
**Behaviour:** Same as passive, but at a shorter interval. Cap breaches still trigger aggressive.

### Aggressive Mode
**Trigger:** Either cap is exceeded **and** at least one project has recent activity
**on the EXCEEDED band** within the last 20 minutes (`OVERCAP_WINDOW_MINS`). Per-band
filtering means premium-track usage does not raise the alarm when only the normal cap
is exceeded, and vice versa.
**Poll interval:** Same 3→10 min progression as urgent mode.
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
any mode           →  aggressive  via /refresh command detecting illegal projects
```

### Manual Mode Trigger via /refresh
When the user issues `/refresh`, the bot:
1. Forces an immediate token + cost poll
2. Runs milestone check (fires any newly crossed thresholds)
3. If any cap is exceeded, checks recent activity immediately
4. If illegal projects are found → broadcasts the overcap alert + switches to aggressive mode
5. Reports mode change in the refresh reply

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

### 7.2 Normal-Band Token Milestones (10M/day free)
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

### 7.3 Premium-Band Token Milestones (1M/day free)
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
**Frequency:** Every poll while in aggressive mode (3–10 min interval) — no cooldown.
**Tone:** Red, urgent, named-project list with per-band request counts, explicit "halt" command.

Crucial detail: the filter is per-band. If only the **Normal (10M)** cap is exceeded,
a project burning only premium-band tokens does **not** trigger the alarm — premium is
still under its 1M allowance. The bot fetches recent activity grouped by both project
and model, classifies each model into a band, and then keeps only projects with usage
on the exceeded band(s).

The 20-min window (vs. 5 min for concurrency) absorbs OpenAI's 5–15 min ingestion lag —
a 5-min window would miss activity that completed 9 min ago and hasn't shown up yet.

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

### 7.5 Daily Spend Alerts
**Status: Not currently active.**

The `alert_sent` and `spend_intervals_notified` fields are tracked in state and reset at midnight,
but no spend-alert code path is wired into the poll loop. The `DAILY_LIMIT` constant ($5.00)
is available for implementing this in the future if needed.

### 7.6 Concurrent Project Alerts
**Condition:** ≥ 3 projects active simultaneously in the last 5 minutes.
**Frequency:** At most once per 15 minutes (`CONCURRENCY_COOLDOWN`).
**Source:** `concurrency_check_loop` thread, runs every 5 min independent of poll mode.

### 7.7 Sealing — track-level mass throttle + per-project manual control

The bot prevents track-cap breaches by **mass-throttling every project's rate
limits for a track** as soon as that track passes 95% utilisation. Sealing uses
`POST /v1/organization/projects/{pid}/rate_limits/{rate_limit_id}` because the
project `/archive` endpoint is one-way and cannot be reversed via API.

#### Auto trigger — track-level mass seal

Constants:
```
TRACK_SEAL_REMAINING_PCT      = 0.05
NORMAL_TRACK_SEAL_THRESHOLD   = 9,500,000     (5% of 10M remaining)
PREMIUM_TRACK_SEAL_THRESHOLD  =   950,000     (5% of 1M  remaining)
```

`usage_poll_loop()` and `cmd_refresh()` both check, after every poll:

```
if total_normal_tokens  ≥ 9.5M  and 'normal'  not in sealed_tracks → _handle_track_seal('normal')
if total_premium_tokens ≥ 950k  and 'premium' not in sealed_tracks → _handle_track_seal('premium')
```

Each track fires at most once per UTC day. Once the `sealed_tracks[track]` entry
exists, re-triggering is a no-op.

#### What a mass throttle does

`_handle_track_seal(track)` runs inline (blocks the poll thread for ~30–50 s).

1. Reserves the slot by writing an empty `sealed_tracks[track] = {sealed_at, threshold, originals_by_project: {}}` entry — protects against concurrent triggers.
2. Broadcasts the "throttle starting" banner so the chat knows what's happening.
3. Orders `KNOWN_PROJECTS` by recent activity on that track (descending) so heavy spenders get throttled **first**. Uses `_fetch_recent_activity_by_band(OVERCAP_WINDOW_MINS)` — same window as the overcap alarm to absorb the 5–15 min OpenAI ingestion lag.
4. For each project in order:
   - Skips if `track in track_exemptions[pid]` (manually unsealed earlier today).
   - Skips if `pid in sealed_projects` (already fully sealed by the manual command).
   - Otherwise: GETs rate limits, filters to rows where `_matches_track(model, track)`, captures the originals, POSTs `0` to every present field on each row, and saves the originals under `sealed_tracks[track].originals_by_project[pid]`.
5. Broadcasts the summary: throttled / skipped-exempt / skipped-manual-sealed / failed.

Inter-POST spacing is 50 ms (same as before — bulk writes without spacing occasionally don't persist on the API side).

#### Soft-skip error codes

Some rate-limit rows returned by GET aren't actually updatable. The bot treats these as successful no-ops:
- `rate_limit_does_not_exist_for_org_and_model` — org has no access to that model.
- `rate_limit_not_updatable` — fine-tune / batch-only rows.
- `invalid_rate_limit_type` — model doesn't expose that field (e.g. `sora-2` has no `max_tokens_per_1_minute`).

In all three cases the project can't use the model in a way that bypasses our throttle.

#### Manual commands — `@bot archive`

| Form | Effect |
|---|---|
| `archive` (or `archive list`) | Show per-track usage + sealed/exempt/pending status for every known project. |
| `archive seal <project>` | Manually full-seal one project. Throttles **every** rate-limit row to 0 (both tracks + non-classified models). Folds in any track-level seal already in place — uses saved track originals so the eventual unseal restores the right values. |
| `archive unseal <project>` | Restore **both** tracks for one project. Adds the project to `track_exemptions` for both `normal` and `premium`, so today's auto track-seal will skip it on either track. |
| `archive unseal <project> normal` | Restore only normal-band rate-limit rows. Adds `normal` to that project's exemptions; `premium` is unaffected (if premium was also sealed, it stays). |
| `archive unseal <project> premium` | Symmetric to above. |

Project resolution accepts either the friendly name (`phongnguyen-project`) or the raw `proj_…` ID, case-insensitive on the name side.

**Exempt projects still get the overcap alarm.** If a user unseals a project mid-day and that project keeps burning post-cap tokens, `_handle_overcap` continues to broadcast the red-tone alarm for it — the user is responsible for the bill.

#### Auto-unseal at UTC midnight

`UsageStore.update()` and `UsageStore.__init__()` both detect date changes and call `_reset_daily_state_locked()`. The reset:
- Moves `sealed_projects` → `pending_unseal`.
- Moves `sealed_tracks` → `pending_track_unseal` (merges per-track if a stale entry was still queued).
- Clears `track_exemptions`.

The next poll iteration calls `_process_pending_unseals()` and `_process_pending_track_unseals()` to drain the queues — POSTing saved originals back via API. Failures stay in the queue and retry on subsequent polls, so a midnight outage doesn't leave anything throttled forever.

#### State schema

```jsonc
{
  // Track-level mass seal — auto-fired at 95% utilisation
  "sealed_tracks": {
    "normal":  { "sealed_at": 1737000000, "threshold": 9500000,
                 "originals_by_project": {
                   "proj_xxx": [ { "id": "rl_…", "model": "gpt-4o-mini",
                                   "max_requests_per_1_minute": 5000,
                                   "max_tokens_per_1_minute": 4000000 }, … ],
                   "proj_yyy": [ … ]
                 } },
    "premium": { … or absent if not sealed }
  },
  // Per-project full seal — fired by @bot archive seal <project>
  "sealed_projects": { "proj_zzz": { "sealed_at": …, "reason": …,
                                     "original_limits": [ … ],
                                     "auto_sealed": false } },
  // Per-track manual exemption — set by @bot archive unseal
  "track_exemptions": { "proj_yyy": ["normal"] },
  // Day-rollover restore queues (drained by next poll)
  "pending_unseal":       { same shape as sealed_projects },
  "pending_track_unseal": { same shape as sealed_tracks }
}
```

All five fields are preserved across snapshot updates (in `UsageStore._PRESERVED`).

#### Latency & blast radius

- **Detection latency**: up to one poll cycle. In passive mode that's 30 min, in urgent mode 3–10 min. Once normal hits ~9M (the urgent milestone), mode flips to urgent so the 95% trigger is caught quickly.
- **Per-project throttle cost**: ~75 normal-track or ~80 premium-track rate-limit rows per project, ~50 ms spacing each. ≈ 4 s of API work per project per track.
- **Full mass throttle**: 13 projects × ~75 rows ≈ ~50 s for a single track. Poll thread is blocked during this; Telegram thread stays responsive (it has its own thread).
- **Inflight requests window**: there's still a brief window between threshold detection and full throttle where running requests can complete. Unavoidable, but bounded by the same 50 s wall time above.

---

## 8. Command Reference

Trigger: message must start with `@BachsSlave2Bot` (case-insensitive) followed by a command.
In groups/topics, the bot respects `message_thread_id` — replies stay in the originating thread.
Non-subscribed chats can only use `arise`. All other commands are silently ignored.

| Command | Description |
|---|---|
| `refresh` | Force-poll OpenAI now. Checks caps + switches mode if needed. |
| `tokens` | Per-project token breakdown with per-model detail. |
| `models` | Aggregate model usage across all projects today. |
| `projects` | Project roster with token bar chart and costs. |
| `rank` | Rankings by token consumption and daily spend. |
| `recent` | Last 31 days: per-project cost, total tokens & requests. |
| `spending` | Monthly bill — current + previous month (live fetch). |
| `active` | Projects with API activity in the last 5 min + concurrency status. |
| `archive` | Show per-track usage + per-project seal/exempt/pending state. |
| `archive seal <project>` | Manually full-seal a project (both tracks → 0). |
| `archive unseal <project>` | Restore both tracks for one project; mark exempt on both until UTC midnight. |
| `archive unseal <project> normal` | Restore only normal-band rate-limits; mark exempt on normal. |
| `archive unseal <project> premium` | Restore only premium-band rate-limits; mark exempt on premium. |
| `arise` | Subscribe this chat to all alerts. Plays the Beru GIF on first subscribe. |
| `dismiss` | Unsubscribe this chat (primary chat cannot be dismissed). |
| `setname Name` | Set the name the bot uses to address you in this chat. |
| `help` | Full command registry. |

---

## 9. Known Projects

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

IDs are case-sensitive. Always re-export from Platform → Projects → Export CSV when adding new projects.

---

## 10. Data Persistence

### `bot_data/usage_state.json`
Stores today's snapshot plus all alert-control and mode state.
Fields preserved across snapshot updates (not overwritten by each poll):

| Field | Type | Purpose |
|---|---|---|
| `alert_sent` | bool | Daily spend alert fired flag |
| `token_milestones_notified` | list[int] | Normal-band thresholds already alerted |
| `premium_milestones_notified` | list[int] | Premium-band thresholds already alerted |
| `spend_intervals_notified` | int | Count of $2 intervals above daily limit |
| `last_concurrent_alert_ts` | float | Timestamp of last concurrency alert |
| `active_projects` | dict | Last concurrency-check activity snapshot |
| `costs_cache` | dict | Last successful cost fetch (per-project + total) |
| `bot_mode` | str | Current mode: "passive" / "urgent" / "aggressive" |
| `mode_entered_ts` | float | When current mode was entered |
| `last_milestone_ts` | float | Timestamp of last milestone hit (urgent revert timer) |
| `last_illegal_seen_ts` | float | Timestamp of last poll with active illegal projects |
| `urgent_poll_step` | int | Current step in 3→10 min interval progression |
| `milestones_seeded` | bool | True after first-poll seed runs; guards the /refresh race condition |
| `sealed_tracks` | dict | track → {sealed_at, threshold, originals_by_project: {pid: [rate-limit-rows]}} for every track currently mass-throttled |
| `sealed_projects` | dict | pid → {sealed_at, reason, original_limits, auto_sealed} for projects under a manual full seal |
| `track_exemptions` | dict | pid → list of tracks the project is exempt from for today's auto seal |
| `pending_unseal` | dict | pid → entry to restore via API on next poll. Populated by day rollover from `sealed_projects` |
| `pending_track_unseal` | dict | track → {originals_by_project}. Populated by day rollover from `sealed_tracks` |

All fields reset at UTC midnight. Two reset paths, both internal:

- `UsageStore.__init__`: on load, if the persisted `date` is older than today, reset
  immediately so commands hitting the store before the first poll don't see yesterday's
  data.
- `UsageStore.update()`: each poll's snapshot carries today's date. If it differs from
  the persisted date, the store auto-resets before merging. This closes the race where
  the Telegram thread runs `/refresh` on a new day before the poll loop notices the
  rollover.

Both paths share `_reset_daily_state_locked()` (private — caller must hold the store lock).

### `bot_data/subscribers.json`
JSON array of chat ID strings. Primary chat (`TELEGRAM_CHAT_ID`) is always included and cannot be removed.

### `bot_data/names.json`
JSON object mapping chat ID → display name. Primary chat defaults to "Bach".

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
bash scripts/run_openai_bot.sh
```

The script creates the venv if needed, installs dependencies, validates `.env`, and launches the bot via `exec` (clean Ctrl-C).

On startup the bot:
1. Validates `OPENAI_ADMIN_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
2. Creates `bot_data/` if needed
3. Loads persistent state (resets daily fields if date has changed)
4. Resolves `@BachsSlave2Bot` username via `getMe`
5. Starts `telegram_poll_loop`, `usage_poll_loop`, `concurrency_check_loop` as daemon threads
6. First poll seeds milestones — fires highest already-crossed milestone per track (not silent, not a flood), then marks all lower ones silently
7. Main thread sleeps until `KeyboardInterrupt`

---

## 13. Known Issues & Implementation Notes

- **group_by[] encoding** — Must use list-of-tuples form in `requests.get(params=...)`. Dict form (`{"group_by[]": ...}`) produces the same URL encoding but is rejected by some API versions.
- **Usage lag** — OpenAI usage data has a ~5–15 minute ingestion delay. Not real-time.
- **Cost API same-day 400** — If `start_time == end_time` the costs API returns 400 even when `end_ts > start_ts`. `today_window_costs()` sets `end_time` to tomorrow midnight as a workaround.
- **Thread resilience** — All `requests` calls are wrapped in `try/except`. Network errors log a line and return empty; they do not kill the thread. `usage_poll_loop` body itself is wrapped so any unexpected exception just logs and retries after the normal sleep.
- **Telegram offset** — `telegram_poll_loop` advances `offset` before handling each update. A crash mid-handler never causes a message to be re-processed.
- **UTC alignment** — All dates use UTC. If running in Vietnam (UTC+7), "today" in UTC starts 7 hours behind local midnight. This matches OpenAI's billing day.
- **Aggressive mode no-cooldown** — Overcap active-project broadcasts fire every poll (3–10 min) with no cooldown by design. This is intentional: the situation is a financial emergency and the team must be continuously reminded until action is taken.
- **Concurrency loop is independent** — `concurrency_check_loop` runs every 5 minutes regardless of the current poll mode. It has its own 15-minute cooldown and is a separate concern from budget caps. On API failure it preserves the last known active-projects snapshot instead of overwriting with `{}`, so a brief network blip doesn't make the `/active` command show "no activity" misleadingly.
- **Per-band overcap filtering** — `_fetch_recent_activity_by_band()` groups recent requests by both project and model. `_handle_overcap()` then keeps only projects with activity on the EXCEEDED band(s). A project using premium models cannot trigger the normal-cap alarm and vice versa.
- **Activity fetch failure** — `_fetch_recent_activity*` return `None` on API failure (distinguished from `{}` meaning "no activity"). Callers preserve the previous mode/state rather than acting on missing data.
- **Telegram poll backoff** — `_get_updates()` sleeps 5 s on network error before returning to avoid a tight reconnect loop. Successful long-polls return immediately without added sleep.
