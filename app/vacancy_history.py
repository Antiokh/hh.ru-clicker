"""Local, account-independent observation history; no resume or contact data."""
import json
import threading
from datetime import datetime, timezone

from app.storage import DATA_DIR, _atomic_write_json

_lock = threading.Lock()
HISTORY_FILE = DATA_DIR / "vacancy_observations.json"


def timestamp(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except (ValueError, TypeError):
        return None


def observe(vacancies, now=None):
    """Persist only IDs and dates. Never treat missing/corrupt history as proof of novelty."""
    now = now or datetime.now(timezone.utc)
    with _lock:
        try:
            records = json.loads(HISTORY_FILE.read_text())
            if not isinstance(records, dict):
                raise ValueError("invalid history")
            if any(not isinstance(v, dict) for v in records.values()):
                raise ValueError("invalid history record")
        except FileNotFoundError:
            records = {}
        except (ValueError, OSError):
            for meta in vacancies.values():
                meta["observation_unavailable"] = True
            return
        for vid, meta in vacancies.items():
            if not str(vid).isdigit() or not isinstance(meta, dict):
                continue
            old = records.get(str(vid), {})
            old = old if isinstance(old, dict) else {}
            published = timestamp(meta.get("published_at") or meta.get("created_at"))
            previous = timestamp(old.get("earliest_published_at"))
            first = timestamp(old.get("first_observed_at")) or now
            earliest = min(x for x in (previous, published) if x) if previous or published else None
            entry = {
                "first_observed_at": first.isoformat(),
                "last_observed_at": now.isoformat(),
                "earliest_published_at": earliest.isoformat() if earliest else None,
                "publication_updated": bool(old.get("publication_updated")) or bool(previous and published and published > previous),
            }
            records[str(vid)] = entry
            meta.update(entry)
        # Bounded storage; forgotten IDs are never labelled as proven new.
        if len(records) > 50000:
            records = dict(sorted(records.items(), key=lambda x: str(x[1].get("last_observed_at", "")))[-50000:])
        try:
            _atomic_write_json(HISTORY_FILE, records)
        except OSError:
            for meta in vacancies.values():
                meta["observation_unavailable"] = True
