"""Shared fake credentials and path isolation for tests and latency measurements."""

import io
import itertools
import os
import time
import urllib.error
from pathlib import Path

from claude_code_accounts import codex, core, keychain, profiles, sessions, transcripts


class FakeKeychain:
    """In-memory stand-in for the login keychain, counting every call."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.log: list[tuple[str, str]] = []

    @property
    def reads(self):
        return sum(op == "read" for op, _ in self.log)

    @property
    def writes(self):
        return sum(op == "write" for op, _ in self.log)

    @property
    def deletes(self):
        return sum(op == "delete" for op, _ in self.log)

    def reset(self):
        self.log.clear()

    def read_raw(self, service):
        self.log.append(("read", service))
        return self.store.get(service)

    def write_raw(self, service, value):
        self.log.append(("write", service))
        self.store[service] = value

    def delete(self, service):
        self.log.append(("delete", service))
        self.store.pop(service, None)
        return True

    def install(self, monkeypatch):
        for name in ("read_raw", "write_raw", "delete"):
            monkeypatch.setattr(keychain, name, getattr(self, name))


class FakeApi:
    """Serves /api/oauth/profile and /api/oauth/usage and the token endpoint from tables."""

    def __init__(self):
        self.login_email: str = ""
        self.emails: dict[str, str] = {}
        self.profiles: dict[str, dict] = {}
        self.usage: dict[str, dict] = {}
        self.rejected: set[str] = set()
        self.fail_next_usage: Exception | None = None
        self._refresh: dict[str, str] = {}
        self._spent: set[str] = set()
        self._generations: dict[str, int] = {}
        self.reset()

    def reset(self):
        self.profile_calls = self.usage_calls = self.refresh_calls = 0

    def get(self, path, token, timeout=20):
        if path == "/api/oauth/profile":
            self.profile_calls += 1
        elif path == "/api/oauth/usage":
            self.usage_calls += 1
        else:
            raise ValueError(f"unexpected API path: {path}")
        if token in self.rejected or token not in self.emails:
            raise urllib.error.HTTPError(core.API + path, 401, "Unauthorized", {}, None)
        email = self.emails[token]
        if path == "/api/oauth/profile":
            return self.profiles.get(email, {
                "account": {"uuid": f"uuid-{email}", "email": email},
                "organization": {"rate_limit_tier": "default_claude_max_5x"},
            })
        if self.fail_next_usage is not None:
            error, self.fail_next_usage = self.fail_next_usage, None
            raise error
        return self.usage.setdefault(email, {"limits": [
            {"kind": "session", "percent": 58, "resets_at": "2100-01-01T00:00:00Z"},
            {"kind": "weekly_all", "percent": 71, "resets_at": "2100-01-03T00:00:00Z"},
            {"kind": "weekly_scoped", "percent": 34, "resets_at": "2100-01-03T00:00:00Z",
             "scope": {"model": {"display_name": "Fable"}}},
        ]})

    def post(self, url, body, token=None, timeout=30, extra_headers=None):
        if body.get("grant_type") == "authorization_code":
            if not self.login_email:
                raise urllib.error.HTTPError(
                    url, 400, "Bad Request", {}, io.BytesIO(b'{"error":"invalid_grant"}'))
            blob = self.blob(self.login_email)
            return {"access_token": blob["accessToken"], "refresh_token": blob["refreshToken"],
                    "expires_in": 3600, "refresh_expires_in": 86400,
                    "scope": "user:inference user:profile"}
        if body.get("grant_type") != "refresh_token":
            raise ValueError("only authorization code and refresh grants are supported")
        self.refresh_calls += 1
        refresh = body["refresh_token"]
        if refresh not in self._refresh or refresh in self._spent:
            raise urllib.error.HTTPError(
                url, 400, "Bad Request", {}, io.BytesIO(b'{"error":"invalid_grant"}'))
        self._spent.add(refresh)
        email = self._refresh[refresh]
        blob = self.blob(email, self._generations[email] + 1)
        return {"access_token": blob["accessToken"], "refresh_token": blob["refreshToken"],
                "expires_in": 3600, "scope": "user:inference user:profile"}

    def blob(self, email, gen=1, expires_in=3600, fresh=True):
        access, refresh = f"{email}-gen{gen}", f"{email}-refresh{gen}"
        self.emails[access] = email
        self._refresh[refresh] = email
        self._spent.discard(refresh)
        self._generations[email] = max(gen, self._generations.get(email, 0))
        return {"accessToken": access, "refreshToken": refresh,
                "expiresAt": int((time.time() + (expires_in if fresh else -3600)) * 1000),
                "subscriptionType": "max", "scopes": ["user:inference", "user:profile"]}

    def install(self, monkeypatch):
        monkeypatch.setattr(core, "_get", self.get)
        monkeypatch.setattr(core, "_post", self.post)


def sign_in(name, email, api, gen=1, fresh=True):
    slot = core.slot_dir(name)
    Path(slot).mkdir(parents=True, exist_ok=True)
    keychain.write_credentials(slot, api.blob(email, gen, fresh=fresh))
    return slot


_pids = itertools.count(10000)


def session(term_id, cwd, account, api=None):
    path = core.prepare_session(term_id, account)
    return sessions.Session(pid=next(_pids), config_dir=path, env_config_dir=path,
                            term_id=term_id, cwd=cwd, name=os.path.basename(cwd),
                            kind="interactive", status="idle")


def redirect_home(home: str, setattr):
    """Keep the bench and tests on the same throwaway paths."""
    keychain.forget()
    home = Path(home)
    for module in (core, codex, profiles):
        setattr(module, "HOME", str(home))
    paths = {
        core: {
            "ACCOUNTS_DIR": home / ".claude-accts",
            "DEFAULT_CONFIG": home / ".claude",
            "USAGE_CACHE": home / ".claude-accts/.usage-cache.json",
            "IDENTITY_CACHE": home / ".claude-accts/.identity.json",
            "CHIP_FILE": home / ".claude-accts/.chips.json",
            "PREFS_FILE": home / ".claude-manager/prefs.json",
            "STASH_DIR": home / ".claude-manager/pending",
            "SESSION_DIRS": home / ".claude-ctx",
        },
        codex: {
            "ACCOUNTS_DIR": home / ".codex-accts",
            "DEFAULT_HOME": home / ".codex",
        },
        profiles: {
            "CCM_HOME": home / ".claude-manager",
            "CONFIG": home / ".claude-manager/config.json",
            "ROUTES": home / ".claude-manager/routes.conf",
            "RESOLVER": home / ".claude-manager/resolve.zsh",
        },
        transcripts: {"TOKEN_CACHE": home / ".claude-accts/.tokens.json"},
    }
    for module, constants in paths.items():
        for name, path in constants.items():
            setattr(module, name, str(path))
    setattr(core, "_CHIPS", (-1.0, {}))
    setattr(transcripts, "_tokens", None)
    setattr(transcripts, "_dirty", False)
    setattr(transcripts, "_paths", {})
    setattr(transcripts, "_digests", {})
