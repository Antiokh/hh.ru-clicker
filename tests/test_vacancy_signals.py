from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from app.vacancy_signals import (
    manager_is_online,
    normalize_vacancy_signals,
    vacancy_signal_priority,
)


NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


def _skills(value):
    return {"skills_match": {"match_by_skills_statistics": {"match_percentage": value}}}


def test_normalization_extracts_only_safe_fields_and_does_not_mutate():
    item = {
        "manager_activity": {
            "last_activity_at": "2026-09-05T14:59:00+0300",
            "is_online_until": "2026-09-05T15:01:00+03:00",
            "name": "must not be copied",
        },
        "relations": ["got_response", "unknown", "favorited", "got_response", {}, None],
        **_skills(87.5),
        "other": {"nested": "untouched"},
    }
    original = deepcopy(item)
    assert normalize_vacancy_signals(item) == {
        "manager_activity": {
            "last_activity_at": "2026-09-05T11:59:00+00:00",
            "is_online_until": "2026-09-05T12:01:00+00:00",
        },
        "relations": ["got_response", "favorited"],
        "skills_match_percent": 87.5,
    }
    assert item == original


@pytest.mark.parametrize("item", [None, [], "text", True, 123, {}])
def test_missing_or_malformed_vacancy_has_no_signals(item):
    assert normalize_vacancy_signals(item) == {"manager_activity": {}, "relations": []}
    assert manager_is_online(item, now=NOW) is False
    assert vacancy_signal_priority(item, now=NOW) == (1, 1, 0)


@pytest.mark.parametrize("value", [
    None, True, False, "90", {}, [], -1, 101, float("nan"),
    float("inf"), -float("inf"), 10 ** 500,
])
def test_invalid_percentages_are_unknown(value):
    assert "skills_match_percent" not in normalize_vacancy_signals(_skills(value))
    assert vacancy_signal_priority({"skills_match_percent": value}, now=NOW) == (1, 1, 0)


@pytest.mark.parametrize("value", [0, 0.5, 99, 100, 100.0])
def test_valid_percentage_including_zero_is_known(value):
    meta = normalize_vacancy_signals(_skills(value))
    assert meta["skills_match_percent"] == value
    assert vacancy_signal_priority(meta, now=NOW) == (1, 0, -value)


@pytest.mark.parametrize("skills", [None, [], "bad", {"match_by_skills_statistics": []}])
def test_malformed_nested_skills_are_ignored(skills):
    assert "skills_match_percent" not in normalize_vacancy_signals({"skills_match": skills})


@pytest.mark.parametrize("timestamp", [
    None, True, 1788609600, {}, [], "", "2026-09-05", "2026-09-05T12:01:00",
    "tomorrow", "2026-02-30T12:01:00Z", "2026-09-05X12:01:00Z",
    "2026-09-05T12:01:00+99:00", "2026-09-05T12:01:00Z trailing",
    "2026-09-05T12:01:00+00:99",
    "0001-01-01T00:00:00+01:00", "9" * 1000,
])
def test_invalid_or_naive_timestamps_are_never_online(timestamp):
    item = {"manager_activity": {"is_online_until": timestamp, "last_activity_at": timestamp}}
    assert normalize_vacancy_signals(item)["manager_activity"] == {}
    assert manager_is_online(item, now=NOW) is False


@pytest.mark.parametrize("timestamp, expected", [
    ("2026-09-05T12:00:01Z", True),
    ("2026-09-05T15:00:01+0300", True),
    ("2026-09-05T08:00:01-04:00", True),
    ("2026-09-05T12:00:00.000001Z", True),
    ("2026-09-05T12:00:00Z", False),
    ("2026-09-05T15:00:00+03:00", False),
    ("2026-09-05T11:59:59Z", False),
])
def test_online_requires_strict_future_expiry(timestamp, expected):
    meta = {"manager_activity": {"is_online_until": timestamp}}
    assert manager_is_online(meta, now=NOW) is expected
    assert manager_is_online(normalize_vacancy_signals(meta), now=NOW) is expected


def test_last_activity_alone_never_implies_online_even_when_in_future():
    meta = {"manager_activity": {"last_activity_at": "2099-01-01T00:00:00Z"}}
    assert manager_is_online(meta, now=NOW) is False


@pytest.mark.parametrize("now", [datetime(2026, 9, 5, 12), "2026-09-05T12:00:00Z", False])
def test_bad_or_naive_clock_never_reports_online(now):
    assert manager_is_online({"manager_activity": {"is_online_until": "2099-01-01T00:00:00Z"}}, now=now) is False


def test_timezone_aware_clock_is_compared_as_an_instant():
    local_now = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))
    meta = {"manager_activity": {"is_online_until": "2026-09-05T12:00:01Z"}}
    assert manager_is_online(meta, now=local_now) is True


def test_default_clock_and_expiry_are_evaluated_each_time():
    assert manager_is_online({"manager_activity": {"is_online_until": "9998-01-01T00:00:00Z"}})
    meta = {"manager_activity": {"is_online_until": "2026-09-05T12:00:01Z"}}
    assert manager_is_online(meta, now=NOW)
    assert not manager_is_online(meta, now=NOW + timedelta(seconds=1))


@pytest.mark.parametrize("relations", [None, "favorited", {"favorited": True}, ("favorited",)])
def test_relations_must_be_a_list(relations):
    assert normalize_vacancy_signals({"relations": relations})["relations"] == []


def test_known_relations_are_retained_without_case_coercion_or_hard_skips():
    relations = ["blacklisted", "got_response", "favorited", "got_invitation", "got_rejection"]
    result = normalize_vacancy_signals({"relations": relations + ["GOT_RESPONSE", "unknown"]})
    assert result["relations"] == relations
    assert vacancy_signal_priority(result, now=NOW) == vacancy_signal_priority({}, now=NOW)


def test_priority_online_first_then_known_score_descending_with_stable_ties():
    jobs = [
        {"id": "unknown"},
        {"id": "zero", "skills_match_percent": 0},
        {"id": "high", "skills_match_percent": 99},
        {"id": "online_unknown", "manager_activity": {"is_online_until": "2026-09-05T12:01:00Z"}},
        {"id": "online_low", "skills_match_percent": 1, "manager_activity": {"is_online_until": "2026-09-05T12:01:00Z"}},
        {"id": "tied", "skills_match_percent": 99},
    ]
    before = deepcopy(jobs)
    ordered = sorted(jobs, key=lambda meta: vacancy_signal_priority(meta, now=NOW))
    assert [job["id"] for job in ordered] == ["online_low", "online_unknown", "high", "tied", "zero", "unknown"]
    assert jobs == before
