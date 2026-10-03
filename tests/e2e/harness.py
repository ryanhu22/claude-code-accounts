"""A sandbox that the real `ccm` command runs in, with no way out of it.

HOME is a temp dir, every external tool ccm shells out to is a stub first on
PATH (`stubs/`), the keychain is a JSON file behind a fake `security`, and
the CCM_* overrides point every URL at the fake server. On top of that the
proxy variables send any request for a real host to a closed loopback port,
so a URL that escaped the overrides fails at once rather than leaving the
machine.

Importable without pytest, so `serve_signin.py` can build the same sandbox
for a browser test.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from e2e.fake_server import FakeServer

STUBS = Path(__file__).resolve().parent / "stubs"
TOOLS = ("claude", "codex", "open", "ps", "lsof", "pmset", "osascript", "launchctl")
# A closed port: anything that still tries a real host gets "connection
# refused" from the kernel instead of a route out.
DEAD_PROXY = "http://127.0.0.1:9"
TERM_ID = "E2E00000-0000-4000-8000-000000000001"


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    """The text without its colour codes, as a person reads it."""
    return ANSI.sub("", text)


def ccm_command() -> list[str]:
    """The installed `ccm` entry point of the interpreter running the tests."""
    exe = os.path.join(os.path.dirname(sys.executable), "ccm")
    if os.access(exe, os.X_OK):
        return [exe]
    return [sys.executable, "-m", "claude_code_accounts.cli"]


def _opener() -> urllib.request.OpenerDirector:
    # No proxies, whatever the environment says: these calls are to loopback.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(url: str, timeout: float = 10.0) -> tuple[int, str]:
    """GET a loopback URL, following redirects. Returns (status, body)."""
    with _opener().open(url, timeout=timeout) as r:
        return r.status, r.read().decode(errors="replace")


class Sandbox:
    def __init__(self, root: str | os.PathLike, server: FakeServer) -> None:
        # The real path: a temp dir under /var is really under /private/var,
        # and ccm compares the cwd it is given with the HOME it is given.
        self.root = os.path.realpath(str(root))
        self.server = server
        self.home = os.path.join(self.root, "home")
        self.bin = os.path.join(self.root, "bin")
        self.keychain_file = os.path.join(self.root, "keychain.json")
        self._children: list[subprocess.Popen] = []
        for d in (self.home, self.bin, os.path.join(self.root, "tmp"),
                  os.path.join(self.home, ".claude"), os.path.join(self.home, ".codex")):
            os.makedirs(d, exist_ok=True)
        self._write_wrappers()
        self.env: dict[str, str] = {
            "HOME": self.home, "USER": "e2e", "LOGNAME": "e2e",
            "TMPDIR": os.path.join(self.root, "tmp"),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "PYTHONIOENCODING": "utf-8",
            "TERM_SESSION_ID": TERM_ID,
            "CCM_E2E_SANDBOX": self.root, "CCM_E2E_KEYCHAIN_FILE": self.keychain_file,
            **server.env(),
            "http_proxy": DEAD_PROXY, "https_proxy": DEAD_PROXY,
            "HTTP_PROXY": DEAD_PROXY, "HTTPS_PROXY": DEAD_PROXY,
            "no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1,localhost",
        }

    def _write_wrappers(self) -> None:
        python = sys.executable
        for name in ("security", *TOOLS):
            script = STUBS / ("security.py" if name == "security" else "tools.py")
            arg = "" if name == "security" else f" {name}"
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(f'#!/bin/sh\nexec "{python}" "{script}"{arg} "$@"\n')
            os.chmod(path, 0o755)
        # `ccm` itself, for the shell wrapper: the generated resolver calls it
        # by name, the way a user's shell does.
        path = os.path.join(self.bin, "ccm")
        with open(path, "w") as f:
            f.write("#!/bin/sh\nexec " + " ".join(f'"{c}"' for c in ccm_command()) + ' "$@"\n')
        os.chmod(path, 0o755)

    # ------------------------------------------------------------- paths

    def slot(self, name: str) -> str:
        return os.path.join(self.home, ".claude-accts", name)

    def codex_slot(self, name: str) -> str:
        return os.path.join(self.home, ".codex-accts", name)

    @property
    def default_config(self) -> str:
        return os.path.join(self.home, ".claude")

    # ------------------------------------------------------------- running ccm

    def run(self, *args: str, input: str | None = None, timeout: float = 60.0,
            cwd: str | None = None, env: dict[str, str] | None = None,
            ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*ccm_command(), *args], input=input, capture_output=True, encoding="utf-8",
            errors="replace", timeout=timeout, cwd=cwd or self.home,
            env={**self.env, **(env or {})})

    def popen(self, *args: str, cwd: str | None = None, env: dict[str, str] | None = None,
              ) -> subprocess.Popen:
        return subprocess.Popen(
            [*ccm_command(), *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, encoding="utf-8", errors="replace", cwd=cwd or self.home,
            env={**self.env, **(env or {})})

    def python(self, code: str, env: dict[str, str] | None = None,
               timeout: float = 60.0) -> subprocess.CompletedProcess:
        """Run a snippet with the package importable, in the sandbox environment."""
        return subprocess.run([sys.executable, "-c", code], capture_output=True, encoding="utf-8",
                              errors="replace", timeout=timeout, cwd=self.home,
                              env={**self.env, **(env or {})})

    # ------------------------------------------------------------- what happened

    def _lines(self, name: str) -> list[str]:
        try:
            with open(os.path.join(self.root, name)) as f:
                return [line.rstrip("\n") for line in f if line.strip()]
        except OSError:
            return []

    def opened_urls(self) -> list[str]:
        return self._lines("open-urls.log")

    def tool_calls(self, tool: str | None = None) -> list[dict]:
        calls = [json.loads(line) for line in self._lines("tools.log")]
        return [c for c in calls if tool is None or c["tool"] == tool]

    def codex_execs(self) -> list[dict]:
        return [json.loads(line) for line in self._lines("codex-exec.log")]

    def tripwire(self) -> list[dict]:
        return [json.loads(line) for line in self._lines("tripwire.log")]

    def keychain(self) -> dict[str, dict]:
        try:
            with open(self.keychain_file) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def keychain_log(self) -> list[dict]:
        return [json.loads(line) for line in self._lines("keychain.json.log")]

    def blob(self, config_dir: str) -> dict | None:
        """The claudeAiOauth credential a config dir holds, from the fake keychain."""
        from claude_code_accounts import keychain

        item = self.keychain().get(keychain.service_for(config_dir))
        if not item:
            return None
        return json.loads(item["secret"]).get("claudeAiOauth")

    # ------------------------------------------------------------- seeding

    def seed_claude(self, name: str, email: str, **fields) -> dict:
        """A Claude account signed in already, without the browser: the fast path."""
        from claude_code_accounts import keychain

        if email not in self.server.claude:
            self.server.add_claude(email, **fields)
        blob = self.server.blob(email)
        slot = self.slot(name)
        os.makedirs(slot, exist_ok=True)
        profile = self.server.claude[email].profile()
        with open(os.path.join(slot, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"accountUuid": profile["account"]["uuid"],
                                        "emailAddress": email,
                                        "organizationUuid": profile["organization"]["uuid"]}}, f)
        self._put_item(keychain.service_for(slot), json.dumps({"claudeAiOauth": blob}))
        return blob

    def seed_codex(self, name: str, email: str, **fields) -> dict:
        """A Codex account signed in already, by writing its auth.json."""
        if email not in self.server.codex:
            self.server.add_codex(email, **fields)
        auth = self.server.codex_auth(email)
        home = self.codex_slot(name)
        os.makedirs(home, mode=0o700, exist_ok=True)
        with open(os.path.join(home, "auth.json"), "w") as f:
            json.dump(auth, f)
        os.chmod(os.path.join(home, "auth.json"), 0o600)
        return auth

    def _put_item(self, service: str, secret: str) -> None:
        items = self.keychain()
        items[service] = {"account": self.env["USER"], "secret": secret}
        with open(self.keychain_file, "w") as f:
            json.dump(items, f)

    def seed_session(self, config_dir: str, cwd: str, term_id: str | None = TERM_ID,
                     name: str = "", status: str = "idle", kind: str = "interactive",
                     env_config_dir: str | None = None) -> int:
        """A Claude Code session that is running right now, as ccm sees one.

        Claude Code registers a session as `<config dir>/sessions/<id>.json`
        with its pid, and ccm asks `ps` for that process's environment. The
        process is a `sleep` started here (killed by `close()`), and the fake
        `ps` answers for it from `ps.json` with the CLAUDE_CONFIG_DIR and
        TERM_SESSION_ID given. Returns the pid.
        """
        proc = subprocess.Popen(["/bin/sleep", "600"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._children.append(proc)
        now = int(time.time() * 1000)
        sid = f"e2e-{proc.pid}"
        os.makedirs(os.path.join(config_dir, "sessions"), exist_ok=True)
        with open(os.path.join(config_dir, "sessions", sid + ".json"), "w") as f:
            json.dump({"pid": proc.pid, "sessionId": sid, "cwd": cwd, "name": name,
                       "kind": kind, "status": status, "startedAt": now, "updatedAt": now,
                       "entrypoint": "cli", "nameSource": "user" if name else "derived"}, f)
        env = {"CLAUDE_CONFIG_DIR": env_config_dir or config_dir, "TERM_PROGRAM": "iTerm.app"}
        if term_id:
            env["TERM_SESSION_ID"] = term_id
        self.register_process(proc.pid, env, command="claude", tty="ttys001")
        return proc.pid

    def register_process(self, pid: int, env: dict[str, str], command: str = "",
                         tty: str = "??") -> None:
        """Tell the fake `ps` about a process (see stubs/tools.py)."""
        path = os.path.join(self.root, "ps.json")
        try:
            with open(path) as f:
                procs = json.load(f)
        except (OSError, ValueError):
            procs = {}
        procs[str(pid)] = {"env": env, "command": command, "tty": tty}
        with open(path, "w") as f:
            json.dump(procs, f)

    def close(self) -> None:
        """Kill the processes `seed_session` started."""
        for proc in self._children:
            proc.kill()
            proc.wait()
        self._children.clear()

    # ------------------------------------------------------------- the real sign-in

    def wait_for_url(self, proc: subprocess.Popen, seen: int, timeout: float) -> str:
        """The next URL ccm asks a browser for, or why it never asked."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            urls = self.opened_urls()
            if len(urls) > seen:
                return urls[seen]
            if proc.poll() is not None:
                out, err = proc.communicate()
                raise RuntimeError(f"ccm exited {proc.returncode} before opening a browser\n"
                                   f"stdout: {out}\nstderr: {err}")
            time.sleep(0.05)
        proc.kill()
        raise TimeoutError("ccm never asked to open a browser")

    def approve(self, authorize_url: str) -> tuple[int, str]:
        """Do what a person does on the sign-in page: press Authorize.

        Follows the redirect to ccm's loopback callback, so when this returns
        ccm has the code. For the hosted (paste) flow there is no redirect:
        the body carries the `code#state` to paste.
        """
        _status, page = fetch(authorize_url)
        found = re.search(r'id="authorize" href="([^"]+)"', page)
        if not found:
            raise RuntimeError(f"no Authorize button on the page:\n{page}")
        return fetch(self.server.url + found.group(1))

    def sign_in(self, name: str, browser: str = "", paste: bool = False,
                timeout: float = 30.0) -> subprocess.CompletedProcess:
        """`ccm login <name>` end to end, with the browser's part played here."""
        args = ["login", name, *(["--browser", browser] if browser else []),
                *(["--paste"] if paste else [])]
        seen = len(self.opened_urls())
        proc = self.popen(*args)
        url = self.wait_for_url(proc, seen, timeout)
        pasted = None
        _status, body = self.approve(url)
        if paste:
            shown = re.search(r'id="code">([^<]*)<', body)
            pasted = (shown.group(1) if shown else "") + "\n"
        out, err = proc.communicate(input=pasted, timeout=timeout)
        return subprocess.CompletedProcess(args, proc.returncode, out, err)

    def sign_in_codex(self, name: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
        """`ccm login <name> --codex` end to end. Needs port 1455, as Codex does.

        Another sign-in on this machine (a test running beside this one) can
        hold the port for a moment, so a refusal for that reason is retried
        for up to `timeout` seconds before it counts.
        """
        args = ["login", name, "--codex"]
        deadline = time.monotonic() + timeout
        while True:
            seen = len(self.opened_urls())
            proc = self.popen(*args)
            try:
                url = self.wait_for_url(proc, seen, timeout)
                break
            except RuntimeError as e:
                if "port 1455 is in use" not in str(e) or time.monotonic() > deadline:
                    raise
                time.sleep(1)
        self.approve(url)
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(args, proc.returncode, out, err)

    # ------------------------------------------------------------- in-process use

    def apply_in_process(self, setenv, setattr) -> None:
        """Point this process at the sandbox too, for menu bar code run in-process.

        `setenv` and `setattr` are pytest's monkeypatch methods, so the change
        is undone with the test. The module constants are what the CCM_*
        variables would have set at import time.
        """
        from claude_code_accounts import codex, core, oauth

        for key, value in self.env.items():
            setenv(key, value)
        urls = self.server.env()
        setattr(core, "API", urls["CCM_API_BASE"])
        setattr(core, "TOKEN_URLS", (urls["CCM_TOKEN_URL"],))
        setattr(oauth, "AUTHORIZE_URL", urls["CCM_AUTHORIZE_URL"])
        setattr(codex, "AUTHORIZE_URL", urls["CCM_CODEX_AUTH_BASE"] + "/oauth/authorize")
        setattr(codex, "TOKEN_URL", urls["CCM_CODEX_AUTH_BASE"] + "/oauth/token")
        setattr(codex, "USAGE_URL", urls["CCM_CODEX_API_BASE"] + "/backend-api/wham/usage")
        setattr(codex, "RESET_CREDITS_URL",
                urls["CCM_CODEX_API_BASE"] + "/backend-api/wham/rate-limit-reset-credits")
        setattr(core, "_UA", None)
        setattr(codex, "_UA", None)

    def remove(self) -> None:
        self.close()
        shutil.rmtree(self.root, ignore_errors=True)
