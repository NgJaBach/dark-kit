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
POLL_INTERVAL    = int(os.environ.get("POLL_INTERVAL_MINS", "5")) * 60

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
SPEND_ALERT_INTERVAL = 2.00        # alert every $2 of daily spend after cap

# OpenAI costs API has a ~5-minute ingestion delay (documented).
# We lag end_time by 10 minutes as a safe buffer. If fewer than
# COST_DATA_DELAY_SECS have elapsed since midnight, costs are skipped
# (no data would exist yet anyway).
COST_DATA_DELAY_SECS = 600  # 10 minutes

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
}

OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"
OPENAI_USAGE_URL = "https://api.openai.com/v1/organization/usage/completions"

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
    """
    Returns True if the model is in the 1M/day free-tier band (premium),
    False if it's in the 10M/day band (mini/nano models).
    Rule: any model containing 'mini' or 'nano' is in the normal (10M) band.
    Everything else (full-size models) is in the premium (1M) band.
    """
    m = model.lower()
    return "mini" not in m and "nano" not in m


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


def _fetch_recent_activity(minutes: int = CONCURRENCY_WINDOW_MINS) -> dict[str, int]:
    """Request count per project in the last `minutes` minutes (minute-level buckets)."""
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
        return {}
    if not r.ok:
        print(f"[openai activity {r.status_code}] {r.text[:300]}")
        return {}
    activity: dict[str, int] = {}
    for bucket in r.json().get("data", []):
        for result in bucket.get("results", []):
            pid  = result.get("project_id", "")
            reqs = result.get("num_model_requests", 0)
            if pid and reqs > 0:
                activity[pid] = activity.get(pid, 0) + reqs
    return activity


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
    )

    def __init__(self, path: Path):
        self.path  = path
        self._lock = threading.Lock()
        self._data: dict = {}
        self._load()

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

    def update(self, snapshot: dict):
        """Merge new snapshot, preserving all alert-control fields."""
        with self._lock:
            preserved = {k: self._data[k] for k in self._PRESERVED if k in self._data}
            self._data = snapshot
            self._data.update(preserved)
            self._save()

    def reset_day(self):
        """Called at UTC midnight — resets all daily alert state."""
        with self._lock:
            self._data["alert_sent"]                  = False
            self._data["token_milestones_notified"]   = []
            self._data["premium_milestones_notified"] = []
            self._data["spend_intervals_notified"]    = 0
            self._data.pop("costs_cache", None)
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


def fmt_limit_alert(total_cost: float, name: str = "Bach") -> str:
    return (
        f"⚠️ <b>Daily Spend Limit Reached — ${total_cost:.4f}</b>\n\n"
        f"Daily spend has reached the ${DAILY_LIMIT:.2f} limit.\n"
        f"<i>Monarch {name}, your war chest requires attention.</i>"
    )


def fmt_post_limit_alert(total_cost: float, level: int, name: str = "Bach") -> str:
    """Escalating drama for each $2 interval above the $5 daily limit."""
    if level == 1:
        return (
            f"⚠️ <b>Expenditure Continues — ${total_cost:.4f} today</b>\n\n"
            f"The ${DAILY_LIMIT:.0f} limit has been surpassed and spending persists.\n"
            f"My Liege {name}, this warrants immediate review."
        )
    if level == 2:
        return (
            f"🚨 <b>Sustained Excess — ${total_cost:.4f} today</b>\n\n"
            f"Your organization has now spent <b>${total_cost:.2f}</b> in a single day. "
            f"This is ${total_cost - DAILY_LIMIT:.2f} beyond the sanctioned limit.\n"
            f"Monarch {name} — your intervention is required."
        )
    if level == 3:
        return (
            f"‼️ <b>CRITICAL EXPENDITURE — ${total_cost:.4f} today</b>\n\n"
            f"Three thresholds have been breached. The budget is uncontrolled.\n"
            f"All active projects should be reviewed immediately.\n"
            f"<b>{name} the Monarch — this cannot continue without acknowledgment.</b>"
        )
    return (
        f"🔴 <b>UNRESTRAINED SPEND — ${total_cost:.4f} today</b>\n\n"
        f"Your daily expenditure has reached <b>${total_cost:.2f}</b> — "
        f"<b>${total_cost - DAILY_LIMIT:.2f} above the limit</b>.\n"
        f"Budget integrity has collapsed. Halt all non-essential operations.\n\n"
        f"<b>MONARCH {name.upper()}. THE LEDGER IS BLEEDING. YOUR COMMAND IS REQUIRED.</b>"
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

def check_milestones(snap: dict, usage: UsageStore, subs: SubscriberStore, names: NameStore = None) -> None:
    """Called after every poll. Fires token milestone alerts for both bands."""
    # Normal band (10M free daily)
    total_tok = snap.get("total_normal_tokens", 0)
    notified  = usage.get_milestones_notified()
    for threshold, level in TOKEN_MILESTONES:
        if total_tok >= threshold and threshold not in notified:
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
            usage.add_premium_milestone_notified(threshold)
            if names:
                _broadcast_named(lambda n, t=threshold, c=total_premium, l=level: fmt_premium_token_milestone(t, c, l, n), subs, names)
            else:
                _send_all(fmt_premium_token_milestone(threshold, total_premium, level), subs)


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
            name = KNOWN_PROJECTS.get(pid, pid)
            lines.append(f"• <b>{name}</b>  —  {count:,} requests")

        if len(active) >= CONCURRENCY_THRESHOLD:
            lines.append(f"\n⚠️ <b>{len(active)} projects active simultaneously.</b>")
        else:
            lines.append(f"\n{len(active)} project(s) active. No concurrency threshold reached.")

    lines.append(f"\n<i>This snapshot reflects the last concurrency check, Monarch {name}.</i>")
    return "\n".join(lines)


def cmd_refresh(usage: UsageStore, subs: SubscriberStore, name: str = "Bach") -> str:
    snap = fetch_today_usage()
    if snap:
        usage.update(snap)
        check_milestones(snap, usage, subs)
        enriched      = _enrich_costs(snap, usage)
        total         = enriched.get("total_cost", 0.0)
        total_tok     = sum(p.get("total_tokens", 0) for p in snap.get("projects", {}).values())
        total_premium = snap.get("total_premium_tokens", 0)
        total_normal  = snap.get("total_normal_tokens",  0)
        stale_note    = f"  <i>(cost as of {_fmt_ts(enriched.get('costs_ts'))})</i>" if enriched.get("costs_stale") else ""
        lines = [
            f"🔄 <b>Data refreshed.</b>",
            f"Tokens today: <b>{_fmt_tokens(total_tok)}</b>",
            f"   ⭐ Premium (1M):  <b>{_fmt_tokens(total_premium)}</b> / 1M",
            f"   📦 Normal (10M): <b>{_fmt_tokens(total_normal)}</b> / 10M",
            f"Spend today:  <b>${total:.4f}</b>{stale_note}",
            f"<i>Intelligence updated, Monarch {name}.</i>",
        ]
        return "\n".join(lines)
    # Token fetch failed — return last cached snapshot
    cached = usage.get()
    if cached and cached.get("projects"):
        enriched = _enrich_costs(cached, usage, live=False)
        return (
            f"⚠️ <b>OpenAI API unreachable — showing last known data.</b>\n\n"
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

    routes = {
        "tokens":   lambda: cmd_tokens(usage, name),
        "projects": lambda: cmd_projects(usage, name),
        "rank":     lambda: cmd_rank(usage, name),
        "spending": lambda: cmd_spending(name),
        "recent":   lambda: cmd_recent(name),
        "models":   lambda: cmd_models(usage, name),
        "active":   lambda: cmd_active(usage, name),
        "refresh":  lambda: cmd_refresh(usage, subs, name),
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
    # Reset on startup if persisted state is from a previous day
    persisted_date = usage.get().get("date")
    last_date      = today_str()
    if persisted_date and persisted_date != last_date:
        usage.reset_day()
        print(f"[poll] Stale state from {persisted_date} — daily state reset on startup")

    fail_count = 0

    while True:
        try:
            current_date = today_str()
            if current_date != last_date:
                usage.reset_day()
                last_date = current_date
                print(f"[poll] New day {current_date} — daily state reset")

            snap = fetch_today_usage()
            if snap:
                # Fetch costs and overlay onto snapshot; fall back to cache on failure
                costs = _fetch_costs()
                if costs:
                    org_cost = costs.pop("__org__", 0.0)
                    for pid, p in snap.get("projects", {}).items():
                        p["cost_usd"] = round(costs.get(pid, 0.0), 6)
                    snap["total_cost"] = round(sum(costs.values()) + org_cost, 6)
                    usage.update_costs(costs, snap["total_cost"], org_cost)
                else:
                    cached_costs = usage.get_costs_cache()
                    if cached_costs:
                        per_proj = cached_costs.get("per_project", {})
                        for pid, p in snap.get("projects", {}).items():
                            p["cost_usd"] = round(per_proj.get(pid, 0.0), 6)
                        snap["total_cost"] = cached_costs.get("total", 0.0)

                usage.update(snap)
                normal_tok  = snap.get("total_normal_tokens", 0)
                premium_tok = snap.get("total_premium_tokens", 0)
                n_str    = _color(f"{_fmt_tokens(normal_tok)}/10M",  _tok_color(normal_tok,  TOKEN_HARD_CAP))
                p_str    = _color(f"{_fmt_tokens(premium_tok)}/1M",  _tok_color(premium_tok, PREMIUM_TOKEN_HARD_CAP))
                cost_str = f"  cost=${snap.get('total_cost', 0.0):.4f}" if snap.get("total_cost") else ""
                print(f"[poll] {current_date}  normal={n_str}  premium={p_str}{cost_str}")

                check_milestones(snap, usage, subs, names)
                fail_count = 0
            else:
                fail_count += 1
                wait = min(POLL_INTERVAL * (2 ** min(fail_count - 1, 6)), 3600)
                print(f"[poll] Fetch failed ({fail_count}) — retry in {wait // 60:.0f}min (backoff)")
                time.sleep(wait)
                continue

        except Exception as e:
            print(f"[poll loop error] {e}")
            fail_count += 1

        time.sleep(POLL_INTERVAL)


# ── Concurrency check thread ───────────────────────────────────────────────

def concurrency_check_loop(usage: UsageStore, subs: SubscriberStore, names: NameStore = None) -> None:
    """Every CONCURRENCY_WINDOW_MINS minutes, check for simultaneous project activity."""
    while True:
        try:
            activity = _fetch_recent_activity(CONCURRENCY_WINDOW_MINS)
            active   = {pid: count for pid, count in activity.items() if count > 0}
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
