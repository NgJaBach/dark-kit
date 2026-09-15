"""
Telegram Usage Bot — OpenAI Shadow Ledger

Polls OpenAI organization usage API and reports token/cost stats per project.
Receives @commands from the configured Telegram chat (with inline-button menu).
Monitors token milestones, concurrent project activity, and the per-track 95%
rate-limit seal that prevents cap breaches.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BOT IDENTITY: Marshal-Rank Shadow Commander
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Bound to Bach the Monarch. Speaks with formality and restraint.
No humor. No filler. Precision in all things.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import calendar
import html
import json
import os
import re
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
BOT_UPDATED = "2026-09-16"
BOT_CHANGES = (
    "New \U0001f9ea Exotic lane: $ spent on models outside the free lanes",
    "Each lane now shows its billed cost, e.g. \"/ 1M. Cost: 0.00$\"",
    "Network blips (DNS/timeouts) are retried — no more half-finished seals",
    "Premium seals at 80%; seals are verified against the API each poll",
)

OPENAI_ADMIN_KEY = os.environ.get("OPENAI_ADMIN_KEY", "")
BOT_TOKEN        = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID          = os.environ.get("TELEGRAM_CHAT_ID", "")
DAILY_LIMIT      = 2.00   # daily-spend hard cap (USD); $0 in 2026 was the goal,
                          # but unlisted-model usage (embeddings, audio, image, etc.)
                          # bills from token 1 so we treat $2 as the "alarm-out-loud" line.

# ── Spend monitoring (org-wide + per-project + unlisted-model anomaly) ──────
# The bot was originally token-only. An incident — $6 of embedding usage went
# undetected because embeddings aren't in either free-tier watchlist — exposed
# the gap: any unlisted model spends from token 1 with no token-milestone, no
# overcap alarm, no rate-limit seal trigger. Spend monitoring closes the gap.
#
# Three independent layers, all alarm-only (no auto-seal): unlisted models are
# the likely culprits and they bypass the seal logic by design, so an auto-seal
# would be cosmetic. The user retains manual `archive seal` to stop the bleed.
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

BOT_DATA_DIR     = Path(__file__).parent / "bot_data"
USAGE_STATE_PATH = BOT_DATA_DIR / "usage_state.json"
SUBS_PATH        = BOT_DATA_DIR / "subscribers.json"
NAMES_PATH       = BOT_DATA_DIR / "names.json"
LOGS_DIR         = BOT_DATA_DIR / "logs"

# ── Local intel log ─────────────────────────────────────────────────────────
# Telegram broadcasts used to exist ONLY in the chat — nothing durable on disk
# for later analysis. _log_event mirrors every broadcast and key state change
# to a monthly JSONL file (bot_data/logs/events-YYYY-MM.jsonl). Each line:
#   {"ts": ..., "utc": "...", "kind": "...", ...fields}
# Kinds: broadcast | mode | poll | poll_fail | mass_seal | mass_unseal |
#        manual_seal | manual_unseal | pending_unseal | day_rollover | command
# Best-effort by design: a logging failure prints one line and never breaks
# the bot. Files are small (a quiet month is well under 1 MB); prune by hand.
_LOG_LOCK = threading.Lock()


def _log_event(kind: str, **fields) -> None:
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        now  = datetime.now(timezone.utc)
        path = LOGS_DIR / f"events-{now.strftime('%Y-%m')}.jsonl"
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
# (threshold, level)  level: "casual" | "urgent" | "cap"
# Normal/mini models — 10M free daily (gpt-4o-mini, o1-mini, o3-mini, etc.)
TOKEN_MILESTONES = [
    (1_000_000,  "casual"),
    (4_000_000,  "casual"),
    (7_000_000,  "casual"),
    (8_000_000,  "urgent"),
    (9_000_000,  "urgent"),
    (10_000_000, "cap"),       # switches to spend-based alerting from here
]
TOKEN_HARD_CAP       = 10_000_000

# Premium models — 1M free daily (gpt-4o, gpt-4.1, o1, o3, etc.)
PREMIUM_TOKEN_MILESTONES = [
    (200_000,   "casual"),
    (500_000,   "casual"),
    (800_000,   "urgent"),
    (1_000_000, "cap"),
]
PREMIUM_TOKEN_HARD_CAP = 1_000_000

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
# Concurrency model: a single atomic "busy" claim serialises ALL rate-limit work
# (auto 95% sweep, day-rollover restore, manual button ops). Callers ASK to claim
# the flag — if another op is in progress they bail immediately instead of blocking.
# Manual ops dispatched from the Telegram callback handler run in a background
# worker thread, so the poll loop is never frozen by a 30-50 s sweep.
#
# Previous design used a `threading.Lock` held for the entire operation. Two bugs:
#   1. The TOCTOU race — callbacks read `_SEAL_BUSY` without holding the lock,
#      then `with _SEAL_LOCK:` would block the Telegram thread until the running
#      op finished, freezing every command for 30-50 s.
#   2. No worker thread — even the "successful" callback path ran inline, so the
#      Telegram poll loop couldn't fetch new updates while a seal/unseal ran.
_BUSY_LOCK = threading.Lock()
_BUSY      = False


def _try_claim_busy() -> bool:
    """Atomic check-and-set. Returns True iff this caller now holds the seal-busy
    claim — caller MUST `_release_busy()` in a `finally` once done."""
    global _BUSY
    with _BUSY_LOCK:
        if _BUSY:
            return False
        _BUSY = True
        return True


def _release_busy() -> None:
    global _BUSY
    with _BUSY_LOCK:
        _BUSY = False


def _is_busy() -> bool:
    with _BUSY_LOCK:
        return _BUSY

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
NORMAL_SEAL_REMAINING_PCT      = 0.05   # seal at 9.5M  (500k buffer)
PREMIUM_SEAL_REMAINING_PCT     = 0.20   # seal at 800k  (200k buffer — see note)
NORMAL_TRACK_SEAL_THRESHOLD    = int(TOKEN_HARD_CAP         * (1 - NORMAL_SEAL_REMAINING_PCT))
PREMIUM_TRACK_SEAL_THRESHOLD   = int(PREMIUM_TOKEN_HARD_CAP * (1 - PREMIUM_SEAL_REMAINING_PCT))

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

# ── Known projects (IDs from exported CSV — case-sensitive) ─────────────────
KNOWN_PROJECTS: dict[str, str] = {
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
}

OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"
OPENAI_USAGE_URL = "https://api.openai.com/v1/organization/usage/completions"

# ── Free-tier model classification (from OpenAI's free-usage page) ─────────
# SOURCE OF TRUTH — re-check when OpenAI updates the offer:
#   https://help.openai.com/en/articles/10306912-sharing-feedback-evaluation-and-fine-tuning-data-and-api-inputs-and-outputs-with-openai
#   (section: "What models are included in this offer?")
# Last synced: 2026-08-21.
#
# OpenAI lists DATED SNAPSHOTS (e.g. "gpt-5.4-2026-03-05"). We store the BASE
# name; _is_listed_variant accepts the exact name or base + a -YYYY-MM-DD
# snapshot suffix, so one entry covers every dated snapshot of that model while
# same-prefix paid products (gpt-5.5-pro, o1-pro, gpt-4o-mini-tts, …) stay
# unlisted. Quota is SHARED across each group. Excluded by OpenAI regardless of
# name: fine-tuned models, fine-tuning training, evals, and tool use.
#
# NOTE ON TIERS: the groups are 1M / 10M for usage tier 3+, but only
# 250K / 2.5M for tiers 1-2. TOKEN_HARD_CAP / PREMIUM_TOKEN_HARD_CAP below
# assume tier 3+. Drop them to 2_500_000 / 250_000 if the org is tier 1-2.
#
# Normal-band models share 10M tokens/day free:
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
# Premium-band models share 1M tokens/day free:
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

def today_window() -> tuple[int, int]:
    now   = datetime.now(timezone.utc)
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
        end_ts = int(now.timestamp())
    else:
        end_ts = int(datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    return int(start_dt.timestamp()), end_ts


def prev_month() -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    if now.month == 1:
        return now.year - 1, 12
    return now.year, now.month - 1


# ── OpenAI API ─────────────────────────────────────────────────────────────

def _openai_headers() -> dict:
    return {"Authorization": f"Bearer {OPENAI_ADMIN_KEY}"}


# Network-level failures are transient on this host: 67 DNS resolution failures
# in ~18 h on 2026-09-15. One of them used to fail an entire 186-row quarantine
# sweep (the seal aborts and rolls back on the first failed POST), leaving the
# project unthrottled until the next poll. Retry those in place. HTTP error
# RESPONSES are deliberately not retried — they are answers, not blips.
_NET_RETRY_BACKOFF = (1, 2)   # seconds before attempts 2 and 3


def _openai_call(method: str, url: str, **kwargs):
    """requests.get/post to the OpenAI Admin API, retrying DNS / connection /
    timeout errors up to twice. Re-raises the last error if every attempt fails,
    so every caller's existing `except Exception` handling still applies."""
    for attempt in range(len(_NET_RETRY_BACKOFF) + 1):
        try:
            return getattr(requests, method)(url, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt == len(_NET_RETRY_BACKOFF):
                raise
            time.sleep(_NET_RETRY_BACKOFF[attempt])


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


def _fetch_costs_breakdown() -> Optional[tuple[dict, dict]]:
    """Today's billed cost, grouped by project AND line item in one call.
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
            r = _openai_call("get", OPENAI_COSTS_URL, headers=_openai_headers(), params=p, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            print(f"[openai costs network error] {e}")
            return None if not fetched_any_page else (costs, lanes)
        if not r.ok:
            print(f"[openai costs {r.status_code}] {r.text[:500]}")
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


def _fetch_costs() -> Optional[dict[str, float]]:
    """Today's cost per project ('__org__' = unattributed). None on API failure.
    Thin wrapper kept for callers that don't need the lane split."""
    res = _fetch_costs_breakdown()
    return None if res is None else res[0]


def _fetch_tokens() -> Optional[dict[str, dict]]:
    """Today's token usage per project, broken down by model and band.
    Returns None on API failure (distinguished from {} = no usage today). The
    distinction matters: empty-day must NOT block polling — that creates a window
    where the first request of the day goes undetected."""
    start, end = today_window()
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
            r = _openai_call("get", OPENAI_USAGE_URL, headers=_openai_headers(), params=p, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            print(f"[openai usage network error] {e}")
            return None if not fetched_any_page else tokens
        if not r.ok:
            print(f"[openai usage {r.status_code}] {r.text[:500]}")
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


def _fetch_monthly_costs(year: int, month: int) -> dict[str, float]:
    """Cost per project for a full calendar month. '__org__' key for unattributed costs."""
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
            r = _openai_call("get", OPENAI_COSTS_URL, headers=_openai_headers(), params=p, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            print(f"[openai monthly costs error] {e}")
            break
        if not r.ok:
            print(f"[openai monthly costs {r.status_code}] {r.text[:300]}")
            break
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


def _fetch_recent_activity(minutes: int = CONCURRENCY_WINDOW_MINS) -> Optional[dict[str, int]]:
    """Request count per project in the last `minutes` minutes (minute-level buckets).
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
        r = _openai_call("get", OPENAI_USAGE_URL, headers=_openai_headers(), params=params, timeout=REQUEST_TIMEOUT)
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


def _fetch_recent_activity_by_band(minutes: int) -> Optional[dict[str, dict[str, int]]]:
    """Per-project recent request counts broken down by model band.
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
        r = _openai_call("get", OPENAI_USAGE_URL, headers=_openai_headers(), params=params, timeout=REQUEST_TIMEOUT)
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
                             normal_exceeded: bool, premium_exceeded: bool) -> dict[str, dict[str, int]]:
    """From banded recent activity, return only projects with usage on an exceeded band.
    Preserves the full per-band breakdown so the alert formatter can show detail."""
    out: dict[str, dict[str, int]] = {}
    for pid, bands in banded.items():
        if (normal_exceeded and bands.get("normal", 0) > 0) \
           or (premium_exceeded and bands.get("premium", 0) > 0):
            out[pid] = bands
    return out


# ── OpenAI Admin: project rate-limit API ───────────────────────────────────
OPENAI_RATE_LIMITS_URL_TMPL = "https://api.openai.com/v1/organization/projects/{pid}/rate_limits"


def _fetch_project_rate_limits(pid: str) -> Optional[list[dict]]:
    """Return every rate-limit row for a project (one per model). None on API failure."""
    out: list[dict] = []
    params = [("limit", 100)]
    page = None
    url  = OPENAI_RATE_LIMITS_URL_TMPL.format(pid=pid)
    while True:
        p = list(params)
        if page:
            p.append(("after", page))
        try:
            r = _openai_call("get", url, headers=_openai_headers(), params=p, timeout=REQUEST_TIMEOUT)
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


def _update_project_rate_limit(pid: str, rate_limit_id: str, payload: dict,
                               _attempts_left: int = 4) -> bool:
    """POST a partial update to a single rate-limit row.
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
            headers={**_openai_headers(), "Content-Type": "application/json"},
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
                return _update_project_rate_limit(pid, rate_limit_id, clamped,
                                                  _attempts_left=_attempts_left - 1)
        print(f"[openai rate-limits] {pid}/{rate_limit_id}: above org ceiling and "
              f"not clampable — skipping so the seal state can still clear")
        return True   # soft skip: never strand a project sealed over one row
    print(f"[openai rate-limits POST {r.status_code}] {pid}/{rate_limit_id}: {r.text[:300]}")
    return False


def _fetch_recent_data(days: int = 31) -> dict:
    """Aggregated data for the last `days` calendar days.
    Returns per-project costs, org-level cost, total tokens, total requests."""
    now      = datetime.now(timezone.utc)
    start_dt = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start_ts = int(start_dt.timestamp())
    tok_end  = int(now.timestamp())
    cost_end = int(tomorrow.timestamp())

    proj_costs:     dict[str, float] = {}
    total_tokens   = 0
    total_requests = 0

    # Costs per project — paginated (API max limit=180 for costs endpoint)
    cost_params = [
        ("start_time",   start_ts),
        ("end_time",     cost_end),
        ("bucket_width", "1d"),
        ("group_by[]",   "project_id"),
        ("limit",        100),
    ]
    try:
        page = None
        while True:
            p = list(cost_params)
            if page:
                p.append(("page", page))
            r = _openai_call("get", OPENAI_COSTS_URL, headers=_openai_headers(), params=p, timeout=REQUEST_TIMEOUT)
            if r.ok:
                data = r.json()
                for bucket in data.get("data", []):
                    for result in bucket.get("results", []):
                        pid = result.get("project_id") or "__org__"
                        val = float(result.get("amount", {}).get("value", 0.0))
                        proj_costs[pid] = proj_costs.get(pid, 0.0) + val
                if not data.get("has_more"):
                    break
                page = data.get("next_page")
                if not page:
                    break
            else:
                print(f"[recent costs {r.status_code}] {r.text[:200]}")
                break
    except Exception as e:
        print(f"[recent costs error] {e}")

    # Tokens + requests — paginated (API max limit=31 for bucket_width=1d)
    tok_params = [
        ("start_time",   start_ts),
        ("end_time",     tok_end),
        ("bucket_width", "1d"),
        ("group_by[]",   "project_id"),
        ("limit",        31),
    ]
    try:
        page = None
        while True:
            p = list(tok_params)
            if page:
                p.append(("page", page))
            r = _openai_call("get", OPENAI_USAGE_URL, headers=_openai_headers(), params=p, timeout=REQUEST_TIMEOUT)
            if r.ok:
                data = r.json()
                for bucket in data.get("data", []):
                    for result in bucket.get("results", []):
                        total_tokens   += result.get("input_tokens", 0) + result.get("output_tokens", 0)
                        total_requests += result.get("num_model_requests", 0)
                if not data.get("has_more"):
                    break
                page = data.get("next_page")
                if not page:
                    break
            else:
                print(f"[recent tokens {r.status_code}] {r.text[:200]}")
                break
    except Exception as e:
        print(f"[recent tokens error] {e}")

    return {
        "proj_costs":     proj_costs,
        "total_tokens":   total_tokens,
        "total_requests": total_requests,
        "start_date":     start_dt.strftime("%Y-%m-%d"),
        "end_date":       now.strftime("%Y-%m-%d"),
    }


def fetch_today_usage() -> Optional[dict]:
    """Tokens-only poll — no cost fetch (costs API is unreliable for frequent polling).
    Returns None only on actual API failure. An empty `tokens` dict (no usage yet today)
    yields a valid snap with an empty `projects` map — so the bot stays in its normal
    poll cadence and catches the first request the moment it appears, instead of
    sitting in backoff for hours on a quiet day."""
    tokens = _fetch_tokens()
    if tokens is None:
        return None   # API genuinely failed
    projects: dict[str, dict] = {}
    for pid, tok in tokens.items():
        projects[pid] = {
            "name":           KNOWN_PROJECTS.get(pid, pid),
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
        "date":                 today_str(),
        "projects":             projects,
        "total_cost":           0.0,
        "total_premium_tokens": total_premium,
        "total_normal_tokens":  total_normal,
        "last_polled":          time.time(),
    }


def _enrich_costs(snap: dict, usage: "UsageStore" = None, live: bool = True) -> dict:
    """Overlay costs onto a snapshot copy. costs=None means "API failure" — fall
    back to cache. costs={} means "successful fetch, no spend yet" — overlay zeros
    cleanly. Critical for matching the poll loop's empty-day handling."""
    import copy
    snap  = copy.deepcopy(snap)
    breakdown = _fetch_costs_breakdown() if live else None
    if breakdown is not None:
        costs, lane_costs = breakdown
        org_cost = costs.pop("__org__", 0.0)
        for pid, p in snap.get("projects", {}).items():
            p["cost_usd"] = round(costs.get(pid, 0.0), 6)
        snap["total_cost"] = round(sum(costs.values()) + org_cost, 6)
        snap["org_cost"]   = round(org_cost, 6)
        snap["lane_costs"] = {l: round(v, 6) for l, v in lane_costs.items()}
        if usage:
            usage.update_costs(costs, snap["total_cost"], org_cost, lane_costs)
    else:
        cached = usage.get_costs_cache() if usage else None
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
    """Persists today's usage snapshot and all alert-control state to disk."""

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
        "mode_entered_ts",
        "last_milestone_ts",
        "last_illegal_seen_ts",
        "urgent_poll_step",
        "milestones_seeded",
        # project sealing:
        #   sealed_tracks       — {track: {sealed_at, originals_by_project: {pid: [rows]}}}
        #                         holds every project currently throttled on a track,
        #                         whether by the mass auto-throttle or a manual seal.
        #   mass_sealed_tracks  — [track] for which the 95% mass sweep has fired today
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

    def __init__(self, path: Path):
        self.path  = path
        self._lock = threading.Lock()
        self._data: dict = {}
        self._load()
        # If loaded state is from a previous day, reset daily fields immediately
        # so /tokens, /refresh, etc. don't surface yesterday's numbers in the
        # window between bot start and the first poll.
        persisted = self._data.get("date")
        today     = today_str()
        if persisted and persisted != today:
            with self._lock:
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
        # Move sealed_tracks → pending_track_unseal (merge per-track if the queue
        # already had a stale entry for the same track).
        s_tracks = self._data.get("sealed_tracks", {})
        p_tracks = self._data.get("pending_track_unseal", {})
        for track, info in s_tracks.items():
            if track in p_tracks:
                p_tracks[track].setdefault("originals_by_project", {}).update(
                    info.get("originals_by_project", {})
                )
            else:
                p_tracks[track] = info

        self._data["token_milestones_notified"]   = []
        self._data["premium_milestones_notified"] = []
        self._data["bot_mode"]                    = "passive"
        self._data["mode_entered_ts"]             = None
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
        # Drop fields retired in earlier versions
        for legacy in ("manually_unsealed_today", "sealed_projects", "pending_unseal",
                       "alert_sent", "spend_intervals_notified"):
            self._data.pop(legacy, None)
        self._data.pop("costs_cache", None)

    def update(self, snapshot: dict):
        """Merge new snapshot, preserving all alert-control fields.
        Auto-resets daily state if the snapshot's date is newer than the persisted date.
        Day rollover handled here closes the race where the Telegram thread runs /refresh
        on a new day before the poll loop notices."""
        with self._lock:
            new_date = snapshot.get("date")
            old_date = self._data.get("date")
            if new_date and old_date and new_date != old_date:
                print(f"[store] Day rollover {old_date} → {new_date} — daily state reset")
                _log_event("day_rollover", from_date=old_date, to_date=new_date,
                           final_normal=self._data.get("total_normal_tokens", 0),
                           final_premium=self._data.get("total_premium_tokens", 0),
                           final_cost=self._data.get("total_cost", 0.0))
                self._reset_daily_state_locked()
            preserved = {k: self._data[k] for k in self._PRESERVED if k in self._data}
            self._data = snapshot
            self._data.update(preserved)
            self._save()

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
                     per_lane: dict = None) -> None:
        with self._lock:
            self._data["costs_cache"] = {
                "per_project": dict(per_project),
                "per_lane":    dict(per_lane or {}),
                "total":       total,
                "org":         org,
                "ts":          time.time(),
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
            self._data["bot_mode"]        = mode
            self._data["mode_entered_ts"] = time.time()
            if mode in ("urgent", "aggressive"):
                self._data["urgent_poll_step"] = 0
            self._save()
        if prev != mode:
            _log_event("mode", from_mode=prev, to_mode=mode)

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
    def get_sealed_tracks(self) -> dict:
        with self._lock:
            return {t: dict(info) for t, info in self._data.get("sealed_tracks", {}).items()}

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
            return {t: dict(info) for t, info in self._data.get("pending_track_unseal", {}).items()}

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
        with path.open("rb") as f:
            r = requests.post(url, data=data, files={"animation": f}, timeout=30)
        if not r.ok:
            print(f"[telegram anim {r.status_code}] chat={target_chat} | {r.text[:400]}")
    except Exception as e:
        print(f"[telegram anim error] {e}")


# Set in main() to a callback(old_id, new_id) that rewrites the subscriber and
# name stores when Telegram reports a group→supergroup migration. Module-level
# because _send has no store references.
_MIGRATION_CB = None


def _send(text: str, chat_id: str = None, thread_id: int = None,
          keyboard: list = None) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    target_chat = str(chat_id or CHAT_ID)
    payload = {"chat_id": target_chat, "text": text, "parse_mode": "HTML"}
    if thread_id:
        payload["message_thread_id"] = thread_id
    if keyboard is not None:
        payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    try:
        r = requests.post(
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
            r2 = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
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
        r = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
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
        requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        print(f"[telegram answerCallback error] {e}")


def _spawn_bg(label: str, fn, *args) -> None:
    """Run `fn(*args)` in a daemon thread so slow rate-limit work never blocks the
    caller. Used by cmd_refresh, which executes on the Telegram poll thread — a
    mass seal or quarantine sweep there would freeze every command for minutes.
    Callees self-guard with the busy claim, so concurrent invocations are safe."""
    def _runner():
        try:
            fn(*args)
        except Exception as e:
            print(f"[bg:{label}] error: {e}")
    threading.Thread(target=_runner, daemon=True, name=f"bg-{label}").start()


def _broadcast(fmt_fn, subs: SubscriberStore, names: NameStore = None) -> None:
    """Send a personalised message to every subscriber.
    `fmt_fn` is a one-arg function: it receives the chat's registered display name
    (or "Bach" when no NameStore is wired) and returns the rendered HTML to send.

    Every broadcast is also mirrored to the local intel log (one entry per
    broadcast, canonical "Bach" rendering) — Telegram is no longer the only
    place push alerts exist."""
    try:
        _log_event("broadcast", text=fmt_fn("Bach"))
    except Exception as e:
        print(f"[intel-log render error] {e}")
    for cid in subs.all():
        name = names.get(cid) if names is not None else "Bach"
        _send(fmt_fn(name), cid)


def _get_updates(offset: int) -> list[dict]:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        r = requests.get(
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


def _discard_pending_updates() -> int:
    """Discard any Telegram updates that piled up while the bot was offline.
    Returns the next safe offset to use. Without this, on restart the bot would
    replay up to 24 h of queued updates — including stale `arch:seal:both:all`
    button clicks that could fire a real mass-seal from a UI a user has long
    forgotten about. We grab the latest update_id and ACK it; Telegram drops
    everything ≤ that id from the queue."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"offset": -1, "timeout": 0,
                                       "allowed_updates": json.dumps([])},
                          timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        updates = r.json().get("result", [])
        if not updates:
            return 0
        next_offset = updates[-1]["update_id"] + 1
        # ACK so Telegram drops these from the queue.
        requests.get(url, params={"offset": next_offset, "timeout": 0,
                                   "allowed_updates": json.dumps([])},
                      timeout=REQUEST_TIMEOUT)
        print(f"[telegram] Discarded {len(updates)} stale update(s); next offset={next_offset}")
        return next_offset
    except Exception as e:
        print(f"[telegram offset discard error] {e}")
        return 0


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
                    indent: str = "   ") -> list:
    """The three lane lines shared by every report. Free-tier lanes show tokens
    against their allowance plus billed cost (which stays 0.00$ until the
    allowance is exhausted). Exotic has no allowance to count against, so it
    shows cost only."""
    lc = lane_costs or {}
    return [
        f"{indent}⭐ Premium (1M): <b>{_fmt_tokens(premium_tok)}</b> / 1M. Cost: {_fmt_cost(lc.get('premium'))}",
        f"{indent}📦 Normal (10M): <b>{_fmt_tokens(normal_tok)}</b> / 10M. Cost: {_fmt_cost(lc.get('normal'))}",
        f"{indent}🧪 Exotic: <b>{_fmt_cost(lc.get('exotic'))}</b>",
    ]


def _fmt_ts(ts: Optional[float]) -> str:
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _fmt_month(year: int, month: int) -> str:
    return datetime(year, month, 1).strftime("%B %Y")


# ── Formatters — auto-messages ─────────────────────────────────────────────

def fmt_token_milestone(threshold: int, current: int, level: str, name: str = "Bach") -> str:
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
    # cap (10M)
    return (
        f"🚨 <b>Normal Models Allowance Exhausted — {c}</b>\n\n"
        f"The {t}-token daily allowance for normal models has been crossed.\n"
        f"Mini models (gpt-4o-mini, o1-mini, o3-mini, etc.) are now billing at standard rates.\n\n"
        f"Monarch {name}, the operation requires your oversight."
    )


def fmt_premium_token_milestone(threshold: int, current: int, level: str, name: str = "Bach") -> str:
    t = _fmt_tokens(threshold)
    c = _fmt_tokens(current)
    if level == "casual":
        return (
            f"📊 <b>Premium Token Threshold — {t}</b>\n\n"
            f"Full-size model consumption stands at <b>{c} tokens</b>.\n"
            f"Premium models daily allowance: 1M/day (gpt-4o, gpt-4.1, o1, o3, etc.)\n"
            f"<i>Monitoring continues, Monarch {name}.</i>"
        )
    if level == "urgent":
        return (
            f"⚠️ <b>Premium Model — High Usage — {t}</b>\n\n"
            f"Full-size model usage has reached <b>{c} tokens</b>.\n"
            f"Approaching the 1M daily free allowance for premium models.\n"
            f"Your attention is advised, My Liege {name}."
        )
    # cap (1M)
    return (
        f"🚨 <b>Premium Free Allowance Exhausted — {c}</b>\n\n"
        f"The {t}-token daily allowance for premium models has been crossed.\n"
        f"Premium models (gpt-4o, gpt-4.1, o1, o3, etc.) are now billing at standard rates.\n\n"
        f"Monarch {name}, the operation requires your oversight."
    )


def fmt_concurrency_alert(active: dict, name: str = "Bach") -> str:
    lines = [
        f"⚡ <b>Concurrent Project Activity — {len(active)} Projects</b>\n",
        f"{len(active)} projects recorded activity in the last "
        f"{CONCURRENCY_WINDOW_MINS} minutes:\n",
    ]
    for pid, count in sorted(active.items(), key=lambda x: x[1], reverse=True):
        proj_name = KNOWN_PROJECTS.get(pid, pid)
        lines.append(f"• <b>{proj_name}</b> — {count:,} requests")
    lines.append(f"\n<i>Monarch {name}, multiple operations are in simultaneous execution.</i>")
    return "\n".join(lines)


def fmt_overcap_active_alert(banded_active: dict, normal_exceeded: bool, premium_exceeded: bool,
                             name: str = "Bach") -> str:
    """Render the red-tone overcap alert. `banded_active` maps pid → {"normal": int, "premium": int}
    and contains only projects with usage on at least one exceeded band."""
    bands = []
    if normal_exceeded:
        bands.append("Normal (10M)")
    if premium_exceeded:
        bands.append("Premium (1M)")
    band_str = " & ".join(bands)

    def _illegal_reqs(b: dict) -> int:
        r = 0
        if normal_exceeded:  r += b.get("normal", 0)
        if premium_exceeded: r += b.get("premium", 0)
        return r

    lines = [
        f"🔴 <b>‼️ BUDGET BREACHED — ILLEGAL ACTIVITY DETECTED ‼️</b>\n",
        f"The <b>{band_str}</b> free-tier allowance is <b>exhausted</b>.",
        f"These projects are <b>still burning the exhausted band</b> — every request now bills:\n",
    ]
    for pid, b in sorted(banded_active.items(), key=lambda x: _illegal_reqs(x[1]), reverse=True):
        proj_name = KNOWN_PROJECTS.get(pid, pid)
        parts = []
        if normal_exceeded and b.get("normal", 0) > 0:
            parts.append(f"{b['normal']:,} normal")
        if premium_exceeded and b.get("premium", 0) > 0:
            parts.append(f"{b['premium']:,} premium")
        detail = " + ".join(parts)
        lines.append(f"🚨 <b>{proj_name}</b>  —  {detail} reqs in the last {OVERCAP_WINDOW_MINS} min")
    lines.append(f"\n<b>HALT ALL NON-ESSENTIAL OPERATIONS IMMEDIATELY.</b>")
    lines.append(f"<i>(Activity window: last {OVERCAP_WINDOW_MINS} min — accounts for API ingestion lag)</i>")
    lines.append(f"<i>Monarch {name} — the treasury is bleeding. Your command is required at once.</i>")
    return "\n".join(lines)


# ── Overcap handler ────────────────────────────────────────────────────────

def _handle_overcap(usage: UsageStore, subs: SubscriberStore, names: NameStore,
                    normal_exceeded: bool, premium_exceeded: bool) -> None:
    """Alarm-only handler for cap breaches. The mass-throttle that prevents the breach
    in the first place lives in _handle_track_seal (fired at 95% utilisation, well
    before the cap is reached). When a cap is breached anyway — which now only
    happens for projects that were manually unsealed via the exemption command —
    this handler keeps shouting at them.

    A project using premium models does not trigger the normal-cap alarm and vice versa.
    Uses OVERCAP_WINDOW_MINS to absorb the 5–15 min OpenAI ingestion lag.
    Reverts to passive after AGGRESSIVE_REVERT_SECS quiet on the exceeded band."""
    banded = _fetch_recent_activity_by_band(minutes=OVERCAP_WINDOW_MINS)
    if banded is None:
        # API failure — keep current mode, don't broadcast or revert based on bad data.
        print("[overcap] activity fetch failed — holding mode")
        return
    illegal = _filter_to_exceeded_band(banded, normal_exceeded, premium_exceeded)
    mode    = usage.get_mode()

    if illegal:
        usage.update_last_illegal_seen()
        if mode != "aggressive":
            usage.set_mode("aggressive")
            print(f"[mode] → AGGRESSIVE ({len(illegal)} project(s) burning the exhausted band)")
        _broadcast(lambda n, r=illegal, ne=normal_exceeded, pe=premium_exceeded:
            fmt_overcap_active_alert(r, ne, pe, n), subs, names)
    elif mode == "aggressive":
        last_ts = usage.get_last_illegal_seen_ts()
        if last_ts and time.time() - last_ts > AGGRESSIVE_REVERT_SECS:
            usage.set_mode("passive")
            print("[mode] → PASSIVE (1 h since last illegal-band activity — standing down)")


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


def _compute_canonical_baseline(usage: "UsageStore" = None) -> dict:
    """Per-model canonical rate-limit values, healthy by construction.

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
    row's own captured original.

    `usage` is optional only so the function can run in tests without a store; in
    production always pass it so the pre-throttle captures are available."""
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
    if usage is not None:
        capture_groups = []
        for info in usage.get_sealed_tracks().values():
            capture_groups.extend(info.get("originals_by_project", {}).values())
        for info in usage.get_pending_track_unseal().values():
            capture_groups.extend(info.get("originals_by_project", {}).values())
        for originals in capture_groups:
            for o in originals:
                _ingest(o.get("model", ""), o)

    # Source 2 — live API values
    for pid in KNOWN_PROJECTS:
        rls = _fetch_project_rate_limits(pid)
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


def _restore_rate_limits(pid: str, originals: list[dict],
                         baseline: dict[str, dict] = None) -> int:
    """POST rate-limit rows back to the API one at a time with inter-write spacing.
    Returns the count of failed POSTs (0 on full success).

    When `baseline` is provided, each row is restored to the canonical consensus
    value for its model (from _compute_canonical_baseline) rather than the value
    captured at seal time. This makes restores uniform across projects and immune
    to the 0/0 cascade — even if a captured original was stale (0/0), the baseline
    carries the healthy org-wide value. Rows whose model isn't in the baseline fall
    back to their captured values."""
    failed = 0
    for orig in originals:
        model   = orig.get("model", "")
        payload = dict(baseline[model]) if (baseline and model in baseline) \
                  else _restore_payload(orig)
        if not payload:
            continue
        if _update_project_rate_limit(pid, orig["id"], payload):
            time.sleep(0.05)
        else:
            failed += 1
    return failed


# ── Per-project, per-track throttle / restore primitives ───────────────────

def _throttle_track_for_project(pid: str, track: str, usage: UsageStore) -> str:
    """Throttle every rate-limit row of `pid` that belongs to `track` down to 0,
    saving the pre-throttle originals into sealed_tracks. Returns 'throttled' /
    'noop' (no rows for this track) / 'failed'. On partial failure rolls back its
    own rows so the project is left untouched. Caller must hold the busy claim."""
    rate_limits = _fetch_project_rate_limits(pid)
    if rate_limits is None:
        return "failed"
    track_rls = [rl for rl in rate_limits if _matches_track(rl.get("model", ""), track)]
    if not track_rls:
        return "noop"

    # Skip rows already throttled (idempotent re-seal); capture only healthy rows.
    originals = _capture_originals(
        [rl for rl in track_rls if rl.get("max_requests_per_1_minute") or rl.get("max_tokens_per_1_minute")]
    )
    throttled_ids: list[str] = []
    for rl in track_rls:
        if _update_project_rate_limit(pid, rl["id"], _seal_payload(rl)):
            throttled_ids.append(rl["id"])
            time.sleep(0.05)
        else:
            rollback = [o for o in originals if o["id"] in throttled_ids]
            _restore_rate_limits(pid, rollback)
            return "failed"

    if originals:   # only record if we captured healthy pre-throttle values
        usage.add_track_originals(track, pid, originals)
    return "throttled"


def _restore_track_for_project(pid: str, track: str, usage: UsageStore,
                               baseline: dict) -> str:
    """Restore `pid`'s rows for `track` to the canonical baseline, then drop the
    project from sealed_tracks[track]. Returns 'restored' / 'noop' / 'failed'.
    Caller must hold the busy claim."""
    info      = usage.get_sealed_tracks().get(track, {})
    originals = info.get("originals_by_project", {}).get(pid, [])
    if not originals:
        return "noop"
    failed = _restore_rate_limits(pid, originals, baseline=baseline)
    if failed:
        return "failed"
    usage.pop_track_originals(track, pid)
    return "restored"


# ── Mass throttle (auto 95% sweep + manual "all") ──────────────────────────

def _ordered_projects_for_track_seal(track: str) -> list[str]:
    """KNOWN_PROJECTS ordered most-active-first on `track`, so the sweep throttles
    the heaviest spenders first during the sequential (~30–50s) operation."""
    activity = _fetch_recent_activity_by_band(minutes=OVERCAP_WINDOW_MINS) or {}
    return sorted(KNOWN_PROJECTS.keys(),
                  key=lambda p: activity.get(p, {}).get(track, 0), reverse=True)


def _mass_seal_track(track: str, usage: UsageStore, subs: SubscriberStore,
                     names: NameStore, *, consumed: int = None,
                     respect_exemptions: bool = True) -> None:
    """Throttle `track` to 0 across every project (skipping exemptions when
    respect_exemptions). Concise begin/done broadcast. Marks the track mass-sealed.
    REQUIRES: caller already holds the busy claim (`_try_claim_busy`)."""
    cap = TOKEN_HARD_CAP if track == "normal" else PREMIUM_TOKEN_HARD_CAP
    if consumed is None:
        consumed = cap   # manual trigger: report at/over cap

    usage.mark_mass_sealed(track)
    print(f"[mass-seal] {track} → starting (consumed={consumed:,}, cap={cap:,})")
    _broadcast(lambda n, t=track, c=consumed, cp=cap:
        fmt_seal_batch_begin(t, c, cp, n), subs, names)

    throttled, exempt, noop, failed = [], [], [], []
    todo: list[str] = []
    for pid in _ordered_projects_for_track_seal(track):
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
    if todo:
        with ThreadPoolExecutor(max_workers=SEAL_SWEEP_WORKERS) as ex:
            futs = {ex.submit(_throttle_track_for_project, pid, track, usage): pid
                    for pid in todo}
            for fut in as_completed(futs):
                pid = futs[fut]
                try:
                    results[pid] = fut.result()
                except Exception as e:
                    print(f"[mass-seal] worker error {KNOWN_PROJECTS.get(pid, pid)}: {e}")
                    results[pid] = "failed"

    # One sequential retry for failures — a transient API blip mid-sweep used
    # to leave a project unsealed and burning post-cap (observed live 2026-08-13).
    for pid, r in list(results.items()):
        if r == "failed":
            print(f"[mass-seal] retrying {KNOWN_PROJECTS.get(pid, pid)}/{track}")
            results[pid] = _throttle_track_for_project(pid, track, usage)

    for pid, r in results.items():
        {"throttled": throttled, "noop": noop, "failed": failed}.get(r, failed).append(pid)

    print(f"[mass-seal] {track} → done. throttled={len(throttled)} "
          f"exempt={len(exempt)} noop={len(noop)} failed={len(failed)}")
    _log_event("mass_seal", track=track, consumed=consumed,
               throttled=[KNOWN_PROJECTS.get(p, p) for p in throttled],
               exempt=[KNOWN_PROJECTS.get(p, p) for p in exempt],
               failed=[KNOWN_PROJECTS.get(p, p) for p in failed])
    _broadcast(lambda n, t=track, th=len(throttled), ex=len(exempt),
        f=len(failed): fmt_seal_batch_done(t, th, ex, f, n), subs, names)


def _mass_unseal_track(track: str, usage: UsageStore, subs: SubscriberStore,
                       names: NameStore, *, reason: str = "manual") -> None:
    """Restore `track` across every currently-sealed project to the canonical
    baseline, marking each exempt so the auto-sweep won't re-seal today.
    REQUIRES: caller already holds the busy claim (`_try_claim_busy`)."""
    sealed = usage.get_sealed_tracks().get(track, {}).get("originals_by_project", {})
    pids   = list(sealed.keys())
    if not pids:
        return
    _broadcast(lambda n, t=track: fmt_unseal_batch_begin(t, n), subs, names)

    baseline = _compute_canonical_baseline(usage)
    restored, failed = 0, 0
    for pid in pids:
        result = _restore_track_for_project(pid, track, usage, baseline)
        if result == "restored":
            usage.add_track_exemption(pid, track)
            restored += 1
        elif result == "failed":
            failed += 1
    print(f"[mass-unseal] {track} → restored={restored} failed={failed} ({reason})")
    _log_event("mass_unseal", track=track, restored=restored, failed=failed, reason=reason)
    _broadcast(lambda n, t=track, r=restored, f=failed:
        fmt_unseal_batch_done(t, r, f, n), subs, names)


# ── Single-project manual seal / unseal (button-driven) ────────────────────

def _manual_seal_project(track: str, pid: str, usage: UsageStore,
                         subs: SubscriberStore, names: NameStore) -> str:
    """Throttle one project's `track` band to 0. Clears any exemption so the row
    stays sealed. Returns 'sealed' / 'noop' / 'failed'.
    REQUIRES: caller already holds the busy claim (`_try_claim_busy`)."""
    usage.remove_track_exemption(pid, track)
    if usage.is_project_track_sealed(pid, track):
        return "noop"
    result = _throttle_track_for_project(pid, track, usage)
    proj   = KNOWN_PROJECTS.get(pid, pid)
    if result == "throttled":
        print(f"[manual-seal] {proj}/{track}: sealed")
        _log_event("manual_seal", project=proj, track=track)
        _broadcast(lambda n, p=proj, t=track: fmt_manual_seal(p, t, n), subs, names)
        return "sealed"
    if result == "noop":
        return "noop"
    return "failed"


def _manual_unseal_project(track: str, pid: str, usage: UsageStore,
                           subs: SubscriberStore, names: NameStore) -> str:
    """Restore one project's `track` band and mark it exempt for the day.
    Returns 'unsealed' / 'noop' / 'failed'.
    REQUIRES: caller already holds the busy claim (`_try_claim_busy`)."""
    proj = KNOWN_PROJECTS.get(pid, pid)
    if not usage.is_project_track_sealed(pid, track):
        usage.add_track_exemption(pid, track)   # pre-exempt so sweep skips it
        return "noop"
    baseline = _compute_canonical_baseline(usage)
    result   = _restore_track_for_project(pid, track, usage, baseline)
    if result == "restored":
        usage.add_track_exemption(pid, track)
        print(f"[manual-unseal] {proj}/{track}: restored + exempt")
        _log_event("manual_unseal", project=proj, track=track)
        _broadcast(lambda n, p=proj, t=track: fmt_manual_unseal(p, t, n), subs, names)
        return "unsealed"
    if result == "noop":
        return "noop"
    return "failed"


# ── Auto 95% trigger (poll loop / refresh) ─────────────────────────────────

def _handle_track_seal(track: str, snap: dict, usage: UsageStore,
                       subs: SubscriberStore, names: NameStore) -> None:
    """Auto mass-seal entry: fired by the poll loop when a track crosses 95%.
    Idempotent via the per-day `mass_sealed_tracks` flag. Skips silently if another
    op already holds the busy claim — the next poll re-checks the threshold and
    retries (consumption only grows, so the trigger condition won't disappear)."""
    if usage.is_mass_sealed(track):
        return
    if not _try_claim_busy():
        print(f"[mass-seal] {track} deferred — another seal/unseal in progress")
        return
    try:
        consumed_key = "total_normal_tokens" if track == "normal" else "total_premium_tokens"
        _mass_seal_track(track, usage, subs, names,
                         consumed=snap.get(consumed_key, 0), respect_exemptions=True)
    finally:
        _release_busy()


# Per-(date, track) memo of projects already successfully repaired, so the gap
# check doesn't re-POST zeros to the same project every poll. In-memory only —
# a restart just costs one redundant repair pass.
_REPAIR_DONE: dict[tuple, set] = {}


def fmt_seal_repair(track: str, n: int, name: str = "Bach") -> str:
    return (
        f"🔧 <b>{_band_label(track)} — {n} straggler project(s) sealed.</b>\n"
        f"<i>The initial sweep left gaps (transient API failure); they are "
        f"throttled now, Monarch {name}.</i>"
    )


def _reseal_drifted_project(pid: str, track: str, usage: UsageStore) -> int:
    """Re-throttle rows of an already-'sealed' project that have drifted back to
    healthy values. Returns the number of rows re-zeroed (0 = no drift).

    Captures are MERGED, never overwritten: only some rows may have drifted, and
    replacing the capture list wholesale would discard the originals of rows
    still at 0. Caller must hold the busy claim."""
    rate_limits = _fetch_project_rate_limits(pid)
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
        if payload and _update_project_rate_limit(pid, rl["id"], payload):
            rezeroed += 1
            time.sleep(0.05)
    return rezeroed


def _repair_seal_gaps(track: str, usage: UsageStore, subs: SubscriberStore,
                      names: NameStore) -> None:
    """Self-healing pass, run every poll while a track is mass-sealed and over
    threshold. Two failure modes, both observed live:

    1. GAP — a project missing from `sealed_tracks` because its seal failed
       mid-sweep (2026-08-13: 1/13 premium seals failed and that project kept
       burning post-cap for hours). Re-sealed, then memoized per (day, track).
    2. DRIFT — a project the bot BELIEVES is sealed whose rate limits are
       actually healthy again (2026-08-22: found via smoke test — state and
       reality disagreed and nothing ever noticed). Trusting state alone makes
       the bot confidently wrong, so believed-sealed projects are VERIFIED
       against the live API and re-zeroed on drift. Never memoized: drift can
       recur at any time.
    """
    sealed = usage.get_sealed_tracks().get(track, {}).get("originals_by_project", {})
    memo_key = (today_str(), track)
    done = _REPAIR_DONE.setdefault(memo_key, set())
    candidates = [pid for pid in KNOWN_PROJECTS if not usage.is_exempt(pid, track)]
    gaps    = [p for p in candidates if p not in sealed and p not in done]
    believed = [p for p in candidates if p in sealed]
    if not gaps and not believed:
        return
    if not _try_claim_busy():
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
        if repaired or drifted:
            if repaired:
                print(f"[seal-repair] {track}: sealed gaps -> "
                      f"{', '.join(KNOWN_PROJECTS.get(p, p) for p in repaired)}")
            if drifted:
                print(f"[seal-repair] {track}: re-sealed DRIFTED -> "
                      f"{', '.join(KNOWN_PROJECTS.get(p, p) for p in drifted)}")
            _log_event("seal_repair", track=track,
                       repaired=[KNOWN_PROJECTS.get(p, p) for p in repaired],
                       drifted=[KNOWN_PROJECTS.get(p, p) for p in drifted])
            _broadcast(lambda n, t=track, c=len(repaired) + len(drifted):
                fmt_seal_repair(t, c, n), subs, names)
    finally:
        _release_busy()


def _process_pending_track_unseals(usage: UsageStore, subs: SubscriberStore,
                                   names: NameStore) -> None:
    """At day rollover, sealed_tracks moves into pending_track_unseal. This drains
    it: each project under each track is restored to the canonical baseline.
    Failures stay in the queue and retry next poll. Concise begin/done broadcast.
    Skips silently if the busy claim can't be obtained — the queue persists so the
    next poll cycle picks up where this one left off."""
    pending = usage.get_pending_track_unseal()
    if not pending:
        return
    if sum(len(i.get("originals_by_project", {})) for i in pending.values()) == 0:
        return

    if not _try_claim_busy():
        print("[pending-track-unseal] deferred — another seal/unseal in progress")
        return
    try:
        tracks_str = " & ".join(sorted(pending.keys()))
        _broadcast(lambda n, t=tracks_str: fmt_unseal_batch_begin(t, n), subs, names)

        baseline = _compute_canonical_baseline(usage)
        restored, failed_p = 0, 0
        for track, info in pending.items():
            obp = info.get("originals_by_project", {})
            print(f"[pending-track-unseal] {track} → {len(obp)} project(s)")
            for pid, originals in list(obp.items()):
                if not originals:
                    usage.pop_pending_track_project(track, pid); continue
                failed = _restore_rate_limits(pid, originals, baseline=baseline)
                if failed:
                    failed_p += 1
                    print(f"[pending-track-unseal] {KNOWN_PROJECTS.get(pid, pid)}/{track}: "
                          f"{failed}/{len(originals)} failed — will retry next poll")
                    continue
                usage.pop_pending_track_project(track, pid)
                restored += 1

        _log_event("pending_unseal", tracks=tracks_str, restored=restored, failed=failed_p)
        _broadcast(lambda n, r=restored, f=failed_p, t=tracks_str:
            fmt_unseal_batch_done(t, r, f, n), subs, names)
    finally:
        _release_busy()


# ── Formatters — seal/unseal alerts ────────────────────────────────────────

def _band_label(track: str) -> str:
    if track == "normal":
        return "Normal (10M)"
    if track == "premium":
        return "Premium (1M)"
    return "Quarantine (all models)"   # QUARANTINE_TRACK


def fmt_manual_seal(proj_name: str, track: str, name: str = "Bach") -> str:
    return (
        f"🔒 <b>{proj_name} — {_band_label(track)} sealed.</b>\n"
        f"<i>Rate limits throttled to 0. Auto-restore at UTC midnight. "
        f"Order carried out, Monarch {name}.</i>"
    )


def fmt_manual_unseal(proj_name: str, track: str, name: str = "Bach") -> str:
    return (
        f"🔓 <b>{proj_name} — {_band_label(track)} unsealed.</b>\n"
        f"<i>Restored and exempt from auto-seal until UTC midnight. "
        f"The overcap alarm still fires if it burns past the cap. Monarch {name}.</i>"
    )


def fmt_seal_batch_begin(track: str, consumed: int, cap: int, name: str = "Bach") -> str:
    pct  = consumed / cap * 100 if cap else 100
    band = "Normal (10M)" if track == "normal" else "Premium (1M)"
    return (
        f"🛑 <b>{band} hit {pct:.0f}% — begin sealing all projects…</b>\n"
        f"<i>Throttling {track}-band rate limits to 0. Stand by, Monarch {name}.</i>"
    )


def fmt_seal_batch_done(track: str, throttled: int, exempt: int, failed: int,
                        name: str = "Bach") -> str:
    band = "Normal (10M)" if track == "normal" else "Premium (1M)"
    tail = f"  ({exempt} exempt)" if exempt else ""
    warn = f"  ⚠️ {failed} failed" if failed else ""
    return (
        f"🔒 <b>Done sealing {band}.</b> {throttled} project(s) throttled{tail}{warn}.\n"
        f"<i>Auto-restore at UTC midnight. Exempt one: "
        f"<code>@bot archive unseal &lt;project&gt; {track}</code></i>"
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

def fmt_daily_snapshot(snap: dict) -> str:
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
        f"🔢 Tokens: <b>{_fmt_tokens(total_tok)}</b>   💰 Cost: <b>${total_cost:.4f}</b> / ${DAILY_LIMIT:.2f}{cost_note}"
    )
    lines.extend(_fmt_lane_lines(total_premium, total_normal, snap.get("lane_costs")))
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

    normal_crossed  = [(t, l) for t, l in TOKEN_MILESTONES         if total_normal  >= t]
    premium_crossed = [(t, l) for t, l in PREMIUM_TOKEN_MILESTONES if total_premium >= t]

    # Atomic claim — only the winning caller gets True and broadcasts.
    if not usage.seed_state(
        normal_thresholds  = [t for t, _ in normal_crossed],
        premium_thresholds = [t for t, _ in premium_crossed],
    ):
        return

    if normal_crossed and subs:
        t, l = normal_crossed[-1]   # highest crossed
        _broadcast(lambda n, t=t, c=total_normal, l=l: fmt_token_milestone(t, c, l, n), subs, names)

    if premium_crossed and subs:
        t, l = premium_crossed[-1]
        _broadcast(lambda n, t=t, c=total_premium, l=l: fmt_premium_token_milestone(t, c, l, n), subs, names)


def fmt_spend_milestone(threshold: float, current: float, level: str, name: str = "Bach") -> str:
    """Org-wide cumulative spend milestone."""
    if level == "casual":
        return (
            f"💰 <b>Spend Milestone — ${threshold:.2f}</b>\n\n"
            f"Cumulative outlay today: <b>${current:.4f}</b> of ${DAILY_LIMIT:.2f}.\n"
            f"<i>The treasury is monitored, Monarch {name}.</i>"
        )
    if level == "urgent":
        return (
            f"⚠️ <b>Significant Spend — ${threshold:.2f}</b>\n\n"
            f"Today's expenditure stands at <b>${current:.4f}</b> of ${DAILY_LIMIT:.2f}.\n"
            f"The daily cap is approaching. Your attention is advised, My Liege {name}."
        )
    # cap (DAILY_LIMIT)
    return (
        f"🚨 <b>Daily Spend Cap Breached — ${current:.4f}</b>\n\n"
        f"The ${DAILY_LIMIT:.2f} daily expenditure cap has been crossed.\n"
        f"Polling escalated to <b>AGGRESSIVE</b>. Use <code>@bot archive</code> to seal "
        f"projects manually — note that unlisted models (embeddings, image, audio, etc.) "
        f"bypass the seal logic and must be stopped at the source.\n\n"
        f"Monarch {name}, the operation demands your command."
    )


def fmt_project_spend(pid: str, threshold: float, current: float, name: str = "Bach") -> str:
    proj = KNOWN_PROJECTS.get(pid, pid)
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
    proj = KNOWN_PROJECTS.get(pid, pid)
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
    `cap_crossed` is True the FIRST poll that observes total ≥ DAILY_LIMIT
    (after that, the milestone is in the notified set and won't re-fire).
    Cost data has a 5-10 min OpenAI ingestion lag — alerts may arrive slightly
    delayed, but that's still vastly better than the previous "never" state."""
    total_cost = snap.get("total_cost", 0.0) or 0.0
    hit = False
    cap_crossed = False

    # ── Org-wide spend milestones ───────────────────────────────────────────
    notified = usage.get_spend_milestones_notified()
    for threshold, level in SPEND_MILESTONES:
        if total_cost >= threshold and threshold not in notified:
            hit = True
            usage.add_spend_milestone_notified(threshold)
            _broadcast(lambda n, t=threshold, c=total_cost, l=level:
                fmt_spend_milestone(t, c, l, n), subs, names)
            if level == "cap":
                cap_crossed = True

    # ── Overcap escalation — never go silent while the bleed continues ──────
    # Every extra SPEND_OVERCAP_STEP past the cap fires another cap-level alert
    # ($2.50, $3.00, …). Dynamic thresholds share the same notified list; they
    # never collide with the static ones (all static thresholds ≤ DAILY_LIMIT).
    if total_cost > DAILY_LIMIT:
        steps = int((total_cost - DAILY_LIMIT) / SPEND_OVERCAP_STEP)
        for i in range(1, steps + 1):
            threshold = round(DAILY_LIMIT + i * SPEND_OVERCAP_STEP, 2)
            if threshold not in notified:
                hit = True
                cap_crossed = True
                usage.add_spend_milestone_notified(threshold)
                _broadcast(lambda n, t=threshold, c=total_cost:
                    fmt_spend_milestone(t, c, "cap", n), subs, names)

    # ── Per-project spend thresholds ────────────────────────────────────────
    for pid, p in snap.get("projects", {}).items():
        cost = float(p.get("cost_usd", 0.0) or 0.0)
        if cost <= 0:
            continue
        proj_notified = usage.get_project_spend_notified(pid)
        for threshold in PROJECT_SPEND_THRESHOLDS:
            if cost >= threshold and threshold not in proj_notified:
                hit = True
                usage.add_project_spend_notified(pid, threshold)
                _broadcast(lambda n, p=pid, t=threshold, c=cost:
                    fmt_project_spend(p, t, c, n), subs, names)

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
                fmt_unlisted_model(pi, mo, r, t, c, n), subs, names)
            fired = True
            print(f"[unlisted-alert] {KNOWN_PROJECTS.get(pid, pid)}/{model} "
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


def fmt_quarantine(proj_name: str, models: list, name: str = "Bach") -> str:
    ms = ", ".join(f"<code>{html.escape(m)}</code>" for m in models[:5])
    return (
        f"☣️ <b>{proj_name} — QUARANTINED.</b>\n\n"
        f"Off-watchlist model usage detected: {ms}\n"
        f"Unlisted models bill at standard rates from the first token and cannot "
        f"be selectively throttled — <b>every</b> rate limit of this project is "
        f"now 0 until UTC midnight.\n"
        f"<i>Release: <code>@bot archive</code> → Unseal → Both → project. "
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
    rate_limits = _fetch_project_rate_limits(pid)
    if rate_limits is None:
        return "failed"
    if not rate_limits:
        return "noop"
    # Capture only healthy rows — rows already at 0 (e.g. track-sealed earlier
    # today) stay owned by their existing capture; recording them here would
    # re-create the 0/0 cascade on restore.
    originals = _capture_originals(
        [rl for rl in rate_limits
         if rl.get("max_requests_per_1_minute") or rl.get("max_tokens_per_1_minute")]
    )
    throttled_ids: list[str] = []
    for rl in rate_limits:
        payload = _seal_payload(rl)
        if not payload:
            continue   # row exposes no settable fields
        if _update_project_rate_limit(pid, rl["id"], payload):
            throttled_ids.append(rl["id"])
            time.sleep(0.05)
        else:
            _restore_rate_limits(pid, [o for o in originals if o["id"] in throttled_ids])
            return "failed"
    if originals:
        usage.add_track_originals(QUARANTINE_TRACK, pid, originals)
        return "sealed"
    return "noop"


def _quarantine_unlisted_users(snap: dict, usage: UsageStore, subs: SubscriberStore,
                               names: NameStore = None) -> None:
    """Auto-seal any KNOWN project that touched an off-watchlist model today —
    even once. Runs every poll: sealed_tracks['full'] presence is the dedup,
    an exemption on 'full' (set when the user releases the quarantine) is the
    opt-out, and failures simply retry next poll."""
    offenders: dict[str, list] = {}
    for pid, p in snap.get("projects", {}).items():
        if pid not in KNOWN_PROJECTS:
            continue
        bad = [m for m, mm in p.get("models", {}).items()
               if _track_for_model(m) is None
               and (mm.get("requests", 0) or mm.get("input", 0) + mm.get("output", 0))]
        if bad:
            offenders[pid] = bad

    day = today_str()
    todo = [pid for pid in offenders
            if not usage.is_project_track_sealed(pid, QUARANTINE_TRACK)
            and not usage.is_exempt(pid, QUARANTINE_TRACK)
            and (day, pid) not in _QUARANTINE_NOOP]
    if not todo:
        return
    if not _try_claim_busy():
        print("[quarantine] deferred — another seal/unseal in progress")
        return
    try:
        for pid in todo:
            proj   = KNOWN_PROJECTS.get(pid, pid)
            result = _full_seal_project(pid, usage)
            if result == "sealed":
                print(f"[quarantine] {proj} sealed (models: {offenders[pid]})")
                _log_event("quarantine", project=proj, models=offenders[pid])
                _broadcast(lambda n, p=proj, ms=offenders[pid]:
                    fmt_quarantine(p, ms, n), subs, names)
            elif result == "noop":
                _QUARANTINE_NOOP.add((day, pid))
            else:
                print(f"[quarantine] {proj} failed — will retry next poll")
    finally:
        _release_busy()


def _release_quarantine(pid: str, usage: UsageStore, subs: SubscriberStore,
                        names: NameStore) -> str:
    """Restore a quarantined project's rows and exempt it from re-quarantine
    for the rest of the UTC day. Caller must hold the busy claim."""
    proj = KNOWN_PROJECTS.get(pid, pid)
    if not usage.is_project_track_sealed(pid, QUARANTINE_TRACK):
        usage.add_track_exemption(pid, QUARANTINE_TRACK)   # opt out of re-quarantine
        return "noop"
    baseline = _compute_canonical_baseline(usage)
    result   = _restore_track_for_project(pid, QUARANTINE_TRACK, usage, baseline)
    if result == "restored":
        usage.add_track_exemption(pid, QUARANTINE_TRACK)
        print(f"[quarantine] {proj} released + exempt")
        _log_event("quarantine_release", project=proj)
        _broadcast(lambda n, p=proj: fmt_quarantine_release(p, n), subs, names)
        return "unsealed"
    return result


def seed_spend(snap: dict, usage: UsageStore, subs: SubscriberStore,
               names: NameStore = None) -> None:
    """First-of-day spend seed: mark every already-crossed threshold as notified
    so we don't flood the chat on bot restart, but fire the HIGHEST one as a
    catch-up so the user sees today's true position. Atomic via claim_spend_seed."""
    if not usage.claim_spend_seed():
        return

    total_cost = snap.get("total_cost", 0.0) or 0.0

    # Mark every crossed org threshold as notified — silent — then fire only the
    # highest as a catch-up broadcast (mirrors token seed_milestones pattern).
    # Includes the dynamic overcap steps ($2.50, $3.00, …) so a restart at $3.40
    # doesn't flood every step in one burst on the next poll.
    crossed = [(t, l) for t, l in SPEND_MILESTONES if total_cost >= t]
    if total_cost > DAILY_LIMIT:
        steps = int((total_cost - DAILY_LIMIT) / SPEND_OVERCAP_STEP)
        for i in range(1, steps + 1):
            crossed.append((round(DAILY_LIMIT + i * SPEND_OVERCAP_STEP, 2), "cap"))
    for t, _ in crossed:
        usage.add_spend_milestone_notified(t)
    if crossed and subs:
        t, l = crossed[-1]
        _broadcast(lambda n, t=t, c=total_cost, l=l: fmt_spend_milestone(t, c, l, n),
                   subs, names)

    # Per-project: mark crossed silently (no catch-up broadcast — could be many).
    for pid, p in snap.get("projects", {}).items():
        cost = float(p.get("cost_usd", 0.0) or 0.0)
        for t in PROJECT_SPEND_THRESHOLDS:
            if cost >= t:
                usage.add_project_spend_notified(pid, t)


def check_milestones(snap: dict, usage: UsageStore, subs: SubscriberStore, names: NameStore = None) -> bool:
    """Called after every non-seed poll. Fires alerts for newly crossed thresholds.
    Returns True if at least one new milestone was hit (used to trigger urgent mode)."""
    hit = False

    # Normal band (10M free daily)
    total_tok = snap.get("total_normal_tokens", 0)
    notified  = usage.get_milestones_notified()
    for threshold, level in TOKEN_MILESTONES:
        if total_tok >= threshold and threshold not in notified:
            hit = True
            usage.add_milestone_notified(threshold)
            _broadcast(lambda n, t=threshold, c=total_tok, l=level: fmt_token_milestone(t, c, l, n), subs, names)

    # Premium band (1M free daily)
    total_premium    = snap.get("total_premium_tokens", 0)
    notified_premium = usage.get_premium_milestones_notified()
    for threshold, level in PREMIUM_TOKEN_MILESTONES:
        if total_premium >= threshold and threshold not in notified_premium:
            hit = True
            usage.add_premium_milestone_notified(threshold)
            _broadcast(lambda n, t=threshold, c=total_premium, l=level: fmt_premium_token_milestone(t, c, l, n), subs, names)

    return hit


# ── Command handlers ───────────────────────────────────────────────────────

def cmd_tokens(usage: UsageStore, name: str = "Bach") -> str:
    snap    = usage.get()
    projects = snap.get("projects", {})
    active  = {pid: p for pid, p in projects.items() if p.get("total_tokens", 0) > 0}
    if not active:
        return f"No tokens consumed today, Monarch {name}."

    total_tok = sum(p.get("total_tokens", 0) for p in active.values())
    total_req = sum(p.get("num_requests", 0) for p in active.values())
    lines = [f"🔢 <b>Token Report — {snap.get('date', today_str())}</b>\n"]

    for pid, p in sorted(active.items(), key=lambda x: x[1].get("total_tokens", 0), reverse=True):
        inp  = _fmt_tokens(p.get("input_tokens", 0))
        out  = _fmt_tokens(p.get("output_tokens", 0))
        tot  = _fmt_tokens(p.get("total_tokens", 0))
        reqs = p.get("num_requests", 0)
        lines.append(f"🔹 <b>{p['name']}</b>  {tot}  ({reqs:,} reqs)")
        lines.append(f"   ↳ in: {inp}  /  out: {out}")
        for model, m in sorted(p.get("models", {}).items(),
                               key=lambda x: x[1].get("input", 0) + x[1].get("output", 0),
                               reverse=True):
            mi = _fmt_tokens(m.get("input", 0))
            mo = _fmt_tokens(m.get("output", 0))
            mr = m.get("requests", 0)
            lines.append(f"   <code>{html.escape(model)}</code>  {mi} in / {mo} out  ({mr:,} reqs)")
        lines.append("")

    total_premium = snap.get("total_premium_tokens", 0)
    total_normal  = snap.get("total_normal_tokens",  0)
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(
        f"🔢 Total: <b>{_fmt_tokens(total_tok)}</b>  •  {total_req:,} requests"
    )
    lines.extend(_fmt_lane_lines(total_premium, total_normal, snap.get("lane_costs")))
    return "\n".join(lines)


def cmd_projects(usage: UsageStore, name: str = "Bach") -> str:
    snap      = _enrich_costs(usage.get(), usage)
    projects  = snap.get("projects", {})
    if not projects:
        return f"No project data on record, Monarch {name}."

    total_tok  = sum(p.get("total_tokens", 0) for p in projects.values())
    total_cost = snap.get("total_cost", 0.0)
    lines = [f"🗂️ <b>Project Roster — {snap.get('date', today_str())}</b>\n"]

    for pid, p in sorted(projects.items(), key=lambda x: x[1].get("total_tokens", 0), reverse=True):
        tok  = _fmt_tokens(p.get("total_tokens", 0))
        cost = p.get("cost_usd", 0.0)
        pct  = int(p.get("total_tokens", 0) / max(total_tok, 1) * 10)
        bar  = "█" * pct + "░" * (10 - pct)
        lines.append(f"• <b>{p['name']}</b>\n  [{bar}] {tok}  /  ${cost:.4f}")

    lines.append(f"\n🔢 <b>{_fmt_tokens(total_tok)}</b> tokens   💰 <b>${total_cost:.4f}</b>")
    return "\n".join(lines)


def cmd_spending(name: str = "Bach") -> str:
    """Monthly bill — current month + previous month, fetched live."""
    now        = datetime.now(timezone.utc)
    cy, cm     = now.year, now.month
    py, pm     = prev_month()

    curr_costs = _fetch_monthly_costs(cy, cm)
    prev_costs = _fetch_monthly_costs(py, pm)
    curr_total = sum(curr_costs.values())
    prev_total = sum(prev_costs.values())

    lines = ["💰 <b>Monthly Expenditure Report</b>\n"]

    def _section(label: str, costs: dict, total: float):
        lines.append(f"<b>── {label} ──</b>")
        active = {pid: v for pid, v in costs.items() if v > 0.0}
        if active:
            for pid, cost in sorted(active.items(), key=lambda x: x[1], reverse=True):
                proj_label = "Unattributed" if pid == "__org__" else KNOWN_PROJECTS.get(pid, pid)
                lines.append(f"  • {proj_label}: <b>${cost:.4f}</b>")
        else:
            lines.append("  No spend recorded.")
        lines.append(f"  Total: <b>${total:.4f}</b>\n")

    _section(_fmt_month(cy, cm) + " (current)", curr_costs, curr_total)
    _section(_fmt_month(py, pm) + " (previous)", prev_costs, prev_total)

    lines.append(f"<i>Monarch {name}, your accounts are presented in full.</i>")
    return "\n".join(lines)


def cmd_rank(usage: UsageStore, name: str = "Bach") -> str:
    snap     = usage.get()
    projects = snap.get("projects", {})
    active   = {pid: p for pid, p in projects.items()
                if p.get("total_tokens", 0) > 0 or p.get("cost_usd", 0) > 0}
    if not active:
        return f"No data to rank, Monarch {name}."

    medals = {0: "🥇", 1: "🥈", 2: "🥉"}
    lines  = [f"🏆 <b>Project Rankings — {snap.get('date', today_str())}</b>\n"]

    lines.append("<b>By Token Consumption</b>")
    for i, (pid, p) in enumerate(
            sorted(active.items(), key=lambda x: x[1].get("total_tokens", 0), reverse=True)):
        m   = medals.get(i, f"  {i+1}.")
        tok = _fmt_tokens(p.get("total_tokens", 0))
        lines.append(f"{m} <b>{p['name']}</b>  —  {tok}")

    lines.append("\n<b>By Daily Spend</b>")
    for i, (pid, p) in enumerate(
            sorted(active.items(), key=lambda x: x[1].get("cost_usd", 0), reverse=True)):
        m    = medals.get(i, f"  {i+1}.")
        cost = p.get("cost_usd", 0.0)
        lines.append(f"{m} <b>{p['name']}</b>  —  ${cost:.4f}")

    lines.append(f"\n<i>{name} the Monarch, the standings are clear.</i>")
    return "\n".join(lines)


def cmd_active(usage: UsageStore, name: str = "Bach") -> str:
    active = usage.get_active_projects()
    window = usage.get_active_window_mins()
    lines  = [f"⚡ <b>Active Projects (last {window} min)</b>\n"]

    if not active:
        lines.append(f"No API activity detected in the last {window} minutes.")
    else:
        for pid, count in sorted(active.items(), key=lambda x: x[1], reverse=True):
            proj_name = KNOWN_PROJECTS.get(pid, pid)
            lines.append(f"• <b>{proj_name}</b>  —  {count:,} requests")

        if len(active) >= CONCURRENCY_THRESHOLD:
            lines.append(f"\n⚠️ <b>{len(active)} projects active simultaneously.</b>")
        else:
            lines.append(f"\n{len(active)} project(s) active. No concurrency threshold reached.")

    lines.append(f"\n<i>This snapshot reflects the last concurrency check, Monarch {name}.</i>")
    return "\n".join(lines)


# ── Archive (seal/unseal) — interactive button UI ──────────────────────────
#
# Flow:  archive  →  [Seal] [Unseal] [Cancel]
#          → action chosen → [Normal] [Premium] [Both] [Cancel]   (the "mode")
#            → mode chosen → project buttons + [ALL] [Cancel]
#              → project chosen → applies seal/unseal, re-renders the status
#
# Callback data is compact: "arch:<action>:<mode>:<pidx>"
#   action ∈ {menu, seal, unseal, cancel}
#   mode   ∈ {-, normal, premium, both}
#   pidx   = index into _PROJECT_INDEX, or "all", or "-"
# A stable index list keeps callback_data within Telegram's 64-byte cap.

_PROJECT_INDEX = list(KNOWN_PROJECTS.keys())   # stable order for callback ids


def _archive_tracks_for_mode(mode: str) -> tuple:
    return ("normal", "premium") if mode == "both" else (mode,)


def _kb_archive_root() -> list:
    return [[
        {"text": "🔒 Seal",   "callback_data": "arch:seal:-:-"},
        {"text": "🔓 Unseal", "callback_data": "arch:unseal:-:-"},
        {"text": "✖ Cancel",  "callback_data": "arch:cancel:-:-"},
    ]]


def _kb_archive_mode(action: str) -> list:
    return [
        [
            {"text": "📦 Normal",  "callback_data": f"arch:{action}:normal:-"},
            {"text": "⭐ Premium", "callback_data": f"arch:{action}:premium:-"},
        ],
        [
            {"text": "🔱 Both",    "callback_data": f"arch:{action}:both:-"},
            {"text": "✖ Cancel",  "callback_data": "arch:cancel:-:-"},
        ],
    ]


def _kb_archive_projects(action: str, mode: str, usage: UsageStore) -> list:
    """One button per project, annotated with its current state for this mode.
    Two columns. Trailing row: [ALL] [Cancel]."""
    sealed_tracks = usage.get_sealed_tracks()
    exemptions    = usage.get_track_exemptions()
    tracks        = _archive_tracks_for_mode(mode)

    def _mark(pid: str) -> str:
        sealed = any(pid in sealed_tracks.get(t, {}).get("originals_by_project", {}) for t in tracks)
        exempt = any(t in exemptions.get(pid, []) for t in tracks)
        if action == "seal":
            return "🔒" if sealed else ("🔓" if exempt else "•")
        return "🔒" if sealed else "·"   # unseal view: highlight what's sealable

    rows, row = [], []
    for idx, pid in enumerate(_PROJECT_INDEX):
        label = f"{_mark(pid)} {KNOWN_PROJECTS.get(pid, pid)}"
        row.append({"text": label, "callback_data": f"arch:{action}:{mode}:{idx}"})
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([
        {"text": f"🟥 ALL projects", "callback_data": f"arch:{action}:{mode}:all"},
        {"text": "✖ Cancel",         "callback_data": "arch:cancel:-:-"},
    ])
    return rows


def cmd_archive(usage: UsageStore, name: str = "Bach") -> tuple:
    """Entry point for the @bot archive command. Returns (text, keyboard).
    The text is the live status; the keyboard offers Seal / Unseal / Cancel."""
    return _fmt_archive_status(usage, name), _kb_archive_root()


def _spawn_archive_worker(work_fn, after_kb_fn, chat_id: str, msg_id: int,
                          usage: UsageStore, name: str) -> None:
    """Run `work_fn` in a daemon thread, then edit the originating message with
    the fresh archive status and the keyboard returned by `after_kb_fn()`. The
    busy claim MUST already be held by the caller — this worker releases it.
    Backgrounding lets the Telegram poll loop keep handling other commands while
    a 30–50 s mass-seal runs."""
    def _runner():
        try:
            work_fn()
            if msg_id is not None:
                _edit_message(_fmt_archive_status(usage, name),
                              chat_id, msg_id, keyboard=after_kb_fn())
        except Exception as e:
            print(f"[archive worker error] {e}")
        finally:
            _release_busy()
    threading.Thread(target=_runner, daemon=True).start()


def handle_archive_callback(data: str, usage: UsageStore, subs: SubscriberStore,
                            names: NameStore, name: str, chat_id: str,
                            msg_id: int) -> tuple:
    """Process an 'arch:...' callback. Returns (text, keyboard, toast).
    Navigation paths return immediately. Heavy seal/unseal work is dispatched to
    a background thread so the Telegram poll loop is never blocked. The toast +
    the placeholder ("🔄 Working…") message go out immediately; the worker edits
    the message again when the API work completes."""
    try:
        _, action, mode, pidx = data.split(":", 3)
    except ValueError:
        return None, None, "Malformed action."

    # Defensive: validate enum-like fields before using them.
    if action not in {"cancel", "menu", "seal", "unseal"}:
        return None, None, "Unknown action."
    if mode not in {"-", "normal", "premium", "both"}:
        return None, None, "Unknown mode."

    if action == "cancel":
        return _fmt_archive_status(usage, name), None, "Cancelled."

    if action == "menu":
        return _fmt_archive_status(usage, name), _kb_archive_root(), None

    if action in ("seal", "unseal") and mode == "-":
        # Action chosen → ask for the mode (track scope).
        verb = "seal" if action == "seal" else "unseal"
        return (f"{_fmt_archive_status(usage, name)}\n\n"
                f"<b>Choose a track to {verb}:</b>",
                _kb_archive_mode(action), None)

    if action in ("seal", "unseal") and mode in ("normal", "premium", "both") and pidx == "-":
        # Mode chosen → show project picker.
        verb = "Seal" if action == "seal" else "Unseal"
        return (f"{_fmt_archive_status(usage, name)}\n\n"
                f"<b>{verb} — {mode} — pick a project:</b>",
                _kb_archive_projects(action, mode, usage), None)

    if action in ("seal", "unseal") and mode in ("normal", "premium", "both"):
        # Project (or ALL) chosen → apply. Refuse if an op is already running.
        # Atomic claim closes the prior TOCTOU race (check + acquire were separate).
        if not _try_claim_busy():
            return None, None, "A seal/unseal is already running — try again shortly."
        tracks = _archive_tracks_for_mode(mode)

        if pidx == "all":
            def _work():
                for t in tracks:
                    if action == "seal":
                        _mass_seal_track(t, usage, subs, names, respect_exemptions=False)
                    else:
                        _mass_unseal_track(t, usage, subs, names, reason="manual all")
                if action == "unseal" and mode == "both":
                    # "Both" is the full-release gesture — lift quarantines too.
                    _mass_unseal_track(QUARANTINE_TRACK, usage, subs, names,
                                       reason="manual all (quarantine)")
            _spawn_archive_worker(_work, _kb_archive_root, chat_id, msg_id, usage, name)
            placeholder = (f"{_fmt_archive_status(usage, name)}\n\n"
                           f"🔄 <i>Working on {action} ALL ({mode}) — watch chat for progress…</i>")
            return placeholder, [], f"Started {action} ALL ({mode})."

        # Single project
        try:
            pid = _PROJECT_INDEX[int(pidx)]
        except (ValueError, IndexError):
            _release_busy()
            return None, None, "Unknown project."
        proj = KNOWN_PROJECTS.get(pid, pid)
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
        _spawn_archive_worker(_work,
                              lambda: _kb_archive_projects(action, mode, usage),
                              chat_id, msg_id, usage, name)
        placeholder = (f"{_fmt_archive_status(usage, name)}\n\n"
                       f"🔄 <i>Working on {action} {proj} ({mode})…</i>")
        return placeholder, [], f"Started {action} {proj} ({mode})."

    return None, None, "Unknown action."


def _fmt_archive_status(usage: UsageStore, name: str = "Bach") -> str:
    snap           = usage.get()
    sealed_tracks  = usage.get_sealed_tracks()
    exemptions     = usage.get_track_exemptions()
    pending_tracks = usage.get_pending_track_unseal()

    n_tok = snap.get("total_normal_tokens",  0)
    p_tok = snap.get("total_premium_tokens", 0)
    lane_costs = snap.get("lane_costs") or {}

    lines = [f"🗃️ <b>Archive — {snap.get('date', today_str())}</b>\n"]

    lines.append("<b>Tracks</b>")
    for track, consumed, cap, threshold in (
        ("normal",  n_tok, TOKEN_HARD_CAP,         NORMAL_TRACK_SEAL_THRESHOLD),
        ("premium", p_tok, PREMIUM_TOKEN_HARD_CAP, PREMIUM_TRACK_SEAL_THRESHOLD),
    ):
        pct = consumed / cap * 100 if cap else 0
        n_sealed = len(sealed_tracks.get(track, {}).get("originals_by_project", {}))
        if n_sealed:
            tag = f"🔒 {n_sealed} sealed"
        elif consumed >= threshold:
            tag = f"⚠️ ≥{threshold / cap * 100:.0f}% (not sealed)"
        else:
            tag = "✅ active"
        lines.append(f"  • <b>{track}</b>: {_fmt_tokens(consumed)} / {_fmt_tokens(cap)} "
                     f"({pct:.1f}%). Cost: {_fmt_cost(lane_costs.get(track))}  —  {tag}")
    lines.append(f"  • <b>exotic</b>: {_fmt_cost(lane_costs.get('exotic'))} "
                 f"(off-watchlist spend — no free allowance)")
    lines.append("")

    lines.append("<b>Projects</b>")
    for pid, proj_name in sorted(KNOWN_PROJECTS.items(), key=lambda kv: kv[1]):
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
    lines.append("")
    lines.append("<i>🔒=sealed ☣️=quarantined 🔓=exempt ⏳=restore-pending · "
                 "N=normal P=premium F=quarantine</i>")
    lines.append(f"<i>Monarch {name}, the archive registry is presented.</i>")
    return "\n".join(lines)


def cmd_refresh(usage: UsageStore, subs: SubscriberStore, names: NameStore = None, name: str = "Bach") -> str:
    snap = fetch_today_usage()
    if snap:
        usage.update(snap)
        # If the scheduled poll loop hasn't run its first seed yet, seed now.
        # Only the highest already-crossed milestone fires (no flood).
        if not usage.has_seeded():
            seed_milestones(snap, usage, subs, names)
            new_milestone = False
        else:
            new_milestone = check_milestones(snap, usage, subs, names)
        enriched      = _enrich_costs(snap, usage)
        total         = enriched.get("total_cost", 0.0)
        # Propagate the live cost back onto the snap so spend checks see fresh data
        # (snap from fetch_today_usage() has total_cost=0.0 — costs are overlaid here).
        snap["total_cost"] = total
        if enriched.get("lane_costs") is not None:
            snap["lane_costs"] = enriched["lane_costs"]
        for pid, p in enriched.get("projects", {}).items():
            if pid in snap.get("projects", {}):
                snap["projects"][pid]["cost_usd"] = p.get("cost_usd", 0.0)
        total_tok     = sum(p.get("total_tokens", 0) for p in snap.get("projects", {}).values())
        total_premium = snap.get("total_premium_tokens", 0)
        total_normal  = snap.get("total_normal_tokens",  0)

        # Spend monitoring on demand — same path as the poll loop.
        if not usage.has_spend_seeded():
            seed_spend(snap, usage, subs, names)
            new_spend, spend_cap_crossed = False, False
        else:
            new_spend, spend_cap_crossed = check_spend(snap, usage, subs, names)
        check_unlisted_models(snap, usage, subs, names)
        # Quarantine can full-seal up to 13 projects (~50-80 rate-limit rows each).
        # cmd_refresh runs on the TELEGRAM thread, so doing it inline would freeze
        # every command for minutes — the same freeze that was fixed for archive
        # buttons. Hand it to a daemon worker; it self-guards via the busy claim.
        _spawn_bg("quarantine/refresh", _quarantine_unlisted_users,
                  snap, usage, subs, names)

        mode             = usage.get_mode()
        mode_note        = ""
        normal_exceeded  = total_normal  >= TOKEN_HARD_CAP
        premium_exceeded = total_premium >= PREMIUM_TOKEN_HARD_CAP

        # Track-seal triggers — same logic as the poll loop, fired on demand so
        # /refresh near the threshold doesn't wait for the next poll. _handle_track_seal
        # is self-guarding (idempotent via the per-day mass_sealed flag).
        # Mass sweeps are backgrounded for the same reason as the quarantine above:
        # a sweep takes ~1 min and must never block the Telegram command loop.
        for _trk, _tok, _thr, _pct in (
            ("normal",  total_normal,  NORMAL_TRACK_SEAL_THRESHOLD,  95),
            ("premium", total_premium, PREMIUM_TRACK_SEAL_THRESHOLD, 85),
        ):
            if _tok >= _thr and not usage.is_mass_sealed(_trk):
                _spawn_bg(f"track-seal/{_trk}", _handle_track_seal,
                          _trk, snap, usage, subs, names)
                mode_note = (f"\n🛑 {_trk.title()} track passed {_pct}% — "
                             f"mass throttle started (watch chat for the summary).")

        if normal_exceeded or premium_exceeded:
            banded = _fetch_recent_activity_by_band(minutes=OVERCAP_WINDOW_MINS)
            if banded is None:
                mode_note = "\n⚠️ Budget cap exceeded — activity API unreachable, can't verify."
            else:
                illegal = _filter_to_exceeded_band(banded, normal_exceeded, premium_exceeded)
                if illegal:
                    usage.update_last_illegal_seen()
                    if mode != "aggressive":
                        usage.set_mode("aggressive")
                    _broadcast(lambda n, r=illegal, ne=normal_exceeded, pe=premium_exceeded:
                        fmt_overcap_active_alert(r, ne, pe, n), subs, names)
                    mode_note = "\n🔴 <b>AGGRESSIVE mode active — exempt projects burning the exhausted band, broadcast sent.</b>"
                else:
                    mode_note = "\n⚠️ Budget cap exceeded — no projects active on the exhausted band right now."
        elif spend_cap_crossed:
            usage.set_mode("aggressive")
            mode_note = f"\n🔴 <b>Spend cap (${DAILY_LIMIT:.2f}) crossed — AGGRESSIVE mode.</b>"
        elif (new_milestone or new_spend) and mode == "passive":
            usage.set_mode("urgent")
            usage.set_last_milestone_ts(time.time())
            mode_note = "\n📊 Milestone crossed — switched to <b>URGENT</b> polling mode."

        stale_note = f"  <i>(cost as of {_fmt_ts(enriched.get('costs_ts'))})</i>" if enriched.get("costs_stale") else ""
        lines = [
            "🔄 <b>Data refreshed.</b>",
            f"Tokens today: <b>{_fmt_tokens(total_tok)}</b>",
            *_fmt_lane_lines(total_premium, total_normal, enriched.get("lane_costs")),
            f"Spend today:  <b>${total:.4f}</b>{stale_note}",
            f"<i>Intelligence updated, Monarch {name}.</i>",
        ]
        if mode_note:
            lines.append(mode_note)
        return "\n".join(lines)
    # Token fetch failed — return last cached snapshot
    cached = usage.get()
    if cached and cached.get("projects"):
        enriched = _enrich_costs(cached, usage, live=False)
        return (
            "⚠️ <b>OpenAI API unreachable — showing last known data.</b>\n\n"
            + fmt_daily_snapshot(enriched)
        )
    return f"OpenAI API unreachable and no prior data on record, Monarch {name}."


def cmd_recent(name: str = "Bach") -> str:
    data           = _fetch_recent_data(31)
    proj_costs     = data.get("proj_costs", {})
    total_tokens   = data.get("total_tokens", 0)
    total_requests = data.get("total_requests", 0)
    start_date     = data.get("start_date", "")
    end_date       = data.get("end_date", "")

    org_cost   = proj_costs.pop("__org__", 0.0)
    total_cost = round(sum(proj_costs.values()) + org_cost, 4)

    if not proj_costs and not total_tokens:
        return f"No usage recorded in the last 31 days, Monarch {name}."

    lines = [f"📊 <b>Usage — Last 31 Days</b>  <i>({start_date} → {end_date})</i>\n"]

    active = {pid: v for pid, v in proj_costs.items() if v > 0.0}
    if active:
        for pid, cost in sorted(active.items(), key=lambda x: x[1], reverse=True):
            label = KNOWN_PROJECTS.get(pid, pid)
            lines.append(f"🔹 <b>{label}</b>   ${cost:.4f}")
        if org_cost > 0.0:
            lines.append(f"🔹 <b>Unattributed</b>   ${org_cost:.4f}")
        lines.append("")
    else:
        lines.append("No spend recorded in this period.\n")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(
        f"🔢 Tokens: <b>{_fmt_tokens(total_tokens)}</b>   "
        f"📨 Requests: <b>{total_requests:,}</b>   "
        f"💰 Cost: <b>${total_cost:.4f}</b>"
    )
    lines.append(f"\n<i>Monarch {name}, the 31-day record is presented.</i>")
    return "\n".join(lines)


def cmd_models(usage: UsageStore, name: str = "Bach") -> str:
    snap = usage.get()
    agg: dict[str, dict] = {}
    for p in snap.get("projects", {}).values():
        for model, m in p.get("models", {}).items():
            e = agg.setdefault(model, {"input": 0, "output": 0, "requests": 0})
            e["input"]    += m.get("input", 0)
            e["output"]   += m.get("output", 0)
            e["requests"] += m.get("requests", 0)

    if not agg:
        return f"No model data on record today, Monarch {name}."

    total_tok = sum(e["input"] + e["output"] for e in agg.values()) or 1
    lines     = [f"🤖 <b>Model Usage — {snap.get('date', today_str())}</b>\n"]

    for model, e in sorted(agg.items(), key=lambda x: x[1]["input"] + x[1]["output"], reverse=True):
        inp  = _fmt_tokens(e["input"])
        out  = _fmt_tokens(e["output"])
        tot  = _fmt_tokens(e["input"] + e["output"])
        reqs = e["requests"]
        pct  = int((e["input"] + e["output"]) / total_tok * 100)
        lines.append(
            f"🔹 <code>{html.escape(model)}</code>\n"
            f"   {tot}  ({inp} in / {out} out)  •  {reqs:,} reqs  •  {pct}%"
        )

    lines.append(f"\n<i>Monarch {name}, all models are accounted for.</i>")
    return "\n".join(lines)


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
    return f"Acknowledged. I will address you as <b>{new_name.strip()}</b>."


def cmd_help(bot_username: Optional[str], name: str = "Bach") -> str:
    m = f"@{bot_username}" if bot_username else "@bot"
    return (
        f"📋 <b>Shadow Ledger — Command Registry</b>\n"
        f"Trigger: <code>{m} &lt;command&gt;</code>\n\n"
        f"<b>Daily Usage</b>\n"
        f"<code>{m} refresh</code>    — Force poll; shows last known data if API fails\n"
        f"<code>{m} tokens</code>     — Per-project breakdown by model\n"
        f"<code>{m} models</code>     — Aggregate model usage across all projects\n"
        f"<code>{m} projects</code>   — Project roster with token bar chart\n"
        f"<code>{m} rank</code>       — Rankings by token use and spend\n\n"
        f"<b>Trends &amp; Spending</b>\n"
        f"<code>{m} recent</code>     — Last 31 days: per-project cost, total tokens &amp; requests\n"
        f"<code>{m} spending</code>   — Monthly bill (current + previous month)\n\n"
        f"<b>Concurrency</b>\n"
        f"<code>{m} active</code>     — Projects active in last {CONCURRENCY_WINDOW_MINS} min\n\n"
        f"<b>Archive</b>\n"
        f"<code>{m} archive</code>    — Show, seal, unseal projects (interactive buttons)\n\n"
        f"<b>Notifications</b>\n"
        f"<code>{m} arise</code>      — Subscribe this chat to all alerts\n"
        f"<code>{m} dismiss</code>    — Unsubscribe this chat\n"
        f"<code>{m} setname Name</code> — Set the name the bot uses to address you\n\n"
        f"<code>{m} help</code>       — This registry\n\n"
        f"<b>Latest update — {BOT_UPDATED}</b>\n"
        + "".join(f"• {c}\n" for c in BOT_CHANGES)
        + f"\n<i>Your Majesty {name}, your command is my directive.</i>"
    )


# ── Command dispatch ───────────────────────────────────────────────────────

def _match_prefix(text: str, bot_username: Optional[str]) -> Optional[str]:
    if not bot_username:
        return None
    prefix = f"@{bot_username.lower()}"
    lower  = text.strip().lower()
    if lower.startswith(prefix):
        return text.strip()[len(prefix):].strip()
    return None


def dispatch(text: str, usage: UsageStore, subs: SubscriberStore,
             bot_username: Optional[str], chat_id: str, names: NameStore = None,
             thread_id: int = None) -> tuple:
    """Returns (reply_text_or_None, keyboard_or_None)."""
    rest = _match_prefix(text, bot_username)
    if rest is None:
        return None, None

    parts = rest.split()
    cmd   = parts[0].lower() if parts else "help"

    name = names.get(chat_id) if names else "Bach"

    if cmd == "setname":
        new_name = " ".join(parts[1:]) if len(parts) > 1 else ""
        return (cmd_setname(chat_id, new_name, names) if names else "Name store unavailable."), None

    if cmd == "archive":
        return cmd_archive(usage, name)   # (text, keyboard)

    routes = {
        "tokens":   lambda: cmd_tokens(usage, name),
        "projects": lambda: cmd_projects(usage, name),
        "rank":     lambda: cmd_rank(usage, name),
        "spending": lambda: cmd_spending(name),
        "recent":   lambda: cmd_recent(name),
        "models":   lambda: cmd_models(usage, name),
        "active":   lambda: cmd_active(usage, name),
        "refresh":  lambda: cmd_refresh(usage, subs, names, name),
        "arise":    lambda: cmd_arise(chat_id, subs, name, thread_id),
        "dismiss":  lambda: cmd_dismiss(chat_id, subs, name),
        "help":     lambda: cmd_help(bot_username, name),
    }

    handler = routes.get(cmd)
    if handler:
        return handler(), None

    mention = f"@{bot_username}" if bot_username else "@bot"
    return f"Unknown command: <code>{cmd}</code>. Use <code>{mention} help</code> for the registry.", None


# ── Telegram poll thread ───────────────────────────────────────────────────

def telegram_poll_loop(usage: UsageStore, subs: SubscriberStore,
                       bot_username: Optional[str], names: NameStore = None,
                       initial_offset: int = 0) -> None:
    offset = initial_offset
    while True:
        updates = _get_updates(offset)
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                if upd.get("callback_query"):
                    _handle_callback_update(upd["callback_query"], usage, subs, names)
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
                if cmd != "arise" and chat_id not in subs.all():
                    continue  # not subscribed — ignore all commands except arise
                _log_event("command", chat=chat_id, cmd=rest[:120])
                reply, keyboard = dispatch(text, usage, subs, bot_username, chat_id, names, thread_id)
                if reply:
                    _send(reply, chat_id, thread_id, keyboard=keyboard)
            except Exception as e:
                print(f"[telegram handler error] {e}")


def _handle_callback_update(cq: dict, usage: UsageStore, subs: SubscriberStore,
                            names: NameStore) -> None:
    """Process a callback_query (inline button press). Only 'arch:' callbacks from
    subscribed chats are handled; everything else is acknowledged and ignored."""
    cq_id   = cq.get("id", "")
    data    = cq.get("data", "") or ""
    msg     = cq.get("message", {}) or {}
    chat_id = str(msg.get("chat", {}).get("id", ""))
    msg_id  = msg.get("message_id")
    name    = names.get(chat_id) if names else "Bach"

    if chat_id not in subs.all():
        _answer_callback(cq_id, "This channel isn't subscribed.")
        return
    if not data.startswith("arch:"):
        _answer_callback(cq_id)
        return

    print(f"[callback] chat={chat_id} data={data!r}")
    _log_event("command", chat=chat_id, cmd=f"callback:{data[:100]}")
    try:
        text, keyboard, toast = handle_archive_callback(
            data, usage, subs, names, name, chat_id, msg_id)
    except Exception as e:
        print(f"[callback handler error] {e}")
        _answer_callback(cq_id, "Error — check logs.")
        return

    _answer_callback(cq_id, toast)
    if text is not None and msg_id is not None:
        _edit_message(text, chat_id, msg_id, keyboard=keyboard)


# ── Usage poll thread ──────────────────────────────────────────────────────

def usage_poll_loop(usage: UsageStore, subs: SubscriberStore, names: NameStore = None) -> None:
    """Day rollover is handled by UsageStore.update() (auto-detects date change) and by
    the constructor's stale-date check. seed-vs-check is driven by has_seeded()."""
    fail_count  = 0
    last_logged = None   # (date, normal, premium, cost) of last intel-logged poll
    wave_guard  = _WaveGuard()   # burst-resistant predictive seal trigger
    watch_zone  = False  # True while an unsealed track is near its seal threshold

    while True:
        try:
            snap = fetch_today_usage()
            if snap is not None:
                # Costs API may return:
                #   None — actual failure → fall back to cached costs (don't zero them)
                #   {}   — no spend today → real zero (DON'T fall back to cache, or
                #          we'd never see the day's spend drop to zero at rollover)
                # The distinction is what catches off-watchlist anomalies cleanly.
                breakdown = _fetch_costs_breakdown()
                costs, lane_costs = breakdown if breakdown is not None else (None, None)
                org_cost = 0.0
                if costs is not None:
                    # Successful fetch (may be empty)
                    org_cost = costs.pop("__org__", 0.0)
                    for pid, p in snap.get("projects", {}).items():
                        p["cost_usd"] = round(costs.get(pid, 0.0), 6)
                    snap["total_cost"] = round(sum(costs.values()) + org_cost, 6)
                    snap["lane_costs"] = {l: round(v, 6) for l, v in lane_costs.items()}
                else:
                    # API failure — preserve last known cost picture
                    cached_costs = usage.get_costs_cache()
                    if cached_costs:
                        per_proj = cached_costs.get("per_project", {})
                        for pid, p in snap.get("projects", {}).items():
                            p["cost_usd"] = round(per_proj.get(pid, 0.0), 6)
                        snap["total_cost"] = cached_costs.get("total", 0.0)
                        snap["lane_costs"] = dict(cached_costs.get("per_lane", {}))

                usage.update(snap)   # auto-resets daily state on day rollover
                if costs is not None:
                    usage.update_costs(costs, snap["total_cost"], org_cost, lane_costs)

                # If yesterday's sealed tracks were just moved to pending_track_unseal
                # by the daily reset, restore them via the API now. Failures stay in
                # the queue and retry on the next iteration.
                _process_pending_track_unseals(usage, subs, names)

                normal_tok  = snap.get("total_normal_tokens",  0)
                premium_tok = snap.get("total_premium_tokens", 0)
                n_str    = _color(f"{_fmt_tokens(normal_tok)}/10M",  _tok_color(normal_tok,  TOKEN_HARD_CAP))
                p_str    = _color(f"{_fmt_tokens(premium_tok)}/1M",  _tok_color(premium_tok, PREMIUM_TOKEN_HARD_CAP))
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

                # ── Wave guard: predictive early seal + watch-zone detection ──
                # Static thresholds react to numbers that are already 5–15 min
                # stale. The guard projects each track forward on a WINDOWED
                # burn rate; all gating (window span, 60% floor, 2-poll
                # confirmation) lives in _WaveGuard — see its rationale.
                track_view = (
                    ("normal",  normal_tok,  TOKEN_HARD_CAP,         NORMAL_TRACK_SEAL_THRESHOLD),
                    ("premium", premium_tok, PREMIUM_TOKEN_HARD_CAP, PREMIUM_TRACK_SEAL_THRESHOLD),
                )
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
                        _handle_track_seal(track, snap, usage, subs, names)

                # Watch zone: an unsealed track close to its threshold forces
                # 60 s polling below (urgent mode's 3→10 min stepping would
                # otherwise reopen the detection gap right at the worst time).
                watch_zone = any(
                    tok >= thr - int(cap * WAVE_WATCH_BAND_PCT) and not usage.is_mass_sealed(track)
                    for track, tok, cap, thr in track_view
                )

                # ── Track-level mass throttle (static threshold path) ──
                # Each track is independent and idempotent via the per-day mass_sealed
                # flag — once the sweep has fired for a track today, it won't re-fire.
                if normal_tok >= NORMAL_TRACK_SEAL_THRESHOLD and not usage.is_mass_sealed("normal"):
                    _handle_track_seal("normal", snap, usage, subs, names)
                if premium_tok >= PREMIUM_TRACK_SEAL_THRESHOLD and not usage.is_mass_sealed("premium"):
                    _handle_track_seal("premium", snap, usage, subs, names)

                # ── Seal-gap repair: re-seal projects the sweep missed ──
                # A failed seal used to leave a project burning post-cap all day.
                for track, tok, cap, thr in track_view:
                    if usage.is_mass_sealed(track) and tok >= thr:
                        _repair_seal_gaps(track, usage, subs, names)

                # ── Cap check (alarm-only; mass throttle above should normally
                #    keep this from firing except for manually-exempt projects)
                normal_exceeded  = normal_tok  >= TOKEN_HARD_CAP
                premium_exceeded = premium_tok >= PREMIUM_TOKEN_HARD_CAP

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
                        if snap.get("total_cost", 0.0) >= DAILY_LIMIT:
                            pass   # still bleeding — keep fast polling
                        else:
                            # Token caps cleared (day rollover) and spend under cap
                            usage.set_mode("passive")
                            print("[mode] → PASSIVE (caps no longer exceeded)")

                fail_count = 0
            else:
                fail_count += 1
                mode     = usage.get_mode()
                base     = PASSIVE_INTERVAL_SECS if mode == "passive" else URGENT_INTERVAL_MIN
                max_back = PASSIVE_BACKOFF_MAX   if mode == "passive" else URGENT_INTERVAL_MAX
                backoff  = min(base * (2 ** min(fail_count - 1, 4)), max_back)
                print(f"[poll] Fetch failed ({fail_count}) — retry in {backoff // 60:.0f} min (backoff)")
                if fail_count in (1, 5, 10):   # log the onset + escalation, not every retry
                    _log_event("poll_fail", consecutive=fail_count, backoff_secs=backoff)
                time.sleep(backoff)
                continue
        except Exception as e:
            print(f"[poll loop error] {e}")
            fail_count += 1

        # ── Determine next sleep interval ──────────────────────────────────
        mode = usage.get_mode()
        if mode == "passive":
            sleep_secs = PASSIVE_INTERVAL_SECS
        else:
            sleep_secs = usage.get_urgent_interval()
            usage.increment_urgent_step()

        if watch_zone and sleep_secs > WAVE_WATCH_SLEEP_SECS:
            # Near an unsealed threshold — tighten the loop so the wave can't
            # ride an 8-minute poll gap over the cap.
            sleep_secs = WAVE_WATCH_SLEEP_SECS
            print(f"[poll] Watch zone — next poll in {sleep_secs} s  (mode={mode})")
        else:
            print(f"[poll] Next poll in {sleep_secs // 60:.0f} min  (mode={mode})")
        time.sleep(sleep_secs)


# ── Concurrency check thread ───────────────────────────────────────────────

def concurrency_check_loop(usage: UsageStore, subs: SubscriberStore, names: NameStore = None) -> None:
    """Every CONCURRENCY_WINDOW_MINS minutes, check for simultaneous project activity.
    On API failure, leaves the previous active_projects snapshot intact rather than
    overwriting it with an empty dict (which would falsely report 'no activity')."""
    while True:
        try:
            activity = _fetch_recent_activity(CONCURRENCY_WINDOW_MINS)
            if activity is None:
                print("[concurrency] activity fetch failed — keeping last known snapshot")
            else:
                active = {pid: count for pid, count in activity.items() if count > 0}
                usage.set_active_projects(active, CONCURRENCY_WINDOW_MINS)

                if len(active) >= CONCURRENCY_THRESHOLD:
                    last_ts = usage.get_last_concurrent_alert_ts()
                    if last_ts is None or time.time() - last_ts > CONCURRENCY_COOLDOWN:
                        usage.set_last_concurrent_alert_ts(time.time())
                        _broadcast(lambda n, a=active: fmt_concurrency_alert(a, n), subs, names)
                        print(f"[concurrency] Alert fired — {len(active)} projects active")
        except Exception as e:
            print(f"[concurrency check error] {e}")

        time.sleep(CONCURRENCY_WINDOW_MINS * 60)


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    if not OPENAI_ADMIN_KEY or not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Missing required environment variables.\n"
            "Ensure OPENAI_ADMIN_KEY, TELEGRAM_BOT_TOKEN, and TELEGRAM_CHAT_ID are set in .env"
        )

    BOT_DATA_DIR.mkdir(parents=True, exist_ok=True)

    usage = UsageStore(USAGE_STATE_PATH)
    subs  = SubscriberStore(SUBS_PATH, CHAT_ID)
    names = NameStore(NAMES_PATH, CHAT_ID)

    # Wire the chat-migration handler so a group→supergroup upgrade rewrites
    # the stores instead of 400-ing on every broadcast forever.
    global _MIGRATION_CB
    _MIGRATION_CB = lambda old, new: (subs.migrate(old, new), names.migrate(old, new))

    bot_username = _fetch_bot_username()
    if bot_username:
        print(f"[bot] @{bot_username} ready")
    else:
        print("[bot] WARNING: Could not resolve username — commands will not work")

    # Drop any updates that piled up while the bot was offline (stale archive
    # button clicks would otherwise re-fire seal/unseal on restart).
    initial_offset = _discard_pending_updates()

    threading.Thread(target=telegram_poll_loop,     args=(usage, subs, bot_username, names, initial_offset), daemon=True).start()
    threading.Thread(target=usage_poll_loop,        args=(usage, subs, names),               daemon=True).start()
    threading.Thread(target=concurrency_check_loop, args=(usage, subs, names),               daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("[bot] Shutdown signal received. Standing down.")


if __name__ == "__main__":
    main()
