"""Offline recovery of one verified legacy message pause. Stop service first.

Usage: python -m scripts.recover_message_pause CHAT_ID
Does not contact HH or send messages. Keeps a durable chat exclusion before
releasing the matching sole temporary account's message-only pause.
"""
import sys
from pathlib import Path
import json
from app.message_quarantine import retain
from app.storage import _atomic_write_json


def main(chat_id):
    path = Path('data/browser_sessions.json')
    accounts = json.loads(path.read_text())
    if len(accounts) != 1:
        raise RuntimeError('Expected exactly one verified temporary account')
    acc = accounts[0]
    if not acc.get('paused') or acc.get('paused_reason') != 'message_outcome_unknown':
        raise RuntimeError('Account pause has changed')
    if any(acc.get(k) for k in ('pending_apply', 'pending_applies', 'hard_stopped', 'limit_exceeded', 'cookies_expired')):
        raise RuntimeError('Another protection requires investigation')
    retain(acc, chat_id)
    acc.update(paused=False, paused_reason='')
    _atomic_write_json(path, accounts)
    print('Chat isolated; message-only account pause cleared. No requests sent.')


if __name__ == '__main__':
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        raise SystemExit('One numeric chat ID is required')
    main(sys.argv[1])
