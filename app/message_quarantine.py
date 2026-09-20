"""Persistent chat exclusions after an ambiguous message write; no message text."""
import json
import threading
import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from app.storage import _atomic_write_json

PATH = Path('data/message_quarantine.json')
_lock = threading.RLock()
_active = ContextVar('message_attempt', default=None)
_inflight = set()


def _read():
    value = json.loads(PATH.read_text()) if PATH.exists() else {}
    if not isinstance(value, dict) or any(
        not isinstance(bucket, dict) or any(not isinstance(record, dict) for record in bucket.values())
        for bucket in value.values()
    ):
        raise ValueError('Invalid chat quarantine')
    for bucket in value.values():
        for record in bucket.values():
            if record.get('reason') == 'confirmed' and (
                not isinstance(record.get('confirmed'), list) or
                any(not isinstance(f, str) for f in record['confirmed'])
            ):
                raise ValueError('Invalid receipt history')
    return value


def _key(acc):
    value = acc.get('user_id') or acc.get('resume_hash')
    if not value:
        raise ValueError('Missing account identity')
    return str(value)


def _keys(acc):
    return {str(v) for v in (acc.get('user_id'), acc.get('resume_hash')) if v}


def _bound_read(acc):
    data = _read()
    user, resume = acc.get('user_id'), acc.get('resume_hash')
    if user and resume and str(user) != str(resume) and str(resume) in data:
        bucket = data.setdefault(str(user), {})
        changed = False
        for chat, record in data[str(resume)].items():
            existing = bucket.get(chat)
            if existing is None:
                merged = dict(record)
            else:
                merged = dict(record if existing.get('reason') == 'confirmed' and
                              record.get('reason') != 'confirmed' else existing)
                merged['confirmed'] = sorted(set(existing.get('confirmed', []) + record.get('confirmed', [])))
            if existing != merged:
                bucket[chat] = merged
                changed = True
        if changed:
            _atomic_write_json(PATH, data)
    return data


def bind_account(acc):
    with _lock:
        _bound_read(acc)


def records(acc):
    with _lock:
        data = _bound_read(acc)
        result = {}
        for key in _keys(acc):
            for chat, record in data.get(key, {}).items():
                if chat not in result or record.get('reason') != 'confirmed':
                    result[chat] = dict(record)
        return result


def blocked(acc, chat_id):
    with _lock:
        try:
            data = _bound_read(acc)
            if data and not _keys(acc):
                return True
            return any(str(chat_id) in data.get(key, {}) and
                       data[key][str(chat_id)].get('reason') != 'confirmed'
                       for key in _keys(acc))
        except Exception:
            return True


def retain(acc, chat_id):
    if not chat_id:
        raise ValueError('Missing chat identity')
    with _lock:
        data = _bound_read(acc)
        data.setdefault(_key(acc), {}).setdefault(str(chat_id), {
            'reason': 'message_outcome_unknown',
            'recorded_at': datetime.now(timezone.utc).isoformat(),
        })
        _atomic_write_json(PATH, data)


@contextmanager
def attempt(acc, chat_id, operation, args, kwargs):
    """Write ahead of dispatch; nested fallback clients share one reservation."""
    from app.mutation_safety import MutationBlocked
    identity = (_key(acc), str(chat_id))
    if _active.get() == identity:
        yield {}
        return
    trigger = acc.get('_message_trigger_id')
    payload = ['reply_to', str(trigger)] if trigger else [operation, args, kwargs]
    fingerprint = hashlib.sha256(json.dumps(payload,
        sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()
    with _lock:
        data = _bound_read(acc)
        previous = [data.get(key, {}).get(str(chat_id), {}) for key in _keys(acc)]
        if any(r and (r.get('reason') != 'confirmed' or fingerprint in r.get('confirmed', [])) for r in previous):
            raise MutationBlocked('Чат или эта отправка уже заблокированы от повтора')
        confirmed = list({f for r in previous for f in r.get('confirmed', [])})
        record = {'reason': 'message_outcome_unknown', 'fingerprint': fingerprint, 'has_trigger': bool(trigger),
                  'confirmed': confirmed, 'recorded_at': datetime.now(timezone.utc).isoformat()}
        data.setdefault(_key(acc), {})[str(chat_id)] = record
        _atomic_write_json(PATH, data)
        _inflight.add(identity)
    marker = _active.set(identity)
    outcome = {}
    try:
        yield outcome
    except BaseException as exc:
        # An explicit rejection is not an ambiguous write. Preserve older
        # receipts but release this reservation (no automatic retry here).
        from app.hh_mobile_transport import MobileAPIError
        if isinstance(exc, MutationBlocked) or (isinstance(exc, MobileAPIError) and 400 <= exc.status_code < 500 and not exc.outcome_unknown):
            with _lock:
                data = _read()
                record.update(reason='confirmed', confirmed=confirmed)
                for key in _keys(acc):
                    if str(chat_id) in data.get(key, {}):
                        data[key][str(chat_id)] = record
                _atomic_write_json(PATH, data)
        raise  # Ambiguity/crash leaves the durable reservation in place.
    else:
        with _lock:
            data = _read()
            record.update(reason='confirmed', confirmed=confirmed + ([fingerprint] if outcome.get('confirmed') else []))
            for key in _keys(acc):
                if str(chat_id) in data.get(key, {}):
                    data[key][str(chat_id)] = record
            _atomic_write_json(PATH, data)
    finally:
        _active.reset(marker)
        with _lock:
            _inflight.discard(identity)


def acknowledge(acc, chat_id, fingerprint):
    """Manual review permits only NEW incoming messages, never the held reply."""
    with _lock:
        keys = _keys(acc)
        if any((key, str(chat_id)) in _inflight for key in keys):
            raise ValueError('Отправка ещё выполняется')
        data = _read()
        matches = [(key, data.get(key, {}).get(str(chat_id))) for key in keys]
        matches = [(key, r) for key, r in matches if r]
        if not matches or any(not r.get('has_trigger') or r.get('fingerprint') != fingerprint
                              or r.get('reason') == 'confirmed' for _, r in matches):
            raise ValueError('Запись изменилась или не содержит ID входящего сообщения; автоматическое снятие небезопасно')
        for key, record in matches:
            record['confirmed'] = list(set(record.get('confirmed', []) + [fingerprint]))
            record['reason'] = 'confirmed'
            record['reviewed_at'] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(PATH, data)
