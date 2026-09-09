#!/usr/bin/env python3
"""Measure user moves with fake credentials, or read-only local state."""

import argparse
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import ExitStack, contextmanager
from functools import partial
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from claude_code_accounts import (  # noqa: E402
    codex,
    core,
    keychain,
    profiles,
    sessions,
    transcripts,
)
from fakes import FakeApi, FakeKeychain, redirect_home, session, sign_in  # noqa: E402


class Setter:
    setattr = staticmethod(setattr)


def offline(*args, **kwargs):
    raise OSError("network disabled by the latency bench")


def forbidden(*args, **kwargs):
    raise AssertionError("the latency bench must not write real state")


@contextmanager
def no_network():
    with ExitStack() as stack:
        stack.enter_context(patch.object(urllib.request, "urlopen", offline))
        stack.enter_context(patch.object(socket, "create_connection", offline))
        stack.enter_context(patch.object(socket.socket, "connect", offline))
        stack.enter_context(patch.object(socket.socket, "connect_ex", offline))
        yield


def measure(move, counter, api, count, setup=lambda: None):
    elapsed, calls = [], []
    for _ in range(count):
        setup()
        counter.reset()
        api.reset()
        start = time.perf_counter()
        move()
        elapsed.append((time.perf_counter() - start) * 1000)
        calls.append((counter.reads, counter.writes, api.profile_calls))
    return statistics.median(elapsed), tuple(statistics.median(c[i] for c in calls)
                                            for i in range(3))


def print_table(rows, security_ms, real=False):
    width = max(len(name) for name, _, _ in rows)
    print(f"{'move':<{width}}  {'ms (median)':>11}  {'reads':>5}  {'writes':>6}  "
          f"{'profile':>7}  {'est. real ms':>12}")
    for name, ms, (reads, writes, profile) in rows:
        estimate = ms if real else ms + (reads + writes) * security_ms
        print(f"{name:<{width}}  {ms:11.3f}  {reads:5g}  {writes:6g}  "
              f"{profile:7g}  {estimate:12.3f}")
    if real:
        print("Real wall times already include security calls; the estimate repeats them.")
    else:
        print(f"est. real ms = wall ms + (reads + writes) * {security_ms:g} ms")
        print("Deletes are counted by the fake but excluded from the requested estimate.")


def seed(home, accounts, count, one_account=False):
    redirect_home(home, setattr)
    core.project_root = os.path.abspath
    core._ROOTS = {}
    fake, api = FakeKeychain(), FakeApi()
    fake.install(Setter())
    api.install(Setter())
    names = [f"acct{i}" for i in range(accounts)]
    blobs = {}
    for name in names:
        email = f"{name}@example.com"
        slot = sign_in(name, email, api)
        blob = keychain.read_credentials(slot)
        blobs[name] = blob
        core.identity(slot, blob)
        core.set_chip_index(name, len(blobs) - 1)
    r = profiles.Rules(default_account=names[0],
                       profiles=[profiles.Profile("work", names[0], [home + "/work"])])
    live = []
    for i in range(count):
        name = names[0] if one_account else names[i % accounts]
        cwd = home + f"/repo{i}"
        r.projects[cwd] = name
        live.append(session(f"term{i:06d}", cwd, name))
    core.save_rules(r)
    accts = core.all_accounts(with_usage=True)
    return fake, api, names, blobs, live, accts


FAKE_MOVES = (
    "resolve_dir cold", "resolve_dir warm", "assign project", "assign default",
    "assign profile", "assign session", "clear session", "apply_now behind",
    "apply_now current", "sync_credentials current", "sync_credentials rotated",
    "prepare_session warm", "all_accounts no usage", "all_accounts cached usage",
    "dirs_to_accounts", "poll owners cold", "poll owners warm", "full refresh",
    "remove_account", "rename_account",
)


def fake_move(label, home, accounts, count):
    fake, api, names, blobs, live, accts = seed(
        home, accounts, count, one_account=label.startswith("apply_now"))
    first = live[0]
    target = names[-1]
    if label == "resolve_dir cold":
        move = partial(core.resolve_dir, first.cwd, "coldterm")
    elif label == "resolve_dir warm":
        move = partial(core.resolve_dir, first.cwd, first.term_id)
    elif label.startswith("assign "):
        scope = label.split()[1]
        key = {"project": home + "/newrepo", "default": "", "profile": "work",
               "session": first.term_id}[scope]
        if scope in ("default", "profile"):
            r = core.rules()
            if scope == "default":
                r.default_account = ""
            else:
                r.profile("work").account = ""
            core.save_rules(r)
        move = partial(core.assign, scope, key, target, live=[])
    elif label == "clear session":
        core.assign("session", first.term_id, target, live=[])
        move = partial(core.clear, "session", first.term_id, live=[])
    elif label.startswith("apply_now"):
        if label.endswith("behind"):
            older = api.blob(f"{names[0]}@example.com", gen=0, expires_in=1800)
            for sess in live:
                keychain.write_credentials(sess.config_dir, older)
        move = partial(core.apply_now, live)
    elif label.startswith("sync_credentials"):
        if label.endswith("rotated"):
            for name in names:
                keychain.write_credentials(
                    core.slot_dir(name), api.blob(f"{name}@example.com", gen=2, expires_in=7200))
        move = partial(core.sync_credentials, live)
    elif label == "prepare_session warm":
        move = partial(core.prepare_session, first.term_id, names[0])
    elif label.startswith("all_accounts"):
        move = partial(core.all_accounts, with_usage="cached" in label)
    elif label == "dirs_to_accounts":
        move = partial(core.dirs_to_accounts, [s.config_dir for s in live], accts)
    elif label.startswith("poll owners"):
        paths = [s.config_dir for s in live]
        move = partial(core.owners_now, paths, {}, {}, accts)
    elif label == "full refresh":
        def move():
            accounts_now = core.all_accounts(with_usage=True)
            core.sync_credentials(live)
            return core.dirs_to_accounts([s.config_dir for s in live], accounts_now)
    elif label == "remove_account":
        move = partial(core.remove_account, names[0])
    elif label == "rename_account":
        move = partial(core.rename_account, names[0], "renamed")
    else:
        raise ValueError(label)
    keychain.forget()
    if label == "poll owners warm":
        owners, prints = move()
        move = partial(core.owners_now, paths, owners, prints, accts)
    return move, fake, api


def run_fake(args):
    rows = []
    with no_network(), patch.object(subprocess, "Popen", forbidden):
        for label in FAKE_MOVES:
            times, budgets = [], []
            for _ in range(args.runs):
                # Setup stays outside the timer so each sample measures a real move.
                with tempfile.TemporaryDirectory(
                        prefix="ccm-bench-", dir=Path(__file__).resolve().parents[1]) as home:
                    with patch.dict(os.environ, {"HOME": home}):
                        move, fake, api = fake_move(label, home, args.accounts, args.sessions)
                        ms, calls = measure(move, fake, api, 1)
                        times.append(ms)
                        budgets.append(calls)
            if len(set(budgets)) != 1:
                raise AssertionError(f"unstable keychain budget for {label}: {budgets}")
            rows.append((label, statistics.median(times), budgets[0]))
    print_table(rows, args.security_ms)
    return rows


class SecurityCalls:
    def __init__(self):
        self.original = keychain._run
        self.durations = []
        self.reset()

    def reset(self):
        self.reads = self.writes = 0

    def run(self, args, stdin=None):
        if args[:2] != ["security", "find-generic-password"] or stdin is not None:
            forbidden()
        self.reads += 1
        start = time.perf_counter()
        try:
            return self.original(args, stdin=stdin)
        finally:
            self.durations.append((time.perf_counter() - start) * 1000)


class ProfileCalls:
    def __init__(self):
        self.reset()

    def reset(self):
        self.profile_calls = 0

    def get(self, path, token, timeout=20):
        if path == "/api/oauth/profile":
            self.profile_calls += 1
        return offline()


@contextmanager
def read_only(counter, api):
    """Keep account display and transcript scans from repairing or saving state."""
    replacements = (
        (keychain, "_run", counter.run),
        (keychain, "write_raw", forbidden), (keychain, "delete", forbidden),
        (core, "live_blob", lambda path, allow_refresh=True: keychain.read_credentials(path)),
        (core, "find_live_blob", lambda *args, **kwargs: None),
        (core, "adopt", forbidden), (core, "_persist", forbidden),
        (core, "_cache_write", lambda *args, **kwargs: None),
        (core, "_get", api.get), (core, "_post", forbidden),
        (core, "_CODEX_ADOPTED", True),
        (codex, "live_auth", codex.read_auth),
        (codex, "ensure_account_dir", codex.slot_dir),
        (codex, "adopt_default", forbidden), (codex, "write_auth", forbidden),
        (transcripts, "_tokens_save", lambda: None),
    )
    with no_network(), ExitStack() as stack:
        for module, name, value in replacements:
            stack.enter_context(patch.object(module, name, value))
        yield


def run_real(args):
    counter, api = SecurityCalls(), ProfileCalls()
    rows = []
    with read_only(counter, api):
        dirs = core.credential_dirs()
        names = core.account_names()
        live = sessions.live(dirs)
        accts = core.all_accounts(with_usage=False)
        paths = sorted({s.env_config_dir or s.config_dir for s in live})
        moves = [
            ("account_names", core.account_names),
            ("credential_dirs", core.credential_dirs),
        ]
        if names:
            moves.append(("read first slot", lambda: keychain.read_credentials(
                core.slot_dir(names[0]))))
        for label, env, git, transcript in (
            ("sessions.live no env", False, False, False),
            ("sessions.live env", True, False, False),
            ("sessions.live env+git", True, True, False),
            ("sessions.live env+git+transcript", True, True, True),
        ):
            moves.append((label, lambda env=env, git=git, transcript=transcript: sessions.live(
                dirs, with_env=env, with_git=git, with_transcript=transcript)))
        moves.extend([
            ("all_accounts no usage", lambda: core.all_accounts(with_usage=False)),
            ("dirs_to_accounts", lambda: core.dirs_to_accounts(paths, accts)),
            ("poll fingerprints", lambda: [core.fingerprint(keychain.read_credentials(p))
                                           for p in paths]),
        ])
        counter.durations.clear()
        for label, move in moves:
            ms, calls = measure(move, counter, api, args.runs)
            rows.append((label, ms, calls))
    print_table(rows, args.security_ms, real=True)
    if counter.durations:
        print(f"security per-call median: {statistics.median(counter.durations):.3f} ms "
              f"({len(counter.durations)} calls)")
    else:
        print("security per-call median: n/a (no calls)")
    print("Read-only: refresh, healing, adoption and cache saves are disabled; network is blocked.")
    print("Profile counts are blocked lookup attempts. Session metadata caches stay warm.")
    return rows


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts", type=positive, default=5)
    parser.add_argument("--sessions", type=positive, default=14)
    parser.add_argument("--runs", "-n", type=positive, default=20)
    parser.add_argument("--security-ms", type=float, default=15.0)
    parser.add_argument("--real", action="store_true",
                        help="read local state without writes or network")
    args = parser.parse_args(argv)
    if args.security_ms < 0:
        parser.error("--security-ms must be nonnegative")
    (run_real if args.real else run_fake)(args)


if __name__ == "__main__":
    main()
