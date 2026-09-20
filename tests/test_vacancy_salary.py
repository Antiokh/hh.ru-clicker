"""Threshold comparisons must not silently mix currencies or salary units."""

from copy import deepcopy

import pytest

from app.vacancy_salary import salary_for_ruble_threshold


def salary_range(**overrides):
    return {"currency": "RUR", "mode": {"id": "MONTH"},
            "from": 100_000, "to": 150_000, "gross": True, **overrides}


@pytest.mark.parametrize("currency", ["RUR", "RUB"])
@pytest.mark.parametrize("mode", ["MONTH", {"id": "MONTH"}])
def test_monthly_ruble_range(currency, mode):
    result = salary_for_ruble_threshold({
        "salary_range": salary_range(currency=currency, mode=mode)})
    assert result == 100_000.0
    assert isinstance(result, float)


@pytest.mark.parametrize("mode", ["HOUR", "SHIFT", "FLY_IN_FLY_OUT", "SERVICE",
                                  "YEAR", None, "", {}, {"id": "HOUR"}, []])
def test_nonmonthly_or_unknown_range_does_not_fallback(mode):
    assert salary_for_ruble_threshold({
        "salary_range": salary_range(mode=mode),
        "salary": {"from": 200_000, "currency": "RUR"},
    }) is None


@pytest.mark.parametrize("currency", ["USD", "EUR", "KZT", None, "", "rur", {}, []])
def test_non_ruble_or_unknown_currency(currency):
    assert salary_for_ruble_threshold({
        "salary_range": salary_range(currency=currency)}) is None


@pytest.mark.parametrize("value", [True, False, "100000", -1, float("nan"),
                                   float("inf"), float("-inf"), {}, [], 10 ** 400])
def test_invalid_lower_bound_does_not_use_upper_bound(value):
    assert salary_for_ruble_threshold({
        "salary_range": salary_range(**{"from": value})}) is None


def test_zero_is_a_real_lower_bound():
    assert salary_for_ruble_threshold({
        "salary_range": salary_range(**{"from": 0})}) == 0.0


def test_fractional_amount_and_null_lower_bound():
    assert salary_for_ruble_threshold({
        "salary_range": salary_range(**{"from": None, "to": 123_456.75})
    }) == 123_456.75


def test_missing_lower_bound_uses_upper_bound():
    value = salary_range()
    del value["from"]
    assert salary_for_ruble_threshold({"salary_range": value}) == 150_000.0


@pytest.mark.parametrize("value", [None, -1, True, "10", float("inf")])
def test_invalid_upper_bound_when_lower_absent(value):
    assert salary_for_ruble_threshold({
        "salary_range": salary_range(**{"from": None, "to": value})
    }) is None


def test_range_is_preferred_over_legacy_salary():
    assert salary_for_ruble_threshold({
        "salary_range": salary_range(),
        "salary": {"from": 300_000, "currency": "RUR"},
    }) == 100_000.0


@pytest.mark.parametrize("value", [{}, {"currency": "RUR", "from": 100_000}])
def test_incomplete_range_does_not_fallback(value):
    assert salary_for_ruble_threshold({
        "salary_range": value,
        "salary": {"from": 300_000, "currency": "RUR"},
    }) is None


@pytest.mark.parametrize("value", [None, [], "bad", 100, True])
def test_nonmapping_range_allows_legacy_fallback(value):
    assert salary_for_ruble_threshold({
        "salary_range": value,
        "salary": {"from": 300_000, "currency": "RUB"},
    }) == 300_000.0


def test_legacy_without_range_or_mode():
    assert salary_for_ruble_threshold({
        "salary": {"to": 150_000, "currency": "RUR"},
    }) == 150_000.0


@pytest.mark.parametrize("mode", ["SHIFT", {"id": "HOUR"}, None, {}])
def test_explicit_legacy_unknown_or_nonmonthly_mode_rejected(mode):
    assert salary_for_ruble_threshold({
        "salary": {"from": 150_000, "currency": "RUR", "mode": mode},
    }) is None


@pytest.mark.parametrize("item", [None, [], "bad", {}, {"salary": []},
                                  {"salary": {"from": 100_000}}])
def test_missing_or_malformed_salary(item):
    assert salary_for_ruble_threshold(item) is None


@pytest.mark.parametrize("gross", [True, False, None])
def test_frequency_and_gross_do_not_change_amount_or_input(gross):
    item = {"salary_range": salary_range(
        gross=gross, frequency={"id": "TWICE_PER_MONTH"})}
    before = deepcopy(item)
    assert salary_for_ruble_threshold(item) == 100_000.0
    assert item == before
