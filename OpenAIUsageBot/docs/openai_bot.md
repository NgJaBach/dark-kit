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
       ├── _process_pending_track_unseals()          → drain per-track restore queue (day rollover)
       ├── seed_milestones() if !seeded              → fires top-1 milestone per track (idempotent)
       ├── check_milestones() otherwise              → fires on newly crossed thresholds
       ├── _handle_track_seal("normal")              → if ≥95% utilisation, mass-throttle every project
       ├── _handle_track_seal("premium")             →   (idempotent per UTC day via mass_sealed flag)
       ├── _handle_overcap()                         → alarm-only: shouts at any project burning post-cap
       │     ├── _fetch_recent_activity_by_band()    → per-project, per-band counts
       │     └── _filter_to_exceeded_band()          → drop projects only using OK band
       └── mode transition logic                     → passive / urgent / aggressive

telegram_poll_loop()
  ├── @bot <command>          → dispatch() → (text, keyboard)
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
**Status: Not implemented.**

There is no spend-alert code path. The `DAILY_LIMIT` constant ($5.00) is shown for
reference in usage reports only. (The earlier `alert_sent` / `spend_intervals_notified`
state fields were removed as dead code.)

### 7.6 Concurrent Project Alerts
**Condition:** ≥ 3 projects active simultaneously in the last 5 minutes.
**Frequency:** At most once per 15 minutes (`CONCURRENCY_COOLDOWN`).
**Source:** `concurrency_check_loop` thread, runs every 5 min independent of poll mode.

### 7.7 Sealing — track-level mass throttle + interactive manual control

The bot prevents track-cap breaches by **mass-throttling every project's rate
limits for a track** as soon as that track passes 95% utilisation. Sealing uses
`POST /v1/organization/projects/{pid}/rate_limits/{rate_limit_id}` because the
project `/archive` endpoint is one-way and cannot be reversed via API.

Only models on OpenAI's two free-tier lists are touched. `_track_for_model()`
strict-matches each model to `normal` / `premium` / `None`; unlisted models
(sora-2, babbage-002, dall-e-3, etc.) are never throttled — they bill at standard
rates regardless, so throttling them is pointless.

#### Auto trigger — track-level mass seal

Constants:
```
TRACK_SEAL_REMAINING_PCT      = 0.05
NORMAL_TRACK_SEAL_THRESHOLD   = 9,500,000     (5% of 10M remaining)
PREMIUM_TRACK_SEAL_THRESHOLD  =   950,000     (5% of 1M  remaining)
```

`usage_poll_loop()` and `cmd_refresh()` both check, after every poll:

```
if total_normal_tokens  ≥ 9.5M  and not is_mass_sealed('normal')  → _handle_track_seal('normal')
if total_premium_tokens ≥ 950k  and not is_mass_sealed('premium') → _handle_track_seal('premium')
```

`_handle_track_seal()` is a thin wrapper: it checks the per-day `mass_sealed_tracks`
flag (idempotency) and delegates to `_mass_seal_track()`. Each track's auto-sweep
fires at most once per UTC day.

#### What a mass throttle does

`_mass_seal_track(track)` runs under a single global busy claim so no other
seal/unseal can interleave. Auto-sweep & day-rollover restore run inline in the
poll thread; manual archive ops run in a background worker thread (see "Race
safety" below).

1. Marks the track in `mass_sealed_tracks` (auto-sweep idempotency).
2. Broadcasts a concise "begin sealing…" banner.
3. Orders `KNOWN_PROJECTS` by recent activity on that track (descending) so heavy spenders are throttled **first**. Uses `_fetch_recent_activity_by_band(OVERCAP_WINDOW_MINS)`.
4. For each project: skips if exempt for that track or already sealed; otherwise GETs its rate limits, filters to rows where `_matches_track(model, track)`, captures the pre-throttle originals, and POSTs `0` to every present field. Originals are saved under `sealed_tracks[track].originals_by_project[pid]`.
5. Broadcasts a concise "done sealing… N throttled (M exempt)" summary — **no per-project chatter**.

Inter-POST spacing is 50 ms (bulk writes without spacing occasionally don't persist on the API side).

#### Race safety — atomic busy claim + background workers

Every operation that mutates rate limits — the auto 95% sweep, the day-rollover
restore, and every manual button action — passes through one atomic check-and-set
called `_try_claim_busy()`. Only one operation can hold the claim at a time; every
caller MUST `_release_busy()` in a `finally`. This is intentionally **not** a
blocking lock — callers that cannot claim the flag bail out instead of waiting:

- **Manual button click** (Telegram callback) — refuses immediately with a small
  toast: *"A seal/unseal is already running — try again shortly."* The user can
  click again once the in-flight operation finishes.
- **Auto 95% sweep** (poll loop) — defers silently and logs `mass-seal deferred`.
  The next poll re-checks the threshold and retries; since consumption only
  grows, the trigger condition won't disappear.
- **Day-rollover restore** (poll loop) — defers silently and logs
  `pending-track-unseal deferred`. The pending queue is persisted, so the next
  poll picks up where this one left off.

Manual button operations are additionally **dispatched to a daemon worker thread**
so the Telegram poll loop is never blocked by a 30-50 s mass sweep. The callback
acknowledges immediately with a placeholder ("🔄 Working — watch chat for
progress…") and the worker edits the message again when the API work completes.
This closes two prior bugs:

1. **TOCTOU race** — the old code read `_SEAL_BUSY` *without* the lock, then
   `with _SEAL_LOCK:` inside the heavy function. A click between check and acquire
   would block the Telegram thread for the duration of the running sweep.
2. **Poll-thread freeze** — the old design ran the seal inline. A mass-seal would
   freeze every other Telegram command for 30-50 s.

#### Callback input validation

`handle_archive_callback` validates every field of the `arch:<action>:<mode>:<pidx>`
payload against an enum set before using it:

- `action` ∈ `{cancel, menu, seal, unseal}` — anything else returns "Unknown action."
- `mode`   ∈ `{-, normal, premium, both}` — anything else returns "Unknown mode."
- `pidx`   — must be `"-"`, `"all"`, or a valid index into `_PROJECT_INDEX`.
  Out-of-bounds or non-numeric returns "Unknown project." and releases the busy
  claim that was just taken.

This is defence-in-depth: Telegram already restricts the keyboard to bot-emitted
buttons, but a forged callback (e.g. from a compromised account) cannot trigger a
seal on an unknown project or with an unknown mode.

#### Soft-skip error codes

Some rate-limit rows returned by GET aren't actually updatable. The bot treats these as successful no-ops:
- `rate_limit_does_not_exist_for_org_and_model` — org has no access to that model.
- `rate_limit_not_updatable` — fine-tune / batch-only rows.
- `invalid_rate_limit_type` — model doesn't expose that field (e.g. `sora-2` has no `max_tokens_per_1_minute`).

#### Manual control — `@bot archive` (interactive buttons)

The command takes **no arguments**. It posts the live status plus an inline keyboard
and drives a small button state machine (messages are edited in place, not re-sent):

```
@bot archive
   → status + [🔒 Seal] [🔓 Unseal] [✖ Cancel]
        → Seal/Unseal → [📦 Normal] [⭐ Premium] [🔱 Both] [✖ Cancel]
             → mode → one button per project (2 cols) + [🟥 ALL] [✖ Cancel]
                  → project (or ALL) → applies the change, re-renders status
```

Callback data is compact (`arch:<action>:<mode>:<pidx>`, well under Telegram's
64-byte cap) using a stable `_PROJECT_INDEX`. Choosing a single project calls
`_manual_seal_project` / `_manual_unseal_project`; choosing **ALL** calls
`_mass_seal_track` (ignoring exemptions) / `_mass_unseal_track`.

- A manual **unseal** restores the project's rows for that track to the canonical
  baseline and marks it exempt for the rest of the UTC day (the auto-sweep skips it).
- A manual **seal** clears any exemption and throttles the project's track rows to 0.
- **Exempt projects still get the overcap alarm** — if an unsealed project keeps
  burning post-cap tokens, `_handle_overcap` keeps broadcasting the red-tone alert.
  The user is responsible for the bill.

#### Uniform restore (canonical baseline)

All restores route through `_compute_canonical_baseline()`, which derives one
healthy value per (model, field) by consensus across **state captures first**
(pre-throttle originals in `sealed_tracks` / `pending_track_unseal`) then live API
values. It **never emits a zero** — a field is omitted unless a non-zero value
exists somewhere. This guarantees two things: every project restores to the *same*
per-model limits (uniform), and a restore can never accidentally re-throttle a
project (immune to the 0/0 cascade that bricked projects in an earlier version).

#### Auto-unseal at UTC midnight

`UsageStore.update()` and `__init__()` detect date changes and call
`_reset_daily_state_locked()`, which moves `sealed_tracks` → `pending_track_unseal`,
clears `mass_sealed_tracks` and `track_exemptions`. The next poll calls
`_process_pending_track_unseals()` (gated by the busy claim) to drain the queue —
restoring every project to the canonical baseline. Failures stay queued and retry
on subsequent polls, so a midnight outage never leaves anything throttled forever.

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
  // Tracks whose 95% auto-sweep has fired today (auto-trigger idempotency).
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

- **Detection latency**: up to one poll cycle (30 min passive, 3–10 min urgent). Normal crossing ~9M flips mode to urgent so the 95% trigger is caught quickly.
- **Per-project throttle cost**: ~50–80 track rows × ~50 ms ≈ a few seconds per project per track.
- **Full mass sweep**: 13 projects ≈ ~50 s for one track. Auto-sweep & midnight restore block the **poll** thread for that duration (it has nothing else to do); manual button-driven sweeps run in a daemon worker thread so the Telegram poll thread stays free for other commands.
- **Inflight window**: a brief gap between detection and full throttle where running requests complete. Unavoidable, bounded by the sweep wall-time.

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
| `archive` | Show, seal, unseal projects via interactive buttons (no arguments — see §7.7). |
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
| `mass_sealed_tracks` | list | tracks whose 95% auto-sweep has fired today (auto-trigger idempotency) |
| `track_exemptions` | dict | pid → list of tracks manually unsealed today; the auto-sweep skips these |
| `pending_track_unseal` | dict | track → {originals_by_project}. Populated by day rollover from `sealed_tracks`, drained next poll |

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
- **Telegram offset** — `telegram_poll_loop` advances `offset` before handling each update. A crash mid-handler never causes a message to be re-processed. On startup, `_discard_pending_updates()` runs once to drop any updates that piled up while the bot was offline — without this, stale archive button clicks from before the restart could re-fire a real seal/unseal.
- **Atomic state writes** — every JSON store (`usage_state.json`, `subscribers.json`, `names.json`) writes via `_atomic_write_json`: write to a `.tmp`, `fsync`, then `os.replace`. A crash mid-write leaves either the old file intact or the new file complete — never a half-written file. If a corrupt state file is found on load it is moved to `<path>.corrupt-<ts>` (loud warning to logs) instead of silently wiped, so the operator can inspect it.
- **HTML injection in display names** — `NameStore.set()` runs the name through `html.escape()` and caps to 48 chars. Names are interpolated into many Telegram-HTML messages; without escaping, a `setname </b><a href='...'>` would break the rendering of every subsequent broadcast.
- **UTC alignment** — All dates use UTC. If running in Vietnam (UTC+7), "today" in UTC starts 7 hours behind local midnight. This matches OpenAI's billing day.
- **Aggressive mode no-cooldown** — Overcap active-project broadcasts fire every poll (3–10 min) with no cooldown by design. This is intentional: the situation is a financial emergency and the team must be continuously reminded until action is taken.
- **Concurrency loop is independent** — `concurrency_check_loop` runs every 5 minutes regardless of the current poll mode. It has its own 15-minute cooldown and is a separate concern from budget caps. On API failure it preserves the last known active-projects snapshot instead of overwriting with `{}`, so a brief network blip doesn't make the `/active` command show "no activity" misleadingly.
- **Per-band overcap filtering** — `_fetch_recent_activity_by_band()` groups recent requests by both project and model. `_handle_overcap()` then keeps only projects with activity on the EXCEEDED band(s). A project using premium models cannot trigger the normal-cap alarm and vice versa.
- **Activity fetch failure** — `_fetch_recent_activity*` return `None` on API failure (distinguished from `{}` meaning "no activity"). Callers preserve the previous mode/state rather than acting on missing data.
- **Telegram poll backoff** — `_get_updates()` sleeps 5 s on network error before returning to avoid a tight reconnect loop. Successful long-polls return immediately without added sleep.
