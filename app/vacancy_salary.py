"""Pure helpers for comparing published HH salaries with a ruble threshold."""

from collections.abc import Mapping
from math import isfinite


def salary_for_ruble_threshold(item: Mapping) -> float | None:
    """Return a comparable nominal monthly published amount, or ``None``.

    A ``salary_range`` mapping is authoritative, including when incomplete:
    falling back to legacy ``salary`` could conceal non-monthly units. Only
    RUR/RUB and an explicit MONTH mode are accepted for the new representation.
    Legacy ``salary`` may omit mode, as its historical amount is monthly.

    Prefer the lower bound; use the upper bound only if the lower is absent or
    null, preserving the existing threshold policy (not a guaranteed minimum).
    Invalid explicit bounds, other currencies and unknown units are unknown,
    not zero. Payment frequency is distinct from the amount's time unit.

    No currency or tax conversion is performed: gross remains gross and net
    remains net. This function does not mutate the input or its gross flag.
    """
    if not isinstance(item, Mapping):
        return None

    salary = item.get("salary_range")
    legacy = not isinstance(salary, Mapping)
    if legacy:
        salary = item.get("salary")
    if not isinstance(salary, Mapping):
        return None

    if salary.get("currency") not in ("RUR", "RUB"):
        return None

    mode = salary.get("mode")
    if isinstance(mode, Mapping):
        mode = mode.get("id")
    if mode != "MONTH" and not (legacy and "mode" not in salary):
        return None

    amount = salary.get("from")
    if amount is None:
        amount = salary.get("to")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    try:
        amount = float(amount)
    except (OverflowError, ValueError):
        return None
    return amount if isfinite(amount) and amount >= 0 else None
