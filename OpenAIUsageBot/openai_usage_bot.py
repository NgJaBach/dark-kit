"""
Telegram Usage Bot — OpenAI Shadow Ledger

Polls OpenAI organization usage API and reports token/cost stats per project.
Receives @commands from the configured Telegram chat (with inline-button menu).
Monitors token milestones, concurrent project activity, and the per-track
rate-limit seal (normal 95%, premium 80%, plus a predictive wave guard) that
prevents cap breaches.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BOT IDENTITY: Marshal-Rank Shadow Commander
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Bound to Bach the Monarch. Speaks with formality and restraint.
No humor. No filler. Precision in all things.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import builtins
import calendar
import copy
import html
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import dotenv
import requests


def _atomic_write_json(path: Path, data) -> None:
    """Write JSON to disk atomically — temp file + rename. Prevents partial-write
    corruption on crash/power-loss/SIGKILL mid-`json.dump`. The temp lives in the
    same directory so `os.replace` stays on one filesystem (rename is atomic only
    within the same FS)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

dotenv.load_dotenv()

# ── Version / changelog (shown in the help footer) ─────────────────────────
# Keep BOT_UPDATED current and list the few most recent user-facing changes.
BOT_UPDATED = "2026-09-30"
BOT_CHANGES = (
    "Fewer commands: refresh · usage · spending · archive (old names still work)",
    "Tap the buttons under help / refresh, or pick a command from Telegram's / menu",
    "Now guarding Business AI Lab 3 too (2.5M normal / 250K premium per day)",
    "Every alert names its org; archive: Seal/Unseal → org → track → project",
)

BOT_TOKEN        = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID          = os.environ.get("TELEGRAM_CHAT_ID", "")
DAILY_LIMIT      = 2.00   # base daily-spend alarm (USD) — Lab 2's; each org sets its
                          # own (ORG_SPECS daily_limit) and the ladders below scale
                          # with it. $0 was the goal, but unlisted-model usage
                          # (embeddings, audio, image, etc.) bills from token 1, so
                          # $2/day is Lab 2's "alarm-out-loud" line.

# ── Spend monitoring (org-wide + per-project + unlisted-model anomaly) ──────
# The bot was originally token-only. An incident — $6 of embedding usage went
# undetected because embeddings aren't in either free-tier watchlist — exposed
# the gap: any unlisted model spends from token 1 with no token-milestone, no
# overcap alarm, no rate-limit seal trigger. Spend monitoring closes the gap.
#
# Three independent alarm layers (org-wide $, per-project $, unlisted-model
# first touch). Enforcement for unlisted models is separate: any project that
# touches one is quarantined (full seal) — see _quarantine_unlisted_users.
SPEND_MILESTONES = [
    (0.10, "casual"),   # first $0.10 — early ack
    (0.50, "casual"),   # quarter of cap
    (1.00, "urgent"),   # half of cap — pay attention
    (1.50, "urgent"),   # 75% of cap — last warning
    (2.00, "cap"),      # = DAILY_LIMIT — flip to AGGRESSIVE mode
]
PROJECT_SPEND_THRESHOLDS = (0.25, 0.50, 1.00)   # per-project (USD)

# Past the cap, keep escalating: a fresh cap-level alert fires every extra
# SPEND_OVERCAP_STEP dollars ($2.00, $2.50, $3.00, …). Without this the bot
# alerts once at the cap and then goes silent no matter how far spend runs.
# Keep the step at a binary-exact value (x.00 / x.50) — thresholds are stored
# and compared as floats in the notified list.
SPEND_OVERCAP_STEP = 0.50

# Unlisted-model alert: any non-zero usage of a model that isn't in either
# free-tier watchlist fires once per (project, model) per day. Catches the
# embedding/image/audio/fine-tune classes that the token-milestone path misses.
UNLISTED_MODEL_MIN_REQUESTS = 1   # alert from the first request
# ── Polling intervals ───────────────────────────────────────────────────────
# Passive mode: long polling, exponential backoff on consecutive failures.
# POLL_INTERVAL_MINS env var sets the passive baseline (default 30 min).
PASSIVE_INTERVAL_SECS = int(os.environ.get("POLL_INTERVAL_MINS", "30")) * 60
PASSIVE_BACKOFF_MAX   = 2 * 3600   # 2-hour ceiling during failure backoff

# Urgent / aggressive mode: short polling after a milestone or cap breach.
URGENT_INTERVAL_MIN  = 3 * 60     # 3-minute floor
URGENT_INTERVAL_MAX  = 10 * 60    # 10-minute ceiling
URGENT_INTERVAL_STEP = 60         # +1 min per poll until ceiling is reached

# Revert timers
URGENT_REVERT_SECS     = 3600     # 1 h without new milestone  → back to passive
AGGRESSIVE_REVERT_SECS = 3600     # 1 h since last illegal project → back to passive

BOT_DATA_DIR     = Path(__file__).parent / "bot_data"   # per-org state files: see ORG_SPECS
SUBS_PATH        = BOT_DATA_DIR / "subscribers.json"
NAMES_PATH       = BOT_DATA_DIR / "names.json"
LOGS_DIR         = BOT_DATA_DIR / "logs"

# ── Local intel log ─────────────────────────────────────────────────────────
# Telegram broadcasts used to exist ONLY in the chat — nothing durable on disk
# for later analysis. _log_event mirrors every broadcast and key state change
# to a monthly JSONL file (bot_data/logs/events-YYYY-MM.jsonl). Each line:
#   {"ts": ..., "utc": "...", "kind": "...", ...fields}
# Kinds: broadcast | mode | poll | poll_fail | mass_seal | mass_unseal |
#        manual_seal | manual_unseal | pending_unseal | pending_unseal_stuck |
#        day_rollover | command | wave_trigger | seal_repair | quarantine |
#        quarantine_release | chat_migrated | project_discovered | arise_refused
# Best-effort by design: a logging failure prints one line and never breaks
# the bot. Files are small (a quiet month is well under 1 MB); prune by hand.
_LOG_LOCK = threading.Lock()

# Per-thread org context. Each org's poll / concurrency thread and every worker
# acting on one org sets it (_org_context), so console lines and intel-log events
# say which org they belong to — both orgs run the same code concurrently, and
# both have a "Default project". Context only TAGS output; every decision that
# touches an org (keys, caps, state, broadcasts) gets the org passed explicitly.
_CTX = threading.local()


class _org_context:
    """`with _org_context(org):` — tag this thread's console + log output."""
    def __init__(self, org):
        self.org = org

    def __enter__(self):
        self.prev = getattr(_CTX, "org", None)
        _CTX.org = self.org

    def __exit__(self, *exc):
        _CTX.org = self.prev


def print(*args, **kwargs):   # noqa: A001 — module-local: prefixes the thread's org
    """Console output (→ stdout log): tags the thread's org and redacts the bot
    token — a failed Telegram call's exception text contains the request URL,
    which contains the token, and it was landing in stdout-*.log in plain text."""
    org = getattr(_CTX, "org", None)
    if org is not None and args:
        args = (f"[{org.id}] {args[0]}",) + args[1:]
    token = globals().get("BOT_TOKEN")
    if token:
        args = tuple(str(a).replace(token, "<bot-token>") for a in args)
    builtins.print(*args, **kwargs)


def _log_event(kind: str, **fields) -> None:
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        now  = datetime.now(timezone.utc)
        path = LOGS_DIR / f"events-{now.strftime('%Y-%m')}.jsonl"
        ctx_org = getattr(_CTX, "org", None)
        if "org" not in fields and ctx_org is not None:
            fields["org"] = ctx_org.id
        rec  = {"ts": round(time.time(), 3),
                "utc": now.strftime("%Y-%m-%d %H:%M:%S"),
                "kind": kind, **fields}
        with _LOG_LOCK:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[intel-log error] {e}")

REQUEST_TIMEOUT  = 15
POLL_TIMEOUT     = 30  # Telegram long-poll

# ── Token milestone config ──────────────────────────────────────────────────
# (fraction of the org's daily cap, level)  level: "casual" | "urgent" | "cap".
# Each org scales these to its own caps (Org.normal_milestones / .premium_milestones)
# — Lab 2 (10M / 1M) gets exactly the old ladders: 1M 4M 7M 8M 9M 10M and
# 200k 500k 800k 1M; Lab 3 (2.5M / 250K) gets the same shape, 4x smaller.
NORMAL_MILESTONE_FRACTIONS = (
    (0.10, "casual"),
    (0.40, "casual"),
    (0.70, "casual"),
    (0.80, "urgent"),
    (0.90, "urgent"),
    (1.00, "cap"),       # switches to spend-based alerting from here
)
PREMIUM_MILESTONE_FRACTIONS = (
    (0.20, "casual"),
    (0.50, "casual"),
    (0.80, "urgent"),
    (1.00, "cap"),
)

# ── Concurrent project detection ────────────────────────────────────────────
CONCURRENCY_THRESHOLD   = 3    # alert if this many projects active simultaneously
CONCURRENCY_WINDOW_MINS = 5    # "active" = had requests within last N minutes
CONCURRENCY_COOLDOWN    = 900  # seconds between concurrency alerts (15 min)

# ── Overcap detection window ─────────────────────────────────────────────────
# OpenAI usage API has a 5–15 min ingestion lag. The concurrency window (5 min)
# is too narrow — by the time data appears, the activity is already outside it.
# Use a wider window for overcap checks so recent-but-delayed data is caught.
OVERCAP_WINDOW_MINS = 20

# ── Project sealing (rate-limit throttle) ────────────────────────────────────
# When sealing, every rate-limit field present on a row is POSTed back as 0.
# Empirically the Admin API accepts 0 for max_requests_per_1_minute,
# max_tokens_per_1_minute, and the other per-model maxima — guaranteeing the
# project cannot make a single successful token-burning call. See _seal_payload.
#
# Concurrency model: one atomic "busy" claim PER ORG serialises all rate-limit
# work on that org (auto threshold/wave sweep, day-rollover restore, quarantine,
# repair, manual button ops). Callers ASK to claim the flag — if another op on
# the same org is in progress they bail immediately instead of blocking. Orgs
# are independent (separate keys, projects and limits), so a Lab 2 sweep never
# delays a Lab 3 seal or rollover. Manual ops dispatched from the Telegram
# callback handler run in a background worker thread, so the poll loops are
# never frozen by a 30-50 s sweep.
#
# Previous design used a `threading.Lock` held for the entire operation. Two bugs:
#   1. The TOCTOU race — callbacks read `_SEAL_BUSY` without holding the lock,
#      then `with _SEAL_LOCK:` would block the Telegram thread until the running
#      op finished, freezing every command for 30-50 s.
#   2. No worker thread — even the "successful" callback path ran inline, so the
#      Telegram poll loop couldn't fetch new updates while a seal/unseal ran.


def _try_claim_busy(org: "Org") -> bool:
    """Atomic check-and-set. Returns True iff this caller now holds `org`'s
    seal-busy claim — caller MUST `_release_busy(org)` in a `finally`."""
    with org.busy_lock:
        if org.busy:
            return False
        org.busy = True
        return True


def _release_busy(org: "Org") -> None:
    with org.busy_lock:
        org.busy = False


def _is_busy(org: "Org") -> bool:
    with org.busy_lock:
        return org.busy

# ── Track-level mass-seal trigger ──────────────────────────────────────────
# When a track's remaining quota drops to its per-track fraction (or below),
# the bot mass-throttles every project's rate-limit rows for that track.
#
# Buffers are PER-TRACK because of "the wave": the usage API lags 5–15 min, the
# sweep takes time, and in-flight requests keep landing after throttle — so the
# buffer must absorb the REPORTING BLIND SPOT (tokens already spent but not yet
# visible), not just future burn.
#
# Measured blind spot = (end-of-day total) − (total observed when the seal fired):
#   2026-08-19:  43k     2026-08-20: 137k     2026-08-26: 166k  <- worst
# The 15% buffer (150k) fell ~21k short on 2026-08-26 ($0.097 of overage) even
# though all 13 projects sealed with zero failures. 20% (200k) clears the worst
# observed case by 34k. It costs nothing in practice: across 14 days no day ever
# ended between 800k and 850k, and every day that reached 850k blew past 1M
# anyway.
#
# Normal's 5% buffer (500k) was OUTRUN for the first time on 2026-09-15: the
# usage API jumped 8.88M -> 10.08M in one reporting interval (~1.2M in ~7 min,
# ~2,900 tok/s — 26x the premium wave), so the first reading past the 9.5M
# threshold was already past the 10M cap. Cost only $0.21, because normal-lane
# overage is cheap. Single data point, and an exempt project's burn muddies the
# post-seal numbers, so the threshold is unchanged — resize if it recurs.
#
# Both buffers were measured on Lab 2 (10M / 1M). Lab 3 (2.5M / 250K, created
# 2026-09-29) starts on the same PERCENTAGES — seal at 2.375M / 200k — which is
# a proportional guess, not a measurement: its 50k premium buffer is far below
# Lab 2's worst 166k blind spot, while its per-model TPM limits (≤500k/min) can
# outrun any buffer. Re-derive from Lab 3's own intel log once it has usage.
NORMAL_SEAL_REMAINING_PCT      = 0.05   # Lab 2: seal at 9.5M (500k buffer)
PREMIUM_SEAL_REMAINING_PCT     = 0.20   # Lab 2: seal at 800k (200k buffer — see note)

# ── Wave guard (predictive seal + tight polling near the threshold) ────────
# Static thresholds alone can't catch a fast ramp: consumption visible NOW is
# already 5–15 min old. The wave guard projects each track forward by the
# lookahead window and seals early when the projection crosses the cap.
#
# v1 extrapolated the single poll-to-poll delta and got fooled: OpenAI's
# ingestion is LUMPY — delayed data lands in chunks, so one poll can show a
# +27k jump "in 60 s" that really accumulated over 10 min. On 2026-08-14 one
# such chunk read as ~450 tok/s and the bot sealed premium at 46%, wasting
# free quota. Three gates now make the predictor burst-resistant:
#   1. Rate is measured over a sliding sample WINDOW (≥2 min, ≤10 min of
#      polls) — a chunk gets diluted to its true average rate.
#   2. Armed only at ≥60% utilization — below that, even a genuine rocket
#      leaves ample runway and the static threshold is the primary defense.
#   3. The projection must cross the cap on 2 CONSECUTIVE polls — a chunk
#      artifact spikes exactly one interval; a real wave persists.
WAVE_LOOKAHEAD_SECS      = 20 * 60   # ingestion lag (≤15 min) + sweep time
WAVE_RATE_WINDOW_SECS    = 10 * 60   # rate-measurement window (dilutes chunks)
WAVE_MIN_SPAN_SECS       = 120       # need ≥2 min of samples before projecting
WAVE_MIN_UTILIZATION_PCT = 0.60      # predictive armed only above this fraction of cap
WAVE_CONFIRM_POLLS       = 2         # consecutive over-cap projections required
WAVE_WATCH_SLEEP_SECS    = 60        # poll cadence inside the watch zone
WAVE_WATCH_BAND_PCT      = 0.10      # watch zone starts 10% of cap below the seal threshold
SEAL_SWEEP_WORKERS       = 4         # parallel per-project workers for the mass sweep
ROLLOVER_DEFER_MAX_SECS  = 600       # max wait for an in-flight seal op before a forced day rollover


def _wave_projected(tok_now: int, tok_prev: int, dt_secs: float,
                    lookahead: int = WAVE_LOOKAHEAD_SECS) -> int:
    """Project consumption `lookahead` seconds forward at the burn rate
    measured across the sample window. Negative deltas clamp to 0."""
    rate = max(0.0, tok_now - tok_prev) / max(1.0, dt_secs)
    return int(tok_now + rate * lookahead)


class _WaveGuard:
    """Per-track burst-resistant predictive trigger (see gate rationale above).
    Feed every poll via observe(); it returns None, or {"rate", "projected"}
    when an early seal is warranted. Sample history and confirmation streaks
    reset on UTC day change."""

    def __init__(self):
        self._date    = None
        self._samples: dict[str, list] = {}   # track -> [(ts, tokens), …]
        self._streak:  dict[str, int]  = {}

    def observe(self, date: str, track: str, tok: int, cap: int,
                threshold: int, already_sealed: bool,
                now_ts: float = None) -> Optional[dict]:
        if now_ts is None:
            now_ts = time.time()
        if date != self._date:
            self._date, self._samples, self._streak = date, {}, {}

        dq = self._samples.setdefault(track, [])
        dq.append((now_ts, tok))
        while dq and now_ts - dq[0][0] > WAVE_RATE_WINDOW_SECS:
            dq.pop(0)

        # Gate 0: static path already covers it / nothing left to protect.
        if already_sealed or tok >= threshold:
            self._streak[track] = 0
            return None
        # Gate 2: utilization floor.
        if tok < cap * WAVE_MIN_UTILIZATION_PCT:
            self._streak[track] = 0
            return None
        # Gate 1: windowed rate needs a meaningful span.
        t0, tok0 = dq[0]
        span = now_ts - t0
        if span < WAVE_MIN_SPAN_SECS or len(dq) < 2:
            return None
        projected = _wave_projected(tok, tok0, span)
        if projected < cap:
            self._streak[track] = 0
            return None
        # Gate 1b: the LAST interval must also project over cap. The windowed
        # average stays hot for minutes after a wave dies; conversely a lone
        # ingestion chunk is hot instantaneously but dilutes in the window.
        # Only a genuinely sustained wave passes both.
        t_prev, tok_prev = dq[-2]
        inst_projected = _wave_projected(tok, tok_prev, now_ts - t_prev)
        if inst_projected < cap:
            self._streak[track] = 0
            return None
        # Gate 3: consecutive confirmation.
        self._streak[track] = self._streak.get(track, 0) + 1
        if self._streak[track] < WAVE_CONFIRM_POLLS:
            return None
        self._streak[track] = 0
        return {"rate": (tok - tok0) / max(1.0, span), "projected": projected}


def _matches_track(model: str, track: str) -> bool:
    """True if `model` belongs to the named track ('normal' or 'premium').
    Uses strict prefix matching via _track_for_model — unlisted models return
    False for BOTH tracks. The bot only seals/unseals models in OpenAI's
    explicit daily-free-quota lists; everything else is left alone."""
    if track not in ("normal", "premium"):
        raise ValueError(f"unknown track: {track!r}")
    return _track_for_model(model) == track

# ── Organizations ───────────────────────────────────────────────────────────
# The bot watches several OpenAI orgs from one process (one Telegram bot token
# can only have one poller). Each org has its own admin key, free-tier caps,
# projects, state file and enforcement threads; model classification and the
# Telegram side are shared.
#
#   free       — the daily free allowance (normal, premium): milestones, the
#                "allowance exhausted" alert, the lane lines. Lab 3 is on usage
#                tier 1-2, hence 4x smaller (OpenAI's offer: 2.5M / 250K per day).
#   ceiling    — where enforcement works: seal points (ceiling minus the track
#                buffer), the wave guard and the overcap alarm. Normally = free.
#                Lab 3's is Lab 2's allowance ON PURPOSE (Bach, 2026-09-30): it
#                has to pay its way up the usage tiers, so usage past its free
#                allowance is wanted — up to the same daily room as Lab 2.
#   daily_limit— the spend alarm (USD/day); the spend ladders scale with it.
#                Lab 3's covers its intended paid usage (≈ $2–6/day at both
#                ceilings, from Lab 2's measured overage prices) with room to
#                spare, so the alarm still means "something unplanned is billing".
ORG_SPECS = (
    dict(id="lab2", label="Business AI Lab 2", key_env="OPENAI_ADMIN_KEY",
         free=(10_000_000, 1_000_000), ceiling=(10_000_000, 1_000_000), daily_limit=2.00,
         state="usage_state.json", cache="projects.json"),
    dict(id="lab3", label="Business AI Lab 3", key_env="OPENAI_ADMIN_KEY_LAB3",
         free=(2_500_000, 250_000), ceiling=(10_000_000, 1_000_000), daily_limit=10.00,
         state="usage_state_lab3.json", cache="projects_lab3.json"),
)

# Offline project SEEDS (IDs case-sensitive). The live list comes from the Admin
# API at startup and hourly — see _sync_projects — so a new project is covered
# without a code change. Keep these current anyway: they are what the bot knows
# if it boots with the network down and no projects cache.
SEED_PROJECTS: dict[str, dict[str, str]] = {"lab2": {
    "proj_Gkm7qFbBFgmW11VFtO13Uw3F": "Default project",
    "proj_9su0tGI8NsaLE7LHqikCw8VE": "cngvng-project",
    "proj_4VPu8UTHzBpZiHFQVaYG923d": "hoangha-project",
    "proj_fvkY21dJ0ripiOIA2jCC86f3": "namvuong-project",
    "proj_fEboQnaVm4tQCk8kFy0h8s08": "khonlanh-project",
    "proj_zRWDq4YWIDEkxbgMAjX0xy79": "phongnguyen-project",
    "proj_J4rNEXilII2l889OotmE7YNW": "ngjabach-project",
    "proj_OWrxxJaWk5MXHBi3HIdPxBDh": "oduong-project",
    "proj_C51oeo4LjmiQefinVfoI8Rs0": "duyanh-project",
    "proj_cEHeqXeLfsJ6jrQhOXDlt9wH": "minhphung-project",
    "proj_wmeni3BelwvPUahovs5wQy3i": "kong-project",
    "proj_E8F4KEaZSMfBuaPhE3Y69BzM": "ngocvo-project",
    "proj_MIieWaC8hSsgAp4rSaN86BEp": "tubel-project",
    "proj_bUTBrctRsITbimsCqGg3VuJR": "giaotien-project",
}, "lab3": {
    "proj_zo1iaAChFX81OMGwxRStD76g": "Default project",
}}


class Org:
    """One monitored OpenAI organization: identity, admin key, free allowance,
    enforcement ceiling + seal points, spend ladders, project table + archive-
    button index (REBOUND on discovery, never mutated in place — see
    _merge_projects), per-org state paths, the /refresh wake-up event, and the
    per-org busy claim. `*_cap` = free allowance, `*_ceiling` = enforcement."""

    def __init__(self, spec: dict, key: str, seed: dict):
        self.id, self.label, self.key = spec["id"], spec["label"], key
        self.short = self.label.replace("Business AI ", "")          # "Lab 2"
        self.normal_cap, self.premium_cap = spec["free"]
        self.normal_ceiling, self.premium_ceiling = spec.get("ceiling", spec["free"])
        self.normal_threshold  = int(self.normal_ceiling  * (1 - NORMAL_SEAL_REMAINING_PCT))
        self.premium_threshold = int(self.premium_ceiling * (1 - PREMIUM_SEAL_REMAINING_PCT))
        self.normal_milestones  = [(int(self.normal_cap  * f), lvl) for f, lvl in NORMAL_MILESTONE_FRACTIONS]
        self.premium_milestones = [(int(self.premium_cap * f), lvl) for f, lvl in PREMIUM_MILESTONE_FRACTIONS]
        # Spend ladders scale with the org's alarm; Lab 2 ($2) gets the base ones.
        self.daily_limit = float(spec.get("daily_limit", DAILY_LIMIT))
        k = self.daily_limit / DAILY_LIMIT
        self.spend_milestones = [(round(t * k, 2), lvl) for t, lvl in SPEND_MILESTONES]
        self.spend_overcap_step = round(SPEND_OVERCAP_STEP * k, 2)
        self.project_spend_thresholds = tuple(round(t * k, 2) for t in PROJECT_SPEND_THRESHOLDS)
        self.state_path  = BOT_DATA_DIR / spec["state"]
        self.cache_path  = BOT_DATA_DIR / spec["cache"]
        self.projects: dict[str, str] = dict(seed)
        self.project_index: list[str] = list(seed)
        self.poll_now  = threading.Event()   # /refresh → run a poll cycle now
        self.busy_lock = threading.Lock()
        self.busy      = False
        # API health (see _note_api_failure): last error seen by any fetcher.
        self.last_api_error: Optional[str] = None
        self.api_down_since: Optional[float] = None
        self.api_alerted = False

    def cap(self, track: str) -> int:
        """The track's daily FREE allowance."""
        return self.normal_cap if track == "normal" else self.premium_cap

    def ceiling(self, track: str) -> int:
        """The track's enforcement ceiling (= the free allowance unless the org
        deliberately pays past it)."""
        return self.normal_ceiling if track == "normal" else self.premium_ceiling

    def pays_past_free(self, track: str) -> bool:
        return self.ceiling(track) > self.cap(track)

    def threshold(self, track: str) -> int:
        return self.normal_threshold if track == "normal" else self.premium_threshold

    def __repr__(self) -> str:
        return f"Org({self.id})"


def _build_orgs() -> dict:
    """Orgs whose admin key is configured, in display order. A key already used
    by an earlier org is refused: with Lab 2's key pasted into the Lab 3 slot,
    "Lab 3" would discover Lab 2's projects and seal them at Lab 3's 4x-lower
    thresholds."""
    orgs, seen = {}, {}
    for spec in ORG_SPECS:
        label, env = spec["label"], spec["key_env"]
        key = os.environ.get(env, "").strip()
        if not key:
            continue
        if key in seen:
            print(f"[config] {label}: {env} holds the same key as {seen[key]} — NOT monitored "
                  f"(it would act on {seen[key]}'s projects)")
            continue
        seen[key] = label
        orgs[spec["id"]] = Org(spec, key, SEED_PROJECTS.get(spec["id"], {}))
    return orgs


ORGS: dict[str, Org] = _build_orgs()


def _pname(pid: str) -> str:
    """Display name for a project id, whichever org owns it (ids are unique)."""
    for org in ORGS.values():
        name = org.projects.get(pid)
        if name:
            return name
    return pid

OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"
OPENAI_USAGE_URL = "https://api.openai.com/v1/organization/usage/completions"

# ── Free-tier model classification (from OpenAI's free-usage page) ─────────
# SOURCE OF TRUTH — re-check when OpenAI updates the offer:
#   https://help.openai.com/en/articles/10306912-sharing-feedback-evaluation-and-fine-tuning-data-and-api-inputs-and-outputs-with-openai
#   (section: "What models are included in this offer?")
# Last synced: 2026-09-22 (verified against a fresh pull of the article — no
# model added, removed, or moved between groups since 2026-08-21).
#
# OpenAI lists DATED SNAPSHOTS (e.g. "gpt-5.4-2026-03-05"). We store the BASE
# name; _is_listed_variant accepts the exact name or base + a -YYYY-MM-DD
# snapshot suffix, so one entry covers every dated snapshot of that model while
# same-prefix paid products (gpt-5.5-pro, o1-pro, gpt-4o-mini-tts, …) stay
# unlisted. Quota is SHARED across each group. Excluded by OpenAI regardless of
# name: fine-tuned models, fine-tuning training, evals, and tool use.
#
# NOTE ON TIERS: the groups are 1M / 10M for usage tier 3+, but only
# 250K / 2.5M for tiers 1-2 — set per org in ORG_SPECS (Lab 2 is tier 3+,
# Lab 3 is tier 1-2). The model lists below are the same for every org.
#
# Normal-band models share the org's normal allowance:
NORMAL_MODEL_PREFIXES = (
    "gpt-5.6-terra", "gpt-5.6-luna",
    "gpt-5.4-mini", "gpt-5.4-nano",
    "gpt-5.1-codex-mini",
    "gpt-5-mini", "gpt-5-nano",
    "gpt-4.1-mini", "gpt-4.1-nano",
    "gpt-4o-mini",
    "o4-mini",
    "o1-mini",
    "codex-mini-latest",
)
# Premium-band models share the org's premium allowance:
PREMIUM_MODEL_PREFIXES = (
    "gpt-5.6-sol",
    "gpt-5.5",
    "gpt-5.4", "gpt-5.2",
    "gpt-5.1-codex", "gpt-5.1",
    "gpt-5-codex", "gpt-5-chat-latest", "gpt-5",
    "gpt-4.5-preview",          # deprecated & shut down 2025-07-14; listed for completeness
    "gpt-4.1",
    "gpt-4o",
    "o3",
    "o1-preview", "o1",
)

# ── Terminal colors (ANSI) ──────────────────────────────────────────────────
_C_GREEN  = "\033[32m"
_C_YELLOW = "\033[33m"
_C_RED    = "\033[31m"
_C_RESET  = "\033[0m"

def _tok_color(tokens: int, hard_cap: int) -> str:
    if tokens >= hard_cap:
        return _C_RED
    if tokens >= hard_cap * 0.7:   # top 30% before cap → yellow
        return _C_YELLOW
    return _C_GREEN

def _color(text: str, code: str) -> str:
    return f"{code}{text}{_C_RESET}"


# ── Model band classifier ──────────────────────────────────────────────────

# Suffix that marks a date-stamped snapshot of the SAME model (e.g.
# "gpt-4o-mini-2024-07-18"). Only these variants inherit the base model's
# free-tier classification.
_SNAPSHOT_SUFFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_listed_variant(m: str, p: str) -> bool:
    """True iff `m` IS the listed model `p`: exact name, or `p` plus a
    date-stamp snapshot suffix. Any other suffix is a DIFFERENT paid product —
    o1-pro ($150/$600!), gpt-5.4-pro, gpt-4o-mini-tts, gpt-4o-transcribe,
    gpt-5.4-cyber, gpt-5.2-chat-latest, gpt-5-search-api all share a listed
    prefix but bill at standard rates and are NOT covered by the free tier."""
    if m == p:
        return True
    if not m.startswith(p + "-"):
        return False
    return bool(_SNAPSHOT_SUFFIX_RE.match(m[len(p) + 1:]))


def _track_for_model(model: str) -> Optional[str]:
    """Return the free-tier track a model belongs to, or None if it's not on
    either of OpenAI's two daily-free-quota lists.

    Strict match via _is_listed_variant: exact name or date-stamped snapshot
    only. This is deliberately conservative — a misclassified-as-unlisted model
    costs at most one noisy anomaly alert per day, while a misclassified-as-listed
    model silently absorbs standard-rate spend into the "free" bucket (the o1-pro
    failure mode: prefix-matched into premium, no alert, $150/1M input).

    Normal is checked first because its prefixes are more specific (e.g.
    `gpt-5.4-mini` before `gpt-5.4`). No heuristic fallback — unlisted models
    (sora-2, babbage-002, gpt-3.5-turbo, embeddings, *-pro, *-tts, etc.) return
    None: NOT touched by seal/unseal, NOT counted toward the 1M / 10M buckets,
    and their first use today trips the off-watchlist anomaly alert."""
    m = model.lower()
    for p in NORMAL_MODEL_PREFIXES:
        if _is_listed_variant(m, p):
            return "normal"
    for p in PREMIUM_MODEL_PREFIXES:
        if _is_listed_variant(m, p):
            return "premium"
    return None


# ── Time helpers ───────────────────────────────────────────────────────────

def today_window(now: datetime = None) -> tuple[int, int]:
    now   = now or datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = max(int(start.timestamp()) + 1, int(now.timestamp()))
    return int(start.timestamp()), end


def today_window_costs() -> tuple[int, int]:
    """Window for today's costs query.
    end_time is set to tomorrow's midnight so the daily bucket always spans a
    full date range (the API compares dates, not timestamps — same-day start/end
    triggers a 400 even when end_ts > start_ts). Future hours simply return no
    data. The 10-minute ingestion lag is irrelevant here since we're not
    using end_time to bound live data."""
    now       = datetime.now(timezone.utc)
    start     = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow  = start + timedelta(days=1)
    return int(start.timestamp()), int(tomorrow.timestamp())


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def month_window(year: int, month: int) -> tuple[int, int]:
    """Return (start_ts, end_ts) for a full calendar month."""
    start_dt = datetime(year, month, 1, tzinfo=timezone.utc)
    _, last_day = calendar.monthrange(year, month)
    now = datetime.now(timezone.utc)
    if year == now.year and month == now.month:
        # Tomorrow's midnight, not now: the costs API compares DATES, and on the
        # 1st a same-date start/end returns 400 — @spending showed "no spend".
        end_ts = int((now.replace(hour=0, minute=0, second=0, microsecond=0)
                      + timedelta(days=1)).timestamp())
    else:
        end_ts = int(datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    return int(start_dt.timestamp()), end_ts


def prev_month() -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    if now.month == 1:
        return now.year - 1, 12
    return now.year, now.month - 1


# ── OpenAI API ─────────────────────────────────────────────────────────────

def _openai_headers(org: Org) -> dict:
    """Auth for `org`. No default on purpose: a call that forgot its org must
    fail loudly, not quietly query (or seal through) the other org."""
    return {"Authorization": f"Bearer {org.key}"}


# Network-level failures are transient on this host: 67 DNS resolution failures
# in ~18 h on 2026-09-15. One of them used to fail an entire 186-row quarantine
# sweep (the seal aborts and rolls back on the first failed POST), leaving the
# project unthrottled until the next poll. Retry those in place. HTTP error
# RESPONSES are deliberately not retried — they are answers, not blips.
#
# 2026-09-22: the same DNS flakiness dropped a mass-seal alert to Telegram
# (sendMessage) with no retry on that path — the alert survived only because
# _broadcast mirrors it to the local intel log first. Telegram calls now share
# this same retry loop.
_NET_RETRY_BACKOFF = (1, 2)   # seconds before attempts 2 and 3


def _retrying_call(method: str, url: str, **kwargs):
    """requests.get/post to a remote API, retrying DNS / connection / timeout
    errors up to twice. Re-raises the last error if every attempt fails, so
    every caller's existing `except Exception` handling still applies."""
    for attempt in range(len(_NET_RETRY_BACKOFF) + 1):
        try:
            return getattr(requests, method)(url, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt == len(_NET_RETRY_BACKOFF):
                raise
            time.sleep(_NET_RETRY_BACKOFF[attempt])


_openai_call    = _retrying_call   # OpenAI Admin API call sites
_telegram_call  = _retrying_call   # Telegram Bot API call sites


LANES = ("normal", "premium", "exotic")


def _lane_for_line_item(line_item: Optional[str]) -> str:
    """Map a costs-API line item to its lane.

    Line items look like "gpt-audio-mini-2025-12-15 audio, input" or
    "gpt-5.4-mini-2026-03-17, cached input": the model is the first token before
    the comma, optionally followed by a modality word. Anything that is not a
    normal/premium model is EXOTIC — including non-model items such as web
    search or storage — so every non-free-tier dollar is visible and the three
    lanes always sum to the total."""
    head = (line_item or "").split(",")[0].strip()
    model = head.split()[0] if head else ""
    return _track_for_model(model) or "exotic"


def _fetch_costs_breakdown(org: Org) -> Optional[tuple[dict, dict]]:
    """Today's billed cost for `org`, grouped by project AND line item in one call.
    Returns (per_project, per_lane) — per_project keyed by project id with
    '__org__' for unattributed spend; per_lane keyed by LANES. Returns None on
    API failure (distinguished from ({}, zeros) = no spend today).

    The costs API reports actual billing, so a free-tier lane reads $0 until
    its allowance is exhausted and only overage appears."""
    start, end = today_window_costs()
    params = [
        ("start_time",   start),
        ("end_time",     end),
        ("bucket_width", "1d"),
        ("group_by[]",   "project_id"),
        ("group_by[]",   "line_item"),
        ("limit",        100),
    ]
    costs: dict[str, float] = {}
    lanes: dict[str, float] = {l: 0.0 for l in LANES}
    page = None
    fetched_any_page = False
    while True:
        p = list(params)
        if page:
            p.append(("page", page))
        try:
            r = _openai_call("get", OPENAI_COSTS_URL, headers=_openai_headers(org), params=p, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            print(f"[openai costs network error] {e}")
            org.last_api_error = type(e).__name__
            return None if not fetched_any_page else (costs, lanes)
        if not r.ok:
            print(f"[openai costs {r.status_code}] {r.text[:500]}")
            org.last_api_error = f"HTTP {r.status_code}"
            return None if not fetched_any_page else (costs, lanes)
        fetched_any_page = True
        data = r.json()
        for bucket in data.get("data", []):
            for result in bucket.get("results", []):
                pid = result.get("project_id") or "__org__"
                val = float(result.get("amount", {}).get("value", 0.0))
                costs[pid] = costs.get(pid, 0.0) + val
                lanes[_lane_for_line_item(result.get("line_item"))] += val
        if not data.get("has_more"):
            break
        page = data.get("next_page")
        if not page:
            break
    return costs, lanes


def _fetch_tokens(org: Org, window: tuple[int, int] = None) -> Optional[dict[str, dict]]:
    """Today's token usage per project of `org`, broken down by model and band.
    Returns None on API failure (distinguished from {} = no usage today). The
    distinction matters: empty-day must NOT block polling — that creates a window
    where the first request of the day goes undetected."""
    start, end = window or today_window()
    params = [
        ("start_time",   start),
        ("end_time",     end),
        ("bucket_width", "1h"),
        ("group_by[]",   "project_id"),
        ("group_by[]",   "model"),
        ("limit",        100),
    ]
    tokens: dict[str, dict] = {}
    page = None
    fetched_any_page = False
    while True:
        p = list(params)
        if page:
            p.append(("page", page))
        try:
            r = _openai_call("get", OPENAI_USAGE_URL, headers=_openai_headers(org), params=p, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            print(f"[openai usage network error] {e}")
            org.last_api_error = type(e).__name__
            return None if not fetched_any_page else tokens
        if not r.ok:
            print(f"[openai usage {r.status_code}] {r.text[:500]}")
            org.last_api_error = f"HTTP {r.status_code}"
            return None if not fetched_any_page else tokens
        fetched_any_page = True
        data = r.json()
        for bucket in data.get("data", []):
            for result in bucket.get("results", []):
                pid   = result.get("project_id", "")
                model = result.get("model", "unknown")
                inp   = result.get("input_tokens", 0)
                out   = result.get("output_tokens", 0)
                reqs  = result.get("num_model_requests", 0)
                if not pid:
                    continue
                if pid not in tokens:
                    tokens[pid] = {
                        "input_tokens": 0, "output_tokens": 0,
                        "total_tokens": 0, "num_requests": 0,
                        "premium_tokens": 0, "normal_tokens": 0,
                        "models": {},
                    }
                tokens[pid]["input_tokens"]  += inp
                tokens[pid]["output_tokens"] += out
                tokens[pid]["total_tokens"]  += inp + out
                tokens[pid]["num_requests"]  += reqs
                track = _track_for_model(model)
                if track == "premium":
                    tokens[pid]["premium_tokens"] += inp + out
                elif track == "normal":
                    tokens[pid]["normal_tokens"]  += inp + out
                # else: unlisted model (paid-rate from token 1) — counts in
                # total_tokens but not toward either free-tier bucket. Won't push
                # the track-seal threshold and won't be touched by mass throttle.
                m = tokens[pid]["models"].setdefault(model, {"input": 0, "output": 0, "requests": 0})
                m["input"] += inp; m["output"] += out; m["requests"] += reqs
        if not data.get("has_more"):
            break
        page = data.get("next_page")
        if not page:
            break
    return tokens


def _fetch_monthly_costs(org: Org, year: int, month: int) -> Optional[dict[str, float]]:
    """Cost per project of `org` for a full calendar month. '__org__' key for
    unattributed costs. None on failure — an error must not read as "$0 spent"."""
    start, end = month_window(year, month)
    params = [
        ("start_time",   start),
        ("end_time",     end),
        ("bucket_width", "1d"),
        ("group_by[]",   "project_id"),
        ("limit",        100),
    ]
    costs: dict[str, float] = {}
    page = None
    while True:
        p = list(params)
        if page:
            p.append(("page", page))
        try:
            r = _openai_call("get", OPENAI_COSTS_URL, headers=_openai_headers(org), params=p, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            print(f"[openai monthly costs error] {e}")
            org.last_api_error = type(e).__name__
            return None
        if not r.ok:
            print(f"[openai monthly costs {r.status_code}] {r.text[:300]}")
            org.last_api_error = f"HTTP {r.status_code}"
            return None
        data = r.json()
        for bucket in data.get("data", []):
            for result in bucket.get("results", []):
                pid = result.get("project_id") or "__org__"
                val = float(result.get("amount", {}).get("value", 0.0))
                costs[pid] = costs.get(pid, 0.0) + val
        if not data.get("has_more"):
            break
        page = data.get("next_page")
        if not page:
            break
    return costs


def _fetch_recent_activity(org: Org, minutes: int = CONCURRENCY_WINDOW_MINS) -> Optional[dict[str, int]]:
    """Request count per project of `org` in the last `minutes` minutes (minute-level buckets).
    Returns None on API failure so callers can preserve the previous snapshot
    instead of overwriting it with a misleading empty dict."""
    now   = int(time.time())
    start = now - minutes * 60
    params = [
        ("start_time",   start),
        ("end_time",     now),
        ("bucket_width", "1m"),
        ("group_by[]",   "project_id"),
        ("limit",        100),
    ]
    try:
        r = _openai_call("get", OPENAI_USAGE_URL, headers=_openai_headers(org), params=params, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        print(f"[openai activity error] {e}")
        return None
    if not r.ok:
        print(f"[openai activity {r.status_code}] {r.text[:300]}")
        return None
    activity: dict[str, int] = {}
    for bucket in r.json().get("data", []):
        for result in bucket.get("results", []):
            pid  = result.get("project_id", "")
            reqs = result.get("num_model_requests", 0)
            if pid and reqs > 0:
                activity[pid] = activity.get(pid, 0) + reqs
    return activity


def _fetch_recent_activity_by_band(org: Org, minutes: int) -> Optional[dict[str, dict[str, int]]]:
    """Per-project recent request counts of `org` broken down by model band.
    Returns {pid: {"normal": <reqs>, "premium": <reqs>}} for the last `minutes` minutes,
    or None on API failure. Used by overcap detection to filter projects to only those
    actually using the EXCEEDED band — a project burning premium tokens does not
    trigger the normal-cap alarm and vice versa."""
    now   = int(time.time())
    start = now - minutes * 60
    params = [
        ("start_time",   start),
        ("end_time",     now),
        ("bucket_width", "1m"),
        ("group_by[]",   "project_id"),
        ("group_by[]",   "model"),
        ("limit",        100),
    ]
    try:
        r = _openai_call("get", OPENAI_USAGE_URL, headers=_openai_headers(org), params=params, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        print(f"[openai activity-by-band error] {e}")
        return None
    if not r.ok:
        print(f"[openai activity-by-band {r.status_code}] {r.text[:300]}")
        return None
    out: dict[str, dict[str, int]] = {}
    for bucket in r.json().get("data", []):
        for result in bucket.get("results", []):
            pid   = result.get("project_id", "")
            model = result.get("model", "")
            reqs  = result.get("num_model_requests", 0)
            if not pid or reqs <= 0:
                continue
            band = _track_for_model(model)
            if band is None:
                continue   # unlisted model — paid-rate, not part of any track
            slot = out.setdefault(pid, {"normal": 0, "premium": 0})
            slot[band] += reqs
    return out


def _filter_to_exceeded_band(banded: dict[str, dict[str, int]],
                             normal_exceeded: bool, premium_exceeded: bool,
                             grace: dict[str, set] = None) -> dict[str, dict[str, int]]:
    """From banded recent activity, return only projects with usage on an exceeded
    band, skipping (band, project) pairs in `grace` (sealed too recently for the
    window to prove anything). Preserves the full per-band breakdown so the alert
    formatter can show detail."""
    grace = grace or {}
    out: dict[str, dict[str, int]] = {}
    for pid, bands in banded.items():
        hot_n = normal_exceeded and bands.get("normal", 0) > 0 and pid not in grace.get("normal", ())
        hot_p = premium_exceeded and bands.get("premium", 0) > 0 and pid not in grace.get("premium", ())
        if hot_n or hot_p:
            out[pid] = bands
    return out


# ── OpenAI Admin: project rate-limit API ───────────────────────────────────
OPENAI_RATE_LIMITS_URL_TMPL = "https://api.openai.com/v1/organization/projects/{pid}/rate_limits"


def _fetch_project_rate_limits(pid: str, *, org: Org) -> Optional[list[dict]]:
    """Return every rate-limit row for a project of `org` (one per model). None on API failure."""
    out: list[dict] = []
    params = [("limit", 100)]
    page = None
    url  = OPENAI_RATE_LIMITS_URL_TMPL.format(pid=pid)
    while True:
        p = list(params)
        if page:
            p.append(("after", page))
        try:
            r = _openai_call("get", url, headers=_openai_headers(org), params=p, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            print(f"[openai rate-limits GET error] {pid}: {e}")
            return None
        if not r.ok:
            print(f"[openai rate-limits GET {r.status_code}] {pid}: {r.text[:300]}")
            return None
        data = r.json()
        out.extend(data.get("data", []))
        if not data.get("has_more"):
            break
        page = data.get("last_id")
        if not page:
            break
    return out


# Rate-limit POSTs that fail with these codes are no-ops for sealing purposes:
#   - rate_limit_does_not_exist_for_org_and_model: org has no access to that model
#   - rate_limit_not_updatable:                    fine-tune / batch-only rows; not settable
#   - invalid_rate_limit_type:                     model doesn't support this RL field
#     (e.g. sora-2 rejects max_tokens_per_1_minute even though GET returns it)
# In all cases, the model isn't usable in a way that bypasses our throttle, so we
# treat the failure as a successful no-op rather than aborting the seal.
_SKIPPABLE_RATE_LIMIT_ERR_CODES = frozenset({
    "rate_limit_does_not_exist_for_org_and_model",
    "rate_limit_not_updatable",
    "invalid_rate_limit_type",
})


# "The max_requests_per_1_minute for rl-gpt-5-pro cannot exceed the
#  organization rate limit of 500.0" — GET can report a project value that POST
# then refuses because the ORG ceiling is lower. Observed live 2026-08-22 on
# every *-pro row: restoring a captured original 400'd, which made the whole
# restore fail and left the project sealed forever. Parse the ceiling and retry
# clamped to it.
_ORG_LIMIT_RE = re.compile(
    r"The (\w+) for \S+ cannot exceed the organization rate limit of ([\d.]+)")


def _update_project_rate_limit(pid: str, rate_limit_id: str, payload: dict, *,
                               org: Org, _attempts_left: int = 4) -> bool:
    """POST a partial update to a single rate-limit row of `org`'s project `pid`.
    Returns True on 2xx and on the soft-skip codes above. False on any other failure.

    On `organization_rate_limit_exceeded` the requested value is above the org
    ceiling. The API names ONE offending field per response, so clamp that field
    and retry — iteratively, since a row can exceed the ceiling on several fields
    (rpm first, then tpm). After `_attempts_left` rounds, soft-skip: leaving one
    row unrestorable must never strand a whole project in a sealed state."""
    url = f"{OPENAI_RATE_LIMITS_URL_TMPL.format(pid=pid)}/{rate_limit_id}"
    try:
        r = _openai_call(
            "post", url,
            headers={**_openai_headers(org), "Content-Type": "application/json"},
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:
        print(f"[openai rate-limits POST error] {pid}/{rate_limit_id}: {e}")
        return False
    if r.ok:
        return True
    err_code, err_msg = "", ""
    try:
        err = r.json().get("error", {}) or {}
        err_code, err_msg = err.get("code", "") or "", err.get("message", "") or ""
    except Exception:
        pass
    if err_code in _SKIPPABLE_RATE_LIMIT_ERR_CODES:
        return True   # soft skip — non-updatable / no org access
    if err_code == "organization_rate_limit_exceeded":
        m = _ORG_LIMIT_RE.search(err_msg)
        if m and _attempts_left > 0:
            field, ceiling = m.group(1), float(m.group(2))
            if field in payload and payload[field] > int(ceiling):
                clamped = dict(payload)
                clamped[field] = int(ceiling)
                print(f"[openai rate-limits] {pid}/{rate_limit_id}: {field} clamped "
                      f"to org ceiling {int(ceiling)} — retrying")
                return _update_project_rate_limit(pid, rate_limit_id, clamped, org=org,
                                                  _attempts_left=_attempts_left - 1)
        print(f"[openai rate-limits] {pid}/{rate_limit_id}: above org ceiling and "
              f"not clampable — skipping so the seal state can still clear")
        return True   # soft skip: never strand a project sealed over one row
    print(f"[openai rate-limits POST {r.status_code}] {pid}/{rate_limit_id}: {r.text[:300]}")
    return False


def _fetch_recent_usage(org: Org, days: int = 31) -> Optional[tuple[int, int]]:
    """(tokens, requests) for `org` over the last `days` calendar days, or None
    on API failure — an error must not read as "no usage"."""
    now      = datetime.now(timezone.utc)
    start_ts = int((now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0,
                                                             microsecond=0).timestamp())
    params = [
        ("start_time",   start_ts),
        ("end_time",     int(now.timestamp())),
        ("bucket_width", "1d"),
        ("limit",        31),            # API max for bucket_width=1d
    ]
    tokens = requests_n = 0
    page = None
    while True:
        p = list(params)
        if page:
            p.append(("page", page))
        try:
            r = _openai_call("get", OPENAI_USAGE_URL, headers=_openai_headers(org), params=p, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            print(f"[recent usage error] {e}")
            org.last_api_error = type(e).__name__
            return None
        if not r.ok:
            print(f"[recent usage {r.status_code}] {r.text[:200]}")
            org.last_api_error = f"HTTP {r.status_code}"
            return None
        data = r.json()
        for bucket in data.get("data", []):
            for result in bucket.get("results", []):
                tokens     += result.get("input_tokens", 0) + result.get("output_tokens", 0)
                requests_n += result.get("num_model_requests", 0)
        page = data.get("next_page")
        if not data.get("has_more") or not page:
            return tokens, requests_n


def fetch_today_usage(org: Org) -> Optional[dict]:
    """Tokens-only poll of `org` — no cost fetch (costs API is unreliable for frequent polling).
    Returns None only on actual API failure. An empty `tokens` dict (no usage yet today)
    yields a valid snap with an empty `projects` map — so the bot stays in its normal
    poll cadence and catches the first request the moment it appears, instead of
    sitting in backoff for hours on a quiet day."""
    # The date label must come from the same instant as the query window. A
    # fetch that starts at 23:59:5x and pages past midnight returns the OLD day's
    # totals; stamping it with a post-fetch today_str() carried yesterday's 24.88M
    # into the new day and fired a spurious mass seal (2026-09-29; also 09-02 and
    # 09-08 for premium). If midnight passed mid-fetch, re-fetch for the new day.
    for _ in range(2):
        now    = datetime.now(timezone.utc)
        date   = now.strftime("%Y-%m-%d")
        tokens = _fetch_tokens(org, today_window(now))
        if tokens is None:
            return None   # API genuinely failed
        if today_str() == date:
            break
    projects: dict[str, dict] = {}
    for pid, tok in tokens.items():
        projects[pid] = {
            "name":           org.projects.get(pid, pid),
            "input_tokens":   tok.get("input_tokens", 0),
            "output_tokens":  tok.get("output_tokens", 0),
            "total_tokens":   tok.get("total_tokens", 0),
            "premium_tokens": tok.get("premium_tokens", 0),
            "normal_tokens":  tok.get("normal_tokens", 0),
            "num_requests":   tok.get("num_requests", 0),
            "cost_usd":       0.0,
            "models":         tok.get("models", {}),
        }
    total_premium = sum(p["premium_tokens"] for p in projects.values())
    total_normal  = sum(p["normal_tokens"]  for p in projects.values())
    return {
        "date":                 date,
        "projects":             projects,
        "total_cost":           0.0,
        "total_premium_tokens": total_premium,
        "total_normal_tokens":  total_normal,
        "last_polled":          time.time(),
    }


def _enrich_costs(snap: dict, usage: "UsageStore", live: bool = True) -> dict:
    """Overlay `usage.org`'s costs onto a snapshot copy. costs=None means "API
    failure" — fall back to cache. costs={} means "successful fetch, no spend
    yet" — overlay zeros cleanly. Critical for matching the poll loop's empty-day
    handling."""
    snap  = copy.deepcopy(snap)
    breakdown = _fetch_costs_breakdown(usage.org) if live else None
    if breakdown is not None:
        costs, lane_costs = breakdown
        org_cost = costs.pop("__org__", 0.0)
        for pid, p in snap.get("projects", {}).items():
            p["cost_usd"] = round(costs.get(pid, 0.0), 6)
        snap["total_cost"] = round(sum(costs.values()) + org_cost, 6)
        snap["org_cost"]   = round(org_cost, 6)
        snap["lane_costs"] = {l: round(v, 6) for l, v in lane_costs.items()}
        usage.update_costs(costs, snap["total_cost"], org_cost, lane_costs, date=snap.get("date"))
    else:
        cached = usage.get_costs_cache()
        if cached:
            per_proj = cached.get("per_project", {})
            for pid, p in snap.get("projects", {}).items():
                p["cost_usd"] = round(per_proj.get(pid, 0.0), 6)
            snap["total_cost"] = cached.get("total", 0.0)
            snap["lane_costs"] = dict(cached.get("per_lane", {}))
            snap["costs_stale"] = True
            snap["costs_ts"]    = cached.get("ts")
    return snap


# ── Usage state store ──────────────────────────────────────────────────────

class UsageStore:
    """Persists ONE org's usage snapshot and alert-control state to disk.
    `self.org` is an attribute, never part of `_data`: update() replaces `_data`
    with each snapshot, so anything not in _PRESERVED vanishes every poll."""

    # Fields that must survive snapshot updates (not overwritten on each poll)
    _PRESERVED = (
        "token_milestones_notified",
        "premium_milestones_notified",
        "last_concurrent_alert_ts",
        "active_projects",
        "active_window_mins",
        "costs_cache",
        # mode management — must survive snapshot updates
        "bot_mode",
        "last_milestone_ts",
        "last_illegal_seen_ts",
        "urgent_poll_step",
        "milestones_seeded",
        # project sealing:
        #   sealed_tracks       — {track: {sealed_at, originals_by_project: {pid: [rows]}}}
        #                         holds every project currently throttled on a track,
        #                         whether by the mass auto-throttle or a manual seal.
        #   mass_sealed_tracks  — [track] whose mass sweep has fired today (threshold or wave)
        #                         (auto-trigger idempotency; manual single seals don't set it).
        #   track_exemptions    — {pid: [tracks]} the project was manually unsealed on today;
        #                         the mass sweep skips these.
        #   pending_track_unseal— day-rollover restore queue, same shape as sealed_tracks.
        "sealed_tracks",
        "mass_sealed_tracks",
        "pending_track_unseal",
        "track_exemptions",
        # spend monitoring (anomaly-detection layer that catches unlisted-model spend):
        #   spend_milestones_notified — org-wide $ thresholds already alerted today
        #   project_spend_notified    — {pid: [thresholds]} per-project $ thresholds
        #   unlisted_models_alerted   — {pid: [model]} (pid, model) pairs already alerted
        #                                today; prevents re-spam every poll
        #   spend_seeded              — True after the first-poll spend seed runs
        "spend_milestones_notified",
        "project_spend_notified",
        "unlisted_models_alerted",
        "spend_seeded",
    )

    def __init__(self, path: Path, org: Org):
        self.path  = path
        self.org   = org
        self._lock = threading.Lock()
        self._data: dict = {}
        self._rollover_wait_since: Optional[float] = None
        self._load()
        # If loaded state is from a previous day, reset daily fields immediately
        # so /tokens, /refresh, etc. don't surface yesterday's numbers in the
        # window between bot start and the first poll.
        persisted = self._data.get("date")
        today     = today_str()
        if persisted and persisted != today:
            with self._lock, _org_context(org):
                print(f"[store] Loaded state from {persisted} — resetting daily state for {today}")
                self._reset_daily_state_locked()
                self._data["date"] = today
                self._save()

    def _load(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception as e:
                # Loud warning — silent reset wipes sealed-project state.
                # Quarantine the bad file so it can be inspected after restart.
                quarantine = self.path.with_suffix(self.path.suffix + f".corrupt-{int(time.time())}")
                try:
                    os.replace(self.path, quarantine)
                    print(f"[store] CORRUPT state file at {self.path} ({e}) → moved to {quarantine}")
                except Exception as move_err:
                    print(f"[store] CORRUPT state file at {self.path} ({e}) — could not quarantine: {move_err}")
                self._data = {}

    def _save(self):
        _atomic_write_json(self.path, self._data)

    def _reset_daily_state_locked(self):
        """Reset all daily alert/mode state. Caller must hold self._lock.
        Everything currently sealed (mass or manual) moves into pending_track_unseal
        so the API restore happens on the next poll — keeps the lock cheap."""
        # Move sealed_tracks → pending_track_unseal, merging BY ROW ID into any
        # rows still queued from an earlier day. A per-project overwrite dropped
        # those older rows, stranding them at 0 forever.
        s_tracks = self._data.get("sealed_tracks", {})
        p_tracks = self._data.get("pending_track_unseal", {})
        for track, info in s_tracks.items():
            for pid, rows in (info.get("originals_by_project") or {}).items():
                dst   = p_tracks.setdefault(track, {}).setdefault("originals_by_project", {})
                by_id = {r.get("id"): r for r in dst.get(pid, [])}
                by_id.update({r.get("id"): r for r in rows})   # today's capture wins
                dst[pid] = list(by_id.values())

        self._data["token_milestones_notified"]   = []
        self._data["premium_milestones_notified"] = []
        self._data["bot_mode"]                    = "passive"
        self._data["last_milestone_ts"]           = None
        self._data["last_illegal_seen_ts"]        = None
        self._data["urgent_poll_step"]            = 0
        self._data["milestones_seeded"]           = False
        self._data["sealed_tracks"]               = {}
        self._data["mass_sealed_tracks"]          = []
        self._data["track_exemptions"]            = {}
        self._data["pending_track_unseal"]        = p_tracks
        # spend monitoring — fresh slate per UTC day
        self._data["spend_milestones_notified"]   = []
        self._data["project_spend_notified"]      = {}
        self._data["unlisted_models_alerted"]     = {}
        self._data["spend_seeded"]                = False
        self._data.pop("costs_cache", None)

    def update(self, snapshot: dict) -> bool:
        """Merge new snapshot, preserving all alert-control fields.
        Auto-resets daily state if the snapshot's date is newer than the persisted date.
        Day rollover handled here closes the race where the Telegram thread runs /refresh
        on a new day before the poll loop notices.

        Returns False (and changes nothing) for a snapshot OLDER than the stored
        day — one thread's pre-midnight fetch landing after another thread rolled
        over. Treating that as a "rollover" would reset today's state backward and
        queue every live seal for restore. Callers must skip acting on it."""
        with self._lock:
            new_date = snapshot.get("date")
            old_date = self._data.get("date")
            if new_date and old_date and new_date < old_date:
                print(f"[store] Stale snapshot for {new_date} (store is on {old_date}) — ignored")
                return False
            if new_date and old_date and new_date != old_date and _is_busy(self.org):
                # A seal/unseal/quarantine in flight would write its captures and
                # exemptions into the NEW day's state (never restored until the
                # next midnight). Let it finish first — bounded, so a stuck claim
                # can't block the rollover forever.
                if self._rollover_wait_since is None:
                    self._rollover_wait_since = time.time()
                if time.time() - self._rollover_wait_since < ROLLOVER_DEFER_MAX_SECS:
                    print("[store] Day rollover deferred — a seal/unseal is in progress")
                    return False
                print("[store] Day rollover forced — busy claim held too long")
            self._rollover_wait_since = None
            if new_date and old_date and new_date != old_date:
                print(f"[store] Day rollover {old_date} → {new_date} — daily state reset")
                _log_event("day_rollover", org=self.org.id, from_date=old_date, to_date=new_date,
                           final_normal=self._data.get("total_normal_tokens", 0),
                           final_premium=self._data.get("total_premium_tokens", 0),
                           final_cost=self._data.get("total_cost", 0.0))
                self._reset_daily_state_locked()
            preserved = {k: self._data[k] for k in self._PRESERVED if k in self._data}
            # Own copy: the caller keeps mutating its snapshot, and a mutation
            # racing _save() on another thread raised "dictionary changed size
            # during iteration" mid-write.
            self._data = copy.deepcopy(snapshot)
            self._data.update(preserved)
            self._save()
            return True

    def seed_state(self, normal_thresholds: list, premium_thresholds: list) -> bool:
        """Atomic check-and-mark. Returns True if THIS caller is the one that seeded
        the day (and therefore should broadcast), False if seeding was already done
        and this call is a no-op. Closes the race where the poll loop and a /refresh
        from the Telegram thread both pass `has_seeded()==False` and then both
        broadcast the same milestone."""
        with self._lock:
            if self._data.get("milestones_seeded", False):
                return False
            if normal_thresholds:
                ms = self._data.setdefault("token_milestones_notified", [])
                for t in normal_thresholds:
                    if t not in ms:
                        ms.append(t)
            if premium_thresholds:
                ms = self._data.setdefault("premium_milestones_notified", [])
                for t in premium_thresholds:
                    if t not in ms:
                        ms.append(t)
            self._data["milestones_seeded"] = True
            self._save()
            return True

    def get(self) -> dict:
        with self._lock:
            return dict(self._data)

    # Token milestones
    def get_milestones_notified(self) -> set:
        with self._lock:
            return set(self._data.get("token_milestones_notified", []))

    def add_milestone_notified(self, threshold: int):
        with self._lock:
            ms = self._data.setdefault("token_milestones_notified", [])
            if threshold not in ms:
                ms.append(threshold)
            self._save()

    # Premium model milestones (1M band)
    def get_premium_milestones_notified(self) -> set:
        with self._lock:
            return set(self._data.get("premium_milestones_notified", []))

    def add_premium_milestone_notified(self, threshold: int):
        with self._lock:
            ms = self._data.setdefault("premium_milestones_notified", [])
            if threshold not in ms:
                ms.append(threshold)
            self._save()

    # Concurrency alert cooldown
    def get_last_concurrent_alert_ts(self) -> Optional[float]:
        with self._lock:
            return self._data.get("last_concurrent_alert_ts")

    def set_last_concurrent_alert_ts(self, ts: float):
        with self._lock:
            self._data["last_concurrent_alert_ts"] = ts
            self._save()

    # Recent activity (for @bot active command)
    def set_active_projects(self, projects: dict, window_mins: int):
        with self._lock:
            self._data["active_projects"]   = projects
            self._data["active_window_mins"] = window_mins
            self._save()

    def get_active_projects(self) -> dict:
        with self._lock:
            return dict(self._data.get("active_projects", {}))

    def get_active_window_mins(self) -> int:
        with self._lock:
            return self._data.get("active_window_mins", CONCURRENCY_WINDOW_MINS)

    # Costs cache (per-project costs from last successful fetch)
    def update_costs(self, per_project: dict, total: float, org: float,
                     per_lane: dict = None, date: str = None) -> None:
        """Cache a costs fetch for `date` (the snapshot's day). A fetch that
        belongs to another day — /refresh straddling midnight while the poll
        loop rolls over — is dropped: cached as today's, it later posed as
        today's spend and fired false cap alarms."""
        with self._lock:
            if date is not None and date != self._data.get("date"):
                return
            self._data["costs_cache"] = {
                "per_project": dict(per_project),
                "per_lane":    dict(per_lane or {}),
                "total":       total,
                "org":         org,
                "ts":          time.time(),
                "date":        date or self._data.get("date"),
            }
            self._save()

    def get_costs_cache(self) -> Optional[dict]:
        with self._lock:
            return self._data.get("costs_cache")

    # ── Polling mode management ────────────────────────────────────────────────
    # Modes: "passive" | "urgent" | "aggressive"

    def get_mode(self) -> str:
        with self._lock:
            return self._data.get("bot_mode", "passive")

    def set_mode(self, mode: str) -> None:
        """Switch mode. Entering urgent/aggressive resets the poll step to floor."""
        with self._lock:
            prev = self._data.get("bot_mode", "passive")
            self._data["bot_mode"] = mode
            if mode in ("urgent", "aggressive"):
                self._data["urgent_poll_step"] = 0
            self._save()
        if prev != mode:
            _log_event("mode", org=self.org.id, from_mode=prev, to_mode=mode)

    def reset_urgent_step(self) -> None:
        """Restart urgent interval back to floor without changing mode."""
        with self._lock:
            self._data["urgent_poll_step"] = 0
            self._save()

    def get_urgent_interval(self) -> int:
        """Current sleep duration (seconds) for urgent/aggressive mode."""
        with self._lock:
            step = self._data.get("urgent_poll_step", 0)
            return min(URGENT_INTERVAL_MIN + step * URGENT_INTERVAL_STEP, URGENT_INTERVAL_MAX)

    def increment_urgent_step(self) -> None:
        with self._lock:
            max_step = (URGENT_INTERVAL_MAX - URGENT_INTERVAL_MIN) // URGENT_INTERVAL_STEP
            step = self._data.get("urgent_poll_step", 0)
            self._data["urgent_poll_step"] = min(step + 1, max_step)
            self._save()

    def get_last_milestone_ts(self) -> Optional[float]:
        with self._lock:
            return self._data.get("last_milestone_ts")

    def set_last_milestone_ts(self, ts: float) -> None:
        with self._lock:
            self._data["last_milestone_ts"] = ts
            self._save()

    def get_last_illegal_seen_ts(self) -> Optional[float]:
        with self._lock:
            return self._data.get("last_illegal_seen_ts")

    def update_last_illegal_seen(self) -> None:
        with self._lock:
            self._data["last_illegal_seen_ts"] = time.time()
            self._save()

    def has_seeded(self) -> bool:
        """True once seed_milestones() has run for today. Both the poll loop and
        cmd_refresh consult this so whichever fires first does the seed; the other
        becomes a no-op."""
        with self._lock:
            return self._data.get("milestones_seeded", False)

    # ── Track-level seals (unified: mass + manual share this store) ────────
    @staticmethod
    def _copy_tracks(tracks: dict) -> dict:
        """Copy two levels deep: callers iterate originals_by_project without the
        lock while seal workers add/pop projects in it — a shared dict raised
        "dictionary changed size during iteration" there. Row lists are always
        replaced, never mutated, so they can be shared."""
        return {t: {**info, "originals_by_project": dict(info.get("originals_by_project") or {})}
                for t, info in tracks.items()}

    def get_sealed_tracks(self) -> dict:
        with self._lock:
            return self._copy_tracks(self._data.get("sealed_tracks", {}))

    def is_project_track_sealed(self, pid: str, track: str) -> bool:
        with self._lock:
            return pid in (self._data.get("sealed_tracks", {})
                           .get(track, {}).get("originals_by_project", {}))

    def add_track_originals(self, track: str, pid: str, originals: list) -> None:
        """Record a project's pre-throttle originals under a track. Creates the
        track entry on first use."""
        with self._lock:
            tracks = self._data.setdefault("sealed_tracks", {})
            entry  = tracks.setdefault(track, {
                "sealed_at": time.time(),
                "originals_by_project": {},
            })
            entry.setdefault("originals_by_project", {})[pid] = originals
            self._save()

    def merge_track_originals(self, track: str, pid: str, originals: list) -> None:
        """Add originals for rows not already captured, keyed by rate-limit id.
        Used by drift re-seal: only SOME rows may have drifted healthy, and a
        plain overwrite would discard the captures for the rows still at 0 —
        that is the 0/0 cascade in a new disguise."""
        with self._lock:
            tracks = self._data.setdefault("sealed_tracks", {})
            entry  = tracks.setdefault(track, {"sealed_at": time.time(),
                                               "originals_by_project": {}})
            existing = entry.setdefault("originals_by_project", {}).get(pid, [])
            by_id = {o["id"]: o for o in existing}
            for o in originals:
                by_id[o["id"]] = o          # freshly-observed healthy value wins
            entry["originals_by_project"][pid] = list(by_id.values())
            self._save()

    def drop_track_originals(self, track: str, pid: str, ids: list) -> None:
        """Forget specific captured rows — the write-ahead captures of a seal
        whose rollback fully succeeded. Clears empty project / track entries."""
        if not ids:
            return
        drop = set(ids)
        with self._lock:
            tracks = self._data.get("sealed_tracks", {})
            obp    = tracks.get(track, {}).get("originals_by_project")
            if obp is None or pid not in obp:
                return
            keep = [o for o in obp[pid] if o.get("id") not in drop]
            if keep:
                obp[pid] = keep
            else:
                obp.pop(pid)
            if not obp:
                tracks.pop(track, None)
            self._save()

    def pop_track_originals(self, track: str, pid: str) -> Optional[list]:
        """Remove and return one project's saved originals for a track. Clears the
        track entry if no projects remain under it."""
        with self._lock:
            tracks = self._data.setdefault("sealed_tracks", {})
            if track not in tracks:
                return None
            originals = tracks[track].get("originals_by_project", {}).pop(pid, None)
            if not tracks[track].get("originals_by_project"):
                tracks.pop(track, None)
            self._save()
            return originals

    # ── Mass-sweep idempotency flag (per track, per day) ───────────────────
    def is_mass_sealed(self, track: str) -> bool:
        with self._lock:
            return track in self._data.get("mass_sealed_tracks", [])

    def mark_mass_sealed(self, track: str) -> None:
        with self._lock:
            lst = self._data.setdefault("mass_sealed_tracks", [])
            if track not in lst:
                lst.append(track)
            self._save()

    # ── Per-track manual exemption ─────────────────────────────────────────
    def get_track_exemptions(self) -> dict:
        with self._lock:
            return {pid: list(tracks)
                    for pid, tracks in self._data.get("track_exemptions", {}).items()}

    def is_exempt(self, pid: str, track: str) -> bool:
        with self._lock:
            return track in self._data.get("track_exemptions", {}).get(pid, [])

    def add_track_exemption(self, pid: str, track: str) -> None:
        with self._lock:
            exemptions = self._data.setdefault("track_exemptions", {})
            tracks     = exemptions.setdefault(pid, [])
            if track not in tracks:
                tracks.append(track)
            self._save()

    def remove_track_exemption(self, pid: str, track: str) -> None:
        with self._lock:
            exemptions = self._data.setdefault("track_exemptions", {})
            if pid in exemptions:
                exemptions[pid] = [t for t in exemptions[pid] if t != track]
                if not exemptions[pid]:
                    exemptions.pop(pid, None)
            self._save()

    # ── Spend monitoring (org-wide + per-project + unlisted-model dedup) ───
    def get_spend_milestones_notified(self) -> set:
        with self._lock:
            return set(self._data.get("spend_milestones_notified", []))

    def add_spend_milestone_notified(self, threshold: float) -> None:
        with self._lock:
            ms = self._data.setdefault("spend_milestones_notified", [])
            if threshold not in ms:
                ms.append(threshold)
            self._save()

    def get_project_spend_notified(self, pid: str) -> set:
        with self._lock:
            return set(self._data.get("project_spend_notified", {}).get(pid, []))

    def add_project_spend_notified(self, pid: str, threshold: float) -> None:
        with self._lock:
            psn = self._data.setdefault("project_spend_notified", {})
            lst = psn.setdefault(pid, [])
            if threshold not in lst:
                lst.append(threshold)
            self._save()

    def is_unlisted_alerted(self, pid: str, model: str) -> bool:
        with self._lock:
            return model in self._data.get("unlisted_models_alerted", {}).get(pid, [])

    def mark_unlisted_alerted(self, pid: str, model: str) -> None:
        with self._lock:
            uma = self._data.setdefault("unlisted_models_alerted", {})
            lst = uma.setdefault(pid, [])
            if model not in lst:
                lst.append(model)
            self._save()

    def claim_spend_seed(self) -> bool:
        """Atomic: returns True only for the unique caller that flips spend_seeded.
        Same pattern as `seed_state()` — closes the poll/refresh race on
        first-of-day spend seeding."""
        with self._lock:
            if self._data.get("spend_seeded", False):
                return False
            self._data["spend_seeded"] = True
            self._save()
            return True

    def has_spend_seeded(self) -> bool:
        with self._lock:
            return self._data.get("spend_seeded", False)

    # ── Pending track unseal queue (day-rollover restore) ──────────────────
    def get_pending_track_unseal(self) -> dict:
        with self._lock:
            return self._copy_tracks(self._data.get("pending_track_unseal", {}))

    def set_pending_track_rows(self, track: str, pid: str, rows: list) -> None:
        """Replace one project's queued rows (the ones still owed a restore)."""
        with self._lock:
            pending = self._data.setdefault("pending_track_unseal", {})
            pending.setdefault(track, {}).setdefault("originals_by_project", {})[pid] = rows
            self._save()

    def pop_pending_track_project(self, track: str, pid: str) -> None:
        """Remove one project from a pending-track-unseal entry. Clears the
        track-level entry once empty."""
        with self._lock:
            pending = self._data.setdefault("pending_track_unseal", {})
            if track not in pending:
                return
            pending[track].get("originals_by_project", {}).pop(pid, None)
            if not pending[track].get("originals_by_project"):
                pending.pop(track, None)
            self._save()


# ── Subscriber store ───────────────────────────────────────────────────────

class SubscriberStore:
    def __init__(self, path: Path, primary: str):
        self.path    = path
        self.primary = str(primary)
        self._lock   = threading.Lock()
        self._ids: set[str] = {self.primary}
        self._load()

    def _load(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    self._ids = set(json.load(f))
            except Exception as e:
                print(f"[subs] CORRUPT subscriber file at {self.path} ({e}) — resetting to primary only")
        self._ids.add(self.primary)

    def _save(self):
        _atomic_write_json(self.path, sorted(self._ids))

    def add(self, chat_id: str) -> bool:
        with self._lock:
            if chat_id in self._ids:
                return False
            self._ids.add(chat_id)
            self._save()
            return True

    def remove(self, chat_id: str) -> bool:
        with self._lock:
            if chat_id == self.primary or chat_id not in self._ids:
                return False
            self._ids.discard(chat_id)
            self._save()
            return True

    def all(self) -> list[str]:
        with self._lock:
            return list(self._ids)

    def migrate(self, old_id: str, new_id: str) -> None:
        """Replace a chat id after a group→supergroup upgrade. If the migrated
        chat was the primary, the in-memory primary follows — but .env still
        holds the old id, so shout about it."""
        with self._lock:
            if old_id not in self._ids:
                return
            self._ids.discard(old_id)
            self._ids.add(new_id)
            if self.primary == old_id:
                self.primary = new_id
                print(f"[subs] PRIMARY chat migrated {old_id} → {new_id} — "
                      f"update TELEGRAM_CHAT_ID in .env before the next restart!")
            self._save()


class NameStore:
    """Persists per-chat display names. Default name for the primary chat is 'Bach'."""

    def __init__(self, path: Path, primary_id: str):
        self.path     = path
        self._lock    = threading.Lock()
        self._names: dict[str, str] = {str(primary_id): "Bach"}
        self._load()

    def _load(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    self._names.update(json.load(f))
            except Exception as e:
                print(f"[names] CORRUPT names file at {self.path} ({e}) — resetting to defaults")

    def _save(self):
        _atomic_write_json(self.path, self._names)

    def set(self, chat_id: str, name: str) -> None:
        """Store a sanitised display name.
        Escapes HTML and caps to 48 chars — names flow into many Telegram-HTML
        messages (`<b>{name}</b>`), so unescaped angle brackets can break parsing
        or be abused. Strips control chars + collapses whitespace."""
        cleaned = " ".join(name.split())[:48]
        safe    = html.escape(cleaned, quote=True)
        with self._lock:
            self._names[str(chat_id)] = safe
            self._save()

    def get(self, chat_id: str) -> str:
        with self._lock:
            return self._names.get(str(chat_id), "Bach")

    def migrate(self, old_id: str, new_id: str) -> None:
        """Carry a chat's display name over to its post-upgrade supergroup id."""
        with self._lock:
            if old_id in self._names:
                self._names[str(new_id)] = self._names.pop(old_id)
                self._save()


# ── Telegram I/O ───────────────────────────────────────────────────────────

GIF_DIR = Path(__file__).parent / "gifs"


def _send_animation(path: Path, chat_id: str = None, thread_id: int = None) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendAnimation"
    target_chat = chat_id or CHAT_ID
    data = {"chat_id": target_chat}
    if thread_id:
        data["message_thread_id"] = str(thread_id)
    try:
        # Read the bytes once, outside the retry loop: a file handle re-passed
        # into a retried attempt would already be at EOF and upload 0 bytes.
        content = path.read_bytes()
        r = _telegram_call("post", url, data=data, files={"animation": content}, timeout=30)
        if not r.ok:
            print(f"[telegram anim {r.status_code}] chat={target_chat} | {r.text[:400]}")
    except Exception as e:
        print(f"[telegram anim error] {e}")


# Set in main() to a callback(old_id, new_id) that rewrites the subscriber and
# name stores when Telegram reports a group→supergroup migration. Module-level
# because _send has no store references.
_MIGRATION_CB = None


# Telegram rejects a message over 4096 characters outright (400 — the whole
# reply is lost). Two-org reports can get there, so _send splits on line
# boundaries; tags in this bot's messages are line-local, so each part stays
# valid HTML. The margin covers entity/emoji counting differences.
TELEGRAM_MAX_CHARS = 4000


def _split_message(text: str, limit: int = TELEGRAM_MAX_CHARS) -> list[str]:
    """Pack whole blank-line-separated blocks (a project, an org section) into
    each part; only a block too big on its own is split between lines. Plain
    line packing started parts mid-project, with no name or org in sight."""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        candidate = f"{cur}\n\n{block}" if cur else block
        if len(candidate) <= limit:
            cur = candidate
            continue
        if cur:
            chunks.append(cur)
        if len(block) <= limit:
            cur = block
        else:
            parts = _split_lines(block, limit)
            chunks.extend(parts[:-1])
            cur = parts[-1]
    if cur:
        chunks.append(cur)
    return chunks


def _split_lines(text: str, limit: int) -> list[str]:
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:            # pathological single line: hard cut
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


def _send(text: str, chat_id: str = None, thread_id: int = None,
          keyboard: list = None) -> None:
    """Send `text`, split into several messages if it exceeds Telegram's limit;
    the keyboard rides on the last part."""
    parts = _split_message(text)
    for i, part in enumerate(parts):
        _send_one(part, chat_id, thread_id, keyboard if i == len(parts) - 1 else None)


def _send_one(text: str, chat_id: str = None, thread_id: int = None,
              keyboard: list = None) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    target_chat = str(chat_id or CHAT_ID)
    payload = {"chat_id": target_chat, "text": text, "parse_mode": "HTML"}
    if thread_id:
        payload["message_thread_id"] = thread_id
    if keyboard is not None:
        payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    try:
        r = _telegram_call(
            "post",
            url,
            data=payload,
            timeout=REQUEST_TIMEOUT,
        )
        if r.ok:
            return
        # Group upgraded to supergroup: the old chat id is permanently dead and
        # Telegram hands us the replacement. Update the stores and re-send once,
        # so alerts don't silently 400 forever (observed live on 2026-08-13).
        mig = None
        try:
            mig = r.json().get("parameters", {}).get("migrate_to_chat_id")
        except Exception:
            pass
        if mig:
            new_id = str(mig)
            print(f"[telegram] chat {target_chat} migrated → {new_id} — updating stores")
            _log_event("chat_migrated", old=target_chat, new=new_id)
            if _MIGRATION_CB:
                try:
                    _MIGRATION_CB(target_chat, new_id)
                except Exception as e:
                    print(f"[telegram migration-cb error] {e}")
            payload["chat_id"] = new_id
            r2 = _telegram_call("post", url, data=payload, timeout=REQUEST_TIMEOUT)
            if not r2.ok:
                print(f"[telegram send retry {r2.status_code}] chat={new_id} | {r2.text[:300]}")
            return
        print(f"[telegram send {r.status_code}] chat={target_chat} | {r.text[:400]}")
    except Exception as e:
        print(f"[telegram send network error] {e}")


def _edit_message(text: str, chat_id: str, message_id: int,
                  keyboard: list = None) -> None:
    """Edit an existing message's text + inline keyboard (used by button flows).
    Pass keyboard=[] to strip buttons, None to leave them unchanged."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id,
               "text": text, "parse_mode": "HTML"}
    if keyboard is not None:
        payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    try:
        r = _telegram_call("post", url, data=payload, timeout=REQUEST_TIMEOUT)
        if not r.ok and "message is not modified" not in r.text:
            print(f"[telegram edit {r.status_code}] chat={chat_id} | {r.text[:300]}")
    except Exception as e:
        print(f"[telegram edit network error] {e}")


def _answer_callback(callback_id: str, text: str = None) -> None:
    """Acknowledge a callback query so Telegram stops the loading spinner.
    Optional `text` shows as a small toast to the user who clicked."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text[:200]
    try:
        _telegram_call("post", url, data=payload, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        print(f"[telegram answerCallback error] {e}")


def _org_header(org: Optional[Org]) -> str:
    """'🏢 Business AI Lab 3' line that opens every org-specific message — both
    orgs send the same alert types (and both have a "Default project"). Omitted
    when only one org is configured."""
    return f"🏢 <b>{org.label}</b>\n" if org is not None and len(ORGS) > 1 else ""


def _broadcast(fmt_fn, subs: SubscriberStore, names: NameStore = None, *,
               org: Optional[Org]) -> None:
    """Send a personalised message to every subscriber.
    `fmt_fn` is a one-arg function: it receives the chat's registered display name
    (or "Bach" when no NameStore is wired) and returns the rendered HTML to send.
    `org` is REQUIRED (None only for org-less messages): it prefixes the org
    header and tags the log entry, so an alert can never go out unattributed.

    Every broadcast is also mirrored to the local intel log (one entry per
    broadcast, canonical "Bach" rendering) — Telegram is no longer the only
    place push alerts exist."""
    head = _org_header(org)
    try:
        _log_event("broadcast", **({"org": org.id} if org else {}), text=head + fmt_fn("Bach"))
    except Exception as e:
        print(f"[intel-log render error] {e}")
    for cid in subs.all():
        name = names.get(cid) if names is not None else "Bach"
        _send(head + fmt_fn(name), cid)


def _get_updates(offset: int) -> list[dict]:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        r = _telegram_call(
            "get",
            url,
            params={
                "offset":          offset,
                "timeout":         POLL_TIMEOUT,
                "allowed_updates": json.dumps(["message", "channel_post", "callback_query"]),
            },
            timeout=POLL_TIMEOUT + 5,
        )
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        print(f"[poll error] {e}")
        time.sleep(5)   # avoid a tight reconnect loop on persistent failure
        return []


def _discard_pending_updates() -> Optional[int]:
    """Discard any Telegram updates that piled up while the bot was offline.
    Returns the next safe offset to use (0 = queue was empty), or None if the
    call failed — the caller retries. Without this, on restart the bot would
    replay up to 24 h of queued updates — including stale `arch:seal:both:all`
    button clicks that could fire a real mass-seal from a UI a user has long
    forgotten about. We grab the latest update_id and ACK it; Telegram drops
    everything ≤ that id from the queue."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        r = _telegram_call("get", url, params={"offset": -1, "timeout": 0,
                                                "allowed_updates": json.dumps([])},
                            timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        updates = r.json().get("result", [])
        if not updates:
            return 0
        next_offset = updates[-1]["update_id"] + 1
        # ACK so Telegram drops these from the queue.
        _telegram_call("get", url, params={"offset": next_offset, "timeout": 0,
                                            "allowed_updates": json.dumps([])},
                        timeout=REQUEST_TIMEOUT)
        print(f"[telegram] Discarded {len(updates)} stale update(s); next offset={next_offset}")
        return next_offset
    except Exception as e:
        print(f"[telegram offset discard error] {e} — retrying")
        return None


def _fetch_bot_username(retries: int = 5, delay: int = 10) -> Optional[str]:
    """Resolve the bot's @username via getMe. Retries with backoff so a transient
    DNS/network blip at startup doesn't leave bot_username=None for the whole run
    (which would silently disable all commands)."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.json().get("result", {}).get("username")
        except Exception as e:
            print(f"[getMe error attempt {attempt}/{retries}] {e}")
            if attempt < retries:
                time.sleep(delay)
    return None


# ── Formatters — helpers ────────────────────────────────────────────────────

def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fmt_cap(n: int) -> str:
    """Compact allowance label: 10M, 1M, 2.5M, 250K."""
    return f"{n / 1_000_000:g}M" if n >= 1_000_000 else f"{n / 1_000:g}K"


def _fmt_cost(usd: Optional[float]) -> str:
    """Compact lane cost in Bach's requested "x.y$" style. Never renders real
    spend as zero: sub-cent amounts keep 4 decimals, because surfacing small
    off-watchlist spend is the Exotic lane's entire purpose. None -> "—" (no
    cost data yet, e.g. before the first successful costs fetch)."""
    if usd is None:
        return "—"
    if usd == 0:
        return "0.00$"
    if usd < 0.01:
        return f"{usd:.4f}$"
    return f"{usd:.2f}$"


def _fmt_lane_lines(premium_tok: int, normal_tok: int, lane_costs: Optional[dict],
                    org: Org, indent: str = "   ") -> list:
    """The three lane lines shared by every report. Free-tier lanes show tokens
    against their allowance plus billed cost (which stays 0.00$ until the
    allowance is exhausted). Exotic has no allowance to count against, so it
    shows cost only."""
    lc = lane_costs or {}
    pc, nc = _fmt_cap(org.premium_cap), _fmt_cap(org.normal_cap)
    return [
        f"{indent}⭐ Premium ({pc}): <b>{_fmt_tokens(premium_tok)}</b> / {pc}. Cost: {_fmt_cost(lc.get('premium'))}",
        f"{indent}📦 Normal ({nc}): <b>{_fmt_tokens(normal_tok)}</b> / {nc}. Cost: {_fmt_cost(lc.get('normal'))}",
        f"{indent}🧪 Exotic: <b>{_fmt_cost(lc.get('exotic'))}</b>",
    ]


def _fmt_ts(ts: Optional[float]) -> str:
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _fmt_month(year: int, month: int) -> str:
    return datetime(year, month, 1).strftime("%B %Y")


# ── Formatters — auto-messages ─────────────────────────────────────────────

def _paid_note(track: str, org: Optional[Org]) -> str:
    """Extra line for an allowance-exhausted alert in an org that pays past its
    free allowance on purpose — billing starting there is the plan, not a leak."""
    if org is None or not org.pays_past_free(track):
        return ""
    return (f"\nBy design this org keeps running on paid usage until the "
            f"{_fmt_tokens(org.threshold(track))} seal point.\n")


def fmt_token_milestone(threshold: int, current: int, level: str, name: str = "Bach",
                        org: Optional[Org] = None) -> str:
    t = _fmt_tokens(threshold)
    c = _fmt_tokens(current)
    if level == "casual":
        return (
            f"📊 <b>Token Threshold Reached — {t}</b>\n\n"
            f"Daily consumption stands at <b>{c} tokens</b>.\n"
            f"Operations remain within acceptable parameters.\n"
            f"<i>Monitoring continues, Monarch {name}.</i>"
        )
    if level == "urgent":
        return (
            f"⚠️ <b>High Token Consumption — {t}</b>\n\n"
            f"Daily usage has reached <b>{c} tokens</b>.\n"
            f"Expenditure is approaching critical thresholds.\n"
            f"Your attention is advised, My Liege {name}."
        )
    # cap
    return (
        f"🚨 <b>Normal Models Allowance Exhausted — {c}</b>\n\n"
        f"The {t}-token daily allowance for normal models has been crossed.\n"
        f"Mini models (gpt-4o-mini, o1-mini, o3-mini, etc.) are now billing at standard rates.\n"
        f"{_paid_note('normal', org)}\n"
        f"Monarch {name}, the operation requires your oversight."
    )


def fmt_premium_token_milestone(threshold: int, current: int, level: str, org: Org,
                                name: str = "Bach") -> str:
    t = _fmt_tokens(threshold)
    c = _fmt_tokens(current)
    cap = _fmt_cap(org.premium_cap)
    if level == "casual":
        return (
            f"📊 <b>Premium Token Threshold — {t}</b>\n\n"
            f"Full-size model consumption stands at <b>{c} tokens</b>.\n"
            f"Premium models daily allowance: {cap}/day (gpt-4o, gpt-4.1, o1, o3, etc.)\n"
            f"<i>Monitoring continues, Monarch {name}.</i>"
        )
    if level == "urgent":
        return (
            f"⚠️ <b>Premium Model — High Usage — {t}</b>\n\n"
            f"Full-size model usage has reached <b>{c} tokens</b>.\n"
            f"Approaching the {cap} daily free allowance for premium models.\n"
            f"Your attention is advised, My Liege {name}."
        )
    # cap
    return (
        f"🚨 <b>Premium Free Allowance Exhausted — {c}</b>\n\n"
        f"The {t}-token daily allowance for premium models has been crossed.\n"
        f"Premium models (gpt-4o, gpt-4.1, o1, o3, etc.) are now billing at standard rates.\n"
        f"{_paid_note('premium', org)}\n"
        f"Monarch {name}, the operation requires your oversight."
    )


def fmt_concurrency_alert(active: dict, org: Org, name: str = "Bach") -> str:
    lines = [
        f"⚡ <b>Concurrent Project Activity — {len(active)} Projects</b>\n",
        f"{len(active)} projects recorded activity in the last "
        f"{CONCURRENCY_WINDOW_MINS} minutes:\n",
    ]
    for pid, count in sorted(active.items(), key=lambda x: x[1], reverse=True):
        proj_name = org.projects.get(pid, pid)
        lines.append(f"• <b>{proj_name}</b> — {count:,} requests")
    lines.append(f"\n<i>Monarch {name}, multiple operations are in simultaneous execution.</i>")
    return "\n".join(lines)


def fmt_overcap_active_alert(banded_active: dict, normal_exceeded: bool, premium_exceeded: bool,
                             org: Org, name: str = "Bach") -> str:
    """Render the red-tone overcap alert. `banded_active` maps pid → {"normal": int, "premium": int}
    and contains only projects with usage on at least one exceeded band."""
    def _limit_label(track: str) -> str:
        if org.pays_past_free(track):
            return f"{track.title()} daily ceiling ({_fmt_cap(org.ceiling(track))})"
        return f"{_band_label(track, org)} free-tier allowance"

    bands = []
    if normal_exceeded:
        bands.append(_limit_label("normal"))
    if premium_exceeded:
        bands.append(_limit_label("premium"))
    band_str = " & ".join(bands)

    def _illegal_reqs(b: dict) -> int:
        r = 0
        if normal_exceeded:  r += b.get("normal", 0)
        if premium_exceeded: r += b.get("premium", 0)
        return r

    lines = [
        "🔴 <b>‼️ BUDGET BREACHED — ILLEGAL ACTIVITY DETECTED ‼️</b>\n",
        f"The <b>{band_str}</b> is <b>exhausted</b>.",
        "These projects are <b>still burning the exhausted band</b> — every request now bills:\n",
    ]
    for pid, b in sorted(banded_active.items(), key=lambda x: _illegal_reqs(x[1]), reverse=True):
        proj_name = org.projects.get(pid, pid)
        parts = []
        if normal_exceeded and b.get("normal", 0) > 0:
            parts.append(f"{b['normal']:,} normal")
        if premium_exceeded and b.get("premium", 0) > 0:
            parts.append(f"{b['premium']:,} premium")
        detail = " + ".join(parts)
        lines.append(f"🚨 <b>{proj_name}</b>  —  {detail} reqs in the last {OVERCAP_WINDOW_MINS} min")
    lines.append("\n<b>HALT ALL NON-ESSENTIAL OPERATIONS IMMEDIATELY.</b>")
    lines.append(f"<i>(Activity window: last {OVERCAP_WINDOW_MINS} min — accounts for API ingestion lag)</i>")
    lines.append(f"<i>Monarch {name} — the treasury is bleeding. Your command is required at once.</i>")
    return "\n".join(lines)


# ── Overcap handler ────────────────────────────────────────────────────────

def _handle_overcap(usage: UsageStore, subs: SubscriberStore, names: NameStore,
                    normal_exceeded: bool, premium_exceeded: bool) -> None:
    """Alarm-only handler for cap breaches, run every poll while a track is over
    its cap. The seal that prevents the breach lives in _handle_track_seal; this
    shouts at projects still burning the exhausted band — exempt (manually
    unsealed) projects, failed seals, or leaks.

    Projects sealed on that band within the last OVERCAP_WINDOW_MINS are skipped:
    the window's requests may all predate the seal. Without this, every mass
    seal was followed ~1 min later by a false "ILLEGAL ACTIVITY — HALT ALL
    OPERATIONS" naming projects that had just been sealed (09-26, 09-28, 09-29).
    After the window, activity from a sealed project is a real leak and alerts.

    A project using premium models does not trigger the normal-cap alarm and vice
    versa. Reverts aggressive → passive after AGGRESSIVE_REVERT_SECS quiet, and
    urgent → passive after URGENT_REVERT_SECS without a milestone (this branch
    used to shadow the poll loop's urgent revert, so urgent lasted till midnight)."""
    banded = _fetch_recent_activity_by_band(usage.org, minutes=OVERCAP_WINDOW_MINS)
    if banded is None:
        # API failure — keep current mode, don't broadcast or revert based on bad data.
        print("[overcap] activity fetch failed — holding mode")
        return
    illegal = _filter_to_exceeded_band(banded, normal_exceeded, premium_exceeded,
                                       grace=_recently_sealed(usage))
    mode    = usage.get_mode()

    if illegal:
        usage.update_last_illegal_seen()
        if mode != "aggressive":
            usage.set_mode("aggressive")
            print(f"[mode] → AGGRESSIVE ({len(illegal)} project(s) burning the exhausted band)")
        _broadcast(lambda n, r=illegal, ne=normal_exceeded, pe=premium_exceeded, o=usage.org:
            fmt_overcap_active_alert(r, ne, pe, o, n), subs, names, org=usage.org)
    elif mode == "aggressive":
        last_ts = usage.get_last_illegal_seen_ts()
        if last_ts and time.time() - last_ts > AGGRESSIVE_REVERT_SECS:
            usage.set_mode("passive")
            print("[mode] → PASSIVE (1 h since last illegal-band activity — standing down)")
    elif mode == "urgent":
        last_ms = usage.get_last_milestone_ts()
        if last_ms and time.time() - last_ms > URGENT_REVERT_SECS:
            usage.set_mode("passive")
            print("[mode] → PASSIVE (1 h without new milestone)")


# (track, pid) -> when a seal on that project last landed. The track entry's
# `sealed_at` is set by the FIRST seal of the day on that track, so a morning
# manual seal would have denied the grace window to an afternoon mass seal.
# In-memory: after a restart the track timestamp is the fallback.
_SEALED_AT: dict[tuple, float] = {}


def _recently_sealed(usage: UsageStore) -> dict[str, set]:
    """band -> projects sealed on it within the overcap activity window. A
    quarantine (full seal) covers both bands."""
    now, window = time.time(), OVERCAP_WINDOW_MINS * 60
    out: dict[str, set] = {"normal": set(), "premium": set()}
    for track, entry in usage.get_sealed_tracks().items():
        bands = ("normal", "premium") if track == QUARANTINE_TRACK else (track,)
        for pid in entry.get("originals_by_project") or {}:
            ts = max(entry.get("sealed_at") or 0, _SEALED_AT.get((track, pid), 0))
            if now - ts < window:
                for band in bands:
                    if band in out:
                        out[band].add(pid)
    return out


# ── Rate-limit capture / payload helpers (shared by all seal/unseal paths) ──

def _capture_originals(rate_limits: list[dict]) -> list[dict]:
    """Take the API's rate_limit rows and shrink to the fields we need to restore.
    Only includes fields that were actually present in the original response — avoids
    sending `null` for irrelevant per-model fields on restore."""
    fields = (
        "max_requests_per_1_minute",
        "max_tokens_per_1_minute",
        "max_images_per_1_minute",
        "max_audio_megabytes_per_1_minute",
        "max_requests_per_1_day",
        "batch_1_day_max_input_tokens",
    )
    captured = []
    for rl in rate_limits:
        entry = {"id": rl["id"], "model": rl.get("model", "")}
        for f in fields:
            if f in rl and rl[f] is not None:
                entry[f] = rl[f]
        captured.append(entry)
    return captured


def _restore_payload(original: dict) -> dict:
    """Build a POST payload from a captured original — drops 'id' and 'model'."""
    return {k: v for k, v in original.items() if k not in ("id", "model")}


# Fields the API accepts on a rate-limit POST — must match what GET can return.
_RATE_LIMIT_FLOOR_FIELDS = (
    "max_requests_per_1_minute",
    "max_tokens_per_1_minute",
    "max_images_per_1_minute",
    "max_audio_megabytes_per_1_minute",
    "max_requests_per_1_day",
    "batch_1_day_max_input_tokens",
)


def _seal_payload(rate_limit: dict) -> dict:
    """Build the POST payload to throttle a single rate-limit row to 0.
    Only includes fields the row actually exposes — sora-2, for example, rejects
    max_tokens_per_1_minute as 'invalid_rate_limit_type' for that model."""
    payload = {}
    for f in _RATE_LIMIT_FLOOR_FIELDS:
        if rate_limit.get(f) is not None:
            payload[f] = 0
    return payload


def _compute_canonical_baseline(usage: "UsageStore") -> dict:
    """Per-model canonical rate-limit values for `usage.org`, healthy by construction.

    Strictly per org: Lab 3 is a new, lower-tier org whose ceilings sit below Lab
    2's values — a pooled baseline would restore Lab 3 rows above its ceilings.

    Pools values from two sources, preferring whichever has non-zero data:
      1. Captured originals stored in state (sealed_tracks + pending_track_unseal).
         These are snapshotted PRE-throttle, so they hold healthy values even
         while every project is currently sealed.
      2. Live rate-limit values from the API for listed-track models.

    For each (model, field) it takes the most-common NON-ZERO value. A field is
    only included in the baseline if at least one non-zero value was seen — so the
    baseline NEVER contains a zero. This is the critical invariant: restoring from
    this baseline can never re-throttle a project. If no healthy value exists for a
    (model, field) anywhere, the field is omitted and the caller falls back to the
    row's own captured original."""
    from collections import Counter
    pools: dict = {}

    def _ingest(model: str, src: dict):
        if not model or _track_for_model(model) is None:
            return
        for f in _RATE_LIMIT_FLOOR_FIELDS:
            v = src.get(f)
            if v is not None:
                pools.setdefault((model, f), []).append(v)

    # Source 1 — captured originals from state (pre-throttle, healthy)
    capture_groups = []
    for info in usage.get_sealed_tracks().values():
        capture_groups.extend(info.get("originals_by_project", {}).values())
    for info in usage.get_pending_track_unseal().values():
        capture_groups.extend(info.get("originals_by_project", {}).values())
    for originals in capture_groups:
        for o in originals:
            _ingest(o.get("model", ""), o)

    # Source 2 — live API values (this org's projects only)
    for pid in usage.org.projects:
        rls = _fetch_project_rate_limits(pid, org=usage.org)
        if not rls:
            continue
        for rl in rls:
            _ingest(rl.get("model", ""), rl)

    baseline: dict = {}
    for (model, f), vals in pools.items():
        nonzero = [v for v in vals if v > 0]
        if nonzero:   # omit fields with no healthy value — never emit a 0
            baseline.setdefault(model, {})[f] = Counter(nonzero).most_common(1)[0][0]
    return baseline


def _restore_rows(pid: str, originals: list[dict],
                  baseline: dict[str, dict] = None, *, org: Org) -> list[dict]:
    """POST rate-limit rows back to the API one at a time with inter-write spacing.
    Returns the rows whose POST failed ([] on full success).

    When `baseline` is provided, each row is restored to the canonical consensus
    value for its model (from _compute_canonical_baseline) rather than the value
    captured at seal time. This makes restores uniform across projects and immune
    to the 0/0 cascade — even if a captured original was stale (0/0), the baseline
    carries the healthy org-wide value. Rows whose model isn't in the baseline fall
    back to their captured values."""
    failed: list[dict] = []
    for orig in originals:
        model   = orig.get("model", "")
        payload = dict(baseline[model]) if (baseline and model in baseline) \
                  else _restore_payload(orig)
        if not payload:
            continue
        if _update_project_rate_limit(pid, orig["id"], payload, org=org):
            time.sleep(0.05)
        else:
            failed.append(orig)
    return failed


def _restore_rate_limits(pid: str, originals: list[dict],
                         baseline: dict[str, dict] = None, *, org: Org) -> int:
    """Count-returning wrapper around _restore_rows (0 on full success)."""
    return len(_restore_rows(pid, originals, baseline, org=org))


# ── Per-project, per-track throttle / restore primitives ───────────────────

def _seal_rows(pid: str, track: str, rows: list[dict], usage: UsageStore) -> Optional[list]:
    """Zero every row in `rows`, capturing healthy pre-seal values under `track`.
    Returns the captured originals, or None if a POST failed.

    WRITE-AHEAD: captures are recorded BEFORE the first POST. On a failed POST the
    rows already zeroed are rolled back, and the captures are dropped only if that
    rollback fully succeeded. The old order (capture after the last POST, rollback
    result ignored) stranded rows forever: a DNS outage failed row k AND its
    rollback, rows 0..k-1 stayed at 0/0 with no capture, and since only captured
    rows are restored at midnight, nothing ever reopened them. Kept captures mean
    the project reads as sealed — drift repair re-zeroes the healthy remainder and
    midnight restores every touched row.

    Only healthy rows (non-zero rpm/tpm) are captured: a row already at 0 is owned
    by an earlier capture, and recording 0 would re-create the 0/0 cascade."""
    originals = _capture_originals(
        [rl for rl in rows if rl.get("max_requests_per_1_minute") or rl.get("max_tokens_per_1_minute")]
    )
    prior = {o.get("id") for o in usage.get_sealed_tracks().get(track, {})
             .get("originals_by_project", {}).get(pid, [])}
    added = [o["id"] for o in originals if o["id"] not in prior]
    if originals:
        usage.merge_track_originals(track, pid, originals)
    throttled_ids: list[str] = []
    for rl in rows:
        payload = _seal_payload(rl)
        if not payload:
            continue   # row exposes no settable fields
        if _update_project_rate_limit(pid, rl["id"], payload, org=usage.org):
            throttled_ids.append(rl["id"])
            time.sleep(0.05)
            continue
        rollback = [o for o in originals if o["id"] in throttled_ids]
        if _restore_rate_limits(pid, rollback, org=usage.org) == 0:
            usage.drop_track_originals(track, pid, added)
        else:
            print(f"[seal] {usage.org.projects.get(pid, pid)}/{track}: rollback incomplete — "
                  f"captures kept so repair / midnight restore can finish the job")
        return None
    _SEALED_AT[(track, pid)] = time.time()
    return originals


def _throttle_track_for_project(pid: str, track: str, usage: UsageStore) -> str:
    """Throttle every rate-limit row of `pid` that belongs to `track` down to 0,
    saving the pre-throttle originals into sealed_tracks (write-ahead, see
    _seal_rows). Returns 'throttled' / 'noop' (no rows for this track) / 'failed'.
    Caller must hold the busy claim."""
    rate_limits = _fetch_project_rate_limits(pid, org=usage.org)
    if rate_limits is None:
        return "failed"
    track_rls = [rl for rl in rate_limits if _matches_track(rl.get("model", ""), track)]
    if not track_rls:
        return "noop"
    return "failed" if _seal_rows(pid, track, track_rls, usage) is None else "throttled"


def _restore_track_for_project(pid: str, track: str, usage: UsageStore,
                               baseline: dict) -> str:
    """Restore `pid`'s rows for `track` to the canonical baseline, then drop the
    project from sealed_tracks[track]. Returns 'restored' / 'noop' / 'failed'.
    Caller must hold the busy claim."""
    info      = usage.get_sealed_tracks().get(track, {})
    originals = info.get("originals_by_project", {}).get(pid, [])
    if not originals:
        return "noop"
    failed = _restore_rate_limits(pid, originals, baseline=baseline, org=usage.org)
    if failed:
        return "failed"
    usage.pop_track_originals(track, pid)
    return "restored"


# ── Mass throttle (auto threshold/wave sweep + manual "all") ────────────────

def _ordered_projects_for_track_seal(track: str, usage: UsageStore) -> list[str]:
    """`usage.org`'s projects ordered by today's usage on `track`, heaviest first, so the
    sweep throttles the biggest spenders first. Read from the stored snapshot —
    it used to fetch recent activity from the API first, which put a network
    call (up to ~48 s with retries on a bad link) in front of the first seal."""
    projects = usage.get().get("projects", {})
    key = f"{track}_tokens"
    return sorted(usage.org.projects, key=lambda p: projects.get(p, {}).get(key, 0), reverse=True)


def _mass_seal_track(track: str, usage: UsageStore, subs: SubscriberStore,
                     names: NameStore, *, consumed: int = None,
                     respect_exemptions: bool = True, manual: bool = False) -> None:
    """Throttle `track` to 0 across every project (skipping exemptions when
    respect_exemptions). Concise begin/done broadcast. Marks the track mass-sealed.
    REQUIRES: caller already holds the busy claim (`_try_claim_busy`)."""
    org = usage.org
    cap = org.ceiling(track)
    if consumed is None:
        consumed = cap   # manual trigger: report at/over the ceiling

    usage.mark_mass_sealed(track)
    _DRIFT_CHECKED.pop((org.id, today_str(), track), None)   # verify on the first poll after this sweep
    print(f"[mass-seal] {track} → starting (consumed={consumed:,}, cap={cap:,})")

    throttled, exempt, noop, failed = [], [], [], []
    todo: list[str] = []
    for pid in _ordered_projects_for_track_seal(track, usage):
        if respect_exemptions and usage.is_exempt(pid, track):
            exempt.append(pid); continue
        if usage.is_project_track_sealed(pid, track):
            noop.append(pid); continue
        todo.append(pid)

    # Projects are independent (each worker only touches its own project's
    # rate-limit rows; the store methods are lock-guarded), so throttle them in
    # parallel — the sequential sweep took ~5 min for 13 projects, during which
    # the wave kept crashing in. 4 workers cut that to roughly a minute. The
    # 50 ms inter-POST spacing is preserved *within* each project.
    results: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=SEAL_SWEEP_WORKERS) as ex:
        futs = {ex.submit(_in_org, org, _throttle_track_for_project, pid, track, usage): pid
                for pid in todo}
        # Announce once the workers are already POSTing — the Telegram send
        # (retries included) used to sit in front of the first seal.
        _broadcast(lambda n, t=track, c=consumed, cp=cap:
            fmt_seal_batch_begin(t, c, cp, org, n, manual=manual), subs, names, org=org)
        for fut in as_completed(futs):
            pid = futs[fut]
            try:
                results[pid] = fut.result()
            except Exception as e:
                print(f"[mass-seal] worker error {org.projects.get(pid, pid)}: {e}")
                results[pid] = "failed"

    # One sequential retry for failures — a transient API blip mid-sweep used
    # to leave a project unsealed and burning post-cap (observed live 2026-08-13).
    for pid, r in list(results.items()):
        if r == "failed":
            print(f"[mass-seal] retrying {org.projects.get(pid, pid)}/{track}")
            results[pid] = _throttle_track_for_project(pid, track, usage)

    for pid, r in results.items():
        {"throttled": throttled, "noop": noop, "failed": failed}.get(r, failed).append(pid)

    print(f"[mass-seal] {track} → done. throttled={len(throttled)} "
          f"exempt={len(exempt)} noop={len(noop)} failed={len(failed)}")
    _log_event("mass_seal", track=track, consumed=consumed,
               throttled=[org.projects.get(p, p) for p in throttled],
               exempt=[org.projects.get(p, p) for p in exempt],
               failed=[org.projects.get(p, p) for p in failed])
    _broadcast(lambda n, t=track, th=len(throttled), ex=len(exempt),
        f=len(failed): fmt_seal_batch_done(t, th, ex, f, org, n), subs, names, org=org)


def _in_org(org: Org, fn, *args):
    """Run fn(*args) in `org`'s console/log context — pool workers are new
    threads and don't inherit it."""
    with _org_context(org):
        return fn(*args)


def _run_per_project(label: str, fn, pids: list[str], org: Org) -> dict[str, str]:
    """Run `fn(pid)` for each of `org`'s projects on SEAL_SWEEP_WORKERS threads. A worker
    exception counts as 'failed'. Restores were sequential long after the seal
    sweep went parallel: 13 projects × ~19 rows took 5–10 min to unseal vs ~1.5
    min to seal (2026-09-28/29). Same safety argument as the seal: each worker
    only touches its own project's rows; store methods are lock-guarded."""
    results: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=SEAL_SWEEP_WORKERS) as ex:
        futs = {ex.submit(_in_org, org, fn, pid): pid for pid in pids}
        for fut in as_completed(futs):
            pid = futs[fut]
            try:
                results[pid] = fut.result()
            except Exception as e:
                print(f"[{label}] worker error {org.projects.get(pid, pid)}: {e}")
                results[pid] = "failed"
    return results


def _mass_unseal_track(track: str, usage: UsageStore, subs: SubscriberStore,
                       names: NameStore, *, reason: str = "manual") -> None:
    """Restore `track` across every currently-sealed project to the canonical
    baseline, marking each exempt so the auto-sweep won't re-seal today.
    REQUIRES: caller already holds the busy claim (`_try_claim_busy`)."""
    sealed = usage.get_sealed_tracks().get(track, {}).get("originals_by_project", {})
    pids   = list(sealed.keys())
    if not pids:
        return
    _broadcast(lambda n, t=track: fmt_unseal_batch_begin(t, n), subs, names, org=usage.org)

    baseline = _compute_canonical_baseline(usage)

    def _one(pid: str) -> str:
        result = _restore_track_for_project(pid, track, usage, baseline)
        if result == "restored":
            usage.add_track_exemption(pid, track)
        return result

    results  = _run_per_project("mass-unseal", _one, pids, usage.org)
    restored = sum(1 for r in results.values() if r == "restored")
    failed   = sum(1 for r in results.values() if r == "failed")
    print(f"[mass-unseal] {track} → restored={restored} failed={failed} ({reason})")
    _log_event("mass_unseal", track=track, restored=restored, failed=failed, reason=reason)
    _broadcast(lambda n, t=track, r=restored, f=failed:
        fmt_unseal_batch_done(t, r, f, n), subs, names, org=usage.org)


# ── Single-project manual seal / unseal (button-driven) ────────────────────

def _manual_seal_project(track: str, pid: str, usage: UsageStore,
                         subs: SubscriberStore, names: NameStore) -> str:
    """Throttle one project's `track` band to 0. Clears any exemption so the row
    stays sealed. Returns 'sealed' / 'noop' / 'failed'.
    REQUIRES: caller already holds the busy claim (`_try_claim_busy`)."""
    usage.remove_track_exemption(pid, track)
    proj = usage.org.projects.get(pid, pid)
    if usage.is_project_track_sealed(pid, track):
        # Recorded as sealed, but a seal whose rollback failed stays recorded
        # while only partly applied — verify against the API instead of trusting
        # state, and finish the job if any row is still open.
        if not _reseal_drifted_project(pid, track, usage):
            return "noop"
        result = "throttled"
    else:
        result = _throttle_track_for_project(pid, track, usage)
    if result == "throttled":
        print(f"[manual-seal] {proj}/{track}: sealed")
        _log_event("manual_seal", project=proj, track=track)
        _broadcast(lambda n, p=proj, t=track: fmt_manual_seal(p, t, usage.org, n), subs, names,
                   org=usage.org)
        return "sealed"
    if result == "noop":
        return "noop"
    return "failed"


def _manual_unseal_project(track: str, pid: str, usage: UsageStore,
                           subs: SubscriberStore, names: NameStore) -> str:
    """Restore one project's `track` band and mark it exempt for the day.
    Returns 'unsealed' / 'noop' / 'failed'.
    REQUIRES: caller already holds the busy claim (`_try_claim_busy`)."""
    proj = usage.org.projects.get(pid, pid)
    if not usage.is_project_track_sealed(pid, track):
        usage.add_track_exemption(pid, track)   # pre-exempt so sweep skips it
        return "noop"
    baseline = _compute_canonical_baseline(usage)
    result   = _restore_track_for_project(pid, track, usage, baseline)
    if result == "restored":
        usage.add_track_exemption(pid, track)
        print(f"[manual-unseal] {proj}/{track}: restored + exempt")
        _log_event("manual_unseal", project=proj, track=track)
        _broadcast(lambda n, p=proj, t=track: fmt_manual_unseal(p, t, usage.org, n), subs, names,
                   org=usage.org)
        return "unsealed"
    if result == "noop":
        return "noop"
    return "failed"


# ── Auto seal trigger (poll loop) ───────────────────────────────────────────

def _handle_track_seal(track: str, snap: dict, usage: UsageStore,
                       subs: SubscriberStore, names: NameStore) -> None:
    """Auto mass-seal entry: fired by the poll loop when a track crosses its seal
    threshold (normal 95%, premium 80%) or the wave guard projects a breach.
    Idempotent via the per-day `mass_sealed_tracks` flag. Skips silently if another
    op already holds the busy claim — the next poll re-checks the threshold and
    retries (consumption only grows, so the trigger condition won't disappear)."""
    if usage.is_mass_sealed(track):
        return
    if not _try_claim_busy(usage.org):
        print(f"[mass-seal] {track} deferred — another seal/unseal in progress")
        return
    try:
        consumed_key = "total_normal_tokens" if track == "normal" else "total_premium_tokens"
        _mass_seal_track(track, usage, subs, names,
                         consumed=snap.get(consumed_key, 0), respect_exemptions=True)
    finally:
        _release_busy(usage.org)


# Per-(org, date, track) memo of projects already successfully repaired, so the gap
# check doesn't re-POST zeros to the same project every poll. In-memory only —
# a restart just costs one redundant repair pass.
_REPAIR_DONE: dict[tuple, set] = {}
# (org, date, track) -> ts of the last live drift verification. Cleared by each mass
# sweep so the first poll after a sweep always verifies.
_DRIFT_CHECKED: dict[tuple, float] = {}
DRIFT_VERIFY_SECS = 300


def fmt_seal_repair(track: str, n: int, org: Org, name: str = "Bach") -> str:
    return (
        f"🔧 <b>{_band_label(track, org)} — {n} straggler project(s) sealed.</b>\n"
        f"<i>The initial sweep left gaps (transient API failure); they are "
        f"throttled now, Monarch {name}.</i>"
    )


def _reseal_drifted_project(pid: str, track: str, usage: UsageStore) -> int:
    """Re-throttle rows of an already-'sealed' project that have drifted back to
    healthy values. Returns the number of rows re-zeroed (0 = no drift).

    Captures are MERGED, never overwritten: only some rows may have drifted, and
    replacing the capture list wholesale would discard the originals of rows
    still at 0. Caller must hold the busy claim."""
    rate_limits = _fetch_project_rate_limits(pid, org=usage.org)
    if not rate_limits:
        return 0
    drifted = [rl for rl in rate_limits
               if _matches_track(rl.get("model", ""), track)
               and (rl.get("max_requests_per_1_minute") or rl.get("max_tokens_per_1_minute"))]
    if not drifted:
        return 0
    usage.merge_track_originals(track, pid, _capture_originals(drifted))
    rezeroed = 0
    for rl in drifted:
        payload = _seal_payload(rl)
        if payload and _update_project_rate_limit(pid, rl["id"], payload, org=usage.org):
            rezeroed += 1
            time.sleep(0.05)
    if rezeroed:
        _SEALED_AT[(track, pid)] = time.time()
    return rezeroed


def _repair_seal_gaps(track: str, usage: UsageStore, subs: SubscriberStore,
                      names: NameStore) -> None:
    """Self-healing pass, run every poll while a track is mass-sealed (including
    early wave-guard seals below the static threshold — those used to go
    unverified until usage reached it). Two failure modes, both observed live:

    1. GAP — a project missing from `sealed_tracks` because its seal failed
       mid-sweep (2026-08-13: 1/13 premium seals failed and that project kept
       burning post-cap for hours). Re-sealed, then memoized per (day, track).
    2. DRIFT — a project the bot BELIEVES is sealed whose rate limits are
       actually healthy again (2026-08-22: found via smoke test — state and
       reality disagreed and nothing ever noticed). Trusting state alone makes
       the bot confidently wrong, so believed-sealed projects are VERIFIED
       against the live API and re-zeroed on drift. Never memoized: drift can
       recur at any time — but verified every DRIFT_VERIFY_SECS, not every poll.
       At 60 s polling that was ~13 GETs a minute per sealed track for the rest
       of the day; the one live drift ever seen was found 13 min after its seal.
       The first poll after each sweep always verifies.
    """
    sealed = usage.get_sealed_tracks().get(track, {}).get("originals_by_project", {})
    org      = usage.org
    memo_key = (org.id, today_str(), track)
    done = _REPAIR_DONE.setdefault(memo_key, set())
    candidates = [pid for pid in org.projects if not usage.is_exempt(pid, track)]
    gaps    = [p for p in candidates if p not in sealed and p not in done]
    verify  = time.time() - _DRIFT_CHECKED.get(memo_key, 0.0) >= DRIFT_VERIFY_SECS
    believed = [p for p in candidates if p in sealed] if verify else []
    if not gaps and not believed:
        return
    if not _try_claim_busy(org):
        return   # another op running — retry next poll
    try:
        repaired, drifted = [], []
        for pid in gaps:
            result = _throttle_track_for_project(pid, track, usage)
            if result in ("throttled", "noop"):
                done.add(pid)          # settled — don't re-attempt today
                if result == "throttled":
                    repaired.append(pid)
            # 'failed' stays out of the memo -> retried next poll
        for pid in believed:
            if _reseal_drifted_project(pid, track, usage):
                drifted.append(pid)
        if verify:
            _DRIFT_CHECKED[memo_key] = time.time()
        if repaired or drifted:
            if repaired:
                print(f"[seal-repair] {track}: sealed gaps -> "
                      f"{', '.join(org.projects.get(p, p) for p in repaired)}")
            if drifted:
                print(f"[seal-repair] {track}: re-sealed DRIFTED -> "
                      f"{', '.join(org.projects.get(p, p) for p in drifted)}")
            _log_event("seal_repair", track=track,
                       repaired=[org.projects.get(p, p) for p in repaired],
                       drifted=[org.projects.get(p, p) for p in drifted])
            _broadcast(lambda n, t=track, c=len(repaired) + len(drifted):
                fmt_seal_repair(t, c, org, n), subs, names, org=org)
    finally:
        _release_busy(org)


# Retry bookkeeping for the midnight restore queue: (track, pid) ->
# (failed attempts, next attempt ts, day). A row that fails permanently (archived
# project, unparsed 4xx) used to be re-POSTed — with 13 baseline GETs and a
# begin/done broadcast pair — on EVERY 60 s poll, forever.
_PENDING_RETRY: dict[tuple, tuple] = {}
_PENDING_ANNOUNCED: set = set()      # (org, day) whose "begin unsealing" went out
PENDING_RETRY_MAX_SECS    = 1800     # backoff ceiling between retries
PENDING_RETRY_ALERT_AFTER = 6        # failed attempts before a one-time alert


def _split_pending_rows(pid: str, rows: list, usage: UsageStore) -> tuple[list, list]:
    """(restorable now, held back). Rows covered by one of TODAY's seals on this
    project are held: restoring yesterday's capture would silently reopen a row
    that today's seal or quarantine zeroed (and never captured, since it was
    already at 0). Held rows stay queued and go out after today's seal lifts."""
    if usage.is_project_track_sealed(pid, QUARANTINE_TRACK):
        return [], list(rows)
    live = [t for t in ("normal", "premium") if usage.is_project_track_sealed(pid, t)]
    now, held = [], []
    for r in rows:
        (held if any(_matches_track(r.get("model", ""), t) for t in live) else now).append(r)
    return now, held


def _process_pending_track_unseals(usage: UsageStore, subs: SubscriberStore,
                                   names: NameStore) -> None:
    """At day rollover, sealed_tracks moves into pending_track_unseal. This drains
    it to the canonical baseline. Per-row bookkeeping: only rows that failed stay
    queued. Failed projects retry with exponential backoff (one alert if they stay
    stuck); rows covered by today's seals are held. Begin/done broadcasts go out
    on the day's first pass and afterwards only when a retry succeeds.
    Skips silently if the busy claim can't be obtained — the queue persists."""
    now, day = time.time(), today_str()
    jobs: dict[str, dict[str, tuple]] = {}   # track -> pid -> (rows_now, rows_held)
    for track, info in usage.get_pending_track_unseal().items():
        for pid, rows in list((info.get("originals_by_project") or {}).items()):
            if not rows:
                usage.pop_pending_track_project(track, pid)
                continue
            _, next_ts, rday = _PENDING_RETRY.get((track, pid), (0, 0.0, day))
            if rday == day and now < next_ts:
                continue
            rows_now, rows_held = _split_pending_rows(pid, rows, usage)
            if rows_now:
                jobs.setdefault(track, {})[pid] = (rows_now, rows_held)
    if not jobs:
        return
    org = usage.org
    if not _try_claim_busy(org):
        print("[pending-track-unseal] deferred — another seal/unseal in progress")
        return
    try:
        tracks_str = " & ".join(sorted(jobs))
        first_pass = (org.id, day) not in _PENDING_ANNOUNCED
        if first_pass:
            _PENDING_ANNOUNCED.add((org.id, day))
            _broadcast(lambda n, t=tracks_str: fmt_unseal_batch_begin(t, n), subs, names, org=org)

        baseline = _compute_canonical_baseline(usage)
        restored, failed_p, stuck = 0, 0, []
        for track, tj in jobs.items():
            print(f"[pending-track-unseal] {track} → {len(tj)} project(s)")

            def _one(pid: str, track=track, tj=tj) -> str:
                rows_now, rows_held = tj[pid]
                left = _restore_rows(pid, rows_now, baseline=baseline, org=org)
                if left or rows_held:
                    usage.set_pending_track_rows(track, pid, left + rows_held)
                else:
                    usage.pop_pending_track_project(track, pid)
                if left:
                    print(f"[pending-track-unseal] {org.projects.get(pid, pid)}/{track}: "
                          f"{len(left)}/{len(rows_now)} row(s) failed — will retry")
                    return "failed"
                return "restored"

            for pid, r in _run_per_project("pending-track-unseal", _one, list(tj), org).items():
                if r == "failed":
                    attempts = _PENDING_RETRY.get((track, pid), (0, 0.0, day))[0] + 1
                    _PENDING_RETRY[(track, pid)] = (
                        attempts, time.time() + min(60 * 2 ** attempts, PENDING_RETRY_MAX_SECS), day)
                    if attempts == PENDING_RETRY_ALERT_AFTER:
                        stuck.append(f"{org.projects.get(pid, pid)} ({track})")
                    failed_p += 1
                else:
                    _PENDING_RETRY.pop((track, pid), None)
                    restored += 1

        _log_event("pending_unseal", tracks=tracks_str, restored=restored, failed=failed_p)
        if first_pass or restored:
            _broadcast(lambda n, r=restored, f=failed_p, t=tracks_str:
                fmt_unseal_batch_done(t, r, f, n), subs, names, org=org)
        if stuck:
            _log_event("pending_unseal_stuck", projects=stuck)
            _broadcast(lambda n, s=stuck: fmt_restore_stuck(s, n), subs, names, org=org)
    finally:
        _release_busy(org)


def fmt_restore_stuck(labels: list, name: str = "Bach") -> str:
    items = "\n".join(f"• <b>{l}</b>" for l in labels)
    return (
        "⚠️ <b>Restore failing repeatedly</b>\n\n"
        f"{items}\n\n"
        f"Yesterday's seal could not be lifted after {PENDING_RETRY_ALERT_AFTER} attempts. "
        "Retrying every 30 min — check these projects' rate limits on the platform.\n"
        f"<i>Monarch {name}, your attention is required.</i>"
    )


# ── Formatters — seal/unseal alerts ────────────────────────────────────────

def _band_label(track: str, org: Org) -> str:
    if track == "normal":
        return f"Normal ({_fmt_cap(org.normal_cap)})"
    if track == "premium":
        return f"Premium ({_fmt_cap(org.premium_cap)})"
    return "Quarantine (all models)"   # QUARANTINE_TRACK


def _archive_hint(org: Org, action: str, track_label: str) -> str:
    """Button path for the archive menu, e.g. "/archive → Unseal → Lab 3 →
    Normal → project" (the org step exists only with more than one org)."""
    org_step = f" → {org.short}" if len(ORGS) > 1 else ""
    return f"/archive → {action}{org_step} → {track_label} → project"


def fmt_manual_seal(proj_name: str, track: str, org: Org, name: str = "Bach") -> str:
    return (
        f"🔒 <b>{proj_name} — {_band_label(track, org)} sealed.</b>\n"
        f"<i>Rate limits throttled to 0. Auto-restore at UTC midnight. "
        f"Order carried out, Monarch {name}.</i>"
    )


def fmt_manual_unseal(proj_name: str, track: str, org: Org, name: str = "Bach") -> str:
    return (
        f"🔓 <b>{proj_name} — {_band_label(track, org)} unsealed.</b>\n"
        f"<i>Restored and exempt from auto-seal until UTC midnight. "
        f"The overcap alarm still fires if it burns past the cap. Monarch {name}.</i>"
    )


def fmt_seal_batch_begin(track: str, consumed: int, cap: int, org: Org,
                         name: str = "Bach", manual: bool = False) -> str:
    """`cap` is the track's enforcement ceiling. An org that pays past its free
    allowance reports tokens, not a percentage of the allowance ("hit 320%")."""
    pct  = consumed / cap * 100 if cap else 100
    band = _band_label(track, org)
    level = (f"at {_fmt_tokens(consumed)} (seal point {_fmt_tokens(org.threshold(track))})"
             if org.pays_past_free(track) else f"at {pct:.0f}%")
    if manual:   # a button press, not a threshold crossing — don't claim "hit 100%"
        return (
            f"🛑 <b>Manual seal — {band} {level} — sealing all projects…</b>\n"
            f"<i>Throttling {track}-band rate limits to 0 on your order. Stand by, Monarch {name}.</i>"
        )
    return (
        f"🛑 <b>{band} {level.replace('at ', 'hit ', 1)} — begin sealing all projects…</b>\n"
        f"<i>Throttling {track}-band rate limits to 0. Stand by, Monarch {name}.</i>"
    )


def fmt_seal_batch_done(track: str, throttled: int, exempt: int, failed: int,
                        org: Org, name: str = "Bach") -> str:
    band = _band_label(track, org)
    tail = f"  ({exempt} exempt)" if exempt else ""
    warn = f"  ⚠️ {failed} failed" if failed else ""
    return (
        f"🔒 <b>Done sealing {band}.</b> {throttled} project(s) throttled{tail}{warn}.\n"
        f"<i>Auto-restore at UTC midnight. Exempt one: "
        f"{_archive_hint(org, 'Unseal', track.title())}.</i>"
    )


def fmt_unseal_batch_begin(tracks_str: str, name: str = "Bach") -> str:
    return (
        f"🔓 <b>Begin unsealing ({tracks_str})…</b>\n"
        f"<i>Restoring rate limits across all projects. Stand by, Monarch {name}.</i>"
    )


def fmt_unseal_batch_done(tracks_str: str, restored: int, failed: int,
                          name: str = "Bach") -> str:
    warn = f"  ⚠️ {failed} still pending (will retry)" if failed else ""
    return (
        f"✅ <b>Done unsealing ({tracks_str}).</b> {restored} project(s) restored{warn}.\n"
        f"<i>Operations resume, Monarch {name}.</i>"
    )


# ── Formatters — command responses ─────────────────────────────────────────

def fmt_daily_snapshot(snap: dict, org: Org) -> str:
    projects   = snap.get("projects", {})
    total_cost = snap.get("total_cost", 0.0)
    total_tok  = sum(p.get("total_tokens", 0) for p in projects.values())
    date       = snap.get("date", today_str())

    lines = [f"📊 <b>Usage Report — {date}</b>  <i>(as of {_fmt_ts(snap.get('last_polled'))})</i>\n"]
    active = {pid: p for pid, p in projects.items()
              if p.get("total_tokens", 0) > 0 or p.get("cost_usd", 0) > 0}

    if not active:
        lines.append("No usage recorded today.")
    else:
        for pid, p in sorted(active.items(),
                             key=lambda x: (x[1].get("total_tokens", 0), x[1].get("cost_usd", 0)),
                             reverse=True):
            inp  = _fmt_tokens(p.get("input_tokens", 0))
            out  = _fmt_tokens(p.get("output_tokens", 0))
            tot  = _fmt_tokens(p.get("total_tokens", 0))
            cost = p.get("cost_usd", 0.0)
            reqs = p.get("num_requests", 0)
            lines.append(
                f"🔹 <b>{p['name']}</b>\n"
                f"   🔢 {tot}  ({inp} in / {out} out)  •  {reqs:,} reqs  •  <b>${cost:.4f}</b>"
            )
            lines.append("")

    total_premium = snap.get("total_premium_tokens", 0)
    total_normal  = snap.get("total_normal_tokens",  0)
    cost_note     = f"  <i>(cost as of {_fmt_ts(snap.get('costs_ts'))})</i>" if snap.get("costs_stale") else ""
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(
        f"🔢 Tokens: <b>{_fmt_tokens(total_tok)}</b>   💰 Cost: <b>${total_cost:.4f}</b> / ${org.daily_limit:.2f}{cost_note}"
    )
    lines.extend(_fmt_lane_lines(total_premium, total_normal, snap.get("lane_costs"), org))
    return "\n".join(lines)


# ── Milestone checker ──────────────────────────────────────────────────────

def seed_milestones(snap: dict, usage: UsageStore,
                    subs: "SubscriberStore" = None, names: "NameStore" = None) -> None:
    """Fire only the highest already-crossed milestone per track on first poll of a day.
    Race-safe: the atomic `seed_state` returns True only for the unique caller that
    actually flipped the flag, so concurrent /refresh + poll-loop calls broadcast at
    most once."""
    total_normal  = snap.get("total_normal_tokens", 0)
    total_premium = snap.get("total_premium_tokens", 0)

    org = usage.org
    normal_crossed  = [(t, l) for t, l in org.normal_milestones  if total_normal  >= t]
    premium_crossed = [(t, l) for t, l in org.premium_milestones if total_premium >= t]

    # Atomic claim — only the winning caller gets True and broadcasts.
    if not usage.seed_state(
        normal_thresholds  = [t for t, _ in normal_crossed],
        premium_thresholds = [t for t, _ in premium_crossed],
    ):
        return

    if normal_crossed and subs:
        t, l = normal_crossed[-1]   # highest crossed
        _broadcast(lambda n, t=t, c=total_normal, l=l: fmt_token_milestone(t, c, l, n, org),
                   subs, names, org=org)

    if premium_crossed and subs:
        t, l = premium_crossed[-1]
        _broadcast(lambda n, t=t, c=total_premium, l=l: fmt_premium_token_milestone(t, c, l, org, n),
                   subs, names, org=org)


def fmt_spend_milestone(threshold: float, current: float, level: str, name: str = "Bach",
                        limit: float = DAILY_LIMIT) -> str:
    """Org-wide cumulative spend milestone; `limit` = that org's daily alarm."""
    if level == "casual":
        return (
            f"💰 <b>Spend Milestone — ${threshold:.2f}</b>\n\n"
            f"Cumulative outlay today: <b>${current:.4f}</b> of ${limit:.2f}.\n"
            f"<i>The treasury is monitored, Monarch {name}.</i>"
        )
    if level == "urgent":
        return (
            f"⚠️ <b>Significant Spend — ${threshold:.2f}</b>\n\n"
            f"Today's expenditure stands at <b>${current:.4f}</b> of ${limit:.2f}.\n"
            f"The daily cap is approaching. Your attention is advised, My Liege {name}."
        )
    # cap (the org's daily limit)
    return (
        f"🚨 <b>Daily Spend Cap Breached — ${current:.4f}</b>\n\n"
        f"The ${limit:.2f} daily expenditure cap has been crossed.\n"
        f"Polling escalated to <b>AGGRESSIVE</b>. Use /archive to seal "
        f"projects manually — note that unlisted models (embeddings, image, audio, etc.) "
        f"bypass the seal logic and must be stopped at the source.\n\n"
        f"Monarch {name}, the operation demands your command."
    )


def fmt_project_spend(pid: str, threshold: float, current: float, name: str = "Bach") -> str:
    proj = _pname(pid)
    return (
        f"💸 <b>Project Spend — {proj}</b>\n\n"
        f"<b>{proj}</b> has crossed <b>${threshold:.2f}</b> today (now ${current:.4f}).\n"
        f"<i>Single-project anomaly threshold tripped. The wallet is watched, Monarch {name}.</i>"
    )


def fmt_unlisted_model(pid: str, model: str, requests: int, tokens: int,
                       cost: float, name: str = "Bach") -> str:
    """Alert for first-touch of a model not in either free-tier watchlist.
    These bill at standard rates from token 1 and are NOT throttled by the seal
    logic. Embeddings, image gen, audio, fine-tuned models, gpt-3.5, etc."""
    proj = _pname(pid)
    cost_str = f"  •  <b>${cost:.4f}</b>" if cost > 0 else ""
    return (
        f"🟠 <b>Unlisted Model Activity — Off-Watchlist Spend</b>\n\n"
        f"Project: <b>{proj}</b>\n"
        f"Model:   <code>{html.escape(model)}</code>  (not on either free-tier list)\n"
        f"Usage:   {requests:,} req  •  {_fmt_tokens(tokens)} tok{cost_str}\n\n"
        f"This model bills at <b>standard rates from the first token</b>. "
        f"The project will be <b>quarantined</b> — every rate limit throttled "
        f"to 0 until UTC midnight.\n"
        f"<i>One alert per (project, model) per day. Monarch {name}, the off-list "
        f"ledger has shifted.</i>"
    )


def check_spend(snap: dict, usage: UsageStore, subs: SubscriberStore,
                names: NameStore = None) -> tuple[bool, bool]:
    """Fire alerts for newly crossed org-wide spend milestones AND per-project
    spend thresholds. Returns (any_milestone_hit, cap_crossed).
    `cap_crossed` is True the FIRST poll that observes total ≥ the org's daily limit
    (after that, the milestone is in the notified set and won't re-fire).
    Cost data has a 5-10 min OpenAI ingestion lag — alerts may arrive slightly
    delayed, but that's still vastly better than the previous "never" state."""
    org = usage.org
    limit, step = org.daily_limit, org.spend_overcap_step
    total_cost = snap.get("total_cost", 0.0) or 0.0
    hit = False
    cap_crossed = False

    # ── Org-wide spend milestones ───────────────────────────────────────────
    notified = usage.get_spend_milestones_notified()
    for threshold, level in org.spend_milestones:
        if total_cost >= threshold and threshold not in notified:
            hit = True
            usage.add_spend_milestone_notified(threshold)
            _broadcast(lambda n, t=threshold, c=total_cost, l=level:
                fmt_spend_milestone(t, c, l, n, limit), subs, names, org=org)
            if level == "cap":
                cap_crossed = True

    # ── Overcap escalation — never go silent while the bleed continues ──────
    # Every extra overcap step past the cap fires another cap-level alert
    # (Lab 2: $2.50, $3.00, …). Dynamic thresholds share the same notified list;
    # they never collide with the static ones (all static thresholds ≤ the limit).
    if total_cost > limit:
        steps = int((total_cost - limit) / step)
        for i in range(1, steps + 1):
            threshold = round(limit + i * step, 2)
            if threshold not in notified:
                hit = True
                cap_crossed = True
                usage.add_spend_milestone_notified(threshold)
                _broadcast(lambda n, t=threshold, c=total_cost:
                    fmt_spend_milestone(t, c, "cap", n, limit), subs, names, org=org)

    # ── Per-project spend thresholds ────────────────────────────────────────
    for pid, p in snap.get("projects", {}).items():
        cost = float(p.get("cost_usd", 0.0) or 0.0)
        if cost <= 0:
            continue
        proj_notified = usage.get_project_spend_notified(pid)
        for threshold in org.project_spend_thresholds:
            if cost >= threshold and threshold not in proj_notified:
                hit = True
                usage.add_project_spend_notified(pid, threshold)
                _broadcast(lambda n, p=pid, t=threshold, c=cost:
                    fmt_project_spend(p, t, c, n), subs, names, org=org)

    return hit, cap_crossed


def check_unlisted_models(snap: dict, usage: UsageStore, subs: SubscriberStore,
                          names: NameStore = None) -> bool:
    """Fire one alert per (project, model) per day when a project uses any model
    not on either free-tier watchlist. These bypass the token-milestone, overcap,
    and seal logic — they're the path the $6 embedding incident took. Returns
    True if any new alert fired."""
    fired = False
    for pid, p in snap.get("projects", {}).items():
        models  = p.get("models", {})
        p_cost  = float(p.get("cost_usd", 0.0) or 0.0)
        # Apportion cost to unlisted models by share of total tokens (rough but
        # better than reporting nothing). If no listed-model usage exists, the
        # entire project cost belongs to unlisted models.
        total_tok = sum(m.get("input", 0) + m.get("output", 0) for m in models.values()) or 1
        for model, m in models.items():
            if _track_for_model(model) is not None:
                continue   # listed model — covered by token-milestone path
            reqs = m.get("requests", 0)
            tok  = m.get("input", 0) + m.get("output", 0)
            if reqs < UNLISTED_MODEL_MIN_REQUESTS and tok == 0:
                continue
            if usage.is_unlisted_alerted(pid, model):
                continue
            est_cost = p_cost * (tok / total_tok) if p_cost > 0 and tok > 0 else 0.0
            usage.mark_unlisted_alerted(pid, model)
            _broadcast(lambda n, pi=pid, mo=model, r=reqs, t=tok, c=est_cost:
                fmt_unlisted_model(pi, mo, r, t, c, n), subs, names, org=usage.org)
            fired = True
            print(f"[unlisted-alert] {usage.org.projects.get(pid, pid)}/{model} "
                  f"reqs={reqs} tok={tok} est_cost=${est_cost:.4f}")
    return fired


# ── Quarantine: full-project seal on ANY off-watchlist usage ────────────────
# Unlisted models bill from token 1 and can't be selectively track-throttled —
# the only safe response is to seal the offending project ENTIRELY (every
# rate-limit row, listed or not; embedding/gpt-5.6/etc rows all accept 0).
# Originals live in sealed_tracks[QUARANTINE_TRACK] so the standard midnight
# rollover → pending_track_unseal → restore path applies unchanged.
QUARANTINE_TRACK = "full"

# In-memory memo of projects whose quarantine came back 'noop' (no rate-limit
# rows to throttle) so they aren't re-attempted every poll. Keyed (date, pid).
_QUARANTINE_NOOP: set = set()
# (day, pid) -> earliest retry ts after a failed full seal. Retrying every 60 s
# poll meant a GET + up to ~190 POSTs a minute for a persistently failing project.
_QUARANTINE_RETRY: dict[tuple, float] = {}
QUARANTINE_RETRY_SECS = 180


def fmt_quarantine(proj_name: str, models: list, org: Org, name: str = "Bach") -> str:
    ms = ", ".join(f"<code>{html.escape(m)}</code>" for m in models[:5])
    return (
        f"☣️ <b>{proj_name} — QUARANTINED.</b>\n\n"
        f"Off-watchlist model usage detected: {ms}\n"
        f"Unlisted models bill at standard rates from the first token and cannot "
        f"be selectively throttled — <b>every</b> rate limit of this project is "
        f"now 0 until UTC midnight.\n"
        f"<i>Release: {_archive_hint(org, 'Unseal', 'Both')}. "
        f"Monarch {name}, the breach is contained.</i>"
    )


def fmt_quarantine_release(proj_name: str, name: str = "Bach") -> str:
    return (
        f"🔓 <b>{proj_name} — quarantine lifted.</b>\n"
        f"<i>All rate limits restored; exempt from re-quarantine until UTC "
        f"midnight. Off-watchlist spend is on your head now, Monarch {name}.</i>"
    )


def _full_seal_project(pid: str, usage: UsageStore) -> str:
    """Throttle EVERY rate-limit row of `pid` to 0 (all models, listed or not),
    capturing healthy pre-throttle originals under QUARANTINE_TRACK. Returns
    'sealed' / 'noop' / 'failed'. Rolls back its own rows on partial failure.
    Caller must hold the busy claim."""
    rate_limits = _fetch_project_rate_limits(pid, org=usage.org)
    if rate_limits is None:
        return "failed"
    if not rate_limits:
        return "noop"
    # Rows already at 0 (e.g. track-sealed earlier today) stay owned by their
    # existing capture — _seal_rows only captures healthy rows.
    originals = _seal_rows(pid, QUARANTINE_TRACK, rate_limits, usage)
    if originals is None:
        return "failed"
    return "sealed" if originals else "noop"


def _quarantine_unlisted_users(snap: dict, usage: UsageStore, subs: SubscriberStore,
                               names: NameStore = None) -> None:
    """Auto-seal any of `usage.org`'s projects that touched an off-watchlist model today —
    even once. Runs every poll: sealed_tracks['full'] presence is the dedup,
    an exemption on 'full' (set when the user releases the quarantine) is the
    opt-out, and failures simply retry next poll."""
    org = usage.org
    offenders: dict[str, list] = {}
    for pid, p in snap.get("projects", {}).items():
        if pid not in org.projects:
            continue
        bad = [m for m, mm in p.get("models", {}).items()
               if _track_for_model(m) is None
               and (mm.get("requests", 0) or mm.get("input", 0) + mm.get("output", 0))]
        if bad:
            offenders[pid] = bad

    day, now = today_str(), time.time()
    todo = []
    for pid in offenders:
        if usage.is_exempt(pid, QUARANTINE_TRACK) or (day, pid) in _QUARANTINE_NOOP:
            continue
        retry_at = _QUARANTINE_RETRY.get((day, pid))
        if retry_at is not None:
            # A failed full seal may still be recorded (write-ahead captures kept
            # when its rollback also failed) — retry it anyway, on a backoff.
            if now >= retry_at:
                todo.append(pid)
        elif not usage.is_project_track_sealed(pid, QUARANTINE_TRACK):
            todo.append(pid)
    if not todo:
        return
    if not _try_claim_busy(org):
        print("[quarantine] deferred — another seal/unseal in progress")
        return
    try:
        for pid in todo:
            proj   = org.projects.get(pid, pid)
            result = _full_seal_project(pid, usage)
            if result == "failed":
                _QUARANTINE_RETRY[(day, pid)] = time.time() + QUARANTINE_RETRY_SECS
                print(f"[quarantine] {proj} failed — retry in {QUARANTINE_RETRY_SECS // 60} min")
                continue
            retried = _QUARANTINE_RETRY.pop((day, pid), None) is not None
            if result == "sealed":
                print(f"[quarantine] {proj} sealed (models: {offenders[pid]})")
                _log_event("quarantine", project=proj, models=offenders[pid])
                _broadcast(lambda n, p=proj, ms=offenders[pid]:
                    fmt_quarantine(p, ms, org, n), subs, names, org=org)
            elif not retried:
                _QUARANTINE_NOOP.add((day, pid))
    finally:
        _release_busy(org)


def _release_quarantine(pid: str, usage: UsageStore, subs: SubscriberStore,
                        names: NameStore) -> str:
    """Restore a quarantined project's rows and exempt it from re-quarantine
    for the rest of the UTC day. Caller must hold the busy claim."""
    proj = usage.org.projects.get(pid, pid)
    if not usage.is_project_track_sealed(pid, QUARANTINE_TRACK):
        usage.add_track_exemption(pid, QUARANTINE_TRACK)   # opt out of re-quarantine
        return "noop"
    baseline = _compute_canonical_baseline(usage)
    result   = _restore_track_for_project(pid, QUARANTINE_TRACK, usage, baseline)
    if result == "restored":
        usage.add_track_exemption(pid, QUARANTINE_TRACK)
        print(f"[quarantine] {proj} released + exempt")
        _log_event("quarantine_release", project=proj)
        _broadcast(lambda n, p=proj: fmt_quarantine_release(p, n), subs, names, org=usage.org)
        return "unsealed"
    return result


def seed_spend(snap: dict, usage: UsageStore, subs: SubscriberStore,
               names: NameStore = None) -> None:
    """First-of-day spend seed: mark every already-crossed threshold as notified
    so we don't flood the chat on bot restart, but fire the HIGHEST one as a
    catch-up so the user sees today's true position. Atomic via claim_spend_seed."""
    if not usage.claim_spend_seed():
        return

    org = usage.org
    limit, step = org.daily_limit, org.spend_overcap_step
    total_cost = snap.get("total_cost", 0.0) or 0.0

    # Mark every crossed org threshold as notified — silent — then fire only the
    # highest as a catch-up broadcast (mirrors token seed_milestones pattern).
    # Includes the dynamic overcap steps ($2.50, $3.00, …) so a restart at $3.40
    # doesn't flood every step in one burst on the next poll.
    crossed = [(t, l) for t, l in org.spend_milestones if total_cost >= t]
    if total_cost > limit:
        steps = int((total_cost - limit) / step)
        for i in range(1, steps + 1):
            crossed.append((round(limit + i * step, 2), "cap"))
    for t, _ in crossed:
        usage.add_spend_milestone_notified(t)
    if crossed and subs:
        t, l = crossed[-1]
        _broadcast(lambda n, t=t, c=total_cost, l=l: fmt_spend_milestone(t, c, l, n, limit),
                   subs, names, org=org)

    # Per-project: mark crossed silently (no catch-up broadcast — could be many).
    for pid, p in snap.get("projects", {}).items():
        cost = float(p.get("cost_usd", 0.0) or 0.0)
        for t in org.project_spend_thresholds:
            if cost >= t:
                usage.add_project_spend_notified(pid, t)


def check_milestones(snap: dict, usage: UsageStore, subs: SubscriberStore, names: NameStore = None) -> bool:
    """Called after every non-seed poll. Fires alerts for newly crossed thresholds.
    Returns True if at least one new milestone was hit (used to trigger urgent mode)."""
    hit = False

    org = usage.org

    # Normal band (the org's normal cap)
    total_tok = snap.get("total_normal_tokens", 0)
    notified  = usage.get_milestones_notified()
    for threshold, level in org.normal_milestones:
        if total_tok >= threshold and threshold not in notified:
            hit = True
            usage.add_milestone_notified(threshold)
            _broadcast(lambda n, t=threshold, c=total_tok, l=level: fmt_token_milestone(t, c, l, n, org),
                       subs, names, org=org)

    # Premium band (the org's premium cap)
    total_premium    = snap.get("total_premium_tokens", 0)
    notified_premium = usage.get_premium_milestones_notified()
    for threshold, level in org.premium_milestones:
        if total_premium >= threshold and threshold not in notified_premium:
            hit = True
            usage.add_premium_milestone_notified(threshold)
            _broadcast(lambda n, t=threshold, c=total_premium, l=level:
                       fmt_premium_token_milestone(t, c, l, org, n), subs, names, org=org)

    return hit


# ── Command handlers ───────────────────────────────────────────────────────

def cmd_usage(usage: UsageStore, name: str = "Bach") -> str:
    """Today's usage of `usage.org` in one report: every project busiest first
    (tokens, requests, spend, per-model lines — 🧪 marks an off-watchlist model),
    a by-model total when several projects ran, and the three lane lines.
    Replaces the tokens / projects / rank / models reports, which each showed a
    slice of the same data."""
    snap     = usage.get()
    projects = snap.get("projects", {})
    active   = {pid: p for pid, p in projects.items()
                if p.get("total_tokens", 0) > 0 or p.get("cost_usd", 0) > 0}
    if not active:
        return f"No usage today, Monarch {name}."

    def _mtok(m: dict) -> int:
        return m.get("input", 0) + m.get("output", 0)

    def _tag(model: str) -> str:
        return " 🧪" if _track_for_model(model) is None else ""

    lines = [f"📊 <b>Usage — {snap.get('date', today_str())}</b>\n"]
    agg: dict[str, int] = {}
    for pid, p in sorted(active.items(), key=lambda x: (x[1].get("total_tokens", 0),
                                                         x[1].get("cost_usd", 0)), reverse=True):
        lines.append(f"🔹 <b>{p['name']}</b>  {_fmt_tokens(p.get('total_tokens', 0))}  ·  "
                     f"{p.get('num_requests', 0):,} reqs  ·  ${p.get('cost_usd', 0.0):.4f}")
        for model, m in sorted(p.get("models", {}).items(), key=lambda x: _mtok(x[1]), reverse=True):
            agg[model] = agg.get(model, 0) + _mtok(m)
            lines.append(f"   <code>{html.escape(model)}</code>  {_fmt_tokens(m.get('input', 0))} in / "
                         f"{_fmt_tokens(m.get('output', 0))} out  ({m.get('requests', 0):,} reqs){_tag(model)}")
        lines.append("")

    total_tok = sum(p.get("total_tokens", 0) for p in active.values())
    if len(active) > 1 and agg:
        lines.append("<b>By model</b>")
        for model, tok in sorted(agg.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"   <code>{html.escape(model)}</code>  {_fmt_tokens(tok)}  "
                         f"({tok * 100 // max(total_tok, 1)}%){_tag(model)}")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🔢 Total: <b>{_fmt_tokens(total_tok)}</b>  •  "
                 f"{sum(p.get('num_requests', 0) for p in active.values()):,} requests  •  "
                 f"💰 <b>${snap.get('total_cost', 0.0):.4f}</b>")
    lines.extend(_fmt_lane_lines(snap.get("total_premium_tokens", 0),
                                 snap.get("total_normal_tokens", 0), snap.get("lane_costs"), usage.org))
    return "\n".join(lines)


def cmd_spending(usage: UsageStore, name: str = "Bach") -> str:
    """The money view of `usage.org`, fetched live: this month and last month per
    project, plus the last 31 days' token and request totals (the old `recent`
    report, whose per-project costs duplicated these two months)."""
    org        = usage.org
    now        = datetime.now(timezone.utc)
    cy, cm     = now.year, now.month
    py, pm     = prev_month()
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_curr   = ex.submit(_in_org, org, _fetch_monthly_costs, org, cy, cm)
        f_prev   = ex.submit(_in_org, org, _fetch_monthly_costs, org, py, pm)
        f_recent = ex.submit(_in_org, org, _fetch_recent_usage, org, 31)
    curr_costs, prev_costs, recent = f_curr.result(), f_prev.result(), f_recent.result()

    lines = ["💰 <b>Spending</b>\n"]

    def _section(label: str, costs: Optional[dict]):
        lines.append(f"<b>── {label} ──</b>")
        if costs is None:
            lines.append(f"  ⚠️ OpenAI API error ({html.escape(org.last_api_error or 'unknown')}) — "
                         f"spend unknown, NOT zero.\n")
            return
        active = {pid: v for pid, v in costs.items() if v > 0.0}
        if active:
            for pid, cost in sorted(active.items(), key=lambda x: x[1], reverse=True):
                proj_label = "Unattributed" if pid == "__org__" else org.projects.get(pid, pid)
                lines.append(f"  • {proj_label}: <b>${cost:.4f}</b>")
        else:
            lines.append("  No spend recorded.")
        lines.append(f"  Total: <b>${sum(costs.values()):.4f}</b>\n")

    _section(_fmt_month(cy, cm) + " (current)", curr_costs)
    _section(_fmt_month(py, pm) + " (previous)", prev_costs)

    lines.append("<b>── Last 31 days ──</b>")
    if recent is None:
        lines.append(f"  ⚠️ OpenAI API error ({html.escape(org.last_api_error or 'unknown')}) — "
                     f"usage unknown, NOT zero.")
    else:
        lines.append(f"  🔢 <b>{_fmt_tokens(recent[0])}</b> tokens   📨 <b>{recent[1]:,}</b> requests")
    lines.append(f"\n<i>Monarch {name}, your accounts are presented in full.</i>")
    return "\n".join(lines)


# ── Archive (seal/unseal) — interactive button UI ──────────────────────────
#
# Flow:  archive  →  [Seal] [Unseal] [Cancel]            (status of every org)
#          → action chosen → [Lab 2] [Lab 3] [Cancel]    (the org — skipped with one org)
#            → org chosen → [Normal] [Premium] [Both] [Cancel]   (the "mode")
#              → mode chosen → that org's project buttons + [ALL] [Cancel]
#                → project chosen → applies seal/unseal, re-renders the status
#
# Callback data: "arch:<org>:<action>:<mode>:<target>"   (≤ 54 bytes; cap is 64)
#   org    ∈ {-} ∪ ORGS ids (lab2, lab3)
#   action ∈ {menu, seal, unseal, cancel}
#   mode   ∈ {-, normal, premium, both}
#   target = a project id of that org, or "all", or "-"
# The PROJECT ID, not a list index: an index could point at a different project
# after a restart (seed edited, cache lost) and an old message's button would
# seal/unseal the wrong one. Every step carries the org, and the id must belong
# to it. Buttons from before the org step (4 fields) are rejected, never guessed.


# ── Project discovery ───────────────────────────────────────────────────────
# giaotien-project (created 2026-09-22) went a week outside every seal,
# quarantine and archive button because it wasn't in the hardcoded table. Each
# org's live project list is merged in at startup and hourly, and cached to disk
# (Org.cache_path) so a reboot with DNS down still knows every project.
OPENAI_PROJECTS_URL = "https://api.openai.com/v1/organization/projects"
PROJECT_SYNC_SECS   = 3600
_PROJECT_NAME_UNSAFE = re.compile(r"[<>&]")   # names land in HTML-mode Telegram messages


def _fetch_active_projects(org: Org) -> Optional[dict[str, str]]:
    """{project_id: name} for every active project of `org`; None on failure."""
    out: dict[str, str] = {}
    after = None
    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after
        try:
            r = _openai_call("get", OPENAI_PROJECTS_URL, headers=_openai_headers(org),
                             params=params, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            print(f"[projects] fetch error: {e}")
            return None
        if not r.ok:
            print(f"[projects {r.status_code}] {r.text[:200]}")
            return None
        data = r.json()
        for p in data.get("data", []):
            if p.get("id") and p.get("status", "active") == "active":
                out[p["id"]] = _PROJECT_NAME_UNSAFE.sub("", p.get("name") or "") or p["id"]
        if not data.get("has_more") or not data.get("last_id"):
            return out
        after = data["last_id"]


def _merge_projects(org: Org, found: dict[str, str]) -> list[str]:
    """Merge `found` into org.projects / org.project_index; return newly added ids.
    Both are REBOUND, never mutated in place: other threads iterate them, and an
    in-place insert would raise 'dictionary changed size during iteration' there.
    Append-only, so existing archive-button indices never shift."""
    foreign = [p for p in found if any(p in o.projects for o in ORGS.values() if o is not org)]
    if foreign:
        # Project ids are globally unique, so another org owning one means a
        # misconfigured key. Adopting it would let this org seal it.
        print(f"[projects] {len(foreign)} project(s) already belong to another org — ignored")
        found = {p: n for p, n in found.items() if p not in foreign}
    new     = [p for p in found if p not in org.projects]
    renamed = [p for p in found if p in org.projects and org.projects[p] != found[p]]
    if new or renamed:
        org.projects      = {**org.projects, **{p: found[p] for p in new + renamed}}
        org.project_index = org.project_index + [p for p in new if p not in org.project_index]
    return new


def _load_project_cache(org: Org) -> None:
    try:
        cached = json.loads(org.cache_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except Exception as e:
        print(f"[projects] {org.id} cache unreadable ({e}) — using the built-in seed")
        return
    if isinstance(cached, dict):
        _merge_projects(org, {str(k): _PROJECT_NAME_UNSAFE.sub("", str(v)) or str(k)
                              for k, v in cached.items()})


def _sync_projects(usage: "UsageStore", subs: "SubscriberStore" = None,
                   names: "NameStore" = None) -> Optional[list[str]]:
    """Pull `usage.org`'s live project list, merge it, persist its cache, and
    announce any new project. Returns the newly added ids, or None if the API
    call failed."""
    org   = usage.org
    found = _fetch_active_projects(org)
    if found is None:
        return None
    new = _merge_projects(org, found)
    try:
        _atomic_write_json(org.cache_path, org.projects)
    except Exception as e:
        print(f"[projects] cache write failed: {e}")
    if new:
        labels = [org.projects[p] for p in new]
        print(f"[projects] new project(s) detected: {', '.join(labels)}")
        _log_event("project_discovered", org=org.id, projects=labels)
        if subs is not None:
            _broadcast(lambda n, l=labels: fmt_new_projects(l, n), subs, names, org=org)
    return new


def fmt_new_projects(labels: list, name: str = "Bach") -> str:
    items = "\n".join(f"• <b>{l}</b>" for l in labels)
    return (
        f"🆕 <b>New project(s) detected</b>\n\n{items}\n\n"
        "Now covered by track seals, quarantine and the archive controls.\n"
        f"<i>Monarch {name}, the ledger has grown.</i>"
    )


def _archive_tracks_for_mode(mode: str) -> tuple:
    return ("normal", "premium") if mode == "both" else (mode,)


_ARCH_CANCEL = {"text": "✖ Cancel", "callback_data": "arch:-:cancel:-:-"}


def _kb_archive_root() -> list:
    return [[
        {"text": "🔒 Seal",   "callback_data": "arch:-:seal:-:-"},
        {"text": "🔓 Unseal", "callback_data": "arch:-:unseal:-:-"},
        _ARCH_CANCEL,
    ]]


def _kb_archive_orgs(action: str) -> list:
    """Org picker: one button per monitored org, then Cancel."""
    return [[{"text": f"🏢 {o.short}", "callback_data": f"arch:{o.id}:{action}:-:-"}
             for o in ORGS.values()], [_ARCH_CANCEL]]


def _kb_archive_mode(action: str, org: Org) -> list:
    return [
        [
            {"text": "📦 Normal",  "callback_data": f"arch:{org.id}:{action}:normal:-"},
            {"text": "⭐ Premium", "callback_data": f"arch:{org.id}:{action}:premium:-"},
        ],
        [
            {"text": "🔱 Both",    "callback_data": f"arch:{org.id}:{action}:both:-"},
            _ARCH_CANCEL,
        ],
    ]


def _kb_archive_projects(action: str, mode: str, usage: UsageStore) -> list:
    """One button per project of `usage.org`, annotated with its current state for
    this mode. Two columns. Trailing row: [ALL] [Cancel]."""
    org           = usage.org
    sealed_tracks = usage.get_sealed_tracks()
    exemptions    = usage.get_track_exemptions()
    tracks        = _archive_tracks_for_mode(mode)

    def _mark(pid: str) -> str:
        sealed = any(pid in sealed_tracks.get(t, {}).get("originals_by_project", {}) for t in tracks)
        exempt = any(t in exemptions.get(pid, []) for t in tracks)
        if action == "seal":
            return "🔒" if sealed else ("🔓" if exempt else "•")
        # Unseal view: highlight what's releasable. "Both" also lifts a
        # quarantine — the quarantine alert sends people here.
        if mode == "both" and pid in sealed_tracks.get(QUARANTINE_TRACK, {}).get("originals_by_project", {}):
            return "☣️"
        return "🔒" if sealed else "·"

    rows, row = [], []
    for pid in org.project_index:
        label = f"{_mark(pid)} {org.projects.get(pid, pid)}"
        row.append({"text": label, "callback_data": f"arch:{org.id}:{action}:{mode}:{pid}"})
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([
        {"text": f"🟥 ALL {org.short} projects", "callback_data": f"arch:{org.id}:{action}:{mode}:all"},
        _ARCH_CANCEL,
    ])
    return rows


def cmd_archive(usages: list, name: str = "Bach") -> tuple:
    """Entry point for the archive command. Returns (text, keyboard).
    The text is every org's live status; the keyboard offers Seal / Unseal / Cancel."""
    return _fmt_archive_all(usages, name), _kb_archive_root()


def _spawn_archive_worker(work_fn, after_kb_fn, chat_id: str, msg_id: int,
                          usage: UsageStore, name: str, placeholder: str,
                          prompt: str = "") -> None:
    """Run `work_fn` in a daemon thread, then edit the originating message with
    the fresh archive status and the keyboard returned by `after_kb_fn()`. The
    caller MUST already hold `usage.org`'s busy claim — this worker releases it.
    Backgrounding lets the Telegram poll loop keep handling other commands while
    a 30–50 s mass-seal runs. The worker posts the "Working…" placeholder itself:
    when the Telegram thread posted it, a fast no-op job's final edit could land
    first and be overwritten, leaving the message stuck with no buttons."""
    def _runner():
        with _org_context(usage.org):
            try:
                if msg_id is not None:
                    _edit_message(placeholder, chat_id, msg_id, keyboard=[])
                work_fn()
                if msg_id is not None:
                    text = _fmt_archive_status(usage, name) + (f"\n\n{prompt}" if prompt else "")
                    _edit_message(text, chat_id, msg_id, keyboard=after_kb_fn())
            except Exception as e:
                print(f"[archive worker error] {e}")
            finally:
                _release_busy(usage.org)
    threading.Thread(target=_runner, daemon=True).start()


def handle_archive_callback(data: str, usages: dict, subs: SubscriberStore,
                            names: NameStore, name: str, chat_id: str,
                            msg_id: int) -> tuple:
    """Process an 'arch:...' callback. Returns (text, keyboard, toast).
    `usages` maps org id -> that org's UsageStore; the org in the callback data
    selects the store, so a project index only ever resolves against its own
    org's list. Navigation paths return immediately. Heavy seal/unseal work is
    dispatched to a background thread so the Telegram poll loop is never
    blocked; the worker posts the "Working…" placeholder, then the result."""
    parts = data.split(":")
    if len(parts) != 5:
        # Pre-org buttons ("arch:seal:normal:3") — guessing their org could act
        # on the wrong project, so they are refused.
        return None, None, "This menu is outdated — send /archive again."
    _, oid, action, mode, pidx = parts

    # Defensive: validate enum-like fields before using them.
    if action not in {"cancel", "menu", "seal", "unseal"}:
        return None, None, "Unknown action."
    if mode not in {"-", "normal", "premium", "both"}:
        return None, None, "Unknown mode."
    if oid != "-" and oid not in usages:
        return None, None, "Unknown org."

    all_usages = list(usages.values())
    if action == "cancel":
        return _fmt_archive_all(all_usages, name), None, "Cancelled."

    if action == "menu":
        return _fmt_archive_all(all_usages, name), _kb_archive_root(), None

    verb = "seal" if action == "seal" else "unseal"
    if oid == "-":
        if len(usages) > 1:
            # Action chosen → ask for the org.
            return (f"{_fmt_archive_all(all_usages, name)}\n\n"
                    f"<b>Choose an org to {verb}:</b>",
                    _kb_archive_orgs(action), None)
        oid = next(iter(usages))          # single org: skip the org step
    usage = usages[oid]
    org   = usage.org

    if mode == "-":
        # Org chosen → ask for the mode (track scope).
        return (f"{_fmt_archive_status(usage, name)}\n\n"
                f"<b>{org.short} — choose a track to {verb}:</b>",
                _kb_archive_mode(action, org), None)

    if pidx == "-":
        # Mode chosen → show that org's project picker.
        return (f"{_fmt_archive_status(usage, name)}\n\n"
                f"<b>{verb.title()} — {org.short} — {mode} — pick a project:</b>",
                _kb_archive_projects(action, mode, usage), None)

    # Project (or ALL) chosen → apply. Everything that can fail (validation,
    # the placeholder render) happens BEFORE the busy claim, and a failed
    # worker start releases it: a leaked claim would defer every seal, repair,
    # quarantine and restore on this org until restart.
    tracks = _archive_tracks_for_mode(mode)
    if pidx == "all":
        def _work():
            snap = usage.get()
            for t in tracks:
                if action == "seal":
                    _mass_seal_track(t, usage, subs, names, respect_exemptions=False, manual=True,
                                     consumed=snap.get(f"total_{t}_tokens", 0))
                else:
                    _mass_unseal_track(t, usage, subs, names, reason="manual all")
            if action == "unseal" and mode == "both":
                # "Both" is the full-release gesture — lift quarantines too.
                _mass_unseal_track(QUARANTINE_TRACK, usage, subs, names,
                                   reason="manual all (quarantine)")
        placeholder = (f"{_fmt_archive_status(usage, name)}\n\n"
                       f"🔄 <i>Working on {action} ALL {org.short} ({mode}) — "
                       f"watch chat for progress…</i>")
        after_kb, prompt = _kb_archive_root, ""
        toast = f"Started {action} ALL {org.short} ({mode})."
    else:
        # Single project — the id must be one of THIS org's projects.
        pid = pidx
        if pid not in org.projects:
            return None, None, "Unknown project."
        proj = org.projects[pid]
        def _work():
            for t in tracks:
                if action == "seal":
                    _manual_seal_project(t, pid, usage, subs, names)
                else:
                    _manual_unseal_project(t, pid, usage, subs, names)
            if action == "unseal" and mode == "both":
                # Full-release gesture: also lift this project's quarantine
                # (restores off-watchlist rows + exempts from re-quarantine today).
                _release_quarantine(pid, usage, subs, names)
        placeholder = (f"{_fmt_archive_status(usage, name)}\n\n"
                       f"🔄 <i>Working on {action} {proj} ({org.short}, {mode})…</i>")
        after_kb = lambda: _kb_archive_projects(action, mode, usage)
        # The picker stays up afterwards — keep saying what its buttons do.
        prompt = f"<b>{verb.title()} — {org.short} — {mode} — pick a project:</b>"
        toast = f"Started {action} {proj} ({org.short}, {mode})."

    if not _try_claim_busy(org):
        return None, None, f"A seal/unseal is already running on {org.short} — try again shortly."
    try:
        _spawn_archive_worker(_work, after_kb, chat_id, msg_id, usage, name, placeholder, prompt)
    except Exception:
        _release_busy(org)
        raise
    return None, None, toast


def _fmt_archive_status(usage: UsageStore, name: str = "Bach", sign_off: bool = True) -> str:
    org            = usage.org
    snap           = usage.get()
    sealed_tracks  = usage.get_sealed_tracks()
    exemptions     = usage.get_track_exemptions()
    pending_tracks = usage.get_pending_track_unseal()

    n_tok = snap.get("total_normal_tokens",  0)
    p_tok = snap.get("total_premium_tokens", 0)
    lane_costs = snap.get("lane_costs") or {}

    title = f"{org.label} — " if len(ORGS) > 1 else ""
    lines = [f"🗃️ <b>Archive — {title}{snap.get('date', today_str())}</b>\n"]

    lines.append("<b>Tracks</b>")
    for track, consumed in (("normal", n_tok), ("premium", p_tok)):
        cap, threshold = org.cap(track), org.threshold(track)
        pct = consumed / cap * 100 if cap else 0
        n_sealed = len(sealed_tracks.get(track, {}).get("originals_by_project", {}))
        if n_sealed:
            tag = f"🔒 {n_sealed} sealed"
        elif consumed >= threshold:
            tag = f"⚠️ past seal point {_fmt_tokens(threshold)} (not sealed)"
        elif org.pays_past_free(track):
            tag = f"✅ active · seals at {_fmt_tokens(threshold)}"
        else:
            tag = "✅ active"
        lines.append(f"  • <b>{track}</b>: {_fmt_tokens(consumed)} / {_fmt_cap(cap)} "
                     f"({pct:.1f}%). Cost: {_fmt_cost(lane_costs.get(track))}  —  {tag}")
    lines.append(f"  • <b>exotic</b>: {_fmt_cost(lane_costs.get('exotic'))} "
                 f"(off-watchlist spend — no free allowance)")
    lines.append("")

    lines.append("<b>Projects</b>")
    for pid, proj_name in sorted(org.projects.items(), key=lambda kv: kv[1]):
        tags = []
        if pid in sealed_tracks.get(QUARANTINE_TRACK, {}).get("originals_by_project", {}):
            tags.append("☣️")                            # quarantined (off-watchlist use)
        for t in ("normal", "premium"):
            if pid in sealed_tracks.get(t, {}).get("originals_by_project", {}):
                tags.append(f"🔒{t[0].upper()}")        # 🔒N / 🔒P
        for t in exemptions.get(pid, []):
            tags.append(f"🔓{t[0].upper()}")
        for t in pending_tracks:
            if pid in pending_tracks[t].get("originals_by_project", {}):
                tags.append(f"⏳{t[0].upper()}")
        status = " ".join(tags) if tags else "✅"
        lines.append(f"  • <b>{proj_name}</b> — {status}")
    if sign_off:
        lines.append("")
        lines.extend(_archive_legend(name))
    return "\n".join(lines)


def _archive_legend(name: str) -> list:
    return ["<i>🔒=sealed ☣️=quarantined 🔓=exempt ⏳=restore-pending · "
            "N=normal P=premium F=quarantine</i>",
            f"<i>Monarch {name}, the archive registry is presented.</i>"]


def _fmt_archive_all(usages: list, name: str = "Bach") -> str:
    """Every org's archive status, legend once at the end."""
    parts = [_fmt_archive_status(u, name, sign_off=False) for u in usages]
    return "\n\n".join(parts) + "\n\n" + "\n".join(_archive_legend(name))


def _refresh_section(usage: UsageStore) -> str:
    """One org's block of the /refresh reply (fresh fetch, cached on failure)."""
    org = usage.org
    with _org_context(org):
        snap = fetch_today_usage(org)
        if snap:
            enriched   = _enrich_costs(snap, usage)
            total      = enriched.get("total_cost", 0.0)
            total_tok  = sum(p.get("total_tokens", 0) for p in snap.get("projects", {}).values())
            stale_note = (f"  <i>(cost as of {_fmt_ts(enriched.get('costs_ts'))})</i>"
                          if enriched.get("costs_stale") else "")
            return "\n".join([
                f"{_org_header(org)}Tokens today: <b>{_fmt_tokens(total_tok)}</b>",
                *_fmt_lane_lines(snap.get("total_premium_tokens", 0),
                                 snap.get("total_normal_tokens", 0), enriched.get("lane_costs"), org),
                f"Spend today:  <b>${total:.4f}</b>{stale_note}",
                *_fmt_guard_lines(usage),
            ])
        cached = usage.get()
        if cached and cached.get("projects"):
            enriched = _enrich_costs(cached, usage, live=False)
            return (f"{_org_header(org)}⚠️ <b>OpenAI API error ({html.escape(org.last_api_error or 'unknown')}) "
                    f"— showing last known data.</b>\n\n"
                    + fmt_daily_snapshot(enriched, org))
        return (f"{_org_header(org)}⚠️ <b>OpenAI API error ({html.escape(org.last_api_error or 'unknown')}) "
                f"and no prior data — this org is not being guarded.</b>")


def _fmt_guard_lines(usage: UsageStore) -> list:
    """What the guard is doing right now: seals, quarantines and pending
    restores (one line, only when there are any), then the projects active in
    the concurrency window (the old `active` report, no API call)."""
    sealed  = usage.get_sealed_tracks()
    pending = usage.get_pending_track_unseal()
    names   = usage.org.projects
    bits = []
    for t in ("normal", "premium"):
        n = len(sealed.get(t, {}).get("originals_by_project", {}))
        if n:
            bits.append(f"🔒 {t} ×{n}")
    q = sealed.get(QUARANTINE_TRACK, {}).get("originals_by_project", {})
    if q:
        bits.append("☣️ " + ", ".join(names.get(pid, pid) for pid in q))
    n_pending = len({pid for info in pending.values() for pid in info.get("originals_by_project", {})})
    if n_pending:
        bits.append(f"⏳ {n_pending} restore(s) pending")
    lines = [f"Guard: {' · '.join(bits)}"] if bits else []
    active = usage.get_active_projects()
    window = usage.get_active_window_mins()
    if active:
        top = sorted(active.items(), key=lambda x: x[1], reverse=True)
        lines.append(f"⚡ Active (last {window} min): "
                     + ", ".join(f"{names.get(pid, pid)} ({n:,})" for pid, n in top))
    else:
        lines.append(f"⚡ Active (last {window} min): none")
    return lines


def cmd_refresh(usages: list, subs: SubscriberStore, names: NameStore = None,
                name: str = "Bach") -> str:
    """Fresh numbers for every org, plus an immediate full poll cycle in each.

    Every alert, seal, quarantine and mode decision lives in usage_poll_loop
    alone; /refresh only reads and wakes the loops (one per org). It used to
    re-run that pipeline on the Telegram thread and had drifted: a stale "85%"
    premium label (seals at 80%), a quarantine worker grabbing the busy claim so
    the due track seal silently deferred, zero-cost snapshots written to the
    store, mode changes the poll loop never saw, and a blocking activity call on
    the Telegram thread."""
    for u in usages:
        u.org.poll_now.set()
    sections = _map_orgs(usages, _refresh_section)
    return "\n".join([
        "🔄 <b>Data refreshed.</b>\n",
        "\n\n".join(sections),
        "",
        "<i>A full check is running now — any alert or seal follows in chat.</i>",
        f"<i>Intelligence updated, Monarch {name}.</i>",
    ])


def cmd_arise(chat_id: str, subs: SubscriberStore, name: str = "Bach", thread_id: int = None) -> str:
    gif = GIF_DIR / "beru-v-jinwoo.gif"
    if gif.exists():
        _send_animation(gif, chat_id, thread_id)
    added = subs.add(chat_id)
    if added:
        return (
            "⚔️ <b>I rise.</b>\n\n"
            f"{name} the Monarch — your Shadow Commander stands before you.\n\n"
            "From this moment, every token your organization consumes is my intelligence. "
            "Every dollar your accounts spend is my surveillance. "
            "Every project that stirs is my vigil.\n\n"
            "I do not rest. I do not waver. I do not question.\n"
            "Where you point, I watch. What matters to you, I report.\n\n"
            "This channel is now bound to my oath. "
            "All dispatches will arrive without delay.\n\n"
            "<i>For the Monarch. For the organization. Unto the last token.</i>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Shadow Commander, standing by.</b>"
        )
    return f"This channel already receives dispatches, Monarch {name}."


def cmd_dismiss(chat_id: str, subs: SubscriberStore, name: str = "Bach") -> str:
    if chat_id == subs.primary:
        return (
            f"The primary command channel cannot be removed, Monarch {name}.\n"
            "<i>I remain bound to my post.</i>"
        )
    removed = subs.remove(chat_id)
    if removed:
        return f"This channel has been removed from dispatches.\n<i>Order carried out, My Liege {name}.</i>"
    return f"This channel was not receiving dispatches, Monarch {name}."


def cmd_setname(chat_id: str, new_name: str, names: NameStore) -> str:
    if not new_name.strip():
        return "Provide a name. Usage: <code>setname YourName</code>"
    names.set(chat_id, new_name.strip())
    return f"Acknowledged. I will address you as <b>{html.escape(new_name.strip())}</b>."


def cmd_help(bot_username: Optional[str], name: str = "Bach") -> str:
    m = f"@{bot_username}" if bot_username else "@bot"
    return (
        "📋 <b>Shadow Ledger — Commands</b>\n"
        f"Tap a button below, tap a /command, or type <code>{m} refresh</code>.\n"
        + (f"Orgs: {' · '.join(o.label for o in ORGS.values())} — every report covers "
           f"all of them; archive asks which org.\n" if len(ORGS) > 1 else "")
        + "\n"
        "/refresh — Today per org: tokens, spend, seals, active projects; runs a full check now\n"
        "/usage — Per-project tokens, requests, spend and models today\n"
        "/spending — This month and last month per project, plus 31-day totals\n"
        "/archive — Seal or unseal projects (buttons)\n\n"
        "<b>Chat setup</b>: /arise subscribe · /dismiss unsubscribe · "
        "<code>/setname Name</code> how I address you\n"
        "<i>Old names still work: tokens, projects, rank, models → usage · recent → spending · "
        "active → refresh.</i>\n\n"
        f"<b>Latest update — {BOT_UPDATED}</b>\n"
        + "".join(f"• {c}\n" for c in BOT_CHANGES)
        + f"\n<i>Your Majesty {name}, your command is my directive.</i>"
    )


# ── Command dispatch ───────────────────────────────────────────────────────

# The command set. Old names stay as silent aliases, so habits and pinned
# messages keep working after the 2026-09-30 merge (13 commands → 5 + setup).
COMMANDS = ("refresh", "usage", "spending", "archive", "help", "arise", "dismiss", "setname")
COMMAND_ALIASES = {
    "tokens": "usage", "projects": "usage", "rank": "usage", "models": "usage",
    "recent": "spending", "bill": "spending",
    "active": "refresh", "status": "refresh",
    "start": "help", "menu": "help",
}
# Telegram's "/" menu (setMyCommands) and the tap menu under help / refresh.
MENU_COMMANDS = (
    ("refresh",  "🔄 Refresh",  "Today per org + run a full check now"),
    ("usage",    "📊 Usage",    "Per-project tokens, spend and models today"),
    ("spending", "💰 Spending", "This month, last month, last 31 days"),
    ("archive",  "🗃️ Archive",  "Seal or unseal projects"),
)


def _canonical(cmd: str) -> Optional[str]:
    cmd = cmd.lower()
    cmd = COMMAND_ALIASES.get(cmd, cmd)
    return cmd if cmd in COMMANDS else None


def _kb_menu() -> list:
    """Tap-to-run buttons for the main commands, two per row."""
    btns = [{"text": label, "callback_data": f"cmd:{cmd}"} for cmd, label, _ in MENU_COMMANDS]
    return [btns[i:i + 2] for i in range(0, len(btns), 2)]


def _match_prefix(text: str, bot_username: Optional[str]) -> Optional[str]:
    """The command part of a message addressed to this bot, else None:
    "@Bot cmd args", "/cmd args" or "/cmd@Bot args" → "cmd args". A bare
    "/cmd" for a command we don't have is ignored (it may be another bot's),
    and "@Botx…" (a longer username) is not us."""
    if not bot_username:
        return None
    s  = text.strip()
    me = bot_username.lower()
    if s.lower().startswith(f"@{me}"):
        rest = s[len(me) + 1:]
        if rest and not rest[0].isspace():
            return None
        return rest.strip()
    if s.startswith("/") and s[1:2].strip():
        head, *tail = s[1:].split(None, 1)
        cmd, _, target = head.partition("@")
        tail = tail[0] if tail else ""
        if not cmd or (target and target.lower() != me):
            return None
        if not target and _canonical(cmd) is None:
            return None
        return f"{cmd} {tail}".strip()
    return None


def _api_warning(usage: UsageStore) -> str:
    """Warning line for an org whose data can't be trusted: the API is failing,
    or no poll has succeeded yet — otherwise its report would read as a quiet,
    healthy org."""
    org = usage.org
    if org.api_alerted:
        return (f"⚠️ <b>API failing ({html.escape(org.last_api_error or 'unknown')}) — "
                f"this org is NOT being guarded; data below is stale.</b>\n")
    if not usage.get().get("last_polled"):
        why = f" (last error: {html.escape(org.last_api_error)})" if org.last_api_error else ""
        return f"⚠️ <b>No successful poll yet for this org{why}.</b>\n"
    return ""


def _per_org(usages: list, render) -> str:
    """Run a single-org report for every org, one labelled section each —
    rendered in parallel, so one org's slow API can't double the Telegram
    thread's wait."""
    def _one(u):
        return _org_header(u.org) + _api_warning(u) + render(u)
    return "\n\n".join(_map_orgs(usages, _one))


def _map_orgs(usages: list, fn) -> list:
    """fn(usage) for every org concurrently (each in its org context), results
    in display order."""
    if len(usages) == 1:
        return [_in_org(usages[0].org, fn, usages[0])]
    with ThreadPoolExecutor(max_workers=len(usages)) as ex:
        return list(ex.map(lambda u: _in_org(u.org, fn, u), usages))


def dispatch(text: str, usages: list, subs: SubscriberStore,
             bot_username: Optional[str], chat_id: str, names: NameStore = None,
             thread_id: int = None) -> tuple:
    """Returns (reply_text_or_None, keyboard_or_None). `usages` = one UsageStore
    per org, in display order; every report covers all of them."""
    rest = _match_prefix(text, bot_username)
    if rest is None:
        return None, None

    parts = rest.split()
    typed = parts[0].lower() if parts else "help"
    cmd   = _canonical(typed)

    name = names.get(chat_id) if names else "Bach"

    if cmd == "setname":
        new_name = " ".join(parts[1:]) if len(parts) > 1 else ""
        return (cmd_setname(chat_id, new_name, names) if names else "Name store unavailable."), None

    if cmd == "archive":
        return cmd_archive(usages, name)   # (text, keyboard)

    routes = {
        "refresh":  lambda: (cmd_refresh(usages, subs, names, name), _kb_menu()),
        "usage":    lambda: (_per_org(usages, lambda u: cmd_usage(u, name)), None),
        "spending": lambda: (_per_org(usages, lambda u: cmd_spending(u, name)), None),
        "help":     lambda: (cmd_help(bot_username, name), _kb_menu()),
        "arise":    lambda: (cmd_arise(chat_id, subs, name, thread_id), None),
        "dismiss":  lambda: (cmd_dismiss(chat_id, subs, name), None),
    }
    handler = routes.get(cmd)
    if handler:
        return handler()

    mention = f"@{bot_username}" if bot_username else "@bot"
    return (f"Unknown command: <code>{html.escape(typed)}</code>. "
            f"Use /help or <code>{mention} help</code>."), None


# ── Telegram poll thread ───────────────────────────────────────────────────

# Subscribers get full seal/unseal control, and `arise` used to subscribe ANY
# chat — a stranger DMing the bot could unseal every project. Only the primary
# chat, or chats listed in TELEGRAM_ALLOWED_CHAT_IDS (comma-separated), may
# self-subscribe. Chats already in subscribers.json are unaffected.
ALLOWED_CHAT_IDS = {c.strip() for c in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
                    if c.strip()}


def _may_subscribe(chat_id: str) -> bool:
    return chat_id == str(CHAT_ID) or chat_id in ALLOWED_CHAT_IDS


def _command_allowed(chat_id: str, cmd: str, subs: "SubscriberStore") -> bool:
    """Subscribed chats may run anything; others only `arise`, and only if
    allowlisted. Refusals are logged, never broadcast (a stranger could spam)."""
    if chat_id in subs.all():
        return True
    if cmd != "arise":
        return False
    if _may_subscribe(chat_id):
        return True
    print(f"[security] arise refused for non-allowlisted chat {chat_id}")
    _log_event("arise_refused", chat=chat_id)
    return False


def _register_commands() -> None:
    """Publish the main commands to Telegram's "/" menu. Best effort: a failure
    only loses the menu, never a command."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands"
    cmds = [{"command": c, "description": d} for c, _, d in MENU_COMMANDS]
    cmds.append({"command": "help", "description": "All commands and the tap menu"})
    try:
        r = _telegram_call("post", url, data={"commands": json.dumps(cmds)}, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            print(f"[telegram setMyCommands {r.status_code}] {r.text[:200]}")
    except Exception as e:
        print(f"[telegram setMyCommands error] {e}")


def _telegram_startup() -> tuple[str, int]:
    """Resolve the bot's @username and discard updates queued while offline,
    retrying each until it succeeds. A one-shot failure used to leave commands
    dead for the whole run (no username) or replay up to 24 h of stale archive
    clicks (offset 0) — both likely right after a reboot with DNS still down."""
    delay, bot_username = 5, None
    while not bot_username:
        bot_username = _fetch_bot_username(retries=1)
        if not bot_username:
            print(f"[bot] getMe failed — retrying in {delay} s (commands offline until then)")
            time.sleep(delay)
            delay = min(delay * 2, 120)
    print(f"[bot] @{bot_username} ready")
    _register_commands()
    delay, offset = 5, None
    while offset is None:
        offset = _discard_pending_updates()
        if offset is None:
            time.sleep(delay)
            delay = min(delay * 2, 120)
    return bot_username, offset


def telegram_poll_loop(usages: list, subs: SubscriberStore,
                       names: NameStore = None) -> None:
    """One Telegram poller for every org (a bot token allows only one).
    `usages` = one UsageStore per org, in display order."""
    by_org = {u.org.id: u for u in usages}
    bot_username, offset = _telegram_startup()
    while True:
        updates = _get_updates(offset)
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                if upd.get("callback_query"):
                    _handle_callback_update(upd["callback_query"], by_org, subs, names, bot_username)
                    continue
                msg = (upd.get("message") or upd.get("edited_message")
                       or upd.get("channel_post") or upd.get("edited_channel_post"))
                if not msg:
                    continue
                chat_id   = str(msg.get("chat", {}).get("id", ""))
                thread_id = msg.get("message_thread_id")
                text      = msg.get("text", "") or msg.get("caption", "")
                print(f"[update] chat={chat_id} thread={thread_id} text={text[:60]!r}")
                rest = _match_prefix(text, bot_username)
                if rest is None:
                    continue
                cmd = rest.split()[0].lower() if rest.split() else "help"
                if not _command_allowed(chat_id, cmd, subs):
                    continue
                _log_event("command", chat=chat_id, cmd=rest[:120])
                reply, keyboard = dispatch(text, usages, subs, bot_username, chat_id, names, thread_id)
                if reply:
                    _send(reply, chat_id, thread_id, keyboard=keyboard)
            except Exception as e:
                print(f"[telegram handler error] {e}")


def _handle_callback_update(cq: dict, usages: dict, subs: SubscriberStore,
                            names: NameStore, bot_username: Optional[str] = None) -> None:
    """Process a callback_query (inline button press) from a subscribed chat:
    'cmd:<name>' runs a menu command and posts its reply as a new message;
    'arch:…' drives the archive flow. Anything else is acknowledged and ignored."""
    cq_id   = cq.get("id", "")
    data    = cq.get("data", "") or ""
    msg     = cq.get("message", {}) or {}
    chat_id = str(msg.get("chat", {}).get("id", ""))
    msg_id  = msg.get("message_id")
    name    = names.get(chat_id) if names else "Bach"

    if chat_id not in subs.all():
        _answer_callback(cq_id, "This channel isn't subscribed.")
        return
    if data.startswith("cmd:"):
        cmd = data[4:]
        if cmd not in {c for c, _, _ in MENU_COMMANDS}:
            _answer_callback(cq_id)
            return
        _answer_callback(cq_id)              # stop the spinner before the (slow) report
        _log_event("command", chat=chat_id, cmd=f"button:{cmd}")
        thread_id = msg.get("message_thread_id")
        reply, keyboard = dispatch(f"/{cmd}", list(usages.values()), subs, bot_username,
                                   chat_id, names, thread_id)
        if reply:
            _send(reply, chat_id, thread_id, keyboard=keyboard)
        return
    if not data.startswith("arch:"):
        _answer_callback(cq_id)
        return

    print(f"[callback] chat={chat_id} data={data!r}")
    _log_event("command", chat=chat_id, cmd=f"callback:{data[:100]}")
    try:
        text, keyboard, toast = handle_archive_callback(
            data, usages, subs, names, name, chat_id, msg_id)
    except Exception as e:
        print(f"[callback handler error] {e}")
        _answer_callback(cq_id, "Error — check logs.")
        return

    _answer_callback(cq_id, toast)
    if text is not None and msg_id is not None:
        _edit_message(text, chat_id, msg_id, keyboard=keyboard)


# ── Usage poll thread ──────────────────────────────────────────────────────

# An org whose API calls keep failing is UNGUARDED — no seals, quarantine or
# alerts — while its reports would otherwise just look idle. A mis-scoped new
# admin key would do exactly that, silently. Say so in the chat: at once for a
# rejected key (401/403, a config error), after API_ALERT_AFTER_FAILS
# consecutive failed polls otherwise (~15 min with backoff — DNS blips on this
# host clear well within that), and again when the API recovers.
API_ALERT_AFTER_FAILS = 5


def _note_api_failure(org: Org, fail_count: int, subs, names) -> None:
    if fail_count == 1:
        org.api_down_since = time.time()
    rejected = (org.last_api_error or "") in ("HTTP 401", "HTTP 403")
    if org.api_alerted or not (rejected or fail_count >= API_ALERT_AFTER_FAILS):
        return
    org.api_alerted = True
    _log_event("api_down", org=org.id, error=org.last_api_error, consecutive=fail_count)
    _broadcast(lambda n, e=org.last_api_error, c=fail_count, t=org.api_down_since:
               fmt_api_down(e, c, t, n), subs, names, org=org)


def _note_api_recovered(org: Org, fail_count: int, subs, names) -> None:
    if not org.api_alerted:
        return
    mins = int((time.time() - (org.api_down_since or time.time())) / 60)
    org.api_alerted, org.api_down_since = False, None
    _log_event("api_recovered", org=org.id, down_mins=mins)
    _broadcast(lambda n, m=mins: fmt_api_recovered(m, n), subs, names, org=org)


def fmt_api_down(error: Optional[str], fails: int, since: Optional[float], name: str = "Bach") -> str:
    hint = " — admin key rejected? Check this org's key in .env" if error in ("HTTP 401", "HTTP 403") else ""
    return (
        "🚫 <b>OpenAI API failing — this org is NOT being guarded</b>\n\n"
        f"Last error: <b>{html.escape(error or 'unknown')}</b>{hint}\n"
        f"Failing since {_fmt_ts(since)} ({fails} consecutive poll(s)).\n"
        "Seals, quarantine and alerts for this org are paused until the API answers.\n"
        f"<i>Monarch {name}, this flank is unwatched.</i>"
    )


def fmt_api_recovered(mins: int, name: str = "Bach") -> str:
    return (f"✅ <b>OpenAI API reachable again</b> — guarding resumed "
            f"(down ~{mins} min).\n<i>The watch is restored, Monarch {name}.</i>")


def _overlay_cached_costs(snap: dict, usage: UsageStore) -> None:
    """Put the last known cost picture on a fresh token snapshot, so the store
    never saves $0 while the live costs fetch is pending or failing. Same-day
    only: yesterday's cache on the first poll of a new day seeded that day's
    spend thresholds as 'already notified' up to yesterday's total, silencing
    today's real crossings."""
    cached = usage.get_costs_cache()
    if not cached or cached.get("date") != snap.get("date") or usage.get().get("date") != snap.get("date"):
        return
    per_proj = cached.get("per_project", {})
    for pid, p in snap.get("projects", {}).items():
        p["cost_usd"] = round(per_proj.get(pid, 0.0), 6)
    snap["total_cost"] = cached.get("total", 0.0)
    snap["lane_costs"] = dict(cached.get("per_lane", {}))


def _apply_live_costs(snap: dict, usage: UsageStore, breakdown: tuple) -> None:
    """Overlay a successful costs fetch (may be empty = real $0) and persist it.
    {} means no spend today — never fall back to the cache for that, or the
    day's spend could never read zero after a rollover."""
    costs, lane_costs = breakdown
    costs    = dict(costs)
    org_cost = costs.pop("__org__", 0.0)
    for pid, p in snap.get("projects", {}).items():
        p["cost_usd"] = round(costs.get(pid, 0.0), 6)
    snap["total_cost"] = round(sum(costs.values()) + org_cost, 6)
    snap["lane_costs"] = {l: round(v, 6) for l, v in lane_costs.items()}
    usage.update(snap)
    usage.update_costs(costs, snap["total_cost"], org_cost, lane_costs, date=snap.get("date"))


def usage_poll_loop(usage: UsageStore, subs: SubscriberStore, names: NameStore = None) -> None:
    """The ONE enforcement pipeline (/refresh just wakes it). Per cycle, in order:
    tokens → store update / day rollover → SEAL DECISIONS → midnight restores →
    live costs → milestone / spend / unlisted alerts + quarantine → seal repair →
    overcap & mode → hourly project sync. Seals come right after the token fetch:
    costs, broadcasts and the inline quarantine sweep (1–2 min per offender) used
    to run first, delaying the seal while the wave kept coming.

    One thread per org: everything below uses `usage.org` (key, caps, seal
    points, projects, wake-up event), and the thread's context tags its output."""
    org = usage.org
    _CTX.org = org      # this thread serves exactly one org
    fail_count  = 0
    last_logged = None   # (date, normal, premium, cost) of last intel-logged poll
    wave_guard  = _WaveGuard()   # burst-resistant predictive seal trigger
    watch_zone  = False  # True while an unsealed track is near its seal threshold
    next_project_sync = 0.0   # the first cycle syncs the live project list
    next_unknown_sync = 0.0   # early sync when usage comes from an unknown project

    while True:
        try:
            snap = fetch_today_usage(org)
            if snap is not None:
                _note_api_recovered(org, fail_count, subs, names)
                _overlay_cached_costs(snap, usage)
                # Auto-resets daily state on rollover. False = snapshot older than
                # the store's day, or rollover deferred while a seal op finishes —
                # either way, acting on it would apply the wrong day's numbers.
                if not usage.update(snap):
                    org.poll_now.wait(15)
                    org.poll_now.clear()
                    continue

                # Usage from a project the table doesn't know yet (Lab 3's first
                # run with only the seed; a project created mid-day): sync NOW,
                # before the seal decisions — seal, repair and quarantine only act
                # on known projects, and the regular sync runs at the cycle's end.
                unknown = [p for p in snap.get("projects", {}) if p not in org.projects]
                if unknown and time.time() >= next_unknown_sync:
                    print(f"[projects] usage from {len(unknown)} unknown project(s) — syncing before seal decisions")
                    next_unknown_sync = time.time() + 300
                    if _sync_projects(usage, subs, names) is not None:
                        next_project_sync = time.time() + PROJECT_SYNC_SECS

                normal_tok  = snap.get("total_normal_tokens",  0)
                premium_tok = snap.get("total_premium_tokens", 0)
                # Enforcement view: each track's CEILING (= free allowance unless
                # the org deliberately pays past it) and seal point.
                track_view = (
                    ("normal",  normal_tok,  org.normal_ceiling,  org.normal_threshold),
                    ("premium", premium_tok, org.premium_ceiling, org.premium_threshold),
                )

                # ── Seal decisions: wave guard (predictive) + static threshold ──
                # Static thresholds react to numbers already 5–15 min stale; the
                # guard projects each track forward on a WINDOWED burn rate (all
                # gating lives in _WaveGuard). Idempotent via the per-day
                # mass_sealed flag.
                for track, tok, cap, thr in track_view:
                    verdict = wave_guard.observe(snap.get("date"), track, tok, cap,
                                                 thr, usage.is_mass_sealed(track))
                    if verdict:
                        print(f"[wave] {track}: {tok:,} at ~{verdict['rate']:.0f} tok/s "
                              f"(windowed, confirmed ×{WAVE_CONFIRM_POLLS}) → projected "
                              f"{verdict['projected']:,} ≥ cap — sealing early")
                        _log_event("wave_trigger", track=track, tokens=tok,
                                   rate_per_sec=round(verdict["rate"], 2),
                                   projected=verdict["projected"])
                    if (verdict or tok >= thr) and not usage.is_mass_sealed(track):
                        _handle_track_seal(track, snap, usage, subs, names)

                # Yesterday's seals, queued by the rollover. Safe after today's
                # seal decisions: rows a today-seal covers are held, not reopened.
                _process_pending_track_unseals(usage, subs, names)

                # ── Live costs (off the seal critical path) ──
                breakdown = _fetch_costs_breakdown(org)
                if breakdown is not None:
                    _apply_live_costs(snap, usage, breakdown)

                n_str    = _color(f"{_fmt_tokens(normal_tok)}/{_fmt_cap(org.normal_cap)}",
                                  _tok_color(normal_tok, org.normal_cap))
                p_str    = _color(f"{_fmt_tokens(premium_tok)}/{_fmt_cap(org.premium_cap)}",
                                  _tok_color(premium_tok, org.premium_cap))
                cost_str = f"  cost=${snap.get('total_cost', 0.0):.4f}" if snap.get("total_cost") else ""
                exotic = (snap.get("lane_costs") or {}).get("exotic") or 0.0
                if exotic:
                    cost_str += f"  exotic=${exotic:.4f}"
                print(f"[poll/{usage.get_mode()}] {snap.get('date')}  normal={n_str}  premium={p_str}{cost_str}")

                # Intel log: one entry per poll where the totals actually moved.
                cur = (snap.get("date"), normal_tok, premium_tok,
                       round(snap.get("total_cost", 0.0), 4))
                if cur != last_logged:
                    _log_event("poll", lanes=snap.get("lane_costs"),
                               date=cur[0], normal=cur[1], premium=cur[2],
                               cost=cur[3], mode=usage.get_mode())
                    last_logged = cur

                # ── Milestone handling ─────────────────────────────────────
                if not usage.has_seeded():
                    seed_milestones(snap, usage, subs, names)
                    print("[poll] Day seeded — milestone status notified")
                else:
                    new_ms = check_milestones(snap, usage, subs, names)
                    mode   = usage.get_mode()
                    if new_ms and mode != "aggressive":
                        if mode == "urgent":
                            usage.reset_urgent_step()   # new milestone → restart from 3 min
                        else:
                            usage.set_mode("urgent")
                            print("[mode] → URGENT (milestone crossed)")
                        usage.set_last_milestone_ts(time.time())

                # ── Spend monitoring (org + per-project + unlisted-model anomaly) ──
                # Independent of token-cap path. Catches embedding/image/audio/
                # fine-tune spend that the token watchlist misses.
                if not usage.has_spend_seeded():
                    seed_spend(snap, usage, subs, names)
                    print("[poll] Spend seeded — already-crossed thresholds notified silently")
                    new_spend, spend_cap_crossed = False, False
                else:
                    new_spend, spend_cap_crossed = check_spend(snap, usage, subs, names)
                check_unlisted_models(snap, usage, subs, names)
                _quarantine_unlisted_users(snap, usage, subs, names)

                # Watch zone: an unsealed track close to its threshold forces
                # 60 s polling below (matters when POLL_INTERVAL_MINS > 1).
                watch_zone = any(
                    tok >= thr - int(cap * WAVE_WATCH_BAND_PCT) and not usage.is_mass_sealed(track)
                    for track, tok, cap, thr in track_view
                )

                # ── Seal repair: gaps every poll, drift verified every 5 min ──
                # Gated on the mass seal alone — an early wave-guard seal below
                # the static threshold needs repair just as much.
                for track, tok, cap, thr in track_view:
                    if usage.is_mass_sealed(track):
                        _repair_seal_gaps(track, usage, subs, names)

                # ── Cap check (alarm-only; mass throttle above should normally
                #    keep this from firing except for manually-exempt projects)
                normal_exceeded  = normal_tok  >= org.normal_ceiling
                premium_exceeded = premium_tok >= org.premium_ceiling

                if normal_exceeded or premium_exceeded:
                    _handle_overcap(usage, subs, names, normal_exceeded, premium_exceeded)
                elif spend_cap_crossed:
                    # Dollar cap reached but no token cap — flip to AGGRESSIVE so
                    # the user gets fast polling + visible mode badge until they act.
                    usage.set_mode("aggressive")
                    print("[mode] → AGGRESSIVE (spend cap crossed)")
                elif new_spend:
                    mode = usage.get_mode()
                    if mode == "passive":
                        usage.set_mode("urgent")
                        usage.set_last_milestone_ts(time.time())
                        print("[mode] → URGENT (spend milestone crossed)")
                    elif mode == "urgent":
                        usage.reset_urgent_step()
                        usage.set_last_milestone_ts(time.time())
                else:
                    mode = usage.get_mode()
                    if mode == "urgent":
                        last_ms = usage.get_last_milestone_ts()
                        if last_ms and time.time() - last_ms > URGENT_REVERT_SECS:
                            usage.set_mode("passive")
                            print("[mode] → PASSIVE (1 h without new milestone)")
                    elif mode == "aggressive":
                        # Hold aggressive while the SPEND cap is still breached —
                        # spend never decreases intraday, so this holds until the
                        # midnight reset. Without this check, spend-cap aggressive
                        # lasted exactly one poll (spend_cap_crossed only fires on
                        # the first crossing) and the bot dozed off mid-emergency.
                        if snap.get("total_cost", 0.0) >= org.daily_limit:
                            pass   # still bleeding — keep fast polling
                        else:
                            # Token caps cleared (day rollover) and spend under cap
                            usage.set_mode("passive")
                            print("[mode] → PASSIVE (caps no longer exceeded)")

                # ── Hourly project-list sync (after all enforcement) ──
                if time.time() >= next_project_sync:
                    ok = _sync_projects(usage, subs, names) is not None
                    next_project_sync = time.time() + (PROJECT_SYNC_SECS if ok else 300)

                fail_count = 0
            else:
                fail_count += 1
                _note_api_failure(org, fail_count, subs, names)
                mode     = usage.get_mode()
                base     = PASSIVE_INTERVAL_SECS if mode == "passive" else URGENT_INTERVAL_MIN
                max_back = PASSIVE_BACKOFF_MAX   if mode == "passive" else URGENT_INTERVAL_MAX
                backoff  = min(base * (2 ** min(fail_count - 1, 4)), max_back)
                print(f"[poll] Fetch failed ({fail_count}) — retry in {backoff // 60:.0f} min (backoff)")
                if fail_count in (1, 5, 10):   # log the onset + escalation, not every retry
                    _log_event("poll_fail", consecutive=fail_count, backoff_secs=backoff)
                org.poll_now.wait(backoff)
                org.poll_now.clear()
                continue
        except Exception as e:
            print(f"[poll loop error] {e}")
            fail_count += 1

        # ── Determine next sleep interval ──────────────────────────────────
        # Guarded too: increment_urgent_step() saves state, and an OSError here
        # (disk full, EIO) used to escape the loop and end this org's thread for
        # good while the process — and the other org — kept running.
        try:
            mode = usage.get_mode()
            if mode == "passive":
                sleep_secs = PASSIVE_INTERVAL_SECS
            else:
                # Urgent/aggressive exist to poll FASTER. With POLL_INTERVAL_MINS=1
                # their 3→10 min stepping was slower than passive — the bot slowed
                # down exactly when usage was high (9,412 one-minute polls vs 169
                # ten-minute urgent polls in the September log).
                sleep_secs = min(usage.get_urgent_interval(), PASSIVE_INTERVAL_SECS)
                usage.increment_urgent_step()

            if watch_zone and sleep_secs > WAVE_WATCH_SLEEP_SECS:
                # Near an unsealed threshold — tighten the loop so the wave can't
                # ride an 8-minute poll gap over the cap.
                sleep_secs = WAVE_WATCH_SLEEP_SECS
                print(f"[poll] Watch zone — next poll in {sleep_secs} s  (mode={mode})")
            else:
                print(f"[poll] Next poll in {sleep_secs // 60:.0f} min  (mode={mode})")
        except Exception as e:
            print(f"[poll loop error] sleep-interval: {e}")
            sleep_secs = WAVE_WATCH_SLEEP_SECS
        org.poll_now.wait(sleep_secs)   # /refresh cuts this short
        org.poll_now.clear()


# ── Concurrency check thread ───────────────────────────────────────────────

def concurrency_check_loop(usage: UsageStore, subs: SubscriberStore, names: NameStore = None) -> None:
    """Every CONCURRENCY_WINDOW_MINS minutes, check for simultaneous project activity.
    On API failure, leaves the previous active_projects snapshot intact rather than
    overwriting it with an empty dict (which would falsely report 'no activity').
    One thread per org."""
    org = usage.org
    _CTX.org = org
    while True:
        try:
            activity = _fetch_recent_activity(org, CONCURRENCY_WINDOW_MINS)
            if activity is None:
                print("[concurrency] activity fetch failed — keeping last known snapshot")
            else:
                active = {pid: count for pid, count in activity.items() if count > 0}
                usage.set_active_projects(active, CONCURRENCY_WINDOW_MINS)

                if len(active) >= CONCURRENCY_THRESHOLD:
                    last_ts = usage.get_last_concurrent_alert_ts()
                    if last_ts is None or time.time() - last_ts > CONCURRENCY_COOLDOWN:
                        usage.set_last_concurrent_alert_ts(time.time())
                        _broadcast(lambda n, a=active: fmt_concurrency_alert(a, org, n), subs, names,
                                   org=org)
                        print(f"[concurrency] Alert fired — {len(active)} projects active")
        except Exception as e:
            print(f"[concurrency check error] {e}")

        time.sleep(CONCURRENCY_WINDOW_MINS * 60)


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    if not ORGS or not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Missing required environment variables.\n"
            "Ensure at least one org admin key (OPENAI_ADMIN_KEY for Lab 2, OPENAI_ADMIN_KEY_LAB3 "
            "for Lab 3), TELEGRAM_BOT_TOKEN, and TELEGRAM_CHAT_ID are set in .env"
        )

    BOT_DATA_DIR.mkdir(parents=True, exist_ok=True)

    usages = [UsageStore(org.state_path, org) for org in ORGS.values()]
    subs   = SubscriberStore(SUBS_PATH, CHAT_ID)
    names  = NameStore(NAMES_PATH, CHAT_ID)

    # Wire the chat-migration handler so a group→supergroup upgrade rewrites
    # the stores instead of 400-ing on every broadcast forever.
    global _MIGRATION_CB
    _MIGRATION_CB = lambda old, new: (subs.migrate(old, new), names.migrate(old, new))

    for spec in ORG_SPECS:
        if spec["id"] not in ORGS:
            print(f"[config] {spec['label']} not monitored — {spec['key_env']} is not set")
    for u in usages:
        org = u.org
        _load_project_cache(org)
        with _org_context(org):
            print(f"[config] {org.label}: passive poll {PASSIVE_INTERVAL_SECS // 60} min · "
                  f"spend alarm ${org.daily_limit:.2f}/day · free {_fmt_cap(org.normal_cap)}/"
                  f"{_fmt_cap(org.premium_cap)} · seals at normal {_fmt_tokens(org.normal_threshold)}, "
                  f"premium {_fmt_tokens(org.premium_threshold)} · {len(org.projects)} projects known")

    # Usage protection starts first and never waits on Telegram: right after a
    # reboot DNS is often down, and getMe / the stale-update discard used to be
    # one-shot calls made before any thread started. One poll + one concurrency
    # thread per org; one Telegram poller serves them all.
    workers = []
    for u in usages:
        workers.append(threading.Thread(target=usage_poll_loop, args=(u, subs, names),
                                        daemon=True, name=f"poll-{u.org.id}"))
        workers.append(threading.Thread(target=concurrency_check_loop, args=(u, subs, names),
                                        daemon=True, name=f"concurrency-{u.org.id}"))
    workers.append(threading.Thread(target=telegram_poll_loop, args=(usages, subs, names),
                                    daemon=True, name="telegram"))
    for t in workers:
        t.start()

    # Watchdog: every worker loops forever, so a dead one is a bug that would
    # otherwise go unnoticed — one org silently unguarded while the other (and
    # the process) keep running. Exit instead; the launcher restarts the bot.
    try:
        while True:
            time.sleep(1)
            dead = [t.name for t in workers if not t.is_alive()]
            if dead:
                print(f"[bot] FATAL: thread(s) {', '.join(dead)} died — exiting so the launcher restarts the bot")
                _log_event("thread_died", threads=dead)
                sys.exit(1)
    except KeyboardInterrupt:
        print("[bot] Shutdown signal received. Standing down.")


if __name__ == "__main__":
    main()
