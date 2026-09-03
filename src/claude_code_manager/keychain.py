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
from typing import Optional

LEGACY_SERVICE = "Claude Code-credentials"
_NOT_FOUND_RC = 44
# `security` reads a whole line from stdin; keep a margin under its limit.
_STDIN_LIMIT = 4096 - 64


def account_name() -> str:
    return os.environ.get("USER") or pwd.getpwuid(os.geteuid()).pw_name


def service_for(config_dir: str) -> str:
    digest = hashlib.sha256(os.path.abspath(config_dir).encode()).hexdigest()[:8]
    return f"{LEGACY_SERVICE}-{digest}"


def _run(args: list[str], stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=20)


def read_raw(service: str) -> Optional[str]:
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


def read_credentials(config_dir: str) -> Optional[dict]:
    """The claudeAiOauth blob a config dir is logged in with."""
    raw = read_raw(service_for(config_dir))
    if raw is None and os.path.abspath(config_dir) == os.path.expanduser("~/.claude"):
        raw = read_raw(LEGACY_SERVICE)
    if not raw:
        return None
    try:
        return json.loads(raw).get("claudeAiOauth") or None
    except ValueError:
        return None


def write_credentials(config_dir: str, blob: dict) -> None:
    write_raw(service_for(config_dir), json.dumps({"claudeAiOauth": blob}))
