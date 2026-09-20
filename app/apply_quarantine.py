"""Durable per-account exclusions for applications with unknown outcomes."""
import json
import threading
from pathlib import Path

from app.storage import _atomic_write_json

PATH = Path('data/apply_quarantine.json')
_lock = threading.RLock()


def _key(acc):
    identity = acc.get('user_id') or acc.get('resume_hash')
    if not identity:
        raise ValueError('Account identity required')
    return str(identity)


def _read():
    data = json.loads(PATH.read_text()) if PATH.exists() else {}
    if not isinstance(data, dict) or any(not isinstance(bucket, dict) or
            any(not isinstance(record, dict) for record in bucket.values())
            for bucket in data.values()):
        raise ValueError('Invalid quarantine')
    return data


def blocked(acc, vacancy_id):
    with _lock:
        try:
            data = _bound_read(acc)
            if data and not (acc.get('user_id') or acc.get('resume_hash')):
                return True
            return any(str(vacancy_id) in data.get(str(key), {})
                       for key in (acc.get('user_id'), acc.get('resume_hash')) if key)
        except Exception:
            return True  # Corrupt/unreadable storage must never enable a retry.


def retain(acc, pending):
    with _lock:
        data = _bound_read(acc)
        data.setdefault(_key(acc), {})[str(pending['vacancy_id'])] = dict(pending)
        _atomic_write_json(PATH, data)


def _bound_read(acc):
    data = _read()
    user, resume = acc.get('user_id'), acc.get('resume_hash')
    if user and resume and str(user) != str(resume) and str(resume) in data:
        bucket = data.setdefault(str(user), {})
        missing = {vid: record for vid, record in data[str(resume)].items() if vid not in bucket}
        if missing:
            bucket.update(missing)
            _atomic_write_json(PATH, data)
    return data


def bind_account(acc):
    with _lock:
        _bound_read(acc)
