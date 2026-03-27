"""
Telegram GPU VRAM Sentinel Bot

Monitors VRAM usage across all detected GPUs and alerts when availability drops
below the configured threshold. Enables on-command VRAM bloating to pre-reserve
GPU memory on shared servers — preventing others from claiming it with small workloads.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BOT IDENTITY: VRAM Garrison Commander
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Bound to Bach the Monarch. Claims GPU territory on command.
Speaks with a soldier's discipline. Reports without sentiment.
No humor. No filler. The GPU is the battlefield.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import ctypes
import json
import os
import platform
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import dotenv
import requests

dotenv.load_dotenv()

BOT_TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID        = os.environ.get("TELEGRAM_CHAT_ID", "")
THREAD_ID      = int(os.environ["TELEGRAM_THREAD_ID"]) if os.environ.get("TELEGRAM_THREAD_ID") else None
POLL_INTERVAL  = int(os.environ.get("GPU_POLL_INTERVAL_SECS", "60"))
HIGH_THRESHOLD = int(os.environ.get("VRAM_HIGH_THRESHOLD_PCT", "50"))  # alert when free VRAM >= this %
HIGH_COOLDOWN  = 600   # minimum seconds between high-VRAM alerts per GPU

BOT_DATA_DIR    = Path(__file__).parent / "bot_data"
SUBS_PATH       = BOT_DATA_DIR / "subscribers.json"

REQUEST_TIMEOUT = 15
POLL_TIMEOUT    = 30

BLOAT_LEVELS          = [20, 50, 70, 90]   # % occupation targets
KILLER_THRESHOLDS     = [10, 50, 70]       # free VRAM % triggers for killer mode
KILLER_REMINDER_SECS  = 60                 # reminder interval while killer mode is armed


# ── CUDA Driver API ─────────────────────────────────────────────────────────

def _load_cuda_lib():
    """Load the CUDA Driver API shared library (platform-agnostic)."""
    if platform.system() == "Windows":
        candidates = ["nvcuda.dll", r"C:\Windows\System32\nvcuda.dll"]
    else:
        candidates = ["libcuda.so.1", "libcuda.so"]
    for path in candidates:
        try:
            return ctypes.CDLL(path)
        except OSError:
            continue
    return None


_cuda_lib   = _load_cuda_lib()
_cuda_ready = False


def _cuda_init() -> bool:
    global _cuda_ready
    if _cuda_ready:
        return True
    if _cuda_lib is None:
        return False
    ret = _cuda_lib.cuInit(ctypes.c_uint(0))
    _cuda_ready = (ret == 0)
    return _cuda_ready


# ── Bloat session management ─────────────────────────────────────────────────

@dataclass
class KillerSession:
    gpu_idx:      int
    threshold_pct: int   # auto-bloat fires when free VRAM drops below this %
    armed_at:     float = field(default_factory=time.time)


@dataclass
class BloatSession:
    gpu_idx:      int
    target_pct:   int
    allocated_mb: int
    ctx:          ctypes.c_void_p
    ptr:          ctypes.c_uint64
    started_at:   float = field(default_factory=time.time)


_sessions:      dict[int, BloatSession] = {}
_sessions_lock: threading.Lock          = threading.Lock()

_killer_sessions:      dict[int, KillerSession] = {}
_killer_lock:          threading.Lock            = threading.Lock()


def _alloc_cuda_vram(gpu_idx: int, target_mb: int) -> tuple[
        Optional[ctypes.c_void_p], Optional[ctypes.c_uint64], int]:
    """
    Create a CUDA context on gpu_idx and allocate up to target_mb of VRAM.
    Steps down in 256MB increments on OOM to find the largest viable allocation.
    Returns (ctx, ptr, actual_mb). Returns (None, None, 0) on total failure.
    The context is popped off the current thread stack after allocation —
    memory remains resident until the context is destroyed.
    """
    if not _cuda_init():
        return None, None, 0

    device = ctypes.c_int(0)
    if _cuda_lib.cuDeviceGet(ctypes.byref(device), ctypes.c_int(gpu_idx)) != 0:
        return None, None, 0

    ctx = ctypes.c_void_p(0)
    if _cuda_lib.cuCtxCreate_v2(ctypes.byref(ctx), ctypes.c_uint(0), device) != 0:
        return None, None, 0

    mb = target_mb
    while mb >= 128:
        ptr  = ctypes.c_uint64(0)
        ret  = _cuda_lib.cuMemAlloc_v2(ctypes.byref(ptr), ctypes.c_size_t(mb * 1024 * 1024))
        if ret == 0:
            # Force physical VRAM residency — critical under WDDM where pages are
            # lazily committed. cuMemsetD8 writes every byte, ensuring the OS
            # actually reserves physical VRAM rather than just virtual addresses.
            _cuda_lib.cuMemsetD8_v2(ptr, ctypes.c_uint8(0), ctypes.c_size_t(mb * 1024 * 1024))
            popped = ctypes.c_void_p(0)
            _cuda_lib.cuCtxPopCurrent_v2(ctypes.byref(popped))
            return ctx, ptr, mb
        mb -= 256

    _cuda_lib.cuCtxDestroy_v2(ctx)
    return None, None, 0


def _free_session(session: BloatSession) -> None:
    """Free CUDA memory and destroy the context for a session."""
    if _cuda_lib is None:
        return
    try:
        _cuda_lib.cuCtxPushCurrent_v2(session.ctx)
        _cuda_lib.cuMemFree_v2(session.ptr)
        popped = ctypes.c_void_p(0)
        _cuda_lib.cuCtxPopCurrent_v2(ctypes.byref(popped))
        _cuda_lib.cuCtxDestroy_v2(session.ctx)
    except Exception as e:
        print(f"[cuda free error] GPU {session.gpu_idx}: {e}")


def bloat_gpu(gpu_idx: int, target_pct: int) -> tuple[bool, int, str]:
    """
    Allocate VRAM on gpu_idx to reach target_pct% of total VRAM.
    Safety margin: always leaves 300MB free to avoid hard OOM.
    Returns (success, allocated_mb, message).
    """
    with _sessions_lock:
        if gpu_idx in _sessions:
            s = _sessions[gpu_idx]
            return (False, 0,
                    f"GPU {gpu_idx} already has active occupation at {s.target_pct}% "
                    f"({s.allocated_mb:,}MB held). Release it first.")

    gpus = get_gpu_stats()
    gpu  = next((g for g in gpus if g["index"] == gpu_idx), None)
    if gpu is None:
        return False, 0, f"GPU {gpu_idx} not found."

    total_mb    = gpu["total_mb"]
    used_mb     = gpu["used_mb"]
    target_mb   = int(total_mb * target_pct / 100)
    to_alloc_mb = target_mb - used_mb - 300   # 300MB safety margin

    if to_alloc_mb < 128:
        pct_now = int(used_mb / total_mb * 100)
        return (False, 0,
                f"GPU {gpu_idx} already at {used_mb:,}MB/{total_mb:,}MB ({pct_now}%). "
                f"Target {target_pct}% leaves no room to bloat.")

    ctx, ptr, allocated_mb = _alloc_cuda_vram(gpu_idx, to_alloc_mb)
    if ctx is None or allocated_mb == 0:
        return (False, 0,
                f"CUDA allocation failed on GPU {gpu_idx}. "
                f"Insufficient VRAM or driver error.")

    session = BloatSession(
        gpu_idx=gpu_idx,
        target_pct=target_pct,
        allocated_mb=allocated_mb,
        ctx=ctx,
        ptr=ptr,
    )
    with _sessions_lock:
        _sessions[gpu_idx] = session

    achieved_pct = int((used_mb + allocated_mb) / total_mb * 100)
    return (True, allocated_mb,
            f"GPU {gpu_idx}: {allocated_mb:,}MB allocated — ~{achieved_pct}% occupied.")


def release_gpu(gpu_idx: int) -> tuple[bool, int, str]:
    """Release bloat on a single GPU."""
    with _sessions_lock:
        session = _sessions.pop(gpu_idx, None)
    if session is None:
        return False, 0, f"GPU {gpu_idx} has no active occupation."
    _free_session(session)
    return True, session.allocated_mb, f"GPU {gpu_idx}: {session.allocated_mb:,}MB freed."


def release_all() -> list[tuple[int, int]]:
    """Release all active bloat. Returns [(gpu_idx, freed_mb), ...]."""
    with _sessions_lock:
        sessions = dict(_sessions)
        _sessions.clear()
    results = []
    for idx, session in sessions.items():
        _free_session(session)
        results.append((idx, session.allocated_mb))
    return results


# ── Killer mode management ────────────────────────────────────────────────────

def killer_arm(gpu_idx: int, threshold_pct: int) -> tuple[bool, str]:
    """Arm killer mode on a GPU. Returns (success, message)."""
    with _killer_lock:
        if gpu_idx in _killer_sessions:
            existing = _killer_sessions[gpu_idx]
            _killer_sessions[gpu_idx] = KillerSession(gpu_idx=gpu_idx, threshold_pct=threshold_pct)
            return True, (
                f"GPU {gpu_idx}: killer mode re-armed at {threshold_pct}% threshold "
                f"(was {existing.threshold_pct}%)."
            )
        _killer_sessions[gpu_idx] = KillerSession(gpu_idx=gpu_idx, threshold_pct=threshold_pct)
    return True, f"GPU {gpu_idx}: killer mode armed — auto-bloat fires when free VRAM < {threshold_pct}%."


def killer_disarm(gpu_idx: int) -> tuple[bool, str]:
    """Disarm killer mode on a single GPU."""
    with _killer_lock:
        session = _killer_sessions.pop(gpu_idx, None)
    if session is None:
        return False, f"GPU {gpu_idx} has no active killer mode."
    return True, f"GPU {gpu_idx}: killer mode disarmed."


def killer_disarm_all() -> list[int]:
    """Disarm killer mode on all GPUs. Returns list of disarmed gpu indices."""
    with _killer_lock:
        indices = list(_killer_sessions.keys())
        _killer_sessions.clear()
    return indices


# ── GPU stats ────────────────────────────────────────────────────────────────

def get_gpu_stats() -> list[dict]:
    """
    Query nvidia-smi for all GPU stats.
    Returns list of dicts with index, name, memory, utilization, temperature.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,memory.used,memory.free,"
                "utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=10,
        )
        gpus = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 7:
                continue
            total = int(parts[2])
            used  = int(parts[3])
            free  = int(parts[4])
            gpus.append({
                "index":    int(parts[0]),
                "name":     parts[1],
                "total_mb": total,
                "used_mb":  used,
                "free_mb":  free,
                "util_pct": int(parts[5]),
                "temp_c":   int(parts[6]),
                "used_pct": int(used / total * 100) if total else 0,
                "free_pct": int(free / total * 100) if total else 0,
            })
        return gpus
    except Exception as e:
        print(f"[nvidia-smi error] {e}")
        return []


# ── Subscriber store ──────────────────────────────────────────────────────────

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


# ── Telegram I/O ─────────────────────────────────────────────────────────────

def _send(text: str, chat_id: str = None, thread_id: int = None,
          reply_markup: dict = None) -> Optional[int]:
    """Send a message. Returns message_id on success."""
    url    = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    target = chat_id or CHAT_ID
    effective_thread = (
        thread_id if thread_id is not None
        else (THREAD_ID if target == CHAT_ID else None)
    )
    payload = {"chat_id": target, "text": text, "parse_mode": "HTML"}
    if effective_thread:
        payload["message_thread_id"] = effective_thread
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
        if r.ok:
            return r.json().get("result", {}).get("message_id")
        print(f"[send {r.status_code}] {r.text[:300]}")
    except Exception as e:
        print(f"[send error] {e}")
    return None


def _send_all(text: str, subs: SubscriberStore) -> None:
    for cid in subs.all():
        _send(text, cid)


def _edit_message(chat_id: str, message_id: int, text: str,
                  reply_markup: dict = None) -> None:
    """Edit an existing message (used to update inline keyboard state)."""
    url     = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id":    chat_id,
        "message_id": message_id,
        "text":       text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            print(f"[edit {r.status_code}] {r.text[:200]}")
    except Exception as e:
        print(f"[edit error] {e}")


def _answer_callback(callback_id: str, text: str = "") -> None:
    """Acknowledge a callback query (clears the loading indicator)."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    try:
        requests.post(
            url,
            data={"callback_query_id": callback_id, "text": text},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:
        print(f"[answer_callback error] {e}")


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


# ── Inline keyboard builders ──────────────────────────────────────────────────

def _kb(rows: list[list[tuple[str, str]]]) -> dict:
    """Build an InlineKeyboardMarkup from (label, callback_data) rows."""
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in rows
        ]
    }


def kb_bloat_gpu_select(gpus: list[dict]) -> dict:
    """GPU selection keyboard (multi-GPU case)."""
    rows = []
    for g in gpus:
        label = f"GPU {g['index']}: {g['name']}  ({g['used_pct']}% used)"
        rows.append([(label, f"G:{g['index']}")])
    rows.append([("All GPUs", "G:all")])
    rows.append([("❌ Cancel", "X")])
    return _kb(rows)


def kb_bloat_pct_select(gpu_spec: str) -> dict:
    """Percentage selection keyboard."""
    row1 = [(f"20%", f"P:20:{gpu_spec}"), (f"50%", f"P:50:{gpu_spec}")]
    row2 = [(f"70%", f"P:70:{gpu_spec}"), (f"90%", f"P:90:{gpu_spec}")]
    return _kb([row1, row2, [("❌ Cancel", "X")]])


def kb_release_select(gpus: list[dict]) -> dict:
    """Release selection keyboard based on active sessions."""
    with _sessions_lock:
        active = dict(_sessions)
    if not active:
        return _kb([[("No active bloat", "X")]])
    rows = []
    for idx, s in sorted(active.items()):
        g     = next((g for g in gpus if g["index"] == idx), None)
        name  = g["name"] if g else f"GPU {idx}"
        label = f"Release GPU {idx} — {name} ({s.allocated_mb:,}MB)"
        rows.append([(label, f"R:{idx}")])
    if len(active) > 1:
        rows.append([("Release All GPUs", "R:all")])
    rows.append([("❌ Cancel", "X")])
    return _kb(rows)


def kb_killer_gpu_select(gpus: list[dict]) -> dict:
    """GPU selection keyboard for killer mode."""
    rows = []
    for g in gpus:
        label = f"GPU {g['index']}: {g['name']}  ({g['free_pct']}% free)"
        rows.append([(label, f"K:{g['index']}")])
    rows.append([("All GPUs", "K:all")])
    rows.append([("❌ Cancel", "X")])
    return _kb(rows)


def kb_killer_threshold_select(gpu_spec: str) -> dict:
    """Threshold selection keyboard for killer mode."""
    row = [(f"< {t}% free", f"KT:{t}:{gpu_spec}") for t in KILLER_THRESHOLDS]
    return _kb([row, [("❌ Cancel", "X")]])


def kb_unkill_select(active: dict[int, "KillerSession"]) -> dict:
    """Disarm selection keyboard based on active killer sessions."""
    if not active:
        return _kb([[("No killer mode active", "X")]])
    rows = []
    for idx, s in sorted(active.items()):
        label = f"Disarm GPU {idx}  (threshold: < {s.threshold_pct}% free)"
        rows.append([(label, f"UK:{idx}")])
    if len(active) > 1:
        rows.append([("Disarm All GPUs", "UK:all")])
    rows.append([("❌ Cancel", "X")])
    return _kb(rows)


# ── Formatters ────────────────────────────────────────────────────────────────

def _bar(pct: int, width: int = 10) -> str:
    filled = max(0, min(width, int(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def fmt_gpu_status() -> str:
    gpus = get_gpu_stats()
    if not gpus:
        return (
            "⚠️ <b>No GPU data available.</b>\n"
            "nvidia-smi failed to respond, Monarch Bach."
        )
    with _sessions_lock:
        active_sessions = dict(_sessions)

    ts    = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines = [f"🖥️ <b>GPU Status — {ts}</b>\n"]

    for g in gpus:
        bloat = active_sessions.get(g["index"])
        if bloat:
            bloat_tag = (
                f"\n  🔒 <i>Occupied: {bloat.target_pct}% target "
                f"({bloat.allocated_mb:,}MB garrison held)</i>"
            )
        else:
            bloat_tag = ""

        lines.append(
            f"<b>GPU {g['index']}: {g['name']}</b>{bloat_tag}\n"
            f"  VRAM [{_bar(g['used_pct'])}] "
            f"{g['used_mb']:,} / {g['total_mb']:,} MB  "
            f"({g['used_pct']}% used · {g['free_pct']}% free)\n"
            f"  Compute: {g['util_pct']}%   Temp: {g['temp_c']}°C"
        )

    lines.append("\n<i>Garrison report complete, Monarch Bach.</i>")
    return "\n".join(lines)


def fmt_high_vram_alert(gpu: dict, bot_username: Optional[str] = None) -> str:
    mention = f"@{bot_username}" if bot_username else "@bot"
    return (
        f"🟢 <b>VRAM Available — GPU {gpu['index']}: {gpu['name']}</b>\n\n"
        f"Free VRAM: <b>{gpu['free_pct']}%</b> "
        f"({gpu['free_mb']:,}MB of {gpu['total_mb']:,}MB).\n"
        f"Compute util: {gpu['util_pct']}%  ·  Temp: {gpu['temp_c']}°C\n\n"
        f"<i>Bloating strike window open, Monarch Bach.</i>\n"
        f"Use <code>{mention} bloat</code> to claim the territory now."
    )


def fmt_bloat_results(results: list[tuple[bool, int, str]]) -> str:
    all_ok = all(ok for ok, _, _ in results)
    header = "🔒 <b>VRAM Occupation — Successful</b>" if all_ok else "⚠️ <b>VRAM Occupation — Partial/Failed</b>"
    lines  = [header, ""]
    for ok, mb, msg in results:
        lines.append(f"{'✅' if ok else '❌'} {msg}")
    if all_ok:
        lines.append("\n<i>Territory claimed and held, Monarch Bach.</i>")
    else:
        lines.append("\n<i>Occupation incomplete. Verify VRAM availability.</i>")
    return "\n".join(lines)


def fmt_release_results(results: list[tuple[int, int]]) -> str:
    if not results:
        return "No active bloat was found to release."
    lines = ["🔓 <b>VRAM Released</b>\n"]
    for idx, mb in results:
        lines.append(f"✅ GPU {idx}: {mb:,}MB freed")
    lines.append("\n<i>Garrison withdrawn. Territory relinquished, Monarch Bach.</i>")
    return "\n".join(lines)


def fmt_killer_armed(gpu_indices: list[int], threshold_pct: int) -> str:
    gpu_list = ", ".join(f"GPU {i}" for i in sorted(gpu_indices))
    return (
        f"☠️ <b>KILLER MODE ARMED — {gpu_list}</b>\n\n"
        f"Trigger: free VRAM drops below <b>{threshold_pct}%</b>\n"
        f"Action:  immediate full VRAM seizure (100% occupation)\n\n"
        f"<i>Autopilot engaged. I will strike without waiting for orders, Monarch Bach.\n"
        f"Reminders every {KILLER_REMINDER_SECS}s until disarmed.</i>"
    )


def fmt_killer_reminder(gpus: list[dict]) -> str:
    with _killer_lock:
        active = dict(_killer_sessions)
    with _sessions_lock:
        bloated = dict(_sessions)

    if not active:
        return ""

    lines = ["☠️ <b>KILLER MODE ACTIVE — Reminder</b>\n"]
    for idx, ks in sorted(active.items()):
        g = next((g for g in gpus if g["index"] == idx), None)
        if g:
            state = "🔒 BLOATED" if idx in bloated else f"{g['free_pct']}% free"
            lines.append(
                f"GPU {idx}: threshold &lt; {ks.threshold_pct}% free  |  now: {state}"
            )
        else:
            lines.append(f"GPU {idx}: threshold &lt; {ks.threshold_pct}% free  |  no data")
    lines.append("\n<i>Killer mode still armed. Use <code>unkill</code> to stand down.</i>")
    return "\n".join(lines)


def fmt_killer_strike(gpu: dict, allocated_mb: int) -> str:
    return (
        f"☠️ <b>KILLER MODE STRUCK — GPU {gpu['index']}: {gpu['name']}</b>\n\n"
        f"Free VRAM fell to <b>{gpu['free_pct']}%</b> — autopilot triggered.\n"
        f"Allocated <b>{allocated_mb:,}MB</b> — VRAM maxed out.\n\n"
        f"<i>Territory seized automatically, Monarch Bach.</i>"
    )


def fmt_killer_disarmed(indices: list[int]) -> str:
    if not indices:
        return "No active killer mode to disarm."
    gpu_list = ", ".join(f"GPU {i}" for i in sorted(indices))
    return (
        f"🔓 <b>Killer Mode Disarmed — {gpu_list}</b>\n\n"
        f"<i>Autopilot stood down. Manual control restored, Monarch Bach.</i>"
    )


# ── Command handlers ──────────────────────────────────────────────────────────

def cmd_status() -> str:
    return fmt_gpu_status()


def cmd_bloat(chat_id: str, thread_id: Optional[int]) -> None:
    gpus = get_gpu_stats()
    if not gpus:
        _send("⚠️ No GPUs detected. nvidia-smi unavailable.", chat_id, thread_id)
        return

    if len(gpus) == 1:
        # Single GPU — skip GPU selection, go straight to percentage
        g    = gpus[0]
        text = (
            f"🔒 <b>VRAM Occupation Order — GPU {g['index']}: {g['name']}</b>\n\n"
            f"Current:  {g['used_mb']:,}MB / {g['total_mb']:,}MB  ({g['used_pct']}% used)\n"
            f"Available: {g['free_mb']:,}MB  ({g['free_pct']}% free)\n\n"
            f"Choose occupation level:"
        )
        _send(text, chat_id, thread_id, reply_markup=kb_bloat_pct_select("0"))
    else:
        text = (
            f"🔒 <b>Territorial Claim — Select Target GPU(s)</b>\n\n"
            f"Available GPUs:"
        )
        _send(text, chat_id, thread_id, reply_markup=kb_bloat_gpu_select(gpus))


def cmd_release(chat_id: str, thread_id: Optional[int]) -> None:
    with _sessions_lock:
        active = dict(_sessions)
    if not active:
        _send(
            "🔓 <b>No Active Occupation</b>\n\n"
            "No VRAM garrison is currently deployed, Monarch Bach.",
            chat_id, thread_id,
        )
        return
    gpus = get_gpu_stats()
    _send(
        "🔓 <b>Release VRAM Garrison</b>\n\nSelect what to release:",
        chat_id, thread_id,
        reply_markup=kb_release_select(gpus),
    )


def cmd_killer(chat_id: str, thread_id: Optional[int]) -> None:
    gpus = get_gpu_stats()
    if not gpus:
        _send("⚠️ No GPUs detected. nvidia-smi unavailable.", chat_id, thread_id)
        return

    if len(gpus) == 1:
        g    = gpus[0]
        text = (
            f"☠️ <b>Killer Mode — GPU {g['index']}: {g['name']}</b>\n\n"
            f"Current: {g['used_mb']:,}MB / {g['total_mb']:,}MB  ({g['free_pct']}% free)\n\n"
            f"Set trigger threshold — auto-bloat fires when free VRAM drops below:"
        )
        _send(text, chat_id, thread_id, reply_markup=kb_killer_threshold_select("0"))
    else:
        _send(
            "☠️ <b>Killer Mode — Select Target GPU(s)</b>\n\nArm killer mode on:",
            chat_id, thread_id,
            reply_markup=kb_killer_gpu_select(gpus),
        )


def cmd_unkill(chat_id: str, thread_id: Optional[int]) -> None:
    with _killer_lock:
        active = dict(_killer_sessions)
    if not active:
        _send(
            "🔓 <b>No Active Killer Mode</b>\n\n"
            "Autopilot is not armed on any GPU, Monarch Bach.",
            chat_id, thread_id,
        )
        return
    _send(
        "☠️ <b>Disarm Killer Mode</b>\n\nSelect what to stand down:",
        chat_id, thread_id,
        reply_markup=kb_unkill_select(active),
    )


def cmd_arise(chat_id: str, subs: SubscriberStore) -> str:
    added = subs.add(chat_id)
    if added:
        return (
            "⚔️ <b>Garrison activated.</b>\n\n"
            "Bach the Monarch — the VRAM Sentinel stands guard.\n\n"
            "Every GPU on this server is under my surveillance. "
            f"When free VRAM rises above {HIGH_THRESHOLD}% on any unoccupied device, "
            "I will report the strike window immediately. When you order occupation, "
            "I execute without hesitation and hold the line.\n\n"
            "No trivial model will claim your compute unchallenged.\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>VRAM Sentinel, standing by.</b>"
        )
    return "This channel already receives garrison reports, Monarch Bach."


def cmd_dismiss(chat_id: str, subs: SubscriberStore) -> str:
    if chat_id == subs.primary:
        return (
            "The primary command channel cannot be removed, Monarch Bach.\n"
            "<i>The sentinel remains at post.</i>"
        )
    removed = subs.remove(chat_id)
    if removed:
        return "This channel has been removed from garrison reports.\n<i>Order carried out.</i>"
    return "This channel was not receiving reports, Monarch Bach."


def cmd_help(bot_username: Optional[str]) -> str:
    m = f"@{bot_username}" if bot_username else "@bot"
    return (
        f"📋 <b>VRAM Sentinel — Command Registry</b>\n"
        f"Trigger: <code>{m} &lt;command&gt;</code>\n\n"
        f"<b>Monitoring</b>\n"
        f"<code>{m} status</code>    — VRAM usage across all GPUs\n\n"
        f"<b>VRAM Control</b>\n"
        f"<code>{m} bloat</code>     — Pre-reserve VRAM (interactive buttons)\n"
        f"<code>{m} release</code>   — Release held VRAM (interactive buttons)\n\n"
        f"<b>Killer Mode (Autopilot)</b>\n"
        f"<code>{m} killer</code>    — Arm autopilot: full VRAM seizure when threshold crossed\n"
        f"<code>{m} unkill</code>    — Disarm killer mode (interactive buttons)\n"
        f"Thresholds: {', '.join(f'{t}%' for t in KILLER_THRESHOLDS)} free VRAM\n"
        f"Reminders fire every {KILLER_REMINDER_SECS}s while armed.\n\n"
        f"<b>Notifications</b>\n"
        f"<code>{m} arise</code>     — Subscribe this chat to VRAM alerts\n"
        f"<code>{m} dismiss</code>   — Unsubscribe this chat\n\n"
        f"<code>{m} help</code>      — This registry\n\n"
        f"Auto-alert fires when free VRAM rises above <b>{HIGH_THRESHOLD}%</b> on any unoccupied GPU.\n"
        f"Alerts suppressed for actively-bloated GPUs.\n\n"
        f"<i>Monarch Bach, your garrison awaits your command.</i>"
    )


# ── Command dispatch ──────────────────────────────────────────────────────────

def _match_prefix(text: str, bot_username: Optional[str]) -> Optional[str]:
    if not bot_username:
        return None
    prefix = f"@{bot_username.lower()}"
    lower  = text.strip().lower()
    if lower.startswith(prefix):
        return text.strip()[len(prefix):].strip()
    return None


def dispatch_text(text: str, subs: SubscriberStore, bot_username: Optional[str],
                  chat_id: str, thread_id: Optional[int]) -> None:
    rest = _match_prefix(text, bot_username)
    if rest is None:
        return

    parts = rest.split()
    cmd   = parts[0].lower() if parts else "help"

    if cmd != "arise" and chat_id not in subs.all():
        return  # Not subscribed — ignore all commands except arise

    if cmd == "status":
        _send(cmd_status(), chat_id, thread_id)
    elif cmd == "bloat":
        cmd_bloat(chat_id, thread_id)
    elif cmd == "release":
        cmd_release(chat_id, thread_id)
    elif cmd == "killer":
        cmd_killer(chat_id, thread_id)
    elif cmd == "unkill":
        cmd_unkill(chat_id, thread_id)
    elif cmd == "arise":
        _send(cmd_arise(chat_id, subs), chat_id, thread_id)
    elif cmd == "dismiss":
        _send(cmd_dismiss(chat_id, subs), chat_id, thread_id)
    elif cmd == "help":
        _send(cmd_help(bot_username), chat_id, thread_id)
    else:
        name = f"@{bot_username}" if bot_username else "@bot"
        _send(
            f"Unknown command: <code>{cmd}</code>. "
            f"Use <code>{name} help</code> for the registry.",
            chat_id, thread_id,
        )


# ── Callback query handler ────────────────────────────────────────────────────

def _handle_callback(cbq: dict, subs: SubscriberStore) -> None:
    """
    Callback data protocol:
      G:{gpu_spec}          — GPU selection step (gpu_spec: "0", "1", ..., "all")
      P:{pct}:{gpu_spec}    — Bloat execution (pct: 20/50/70/90)
      R:{gpu_spec}          — Release execution
      K:{gpu_spec}          — Killer mode GPU selection, show threshold picker
      KT:{threshold}:{gpu_spec} — Arm killer mode at threshold %
      UK:{gpu_spec}         — Disarm killer mode
      X                     — Cancel
    """
    cbq_id     = cbq["id"]
    data       = cbq.get("data", "")
    msg        = cbq.get("message", {})
    chat_id    = str(msg.get("chat", {}).get("id", ""))
    message_id = msg.get("message_id")

    if chat_id not in subs.all():
        _answer_callback(cbq_id, "Not authorized.")
        return

    # ── Cancel ────────────────────────────────────────────────────────────────
    if data == "X":
        _answer_callback(cbq_id, "Cancelled.")
        _edit_message(chat_id, message_id, "🚫 <b>Operation cancelled.</b>")
        return

    # ── GPU selection: G:{gpu_spec} ───────────────────────────────────────────
    if data.startswith("G:"):
        gpu_spec = data[2:]
        gpus     = get_gpu_stats()
        if gpu_spec == "all":
            label = "All GPUs"
        else:
            idx   = int(gpu_spec)
            g     = next((g for g in gpus if g["index"] == idx), None)
            label = f"GPU {idx}: {g['name']}" if g else f"GPU {idx}"

        text = (
            f"🔒 <b>VRAM Occupation — Target: {label}</b>\n\n"
            f"Choose occupation level:"
        )
        _answer_callback(cbq_id)
        _edit_message(chat_id, message_id, text, reply_markup=kb_bloat_pct_select(gpu_spec))
        return

    # ── Percentage / bloat: P:{pct}:{gpu_spec} ───────────────────────────────
    if data.startswith("P:"):
        _, pct_str, gpu_spec = data.split(":", 2)
        pct = int(pct_str)

        _answer_callback(cbq_id, f"Executing {pct}% occupation...")
        _edit_message(
            chat_id, message_id,
            f"🔒 <b>VRAM Occupation — {pct}% — In progress...</b>\n<i>Allocating...</i>",
        )

        gpus    = get_gpu_stats()
        targets = [g["index"] for g in gpus] if gpu_spec == "all" else [int(gpu_spec)]
        results = []
        for idx in targets:
            ok, mb, msg_txt = bloat_gpu(idx, pct)
            results.append((ok, mb, msg_txt))

        _edit_message(chat_id, message_id, fmt_bloat_results(results))
        return

    # ── Release: R:{gpu_spec} ─────────────────────────────────────────────────
    if data.startswith("R:"):
        gpu_spec = data[2:]
        _answer_callback(cbq_id, "Releasing...")

        if gpu_spec == "all":
            freed       = release_all()
            result_text = fmt_release_results(freed)
        else:
            idx  = int(gpu_spec)
            ok, mb, msg_txt = release_gpu(idx)
            result_text = (
                fmt_release_results([(idx, mb)])
                if ok else f"⚠️ {msg_txt}"
            )

        _edit_message(chat_id, message_id, result_text)
        return

    # ── Killer GPU selection: K:{gpu_spec} ───────────────────────────────────
    if data.startswith("K:"):
        gpu_spec = data[2:]
        _answer_callback(cbq_id)
        _edit_message(
            chat_id, message_id,
            f"☠️ <b>Killer Mode — Target: {'All GPUs' if gpu_spec == 'all' else f'GPU {gpu_spec}'}</b>\n\n"
            f"Set trigger threshold — auto-bloat fires when free VRAM drops below:",
            reply_markup=kb_killer_threshold_select(gpu_spec),
        )
        return

    # ── Arm killer mode: KT:{threshold}:{gpu_spec} ───────────────────────────
    if data.startswith("KT:"):
        _, threshold_str, gpu_spec = data.split(":", 2)
        threshold = int(threshold_str)

        _answer_callback(cbq_id, f"Arming killer mode at < {threshold}% free...")
        _edit_message(
            chat_id, message_id,
            f"☠️ <b>Killer Mode — Arming...</b>\n<i>Setting autopilot...</i>",
        )

        gpus    = get_gpu_stats()
        targets = [g["index"] for g in gpus] if gpu_spec == "all" else [int(gpu_spec)]
        for idx in targets:
            killer_arm(idx, threshold)

        _edit_message(chat_id, message_id, fmt_killer_armed(targets, threshold))
        return

    # ── Disarm killer mode: UK:{gpu_spec} ────────────────────────────────────
    if data.startswith("UK:"):
        gpu_spec = data[3:]
        _answer_callback(cbq_id, "Disarming...")

        if gpu_spec == "all":
            indices = killer_disarm_all()
        else:
            idx = int(gpu_spec)
            ok, _ = killer_disarm(idx)
            indices = [idx] if ok else []

        _edit_message(chat_id, message_id, fmt_killer_disarmed(indices))
        return

    _answer_callback(cbq_id)


# ── Telegram poll thread ──────────────────────────────────────────────────────

def telegram_poll_loop(subs: SubscriberStore, bot_username: Optional[str]) -> None:
    offset = 0
    while True:
        updates = _get_updates(offset)
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                # Callback query (inline button press)
                cbq = upd.get("callback_query")
                if cbq:
                    _handle_callback(cbq, subs)
                    continue

                # Text message
                msg = (
                    upd.get("message") or upd.get("edited_message")
                    or upd.get("channel_post") or upd.get("edited_channel_post")
                )
                if not msg:
                    continue
                chat_id   = str(msg.get("chat", {}).get("id", ""))
                thread_id = msg.get("message_thread_id")
                text      = msg.get("text", "") or msg.get("caption", "")
                if not text:
                    continue
                print(f"[update] chat={chat_id} thread={thread_id} text={text[:60]!r}")
                dispatch_text(text, subs, bot_username, chat_id, thread_id)
            except Exception as e:
                print(f"[telegram handler error] {e}")


# ── GPU monitor thread ────────────────────────────────────────────────────────

def gpu_monitor_loop(subs: SubscriberStore, bot_username: Optional[str] = None) -> None:
    """
    Poll GPU stats every POLL_INTERVAL seconds.
    Alert if free VRAM >= HIGH_THRESHOLD% on any GPU that is not actively bloated.
    Alert cooldown: HIGH_COOLDOWN seconds per GPU to avoid spam.
    """
    last_alert: dict[int, float] = {}

    while True:
        try:
            gpus = get_gpu_stats()
            with _sessions_lock:
                bloated_gpus = set(_sessions.keys())

            with _killer_lock:
                killer_gpus = dict(_killer_sessions)

            for g in gpus:
                idx = g["index"]

                # ── Killer mode: auto-bloat to 100% when threshold crossed ──
                if idx in killer_gpus and idx not in bloated_gpus:
                    ks = killer_gpus[idx]
                    if g["free_pct"] < ks.threshold_pct:
                        print(f"[killer] GPU {idx}: {g['free_pct']}% free < {ks.threshold_pct}% — striking")
                        ok, allocated_mb, _ = bloat_gpu(idx, 100)
                        if ok:
                            _send_all(fmt_killer_strike(g, allocated_mb), subs)
                        continue  # skip high-VRAM alert for this GPU this cycle

                # ── High VRAM alert (bloat opportunity) ───────────────────────
                if idx in bloated_gpus:
                    continue
                if g["free_pct"] >= HIGH_THRESHOLD:
                    now     = time.time()
                    last_ts = last_alert.get(idx, 0)
                    if now - last_ts > HIGH_COOLDOWN:
                        last_alert[idx] = now
                        _send_all(fmt_high_vram_alert(g, bot_username), subs)
                        print(f"[monitor] High VRAM alert — GPU {idx}: {g['free_pct']}% free")

            summary = "  ".join(
                f"GPU{g['index']}:{g['used_mb']}MB/{g['total_mb']}MB({g['used_pct']}%)"
                for g in gpus
            )
            print(f"[monitor] {summary}")

        except Exception as e:
            print(f"[monitor error] {e}")

        time.sleep(POLL_INTERVAL)


# ── Killer reminder thread ────────────────────────────────────────────────────

def killer_reminder_loop(subs: SubscriberStore) -> None:
    """Send a reminder every KILLER_REMINDER_SECS while any killer session is armed."""
    while True:
        time.sleep(KILLER_REMINDER_SECS)
        try:
            with _killer_lock:
                active = dict(_killer_sessions)
            if not active:
                continue
            gpus = get_gpu_stats()
            msg  = fmt_killer_reminder(gpus)
            if msg:
                _send_all(msg, subs)
                print(f"[killer] Reminder sent — {len(active)} GPU(s) armed")
        except Exception as e:
            print(f"[killer reminder error] {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Missing required environment variables.\n"
            "Ensure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set in .env"
        )

    BOT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    subs = SubscriberStore(SUBS_PATH, CHAT_ID)

    bot_username = _fetch_bot_username()
    if bot_username:
        print(f"[bot] @{bot_username} ready")
    else:
        print("[bot] WARNING: Could not resolve username — @mention commands will not work")

    cuda_ok = _cuda_init()
    print(f"[bot] CUDA driver: {'available ✓' if cuda_ok else 'NOT available — bloat disabled'}")

    gpus = get_gpu_stats()
    if gpus:
        for g in gpus:
            print(f"[bot] GPU {g['index']}: {g['name']}  "
                  f"{g['used_mb']}MB/{g['total_mb']}MB ({g['used_pct']}%)")
    else:
        print("[bot] WARNING: No GPUs detected")

    threading.Thread(
        target=telegram_poll_loop, args=(subs, bot_username), daemon=True
    ).start()
    threading.Thread(
        target=killer_reminder_loop, args=(subs,), daemon=True
    ).start()
    threading.Thread(
        target=gpu_monitor_loop, args=(subs, bot_username), daemon=True
    ).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("[bot] Shutdown — releasing all VRAM garrisons...")
        freed = release_all()
        for idx, mb in freed:
            print(f"[bot] GPU {idx}: {mb:,}MB freed")
        print("[bot] Standing down.")


if __name__ == "__main__":
    main()
