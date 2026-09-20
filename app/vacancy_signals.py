"""Pure, optional vacancy signals; unknown values never imply availability.

Manager presence is an expiring HH hint, not proof of active hiring. Relations
are descriptive only: an existing response may belong to another resume.
"""

import math
import re
from collections.abc import Mapping
from datetime import datetime, timezone


_RELATIONS = frozenset({
    "blacklisted", "got_response", "favorited", "got_invitation", "got_rejection",
})
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?"
    r"(?:[Zz]|[+-](?:[01]\d|2[0-3]):?[0-5]\d)"
)


def _mapping(value) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _timestamp(value) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    if not _TIMESTAMP.fullmatch(value):
        return None
    try:
        # Python 3.10 needs a colon in HH's common +0300 timezone suffix.
        value = value.replace("t", "T").replace(",", ".")
        if re.search(r"[+-]\d{4}$", value):
            value = value[:-2] + ":" + value[-2:]
        parsed = datetime.fromisoformat(value.replace("z", "Z").replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _percentage(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value <= 100 or not math.isfinite(value):
        return None
    return float(value)


def normalize_vacancy_signals(item) -> dict:
    """Extract safe JSON-ready fields without modifying the source vacancy.

    Always returns ``manager_activity`` and ``relations``; the optional
    ``skills_match_percent`` is omitted when HH supplied no valid percentage.
    Timestamps are canonical UTC ISO strings. Unknown relation names are dropped.
    """
    item = _mapping(item)
    manager = _mapping(item.get("manager_activity"))
    activity = {}
    for field in ("last_activity_at", "is_online_until"):
        parsed = _timestamp(manager.get(field))
        if parsed is not None:
            activity[field] = parsed.isoformat()

    relations = item.get("relations")
    safe_relations = []
    if isinstance(relations, list):
        for relation in relations:
            if (isinstance(relation, str) and relation in _RELATIONS
                    and relation not in safe_relations):
                safe_relations.append(relation)

    result = {"manager_activity": activity, "relations": safe_relations}
    skills = _mapping(item.get("skills_match"))
    statistics = _mapping(skills.get("match_by_skills_statistics"))
    percent = _percentage(statistics.get("match_percentage"))
    if percent is not None:
        result["skills_match_percent"] = percent
    return result


def manager_is_online(meta, now: datetime | None = None) -> bool:
    """True only for a valid, strictly future ``is_online_until`` timestamp.

    ``now`` must be timezone-aware when supplied; naive or malformed clocks
    conservatively return False. ``last_activity_at`` never implies online.
    """
    activity = _mapping(_mapping(meta).get("manager_activity"))
    expiry = _timestamp(activity.get("is_online_until"))
    if expiry is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime):
        return False
    try:
        if now.tzinfo is None or now.utcoffset() is None:
            return False
        return expiry > now.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return False


def vacancy_signal_priority(meta, now: datetime | None = None) -> tuple:
    """Ascending sort key: online first, then known skills scores descending.

    Pass one shared ``now`` while sorting to make expiry decisions consistent.
    Equal keys preserve Python's stable input order; no relation removes a job.
    """
    percent = _percentage(_mapping(meta).get("skills_match_percent"))
    return (
        0 if manager_is_online(meta, now=now) else 1,
        1 if percent is None else 0,
        0 if percent is None else -percent,
    )
