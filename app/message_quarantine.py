"""Persistent chat exclusions after an ambiguous message write; no message text."""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from app.storage import _atomic_write_json

PATH = Path('data/message_quarantine.json')
_lock = threading.RLock()


def _read():
    value = json.loads(PATH.read_text()) if PATH.exists() else {}
    if not isinstance(value, dict):
        raise ValueError('Invalid chat quarantine')
    return value


def _key(acc):
    value = acc.get('user_id') or acc.get('resume_hash')
    if not value:
        raise ValueError('Missing account identity')
    return str(value)


def blocked(acc, chat_id):
    with _lock:
        try:
            data = _read()
            return bool(data) and str(chat_id) in data.get(_key(acc), {})
        except Exception:
            return True


def retain(acc, chat_id):
    if not chat_id:
        raise ValueError('Missing chat identity')
    with _lock:
        data = _read()
        data.setdefault(_key(acc), {}).setdefault(str(chat_id), {
            'reason': 'message_outcome_unknown',
            'recorded_at': datetime.now(timezone.utc).isoformat(),
        })
        _atomic_write_json(PATH, data)
