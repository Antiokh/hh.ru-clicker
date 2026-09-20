"""Mobile daily quota is unknown: responses_streak measures an achievement.

A missing or completed streak never grants permission to resume applications.
Confirmed application counts, explicit limit responses and daily rollover are
handled by the manager. Do not spend a request on an unrelated streak endpoint.
"""


def check_limit(acc: dict) -> dict:
    return {"applied_today": None, "limit": None, "can_apply": None}
