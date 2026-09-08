"""Memoized reads save subprocesses without weakening fresh credential reads."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from claude_code_accounts import core, keychain
from test_moves import budget


def external_write(fake, path, blob):
    # A peer process cannot update this process's memo.
    fake.store[keychain.service_for(path)] = json.dumps({"claudeAiOauth": blob})


def test_memo_age_and_fresh_default(fake_keychain, monkeypatch, tmp_path):
    path = str(tmp_path / "session")
    now = [100.0]
    monkeypatch.setattr(keychain.time, "monotonic", lambda: now[0])
    first, second = {"refreshToken": "first"}, {"refreshToken": "second"}
    external_write(fake_keychain, path, first)
    with budget(fake_keychain, 1):
        assert keychain.read_credentials(path, max_age=keychain.RECENT) == first
    external_write(fake_keychain, path, second)
    now[0] += keychain.RECENT - 0.1
    with budget(fake_keychain, 0):
        assert keychain.read_credentials(path, max_age=keychain.RECENT) == first
    now[0] += 0.1
    with budget(fake_keychain, 1):
        assert keychain.read_credentials(path, max_age=keychain.RECENT) == second
    with budget(fake_keychain, 3):
        for kwargs in ({}, {"max_age": 0}, {"max_age": -1}):
            assert keychain.read_credentials(path, **kwargs) == second


@pytest.mark.parametrize("raw", [None, "invalid JSON", "{}"])
def test_memo_keeps_missing_credentials(fake_keychain, tmp_path, raw):
    path = str(tmp_path / "empty")
    if raw is not None:
        fake_keychain.store[keychain.service_for(path)] = raw
    with budget(fake_keychain, 1):
        assert keychain.read_credentials(path, max_age=keychain.RECENT) is None
        assert keychain.read_credentials(path, max_age=keychain.RECENT) is None


def test_legacy_fallback_is_memoized(fake_keychain):
    blob = {"refreshToken": "legacy"}
    fake_keychain.store[keychain.LEGACY_SERVICE] = json.dumps({"claudeAiOauth": blob})
    with budget(fake_keychain, 2):
        assert keychain.read_credentials(core.DEFAULT_CONFIG, max_age=keychain.RECENT) == blob
        assert keychain.read_credentials(core.DEFAULT_CONFIG, max_age=keychain.RECENT) == blob
    current = {"refreshToken": "current"}
    keychain.write_credentials(core.DEFAULT_CONFIG, current)
    with budget(fake_keychain, 1):
        assert keychain.read_credentials(core.DEFAULT_CONFIG) == current


def test_write_delete_and_forget_update_memo(fake_keychain, tmp_path):
    first, second = (str(tmp_path / name) for name in ("first", "second"))
    blob = {"refreshToken": "live"}
    for path in (first, second):
        keychain.write_credentials(path, blob)
    with budget(fake_keychain, 0):
        assert keychain.read_credentials(first, max_age=keychain.RECENT) == blob
    keychain.forget(first)
    with budget(fake_keychain, 1):
        assert keychain.read_credentials(first, max_age=keychain.RECENT) == blob
        assert keychain.read_credentials(second, max_age=keychain.RECENT) == blob
    with budget(fake_keychain, 0, deletes=1):
        assert keychain.delete_credentials(first)
        assert keychain.read_credentials(first, max_age=keychain.RECENT) is None
    keychain.forget()
    assert keychain._memo == {}


def test_failed_write_keeps_last_successful_value(fake_keychain, monkeypatch, tmp_path):
    path = str(tmp_path / "session")
    blob = {"refreshToken": "live"}
    keychain.write_credentials(path, blob)

    def fail(*args):
        raise RuntimeError("write refused")

    monkeypatch.setattr(keychain, "write_raw", fail)
    with pytest.raises(RuntimeError):
        keychain.write_credentials(path, {"refreshToken": "not stored"})
    with budget(fake_keychain, 0):
        assert keychain.read_credentials(path, max_age=keychain.RECENT) == blob


def test_memo_is_bounded(fake_keychain, tmp_path):
    for i in range(513):
        keychain.read_credentials(str(tmp_path / str(i)))
    assert len(keychain._memo) <= 512
    for i in range(513):
        keychain.write_credentials(str(tmp_path / str(i)), {"refreshToken": str(i)})
    assert len(keychain._memo) <= 512


def test_concurrent_reads_never_block_each_other(fake_keychain, monkeypatch, tmp_path):
    path = str(tmp_path / "session")
    blob = {"refreshToken": "live"}
    external_write(fake_keychain, path, blob)
    ready = threading.Barrier(3, timeout=5)
    read_raw = keychain.read_raw

    def read(service):
        ready.wait()
        return read_raw(service)

    monkeypatch.setattr(keychain, "read_raw", read)
    with budget(fake_keychain, 3), ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(keychain.read_credentials, path, max_age=keychain.RECENT)
            for _ in range(3)
        ]
        assert [future.result(timeout=5) for future in futures] == [blob] * 3


def test_slow_read_does_not_overwrite_newer_write(fake_keychain, monkeypatch, tmp_path):
    path = str(tmp_path / "session")
    old, new = {"refreshToken": "old"}, {"refreshToken": "new"}
    external_write(fake_keychain, path, old)
    entered = threading.Event()
    release = threading.Event()
    read_raw = keychain.read_raw

    def read(service):
        raw = read_raw(service)
        if not entered.is_set():
            entered.set()
            assert release.wait(timeout=5)
        return raw

    monkeypatch.setattr(keychain, "read_raw", read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        reader = pool.submit(keychain.read_credentials, path, max_age=keychain.RECENT)
        try:
            assert entered.wait(timeout=5)
            keychain.write_credentials(path, new)
        finally:
            release.set()
        assert reader.result(timeout=5) == old
    with budget(fake_keychain, 0):
        assert keychain.read_credentials(path, max_age=keychain.RECENT) == new


def test_delete_beats_in_flight_read(fake_keychain, monkeypatch, tmp_path):
    path = str(tmp_path / "session")
    blob = {"refreshToken": "live"}
    external_write(fake_keychain, path, blob)
    entered = threading.Event()
    release = threading.Event()
    read_raw = keychain.read_raw

    def read(service):
        raw = read_raw(service)
        if not entered.is_set():
            entered.set()
            assert release.wait(timeout=5)
        return raw

    monkeypatch.setattr(keychain, "read_raw", read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        reader = pool.submit(keychain.read_credentials, path, max_age=keychain.RECENT)
        try:
            assert entered.wait(timeout=5)
            assert keychain.delete_credentials(path)
        finally:
            release.set()
        assert reader.result(timeout=5) == blob
    with budget(fake_keychain, 0):
        assert keychain.read_credentials(path, max_age=keychain.RECENT) is None
