"""
Telegram Usage Bot — OpenAI Shadow Ledger

Polls OpenAI organization usage API and reports token/cost stats per project.
Receives @commands from the configured Telegram chat.
Monitors token milestones, concurrent project activity, and daily expenditure.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BOT IDENTITY: Marshal-Rank Shadow Commander
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Bound to Bach the Monarch. Speaks with formality and restraint.
No humor. No filler. Precision in all things.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import calendar
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import dotenv
import requests

dotenv.load_dotenv()

OPENAI_ADMIN_KEY = os.environ.get("OPENAI_ADMIN_KEY", "")
BOT_TOKEN        = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID          = os.environ.get("TELEGRAM_CHAT_ID", "")
DAILY_LIMIT      = 5.00   # hardcoded — alerts fire at $5, then every $2 above
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

# ── Track-level mass-seal trigger ──────────────────────────────────────────
# When a track's remaining quota drops to this fraction (or below — including
# negative remaining if the cap has been overshot), the bot mass-throttles every
# project's rate-limit rows for that track's models. 0.05 = 5% remaining =
# 9.5M of the 10M normal cap, 950k of the 1M premium cap.
TRACK_SEAL_REMAINING_PCT       = 0.05
NORMAL_TRACK_SEAL_THRESHOLD    = int(TOKEN_HARD_CAP         * (1 - TRACK_SEAL_REMAINING_PCT))
PREMIUM_TRACK_SEAL_THRESHOLD   = int(PREMIUM_TOKEN_HARD_CAP * (1 - TRACK_SEAL_REMAINING_PCT))


def _matches_track(model: str, track: str) -> bool:
    """True if `model` belongs to the named track ('normal' or 'premium')."""
    if track == "premium":
        return _is_premium_model(model)
    if track == "normal":
        return not _is_premium_model(model)
    raise ValueError(f"unknown track: {track!r}")

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
# Match by prefix so date-suffixed variants ("gpt-4o-mini-2024-07-18") still classify.
# Normal-band models share 10M tokens/day free:
NORMAL_MODEL_PREFIXES = (
    "gpt-5.4-mini", "gpt-5.4-nano",
    "gpt-5.1-codex-mini",
    "gpt-5-mini", "gpt-5-nano",
    "gpt-4.1-mini", "gpt-4.1-nano",
    "gpt-4o-mini",
    "o1-mini",
    "o3-mini", "o4-mini",
    "codex-mini-latest",
)
# Premium-band models share 1M tokens/day free:
PREMIUM_MODEL_PREFIXES = (
    "gpt-5.4", "gpt-5.2",
    "gpt-5.1-codex", "gpt-5.1",
    "gpt-5-codex", "gpt-5-chat-latest", "gpt-5",
    "gpt-4.1",
    "gpt-4o",
    "o1",
    "o3",
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

def _is_premium_model(model: str) -> bool:
    """True if the model belongs to the 1M/day premium band; False for the 10M normal band.

    Classification order (mini/nano always wins, even if the listed premium prefix matches):
      1. Explicit normal prefixes (listed mini/nano variants)        → normal.
      2. 'mini' or 'nano' substring anywhere                          → normal.
         (covers unlisted future variants like 'gpt-5.5-mini' that would otherwise be
         caught by the broader 'gpt-5' premium prefix.)
      3. Anything else (listed premium + unknown full-size)           → premium.
    Premium-by-default is the conservative choice for free-tier alerting."""
    m = model.lower()
    if any(m.startswith(p) for p in NORMAL_MODEL_PREFIXES):
        return False
    if "mini" in m or "nano" in m:
        return False
    return True


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


def _fetch_costs() -> dict[str, float]:
    """Today's cost per project. '__org__' key holds any unattributed org-level cost."""
    start, end = today_window_costs()
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
            r = requests.get(OPENAI_COSTS_URL, headers=_openai_headers(), params=p, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            print(f"[openai costs network error] {e}")
            break
        if not r.ok:
            print(f"[openai costs {r.status_code}] {r.text[:500]}")
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


def _fetch_tokens() -> dict[str, dict]:
    """Today's token usage per project, broken down by model and band."""
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
    while True:
        p = list(params)
        if page:
            p.append(("page", page))
        try:
            r = requests.get(OPENAI_USAGE_URL, headers=_openai_headers(), params=p, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            print(f"[openai usage network error] {e}")
            break
        if not r.ok:
            print(f"[openai usage {r.status_code}] {r.text[:500]}")
            break
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
                if _is_premium_model(model):
                    tokens[pid]["premium_tokens"] += inp + out
                else:
                    tokens[pid]["normal_tokens"]  += inp + out
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
            r = requests.get(OPENAI_COSTS_URL, headers=_openai_headers(), params=p, timeout=REQUEST_TIMEOUT)
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
        r = requests.get(OPENAI_USAGE_URL, headers=_openai_headers(), params=params, timeout=REQUEST_TIMEOUT)
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
        r = requests.get(OPENAI_USAGE_URL, headers=_openai_headers(), params=params, timeout=REQUEST_TIMEOUT)
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
            band  = "premium" if _is_premium_model(model) else "normal"
            slot  = out.setdefault(pid, {"normal": 0, "premium": 0})
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
            r = requests.get(url, headers=_openai_headers(), params=p, timeout=REQUEST_TIMEOUT)
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


def _update_project_rate_limit(pid: str, rate_limit_id: str, payload: dict) -> bool:
    """POST a partial update to a single rate-limit row.
    Returns True on 2xx and on the soft-skip codes above. False on any other failure."""
    url = f"{OPENAI_RATE_LIMITS_URL_TMPL.format(pid=pid)}/{rate_limit_id}"
    try:
        r = requests.post(
            url,
            headers={**_openai_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:
        print(f"[openai rate-limits POST error] {pid}/{rate_limit_id}: {e}")
        return False
    if r.ok:
        return True
    err_code = ""
    try:
        err_code = r.json().get("error", {}).get("code", "") or ""
    except Exception:
        pass
    if err_code in _SKIPPABLE_RATE_LIMIT_ERR_CODES:
        return True   # soft skip — non-updatable / no org access
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
            r = requests.get(OPENAI_COSTS_URL, headers=_openai_headers(), params=p, timeout=REQUEST_TIMEOUT)
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
            r = requests.get(OPENAI_USAGE_URL, headers=_openai_headers(), params=p, timeout=REQUEST_TIMEOUT)
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
    """Tokens-only poll — no cost fetch (costs API is unreliable for frequent polling)."""
    tokens = _fetch_tokens()
    if not tokens:
        return None
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
    """Overlay costs onto a snapshot copy.
    Tries a live fetch first (when live=True). On success, writes to usage cache.
    Falls back to cached costs from usage store if live fetch fails or live=False."""
    import copy
    snap  = copy.deepcopy(snap)
    costs = _fetch_costs() if live else None
    if costs:
        org_cost = costs.pop("__org__", 0.0)
        for pid, p in snap.get("projects", {}).items():
            p["cost_usd"] = round(costs.get(pid, 0.0), 6)
        snap["total_cost"] = round(sum(costs.values()) + org_cost, 6)
        snap["org_cost"]   = round(org_cost, 6)
        if usage:
            usage.update_costs(costs, snap["total_cost"], org_cost)
    else:
        cached = usage.get_costs_cache() if usage else None
        if cached:
            per_proj = cached.get("per_project", {})
            for pid, p in snap.get("projects", {}).items():
                p["cost_usd"] = round(per_proj.get(pid, 0.0), 6)
            snap["total_cost"] = cached.get("total", 0.0)
            snap["costs_stale"] = True
            snap["costs_ts"]    = cached.get("ts")
    return snap


# ── Usage state store ──────────────────────────────────────────────────────

class UsageStore:
    """Persists today's usage snapshot and all alert-control state to disk."""

    # Fields that must survive snapshot updates (not overwritten on each poll)
    _PRESERVED = (
        "alert_sent",
        "token_milestones_notified",
        "premium_milestones_notified",
        "spend_intervals_notified",
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
        # project sealing — sealed_projects is for the manual full-seal command;
        # pending_unseal holds entries to be restored on day rollover or after
        # a legacy migration. sealed_tracks holds the mass per-track throttle
        # state. track_exemptions records projects that have been manually
        # unsealed for one or both tracks and should be skipped by future
        # auto-seals of those tracks this day.
        "sealed_projects",
        "pending_unseal",
        "sealed_tracks",
        "pending_track_unseal",
        "track_exemptions",
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
            except Exception:
                self._data = {}

    def _save(self):
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)

    def _reset_daily_state_locked(self):
        """Reset all daily alert/mode state. Caller must hold self._lock.
        Sealed state (per-project full + per-track mass) moves into the matching
        pending_* queue so the API restore happens on the next poll/refresh —
        keeps the lock acquisition cheap."""
        # Move sealed_projects → pending_unseal
        sealed  = self._data.get("sealed_projects", {})
        pending = self._data.get("pending_unseal", {})
        pending.update(sealed)
        # Move sealed_tracks → pending_track_unseal (merge originals_by_project
        # if the queue already has an entry for the same track)
        s_tracks = self._data.get("sealed_tracks", {})
        p_tracks = self._data.get("pending_track_unseal", {})
        for track, info in s_tracks.items():
            if track in p_tracks:
                p_tracks[track].setdefault("originals_by_project", {}).update(
                    info.get("originals_by_project", {})
                )
            else:
                p_tracks[track] = info

        self._data["alert_sent"]                  = False
        self._data["token_milestones_notified"]   = []
        self._data["premium_milestones_notified"] = []
        self._data["spend_intervals_notified"]    = 0
        self._data["bot_mode"]                    = "passive"
        self._data["mode_entered_ts"]             = None
        self._data["last_milestone_ts"]           = None
        self._data["last_illegal_seen_ts"]        = None
        self._data["urgent_poll_step"]            = 0
        self._data["milestones_seeded"]           = False
        self._data["sealed_projects"]             = {}
        self._data["sealed_tracks"]               = {}
        self._data["track_exemptions"]            = {}
        self._data["pending_unseal"]              = pending
        self._data["pending_track_unseal"]        = p_tracks
        # Drop legacy field carried over from older bot versions
        self._data.pop("manually_unsealed_today", None)
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
                self._reset_daily_state_locked()
            preserved = {k: self._data[k] for k in self._PRESERVED if k in self._data}
            self._data = snapshot
            self._data.update(preserved)
            self._save()

    def seed_state(self, normal_thresholds: list, premium_thresholds: list) -> None:
        """Bulk-mark seeded milestones in one disk write. Always sets milestones_seeded=True,
        even when both lists are empty (so unseeded fresh days still flip the flag)."""
        with self._lock:
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

    def get(self) -> dict:
        with self._lock:
            return dict(self._data)

    # Daily cost alert
    def get_alert_sent(self) -> bool:
        with self._lock:
            return self._data.get("alert_sent", False)

    def mark_alert_sent(self):
        with self._lock:
            self._data["alert_sent"] = True
            self._save()

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

    # Spend intervals (post-cap, one count per $2)
    def get_spend_intervals_notified(self) -> int:
        with self._lock:
            return self._data.get("spend_intervals_notified", 0)

    def set_spend_intervals_notified(self, count: int):
        with self._lock:
            self._data["spend_intervals_notified"] = count
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
    def update_costs(self, per_project: dict, total: float, org: float) -> None:
        with self._lock:
            self._data["costs_cache"] = {
                "per_project": dict(per_project),
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
            self._data["bot_mode"]        = mode
            self._data["mode_entered_ts"] = time.time()
            if mode in ("urgent", "aggressive"):
                self._data["urgent_poll_step"] = 0
            self._save()

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

    # ── Project sealing ────────────────────────────────────────────────────
    def get_sealed_projects(self) -> dict:
        with self._lock:
            return {pid: dict(info) for pid, info in self._data.get("sealed_projects", {}).items()}

    def is_sealed(self, pid: str) -> bool:
        with self._lock:
            return pid in self._data.get("sealed_projects", {})

    def get_sealed_info(self, pid: str) -> Optional[dict]:
        with self._lock:
            info = self._data.get("sealed_projects", {}).get(pid)
            return dict(info) if info else None

    def mark_sealed(self, pid: str, reason: str, original_limits: list, auto_sealed: bool) -> None:
        with self._lock:
            sealed = self._data.setdefault("sealed_projects", {})
            sealed[pid] = {
                "sealed_at":       time.time(),
                "reason":          reason,
                "original_limits": original_limits,
                "auto_sealed":     auto_sealed,
            }
            self._save()

    def get_pending_unseal(self) -> dict:
        with self._lock:
            return {pid: dict(info) for pid, info in self._data.get("pending_unseal", {}).items()}

    def clear_pending_unseal(self, pid: str) -> None:
        with self._lock:
            pending = self._data.setdefault("pending_unseal", {})
            pending.pop(pid, None)
            self._save()

    # ── Track-level seals ──────────────────────────────────────────────────
    def is_track_sealed(self, track: str) -> bool:
        with self._lock:
            return track in self._data.get("sealed_tracks", {})

    def get_sealed_tracks(self) -> dict:
        with self._lock:
            return {t: dict(info) for t, info in self._data.get("sealed_tracks", {}).items()}

    def mark_track_seal_started(self, track: str, threshold: int) -> None:
        """Create the sealed_tracks[track] entry early so concurrent triggers see it."""
        with self._lock:
            tracks = self._data.setdefault("sealed_tracks", {})
            if track in tracks:
                return
            tracks[track] = {
                "sealed_at":           time.time(),
                "threshold":           threshold,
                "originals_by_project": {},
            }
            self._save()

    def add_track_originals(self, track: str, pid: str, originals: list) -> None:
        with self._lock:
            tracks = self._data.setdefault("sealed_tracks", {})
            entry  = tracks.setdefault(track, {
                "sealed_at": time.time(),
                "originals_by_project": {},
            })
            entry.setdefault("originals_by_project", {})[pid] = originals
            self._save()

    def pop_track_originals(self, track: str, pid: str) -> Optional[list]:
        """Remove and return one project's saved originals for a track. Clears
        sealed_tracks[track] if no projects remain under it."""
        with self._lock:
            tracks = self._data.setdefault("sealed_tracks", {})
            if track not in tracks:
                return None
            originals = tracks[track].get("originals_by_project", {}).pop(pid, None)
            if not tracks[track].get("originals_by_project"):
                tracks.pop(track, None)
            self._save()
            return originals

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

    def remove_sealed_project_rate_limits(self, pid: str, rl_ids: set) -> None:
        """Drop specific rate-limit-id entries from sealed_projects[pid].original_limits,
        used after a partial unseal of one track. Clears the project entry if empty."""
        with self._lock:
            sealed = self._data.setdefault("sealed_projects", {})
            entry  = sealed.get(pid)
            if not entry:
                return
            entry["original_limits"] = [
                o for o in entry.get("original_limits", []) if o.get("id") not in rl_ids
            ]
            if not entry["original_limits"]:
                sealed.pop(pid, None)
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
            except Exception:
                pass
        self._ids.add(self.primary)

    def _save(self):
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(sorted(self._ids), f, indent=2)

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
            except Exception:
                pass

    def _save(self):
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(self._names, f, indent=2)

    def set(self, chat_id: str, name: str) -> None:
        with self._lock:
            self._names[str(chat_id)] = name
            self._save()

    def get(self, chat_id: str) -> str:
        with self._lock:
            return self._names.get(str(chat_id), "Commander")


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


def _send(text: str, chat_id: str = None, thread_id: int = None) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    target_chat = chat_id or CHAT_ID
    payload = {"chat_id": target_chat, "text": text, "parse_mode": "HTML"}
    if thread_id:
        payload["message_thread_id"] = thread_id
    try:
        r = requests.post(
            url,
            data=payload,
            timeout=REQUEST_TIMEOUT,
        )
        if not r.ok:
            print(f"[telegram send {r.status_code}] chat={chat_id or CHAT_ID} | {r.text[:400]}")
    except Exception as e:
        print(f"[telegram send network error] {e}")


def _send_all(text: str, subs: SubscriberStore) -> None:
    for chat_id in subs.all():
        _send(text, chat_id)


def _broadcast_named(fmt_fn, subs: SubscriberStore, names: NameStore) -> None:
    """Send a personalized message to each subscriber using their registered name."""
    for cid in subs.all():
        _send(fmt_fn(names.get(cid)), cid)


def _get_updates(offset: int) -> list[dict]:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        r = requests.get(
            url,
            params={
                "offset":          offset,
                "timeout":         POLL_TIMEOUT,
                "allowed_updates": json.dumps(["message", "channel_post"]),
            },
            timeout=POLL_TIMEOUT + 5,
        )
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        print(f"[poll error] {e}")
        time.sleep(5)   # avoid a tight reconnect loop on persistent failure
        return []


def _fetch_bot_username() -> Optional[str]:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json().get("result", {}).get("username")
    except Exception as e:
        print(f"[getMe error] {e}")
        return None


# ── Formatters — helpers ────────────────────────────────────────────────────

def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


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
        if names:
            _broadcast_named(
                lambda n, r=illegal, ne=normal_exceeded, pe=premium_exceeded:
                    fmt_overcap_active_alert(r, ne, pe, n),
                subs, names,
            )
        else:
            _send_all(fmt_overcap_active_alert(illegal, normal_exceeded, premium_exceeded), subs)
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


def _restore_rate_limits(pid: str, originals: list[dict]) -> int:
    """POST originals back to the API one at a time with inter-write spacing.
    Returns the count of failed POSTs (0 on full success)."""
    failed = 0
    for orig in originals:
        if _update_project_rate_limit(pid, orig["id"], _restore_payload(orig)):
            time.sleep(0.05)
        else:
            failed += 1
    return failed


# ── Manual full-project seal (cmd_archive seal <project>) ──────────────────

def _seal_project(usage: UsageStore, subs: SubscriberStore, names: NameStore,
                  pid: str, reason: str) -> bool:
    """Throttle every rate-limit row on `pid` to 0. If the project already has
    track-level seals from the mass throttle, those are folded in: we use the
    saved per-track originals for the throttled bands and capture live values
    for the rest. Returns False if the project is already fully sealed or the
    initial API fetch fails."""
    if usage.is_sealed(pid):
        return False
    proj_name   = KNOWN_PROJECTS.get(pid, pid)
    rate_limits = _fetch_project_rate_limits(pid)
    if rate_limits is None:
        print(f"[seal] {proj_name}: rate-limits fetch failed — aborting")
        return False
    if not rate_limits:
        print(f"[seal] {proj_name}: no rate-limit rows — nothing to throttle")
        return False

    # If any track-level seal already touched this project, the live rate-limit
    # values for that track are now 0 — use the saved track originals instead so
    # the eventual unseal restores the right values.
    live_originals  = _capture_originals(rate_limits)
    track_overrides = {}
    for track, info in usage.get_sealed_tracks().items():
        for o in info.get("originals_by_project", {}).get(pid, []):
            track_overrides[o["id"]] = o
    merged_originals = []
    for o in live_originals:
        merged_originals.append(track_overrides.get(o["id"], o))

    # POST 0 to every row we haven't already throttled this day. Skip ones the
    # track-level seal already throttled — they're at 0 already.
    track_sealed_ids = set(track_overrides.keys())
    sealed_now: list[str] = []
    for rl in rate_limits:
        if rl["id"] in track_sealed_ids:
            continue   # already at 0 from the mass throttle, no-op
        if _update_project_rate_limit(pid, rl["id"], _seal_payload(rl)):
            sealed_now.append(rl["id"])
            time.sleep(0.05)
        else:
            # Partial failure — restore only the rows we threw down in this call.
            print(f"[seal] {proj_name}: throttle failed at {rl['id']} — rolling back this call")
            rollback = [o for o in live_originals if o["id"] in sealed_now]
            _restore_rate_limits(pid, rollback)
            if names:
                _broadcast_named(
                    lambda n, p=proj_name: f"⚠️ Seal attempt for <b>{p}</b> failed mid-way — rolled back, manual check advised, Monarch {n}.",
                    subs, names,
                )
            return False

    usage.mark_sealed(pid, reason, merged_originals, auto_sealed=False)
    if names:
        _broadcast_named(
            lambda n, p=proj_name, r=reason, c=len(merged_originals):
                fmt_seal_alert(p, r, c, n),
            subs, names,
        )
    else:
        _send_all(fmt_seal_alert(proj_name, reason, len(merged_originals)), subs)
    print(f"[seal] {proj_name}: throttled {len(rate_limits)} rate-limit row(s)")
    return True


def _unseal_project(usage: UsageStore, subs: SubscriberStore, names: NameStore,
                    pid: str, track: Optional[str], reason: str,
                    broadcast: bool = True) -> bool:
    """Manual unseal entry point. If `track` is None → unseal both tracks (full).
    If `track` in {"normal","premium"} → restore only that track's rows.

    The function consults both sealed_projects (full manual seal) AND sealed_tracks
    (mass per-track seal) and restores from whichever holds the originals. The
    project is added to track_exemptions for every track that was unsealed, so
    today's automatic re-seal of the same track will skip it."""
    proj_name = KNOWN_PROJECTS.get(pid, pid)
    targets   = ("normal", "premium") if track is None else (track,)

    # Collect rate-limit rows to restore from BOTH sources, filtered to targets.
    full_entry      = usage.get_sealed_info(pid)
    full_originals  = full_entry.get("original_limits", []) if full_entry else []
    sealed_tracks   = usage.get_sealed_tracks()

    to_restore: list[dict] = []
    rl_ids_restored_per_track: dict[str, set[str]] = {t: set() for t in targets}

    # Pull from sealed_projects[pid] originals matching target tracks
    for o in full_originals:
        for t in targets:
            if _matches_track(o.get("model", ""), t):
                to_restore.append(o)
                rl_ids_restored_per_track[t].add(o["id"])
                break

    # Pull from sealed_tracks[t].originals_by_project[pid]
    for t in targets:
        info = sealed_tracks.get(t)
        if not info:
            continue
        for o in info.get("originals_by_project", {}).get(pid, []):
            # Avoid double-add if the same row was captured in full_originals
            if o["id"] in rl_ids_restored_per_track[t]:
                continue
            to_restore.append(o)
            rl_ids_restored_per_track[t].add(o["id"])

    if not to_restore:
        # Nothing to do — project isn't sealed for the requested track(s)
        return True

    failed = _restore_rate_limits(pid, to_restore)
    if failed:
        print(f"[unseal] {proj_name}: {failed}/{len(to_restore)} restore POSTs failed")
        if broadcast and names:
            _broadcast_named(
                lambda n, p=proj_name, f=failed, t=len(to_restore):
                    f"⚠️ Unseal of <b>{p}</b> failed on {f}/{t} rate-limit(s) — will retry, Monarch {n}.",
                subs, names,
            )
        return False

    # Update state: drop restored rows from sealed_projects, drop project from
    # the sealed_tracks entry, and mark exemption.
    for t in targets:
        rl_ids = rl_ids_restored_per_track[t]
        if rl_ids:
            usage.remove_sealed_project_rate_limits(pid, rl_ids)
            usage.pop_track_originals(t, pid)
        usage.add_track_exemption(pid, t)

    print(f"[unseal] {proj_name}: restored {len(to_restore)} row(s) "
          f"for {','.join(targets)} ({reason})")
    if broadcast:
        if names:
            _broadcast_named(
                lambda n, p=proj_name, r=reason, ts=targets:
                    fmt_unseal_alert(p, r, ts, n),
                subs, names,
            )
        else:
            _send_all(fmt_unseal_alert(proj_name, reason, targets), subs)
    return True


def _process_pending_unseals(usage: UsageStore, subs: SubscriberStore, names: NameStore) -> None:
    """Drain the legacy pending_unseal queue — full-restore each project. Used
    for the day-rollover restore of yesterday's manual full seals AND for the
    one-shot migration of state files written by older bot versions."""
    pending = usage.get_pending_unseal()
    if not pending:
        return
    print(f"[pending-unseal] {len(pending)} project(s) to restore")
    for pid, info in pending.items():
        originals = info.get("original_limits", [])
        if not originals:
            usage.clear_pending_unseal(pid)
            continue
        proj_name = KNOWN_PROJECTS.get(pid, pid)
        failed    = _restore_rate_limits(pid, originals)
        if failed:
            print(f"[pending-unseal] {proj_name}: {failed}/{len(originals)} failed — will retry next poll")
            continue
        usage.clear_pending_unseal(pid)
        if names:
            _broadcast_named(
                lambda n, p=proj_name: fmt_unseal_alert(p, "day rollover", ("normal", "premium"), n),
                subs, names,
            )
        else:
            _send_all(fmt_unseal_alert(proj_name, "day rollover", ("normal", "premium")), subs)


# ── Track-level mass throttle / restore ────────────────────────────────────

def _ordered_projects_for_track_seal(track: str) -> list[str]:
    """Return KNOWN_PROJECTS ordered with the projects currently most active on
    `track` first, so the mass throttle hits the heaviest spenders first."""
    activity = _fetch_recent_activity_by_band(minutes=OVERCAP_WINDOW_MINS) or {}
    return sorted(
        KNOWN_PROJECTS.keys(),
        key=lambda p: activity.get(p, {}).get(track, 0),
        reverse=True,
    )


def _throttle_track_for_project(pid: str, track: str, usage: UsageStore) -> str:
    """Throttle all of `pid`'s rate-limit rows that belong to `track` down to 0.
    Returns one of: 'throttled' / 'noop' / 'failed'.
    On partial failure rolls back its own rows so the project is left untouched."""
    rate_limits = _fetch_project_rate_limits(pid)
    if rate_limits is None:
        return "failed"
    track_rls = [rl for rl in rate_limits if _matches_track(rl.get("model", ""), track)]
    if not track_rls:
        return "noop"

    originals = _capture_originals(track_rls)
    throttled_ids: list[str] = []
    for rl in track_rls:
        if _update_project_rate_limit(pid, rl["id"], _seal_payload(rl)):
            throttled_ids.append(rl["id"])
            time.sleep(0.05)
        else:
            # Roll back this project's rows; let the caller continue with others.
            rollback = [o for o in originals if o["id"] in throttled_ids]
            _restore_rate_limits(pid, rollback)
            return "failed"

    usage.add_track_originals(track, pid, originals)
    return "throttled"


def _handle_track_seal(track: str, snap: dict, usage: UsageStore,
                       subs: SubscriberStore, names: NameStore) -> None:
    """Mass-throttle every non-exempt, non-manually-sealed project for `track`.
    Idempotent: if sealed_tracks already contains an entry for this track, returns
    immediately. Broadcasts a 'sealing now…' message before starting and a summary
    when done. Projects ordered active-first so heavy spenders are throttled
    sooner during the (sequential, ~30–50s) operation."""
    if usage.is_track_sealed(track):
        return

    cap          = TOKEN_HARD_CAP if track == "normal" else PREMIUM_TOKEN_HARD_CAP
    threshold    = NORMAL_TRACK_SEAL_THRESHOLD if track == "normal" else PREMIUM_TRACK_SEAL_THRESHOLD
    consumed_key = "total_normal_tokens"     if track == "normal" else "total_premium_tokens"
    consumed     = snap.get(consumed_key, 0)

    # Reserve the seal slot so a concurrent trigger doesn't double-throttle.
    usage.mark_track_seal_started(track, threshold)

    print(f"[track-seal] {track} → starting (consumed={consumed:,}, cap={cap:,})")
    if names:
        _broadcast_named(
            lambda n, t=track, c=consumed, cap=cap:
                fmt_track_seal_starting(t, c, cap, n),
            subs, names,
        )
    else:
        _send_all(fmt_track_seal_starting(track, consumed, cap), subs)

    project_order = _ordered_projects_for_track_seal(track)

    throttled: list[str] = []
    skipped_exempt: list[str] = []
    skipped_manual: list[str] = []
    noop: list[str] = []
    failed: list[str] = []

    for pid in project_order:
        if usage.is_exempt(pid, track):
            skipped_exempt.append(pid)
            continue
        if usage.is_sealed(pid):
            skipped_manual.append(pid)
            continue
        result = _throttle_track_for_project(pid, track, usage)
        if result == "throttled":
            throttled.append(pid)
        elif result == "noop":
            noop.append(pid)
        else:
            failed.append(pid)

    print(f"[track-seal] {track} → done. throttled={len(throttled)} "
          f"exempt={len(skipped_exempt)} manual-sealed={len(skipped_manual)} "
          f"noop={len(noop)} failed={len(failed)}")

    if names:
        _broadcast_named(
            lambda n, t=track, th=throttled, se=skipped_exempt,
                   sm=skipped_manual, f=failed:
                fmt_track_seal_complete(t, th, se, sm, f, n),
            subs, names,
        )
    else:
        _send_all(fmt_track_seal_complete(track, throttled, skipped_exempt,
                                          skipped_manual, failed), subs)


def _process_pending_track_unseals(usage: UsageStore, subs: SubscriberStore,
                                   names: NameStore) -> None:
    """At day rollover, sealed_tracks moves into pending_track_unseal. This drains
    it: each project under each track gets its originals POSTed back. Failures
    stay in the queue and retry on subsequent calls."""
    pending = usage.get_pending_track_unseal()
    if not pending:
        return
    for track, info in pending.items():
        originals_by_project = info.get("originals_by_project", {})
        if not originals_by_project:
            continue
        print(f"[pending-track-unseal] {track} → {len(originals_by_project)} project(s)")
        for pid, originals in list(originals_by_project.items()):
            if not originals:
                usage.pop_pending_track_project(track, pid)
                continue
            proj_name = KNOWN_PROJECTS.get(pid, pid)
            failed    = _restore_rate_limits(pid, originals)
            if failed:
                print(f"[pending-track-unseal] {proj_name}/{track}: "
                      f"{failed}/{len(originals)} failed — will retry next poll")
                continue
            usage.pop_pending_track_project(track, pid)
            if names:
                _broadcast_named(
                    lambda n, p=proj_name, t=track:
                        fmt_track_unseal_for_project(p, t, "day rollover", n),
                    subs, names,
                )
            else:
                _send_all(fmt_track_unseal_for_project(proj_name, track,
                                                      "day rollover"), subs)


# ── Formatters — seal/unseal alerts ────────────────────────────────────────

def fmt_seal_alert(proj_name: str, reason: str, num_limits: int,
                   name: str = "Bach") -> str:
    return (
        f"🔒 <b>PROJECT SEALED — {proj_name}</b>\n\n"
        f"Reason: {reason}\n"
        f"This project has been <b>fully sealed by command</b>. All {num_limits} "
        f"rate-limit row(s) are throttled to 0 — no token-burning call can succeed.\n\n"
        f"Auto-restore at the next UTC midnight.\n"
        f"To restore one track now: <code>@bot archive unseal {proj_name} normal</code> "
        f"or <code>… premium</code>.\n"
        f"To restore everything: <code>@bot archive unseal {proj_name}</code>.\n"
        f"<i>Monarch {name}, the seal has been laid.</i>"
    )


def fmt_unseal_alert(proj_name: str, reason: str, tracks, name: str = "Bach") -> str:
    """`tracks` is an iterable of restored track names ('normal', 'premium')."""
    tracks_list  = list(tracks)
    track_str    = " + ".join(tracks_list) if len(tracks_list) < 2 else "both tracks"
    exempt_tail  = (
        f"This project is now exempt from auto track-seals on <b>{track_str}</b> for "
        f"the rest of the UTC day. The overcap alarm still fires if it burns past a cap."
    )
    return (
        f"🔓 <b>PROJECT UNSEALED — {proj_name} ({track_str})</b>\n\n"
        f"Reason: {reason}\n"
        f"{exempt_tail}\n"
        f"<i>Monarch {name}, the seal on {track_str} has been lifted.</i>"
    )


def fmt_track_seal_starting(track: str, consumed: int, cap: int,
                            name: str = "Bach") -> str:
    pct       = consumed / cap * 100 if cap else 100
    remaining = cap - consumed
    band      = "Normal (10M)" if track == "normal" else "Premium (1M)"
    return (
        f"🛑 <b>{band} TRACK NEAR CAP — MASS THROTTLE STARTING</b>\n\n"
        f"Consumed: <b>{_fmt_tokens(consumed)}</b> / {_fmt_tokens(cap)} ({pct:.1f}%)\n"
        f"Remaining: <b>{_fmt_tokens(remaining)}</b> ({100 - pct:.1f}%)\n\n"
        f"Throttling every project's <b>{track}</b>-band rate-limit rows to 0 "
        f"to prevent the daily cap from being breached. This takes ~30–60 s.\n"
        f"<i>Monarch {name}, the throttle is going down.</i>"
    )


def fmt_track_seal_complete(track: str, throttled: list, skipped_exempt: list,
                            skipped_manual: list, failed: list,
                            name: str = "Bach") -> str:
    band  = "Normal (10M)" if track == "normal" else "Premium (1M)"
    lines = [
        f"🔒 <b>{band} TRACK SEALED — mass throttle complete</b>\n",
        f"Projects throttled: <b>{len(throttled)}</b>",
    ]
    if skipped_exempt:
        lines.append(f"Skipped (manually exempt): {len(skipped_exempt)} — "
                     + ", ".join(KNOWN_PROJECTS.get(p, p) for p in skipped_exempt))
    if skipped_manual:
        lines.append(f"Skipped (manually sealed): {len(skipped_manual)} — "
                     + ", ".join(KNOWN_PROJECTS.get(p, p) for p in skipped_manual))
    if failed:
        lines.append(f"⚠️ Failed: {len(failed)} — "
                     + ", ".join(KNOWN_PROJECTS.get(p, p) for p in failed))
    lines.append("")
    lines.append(f"Auto-restore at the next UTC midnight.")
    lines.append(f"To exempt a project from this seal: <code>@bot archive unseal &lt;project&gt; {track}</code>")
    lines.append(f"<i>Monarch {name}, the {track} treasury is guarded.</i>")
    return "\n".join(lines)


def fmt_track_unseal_for_project(proj_name: str, track: str, reason: str,
                                 name: str = "Bach") -> str:
    return (
        f"🔓 <b>{proj_name}</b> · {track}-track restored\n"
        f"Reason: {reason}\n"
        f"<i>Monarch {name}, the project resumes normal operation on {track}.</i>"
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
        f"🔢 Tokens: <b>{_fmt_tokens(total_tok)}</b>   💰 Cost: <b>${total_cost:.4f}</b> / ${DAILY_LIMIT:.2f}{cost_note}\n"
        f"   ⭐ Premium (1M): <b>{_fmt_tokens(total_premium)}</b> / 1M"
        f"   •   📦 Normal (10M): <b>{_fmt_tokens(total_normal)}</b> / 10M"
    )
    return "\n".join(lines)


# ── Milestone checker ──────────────────────────────────────────────────────

def seed_milestones(snap: dict, usage: UsageStore,
                    subs: "SubscriberStore" = None, names: "NameStore" = None) -> None:
    """Fire only the highest already-crossed milestone per track on first poll of a day.
    Idempotent: early-returns if already seeded — guards against double-broadcast when
    /refresh and the poll loop race to seed first."""
    if usage.has_seeded():
        return

    total_normal  = snap.get("total_normal_tokens", 0)
    total_premium = snap.get("total_premium_tokens", 0)

    normal_crossed  = [(t, l) for t, l in TOKEN_MILESTONES         if total_normal  >= t]
    premium_crossed = [(t, l) for t, l in PREMIUM_TOKEN_MILESTONES if total_premium >= t]

    # One disk write marks every crossed threshold and flips milestones_seeded.
    usage.seed_state(
        normal_thresholds  = [t for t, _ in normal_crossed],
        premium_thresholds = [t for t, _ in premium_crossed],
    )

    if normal_crossed and subs:
        t, l = normal_crossed[-1]   # highest crossed
        if names:
            _broadcast_named(
                lambda n, t=t, c=total_normal, l=l: fmt_token_milestone(t, c, l, n),
                subs, names,
            )
        else:
            _send_all(fmt_token_milestone(t, total_normal, l), subs)

    if premium_crossed and subs:
        t, l = premium_crossed[-1]
        if names:
            _broadcast_named(
                lambda n, t=t, c=total_premium, l=l: fmt_premium_token_milestone(t, c, l, n),
                subs, names,
            )
        else:
            _send_all(fmt_premium_token_milestone(t, total_premium, l), subs)


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
            if names:
                _broadcast_named(lambda n, t=threshold, c=total_tok, l=level: fmt_token_milestone(t, c, l, n), subs, names)
            else:
                _send_all(fmt_token_milestone(threshold, total_tok, level), subs)

    # Premium band (1M free daily)
    total_premium    = snap.get("total_premium_tokens", 0)
    notified_premium = usage.get_premium_milestones_notified()
    for threshold, level in PREMIUM_TOKEN_MILESTONES:
        if total_premium >= threshold and threshold not in notified_premium:
            hit = True
            usage.add_premium_milestone_notified(threshold)
            if names:
                _broadcast_named(lambda n, t=threshold, c=total_premium, l=level: fmt_premium_token_milestone(t, c, l, n), subs, names)
            else:
                _send_all(fmt_premium_token_milestone(threshold, total_premium, level), subs)

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
            lines.append(f"   <code>{model}</code>  {mi} in / {mo} out  ({mr:,} reqs)")
        lines.append("")

    total_premium = snap.get("total_premium_tokens", 0)
    total_normal  = snap.get("total_normal_tokens",  0)
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(
        f"🔢 Total: <b>{_fmt_tokens(total_tok)}</b>  •  {total_req:,} requests\n"
        f"   ⭐ Premium (1M): <b>{_fmt_tokens(total_premium)}</b> / 1M"
        f"   •   📦 Normal (10M): <b>{_fmt_tokens(total_normal)}</b> / 10M"
    )
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


# ── Archive (seal/unseal) command ──────────────────────────────────────────

def _resolve_project(token: str) -> Optional[str]:
    """Map a human-typed token (project name or proj_ ID) to a known project ID."""
    if not token:
        return None
    token = token.strip()
    if token in KNOWN_PROJECTS:
        return token
    needle = token.lower()
    for pid, pname in KNOWN_PROJECTS.items():
        if pname.lower() == needle:
            return pid
    return None


def cmd_archive(args: list, usage: UsageStore, subs: SubscriberStore,
                names: NameStore = None, name: str = "Bach") -> str:
    """Sub-command driven archive controller.

    Forms:
      archive                                → status: track seals + per-project state
      archive seal <name|id>                 → manually FULL-seal a project (both tracks)
      archive unseal <name|id>               → manually unseal both tracks for a project
      archive unseal <name|id> <track>       → manually unseal only that track ('normal' or 'premium')

    Note: the bot also mass-throttles every project's rate limits for a track
    whenever that track passes 95% of its daily cap. Manual `unseal` for a track
    marks the project as exempt from that day's mass throttle on that track."""
    sub = args[0].lower() if args else "list"

    if sub == "list":
        return _fmt_archive_status(usage, name)

    if sub == "seal":
        if len(args) < 2:
            return (f"Usage: <code>@bot archive seal &lt;project&gt;</code>\n"
                    f"Try <code>@bot archive</code> for the project list, Monarch {name}.")
        target = " ".join(args[1:])
        pid    = _resolve_project(target)
        if not pid:
            return f"Unknown project <code>{target}</code>, Monarch {name}. Try <code>@bot archive</code>."
        if usage.is_sealed(pid):
            return f"<b>{KNOWN_PROJECTS.get(pid, pid)}</b> is already fully sealed."
        proj_name = KNOWN_PROJECTS.get(pid, pid)
        reason    = f"manual full seal by Monarch {name}"
        ok        = _seal_project(usage, subs, names, pid, reason)
        if ok:
            return f"🔒 <b>{proj_name}</b> fully sealed.\n<i>Order carried out, My Liege {name}.</i>"
        return f"⚠️ Failed to seal <b>{proj_name}</b> — check logs, Monarch {name}."

    if sub == "unseal":
        if len(args) < 2:
            return (f"Usage: <code>@bot archive unseal &lt;project&gt; [normal|premium]</code>\n"
                    f"Try <code>@bot archive</code> for the project list, Monarch {name}.")
        # Detect an optional trailing track keyword.
        tail = args[-1].lower()
        if tail in ("normal", "premium"):
            target = " ".join(args[1:-1])
            track  = tail
        else:
            target = " ".join(args[1:])
            track  = None   # full unseal (both tracks)

        pid = _resolve_project(target)
        if not pid:
            return f"Unknown project <code>{target}</code>, Monarch {name}. Try <code>@bot archive</code>."

        # Was the project actually sealed for the requested track(s)?
        sealed_full  = usage.get_sealed_info(pid)
        sealed_tks   = usage.get_sealed_tracks()
        affected = []
        for t in (("normal", "premium") if track is None else (track,)):
            in_track_seal = pid in sealed_tks.get(t, {}).get("originals_by_project", {})
            in_full_seal  = bool(sealed_full and any(
                _matches_track(o.get("model", ""), t)
                for o in sealed_full.get("original_limits", [])
            ))
            if in_track_seal or in_full_seal:
                affected.append(t)

        if not affected:
            scope = "any track" if track is None else f"the {track} track"
            return f"<b>{KNOWN_PROJECTS.get(pid, pid)}</b> is not sealed on {scope}, Monarch {name}."

        reason = (f"manual {'full' if track is None else track + '-track'} "
                  f"unseal by Monarch {name}")
        ok = _unseal_project(usage, subs, names, pid, track, reason)
        proj_name = KNOWN_PROJECTS.get(pid, pid)
        if ok:
            scope = "both tracks" if track is None else f"the {track} track"
            return (f"🔓 <b>{proj_name}</b> restored on {scope} and exempt from auto-seal "
                    f"until UTC midnight.")
        return f"⚠️ Failed to unseal <b>{proj_name}</b> — check logs."

    return (f"Unknown <code>archive</code> sub-command <code>{sub}</code>, Monarch {name}. "
            f"Use <code>list</code>, <code>seal &lt;p&gt;</code>, "
            f"or <code>unseal &lt;p&gt; [normal|premium]</code>.")


def _fmt_archive_status(usage: UsageStore, name: str = "Bach") -> str:
    snap          = usage.get()
    sealed_tracks = usage.get_sealed_tracks()
    sealed_full   = usage.get_sealed_projects()
    exemptions    = usage.get_track_exemptions()
    pending_full  = usage.get_pending_unseal()
    pending_tracks= usage.get_pending_track_unseal()

    n_tok = snap.get("total_normal_tokens",  0)
    p_tok = snap.get("total_premium_tokens", 0)

    lines = [f"🗃️ <b>Archive Status — {snap.get('date', today_str())}</b>\n"]

    # ── Track usage + seal state
    lines.append("<b>Tracks</b>")
    for track, consumed, cap, threshold in (
        ("normal",  n_tok, TOKEN_HARD_CAP,         NORMAL_TRACK_SEAL_THRESHOLD),
        ("premium", p_tok, PREMIUM_TOKEN_HARD_CAP, PREMIUM_TRACK_SEAL_THRESHOLD),
    ):
        pct = consumed / cap * 100 if cap else 0
        if track in sealed_tracks:
            n_projects = len(sealed_tracks[track].get("originals_by_project", {}))
            tag = f"🔒 <b>SEALED</b> ({n_projects} project(s) throttled)"
        elif consumed >= threshold:
            tag = f"⚠️ threshold crossed but not yet sealed"
        else:
            tag = f"✅ active"
        lines.append(f"  • <b>{track}</b>: {_fmt_tokens(consumed)} / {_fmt_tokens(cap)} "
                     f"({pct:.1f}%)  —  {tag}")
    lines.append("")

    # ── Per-project breakdown
    lines.append("<b>Projects</b>")
    for pid, proj_name in sorted(KNOWN_PROJECTS.items(), key=lambda kv: kv[1]):
        tags = []
        if pid in sealed_full:
            tags.append("🔒 full-sealed")
        for t in ("normal", "premium"):
            if pid in sealed_tracks.get(t, {}).get("originals_by_project", {}):
                tags.append(f"🔒 {t}")
        for t in exemptions.get(pid, []):
            tags.append(f"🔓 {t}-exempt")
        if pid in pending_full:
            tags.append("⏳ full-restore pending")
        for t in pending_tracks:
            if pid in pending_tracks[t].get("originals_by_project", {}):
                tags.append(f"⏳ {t}-restore pending")
        status = " · ".join(tags) if tags else "✅ active"
        lines.append(f"  • <b>{proj_name}</b> — {status}")
    lines.append("")

    lines.append(
        "Commands: <code>seal &lt;project&gt;</code> · "
        "<code>unseal &lt;project&gt; [normal|premium]</code>"
    )
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
        total_tok     = sum(p.get("total_tokens", 0) for p in snap.get("projects", {}).values())
        total_premium = snap.get("total_premium_tokens", 0)
        total_normal  = snap.get("total_normal_tokens",  0)

        mode             = usage.get_mode()
        mode_note        = ""
        normal_exceeded  = total_normal  >= TOKEN_HARD_CAP
        premium_exceeded = total_premium >= PREMIUM_TOKEN_HARD_CAP

        # Track-seal triggers — same logic as the poll loop, fired on demand
        # so /refresh near the threshold doesn't have to wait for the next poll.
        if total_normal >= NORMAL_TRACK_SEAL_THRESHOLD and not usage.is_track_sealed("normal"):
            _handle_track_seal("normal", snap, usage, subs, names)
            mode_note = "\n🛑 Normal track passed 95% — mass throttle complete."
        if total_premium >= PREMIUM_TRACK_SEAL_THRESHOLD and not usage.is_track_sealed("premium"):
            _handle_track_seal("premium", snap, usage, subs, names)
            mode_note = "\n🛑 Premium track passed 95% — mass throttle complete."

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
                    if names:
                        _broadcast_named(
                            lambda n, r=illegal, ne=normal_exceeded, pe=premium_exceeded:
                                fmt_overcap_active_alert(r, ne, pe, n),
                            subs, names,
                        )
                    else:
                        _send_all(fmt_overcap_active_alert(illegal, normal_exceeded, premium_exceeded), subs)
                    mode_note = "\n🔴 <b>AGGRESSIVE mode active — exempt projects burning the exhausted band, broadcast sent.</b>"
                else:
                    mode_note = "\n⚠️ Budget cap exceeded — no projects active on the exhausted band right now."
        elif new_milestone and mode == "passive":
            usage.set_mode("urgent")
            usage.set_last_milestone_ts(time.time())
            mode_note = "\n📊 Milestone crossed — switched to <b>URGENT</b> polling mode."

        stale_note = f"  <i>(cost as of {_fmt_ts(enriched.get('costs_ts'))})</i>" if enriched.get("costs_stale") else ""
        lines = [
            "🔄 <b>Data refreshed.</b>",
            f"Tokens today: <b>{_fmt_tokens(total_tok)}</b>",
            f"   ⭐ Premium (1M):  <b>{_fmt_tokens(total_premium)}</b> / 1M",
            f"   📦 Normal (10M): <b>{_fmt_tokens(total_normal)}</b> / 10M",
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
            f"🔹 <code>{model}</code>\n"
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
        f"<b>Archive (rate-limit seal)</b>\n"
        f"<code>{m} archive</code>                          — Show track-seal + per-project status\n"
        f"<code>{m} archive seal &lt;project&gt;</code>             — Manually full-seal a project (both tracks → 0)\n"
        f"<code>{m} archive unseal &lt;project&gt;</code>           — Restore both tracks; exempt until UTC midnight\n"
        f"<code>{m} archive unseal &lt;project&gt; normal</code>    — Restore only normal-band (mini/nano models)\n"
        f"<code>{m} archive unseal &lt;project&gt; premium</code>   — Restore only premium-band (full-size models)\n"
        f"<i>Bot also auto mass-throttles every project at 95% utilisation per track.</i>\n\n"
        f"<b>Notifications</b>\n"
        f"<code>{m} arise</code>      — Subscribe this chat to all alerts\n"
        f"<code>{m} dismiss</code>    — Unsubscribe this chat\n"
        f"<code>{m} setname Name</code> — Set the name the bot uses to address you\n\n"
        f"<code>{m} help</code>       — This registry\n\n"
        f"<i>Your Majesty {name}, your command is my directive.</i>"
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
             thread_id: int = None) -> Optional[str]:
    rest = _match_prefix(text, bot_username)
    if rest is None:
        return None

    parts = rest.split()
    cmd   = parts[0].lower() if parts else "help"

    name = names.get(chat_id) if names else "Bach"

    if cmd == "setname":
        new_name = " ".join(parts[1:]) if len(parts) > 1 else ""
        return cmd_setname(chat_id, new_name, names) if names else "Name store unavailable."

    if cmd == "archive":
        return cmd_archive(parts[1:], usage, subs, names, name)

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
        return handler()

    mention = f"@{bot_username}" if bot_username else "@bot"
    return f"Unknown command: <code>{cmd}</code>. Use <code>{mention} help</code> for the registry."


# ── Telegram poll thread ───────────────────────────────────────────────────

def telegram_poll_loop(usage: UsageStore, subs: SubscriberStore,
                       bot_username: Optional[str], names: NameStore = None) -> None:
    offset = 0
    while True:
        updates = _get_updates(offset)
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
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
                reply = dispatch(text, usage, subs, bot_username, chat_id, names, thread_id)
                if reply:
                    _send(reply, chat_id, thread_id)
            except Exception as e:
                print(f"[telegram handler error] {e}")


# ── Usage poll thread ──────────────────────────────────────────────────────

def usage_poll_loop(usage: UsageStore, subs: SubscriberStore, names: NameStore = None) -> None:
    """Day rollover is handled by UsageStore.update() (auto-detects date change) and by
    the constructor's stale-date check. seed-vs-check is driven by has_seeded()."""
    fail_count = 0

    while True:
        try:
            snap = fetch_today_usage()
            if snap:
                # Overlay costs; fall back to cache on failure.
                costs    = _fetch_costs()
                org_cost = 0.0
                if costs:
                    org_cost = costs.pop("__org__", 0.0)
                    for pid, p in snap.get("projects", {}).items():
                        p["cost_usd"] = round(costs.get(pid, 0.0), 6)
                    snap["total_cost"] = round(sum(costs.values()) + org_cost, 6)
                else:
                    cached_costs = usage.get_costs_cache()
                    if cached_costs:
                        per_proj = cached_costs.get("per_project", {})
                        for pid, p in snap.get("projects", {}).items():
                            p["cost_usd"] = round(per_proj.get(pid, 0.0), 6)
                        snap["total_cost"] = cached_costs.get("total", 0.0)

                usage.update(snap)   # auto-resets daily state on day rollover
                if costs:
                    usage.update_costs(costs, snap["total_cost"], org_cost)

                # If yesterday's sealed projects were just moved to pending_unseal by
                # the daily reset, restore them via the API now. Failures stay in the
                # queue and are retried on the next iteration.
                _process_pending_unseals(usage, subs, names)
                _process_pending_track_unseals(usage, subs, names)

                normal_tok  = snap.get("total_normal_tokens",  0)
                premium_tok = snap.get("total_premium_tokens", 0)
                n_str    = _color(f"{_fmt_tokens(normal_tok)}/10M",  _tok_color(normal_tok,  TOKEN_HARD_CAP))
                p_str    = _color(f"{_fmt_tokens(premium_tok)}/1M",  _tok_color(premium_tok, PREMIUM_TOKEN_HARD_CAP))
                cost_str = f"  cost=${snap.get('total_cost', 0.0):.4f}" if snap.get("total_cost") else ""
                print(f"[poll/{usage.get_mode()}] {snap.get('date')}  normal={n_str}  premium={p_str}{cost_str}")

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

                # ── Track-level mass throttle (preventive, at 95% utilisation) ──
                # Each track is independent and idempotent. Once sealed_tracks[track]
                # exists for today, no re-trigger fires.
                if normal_tok >= NORMAL_TRACK_SEAL_THRESHOLD and not usage.is_track_sealed("normal"):
                    _handle_track_seal("normal", snap, usage, subs, names)
                if premium_tok >= PREMIUM_TRACK_SEAL_THRESHOLD and not usage.is_track_sealed("premium"):
                    _handle_track_seal("premium", snap, usage, subs, names)

                # ── Cap check (alarm-only; mass throttle above should normally
                #    keep this from firing except for manually-exempt projects)
                normal_exceeded  = normal_tok  >= TOKEN_HARD_CAP
                premium_exceeded = premium_tok >= PREMIUM_TOKEN_HARD_CAP

                if normal_exceeded or premium_exceeded:
                    _handle_overcap(usage, subs, names, normal_exceeded, premium_exceeded)
                else:
                    mode = usage.get_mode()
                    if mode == "urgent":
                        last_ms = usage.get_last_milestone_ts()
                        if last_ms and time.time() - last_ms > URGENT_REVERT_SECS:
                            usage.set_mode("passive")
                            print("[mode] → PASSIVE (1 h without new milestone)")
                    elif mode == "aggressive":
                        # Both caps cleared (likely day rollover handled by update())
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
                        if names:
                            _broadcast_named(lambda n, a=active: fmt_concurrency_alert(a, n), subs, names)
                        else:
                            _send_all(fmt_concurrency_alert(active), subs)
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

    bot_username = _fetch_bot_username()
    if bot_username:
        print(f"[bot] @{bot_username} ready")
    else:
        print("[bot] WARNING: Could not resolve username — commands will not work")

    threading.Thread(target=telegram_poll_loop,     args=(usage, subs, bot_username, names), daemon=True).start()
    threading.Thread(target=usage_poll_loop,        args=(usage, subs, names),               daemon=True).start()
    threading.Thread(target=concurrency_check_loop, args=(usage, subs, names),               daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("[bot] Shutdown signal received. Standing down.")


if __name__ == "__main__":
    main()
