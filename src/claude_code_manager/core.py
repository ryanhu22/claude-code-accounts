"""Accounts, contexts, usage, and swapping.

Two kinds of directory, kept strictly separate:

* **Account slots** (``~/.claude-accts/<name>``) each hold one subscription's
  login and nothing else. No Claude session ever runs in a slot, so its login
  stays valid and its usage is always readable.
* **Contexts** are the config dirs sessions actually run in. They own history
  and settings. Swapping copies a slot's live login into a context.

Nothing here ever mints a credential: it copies whole login blobs that Claude
Code itself wrote, so a swapped context keeps `subscriptionType`, scopes and
rate-limit tier and behaves exactly like a real login.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Iterable, Optional

from . import keychain

HOME = os.path.expanduser("~")
ACCOUNTS_DIR = os.environ.get("CCM_ACCOUNTS_DIR", os.path.join(HOME, ".claude-accts"))
CTX_DIR = os.environ.get("CCM_CONTEXTS_DIR", os.path.join(HOME, ".claude-ctx"))
DEFAULT_CONFIG = os.path.join(HOME, ".claude")
ROUTES_FILE = os.environ.get("CCM_ROUTES", os.path.join(HOME, ".claude", "subs.conf"))
SWAP_LOG = os.path.join(HOME, ".claude", "swap.log")

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TOKEN_URLS = ("https://platform.claude.com/v1/oauth/token",
              "https://console.anthropic.com/v1/oauth/token")
API = "https://api.anthropic.com"
UA = "claude-cli/2.1.236 (external, cli)"
OAUTH_HEADERS = {"anthropic-beta": "oauth-2025-04-20", "User-Agent": UA}


# --------------------------------------------------------------------------- http

def _post(url: str, body: dict, token: Optional[str] = None, timeout: int = 30) -> dict:
    headers = {"Content-Type": "application/json", **OAUTH_HEADERS}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _get(path: str, token: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(API + path, headers={
        "Authorization": f"Bearer {token}", **OAUTH_HEADERS})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# --------------------------------------------------------------------------- tokens

def refresh(blob: dict) -> Optional[dict]:
    """Exchange the refresh token, preserving every other field of the blob."""
    if not blob.get("refreshToken"):
        return None
    for url in TOKEN_URLS:
        try:
            resp = _post(url, {"grant_type": "refresh_token",
                               "refresh_token": blob["refreshToken"],
                               "client_id": CLIENT_ID})
        except Exception:
            continue
        out = dict(blob)
        out["accessToken"] = resp.get("access_token") or blob["accessToken"]
        if resp.get("refresh_token"):
            out["refreshToken"] = resp["refresh_token"]
        out["expiresAt"] = int((time.time() + resp.get("expires_in", 3600)) * 1000)
        return out
    return None


def live_blob(config_dir: str, allow_refresh: bool = True) -> Optional[dict]:
    """Usable credentials for a config dir, refreshed in place when stale.

    Only ever refresh a slot. Refreshing a context could rotate the token out
    from under a session running there.
    """
    blob = keychain.read_credentials(config_dir)
    if not blob:
        return None
    expires = blob.get("expiresAt")
    if allow_refresh and expires and expires / 1000 < time.time() + 120:
        newer = refresh(blob)
        if newer:
            try:
                keychain.write_credentials(config_dir, newer)
            except RuntimeError:
                pass
            return newer
    return blob


def whoami(token: Optional[str]) -> Optional[str]:
    """The account a token belongs to. Identity is never inferred from a name."""
    if not token:
        return None
    try:
        return (_get("/api/oauth/profile", token).get("account") or {}).get("email")
    except Exception:
        return None


# --------------------------------------------------------------------------- model

@dataclass
class Limit:
    kind: str
    label: str
    percent: float
    resets_at: Optional[str]

    @property
    def resets_in(self) -> str:
        return human_delta(self.resets_at)


@dataclass
class Account:
    name: str
    slot: str
    email: Optional[str] = None
    plan: Optional[str] = None
    limits: list[Limit] = field(default_factory=list)
    error: Optional[str] = None
    checked_at: float = 0.0

    def limit(self, kind: str) -> Optional[Limit]:
        return next((l for l in self.limits if l.kind == kind), None)

    @property
    def session_pct(self) -> Optional[float]:
        l = self.limit("session")
        return l.percent if l else None

    @property
    def weekly_pct(self) -> Optional[float]:
        l = self.limit("weekly_all")
        return l.percent if l else None

    @property
    def ok(self) -> bool:
        return self.email is not None and self.error is None


def human_delta(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        dt = _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return ""
    mins = int((dt - _dt.datetime.now(_dt.timezone.utc)).total_seconds() // 60)
    if mins <= 0:
        return "now"
    if mins < 120:
        return f"{mins}m"
    if mins < 48 * 60:
        return f"{mins // 60}h {mins % 60}m"
    return f"{mins // 1440}d {(mins % 1440) // 60}h"


def _parse_limits(data: dict) -> list[Limit]:
    """The `limits` array is what Claude Code's own /usage screen renders.

    A window whose reset time has passed reads as 0%: the server has rolled it
    over even if a fresh fetch has not happened yet.
    """
    out: list[Limit] = []
    now = _dt.datetime.now(_dt.timezone.utc)
    for lim in data.get("limits") or []:
        kind = lim.get("kind") or ""
        label = {"session": "5h", "weekly_all": "7d"}.get(kind)
        if not label:
            model = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
            label = model.lower() if model else kind.replace("_", " ")
        pct = float(lim.get("percent") or 0)
        resets = lim.get("resets_at")
        if resets:
            try:
                if _dt.datetime.fromisoformat(str(resets).replace("Z", "+00:00")) <= now:
                    pct, resets = 0.0, None
            except ValueError:
                pass
        out.append(Limit(kind=kind, label=label, percent=pct, resets_at=resets))
    return out


# --------------------------------------------------------------------------- accounts

def slot_dir(name: str) -> str:
    return os.path.join(ACCOUNTS_DIR, name)


def account_names() -> list[str]:
    try:
        return sorted(n for n in os.listdir(ACCOUNTS_DIR)
                      if os.path.isdir(os.path.join(ACCOUNTS_DIR, n)) and not n.startswith("."))
    except OSError:
        return []


class UnknownAccount(Exception):
    pass


def resolve_account(query: str) -> str:
    """Accept a short nickname for an account slot.

    Exact name wins, then a unique prefix, then a unique substring, so "rr"
    finds "account-c", "200" finds "account-a" and "acme" finds
    "work" without anyone maintaining an alias table.
    """
    names = account_names()
    if query in names:
        return query
    q = query.lower()
    for pool in ([n for n in names if n.lower().startswith(q)],
                 [n for n in names if q in n.lower()]):
        if len(pool) == 1:
            return pool[0]
        if len(pool) > 1:
            raise UnknownAccount(f"{query!r} matches {', '.join(pool)}")
    raise UnknownAccount(f"no account matches {query!r} (have: {', '.join(names) or 'none'})")


def find_live_blob(email: str, prefer: Optional[str] = None) -> Optional[dict]:
    """Any live login for an account, from wherever it currently exists.

    A swap copies one login into a context, so a slot and a context can share a
    refresh-token lineage; whichever refreshes first invalidates the other copy.
    Rather than forcing a re-login, recover from a copy that still works.
    """
    if not email:
        return None
    slots = {os.path.abspath(slot_dir(n)) for n in account_names()}
    candidates = ([prefer] if prefer else []) + [slot_dir(n) for n in account_names()] \
        + [c.path for c in contexts()]
    for cand in dict.fromkeys(c for c in candidates if c):
        # never refresh a context: a session may be running there
        blob = live_blob(cand) if os.path.abspath(cand) in slots else keychain.read_credentials(cand)
        if blob and (whoami(blob.get("accessToken")) or "").lower() == email.lower():
            return blob
    return None


def load_account(name: str, with_usage: bool = True) -> Account:
    slot = slot_dir(name)
    acct = Account(name=name, slot=slot, checked_at=time.time())
    blob = live_blob(slot)
    email = whoami(blob.get("accessToken")) if blob else None
    if not email:
        healed = find_live_blob(recorded_email(slot))
        if healed:
            try:
                keychain.write_credentials(slot, healed)
            except RuntimeError:
                pass
            blob, email = healed, recorded_email(slot)
    if not blob:
        acct.error = "not signed in"
        return acct
    if not email:
        acct.error = "login expired"
        return acct
    acct.email, acct.plan = email, blob.get("subscriptionType")
    if with_usage:
        try:
            acct.limits = _parse_limits(_get("/api/oauth/usage", blob["accessToken"]))
        except urllib.error.HTTPError as e:
            acct.error = f"usage HTTP {e.code}"
        except Exception as e:
            acct.error = str(e)[:60]
    return acct


def recorded_email(config_dir: str) -> str:
    """Account a dir last authenticated as, per Claude Code's own record.

    Only a hint: this file lags the keychain, so it is never used to decide
    what an account IS, only to guess what a broken slot should be.
    """
    path = os.path.join(config_dir, ".claude.json")
    if os.path.abspath(config_dir) == DEFAULT_CONFIG and not os.path.exists(path):
        path = os.path.join(HOME, ".claude.json")
    try:
        with open(path) as f:
            return ((json.load(f).get("oauthAccount") or {}).get("emailAddress") or "").lower()
    except (OSError, ValueError):
        return ""


def add_account_command(name: str) -> str:
    """Shell command that signs an account into its slot (needs a browser)."""
    slot = slot_dir(name)
    return (f'mkdir -p {slot!r} && CLAUDE_CONFIG_DIR={slot!r} claude '
            f'# then type /login, sign in as {name}, and /exit')


def remove_account(name: str) -> bool:
    slot = slot_dir(name)
    ok = keychain.delete(keychain.service_for(slot))
    try:
        import shutil
        shutil.rmtree(slot, ignore_errors=True)
    except Exception:
        pass
    return ok


def poke(name: str) -> tuple[bool, str]:  # noqa: D401
    """Spend one token on an account to start its 5-hour window.

    A freshly reset account sits at 0% with no window running, so the countdown
    only starts on first use. This starts it deliberately, for about 22 input
    tokens, so the window is aligned with when you want it.
    """
    try:
        name = resolve_account(name)
    except UnknownAccount as e:
        return False, str(e)
    blob = live_blob(slot_dir(name))
    if not blob:
        return False, "not signed in"
    try:
        _post(f"{API}/v1/messages", {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1,
            "system": [{"type": "text",
                        "text": "You are Claude Code, Anthropic's official CLI for Claude."}],
            "messages": [{"role": "user", "content": "hi"}],
        }, token=blob["accessToken"], timeout=45)
        return True, "window started"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)[:80]


# --------------------------------------------------------------------------- contexts

@dataclass
class Context:
    name: str
    path: str

    @property
    def email(self) -> str:
        blob = keychain.read_credentials(self.path)
        return (whoami(blob.get("accessToken")) if blob else None) or recorded_email(self.path)


def _routes() -> tuple[dict[str, str], list[tuple[str, str]]]:
    named: dict[str, str] = {}
    paths: list[tuple[str, str]] = []
    try:
        for line in open(ROUTES_FILE):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("path:"):
                pth, dst = line[5:].split("=", 1)
                paths.append((pth.rstrip("/"), dst))
            else:
                k, v = line.split("=", 1)
                named[k] = v
    except OSError:
        pass
    return named, paths


def contexts() -> list[Context]:
    named, paths = _routes()
    out = [Context(name=k, path=v) for k, v in sorted(named.items())]
    if not out:
        out = [Context(name="default", path=DEFAULT_CONFIG)]
    seen = {os.path.abspath(c.path) for c in out}
    for _, dst in paths:
        if os.path.abspath(dst) not in seen:
            out.append(Context(name=os.path.basename(dst.rstrip("/")), path=dst))
            seen.add(os.path.abspath(dst))
    try:
        for n in sorted(os.listdir(CTX_DIR)):
            d = os.path.join(CTX_DIR, n)
            if os.path.isdir(d) and os.path.abspath(d) not in seen:
                out.append(Context(name=n, path=d))
                seen.add(os.path.abspath(d))
    except OSError:
        pass
    return out


def context_for(cwd: str) -> Context:
    """Which context a directory runs in. Longest matching rule wins."""
    named, paths = _routes()
    cands = {os.path.abspath(cwd), os.path.realpath(cwd)}
    best, best_len = None, -1
    for pth, dst in paths:
        if any((c + "/").startswith(pth + "/") for c in cands) and len(pth) > best_len:
            best, best_len = dst, len(pth)
    if best:
        return Context(name=os.path.basename(best.rstrip("/")), path=best)
    work = named.get("work")
    if work and any((c + "/").startswith(os.path.join(HOME, "Desktop", "acme")) for c in cands):
        return Context(name="work", path=work)
    return Context(name="default", path=named.get("default", DEFAULT_CONFIG))


SHARED_ITEMS = ("CLAUDE.md", "commands", "agents", "skills", "hooks", "workflows",
                "bin", "automations", "settings.json", "design", "appstore",
                "plugins", "projects")


def project_root(cwd: str) -> str:
    """Main checkout for a directory: a worktree resolves to its parent repo."""
    import subprocess as _sp
    r = _sp.run(["git", "rev-parse", "--git-common-dir"], cwd=cwd,
                capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        gitdir = r.stdout.strip()
        if not os.path.isabs(gitdir):
            gitdir = os.path.join(cwd, gitdir)
        return os.path.dirname(os.path.abspath(gitdir)) or cwd
    return os.path.abspath(cwd)


def _write_route(root: str, context_path: Optional[str]) -> None:
    lines: list[str] = []
    try:
        with open(ROUTES_FILE) as f:
            lines = [l.rstrip("\n") for l in f]
    except OSError:
        pass
    key = f"path:{root}="
    lines = [l for l in lines if not l.startswith(key)]
    if context_path:
        lines.append(key + context_path)
    os.makedirs(os.path.dirname(ROUTES_FILE), exist_ok=True)
    with open(ROUTES_FILE, "w") as f:
        f.write("\n".join(l for l in lines if l.strip()) + "\n")


def isolate(cwd: str) -> tuple[bool, str]:
    """Give this project its own context, so swapping here affects only it.

    Shared config and the transcripts directory are symlinked back to
    ~/.claude, so settings, commands and history stay in one place and
    `claude -c` still finds past conversations after the move.
    """
    root = project_root(cwd)
    name = "".join(c if (c.isalnum() or c in "._-") else "-" for c in os.path.basename(root)) or "project"
    ctx_path = os.path.join(CTX_DIR, name)
    current = context_for(root)
    if os.path.abspath(ctx_path) == os.path.abspath(current.path):
        return True, f"{root} already has its own context"
    os.makedirs(ctx_path, exist_ok=True)
    for item in SHARED_ITEMS:
        target = os.path.join(DEFAULT_CONFIG, item)
        link = os.path.join(ctx_path, item)
        if os.path.exists(target) and not os.path.lexists(link):
            os.symlink(target, link)
    blob = keychain.read_credentials(current.path)
    if blob:
        keychain.write_credentials(ctx_path, blob)      # start where it already was
    _write_route(root, ctx_path)
    who = whoami(blob.get("accessToken")) if blob else None
    return True, (f"{root}\n  own context: {ctx_path.replace(HOME, '~')}"
                  f"\n  account    : {who or 'unknown'}")


def unroute(cwd: str) -> tuple[bool, str]:
    """Drop this project's routing override; it follows the defaults again."""
    root = project_root(cwd)
    _write_route(root, None)
    return True, f"override removed for {root}"


def swap(account: str, context: Context) -> tuple[bool, str]:
    """Point a context at an account by copying that account's live login."""
    try:
        account = resolve_account(account)
    except UnknownAccount as e:
        return False, str(e)
    blob = live_blob(slot_dir(account))
    email = whoami(blob.get("accessToken")) if blob else None
    if not email:
        healed = find_live_blob(recorded_email(slot_dir(account)))
        if healed:
            blob, email = healed, whoami(healed.get("accessToken"))
    if not blob or not email:
        return False, f"{account} has no usable login (add it again)"
    before = context.email or "unknown"
    try:
        keychain.write_credentials(context.path, blob)
    except RuntimeError as e:
        return False, str(e)
    try:
        with open(SWAP_LOG, "a") as f:
            f.write(f"{_dt.datetime.now().isoformat(timespec='seconds')}  "
                    f"{context.path.replace(HOME, '~')}  {before} -> {email}  via=menubar\n")
    except OSError:
        pass
    return True, f"{context.name} now uses {email}"
