"""Offline simulation tests for openai_usage_bot — security + spend monitoring.

Pure-Python, no network. Mocks every requests.* and Telegram entry point.
Run: python3 OpenAIUsageBot/tests/test_spend_security.py
"""

import os
import sys
import time
import json
import threading
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("OPENAI_ADMIN_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test_primary")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openai_usage_bot as bot

# Redirect the intel log for the WHOLE suite — without this, test broadcasts
# (fake milestones, fake anomaly alerts) pollute the real events-*.jsonl.
bot.LOGS_DIR = Path(tempfile.mkdtemp(prefix="intel_suite_"))


def _fresh_stores():
    tmp = tempfile.mkdtemp(prefix="bot_test_")
    return (
        bot.UsageStore(Path(tmp) / "usage.json"),
        bot.SubscriberStore(Path(tmp) / "subs.json", "test_primary"),
        bot.NameStore(Path(tmp) / "names.json", "test_primary"),
        tmp,
    )


# ─── Security regression tests (carried forward from previous pass) ────────

def test_name_html_escape():
    _, _, names, _ = _fresh_stores()
    names.set("c1", "<script>alert(1)</script>")
    assert names.get("c1") == "&lt;script&gt;alert(1)&lt;/script&gt;", names.get("c1")
    names.set("c2", "  Bach   the   Monarch  ")
    assert names.get("c2") == "Bach the Monarch", names.get("c2")
    names.set("c3", "x" * 200)
    assert len(names.get("c3")) <= 48
    print("  ✅ HTML escape + whitespace collapse + length cap")


def test_atomic_write():
    tmp = Path(tempfile.mkdtemp(prefix="atomic_"))
    target = tmp / "data.json"
    bot._atomic_write_json(target, {"a": 1})
    assert target.exists() and not (tmp / "data.json.tmp").exists()
    print("  ✅ _atomic_write_json leaves no tmp file")


def test_busy_claim_atomic():
    bot._release_busy()
    assert bot._try_claim_busy() is True
    assert bot._try_claim_busy() is False
    bot._release_busy()
    assert bot._try_claim_busy() is True
    bot._release_busy()
    print("  ✅ Busy claim atomic, releases cleanly")


def test_callback_refuses_when_busy():
    usage, subs, names, _ = _fresh_stores()
    bot._release_busy()
    assert bot._try_claim_busy()
    _, _, toast = bot.handle_archive_callback(
        "arch:seal:normal:all", usage, subs, names, "Bach", "chat1", 999)
    assert "already running" in toast.lower()
    bot._release_busy()
    print("  ✅ Callback refuses when busy claim held")


def test_callback_validates_inputs():
    usage, subs, names, _ = _fresh_stores()
    bot._release_busy()
    for data, expect_substr in [
        ("arch:nuke:normal:all",      "unknown action"),
        ("arch:seal:everything:all",  "unknown mode"),
        ("arch:seal:normal:9999",     "unknown project"),
        ("arch:seal:normal:abc",      "unknown project"),
        ("arch:weird",                "malformed"),
    ]:
        _, _, toast = bot.handle_archive_callback(data, usage, subs, names, "Bach", "chat1", 999)
        assert expect_substr in toast.lower(), f"input {data!r} → {toast!r}"
    assert not bot._is_busy(), "busy claim must be released after invalid inputs"
    print("  ✅ Callback validates action/mode/pidx and releases busy on errors")


# ─── Model classification ──────────────────────────────────────────────────

def test_model_classification():
    """Mirrors OpenAI's published free-tier lists verbatim (synced 2026-08-21).
    Re-check: help.openai.com article 10306912 -> "What models are included"."""
    # 1M group (250K for usage tiers 1-2) - OpenAI publishes dated snapshots
    premium = ["gpt-5.6-sol", "gpt-5.5-2026-04-23", "gpt-5.4-2026-03-05",
               "gpt-5.2-2025-12-11", "gpt-5.1-2025-11-13", "gpt-5.1-codex",
               "gpt-5-codex", "gpt-5-2025-08-07", "gpt-5-chat-latest",
               "gpt-4.5-preview-2025-02-27", "gpt-4.1-2025-04-14",
               "gpt-4o-2024-05-13", "gpt-4o-2024-08-06", "gpt-4o-2024-11-20",
               "o3-2025-04-16", "o1-preview-2024-09-12", "o1-2024-12-17"]
    # 10M group (2.5M for usage tiers 1-2)
    normal = ["gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.4-mini-2026-03-17",
              "gpt-5.4-nano-2026-03-17", "gpt-5.1-codex-mini",
              "gpt-5-mini-2025-08-07", "gpt-5-nano-2025-08-07",
              "gpt-4.1-mini-2025-04-14", "gpt-4.1-nano-2025-04-14",
              "gpt-4o-mini-2024-07-18", "o4-mini-2025-04-16",
              "o1-mini-2024-09-12", "codex-mini-latest"]
    for m in premium: assert bot._track_for_model(m) == "premium", m
    for m in normal:  assert bot._track_for_model(m) == "normal",  m

    # Bare aliases must resolve too - the usage API sometimes reports the alias
    # rather than the resolved snapshot.
    for m, exp in [("gpt-5.6-sol", "premium"), ("gpt-5.5", "premium"),
                   ("gpt-5.4", "premium"), ("gpt-5", "premium"),
                   ("gpt-4o", "premium"), ("o1", "premium"), ("o3", "premium"),
                   ("gpt-5.6-terra", "normal"), ("gpt-5.6-luna", "normal"),
                   ("gpt-4o-mini", "normal"), ("gpt-5-mini", "normal"),
                   ("o4-mini", "normal")]:
        assert bot._track_for_model(m) == exp, f"{m} -> {bot._track_for_model(m)}"

    # Unlisted models - the spend-anomaly / quarantine target class
    for m in ["sora-2", "dall-e-3", "gpt-3.5-turbo", "text-embedding-3-small",
              "text-embedding-3-large", "whisper-1", "tts-1", "gpt-image-2"]:
        assert bot._track_for_model(m) is None, m
    # OpenAI excludes fine-tuned models from the offer regardless of base model
    assert bot._track_for_model("ft:gpt-4o-mini-2024-07-18:acme::abc123") is None
    print("  \u2705 Both free-tier lists match OpenAI's published groups (30 models + aliases)")


def test_same_prefix_paid_products_are_unlisted():
    """Regression for the greedy-prefix bug: paid products sharing a listed
    prefix (o1-pro is $150/$600 per 1M!) must NOT classify into a free tier —
    they must be None so the off-watchlist anomaly alert fires."""
    paid_lookalikes = [
        "o1-pro",                 # was: premium via 'o1-' prefix
        "o3-pro",                 # was: premium via 'o3-'
        "gpt-5-pro",              # was: premium via 'gpt-5-'
        "gpt-5.4-pro",            # was: premium via 'gpt-5.4-'
        "gpt-5.2-pro",            # was: premium via 'gpt-5.2-'
        "gpt-5.4-cyber",          # was: premium via 'gpt-5.4-'
        "gpt-5.2-chat-latest",    # was: premium via 'gpt-5.2-' (only gpt-5-chat-latest is free)
        "gpt-5-search-api",       # was: premium via 'gpt-5-'
        "gpt-4o-mini-tts",        # was: NORMAL via 'gpt-4o-mini-'
        "gpt-4o-mini-transcribe", # was: NORMAL via 'gpt-4o-mini-'
        "gpt-4o-transcribe",      # was: premium via 'gpt-4o-'
        "gpt-4o-transcribe-diarize",
        "o1-mini-tts-hypothetical",  # future-proofing: suffix on a normal-list name
        # paid siblings of NOW-LISTED families (gpt-5.5 / gpt-5.6-* joined the
        # free tiers on 2026-08-21; their -pro/-codex variants did NOT)
        "gpt-5.5-pro", "gpt-5.6-sol-pro", "gpt-5.3-codex", "gpt-5.3-chat-latest",
        "chat-latest", "gpt-image-2", "gpt-realtime-2.1", "gpt-audio-1.5",
    ]
    for m in paid_lookalikes:
        got = bot._track_for_model(m)
        assert got is None, f"{m} classified as {got!r} — should be unlisted (None)"
    # And the exact listed names still work
    assert bot._track_for_model("gpt-5-chat-latest") == "premium"
    assert bot._track_for_model("codex-mini-latest") == "normal"
    print("  ✅ Same-prefix paid products (o1-pro, *-tts, *-pro, …) all unlisted")


# ─── Spend monitoring ──────────────────────────────────────────────────────

def test_daily_limit_is_two_dollars():
    assert bot.DAILY_LIMIT == 2.00, f"DAILY_LIMIT = {bot.DAILY_LIMIT}"
    # The cap milestone in SPEND_MILESTONES matches DAILY_LIMIT
    cap_entries = [(t, l) for t, l in bot.SPEND_MILESTONES if l == "cap"]
    assert len(cap_entries) == 1, f"expected one cap entry, got {cap_entries}"
    assert cap_entries[0][0] == bot.DAILY_LIMIT
    print("  ✅ DAILY_LIMIT = $2.00 and SPEND_MILESTONES cap aligns")


def test_spend_milestones_fire_in_order():
    usage, subs, names, _ = _fresh_stores()
    usage._data["spend_seeded"] = True
    fired = []
    with mock.patch.object(bot, "_send", side_effect=lambda text, *a, **kw: fired.append(text)):
        for total, expect, cap_expected in [
            (0.30, "$0.10", False),
            (0.80, "$0.50", False),
            (1.20, "$1.00", False),
            (1.70, "$1.50", False),
            (2.10, "$2.00", True),
        ]:
            snap = {"total_cost": total, "projects": {}}
            new_spend, cap = bot.check_spend(snap, usage, subs, names)
            assert new_spend, f"new=False at total=${total}"
            assert cap is cap_expected, f"cap={cap} expected={cap_expected} at total=${total}"
            assert expect in fired[-1], f"expected {expect} in last msg, got {fired[-1][:200]!r}"

        # Re-check — no new alerts when nothing crossed
        before = len(fired)
        new_spend, cap = bot.check_spend({"total_cost": 2.10, "projects": {}},
                                          usage, subs, names)
        assert not new_spend and not cap and len(fired) == before
    print("  ✅ Spend milestones fire in order; dedup prevents re-spam")


def test_per_project_spend_thresholds():
    usage, subs, names, _ = _fresh_stores()
    usage._data["spend_seeded"] = True
    fired = []
    with mock.patch.object(bot, "_send", side_effect=lambda text, *a, **kw: fired.append(text)):
        snap = {
            "total_cost": 0.05,  # below first org milestone
            "projects": {
                "proj_J4rNEXilII2l889OotmE7YNW": {"cost_usd": 0.30, "models": {}},
            },
        }
        new, _ = bot.check_spend(snap, usage, subs, names)
        assert new
        proj_msgs = [t for t in fired if "Project Spend" in t]
        assert proj_msgs, f"expected per-project alert in: {fired}"
        assert "ngjabach-project" in proj_msgs[-1]
        assert "$0.25" in proj_msgs[-1]
    print("  ✅ Per-project spend threshold fires with project name + threshold")


def test_per_project_no_duplicate_alerts():
    usage, subs, names, _ = _fresh_stores()
    usage._data["spend_seeded"] = True
    fired = []
    with mock.patch.object(bot, "_send", side_effect=lambda text, *a, **kw: fired.append(text)):
        snap = {"total_cost": 0.30, "projects": {
            "proj_X": {"cost_usd": 0.30, "models": {}}}}
        bot.check_spend(snap, usage, subs, names)
        first = len(fired)
        bot.check_spend(snap, usage, subs, names)
        assert len(fired) == first, "second call should not re-fire same threshold"
    print("  ✅ Per-project threshold is deduped per (pid, threshold)")


def test_unlisted_model_alert_first_touch():
    usage, subs, names, _ = _fresh_stores()
    fired = []
    with mock.patch.object(bot, "_send", side_effect=lambda text, *a, **kw: fired.append(text)):
        snap = {
            "projects": {
                "proj_J4rNEXilII2l889OotmE7YNW": {
                    "cost_usd": 0.08,
                    "models": {
                        "text-embedding-3-small": {"input": 1000, "output": 0, "requests": 5},
                        "gpt-4o-mini": {"input": 200, "output": 100, "requests": 2},
                    },
                },
            },
        }
        assert bot.check_unlisted_models(snap, usage, subs, names)
        unlisted_msgs = [t for t in fired if "Unlisted Model" in t]
        assert len(unlisted_msgs) == 1
        assert "text-embedding-3-small" in unlisted_msgs[0]
        assert "gpt-4o-mini" not in unlisted_msgs[0]

        # Re-run — dedup
        fired.clear()
        assert not bot.check_unlisted_models(snap, usage, subs, names)
        assert not [t for t in fired if "Unlisted Model" in t]
    print("  ✅ Unlisted-model alert fires once per (pid, model) per day")


def test_unlisted_model_skips_zero_usage():
    usage, subs, names, _ = _fresh_stores()
    fired = []
    with mock.patch.object(bot, "_send", side_effect=lambda text, *a, **kw: fired.append(text)):
        snap = {"projects": {"proj_X": {"cost_usd": 0.0, "models": {
            "sora-2": {"input": 0, "output": 0, "requests": 0}}}}}
        assert not bot.check_unlisted_models(snap, usage, subs, names)
        assert not fired
    print("  ✅ Zero-usage unlisted model does not spam alerts")


def test_unlisted_model_per_project_dedup():
    """A model alerted in project A should still alert in project B."""
    usage, subs, names, _ = _fresh_stores()
    fired = []
    with mock.patch.object(bot, "_send", side_effect=lambda text, *a, **kw: fired.append(text)):
        snap_a = {"projects": {"proj_A": {"cost_usd": 0.01, "models": {
            "text-embedding-3-small": {"input": 100, "output": 0, "requests": 1}}}}}
        snap_b = {"projects": {"proj_B": {"cost_usd": 0.01, "models": {
            "text-embedding-3-small": {"input": 100, "output": 0, "requests": 1}}}}}
        bot.check_unlisted_models(snap_a, usage, subs, names)
        bot.check_unlisted_models(snap_b, usage, subs, names)
        unlisted_msgs = [t for t in fired if "Unlisted Model" in t]
        assert len(unlisted_msgs) == 2, f"expected 2 alerts (1 per project), got {len(unlisted_msgs)}"
    print("  ✅ Unlisted model dedup is per (pid, model), not just per model")


def test_seed_spend_marks_crossed_silently():
    """Bot restart at $1.60 spend: seed should mark $0.10/$0.50/$1.00/$1.50 notified,
    fire ONE catch-up alert for $1.50 (the highest crossed), and not re-fire on next poll."""
    usage, subs, names, _ = _fresh_stores()
    fired = []
    with mock.patch.object(bot, "_send", side_effect=lambda text, *a, **kw: fired.append(text)):
        snap = {"total_cost": 1.60, "projects": {}}
        bot.seed_spend(snap, usage, subs, names)
        assert usage.has_spend_seeded()
        assert {0.10, 0.50, 1.00, 1.50}.issubset(usage.get_spend_milestones_notified())
        assert 2.00 not in usage.get_spend_milestones_notified()
        # Exactly one catch-up broadcast — the highest crossed
        catchups = [t for t in fired if "Spend" in t]
        assert len(catchups) == 1, f"expected 1 catch-up broadcast, got {len(catchups)}: {fired}"
        # Next normal poll — no new alerts at the same total
        fired.clear()
        new, cap = bot.check_spend(snap, usage, subs, names)
        assert not new and not cap and not fired
    print("  ✅ Spend seed marks crossed silently, fires one catch-up, no re-spam")


def test_spend_seed_atomic():
    usage, _, _, _ = _fresh_stores()
    results = []
    lock = threading.Lock()
    def attempt():
        with lock:
            pass
        won = usage.claim_spend_seed()
        with lock:
            results.append(won)
    threads = [threading.Thread(target=attempt) for _ in range(30)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert sum(results) == 1, f"expected 1 winner, got {sum(results)}"
    print("  ✅ 30 concurrent claim_spend_seed → exactly 1 winner")


def test_overcap_escalation_keeps_alerting():
    """After the $2 cap, every extra $0.50 must fire another cap-level alert —
    the bot must never go silent while the bleed continues."""
    usage, subs, names, _ = _fresh_stores()
    usage._data["spend_seeded"] = True
    fired = []
    with mock.patch.object(bot, "_send", side_effect=lambda text, *a, **kw: fired.append(text)):
        # First poll at $2.10 — static cap ($2.00) fires
        new, cap = bot.check_spend({"total_cost": 2.10, "projects": {}}, usage, subs, names)
        assert new and cap
        n_after_cap = len(fired)

        # Same total again — silent
        new, cap = bot.check_spend({"total_cost": 2.10, "projects": {}}, usage, subs, names)
        assert not new and not cap and len(fired) == n_after_cap

        # Bleed continues to $3.10 — steps $2.50 and $3.00 both fire, cap=True again
        new, cap = bot.check_spend({"total_cost": 3.10, "projects": {}}, usage, subs, names)
        assert new and cap
        assert len(fired) == n_after_cap + 2, f"expected 2 escalation alerts, got {len(fired) - n_after_cap}"

        # Same total — silent again
        new, cap = bot.check_spend({"total_cost": 3.10, "projects": {}}, usage, subs, names)
        assert not new and not cap
    print("  ✅ Overcap escalation: new cap alert per extra $0.50, deduped")


def test_seed_spend_covers_overcap_steps():
    """Restart at $3.40: seed must mark $2.50 and $3.00 silently too, so the
    next poll doesn't flood every escalation step at once."""
    usage, subs, names, _ = _fresh_stores()
    fired = []
    with mock.patch.object(bot, "_send", side_effect=lambda text, *a, **kw: fired.append(text)):
        bot.seed_spend({"total_cost": 3.40, "projects": {}}, usage, subs, names)
        assert len(fired) == 1, f"seed should fire exactly 1 catch-up, got {len(fired)}"
        # Next poll at the same spend — fully silent
        fired.clear()
        new, cap = bot.check_spend({"total_cost": 3.40, "projects": {}}, usage, subs, names)
        assert not new and not cap and not fired
        # Bleed continues to $3.60 — exactly one new step ($3.50) fires
        new, cap = bot.check_spend({"total_cost": 3.60, "projects": {}}, usage, subs, names)
        assert new and cap and len(fired) == 1
    print("  ✅ Seed at $3.40 marks overcap steps silently; escalation resumes cleanly")


def test_day_rollover_resets_spend_tracking():
    usage, _, _, _ = _fresh_stores()
    usage._data["spend_seeded"] = True
    usage.add_spend_milestone_notified(0.50)
    usage.add_project_spend_notified("proj_X", 1.00)
    usage.mark_unlisted_alerted("proj_X", "text-embedding-3-small")

    usage._data["date"] = "2026-01-01"
    usage.update({"date": "2026-01-02", "projects": {}})

    assert not usage.get_spend_milestones_notified()
    assert not usage.get_project_spend_notified("proj_X")
    assert not usage.is_unlisted_alerted("proj_X", "text-embedding-3-small")
    assert not usage.has_spend_seeded()
    print("  ✅ Day rollover resets ALL spend-tracking state")


def test_check_spend_handles_missing_cost():
    """A poll with no cost data shouldn't raise."""
    usage, subs, names, _ = _fresh_stores()
    usage._data["spend_seeded"] = True
    with mock.patch.object(bot, "_send"):
        # total_cost absent
        new, cap = bot.check_spend({"projects": {}}, usage, subs, names)
        assert not new and not cap
        # total_cost = None
        new, cap = bot.check_spend({"total_cost": None, "projects": {}}, usage, subs, names)
        assert not new and not cap
        # project cost = None
        new, cap = bot.check_spend({"total_cost": 0.0, "projects": {
            "proj_X": {"cost_usd": None, "models": {}}}}, usage, subs, names)
        assert not new and not cap
    print("  ✅ Missing/None cost handled gracefully")


# ─── Wave guard (throttle-lag fix) ──────────────────────────────────────────

def test_per_track_seal_thresholds():
    """Premium needs a far bigger relative buffer than normal: the wave crossed
    the old 50k buffer during the sweep itself on 2026-08-13."""
    assert bot.NORMAL_TRACK_SEAL_THRESHOLD  == 9_500_000, bot.NORMAL_TRACK_SEAL_THRESHOLD
    assert bot.PREMIUM_TRACK_SEAL_THRESHOLD ==   800_000, bot.PREMIUM_TRACK_SEAL_THRESHOLD
    # The buffer must exceed the worst MEASURED reporting blind spot (tokens
    # already spent when the seal fires but not yet visible). Observed max on
    # 2026-08-26: 166,438 — the old 150k buffer fell short and cost $0.097.
    buffer = bot.PREMIUM_TOKEN_HARD_CAP - bot.PREMIUM_TRACK_SEAL_THRESHOLD
    assert buffer >= 170_000, f"premium buffer {buffer:,} <= worst blind spot 166,438"
    print(f"  ✅ Premium seals at 800k ({buffer:,} buffer > 166k worst blind spot)")


def test_wave_projection_math():
    # Real numbers from the 2026-08-13 incident: 879.9k → 947.7k over 10 min
    # (~113 tok/s). Projection over 20 min must cross the 1M cap even though
    # 947.7k was still below the OLD static threshold (950k).
    projected = bot._wave_projected(947_700, 879_900, 600, lookahead=1200)
    assert projected >= 1_000_000, f"projected {projected:,} should cross 1M"

    # Slow burn: 10k over 10 min → projection stays far under cap.
    projected = bot._wave_projected(500_000, 490_000, 600, lookahead=1200)
    assert projected < 550_001, projected

    # Day rollover (negative delta) clamps to zero rate, never projects backwards.
    projected = bot._wave_projected(5_000, 900_000, 600, lookahead=1200)
    assert projected == 5_000, projected

    # Zero/near-zero dt must not divide by zero.
    bot._wave_projected(100, 50, 0)
    print("  ✅ Wave projection: catches the real 8-13 ramp, ignores slow burn, rollover-safe")


def _feed(guard, samples, cap=1_000_000, thr=850_000, track="premium", date="2026-08-14"):
    """Feed (ts, tok) samples; return list of verdicts (None or dict)."""
    return [guard.observe(date, track, tok, cap, thr, False, now_ts=ts)
            for ts, tok in samples]


def test_wave_guard_ignores_ingestion_chunk():
    """Regression for the 46% false seal (2026-08-14): one delayed ingestion
    chunk makes a single poll-to-poll delta look like ~450 tok/s. The windowed
    rate + 2-poll confirmation must NOT fire."""
    g = bot._WaveGuard()
    # Flat at 433k for 4 min, then one +27k chunk lands, then flat again.
    verdicts = _feed(g, [
        (0,   433_000), (60,  433_000), (120, 433_000), (180, 433_000),
        (240, 460_000),           # the chunk — instantaneous rate looks huge
        (300, 460_000), (360, 460_000),
    ])
    assert all(v is None for v in verdicts), f"chunk artifact fired: {verdicts}"
    print("  ✅ One ingestion chunk (the 46% incident) no longer triggers a seal")


def test_wave_guard_floor_gate():
    """Below 60% utilization the predictor stays disarmed no matter the rate."""
    g = bot._WaveGuard()
    # Violent sustained ramp but under 600k the whole time.
    verdicts = _feed(g, [(i * 60, 300_000 + i * 40_000) for i in range(7)])  # →540k
    assert all(v is None for v in verdicts), verdicts
    print("  ✅ Utilization floor: no predictive seal below 60% of cap")


def test_wave_guard_fires_on_sustained_wave():
    """A genuine sustained wave above the floor fires after 2 confirmations."""
    g = bot._WaveGuard()
    # 650k climbing 30k/min (500 tok/s) — projects >1M once the window spans 2 min.
    verdicts = _feed(g, [(i * 60, 650_000 + i * 30_000) for i in range(6)])
    fired = [v for v in verdicts if v]
    assert fired, "sustained wave must fire"
    first = next(i for i, v in enumerate(verdicts) if v)
    assert first >= 3, f"needs window span + 2 confirmations, fired at poll {first}"
    assert fired[0]["rate"] > 400, fired[0]
    print(f"  ✅ Sustained 500 tok/s wave fires at poll {first} (~{650 + first * 30}k)")


def test_wave_guard_streak_resets_when_wave_subsides():
    g = bot._WaveGuard()
    verdicts = _feed(g, [
        (0,   650_000), (60,  680_000), (120, 710_000),   # ramp → 1st confirmation
        (180, 711_000), (240, 712_000), (300, 713_000),   # wave dies → streak resets
        (360, 714_000),
    ])
    assert all(v is None for v in verdicts), f"subsided wave must not fire: {verdicts}"
    print("  ✅ Confirmation streak resets when the wave subsides")


def test_wave_guard_resets_on_day_change():
    g = bot._WaveGuard()
    _feed(g, [(0, 650_000), (60, 700_000), (120, 750_000)])
    # New day: history must not carry over (no bogus span/rate)
    v = g.observe("2026-08-15", "premium", 900_000, 1_000_000, 850_000, False, now_ts=200)
    assert v is None
    print("  ✅ Sample history resets on UTC day change")


def test_mass_seal_parallel_covers_all_projects():
    """Parallel sweep must process every non-exempt project and retry failures."""
    usage, subs, names, _ = _fresh_stores()
    bot._release_busy()
    calls, fail_once = [], {"proj_OWrxxJaWk5MXHBi3HIdPxBDh"}   # oduong fails 1st try

    def fake_throttle(pid, track, usage_):
        calls.append(pid)
        if pid in fail_once:
            fail_once.discard(pid)
            return "failed"
        return "throttled"

    with mock.patch.object(bot, "_throttle_track_for_project", side_effect=fake_throttle), \
         mock.patch.object(bot, "_ordered_projects_for_track_seal",
                           return_value=list(bot.KNOWN_PROJECTS)), \
         mock.patch.object(bot, "_send"):
        assert bot._try_claim_busy()
        try:
            bot._mass_seal_track("premium", usage, subs, names, consumed=850_000)
        finally:
            bot._release_busy()

    n_projects = len(bot.KNOWN_PROJECTS)
    assert len(set(calls)) == n_projects, f"only {len(set(calls))}/{n_projects} projects attempted"
    # oduong appears twice: initial failure + retry that succeeds
    assert calls.count("proj_OWrxxJaWk5MXHBi3HIdPxBDh") == 2, "failed project must be retried"
    assert usage.is_mass_sealed("premium")
    print(f"  ✅ Parallel sweep hits all {n_projects} projects; failure retried in-sweep")


def test_seal_gap_repair():
    """A project missing from sealed_tracks after the sweep must be re-sealed
    on the next poll — this is the fix for the oduong post-cap leak."""
    usage, subs, names, _ = _fresh_stores()
    bot._release_busy()
    bot._REPAIR_DONE.clear()
    usage.mark_mass_sealed("premium")
    # Everyone sealed except oduong (failed) and phongnguyen (exempt)
    for pid in bot.KNOWN_PROJECTS:
        if pid in ("proj_OWrxxJaWk5MXHBi3HIdPxBDh", "proj_zRWDq4YWIDEkxbgMAjX0xy79"):
            continue
        usage.add_track_originals("premium", pid, [{"id": "rl1", "model": "gpt-4o"}])
    usage.add_track_exemption("proj_zRWDq4YWIDEkxbgMAjX0xy79", "premium")

    attempted = []
    with mock.patch.object(bot, "_throttle_track_for_project",
                           side_effect=lambda pid, t, u: (attempted.append(pid), "throttled")[1]), \
         mock.patch.object(bot, "_send"):
        bot._repair_seal_gaps("premium", usage, subs, names)

    assert attempted == ["proj_OWrxxJaWk5MXHBi3HIdPxBDh"], \
        f"only the gap project should be re-attempted, got {attempted}"

    # Second pass: memo prevents re-POSTing the same project
    attempted.clear()
    with mock.patch.object(bot, "_throttle_track_for_project",
                           side_effect=lambda pid, t, u: (attempted.append(pid), "throttled")[1]), \
         mock.patch.object(bot, "_send"):
        bot._repair_seal_gaps("premium", usage, subs, names)
    assert attempted == [], "repaired project must not be re-attempted (memo)"
    print("  ✅ Gap repair seals exactly the straggler, skips exempt, memoized")


def test_seal_gap_repair_respects_busy():
    usage, subs, names, _ = _fresh_stores()
    bot._release_busy()
    bot._REPAIR_DONE.clear()
    usage.mark_mass_sealed("premium")   # all projects are gaps
    assert bot._try_claim_busy()
    try:
        with mock.patch.object(bot, "_throttle_track_for_project") as thr, \
             mock.patch.object(bot, "_send"):
            bot._repair_seal_gaps("premium", usage, subs, names)
            assert not thr.called, "repair must defer while busy claim is held"
    finally:
        bot._release_busy()
    print("  ✅ Gap repair defers when another seal op is running")


# ─── Quarantine (auto full-seal on off-watchlist usage) ─────────────────────

OFFENDER_SNAP = {
    "projects": {
        "proj_OWrxxJaWk5MXHBi3HIdPxBDh": {   # offender - used an off-list model
            "cost_usd": 0.5,
            "models": {"text-embedding-3-small": {"input": 9000, "output": 2000, "requests": 4},
                       "gpt-4o-mini": {"input": 100, "output": 50, "requests": 1}},
        },
        "proj_J4rNEXilII2l889OotmE7YNW": {   # ngjabach — listed models only
            "cost_usd": 0.0,
            "models": {"gpt-5-mini": {"input": 500, "output": 100, "requests": 2}},
        },
    },
}


def test_quarantine_seals_offender_only():
    usage, subs, names, _ = _fresh_stores()
    bot._release_busy(); bot._QUARANTINE_NOOP.clear()
    sealed = []
    def fake_full_seal(pid, u):
        sealed.append(pid)
        u.add_track_originals(bot.QUARANTINE_TRACK, pid, [{"id": "r1", "model": "text-embedding-3-small"}])
        return "sealed"
    with mock.patch.object(bot, "_full_seal_project", side_effect=fake_full_seal), \
         mock.patch.object(bot, "_send"):
        bot._quarantine_unlisted_users(OFFENDER_SNAP, usage, subs, names)
    assert sealed == ["proj_OWrxxJaWk5MXHBi3HIdPxBDh"], sealed
    assert usage.is_project_track_sealed("proj_OWrxxJaWk5MXHBi3HIdPxBDh", "full")

    # Second poll: already sealed → no re-attempt
    sealed.clear()
    with mock.patch.object(bot, "_full_seal_project", side_effect=fake_full_seal), \
         mock.patch.object(bot, "_send"):
        bot._quarantine_unlisted_users(OFFENDER_SNAP, usage, subs, names)
    assert sealed == [], "already-quarantined project must not be re-sealed"
    print("  ✅ Quarantine seals exactly the offender, once; clean projects untouched")


def test_quarantine_respects_exemption_and_retries_failure():
    usage, subs, names, _ = _fresh_stores()
    bot._release_busy(); bot._QUARANTINE_NOOP.clear()
    # Exempt (user released earlier today) → skipped
    usage.add_track_exemption("proj_OWrxxJaWk5MXHBi3HIdPxBDh", bot.QUARANTINE_TRACK)
    with mock.patch.object(bot, "_full_seal_project") as fs, mock.patch.object(bot, "_send"):
        bot._quarantine_unlisted_users(OFFENDER_SNAP, usage, subs, names)
        assert not fs.called, "exempt project must not be quarantined"
    usage.remove_track_exemption("proj_OWrxxJaWk5MXHBi3HIdPxBDh", bot.QUARANTINE_TRACK)

    # Failure → retried on the next poll (not memoized)
    calls = []
    with mock.patch.object(bot, "_full_seal_project",
                           side_effect=lambda p, u: (calls.append(p), "failed")[1]), \
         mock.patch.object(bot, "_send"):
        bot._quarantine_unlisted_users(OFFENDER_SNAP, usage, subs, names)
        bot._quarantine_unlisted_users(OFFENDER_SNAP, usage, subs, names)
    assert len(calls) == 2, f"failed quarantine must retry, got {len(calls)} attempts"

    # noop → memoized, no retry
    calls.clear()
    with mock.patch.object(bot, "_full_seal_project",
                           side_effect=lambda p, u: (calls.append(p), "noop")[1]), \
         mock.patch.object(bot, "_send"):
        bot._quarantine_unlisted_users(OFFENDER_SNAP, usage, subs, names)
        bot._quarantine_unlisted_users(OFFENDER_SNAP, usage, subs, names)
    assert len(calls) == 1, "noop quarantine must be memoized"
    print("  ✅ Quarantine skips exempt, retries failures, memoizes noops")


def test_full_seal_project_captures_healthy_only():
    usage, _, _, _ = _fresh_stores()
    rows = [
        {"id": "r-mini", "model": "gpt-4o-mini", "max_requests_per_1_minute": 5000,
         "max_tokens_per_1_minute": 4_000_000},
        {"id": "r-emb", "model": "text-embedding-3-small", "max_requests_per_1_minute": 3000,
         "max_tokens_per_1_minute": 1_000_000},
        {"id": "r-dead", "model": "gpt-3.5-turbo", "max_requests_per_1_minute": 0,
         "max_tokens_per_1_minute": 0},   # pre-zeroed — must NOT be captured
    ]
    posted = []
    with mock.patch.object(bot, "_fetch_project_rate_limits", return_value=rows), \
         mock.patch.object(bot, "_update_project_rate_limit",
                           side_effect=lambda p, rid, pl: (posted.append((rid, pl)), True)[1]), \
         mock.patch.object(bot.time, "sleep"):
        assert bot._full_seal_project("proj_X", usage) == "sealed"
    assert {rid for rid, _ in posted} == {"r-mini", "r-emb", "r-dead"}, posted
    caps = usage.get_sealed_tracks()["full"]["originals_by_project"]["proj_X"]
    ids  = {c["id"] for c in caps}
    assert ids == {"r-mini", "r-emb"}, f"pre-zeroed row must not be captured: {ids}"
    print("  ✅ Full seal throttles ALL rows (embeddings included), captures healthy only")


def test_release_quarantine_restores_and_exempts():
    usage, subs, names, _ = _fresh_stores()
    bot._release_busy()
    pid = "proj_OWrxxJaWk5MXHBi3HIdPxBDh"
    usage.add_track_originals("full", pid, [{"id": "r-emb", "model": "text-embedding-3-small",
                                             "max_requests_per_1_minute": 3000}])
    with mock.patch.object(bot, "_compute_canonical_baseline", return_value={}), \
         mock.patch.object(bot, "_restore_rate_limits", return_value=0), \
         mock.patch.object(bot, "_send"):
        assert bot._release_quarantine(pid, usage, subs, names) == "unsealed"
    assert not usage.is_project_track_sealed(pid, "full")
    assert usage.is_exempt(pid, "full"), "released project must be exempt for the day"
    print("  ✅ Release restores rows, clears seal, exempts from re-quarantine")


def test_quarantine_rolls_over_at_midnight():
    usage, _, _, _ = _fresh_stores()
    usage.add_track_originals("full", "proj_X", [{"id": "r1", "model": "text-embedding-3-small"}])
    usage._data["date"] = "2026-01-01"
    usage.update({"date": "2026-01-02", "projects": {}})
    assert not usage.is_project_track_sealed("proj_X", "full")
    pending = usage.get_pending_track_unseal()
    assert "proj_X" in pending.get("full", {}).get("originals_by_project", {}), \
        "quarantine originals must queue for midnight restore"
    print("  ✅ Quarantine flows through the standard midnight restore queue")


# ─── Thread safety + HTML robustness (2026-08-22 audit) ─────────────────────

def test_cmd_refresh_never_blocks_telegram_thread():
    """cmd_refresh runs on the Telegram poll thread. A quarantine sweep or mass
    seal there would freeze every command for minutes, so both must be handed to
    a background worker. Regression for the audit finding."""
    usage, subs, names, _ = _fresh_stores()
    bot._release_busy()
    usage._data["spend_seeded"] = True
    # Over BOTH seal thresholds and with an off-list model in play.
    snap = {
        "date": bot.today_str(), "total_cost": 0.0,
        "total_normal_tokens": 9_900_000, "total_premium_tokens": 990_000,
        "projects": {"proj_J4rNEXilII2l889OotmE7YNW": {
            "cost_usd": 0.0, "num_requests": 1, "total_tokens": 100,
            "models": {"text-embedding-3-small": {"input": 100, "output": 0, "requests": 1}}}},
    }
    spawned = []
    def slow(*a, **k):
        time.sleep(5)   # would be a 5 s freeze if called inline
    with mock.patch.object(bot, "fetch_today_usage", return_value=snap), \
         mock.patch.object(bot, "_enrich_costs", side_effect=lambda s, *a, **k: s), \
         mock.patch.object(bot, "_fetch_recent_activity_by_band", return_value={}), \
         mock.patch.object(bot, "_quarantine_unlisted_users", side_effect=slow), \
         mock.patch.object(bot, "_handle_track_seal", side_effect=slow), \
         mock.patch.object(bot, "_spawn_bg",
                           side_effect=lambda label, fn, *a: spawned.append(label)), \
         mock.patch.object(bot, "_send"):
        t0 = time.time()
        out = bot.cmd_refresh(usage, subs, names, "Bach")
        elapsed = time.time() - t0
    assert elapsed < 1.0, f"cmd_refresh blocked for {elapsed:.1f}s — must be backgrounded"
    assert "quarantine/refresh" in spawned, spawned
    assert "track-seal/normal" in spawned and "track-seal/premium" in spawned, spawned
    assert "Data refreshed" in out
    print(f"  ✅ cmd_refresh returns in {elapsed*1000:.0f}ms; 3 sweeps backgrounded")


def test_model_names_escaped_in_alerts():
    """A model name containing HTML metacharacters must not break Telegram's
    parser — an unparseable message is a 400 and the alert is LOST entirely."""
    evil = 'gpt-<b>&"x'
    for text in (bot.fmt_unlisted_model("proj_X", evil, 3, 500, 0.02),
                 bot.fmt_quarantine("some-project", [evil])):
        assert "<b>&\"x" not in text, "raw metacharacters leaked into HTML"
        assert "&lt;b&gt;" in text and "&amp;" in text, text[:200]
    # Report commands render model names too
    usage, _, _, _ = _fresh_stores()
    usage._data.update({"date": "2026-08-22", "projects": {"p": {
        "name": "p", "total_tokens": 10, "input_tokens": 5, "output_tokens": 5,
        "num_requests": 1, "models": {evil: {"input": 5, "output": 5, "requests": 1}}}}})
    for out in (bot.cmd_tokens(usage), bot.cmd_models(usage)):
        assert "&lt;b&gt;" in out and "<code>gpt-<b>" not in out, out[:200]
    print("  ✅ Model names HTML-escaped in every alert and report path")


def test_quarantine_and_track_seal_coexist_without_cascade():
    """Full-day integration: a project is BOTH track-sealed and quarantined.
    Neither capture may record an already-zeroed row (that is the 0/0 cascade
    that once bricked projects), and midnight must restore healthy values."""
    usage, subs, names, _ = _fresh_stores()
    bot._release_busy(); bot._QUARANTINE_NOOP.clear()
    pid = "proj_J4rNEXilII2l889OotmE7YNW"
    live = {   # id -> current limits, mutated by the fake API
        "r-4o":  {"id": "r-4o",  "model": "gpt-4o", "max_requests_per_1_minute": 5000,
                  "max_tokens_per_1_minute": 4_000_000},
        "r-emb": {"id": "r-emb", "model": "text-embedding-3-small",
                  "max_requests_per_1_minute": 3000, "max_tokens_per_1_minute": 1_000_000},
    }
    def fake_get(p): return [dict(v) for v in live.values()]
    def fake_post(p, rid, payload):
        live[rid].update(payload); return True
    with mock.patch.object(bot, "_fetch_project_rate_limits", side_effect=fake_get), \
         mock.patch.object(bot, "_update_project_rate_limit", side_effect=fake_post), \
         mock.patch.object(bot.time, "sleep"), mock.patch.object(bot, "_send"):
        # 1. premium track seal zeroes the gpt-4o row and captures its originals
        assert bot._throttle_track_for_project(pid, "premium", usage) == "throttled"
        assert live["r-4o"]["max_requests_per_1_minute"] == 0
        assert live["r-emb"]["max_requests_per_1_minute"] == 3000, "embedding row untouched by track seal"
        # 2. quarantine then full-seals everything; must NOT re-capture the zeroed row
        assert bot._full_seal_project(pid, usage) == "sealed"
        assert live["r-emb"]["max_requests_per_1_minute"] == 0, "embedding row must be zeroed"
        caps = usage.get_sealed_tracks()["full"]["originals_by_project"][pid]
        assert {c["id"] for c in caps} == {"r-emb"}, f"0/0 cascade risk: captured {caps}"
        # 3. midnight rollover queues both entries
        usage._data["date"] = "2026-08-22"
        usage.update({"date": "2026-08-23", "projects": {}})
        pending = usage.get_pending_track_unseal()
        assert pid in pending["premium"]["originals_by_project"]
        assert pid in pending["full"]["originals_by_project"]
        # 4. drain restores HEALTHY values for every row
        with mock.patch.object(bot, "_compute_canonical_baseline", return_value={}):
            bot._process_pending_track_unseals(usage, subs, names)
    assert live["r-4o"]["max_requests_per_1_minute"] == 5000, live["r-4o"]
    assert live["r-emb"]["max_requests_per_1_minute"] == 3000, live["r-emb"]
    assert not usage.get_pending_track_unseal(), "queue must drain fully"
    print("  ✅ Track seal + quarantine coexist; midnight restores all rows, no 0/0 cascade")


def test_org_ceiling_clamp_on_restore():
    """A captured original above the ORG ceiling must clamp-and-retry, and must
    never permanently fail (which would strand the project sealed forever).
    Regression for the 2026-08-22 live finding on every *-pro row."""
    posts = []
    class R:
        def __init__(s, ok, body=None): s.ok, s._b, s.status_code, s.text = ok, body, 200 if ok else 400, str(body)
        def json(s): return s._b
    def fake_post(url, headers=None, json=None, timeout=None):
        posts.append(dict(json))
        if json.get("max_requests_per_1_minute", 0) > 500:
            return R(False, {"error": {"code": "organization_rate_limit_exceeded",
                "message": "The max_requests_per_1_minute for rl-gpt-5-pro cannot "
                           "exceed the organization rate limit of 500.0"}})
        return R(True, {})
    with mock.patch.object(bot.requests, "post", side_effect=fake_post):
        ok = bot._update_project_rate_limit("p", "rl-gpt-5-pro",
                {"max_requests_per_1_minute": 5000, "max_tokens_per_1_minute": 100})
    assert ok is True, "must not hard-fail"
    assert len(posts) == 2, f"expected original + clamped retry, got {posts}"
    assert posts[1]["max_requests_per_1_minute"] == 500, posts[1]
    assert posts[1]["max_tokens_per_1_minute"] == 100, "other fields preserved"

    # Unparseable ceiling -> soft-skip, still never strands the project
    def fake_post2(url, headers=None, json=None, timeout=None):
        return R(False, {"error": {"code": "organization_rate_limit_exceeded",
                                   "message": "nope"}})
    with mock.patch.object(bot.requests, "post", side_effect=fake_post2):
        assert bot._update_project_rate_limit("p", "rl-x", {"max_requests_per_1_minute": 9}) is True
    print("  ✅ Org-ceiling restores clamp-and-retry; never strand a sealed project")


def test_repair_detects_seal_drift():
    """A project the bot BELIEVES is sealed but whose limits are actually healthy
    must be detected and re-zeroed. Regression for the 2026-08-22 drift finding."""
    usage, subs, names, _ = _fresh_stores()
    bot._release_busy(); bot._REPAIR_DONE.clear()
    pid = "proj_J4rNEXilII2l889OotmE7YNW"
    usage.mark_mass_sealed("premium")
    # State says sealed, with one row captured...
    usage.add_track_originals("premium", pid, [
        {"id": "r-4o",  "model": "gpt-4o",  "max_requests_per_1_minute": 5000},
        {"id": "r-41",  "model": "gpt-4.1", "max_requests_per_1_minute": 5000}])
    # ...but reality: r-4o drifted back to healthy, r-41 still correctly 0
    live = {"r-4o": {"id": "r-4o", "model": "gpt-4o", "max_requests_per_1_minute": 5000,
                     "max_tokens_per_1_minute": 400000},
            "r-41": {"id": "r-41", "model": "gpt-4.1", "max_requests_per_1_minute": 0,
                     "max_tokens_per_1_minute": 0}}
    posted = []
    def fake_post(p, rid, payload, **kw):
        posted.append(rid); live[rid].update(payload); return True
    others = {p for p in bot.KNOWN_PROJECTS if p != pid}
    with mock.patch.object(bot, "_fetch_project_rate_limits",
                           side_effect=lambda p: [dict(v) for v in live.values()] if p == pid else []), \
         mock.patch.object(bot, "_update_project_rate_limit", side_effect=fake_post), \
         mock.patch.object(bot.time, "sleep"), mock.patch.object(bot, "_send"):
        for o in others:   # keep other projects out of the way
            usage.add_track_exemption(o, "premium")
        bot._repair_seal_gaps("premium", usage, subs, names)
    assert posted == ["r-4o"], f"only the drifted row should be re-zeroed: {posted}"
    assert live["r-4o"]["max_requests_per_1_minute"] == 0
    caps = {c["id"] for c in usage.get_sealed_tracks()["premium"]["originals_by_project"][pid]}
    assert caps == {"r-4o", "r-41"}, f"merge must preserve the still-sealed row: {caps}"
    print("  ✅ Drift detected and re-sealed; existing captures preserved by merge")


# ─── Lane costs + Exotic lane (2026-09-16) ──────────────────────────────────

def test_lane_for_line_item():
    """Costs-API line items map to lanes by their model token. Real line items
    observed live on 2026-09-15."""
    cases = {
        "gpt-4o-mini-2024-07-18, input":             "normal",
        "gpt-5.4-mini-2026-03-17, cached input":     "normal",
        "gpt-5.4-2026-03-05, output":                "premium",
        "gpt-5.6-sol, input":                        "premium",
        "gpt-audio-mini-2025-12-15 audio, input":    "exotic",   # modality word
        "gpt-audio-mini-2025-12-15 text, output":    "exotic",
        "text-embedding-3-small, input":             "exotic",
        "o1-pro, output":                            "exotic",   # paid lookalike
        "web search tool calls":                     "exotic",   # non-model item
        None:                                        "exotic",
        "":                                          "exotic",
    }
    for li, want in cases.items():
        got = bot._lane_for_line_item(li)
        assert got == want, f"{li!r} -> {got}, want {want}"
    print(f"  \u2705 {len(cases)} line-item shapes map to the right lane")


def test_lane_costs_reconcile_with_total():
    """The three lanes must always sum to the org total — reproduces the real
    2026-09-15 breakdown ($0.328293 across 10 line items)."""
    items = [
        ("proj_fvkY21dJ0ripiOIA2jCC86f3", "gpt-4o-mini-2024-07-18, input",           0.196282),
        ("proj_fvkY21dJ0ripiOIA2jCC86f3", "gpt-audio-mini-2025-12-15 audio, input",  0.045370),
        ("proj_fvkY21dJ0ripiOIA2jCC86f3", "gpt-audio-mini-2025-12-15 text, input",   0.043676),
        ("proj_fvkY21dJ0ripiOIA2jCC86f3", "gpt-audio-mini-2025-12-15 text, output",  0.032734),
        ("proj_fEboQnaVm4tQCk8kFy0h8s08", "gpt-5.4-mini-2026-03-17, output",         0.005162),
        ("proj_fEboQnaVm4tQCk8kFy0h8s08", "gpt-5.4-mini-2026-03-17, input",          0.004293),
        ("proj_fvkY21dJ0ripiOIA2jCC86f3", "gpt-4o-mini-2024-07-18, output",          0.000776),
        ("proj_fEboQnaVm4tQCk8kFy0h8s08", "gpt-5.4-mini-2026-03-17, cached input",   0.0),
    ]
    class R:
        ok = True
        def json(s):
            return {"has_more": False, "data": [{"results": [
                {"project_id": pid, "line_item": li, "amount": {"value": v}}
                for pid, li, v in items]}]}
    with mock.patch.object(bot, "_openai_call", return_value=R()):
        per_project, per_lane = bot._fetch_costs_breakdown()
    total = sum(v for _, _, v in items)
    assert abs(sum(per_lane.values()) - total) < 1e-9, "lanes must sum to total"
    assert abs(sum(per_project.values()) - total) < 1e-9, "projects must sum to total"
    assert abs(per_lane["normal"]  - 0.206513) < 1e-6, per_lane
    assert abs(per_lane["exotic"]  - 0.121780) < 1e-6, per_lane
    assert per_lane["premium"] == 0.0, per_lane
    print(f"  \u2705 Lanes reconcile: normal {per_lane['normal']:.6f} + exotic "
          f"{per_lane['exotic']:.6f} + premium 0 = {total:.6f}")


def test_fmt_cost_never_hides_real_spend():
    assert bot._fmt_cost(None)    == "\u2014"
    assert bot._fmt_cost(0)       == "0.00$"
    assert bot._fmt_cost(0.21)    == "0.21$"
    assert bot._fmt_cost(4.6209)  == "4.62$"
    # sub-cent must NOT round to zero — that is the Exotic lane's whole job
    assert bot._fmt_cost(0.0043)  == "0.0043$"
    assert bot._fmt_cost(0.00004) != "0.00$"
    print("  \u2705 _fmt_cost: x.y$ format, sub-cent spend never shown as zero")


def test_lane_lines_in_every_report():
    """Every token report shows the Cost: suffix on both lanes plus Exotic."""
    usage, subs, names, _ = _fresh_stores()
    usage._data.update({
        "date": "2026-09-15", "total_normal_tokens": 11_387_193,
        "total_premium_tokens": 0, "total_cost": 0.328293,
        "lane_costs": {"normal": 0.206513, "premium": 0.0, "exotic": 0.12178},
        "projects": {"proj_fvkY21dJ0ripiOIA2jCC86f3": {
            "name": "namvuong-project", "total_tokens": 100, "input_tokens": 50,
            "output_tokens": 50, "num_requests": 1, "cost_usd": 0.3,
            "models": {"gpt-4o-mini": {"input": 50, "output": 50, "requests": 1}}}},
    })
    snap = usage.get()
    reports = {
        "tokens":   bot.cmd_tokens(usage),
        "snapshot": bot.fmt_daily_snapshot(snap),
        "archive":  bot._fmt_archive_status(usage),
    }
    for name, out in reports.items():
        assert "0.21$" in out, f"{name}: normal lane cost missing"
        assert "0.12$" in out, f"{name}: exotic cost missing"
    for name in ("tokens", "snapshot"):
        out = reports[name]
        assert "/ 1M. Cost: 0.00$" in out, f"{name}: premium Cost suffix missing"
        assert "/ 10M. Cost: 0.21$" in out, f"{name}: normal Cost suffix missing"
        assert "\U0001f9ea Exotic" in out, f"{name}: Exotic lane missing"

    # Before the first costs fetch there is no lane data: show a dash, never crash
    usage2, _, _, _ = _fresh_stores()
    usage2._data.update({"date": "2026-09-15", "projects": {"p": {
        "name": "p", "total_tokens": 1, "models": {}}}})
    assert "Cost: \u2014" in bot.cmd_tokens(usage2)
    print("  \u2705 Cost suffix + Exotic lane render in tokens, snapshot, archive")


# ─── Network retry (2026-09-16) ─────────────────────────────────────────────

def test_network_errors_are_retried():
    """A DNS blip must not fail the call outright — it used to abort an entire
    186-row quarantine sweep (67 DNS failures in 18 h on 2026-09-15)."""
    import requests as rq
    calls = []
    class OK: ok = True
    def flaky(url, **kw):
        calls.append(url)
        if len(calls) < 3:
            raise rq.exceptions.ConnectionError("Failed to resolve 'api.openai.com'")
        return OK()
    with mock.patch.object(bot.requests, "post", side_effect=flaky), \
         mock.patch.object(bot.time, "sleep"):
        r = bot._openai_call("post", "https://api.openai.com/x", json={})
    assert r.ok and len(calls) == 3, f"expected 2 retries then success, got {len(calls)} calls"

    # Persistent failure: re-raises after 3 attempts so callers' except still fires
    calls.clear()
    def dead(url, **kw):
        calls.append(url); raise rq.exceptions.ConnectionError("down")
    with mock.patch.object(bot.requests, "get", side_effect=dead), \
         mock.patch.object(bot.time, "sleep"):
        try:
            bot._openai_call("get", "https://api.openai.com/x")
            assert False, "must re-raise after exhausting retries"
        except rq.exceptions.ConnectionError:
            pass
    assert len(calls) == 3, len(calls)

    # HTTP error RESPONSES are answers, not blips — never retried
    calls.clear()
    class Bad: ok = False; status_code = 400
    with mock.patch.object(bot.requests, "get", side_effect=lambda u, **k: (calls.append(u), Bad())[1]):
        assert bot._openai_call("get", "https://api.openai.com/x").ok is False
    assert len(calls) == 1, "HTTP errors must not be retried"
    print("  \u2705 DNS/connection errors retried x2; persistent failure re-raises; HTTP 4xx not retried")


def test_seal_survives_single_dns_blip():
    """End-to-end: one DNS failure mid-sweep no longer fails the full seal."""
    import requests as rq
    usage, _, _, _ = _fresh_stores()
    rows = [{"id": f"r{i}", "model": "gpt-4o", "max_requests_per_1_minute": 5000,
             "max_tokens_per_1_minute": 400000} for i in range(5)]
    n = {"posts": 0}
    class OK: ok = True
    def post(url, **kw):
        n["posts"] += 1
        if n["posts"] == 3:   # the third POST hits a DNS blip
            raise rq.exceptions.ConnectionError("Temporary failure in name resolution")
        return OK()
    with mock.patch.object(bot, "_fetch_project_rate_limits", return_value=rows), \
         mock.patch.object(bot.requests, "post", side_effect=post), \
         mock.patch.object(bot.time, "sleep"):
        assert bot._full_seal_project("proj_X", usage) == "sealed"
    assert n["posts"] == 6, f"5 rows + 1 retried = 6 POSTs, got {n['posts']}"
    print("  \u2705 Full seal survives a mid-sweep DNS blip (retried in place, no rollback)")


# ─── Local intel log ────────────────────────────────────────────────────────

def test_intel_log_captures_broadcasts():
    """Every _broadcast must land one JSONL entry in the intel log."""
    usage, subs, names, _ = _fresh_stores()
    logdir = Path(tempfile.mkdtemp(prefix="intel_"))
    with mock.patch.object(bot, "LOGS_DIR", logdir), mock.patch.object(bot, "_send"):
        bot._broadcast(lambda n: f"<b>test alert for {n}</b>", subs, names)
        bot._log_event("mode", from_mode="passive", to_mode="urgent")

    files = list(logdir.glob("events-*.jsonl"))
    assert len(files) == 1, f"expected one monthly file, got {files}"
    lines = [json.loads(l) for l in files[0].read_text().splitlines()]
    kinds = [l["kind"] for l in lines]
    assert kinds == ["broadcast", "mode"], kinds
    assert "test alert for Bach" in lines[0]["text"]
    assert lines[1]["from_mode"] == "passive" and lines[1]["to_mode"] == "urgent"
    assert all("ts" in l and "utc" in l for l in lines)
    print("  ✅ Broadcasts + events land as structured JSONL entries")


def test_intel_log_failure_never_breaks_bot():
    """A logging failure (e.g. unwritable dir) must print and continue."""
    usage, subs, names, _ = _fresh_stores()
    with mock.patch.object(bot, "LOGS_DIR", Path("/proc/definitely/not/writable")), \
         mock.patch.object(bot, "_send") as sender:
        bot._broadcast(lambda n: "still delivered", subs, names)   # must not raise
        assert sender.called, "Telegram delivery must proceed despite log failure"
    print("  ✅ Log failure is swallowed; Telegram delivery unaffected")


def test_mode_change_logged_once():
    """set_mode logs only on actual transitions, not same-mode re-sets."""
    usage, _, _, _ = _fresh_stores()
    logdir = Path(tempfile.mkdtemp(prefix="intel_"))
    with mock.patch.object(bot, "LOGS_DIR", logdir):
        usage.set_mode("urgent")     # passive → urgent: logged
        usage.set_mode("urgent")     # urgent → urgent: not logged
        usage.set_mode("passive")    # urgent → passive: logged
    files = list(logdir.glob("events-*.jsonl"))
    lines = [json.loads(l) for l in files[0].read_text().splitlines()]
    mode_events = [l for l in lines if l["kind"] == "mode"]
    assert len(mode_events) == 2, f"expected 2 mode events, got {len(mode_events)}"
    print("  ✅ Mode transitions logged exactly once each")


# ─── Run ────────────────────────────────────────────────────────────────────

# ─── Midnight straddle + parallel unseal (2026-09-29 incident) ─────────────

def test_midnight_straddle_fetch_labels_correct_day():
    """Replays 2026-09-29: a fetch starting 23:59:58 pages past midnight and gets
    the OLD day's 24.88M. It was stamped with the new date and mass-sealed all 13
    projects. The snapshot must carry the new day's data (re-fetch), never the
    old day's totals under the new date."""
    from datetime import datetime as real_dt, timezone as tz
    clock = {"now": real_dt(2026, 9, 28, 23, 59, 58, tzinfo=tz.utc)}

    class FakeDT(real_dt):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    nam = "proj_fvkY21dJ0ripiOIA2jCC86f3"
    windows = []

    def straddling_fetch(window=None):
        windows.append(window)
        if len(windows) == 1:   # midnight passes mid-pagination
            clock["now"] = real_dt(2026, 9, 29, 0, 0, 3, tzinfo=tz.utc)
            return {nam: {"normal_tokens": 24_882_107, "total_tokens": 24_882_107}}
        return {nam: {"normal_tokens": 3_000, "total_tokens": 3_000}}

    with mock.patch.object(bot, "datetime", FakeDT), \
         mock.patch.object(bot, "_fetch_tokens", side_effect=straddling_fetch):
        snap = bot.fetch_today_usage()
    assert snap["date"] == "2026-09-29", snap["date"]
    assert snap["total_normal_tokens"] == 3_000, "old day's 24.88M leaked into the new day"
    assert len(windows) == 2, f"expected one re-fetch, got {len(windows)} fetches"
    new_midnight = int(real_dt(2026, 9, 29, tzinfo=tz.utc).timestamp())
    assert windows[1][0] == new_midnight, "re-fetch must query the NEW day's window"

    # No straddle: exactly one fetch, labelled with its own window's day.
    clock["now"] = real_dt(2026, 9, 29, 12, 0, tzinfo=tz.utc)
    windows.clear()
    with mock.patch.object(bot, "datetime", FakeDT), \
         mock.patch.object(bot, "_fetch_tokens",
                           side_effect=lambda window=None: windows.append(window) or {}):
        snap = bot.fetch_today_usage()
    assert snap["date"] == "2026-09-29" and len(windows) == 1
    print("  ✅ Midnight-straddling fetch re-fetches the new day; stale totals never relabelled")


def test_store_ignores_older_snapshot():
    """A snapshot OLDER than the store's day must be refused, not treated as a
    rollover — that would reset today's state and queue live seals for restore."""
    usage, _, _, _ = _fresh_stores()
    usage.update({"date": "2026-09-29", "projects": {}, "total_normal_tokens": 3_000})
    usage.add_track_originals("normal", "proj_x", [
        {"id": "r1", "model": "gpt-4o-mini", "max_requests_per_1_minute": 5000}])
    usage.mark_mass_sealed("normal")

    applied = usage.update({"date": "2026-09-28", "projects": {},
                            "total_normal_tokens": 24_882_107})
    assert applied is False, "stale snapshot must be refused"
    assert usage._data["date"] == "2026-09-29"
    assert usage._data.get("total_normal_tokens") == 3_000, "stale totals overwrote today's"
    assert "proj_x" in usage.get_sealed_tracks()["normal"]["originals_by_project"], \
        "live seal wiped by a backward 'rollover'"
    assert usage.is_mass_sealed("normal")
    assert not usage.get_pending_track_unseal(), "today's seals queued for restore"

    assert usage.update({"date": "2026-09-30", "projects": {}}) is True, "forward rollover broken"
    assert "proj_x" in usage.get_pending_track_unseal()["normal"]["originals_by_project"]
    print("  ✅ Older snapshot refused (state intact); forward rollover still works")


def test_refresh_never_acts_on_stale_snapshot():
    """/refresh on the Telegram thread: if its fetch is pre-midnight but the poll
    loop already rolled the store over, it must refetch — not seal on 24.88M."""
    usage, subs, names, _ = _fresh_stores()
    bot._release_busy()
    usage.update({"date": "2026-09-29", "projects": {}})
    usage._data["spend_seeded"] = True
    stale = {"date": "2026-09-28", "total_cost": 0.0, "total_premium_tokens": 0,
             "total_normal_tokens": 24_882_107, "projects": {}}
    fresh = {"date": "2026-09-29", "total_cost": 0.0, "total_premium_tokens": 0,
             "total_normal_tokens": 3_000, "projects": {}}
    spawned, sent = [], []
    with mock.patch.object(bot, "fetch_today_usage", side_effect=[stale, fresh]), \
         mock.patch.object(bot, "_enrich_costs", side_effect=lambda s, *a, **k: s), \
         mock.patch.object(bot, "_fetch_recent_activity_by_band", return_value={}), \
         mock.patch.object(bot, "_spawn_bg",
                           side_effect=lambda label, fn, *a: spawned.append(label)), \
         mock.patch.object(bot, "_send", side_effect=lambda t, *a, **k: sent.append(t)):
        bot.cmd_refresh(usage, subs, names, "Bach")
    assert not [s for s in spawned if s.startswith("track-seal")], f"stale data sealed: {spawned}"
    assert not [t for t in sent if "Exhausted" in t], "stale milestone broadcast"
    assert usage._data.get("total_normal_tokens") == 3_000
    print("  ✅ /refresh refetches past a stale snapshot — no seal, no bogus milestone")


def _seal_state(usage, pids, rows=2):
    for pid in pids:
        usage.add_track_originals("normal", pid, [
            {"id": f"rl-{i}", "model": "gpt-4o-mini", "max_requests_per_1_minute": 5000,
             "max_tokens_per_1_minute": 4_000_000} for i in range(rows)])


class _ConcurrencyProbe:
    """Fake _update_project_rate_limit that records peak parallelism."""
    def __init__(self, fail_pids=(), delay=0.05):
        self.lock, self.live, self.peak, self.calls = threading.Lock(), 0, 0, []
        self.fail_pids, self.delay = set(fail_pids), delay

    def __call__(self, pid, rid, payload, **kw):
        with self.lock:
            self.live += 1; self.peak = max(self.peak, self.live); self.calls.append(pid)
        time.sleep(self.delay)
        with self.lock:
            self.live -= 1
        return pid not in self.fail_pids


def test_mass_unseal_parallel_restores_and_exempts():
    """Manual 'Unseal → All' used to restore one project at a time (10.5 min on
    2026-09-29). It must now run SEAL_SWEEP_WORKERS projects in parallel, exempt
    every restored project, and leave a failed one sealed and non-exempt."""
    usage, subs, names, _ = _fresh_stores()
    bot._release_busy()
    pids = list(bot.KNOWN_PROJECTS)
    _seal_state(usage, pids)
    bad = pids[3]
    probe = _ConcurrencyProbe(fail_pids={bad})
    with mock.patch.object(bot, "_update_project_rate_limit", side_effect=probe), \
         mock.patch.object(bot, "_compute_canonical_baseline", return_value={}), \
         mock.patch.object(bot, "_send"):
        assert bot._try_claim_busy()
        try:
            t0 = time.time()
            bot._mass_unseal_track("normal", usage, subs, names, reason="manual all")
            elapsed = time.time() - t0
        finally:
            bot._release_busy()
    left = usage.get_sealed_tracks().get("normal", {}).get("originals_by_project", {})
    assert set(left) == {bad}, f"expected only the failed project still sealed, got {set(left)}"
    assert all(usage.is_exempt(p, "normal") for p in pids if p != bad)
    assert not usage.is_exempt(bad, "normal"), "failed restore must not be exempted"
    assert 1 < probe.peak <= bot.SEAL_SWEEP_WORKERS, f"peak parallelism {probe.peak}"
    print(f"  ✅ {len(pids)-1}/{len(pids)} restored + exempt, failure stays sealed; "
          f"peak {probe.peak} workers, {elapsed:.2f}s")


def test_pending_unseal_parallel_keeps_failures_queued():
    """Midnight auto-restore: same parallel path; a failed project must stay in
    pending_track_unseal for the next poll, successes must leave the queue."""
    usage, subs, names, _ = _fresh_stores()
    bot._release_busy()
    pids = list(bot.KNOWN_PROJECTS)
    usage.update({"date": "2026-09-28", "projects": {}})
    _seal_state(usage, pids)
    usage.update({"date": "2026-09-29", "projects": {}})   # rollover → pending queue
    bad = pids[0]
    probe = _ConcurrencyProbe(fail_pids={bad})
    with mock.patch.object(bot, "_update_project_rate_limit", side_effect=probe), \
         mock.patch.object(bot, "_compute_canonical_baseline", return_value={}), \
         mock.patch.object(bot, "_send"):
        bot._process_pending_track_unseals(usage, subs, names)
    queue = usage.get_pending_track_unseal().get("normal", {}).get("originals_by_project", {})
    assert set(queue) == {bad}, f"queue should hold only the failure, got {set(queue)}"
    assert 1 < probe.peak <= bot.SEAL_SWEEP_WORKERS, f"peak parallelism {probe.peak}"
    assert bot._try_claim_busy(), "busy claim leaked after the restore"
    bot._release_busy()
    print(f"  ✅ Midnight restore parallel (peak {probe.peak}); failure stays queued for retry")


if __name__ == "__main__":
    tests = [
        ("HTML escape + length cap in setname",          test_name_html_escape),
        ("Atomic JSON write",                            test_atomic_write),
        ("Busy claim is atomic",                         test_busy_claim_atomic),
        ("Callback refuses when busy",                   test_callback_refuses_when_busy),
        ("Callback validates inputs",                    test_callback_validates_inputs),
        ("Model classification (listed + unlisted)",     test_model_classification),
        ("Same-prefix paid products are unlisted",       test_same_prefix_paid_products_are_unlisted),
        ("Overcap escalation keeps alerting",            test_overcap_escalation_keeps_alerting),
        ("Seed covers overcap steps (no restart flood)", test_seed_spend_covers_overcap_steps),
        ("DAILY_LIMIT = $2 and SPEND_MILESTONES align",  test_daily_limit_is_two_dollars),
        ("Spend milestones fire in order",               test_spend_milestones_fire_in_order),
        ("Per-project spend thresholds",                 test_per_project_spend_thresholds),
        ("Per-project alerts deduped",                   test_per_project_no_duplicate_alerts),
        ("Unlisted-model first-touch alert",             test_unlisted_model_alert_first_touch),
        ("Unlisted-model skips zero usage",              test_unlisted_model_skips_zero_usage),
        ("Unlisted-model per-(pid, model) dedup",        test_unlisted_model_per_project_dedup),
        ("Seed spend: catch-up + no re-spam",            test_seed_spend_marks_crossed_silently),
        ("Atomic claim_spend_seed (single winner)",      test_spend_seed_atomic),
        ("Day rollover resets spend tracking",           test_day_rollover_resets_spend_tracking),
        ("check_spend handles missing/None cost",        test_check_spend_handles_missing_cost),
        ("Per-track seal thresholds (wave buffers)",     test_per_track_seal_thresholds),
        ("Wave projection math",                         test_wave_projection_math),
        ("WaveGuard ignores ingestion chunk (46% fix)",  test_wave_guard_ignores_ingestion_chunk),
        ("WaveGuard utilization floor",                  test_wave_guard_floor_gate),
        ("WaveGuard fires on sustained wave",            test_wave_guard_fires_on_sustained_wave),
        ("WaveGuard streak resets on subsided wave",     test_wave_guard_streak_resets_when_wave_subsides),
        ("WaveGuard resets on day change",               test_wave_guard_resets_on_day_change),
        ("Parallel sweep covers all + retries",          test_mass_seal_parallel_covers_all_projects),
        ("Seal-gap repair (oduong leak fix)",            test_seal_gap_repair),
        ("Seal-gap repair respects busy claim",          test_seal_gap_repair_respects_busy),
        ("Quarantine seals offender only, once",         test_quarantine_seals_offender_only),
        ("Quarantine exemption / retry / noop memo",     test_quarantine_respects_exemption_and_retries_failure),
        ("Full seal: all rows, healthy captures only",   test_full_seal_project_captures_healthy_only),
        ("Release quarantine restores + exempts",        test_release_quarantine_restores_and_exempts),
        ("Quarantine rolls over at midnight",            test_quarantine_rolls_over_at_midnight),
        ("cmd_refresh never blocks Telegram thread",     test_cmd_refresh_never_blocks_telegram_thread),
        ("Model names HTML-escaped in alerts",           test_model_names_escaped_in_alerts),
        ("Quarantine + track seal, no 0/0 cascade",      test_quarantine_and_track_seal_coexist_without_cascade),
        ("Org-ceiling clamp on restore",                 test_org_ceiling_clamp_on_restore),
        ("Repair detects seal drift",                    test_repair_detects_seal_drift),
        ("Line item -> lane mapping",                    test_lane_for_line_item),
        ("Lane costs reconcile with total",              test_lane_costs_reconcile_with_total),
        ("_fmt_cost never hides real spend",             test_fmt_cost_never_hides_real_spend),
        ("Lane lines render in every report",            test_lane_lines_in_every_report),
        ("Network errors retried",                       test_network_errors_are_retried),
        ("Seal survives a DNS blip",                     test_seal_survives_single_dns_blip),
        ("Intel log captures broadcasts",                test_intel_log_captures_broadcasts),
        ("Intel log failure never breaks bot",           test_intel_log_failure_never_breaks_bot),
        ("Mode change logged once per transition",       test_mode_change_logged_once),
        ("Midnight-straddle fetch labels correct day",   test_midnight_straddle_fetch_labels_correct_day),
        ("Store ignores older snapshot",                 test_store_ignores_older_snapshot),
        ("/refresh never acts on stale snapshot",        test_refresh_never_acts_on_stale_snapshot),
        ("Mass unseal parallel + exempts",               test_mass_unseal_parallel_restores_and_exempts),
        ("Pending unseal parallel, failures queued",     test_pending_unseal_parallel_keeps_failures_queued),
    ]
    passes, fails = 0, []
    for name, fn in tests:
        print(f"\n[{name}]")
        try:
            fn()
            passes += 1
        except AssertionError as e:
            fails.append((name, str(e)))
            print(f"  ❌ {e}")
        except Exception as e:
            fails.append((name, f"{type(e).__name__}: {e}"))
            print(f"  💥 {type(e).__name__}: {e}")
            import traceback; traceback.print_exc()

    print(f"\n{'='*70}")
    print(f"RESULT: {passes}/{len(tests)} passed")
    if fails:
        for n, e in fails:
            print(f"  ❌ {n}: {e}")
        sys.exit(1)
    print("✅ ALL GREEN")
