"""Start a sandboxed `ccm login` and hand its sign-in page to a browser test.

Prints one JSON line as soon as ccm has asked for a browser:

    {"authorize_url": ..., "fake_server": ..., "callback_url": ...,
     "sandbox": ..., "account": ..., "email": ..., "pid": ...}

A browser (Playwright, a person) opens `authorize_url`, presses the
Authorize button, and lands on ccm's own page at `callback_url`. When ccm
exits, or `--timeout` seconds pass, a second JSON line reports the outcome:

    {"done": true, "returncode": 0, "signed_in": true, "stdout": ..., "stderr": ...}

The exit status is ccm's. The sandbox is removed unless `--keep` is given.
Nothing here touches a real account: see tests/e2e/README.md.

    uv run python tests/e2e/serve_signin.py --account work --email work@example.com
    uv run python tests/e2e/serve_signin.py --codex --deny

`SignIn` is the same thing as an object, for `signin_service.py`, which
runs many of these for a browser suite.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from e2e.fake_server import FakeServer  # noqa: E402
from e2e.harness import Sandbox  # noqa: E402

CODEX_PORT = 1455


class SignIn:
    """One sandboxed `ccm login`, with the browser's part left to the caller.

    Every failure mode a browser test wants is a keyword here:

    - `codex`: `ccm login --codex`, which binds port 1455 as Codex does.
    - `deny`: the next press of Authorize comes back as `access_denied`.
    - `browser`: the email the browser is signed in as, when it is not the
      account being added.
    - `seeded`: the email the slot held before this sign-in, so ccm can say
      the account changed hands.
    - `expired_code`: the token endpoint refuses the code as expired.
    - `slow_token`: seconds the token exchange takes, so the callback tab can
      be reloaded while ccm is still finishing.
    - `busy_port`: something else holds port 1455 before ccm starts.

    `server` and `sandbox` let a second sign-in share the first one's, for a
    second account in the same home.
    """

    def __init__(self, account: str = "work", email: str = "work@example.com", *,
                 tier: str = "default_claude_max_5x", plan: str = "prolite",
                 codex: bool = False, deny: bool = False, browser: str = "",
                 seeded: str = "", expired_code: bool = False, slow_token: float = 0.0,
                 busy_port: bool = False, server: FakeServer | None = None,
                 sandbox: Sandbox | None = None) -> None:
        self.account, self.email, self.codex = account, email, codex
        self.owns_server = server is None
        self.server = server or FakeServer().start()
        self.sandbox = sandbox or Sandbox(tempfile.mkdtemp(prefix="ccm-e2e-"), self.server)
        self.proc: subprocess.Popen | None = None
        self._busy: socket.socket | None = None
        self._outcome: dict | None = None
        self._add(email, tier=tier, plan=plan)
        # The browser is signed in as this account unless told otherwise. A
        # real browser would stay on whoever signed in first; the fake page
        # has no account switcher, so the choice is made here.
        chosen = browser or email
        self._add(chosen, tier=tier, plan=plan)
        if codex:
            self.server.codex_browser = chosen
        else:
            self.server.browser = chosen
        if seeded:
            if codex:
                self.sandbox.seed_codex(account, seeded, plan=plan)
            else:
                self.sandbox.seed_claude(account, seeded, tier=tier)
        if deny:
            self.server.deny_next_authorize = True
        token = "/oauth/token" if codex else "/v1/oauth/token"
        if expired_code:
            # ccm retries a Codex exchange as JSON after a 400 to the form
            # post, so both tries have to be refused.
            self.server.script(token, 400, {"error": "invalid_grant",
                                            "error_description": "Authorization code expired"},
                               times=2 if codex else 1)
        if slow_token:
            self.server.hold[token] = slow_token
        if busy_port:
            self._busy = socket.socket()
            self._busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self._busy.bind(("127.0.0.1", CODEX_PORT))
            except OSError as e:
                # Someone else holds it already: say so, and leave no sandbox
                # behind for a sign-in that never starts.
                self.close()
                raise RuntimeError(f"port {CODEX_PORT} is already in use on this machine") from e
            self._busy.listen(1)

    def _add(self, email: str, tier: str, plan: str) -> None:
        if self.codex:
            if email not in self.server.codex:
                self.server.add_codex(email, plan=plan)
        elif email not in self.server.claude:
            self.server.add_claude(email, tier=tier)

    def start(self, timeout: float = 30.0) -> dict:
        """Run ccm until it asks for a browser. The dict is the first JSON line.

        When ccm exits first (port 1455 busy, say), the dict says so instead:
        `{"exited": true, "returncode": ..., "stdout": ..., "stderr": ...}`.
        """
        seen = len(self.sandbox.opened_urls())
        self.proc = self.sandbox.popen("login", self.account, *(["--codex"] if self.codex else []))
        # Nothing will ever be pasted: a prompt for a code must get EOF, not hang.
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        deadline = time.monotonic() + timeout
        while True:
            urls = self.sandbox.opened_urls()
            if len(urls) > seen:
                url = urls[seen]
                break
            if self.proc.poll() is not None:
                return {"exited": True, **self.outcome()}
            if time.monotonic() > deadline:
                return {"exited": True, "error": "ccm never asked to open a browser",
                        **self.outcome()}
            time.sleep(0.05)
        callback = parse_qs(urlsplit(url).query).get("redirect_uri", [""])[0]
        return {"authorize_url": url, "fake_server": self.server.url, "callback_url": callback,
                "sandbox": self.sandbox.root, "account": self.account, "email": self.email,
                "codex": self.codex, "pid": self.proc.pid}

    def wait(self, timeout: float) -> bool:
        """Whether ccm has exited within `timeout` seconds."""
        assert self.proc is not None
        deadline = time.monotonic() + timeout
        while self.proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.proc.poll() is not None

    def signed_in(self) -> bool:
        if self.codex:
            return os.path.exists(os.path.join(self.sandbox.codex_slot(self.account), "auth.json"))
        return bool(self.sandbox.blob(self.sandbox.slot(self.account)))

    def outcome(self, wait: float = 10.0) -> dict:
        """The second JSON line. Kills ccm if it has not exited within `wait`."""
        assert self.proc is not None
        if self._outcome is None:
            if not self.wait(wait):
                self.proc.kill()
            out, err = self.proc.communicate()
            self._outcome = {"done": True, "returncode": self.proc.returncode,
                             "signed_in": self.signed_in() and self.proc.returncode == 0,
                             "stdout": out, "stderr": err}
        return self._outcome

    def ccm(self, *args: str) -> dict:
        """Run another ccm command in the same sandbox: `ccm list` after a sign-in."""
        r = self.sandbox.run(*args, timeout=60)
        return {"returncode": r.returncode, "stdout": r.stdout, "stderr": r.stderr}

    def close(self, keep: bool = False) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if self._busy is not None:
            self._busy.close()
            self._busy = None
        if self.owns_server:
            self.server.close()
            if not keep:
                self.sandbox.remove()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--account", default="work", help="the ccm account name to sign in")
    p.add_argument("--email", default="work@example.com",
                   help="the account the fake browser session belongs to")
    p.add_argument("--tier", default="default_claude_max_5x")
    p.add_argument("--plan", default="prolite", help="the Codex plan, with --codex")
    p.add_argument("--codex", action="store_true", help="sign in to Codex (port 1455)")
    p.add_argument("--deny", action="store_true", help="the Authorize press is refused")
    p.add_argument("--browser", default="", metavar="EMAIL",
                   help="the account the browser is signed in as, if another one")
    p.add_argument("--seeded", default="", metavar="EMAIL",
                   help="the account the slot held before this sign-in")
    p.add_argument("--expired-code", action="store_true",
                   help="the token endpoint refuses the code as expired")
    p.add_argument("--slow-token", type=float, default=0.0, metavar="SECONDS",
                   help="how long the token exchange takes")
    p.add_argument("--busy-port", action="store_true", help="hold port 1455 before ccm starts")
    p.add_argument("--timeout", type=float, default=300.0,
                   help="seconds to wait for the sign-in to finish")
    p.add_argument("--keep", action="store_true", help="leave the sandbox directory behind")
    args = p.parse_args(argv)

    signin = SignIn(args.account, args.email, tier=args.tier, plan=args.plan, codex=args.codex,
                    deny=args.deny, browser=args.browser, seeded=args.seeded,
                    expired_code=args.expired_code, slow_token=args.slow_token,
                    busy_port=args.busy_port)
    try:
        first = signin.start()
        print(json.dumps(first), flush=True)
        if first.get("exited"):
            return first["returncode"] if first["returncode"] is not None else 1
        signin.wait(args.timeout)
        outcome = signin.outcome()
        print(json.dumps(outcome), flush=True)
        return outcome["returncode"] if outcome["returncode"] is not None else 1
    except KeyboardInterrupt:
        return 130
    finally:
        signin.close(keep=args.keep)


if __name__ == "__main__":
    sys.exit(main())
