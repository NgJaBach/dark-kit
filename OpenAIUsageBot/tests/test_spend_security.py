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
    premium = ["gpt-5.4", "gpt-5.2", "gpt-5.1", "gpt-5.1-codex", "gpt-5",
               "gpt-5-codex", "gpt-5-chat-latest", "gpt-4.1", "gpt-4o", "o1", "o3"]
    normal = ["gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.1-codex-mini",
              "gpt-5-mini", "gpt-5-nano", "gpt-4.1-mini", "gpt-4.1-nano",
              "gpt-4o-mini", "o1-mini", "o3-mini", "o4-mini", "codex-mini-latest"]
    for m in premium: assert bot._track_for_model(m) == "premium", m
    for m in normal:  assert bot._track_for_model(m) == "normal",  m
    # Unlisted models — the spend-anomaly target class
    for m in ["sora-2", "dall-e-3", "gpt-3.5-turbo", "text-embedding-3-small",
              "text-embedding-3-large", "whisper-1", "tts-1"]:
        assert bot._track_for_model(m) is None, m
    # Date-stamped snapshots inherit the base model's track
    assert bot._track_for_model("gpt-4o-mini-2024-07-18") == "normal"
    assert bot._track_for_model("gpt-4o-2024-08-06")      == "premium"
    print("  ✅ Listed-model + unlisted-model classification correct")


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
        # brand-new families that share no boundary — must also be None
        "gpt-5.5", "gpt-5.5-pro", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
        "gpt-5.3-codex", "gpt-5.3-chat-latest", "chat-latest",
        "gpt-image-2", "gpt-realtime-2.1", "gpt-audio-1.5",
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
    assert bot.PREMIUM_TRACK_SEAL_THRESHOLD ==   850_000, bot.PREMIUM_TRACK_SEAL_THRESHOLD
    print("  ✅ Premium seals at 850k (150k buffer), normal at 9.5M (500k buffer)")


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
        "proj_OWrxxJaWk5MXHBi3HIdPxBDh": {   # oduong — used gpt-5.6-sol
            "cost_usd": 0.5,
            "models": {"gpt-5.6-sol": {"input": 9000, "output": 2000, "requests": 4},
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
        u.add_track_originals(bot.QUARANTINE_TRACK, pid, [{"id": "r1", "model": "gpt-5.6-sol"}])
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
    usage.add_track_originals("full", "proj_X", [{"id": "r1", "model": "gpt-5.6-sol"}])
    usage._data["date"] = "2026-01-01"
    usage.update({"date": "2026-01-02", "projects": {}})
    assert not usage.is_project_track_sealed("proj_X", "full")
    pending = usage.get_pending_track_unseal()
    assert "proj_X" in pending.get("full", {}).get("originals_by_project", {}), \
        "quarantine originals must queue for midnight restore"
    print("  ✅ Quarantine flows through the standard midnight restore queue")


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
        ("Intel log captures broadcasts",                test_intel_log_captures_broadcasts),
        ("Intel log failure never breaks bot",           test_intel_log_failure_never_breaks_bot),
        ("Mode change logged once per transition",       test_mode_change_logged_once),
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
