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
THREAD_ID        = int(os.environ["TELEGRAM_THREAD_ID"]) if os.environ.get("TELEGRAM_THREAD_ID") else None
DAILY_LIMIT      = 5.00   # hardcoded — alerts fire at $5, then every $2 above
POLL_INTERVAL    = int(os.environ.get("POLL_INTERVAL_MINS", "60")) * 60

BOT_DATA_DIR     = Path(__file__).parent / "bot_data"
USAGE_STATE_PATH = BOT_DATA_DIR / "usage_state.json"
SUBS_PATH        = BOT_DATA_DIR / "subscribers.json"
NAMES_PATH       = BOT_DATA_DIR / "names.json"

REQUEST_TIMEOUT  = 15
POLL_TIMEOUT     = 30  # Telegram long-poll

# ── Token milestone config ──────────────────────────────────────────────────
# (threshold, level)  level: "casual" | "urgent" | "cap"
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

# ── Concurrent project detection ────────────────────────────────────────────
CONCURRENCY_THRESHOLD   = 3    # alert if this many projects active simultaneously
CONCURRENCY_WINDOW_MINS = 5    # "active" = had requests within last N minutes
CONCURRENCY_COOLDOWN    = 900  # seconds between concurrency alerts (15 min)

# ── Known projects (IDs from exported CSV — case-sensitive) ─────────────────
KNOWN_PROJECTS: dict[str, str] = {
    "proj_Gkm7qFbBFgmW11VFtO13Uw3F": "Default project",      # O not 0
    "proj_9su0tGI8NsaLE7LHqikCw8VE": "cngvng-project",       # i not 1
    "proj_4VPu8UTHzBpZiHFQVaYG923d": "hoangha-project",
    "proj_fvkY21dJ0ripiOIA2jCC86f3": "namvuong-project",
    "proj_fEboQnaVm4tQCk8kFy0h8s08": "khonlanh-project",
    "proj_zRWDq4YWIDEkxbgMAjX0xy79": "phongnguyen-project",  # RW, j, X
    "proj_J4rNEXilII2l889OotmE7YNW": "ngjabach-project",
}

OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"
OPENAI_USAGE_URL = "https://api.openai.com/v1/organization/usage/completions"


# ── Time helpers ───────────────────────────────────────────────────────────

def today_window() -> tuple[int, int]:
    now   = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int(now.timestamp())


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
    """Today's cost per project."""
    start, end = today_window()
    params = [
        ("start_time",   start),
        ("end_time",     end),
        ("bucket_width", "1d"),
        ("group_by[]",   "project_id"),
        ("limit",        30),
    ]
    try:
        r = requests.get(OPENAI_COSTS_URL, headers=_openai_headers(), params=params, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        print(f"[openai costs network error] {e}")
        return {}
    if not r.ok:
        print(f"[openai costs {r.status_code}] {r.text[:500]}")
        return {}
    costs: dict[str, float] = {}
    for bucket in r.json().get("data", []):
        for result in bucket.get("results", []):
            pid = result.get("project_id", "")
            val = result.get("amount", {}).get("value", 0.0)
            if pid:
                costs[pid] = costs.get(pid, 0.0) + val
    return costs


def _fetch_tokens() -> dict[str, dict]:
    """Today's token usage per project, broken down by model."""
    start, end = today_window()
    params = [
        ("start_time",   start),
        ("end_time",     end),
        ("bucket_width", "1h"),
        ("group_by[]",   "project_id"),
        ("group_by[]",   "model"),
        ("limit",        100),
    ]
    try:
        r = requests.get(OPENAI_USAGE_URL, headers=_openai_headers(), params=params, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        print(f"[openai usage network error] {e}")
        return {}
    if not r.ok:
        print(f"[openai usage {r.status_code}] {r.text[:500]}")
        return {}
    tokens: dict[str, dict] = {}
    for bucket in r.json().get("data", []):
        for result in bucket.get("results", []):
            pid   = result.get("project_id", "")
            model = result.get("model", "unknown")
            inp   = result.get("input_tokens", 0)
            out   = result.get("output_tokens", 0)
            reqs  = result.get("num_model_requests", 0)
            if not pid:
                continue
            if pid not in tokens:
                tokens[pid] = {"input_tokens": 0, "output_tokens": 0,
                               "total_tokens": 0, "num_requests": 0, "models": {}}
            tokens[pid]["input_tokens"]  += inp
            tokens[pid]["output_tokens"] += out
            tokens[pid]["total_tokens"]  += inp + out
            tokens[pid]["num_requests"]  += reqs
            m = tokens[pid]["models"].setdefault(model, {"input": 0, "output": 0, "requests": 0})
            m["input"] += inp; m["output"] += out; m["requests"] += reqs
    return tokens


def _fetch_monthly_costs(year: int, month: int) -> dict[str, float]:
    """Cost per project for a full calendar month."""
    start, end = month_window(year, month)
    params = [
        ("start_time",   start),
        ("end_time",     end),
        ("bucket_width", "1d"),
        ("group_by[]",   "project_id"),
        ("limit",        100),
    ]
    try:
        r = requests.get(OPENAI_COSTS_URL, headers=_openai_headers(), params=params, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        print(f"[openai monthly costs error] {e}")
        return {}
    if not r.ok:
        print(f"[openai monthly costs {r.status_code}] {r.text[:300]}")
        return {}
    costs: dict[str, float] = {}
    for bucket in r.json().get("data", []):
        for result in bucket.get("results", []):
            pid = result.get("project_id", "")
            val = result.get("amount", {}).get("value", 0.0)
            if pid:
                costs[pid] = costs.get(pid, 0.0) + val
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


def _fetch_week_data() -> list[dict]:
    """
    Returns last 7 days as [{date, label, tokens, cost, today}], oldest first.
    Makes two calls (costs + tokens) with daily buckets.
    """
    now   = datetime.now(timezone.utc)
    start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    start_ts, end_ts = int(start.timestamp()), int(now.timestamp())

    def _day(ts: int) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    tok_by_day:  dict[str, int]   = {}
    cost_by_day: dict[str, float] = {}

    # Tokens
    try:
        r = requests.get(OPENAI_USAGE_URL, headers=_openai_headers(), params=[
            ("start_time", start_ts), ("end_time", end_ts),
            ("bucket_width", "1d"), ("group_by[]", "project_id"), ("limit", 100),
        ], timeout=REQUEST_TIMEOUT)
        if r.ok:
            for bucket in r.json().get("data", []):
                ts = bucket.get("aggregation_timestamp", 0)
                if not ts:
                    continue
                d = _day(ts)
                for res in bucket.get("results", []):
                    tok_by_day[d] = tok_by_day.get(d, 0) + res.get("input_tokens", 0) + res.get("output_tokens", 0)
        else:
            print(f"[week tokens {r.status_code}] {r.text[:200]}")
    except Exception as e:
        print(f"[week tokens error] {e}")

    # Costs
    try:
        r = requests.get(OPENAI_COSTS_URL, headers=_openai_headers(), params=[
            ("start_time", start_ts), ("end_time", end_ts),
            ("bucket_width", "1d"), ("limit", 30),
        ], timeout=REQUEST_TIMEOUT)
        if r.ok:
            for bucket in r.json().get("data", []):
                ts = bucket.get("aggregation_timestamp", 0)
                if not ts:
                    continue
                d = _day(ts)
                for res in bucket.get("results", []):
                    cost_by_day[d] = cost_by_day.get(d, 0.0) + res.get("amount", {}).get("value", 0.0)
        else:
            print(f"[week costs {r.status_code}] {r.text[:200]}")
    except Exception as e:
        print(f"[week costs error] {e}")

    days = []
    for i in range(6, -1, -1):
        dt = now - timedelta(days=i)
        d  = dt.strftime("%Y-%m-%d")
        days.append({
            "date":   d,
            "label":  dt.strftime("%m/%d"),
            "tokens": tok_by_day.get(d, 0),
            "cost":   round(cost_by_day.get(d, 0.0), 4),
            "today":  (i == 0),
        })
    return days


def fetch_today_usage() -> Optional[dict]:
    costs  = _fetch_costs()
    tokens = _fetch_tokens()
    if not costs and not tokens:
        return None
    projects: dict[str, dict] = {}
    for pid in set(costs) | set(tokens):
        tok = tokens.get(pid, {})
        projects[pid] = {
            "name":          KNOWN_PROJECTS.get(pid, pid),
            "input_tokens":  tok.get("input_tokens", 0),
            "output_tokens": tok.get("output_tokens", 0),
            "total_tokens":  tok.get("total_tokens", 0),
            "num_requests":  tok.get("num_requests", 0),
            "cost_usd":      round(costs.get(pid, 0.0), 6),
            "models":        tok.get("models", {}),
        }
    return {
        "date":        today_str(),
        "projects":    projects,
        "total_cost":  round(sum(costs.values()), 6),
        "last_polled": time.time(),
    }


# ── Usage state store ──────────────────────────────────────────────────────

class UsageStore:
    """Persists today's usage snapshot and all alert-control state to disk."""

    # Fields that must survive snapshot updates (not overwritten on each poll)
    _PRESERVED = (
        "alert_sent",
        "token_milestones_notified",
        "spend_intervals_notified",
        "last_concurrent_alert_ts",
        "active_projects",
        "active_window_mins",
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
            self._data["alert_sent"]                = False
            self._data["token_milestones_notified"] = []
            self._data["spend_intervals_notified"]  = 0
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

def _send(text: str, chat_id: str = None, thread_id: int = None) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    target_chat = chat_id or CHAT_ID
    # Use configured THREAD_ID when sending to the primary chat and no thread override given
    effective_thread = thread_id if thread_id is not None else (THREAD_ID if target_chat == CHAT_ID else None)
    payload = {"chat_id": target_chat, "text": text, "parse_mode": "HTML"}
    if effective_thread:
        payload["message_thread_id"] = effective_thread
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
        f"🚨 <b>Free Token Allowance Exhausted — {c}</b>\n\n"
        f"The {t}-token daily free tier has been crossed.\n"
        f"Mini models (gpt-4o-mini, o1-mini, o3-mini, etc.) are now billing at standard rates.\n\n"
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

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🔢 Tokens: <b>{_fmt_tokens(total_tok)}</b>   💰 Cost: <b>${total_cost:.4f}</b> / ${DAILY_LIMIT:.2f}")
    return "\n".join(lines)


# ── Milestone checker ──────────────────────────────────────────────────────

def check_milestones(snap: dict, usage: UsageStore, subs: SubscriberStore, names: NameStore = None) -> None:
    """Called after every poll. Fires token milestone alerts (informational only)."""
    total_tok = sum(p.get("total_tokens", 0) for p in snap.get("projects", {}).values())
    notified  = usage.get_milestones_notified()
    for threshold, level in TOKEN_MILESTONES:
        if total_tok >= threshold and threshold not in notified:
            usage.add_milestone_notified(threshold)
            if names:
                _broadcast_named(lambda n, t=threshold, c=total_tok, l=level: fmt_token_milestone(t, c, l, n), subs, names)
            else:
                _send_all(fmt_token_milestone(threshold, total_tok, level), subs)


# ── Command handlers ───────────────────────────────────────────────────────

def cmd_today(usage: UsageStore, name: str = "Bach") -> str:
    snap = usage.get()
    if not snap or not snap.get("projects"):
        return f"No usage data on record, Monarch {name}."
    return fmt_daily_snapshot(snap)


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

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🔢 Total: <b>{_fmt_tokens(total_tok)}</b>  •  {total_req:,} requests")
    return "\n".join(lines)


def cmd_projects(usage: UsageStore, name: str = "Bach") -> str:
    snap      = usage.get()
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
                name = KNOWN_PROJECTS.get(pid, pid)
                lines.append(f"  • {name}: <b>${cost:.4f}</b>")
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
        total     = snap.get("total_cost", 0.0)
        total_tok = sum(p.get("total_tokens", 0) for p in snap.get("projects", {}).values())
        return (
            f"🔄 <b>Data refreshed.</b>\n"
            f"Tokens today: <b>{_fmt_tokens(total_tok)}</b>\n"
            f"Spend today:  <b>${total:.4f}</b>\n"
            f"<i>Intelligence updated, Monarch {name}.</i>"
        )
    return f"Could not reach the OpenAI API. Will retry on schedule, My Liege {name}."


def cmd_week(name: str = "Bach") -> str:
    days = _fetch_week_data()
    if not any(d["tokens"] or d["cost"] for d in days):
        return f"No weekly data available from the API, Monarch {name}."

    max_tok = max((d["tokens"] for d in days), default=1) or 1
    lines   = ["📅 <b>7-Day Rolling Trend</b>\n<code>"]

    for d in days:
        bar_w  = int(d["tokens"] / max_tok * 8)
        bar    = "█" * bar_w + "░" * (8 - bar_w)
        marker = " ◄ today" if d["today"] else ""
        tok    = _fmt_tokens(d["tokens"]).rjust(6)
        cost   = f"${d['cost']:.3f}".rjust(7)
        lines.append(f"{d['label']} [{bar}] {tok}  {cost}{marker}")

    lines.append("</code>")
    total_tok  = sum(d["tokens"] for d in days)
    total_cost = sum(d["cost"]   for d in days)
    lines.append(f"7-day total: <b>{_fmt_tokens(total_tok)}</b> tokens  •  <b>${total_cost:.4f}</b>")
    lines.append(f"\n<i>Monarch {name}, the weekly record is presented.</i>")
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


def cmd_arise(chat_id: str, subs: SubscriberStore, name: str = "Bach") -> str:
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
        f"<code>{m} today</code>      — Full token &amp; cost report\n"
        f"<code>{m} tokens</code>     — Per-project breakdown by model\n"
        f"<code>{m} models</code>     — Aggregate model usage across all projects\n"
        f"<code>{m} projects</code>   — Project roster with token bar chart\n"
        f"<code>{m} rank</code>       — Rankings by token use and spend\n"
        f"<code>{m} refresh</code>    — Force immediate poll\n\n"
        f"<b>Trends &amp; Spending</b>\n"
        f"<code>{m} week</code>       — 7-day rolling token &amp; spend trend\n"
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
             bot_username: Optional[str], chat_id: str, names: NameStore = None) -> Optional[str]:
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
        "today":    lambda: cmd_today(usage, name),
        "tokens":   lambda: cmd_tokens(usage, name),
        "projects": lambda: cmd_projects(usage, name),
        "rank":     lambda: cmd_rank(usage, name),
        "spending": lambda: cmd_spending(name),
        "week":     lambda: cmd_week(name),
        "models":   lambda: cmd_models(usage, name),
        "active":   lambda: cmd_active(usage, name),
        "refresh":  lambda: cmd_refresh(usage, subs, name),
        "arise":    lambda: cmd_arise(chat_id, subs, name),
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
                reply = dispatch(text, usage, subs, bot_username, chat_id, names)
                if reply:
                    _send(reply, chat_id, thread_id)
            except Exception as e:
                print(f"[telegram handler error] {e}")


# ── Usage poll thread ──────────────────────────────────────────────────────

def usage_poll_loop(usage: UsageStore, subs: SubscriberStore, names: NameStore = None) -> None:
    last_date = today_str()
    while True:
        try:
            current_date = today_str()
            if current_date != last_date:
                usage.reset_day()
                last_date = current_date
                print(f"[poll] New day {current_date} — daily state reset")

            snap = fetch_today_usage()
            if snap:
                usage.update(snap)
                total     = snap.get("total_cost", 0.0)
                total_tok = sum(p.get("total_tokens", 0) for p in snap.get("projects", {}).values())
                print(f"[poll] {current_date}  tokens={_fmt_tokens(total_tok)}  cost=${total:.4f}")

                check_milestones(snap, usage, subs, names)

                # $5 limit alert — fires once
                if total >= DAILY_LIMIT and not usage.get_alert_sent():
                    usage.mark_alert_sent()
                    if names:
                        _broadcast_named(lambda n, t=total: fmt_limit_alert(t, n), subs, names)
                    else:
                        _send_all(fmt_limit_alert(total), subs)

                # Post-limit: escalating drama every $2 above $5
                if total > DAILY_LIMIT:
                    intervals_above   = int((total - DAILY_LIMIT) / SPEND_ALERT_INTERVAL)
                    intervals_notified = usage.get_spend_intervals_notified()
                    if intervals_above > intervals_notified:
                        usage.set_spend_intervals_notified(intervals_above)
                        for lvl in range(intervals_notified + 1, intervals_above + 1):
                            if names:
                                _broadcast_named(lambda n, t=total, l=lvl: fmt_post_limit_alert(t, l, n), subs, names)
                            else:
                                _send_all(fmt_post_limit_alert(total, lvl), subs)
            else:
                print("[poll] Could not fetch usage — will retry next interval")

        except Exception as e:
            print(f"[poll loop error] {e}")

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
