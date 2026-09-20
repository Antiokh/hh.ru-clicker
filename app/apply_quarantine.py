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
    if not isinstance(data, dict):
        raise ValueError('Invalid quarantine')
    return data


def blocked(acc, vacancy_id):
    with _lock:
        try:
            data = _read()
            return bool(data) and str(vacancy_id) in data.get(_key(acc), {})
        except Exception:
            return True  # Corrupt/unreadable storage must never enable a retry.


def retain(acc, pending):
    with _lock:
        data = _read()
        data.setdefault(_key(acc), {})[str(pending['vacancy_id'])] = dict(pending)
        _atomic_write_json(PATH, data)
