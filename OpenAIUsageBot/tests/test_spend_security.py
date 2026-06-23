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
    # Dated variants
    assert bot._track_for_model("gpt-4o-mini-2024-07-18") == "normal"
    assert bot._track_for_model("gpt-4o-2024-08-06")      == "premium"
    print("  ✅ Listed-model + unlisted-model classification correct")


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


# ─── Run ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        ("HTML escape + length cap in setname",          test_name_html_escape),
        ("Atomic JSON write",                            test_atomic_write),
        ("Busy claim is atomic",                         test_busy_claim_atomic),
        ("Callback refuses when busy",                   test_callback_refuses_when_busy),
        ("Callback validates inputs",                    test_callback_validates_inputs),
        ("Model classification (listed + unlisted)",     test_model_classification),
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
