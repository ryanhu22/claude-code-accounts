"""Cooperate with Claude Code's own advisory locks around credential writes.

Claude Code guards its OAuth token refresh with the npm ``proper-lockfile``
package. The protocol, and the constants below, are documented in the
claude-swap project (https://github.com/realiti4/claude-swap), verified there
against the 2.1.218 bundle:

- The lock artifact is a **directory**; ``mkdir`` atomicity is the mutex.
- The refresh path takes two locks, in order: the primary
  ``<config dir>/.oauth_refresh.lock``, then the legacy ``<config dir>.lock``.
  Both are stale only after 60s, and a live holder touches every 5s.
- Claude Code re-reads the credential once it holds the locks, and skips the
  refresh when what it finds is not expired.

Why this matters here. Every credential write we do is a read-modify-write on
an item a running Claude Code may be rewriting at the same moment:

- A refresh that lands inside Claude Code's own refresh window rotates the
  token twice. One of the two copies keeps a spent refresh token, and its next
  refresh fails with ``invalid_grant``: the session looks logged out.
- A swap that lands in that window is overwritten by the old account's
  refreshed token, so the swap silently reverts.

Holding the same locks closes both. Claude Code retries a held credentials lock
5 times with 1-2s jittered sleeps, so briefly holding it is fully cooperative.
"""
from __future__ import annotations

import contextlib
import os
import random
import threading
import time
from collections.abc import Iterator

# Claude Code's credential locks run `stale: 60000, update: 5000`. A lock
# younger than 60s belongs to a live holder and must never be stolen: its
# toucher can stall well past 10s (laptop sleep, a blocked event loop) while it
# still legitimately owns the lock.
STALE_SECONDS = 60.0
CONFIG_STALE_SECONDS = 10.0   # the config lock keeps proper-lockfile's defaults
TOUCH_SECONDS = 3.0          # a little faster than Claude Code's 5s, for margin
# Claude Code holds the lock for one token-endpoint round trip. Waiting ~9s per
# lock outlasts that without ever stalling us for long. Two locks are taken in
# sequence, so the worst case is about twice this.
TIMEOUT_SECONDS = 9.0


class LockBusy(RuntimeError):
    """Claude Code (or another ccm) held the lock past the timeout."""


def lock_dirs(config_dir: str) -> tuple[str, str]:
    """Claude Code's two credential locks for a config dir, in its own order."""
    config_dir = os.path.abspath(config_dir).rstrip("/")
    return (os.path.join(config_dir, ".oauth_refresh.lock"), config_dir + ".lock")


@contextlib.contextmanager
def _one(path: str, timeout: float, staleness: float = STALE_SECONDS) -> Iterator[None]:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    start = time.monotonic()
    while True:
        try:
            os.mkdir(path)
            break
        except FileExistsError:
            pass
        except OSError as e:
            raise LockBusy(f"cannot create {path}: {e}") from e
        if time.monotonic() - start > timeout:
            raise LockBusy(f"{os.path.basename(path)} held; Claude Code is busy")
        try:
            held = os.stat(path).st_mtime
        except FileNotFoundError:
            continue                      # released between mkdir and stat
        if time.time() - held > staleness:
            try:
                os.rmdir(path)            # dead holder per the protocol
            except OSError:
                time.sleep(0.05)          # cannot remove it either; don't spin
            continue
        time.sleep(0.25 + random.random() * 0.25)

    stop = threading.Event()

    def touch() -> None:
        while not stop.wait(TOUCH_SECONDS):
            try:
                os.utime(path)
            except OSError:
                return                    # lock removed; nothing to keep alive

    toucher = threading.Thread(target=touch, daemon=True)
    toucher.start()
    try:
        yield
    finally:
        stop.set()
        toucher.join(timeout=1.0)
        with contextlib.suppress(OSError):
            os.rmdir(path)


@contextlib.contextmanager
def credentials(config_dir: str, timeout: float = TIMEOUT_SECONDS) -> Iterator[None]:
    """Hold Claude Code's credential locks for one config dir.

    Raises LockBusy rather than waiting forever. A caller that cannot take the
    lock should leave the credential alone and try on the next pass: a missed
    refresh costs one cycle, a write that ignores the lock costs a login.
    """
    primary, legacy = lock_dirs(config_dir)
    with _one(primary, timeout), _one(legacy, timeout):
        yield


@contextlib.contextmanager
def config(config_dir: str, timeout: float = TIMEOUT_SECONDS) -> Iterator[None]:
    """Hold Claude Code's lock on a config dir's .claude.json.

    That file is rewritten whole, so a read-modify-write racing Claude Code's
    own would drop whichever change lost. It keeps proper-lockfile's older
    defaults: stale after 10s rather than 60.
    """
    base = os.path.abspath(config_dir).rstrip("/")
    home = os.path.expanduser("~")
    target = (os.path.join(home, ".claude.json") if base == os.path.join(home, ".claude")
              else os.path.join(base, ".claude.json"))
    with _one(target + ".lock", timeout, staleness=CONFIG_STALE_SECONDS):
        yield
