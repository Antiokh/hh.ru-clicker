"""Receipt-attempt reservations must survive older queued account saves."""
import json
import errno
import threading

import pytest

from app import config, storage


def test_waited_save_is_ordered_after_old_snapshot(monkeypatch):
    release, started = threading.Event(), threading.Event()

    def blocker():
        started.set()
        assert release.wait(5)

    storage._save_executor.submit(blocker)
    assert started.wait(2)
    account = {"name": "fixture", "pending_apply": {"reconcile_attempts": 0},
               "_runtime": threading.Lock()}
    monkeypatch.setattr(config, "accounts_data", [account])
    config.save_accounts()
    account["pending_apply"]["reconcile_attempts"] = 1
    errors = []

    def save():
        try:
            config.save_accounts(wait=True)
        except Exception as exc:
            errors.append(exc)

    waiter = threading.Thread(target=save)
    waiter.start()
    release.set()
    waiter.join(5)
    assert not waiter.is_alive() and not errors
    data = json.loads(config.ACCOUNTS_FILE.read_text())
    assert data[0]["pending_apply"]["reconcile_attempts"] == 1
    assert "_runtime" not in data[0]
    assert config.ACCOUNTS_FILE.stat().st_mode & 0o777 == 0o600


def test_snapshot_does_not_alias_pending(monkeypatch):
    writes = []
    monkeypatch.setattr(storage, "_schedule_save", lambda fn: writes.append(fn))
    account = {"pending_apply": {"reconcile_attempts": 0}}
    monkeypatch.setattr(config, "accounts_data", [account])
    config.save_accounts()
    account["pending_apply"]["reconcile_attempts"] = 2
    writes[0]()
    assert json.loads(config.ACCOUNTS_FILE.read_text())[0]["pending_apply"]["reconcile_attempts"] == 0


def test_waited_save_propagates_write_failure(monkeypatch):
    monkeypatch.setattr(config, "accounts_data", [{"name": "fixture"}])

    def fail(*args):
        raise OSError("fixture write failure")

    monkeypatch.setattr(storage, "_atomic_write_json", fail)
    with pytest.raises(OSError, match="fixture write failure"):
        config.save_accounts(wait=True)
    config.save_accounts()  # Legacy fire-and-forget callers remain non-raising.
    storage._save_executor.submit(lambda: None).result(timeout=3)


def test_waited_shutdown_fallback_propagates_failure(monkeypatch):
    class ClosedExecutor:
        def submit(self, fn):
            raise RuntimeError("closed")

    monkeypatch.setattr(storage, "_save_executor", ClosedExecutor())

    def fail():
        raise OSError("disk unavailable")

    with pytest.raises(OSError, match="disk unavailable"):
        storage._schedule_save(fail, wait=True)


@pytest.mark.parametrize("fail_at", [1, 2])
@pytest.mark.parametrize("disk_error", [errno.EIO, errno.ENOSPC])
def test_waited_save_propagates_file_and_directory_sync_errors(monkeypatch, fail_at, disk_error):
    monkeypatch.setattr(config, "accounts_data", [{"name": "fixture"}])
    calls = []

    def fsync(fd):
        calls.append(fd)
        if len(calls) == fail_at:
            raise OSError(disk_error, "synthetic sync failure")

    monkeypatch.setattr(storage.os, "fsync", fsync)
    with pytest.raises(OSError, match="synthetic sync failure"):
        config.save_accounts(wait=True)
    assert len(calls) == fail_at


def test_session_snapshot_capture_is_ordered_with_sequence_assignment(monkeypatch):
    captured, release, newer_started = threading.Event(), threading.Event(), threading.Event()
    original_copy = storage.copy.deepcopy
    original = {"name": "fixture", "pending_apply": {"reconcile_attempts": 0}}
    newer = {"name": "fixture", "pending_apply": {"reconcile_attempts": 1}}
    writes, errors = [], []

    def delayed_copy(value, *args, **kwargs):
        result = original_copy(value, *args, **kwargs)
        if value is original:
            captured.set()
            assert release.wait(5)
        return result

    def write(path, rows):
        writes.append(rows[0]["pending_apply"]["reconcile_attempts"])

    monkeypatch.setattr(storage.copy, "deepcopy", delayed_copy)
    monkeypatch.setattr(storage, "_atomic_write_json", write)
    monkeypatch.setattr(storage, "_sessions_pending_snapshot", None)
    monkeypatch.setattr(storage, "_sessions_pending_seq", 0)
    monkeypatch.setattr(storage, "_sessions_written_seq", 0)

    def save(row):
        try:
            if row is newer:
                newer_started.set()
            storage.save_browser_sessions([row], wait=True)
        except Exception as exc:
            errors.append(exc)

    old_thread = threading.Thread(target=save, args=(original,))
    new_thread = threading.Thread(target=save, args=(newer,))
    old_thread.start()
    try:
        assert captured.wait(2)
        # This is the essential barrier: the stalled old capture owns the
        # sequence lock, so a newer capture cannot overtake it then be undone.
        acquired = storage._sessions_pending_lock.acquire(blocking=False)
        if acquired:
            storage._sessions_pending_lock.release()
        assert not acquired
        new_thread.start()
        assert newer_started.wait(2)
    finally:
        release.set()
        old_thread.join(5)
        if new_thread.ident is not None:
            new_thread.join(5)
    assert not errors and not old_thread.is_alive() and not new_thread.is_alive()
    assert writes and writes[-1] == 1 and writes == sorted(writes)
