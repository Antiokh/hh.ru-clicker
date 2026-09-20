"""Pure per-cycle accounting: no storage, requests, or runtime startup."""
from datetime import datetime, timedelta, timezone
from threading import Lock
from types import SimpleNamespace

import pytest

from app.cycle_report import (begin_cycle, found_cycle, consider_cycle,
    cycle_outcome, questionnaire_cycle, cycle_operation_error, finish_cycle,
    resolve_cycle_unknown, cycle_snapshot)


def state_with(*ids):
    state = SimpleNamespace(_state_lock=Lock(), sent=900, daily_sent=80)
    begin_cycle(state)
    found_cycle(state, ids, len(ids))
    return state


def test_absent_before_real_cycle_and_restart():
    assert cycle_snapshot(SimpleNamespace()) is None
    state = state_with("a")
    cycle_outcome(state, "a", "sent")
    assert cycle_snapshot(SimpleNamespace(sent=901)) is None


def test_collect_counts_unknown_until_universe_available():
    state = SimpleNamespace()
    begin_cycle(state)
    report = cycle_snapshot(state)
    assert report["found_raw"] is report["found_unique"] is report["remaining"] is None
    assert report["partial"] and report["sent"] == 0


def test_unique_terminal_partition_and_already_subset():
    state = state_with("a", "b", "c", "d", "e", "f", "g")
    for vid, outcome, reason in [("a", "sent", None), ("b", "already", None),
            ("c", "skipped", "salary"), ("d", "error", None), ("e", "unknown", None)]:
        cycle_outcome(state, vid, outcome, reason)
    questionnaire_cycle(state, "f")
    report = cycle_snapshot(state)
    assert report["processed"] == 5
    assert report["processed"] == sum(report[k] for k in ("sent", "skipped", "errors", "unknown"))
    assert report["already"] == 1 and report["skipped"] == 2
    assert report["questionnaires_pending"] == 1 and report["remaining"] == 2
    assert report["considered"] == 6 and report["partial"]
    assert sum(r["count"] for r in report["skip_reasons"]) == report["skipped"]


@pytest.mark.parametrize("weaker", ["sent", "already", "error", "unknown", "skipped"])
def test_sent_receipt_sticky_against_late_duplicate(weaker):
    state = state_with("a")
    cycle_outcome(state, "a", "sent")
    cycle_outcome(state, "a", weaker)
    report = cycle_snapshot(state)
    assert report["sent"] == report["processed"] == 1
    assert report["already"] == report["errors"] == report["unknown"] == 0


def test_questionnaire_intermediate_then_receipt_only_one_outcome():
    state = state_with("a")
    questionnaire_cycle(state, "a")
    assert cycle_snapshot(state)["processed"] == 0
    cycle_outcome(state, "a", "sent")
    report = cycle_snapshot(state)
    assert report["questionnaires_pending"] == 0 and report["processed"] == 1


@pytest.mark.parametrize("stamp_kind,expected", [("current", "sent"), ("old", "already"),
    ("missing", "already"), ("naive", "already"), ("future", "already")])
def test_unknown_resolution_uses_receipt_time_not_attempt_time(stamp_kind, expected):
    now = datetime.now(timezone.utc)
    state = SimpleNamespace(_state_lock=Lock())
    begin_cycle(state, now=now - timedelta(minutes=1))
    found_cycle(state, ["a"], 1)
    cycle_outcome(state, "a", "unknown")
    finish_cycle(state, "blocked")
    stamp = {"current": now.isoformat(), "old": (now - timedelta(days=1)).isoformat(),
        "missing": "", "naive": now.replace(tzinfo=None).isoformat(),
        "future": (now + timedelta(days=1)).isoformat()}[stamp_kind]
    resolve_cycle_unknown(state, "a", stamp)
    report = cycle_snapshot(state)
    assert report[expected] == 1 and report["unknown"] == 0 and report["processed"] == 1


def test_external_manual_outcome_not_in_cycle():
    state = state_with("a")
    cycle_outcome(state, "external", "sent")
    resolve_cycle_unknown(state, "external", datetime.now(timezone.utc).isoformat())
    assert cycle_snapshot(state)["processed"] == 0


def test_wait_retains_snapshot_next_cycle_resets_without_cumulative_inference():
    state = state_with("a")
    cycle_outcome(state, "a", "already")
    finish_cycle(state)
    first = cycle_snapshot(state)
    finish_cycle(state, "error")
    assert cycle_snapshot(state) == first
    begin_cycle(state)
    second = cycle_snapshot(state)
    assert second["cycle_id"] != first["cycle_id"] and second["sent"] == 0
    assert second["finished_at"] is None and second["processed"] == 0
    assert state.sent == 900 and state.daily_sent == 80


def test_operation_errors_partial_not_application_errors_and_snapshot_copy():
    state = state_with()
    cycle_operation_error(state)
    finish_cycle(state)
    report = cycle_snapshot(state)
    assert report["operation_errors"] == 1 and report["errors"] == 0 and report["partial"]
    report["status"] = "changed"
    assert cycle_snapshot(state)["status"] == "waiting"


def test_raw_duplicates_and_unknown_source_count_are_explicit():
    state = state_with("a", "b", "b")
    report = cycle_snapshot(state)
    assert report["found_raw"] == 3 and report["found_unique"] == 2
    found_cycle(state, ["a", "b"], None)
    assert cycle_snapshot(state)["found_raw"] is None


def test_blocked_view_does_not_mutate_cycle_lifecycle_or_timestamp():
    state = state_with("a")
    started = cycle_snapshot(state)["started_at"]
    consider_cycle(state, "a")
    assert cycle_snapshot(state, blocked=True)["status"] == "blocked"
    assert cycle_snapshot(state)["status"] == "running"
    assert cycle_snapshot(state)["started_at"] == started
