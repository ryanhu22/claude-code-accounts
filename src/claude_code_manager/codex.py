"""Codex accounts, each with one home and one copy of its login.

Each account owns a CODEX_HOME under ``~/.codex-accts/<name>``. Only auth.json
belongs to that account; everything else is symlinked back to ``~/.codex`` so
settings and history stay shared. The default login is tracked through a
symlink too, because copying its refresh token would leave two homes racing
to spend the same single-use grant.

Nothing here mints a credential except the sign-in exchange, mirroring
`oauth.py`. Most reads simply use auth.json as it stands: a Codex process
running in that home already knows how to refresh it.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

HOME = os.path.expanduser("~")
DEFAULT_HOME = os.path.join(HOME, ".codex")
ACCOUNTS_DIR = os.environ.get("CCM_CODEX_ACCOUNTS_DIR", os.path.join(HOME, ".codex-accts"))
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPES = "openid profile email offline_access api.connectors.read api.connectors.invoke"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
UA_FALLBACK_VERSION = "0.153.0"
PROVIDER = "codex"

_AUTH_CLAIM = "https://api.openai.com/auth"
_PROFILE_CLAIM = "https://api.openai.com/profile"
_UA: str | None = None
_RUNNING: tuple[float, set[str]] = (-1.0, set())


def user_agent() -> str:
    """Use the installed CLI's version so the server sees a familiar client."""
    global _UA
    if _UA is None:
        version = UA_FALLBACK_VERSION
        try:
            out = subprocess.run(["codex", "--version"], capture_output=True,
                                 text=True, timeout=5).stdout
            found = re.search(r"(\d+\.\d+\.\d+)", out)
            if found:
                version = found.group(1)
        except (OSError, subprocess.SubprocessError):
            pass
        _UA = f"codex_cli_rs/{version}"
    return _UA


def account_names() -> list[str]:
    try:
        return sorted(n for n in os.listdir(ACCOUNTS_DIR)
                      if not n.startswith(".") and os.path.isdir(slot_dir(n)))
    except OSError:
        return []


def slot_dir(name: str) -> str:
    return os.path.join(ACCOUNTS_DIR, name)


def is_account_dir(path: str) -> bool:
    return os.path.dirname(os.path.abspath(path)) == os.path.abspath(ACCOUNTS_DIR)


def shared_items() -> list[str]:
    try:
        return sorted(n for n in os.listdir(DEFAULT_HOME)
                      if n != "auth.json" and not n.endswith(".lock"))
    except OSError:
        return []


def ensure_account_dir(name: str) -> str:
    """Link new shared files on every load, since Codex adds them over time."""
    path = slot_dir(name)
    if os.path.islink(path):
        return path
    os.makedirs(ACCOUNTS_DIR, mode=0o700, exist_ok=True)
    os.makedirs(path, mode=0o700, exist_ok=True)
    for item in shared_items():
        link = os.path.join(path, item)
        if not os.path.lexists(link):
            try:
                os.symlink(os.path.join(DEFAULT_HOME, item), link)
            except FileExistsError:
                pass                 # another load linked it while we were looking
    return path


def adopt_default() -> str | None:
    """Track the existing login without copying its refresh-token lineage."""
    if not os.path.exists(os.path.join(DEFAULT_HOME, "auth.json")):
        return None
    if any(os.path.realpath(slot_dir(n)) == os.path.realpath(DEFAULT_HOME)
           for n in account_names()):
        return None
    os.makedirs(ACCOUNTS_DIR, mode=0o700, exist_ok=True)
    name = "codex" if not os.path.lexists(slot_dir("codex")) else "codex-main"
    try:
        os.symlink(DEFAULT_HOME, slot_dir(name))
    except FileExistsError:
        return None
    return name


def read_auth(home: str) -> dict | None:
    try:
        with open(os.path.join(home, "auth.json")) as f:
            data = json.load(f)
        tokens = data.get("tokens") if isinstance(data, dict) else None
        access = tokens.get("access_token") if isinstance(tokens, dict) else None
        return data if isinstance(access, str) and access else None
    except (OSError, ValueError):
        return None


def write_auth(home: str, data: dict) -> None:
    path = os.path.join(home, "auth.json")
    tmp = path + ".tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        os.fchmod(f.fileno(), 0o600)
        json.dump(data, f)
    os.replace(tmp, path)


def claims(jwt: str) -> dict:
    """Read the identity snapshot locally; a usage request proves a new login."""
    try:
        payload = jwt.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def plan_label(key: str) -> str:
    labels = {
        "prolite": "Pro Lite", "plus": "Plus", "pro": "Pro", "free": "Free",
        "go": "Go", "team": "Team", "business": "Business", "enterprise": "Enterprise",
        "edu": "Edu", "edu_plus": "Edu Plus", "edu_pro": "Edu Pro",
        "self_serve_business_prolite": "Business Pro Lite",
        "self_serve_business_usage_based": "Business",
        "enterprise_cbp_automation": "Enterprise", "enterprise_cbp_usage_based": "Enterprise",
        "ent26": "Enterprise", "unknown": "", "": "",
    }
    key = key or ""
    return labels.get(key, " ".join(key.split("_")).title())


def identity(auth: dict) -> dict:
    tokens = auth.get("tokens") or {}
    ident = claims(tokens.get("id_token"))
    access = claims(tokens.get("access_token"))
    primary = ident.get(_AUTH_CLAIM) or {}
    fallback = access.get(_AUTH_CLAIM) or {}
    plan = primary.get("chatgpt_plan_type") or fallback.get("chatgpt_plan_type") or ""
    return {
        "email": ident.get("email") or access.get("email")
                 or (access.get(_PROFILE_CLAIM) or {}).get("email") or "",
        "plan": plan_label(plan), "plan_key": plan,
        "account_id": primary.get("chatgpt_account_id")
                      or fallback.get("chatgpt_account_id") or tokens.get("account_id") or "",
        "exp": access.get("exp"),
    }


def expiring(auth: dict, margin: float = 86400) -> bool:
    exp = claims((auth.get("tokens") or {}).get("access_token")).get("exp")
    try:
        return not exp or float(exp) <= time.time() + margin
    except (TypeError, ValueError):
        return True


def running_homes() -> set[str]:
    """Leave refresh to any Codex process already using the home.

    Process environments are only read locally and never logged. A short cache
    avoids asking ps once per account on every pass.
    """
    global _RUNNING
    now = time.monotonic()
    if _RUNNING[0] >= 0 and now - _RUNNING[0] < 5:
        return set(_RUNNING[1])
    homes: set[str] = set()
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                             text=True, timeout=5).stdout
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            pid, command = parts
            executable = os.path.basename(command.split(None, 1)[0])
            if executable not in ("codex", "codex.js") and "/@openai/codex/" not in command:
                continue
            try:
                env = subprocess.run(["ps", "eww", "-p", pid], capture_output=True,
                                     text=True, timeout=5).stdout
            except (OSError, subprocess.SubprocessError):
                env = ""
            found = re.search(r"(?:^|\s)CODEX_HOME=(.*?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)",
                              env.strip())
            homes.add(os.path.abspath(found.group(1) if found and found.group(1) else DEFAULT_HOME))
    except (OSError, subprocess.SubprocessError):
        pass
    _RUNNING = (now, homes)
    return set(homes)


def _post(body: dict, form: bool = False) -> dict:
    data = urllib.parse.urlencode(body) if form else json.dumps(body)
    content_type = "application/x-www-form-urlencoded" if form else "application/json"
    req = urllib.request.Request(TOKEN_URL, data=data.encode(),
                                 headers={"Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _stamp() -> str:
    now = _dt.datetime.now(_dt.timezone.utc)
    return now.isoformat(timespec="microseconds").replace("+00:00", "Z")


def refresh(auth: dict) -> tuple[dict | None, str | None]:
    """Only an explicit invalid_grant says the stored lineage is spent.

    As on the Claude side, a nested error or a failed connection proves
    nothing about the login. Never turn either into a request to sign in.
    """
    tokens = auth.get("tokens") or {}
    if not tokens.get("refresh_token"):
        return None, "no_refresh_token"
    body = {"client_id": CLIENT_ID, "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"], "scope": "openid profile email"}
    for form in (False, True):
        try:
            resp = _post(body, form=form)
            if not isinstance(resp, dict) or not resp.get("access_token"):
                return None, "transient"
            rotated = dict(tokens)
            for key in ("access_token", "refresh_token", "id_token"):
                if resp.get(key):
                    rotated[key] = resp[key]
            return {**auth, "tokens": rotated, "last_refresh": _stamp()}, None
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read()).get("error")
            except Exception:
                err = None
            if e.code in (400, 401) and err in ("invalid_grant", "invalid_client"):
                return None, err
            if e.code != 400 or form:
                return None, "transient"
        except Exception:
            return None, "transient"
    return None, "transient"


def live_auth(home: str) -> dict | None:
    """Refresh only an idle home, with one manager holding the grant at a time.

    Claude's copied lineages taught us that single-use refresh tokens cannot
    be raced. A running Codex owns refresh in its home; our lock only keeps
    two manager processes from spending a token together when Codex is absent.
    Re-read under that lock because another manager may have just rotated it.
    """
    auth = read_auth(home)
    if not auth or not expiring(auth):
        return auth
    if os.path.realpath(home) in {os.path.realpath(p) for p in running_homes()}:
        return auth
    lock = os.path.join(home, "auth.json.ccm.lock")
    try:
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if time.time() - os.path.getmtime(lock) <= 60:
                return auth
            os.unlink(lock)
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return auth
    os.close(fd)
    try:
        current = read_auth(home)
        if not current or not expiring(current):
            return current
        rotated, err = refresh(current)
        if not rotated:
            return None if err == "invalid_grant" else current
        write_auth(home, rotated)
        return rotated
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


def fetch_usage(auth: dict) -> dict:
    tokens = auth["tokens"]
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {tokens['access_token']}",
        "ChatGPT-Account-Id": tokens.get("account_id") or identity(auth)["account_id"],
        "Accept": "application/json", "User-Agent": user_agent(),
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def short_name(name: str) -> str:
    name = re.sub(r"^GPT-", "", name or "", flags=re.IGNORECASE)
    parts = [p for p in name.split("-") if p and p.lower() != "codex"]
    return parts[-1].lower()[:5] if parts else ""


def _span_label(seconds: int) -> str:
    for span, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds and seconds % span == 0:
            return f"{seconds // span}{unit}"
    return f"{seconds}s"


def parse_limits(data: dict) -> list:
    """Classify windows by length, since their positions differ between plans."""
    from .core import Limit

    now = _dt.datetime.now(_dt.timezone.utc)

    def windows(rate: dict, scope: str = "", scoped: bool = False) -> list:
        out = []
        for key in ("primary_window", "secondary_window"):
            window = rate.get(key)
            if not window:
                continue
            secs = int(window.get("limit_window_seconds") or 0)
            if scoped:
                kind = {18000: "scoped_session", 604800: "scoped_weekly"}.get(
                    secs, f"scoped_{secs}")
            else:
                kind = {18000: "session", 604800: "weekly_all"}.get(secs, f"window_{secs}")
            percent = float(window.get("used_percent") or 0)
            unused = percent == 0 and (window.get("reset_after_seconds") or 0) >= secs
            resets = None
            if not unused and window.get("reset_at") is not None:
                when = _dt.datetime.fromtimestamp(window["reset_at"], _dt.timezone.utc)
                if when <= now:
                    percent = 0.0
                else:
                    resets = when.isoformat()
            out.append(Limit(kind=kind, label=scope if scoped else _span_label(secs),
                             percent=percent, resets_at=resets, span=secs, scope=scope))
        return sorted(out, key=lambda lim: lim.span)

    out = windows(data.get("rate_limit") or {})
    scoped = []
    for entry in data.get("additional_rate_limits") or []:
        scoped.extend(windows(
            entry.get("rate_limit") or {}, short_name(entry.get("limit_name")), True))
    return out + sorted(scoped, key=lambda lim: lim.span)


def extras(data: dict) -> dict:
    rate = data.get("rate_limit") or {}
    credits = data.get("credits") or {}
    resets = data.get("rate_limit_reset_credits") or {}
    return {
        "has_5h": any((rate.get(k) or {}).get("limit_window_seconds") == 18000
                      for k in ("primary_window", "secondary_window")),
        "credits_balance": str(credits.get("balance") or ""),
        "has_credits": bool(credits.get("has_credits")),
        "unlimited_credits": bool(credits.get("unlimited")),
        "reset_credits": int(resets.get("available_count") or 0),
        "reset_credits_applicable": int(resets.get("applicable_available_count") or 0),
        "limit_reached": bool(rate.get("limit_reached")),
    }


@dataclass
class Attempt:
    verifier: str
    state: str
    account: str
    started_at: float = field(default_factory=time.time)

    @property
    def url(self) -> str:
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip("=")
        query = urllib.parse.urlencode({
            "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI,
            "scope": SCOPES, "code_challenge": challenge, "code_challenge_method": "S256",
            "id_token_add_organizations": "true", "codex_cli_simplified_flow": "true",
            "state": self.state, "originator": "codex_cli_rs",
        })
        return f"{AUTHORIZE_URL}?{query}"


def begin(account: str) -> Attempt:
    return Attempt(verifier=secrets.token_urlsafe(64), state=secrets.token_urlsafe(24),
                   account=account)


def finish(attempt: Attempt, code: str, state: str) -> tuple[dict | None, str]:
    """Save no login until its usage endpoint confirms whose login it is."""
    if not code:
        return None, "no code was returned"
    if not state or state != attempt.state:
        return None, "that code belongs to a different sign-in; start again"
    body = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID, "code_verifier": attempt.verifier}
    try:
        try:
            resp = _post(body, form=True)
        except urllib.error.HTTPError as e:
            if e.code != 400:
                raise
            resp = _post(body)
    except Exception:
        return None, "the code was refused or could not be exchanged. Try signing in again"
    if not isinstance(resp, dict) or not all(isinstance(resp.get(k), str) and resp[k]
            for k in ("access_token", "refresh_token", "id_token")):
        return None, "the response carried no complete token set"
    auth = {
        "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
        "tokens": {"id_token": resp["id_token"], "access_token": resp["access_token"],
                   "refresh_token": resp["refresh_token"],
                   "account_id": (claims(resp["id_token"]).get(_AUTH_CLAIM) or {}).get(
                       "chatgpt_account_id")},
        "last_refresh": _stamp(),
    }
    try:
        email = fetch_usage(auth).get("email")
    except Exception:
        email = None
    if not email:
        return None, "signed in, but usage could not confirm the email. The login was not saved"
    return auth, email


def remove_account(name: str) -> bool:
    path = slot_dir(name)
    try:
        if os.path.islink(path):
            os.unlink(path)
        else:
            shutil.rmtree(path)
    except OSError:
        return False
    return True


def rename_account(old: str, new: str) -> tuple[bool, str]:
    new = "".join(c for c in new.strip() if c.isalnum() or c in "-_")
    if not new:
        return False, "name must contain letters, digits, - or _"
    if new == old:
        return True, "unchanged"
    if os.path.lexists(slot_dir(new)):
        return False, f"{new} already exists"
    try:
        os.rename(slot_dir(old), slot_dir(new))
    except OSError:
        return False, f"could not rename {old}"
    return True, f"{old} is now {new}"
