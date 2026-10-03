"""What the credential lifecycle tests share: sessions, the app, the invariants.

A session here is as real as the sandbox allows. The real `ccm resolve` picks
and prepares its config dir, a live process stands in for Claude Code (so
`os.kill(pid, 0)` says it is alive), the registry file Claude Code would write
sits in that dir, and the `ps` stub answers for the pid with the environment a
Claude Code session has. From there `core.credential_dirs()` and
`sessions.live()` find it the way the app does.

The only thing Claude Code does with a credential that the app cannot see is
spend a refresh token on its own, so `claude_code_refresh` does exactly that,
under Claude Code's own locks, against the fake server.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

from claude_code_accounts import core, keychain, locks, profiles, sessions
from e2e.harness import Sandbox


def one_account(sandbox, server, name: str = "a", email: str = "a@example.com",
                **fields) -> dict:
    """A signed-in account that every session resolves to. Returns its credential."""
    server.add_claude(email, **fields)
    blob = sandbox.seed_claude(name, email)
    core.save_rules(profiles.Rules(default_account=name))
    return blob


class Fleet:
    """The Claude Code sessions a test starts, and how to find them again."""

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox
        self.procs: list[subprocess.Popen] = []
        self.dirs: dict[str, str] = {}        # term id -> config dir
        self.cwds: dict[str, str] = {}

    def start(self, term_id: str, cwd: str | None = None,
              env: dict[str, str] | None = None) -> sessions.Session:
        """A session in one terminal, through the real resolver.

        `ccm resolve` is what the shell wrapper runs before every launch: it
        prepares the terminal's dir and hands it the account's credential.
        """
        cwd = cwd or self.sandbox.home
        r = self.sandbox.run("resolve", cwd=cwd, env={"TERM_SESSION_ID": term_id, **(env or {})})
        assert r.returncode == 0, r.stderr
        path = r.stdout.strip()
        assert path == core.session_dir(term_id), (path, r.stderr)
        proc = subprocess.Popen(["/bin/sleep", "600"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        now = int(time.time() * 1000)
        os.makedirs(os.path.join(path, "sessions"), exist_ok=True)
        with open(os.path.join(path, "sessions", f"{proc.pid}.json"), "w") as f:
            json.dump({"pid": proc.pid, "sessionId": f"sess-{term_id}", "cwd": cwd,
                       "name": os.path.basename(cwd), "kind": "interactive",
                       "entrypoint": "cli", "status": "idle",
                       "startedAt": now, "updatedAt": now}, f)
        table_path = os.path.join(self.sandbox.root, "ps.json")
        try:
            with open(table_path) as f:
                table = json.load(f)
        except (OSError, ValueError):
            table = {}
        table[str(proc.pid)] = {"tty": f"ttys{len(self.procs):03d}", "env": {
            "TERM_SESSION_ID": term_id, "CLAUDE_CONFIG_DIR": path,
            "TERM_PROGRAM": "Apple_Terminal"}}
        with open(table_path, "w") as f:
            json.dump(table, f)
        self.dirs[term_id] = path
        self.cwds[term_id] = cwd
        # The subprocess wrote the keychain; this process's memo is older.
        keychain.forget()
        sess = next(s for s in self.live() if s.term_id == term_id)
        return sess

    def live(self) -> list[sessions.Session]:
        """What the app's session poll finds: discovery through the registry files."""
        keychain.forget()
        return sessions.live(core.credential_dirs())

    def stop(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        self.procs.clear()


def post_token(server_url: str, body: dict) -> dict:
    """A token request straight to the fake, the way Claude Code would send it."""
    req = urllib.request.Request(
        server_url + "/v1/oauth/token", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=10) as r:
        return json.load(r)


def claude_code_refresh(server_url: str, config_dir: str, hold: float = 0.0) -> str:
    """What a Claude Code session does with a refresh token of its own.

    Takes the same two locks Claude Code takes, re-reads under them and skips
    the refresh when what it finds is not within five minutes of expiry (that
    re-read is what makes the locks worth taking), spends the token and
    writes the whole rotated credential back. Returns "rotated", "fresh",
    "no_refresh_token", "invalid_grant" or "transient". `hold` keeps the
    locks for that long after the write, as a slow session would.
    """
    with locks.credentials(config_dir):
        cur = keychain.read_credentials(config_dir)
        if not cur or not cur.get("refreshToken"):
            return "no_refresh_token"
        if not core.expiring(cur, 5 * 60):
            return "fresh"
        try:
            resp = post_token(server_url, {"grant_type": "refresh_token",
                                           "refresh_token": cur["refreshToken"],
                                           "client_id": core.CLIENT_ID})
        except urllib.error.HTTPError as e:
            try:
                err = json.load(e).get("error")
            except (ValueError, AttributeError):
                err = None
            if err == "invalid_grant":
                # Claude Code empties the token fields and stops reading the
                # item: that session is signed out until a /login.
                keychain.write_credentials(config_dir, {**cur, "accessToken": "",
                                                        "refreshToken": ""})
                return "invalid_grant"
            return "transient"
        except Exception:
            return "transient"
        keychain.write_credentials(config_dir, core._apply(cur, resp))
        if hold:
            time.sleep(hold)
        return "rotated"


def menubar_app(live: list[sessions.Session]):
    """The app's sleep, wake and tick handlers, on an app that never drew a menu."""
    from claude_code_accounts import menubar

    app = menubar.ManagerApp.__new__(menubar.ManagerApp)
    app._snapshot = menubar.Snapshot()
    app._snapshot.sessions = list(live)
    app._syncing = False
    app._last_tick = 0.0
    app._lock = threading.Lock()
    app._done = []
    return app


def wait_pass(app, timeout: float = 60.0) -> None:
    """Block until the credential pass the app started off-thread has finished."""
    deadline = time.monotonic() + timeout
    while app._syncing:
        if time.monotonic() > deadline:
            raise TimeoutError("the credential pass did not finish")
        time.sleep(0.02)


def generation(server, blob: dict | None) -> int:
    return server.generation((blob or {}).get("accessToken"))


def owner(server, blob: dict | None) -> str:
    return server._access.get((blob or {}).get("accessToken") or "", "")


def check_invariants(sandbox: Sandbox, server, fleet: Fleet, app_running: bool = True,
                     settled: bool = False) -> None:
    """The three things that must hold whatever the scenario did.

    No session dir holds a refresh token while the app runs; no refresh token
    went to the server twice; and no copy of an account sits on an older
    generation than the account's slot. With `settled`, every copy is on the
    slot's generation exactly: the passes have had their say.
    """
    assert server.reused_refresh_tokens == [], \
        f"a refresh token was sent twice: {server.reused_refresh_tokens}"
    slots = {name: sandbox.blob(core.slot_dir(name)) for name in core.account_names()}
    by_owner = {owner(server, blob): generation(server, blob) for blob in slots.values() if blob}
    for term, path in fleet.dirs.items():
        copy = sandbox.blob(path)
        if not copy or not copy.get("accessToken"):
            continue
        if app_running:
            assert "refreshToken" not in copy, f"{term}: a session copy holds a refresh token"
        who = owner(server, copy)
        if who in by_owner:
            have, slot_gen = generation(server, copy), by_owner[who]
            assert have >= slot_gen, f"{term}: copy is gen {have}, slot is gen {slot_gen}"
            if settled:
                assert have == slot_gen, f"{term}: copy is gen {have}, slot is gen {slot_gen}"


def credential_log(sandbox: Sandbox) -> list[str]:
    try:
        with open(os.path.join(sandbox.home, ".claude-manager", "credentials.log")) as f:
            return [line.rstrip("\n") for line in f]
    except OSError:
        return []


def expire_in(sandbox: Sandbox, config_dir: str, seconds: float) -> dict:
    """Move a stored credential's expiry, as a token that aged would show."""
    blob = sandbox.blob(config_dir)
    assert blob, f"nothing stored for {config_dir}"
    blob = {**blob, "expiresAt": int((time.time() + seconds) * 1000)}
    sandbox._put_item(keychain.service_for(config_dir), json.dumps({"claudeAiOauth": blob}))
    keychain.forget()
    return blob
