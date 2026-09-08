"""macOS Keychain access for Claude Code credentials.

Claude Code 2.1.224+ stores one login per config dir, under the service name
``Claude Code-credentials-<sha256(abs config dir)[:8]>``. Older versions used a
bare ``Claude Code-credentials`` item, which is kept here as a read fallback.
"""
from __future__ import annotations

import binascii
import hashlib
import json
import os
import pwd
import subprocess
import threading
import time

LEGACY_SERVICE = "Claude Code-credentials"
_NOT_FOUND_RC = 44
# `security` reads a whole line from stdin; keep a margin under its limit.
_STDIN_LIMIT = 4096 - 64
# Display and comparison reads may be this stale. Changes in another process
# show within this many seconds.
RECENT = 20.0
_memo: dict[str, tuple[float, dict | None]] = {}
_memo_lock = threading.Lock()


def account_name() -> str:
    return os.environ.get("USER") or pwd.getpwuid(os.geteuid()).pw_name


def service_for(config_dir: str) -> str:
    digest = hashlib.sha256(os.path.abspath(config_dir).encode()).hexdigest()[:8]
    return f"{LEGACY_SERVICE}-{digest}"


def _run(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=20)


def read_raw(service: str) -> str | None:
    r = _run(["security", "find-generic-password", "-s", service, "-a", account_name(), "-w"])
    if r.returncode == _NOT_FOUND_RC:
        return None
    if r.returncode != 0:
        # some items were created without an account attribute
        r = _run(["security", "find-generic-password", "-s", service, "-w"])
        if r.returncode != 0:
            return None
    return r.stdout.strip() or None


def write_raw(service: str, value: str) -> None:
    """Store a secret, keeping it out of the process argument list when possible.

    `security` accepts hex with -X, and reads the command line from stdin with
    -i, so the secret never appears in `ps` output. Very large values fall back
    to argv because -i reads a single bounded line.
    """
    hexed = binascii.hexlify(value.encode()).decode()
    acct = account_name()
    line = f'add-generic-password -U -a "{acct}" -s "{service}" -X {hexed}\n'
    if len(line) <= _STDIN_LIMIT:
        r = _run(["security", "-i"], stdin=line)
        if r.returncode == 0:
            return
    r = _run(["security", "add-generic-password", "-U", "-a", acct, "-s", service, "-w", value])
    if r.returncode != 0:
        raise RuntimeError(f"keychain write failed: {r.stderr.strip()[:200]}")


def delete(service: str) -> bool:
    r = _run(["security", "delete-generic-password", "-s", service])
    return r.returncode in (0, _NOT_FOUND_RC)


def _remember(service: str, blob: dict | None, started: float | None = None) -> None:
    """Store a result while the caller holds the memo lock."""
    _memo[service] = (time.monotonic() if started is None else started, blob)
    if len(_memo) > 512:
        _memo.clear()


def read_credentials(config_dir: str, max_age: float = 0.0) -> dict | None:
    """The claudeAiOauth blob, read fresh unless the caller allows a memo hit.

    Reads under credential locks must use the fresh default. Release the memo
    lock around I/O so a slow Keychain call cannot block other credential calls.
    Keep newer memo entries when a slow read finishes.
    """
    service = service_for(config_dir)
    with _memo_lock:
        hit = _memo.get(service)
        if max_age > 0 and hit is not None and time.monotonic() - hit[0] < max_age:
            return hit[1]
    started = time.monotonic()
    raw = read_raw(service)
    if raw is None and os.path.abspath(config_dir) == os.path.expanduser("~/.claude"):
        raw = read_raw(LEGACY_SERVICE)
    blob = None
    if raw:
        try:
            blob = json.loads(raw).get("claudeAiOauth") or None
        except ValueError:
            pass
    with _memo_lock:
        hit = _memo.get(service)
        if hit is None or hit[0] <= started:
            _remember(service, blob, started)
    return blob


def write_credentials(config_dir: str, blob: dict) -> None:
    service = service_for(config_dir)
    write_raw(service, json.dumps({"claudeAiOauth": blob}))
    with _memo_lock:
        _remember(service, blob)


def delete_credentials(config_dir: str) -> bool:
    service = service_for(config_dir)
    deleted = delete(service)
    with _memo_lock:
        # A tombstone keeps older in-flight reads from restoring deleted credentials.
        _remember(service, None)
    return deleted


def forget(config_dir: str | None = None) -> None:
    """Drop one memo entry, or all entries for a new test or scan."""
    with _memo_lock:
        if config_dir is None:
            _memo.clear()
        else:
            _memo.pop(service_for(config_dir), None)
