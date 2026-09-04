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
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Iterable, Optional

from . import keychain, locks, sessions

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

def fingerprint(blob: Optional[dict]) -> Optional[str]:
    """Identity of a credential GENERATION, from its refresh token.

    Two config dirs showing the same fingerprint hold the same copy of one
    login, so a rotation in either one strands the other.
    """
    token = (blob or {}).get("refreshToken")
    return hashlib.sha256(token.encode()).hexdigest()[:16] if token else None


def expiring(blob: Optional[dict], margin: float = 120) -> bool:
    exp = (blob or {}).get("expiresAt")
    return bool(exp) and exp / 1000 < time.time() + margin


def _apply(blob: dict, resp: dict) -> dict:
    """A blob carrying the tokens from a grant response, other fields intact."""
    out = dict(blob)
    out["accessToken"] = resp.get("access_token") or blob.get("accessToken")
    if resp.get("refresh_token"):
        out["refreshToken"] = resp["refresh_token"]
    out["expiresAt"] = int((time.time() + resp.get("expires_in", 3600)) * 1000)
    if resp.get("scope"):
        out["scopes"] = resp["scope"].split()
    return out


def refresh(blob: dict) -> tuple[Optional[dict], Optional[str]]:
    """Exchange a refresh token. Returns (grant response, error).

    The error is classified because the two failures need opposite handling. A
    network blip must never be read as a dead login: the token is still good
    and the next pass will use it. Only the server explicitly rejecting the
    grant (RFC 6749 invalid_grant) proves the refresh lineage is spent, which
    is what happens when something else already rotated this token.

    The verdict comes from the top-level `error` member of the JSON body, not
    from scanning the text: the marker can appear inside another envelope, and
    calling a live token dead costs the user a login.
    """
    if not blob.get("refreshToken"):
        return None, "no_refresh_token"
    last = "transient"
    for url in TOKEN_URLS:
        try:
            return _post(url, {"grant_type": "refresh_token",
                               "refresh_token": blob["refreshToken"],
                               "client_id": CLIENT_ID}, timeout=15), None
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode(errors="replace")
            except Exception:
                pass
            if e.code in (400, 401, 403):
                try:
                    err = json.loads(body).get("error")
                except (ValueError, AttributeError):
                    err = None
                if err == "invalid_grant":
                    return None, "invalid_grant"
                if err == "invalid_client":
                    return None, "invalid_client"   # our client, not this login
        except Exception:
            pass
    return None, last


def token_account(resp: dict) -> Optional[str]:
    """The email a grant response names, when it names one.

    Identity for free, and from the same exchange that produced the token, so
    it cannot describe a different account than the one we just wrote.
    """
    acct = resp.get("account")
    if not isinstance(acct, dict):
        return None
    return acct.get("email_address") or acct.get("email")


def carry_identity(config_dir: str, old_fp: Optional[str], new_fp: Optional[str]) -> None:
    """Move a cached identity onto a rotated credential.

    Refreshing a token never changes whose token it is, so re-asking the server
    after every rotation would be pure waste. Only move an entry that really
    described the generation we just spent.
    """
    if not old_fp or not new_fp or old_fp == new_fp:
        return
    store = _cache_read(IDENTITY_CACHE)
    key = os.path.abspath(config_dir)
    if (store.get(key) or {}).get("fp") == old_fp:
        store[key]["fp"] = new_fp
        _cache_write(store, IDENTITY_CACHE)


def credential_dirs() -> list[str]:
    """Every config dir that may hold a copy of a login: slots and contexts."""
    seen, out = set(), []
    for d in [slot_dir(n) for n in account_names()] + [c.path for c in contexts()]:
        key = os.path.abspath(d)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def propagate(spent: Optional[str], resp: dict, skip: str) -> list[str]:
    """Hand a rotated token to every other copy of the same credential.

    A refresh token is single use: rotating it in one config dir spends it
    everywhere. Any other dir still holding the spent generation is one refresh
    away from an invalid_grant, which is what a running Claude Code session
    reports as being signed out. So a rotation is not finished until every copy
    has the successor.

    Only dirs whose stored fingerprint still matches the spent generation are
    touched, so a dir holding some other login is never overwritten.
    """
    if not spent:
        return []
    moved, skip_abs = [], os.path.abspath(skip)
    for d in credential_dirs():
        if os.path.abspath(d) == skip_abs:
            continue
        if fingerprint(keychain.read_credentials(d)) != spent:
            continue          # cheap check first: most dirs are a different login
        try:
            with locks.credentials(d, timeout=3.0):
                cur = keychain.read_credentials(d)
                if fingerprint(cur) != spent:
                    continue  # it moved on while we waited
                rotated = _apply(cur, resp)
                keychain.write_credentials(d, rotated)
                carry_identity(d, spent, fingerprint(rotated))
                moved.append(d)
        except (locks.LockBusy, RuntimeError):
            continue          # the next pass finds it still spent and retries
    return moved


def live_blob(config_dir: str, allow_refresh: bool = True) -> Optional[dict]:
    """Usable credentials for a config dir, refreshed in place when stale.

    The refresh runs under Claude Code's own locks and re-reads the credential
    once held, the same double check Claude Code does, so our refresh and a
    session's refresh can never both spend the same token.

    Returns None only when there is nothing usable: no credential at all, or a
    refresh the server rejected. A network failure returns the stored blob,
    because it is probably still valid.
    """
    blob = keychain.read_credentials(config_dir)
    if not blob or not allow_refresh or not expiring(blob):
        return blob
    try:
        with locks.credentials(config_dir):
            current = keychain.read_credentials(config_dir) or blob
            if not expiring(current):
                return current            # somebody else refreshed while we waited
            spent = fingerprint(current)
            resp, err = refresh(current)
            if not resp:
                # invalid_grant means this copy is stranded, not that the
                # account is gone: a peer dir may hold the live successor.
                return None if err == "invalid_grant" else current
            rotated = _apply(current, resp)
            try:
                keychain.write_credentials(config_dir, rotated)
            except RuntimeError:
                return rotated
    except locks.LockBusy:
        return blob                       # Claude Code is mid-refresh; try later
    new_fp = fingerprint(rotated)
    if new_fp != spent:
        carry_identity(config_dir, spent, new_fp)
        propagate(spent, resp, skip=config_dir)
    return rotated


def profile_result(token: Optional[str]) -> tuple[dict, Optional[str]]:
    """Account facts for a token, and why the lookup failed when it did.

    The error matters more than the facts. "rejected" means the server refused
    the token, which is the only evidence that a login is actually gone.
    "transient" is a rate limit or a network problem and says nothing about the
    login, so it must never be allowed to look like one.
    """
    if not token:
        return {}, "no_token"
    try:
        data = _get("/api/oauth/profile", token)
    except urllib.error.HTTPError as e:
        return {}, "rejected" if e.code in (401, 403) else "transient"
    except Exception:
        return {}, "transient"
    account = data.get("account") or {}
    org = data.get("organization") or {}
    tier = org.get("rate_limit_tier") or ""
    label = " ".join(w.title() if w.isalpha() else w
                     for w in tier.replace("default_claude_", "").split("_") if w)
    if not label:
        label = "Max" if account.get("has_claude_max") else "Pro" if account.get("has_claude_pro") else ""
    return {"email": account.get("email"), "plan": label or None,
            "extra_usage": org.get("has_extra_usage_enabled")}, None


def profile(token: Optional[str]) -> dict:
    """Live account facts for a token: identity and plan.

    Read from the API rather than from the stored credential blob. The blob's
    `subscriptionType` and `rateLimitTier` are snapshots taken at login and go
    stale: an account upgraded after signing in still reports its old tier
    there (observed: blob said max_5x while the account was really max_20x).
    """
    return profile_result(token)[0]


IDENTITY_CACHE = os.path.join(ACCOUNTS_DIR, ".identity.json")


def identity(config_dir: str, blob: Optional[dict]) -> tuple[dict, Optional[str]]:
    """Who a config dir is signed in as, asked at most once per credential.

    An account's email and plan cannot change while its credential does not, so
    the answer is cached against the credential's fingerprint and the endpoint
    is only asked when that fingerprint moves. In the steady state this costs
    no requests at all, which is the point: identity used to be re-fetched for
    every account on every refresh, and one rate-limited burst made all five
    accounts report a dead login at once.

    A failed lookup falls back to the last known answer. Only a token the
    server actually rejected returns an error.
    """
    if not blob:
        return {}, "no_credential"
    fp = fingerprint(blob)
    store = _cache_read(IDENTITY_CACHE)
    hit = store.get(os.path.abspath(config_dir)) or {}
    if fp and hit.get("fp") == fp and hit.get("email"):
        return {"email": hit["email"], "plan": hit.get("plan")}, None
    info, err = profile_result(blob.get("accessToken"))
    if info.get("email"):
        store[os.path.abspath(config_dir)] = {"fp": fp, "email": info["email"],
                                              "plan": info.get("plan"), "at": time.time()}
        _cache_write(store, IDENTITY_CACHE)
        return info, None
    if err == "transient" and hit.get("email"):
        # Say nothing about the login: we simply could not ask right now.
        return {"email": hit["email"], "plan": hit.get("plan")}, None
    return {}, err


def whoami(token: Optional[str]) -> Optional[str]:
    """The account a token belongs to. Identity is never inferred from a name."""
    return profile(token).get("email")


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
    usage_at: float = 0.0        # when the usage payload was fetched, 0 if never

    @property
    def usage_age(self) -> float:
        return time.time() - self.usage_at if self.usage_at else 0.0

    @property
    def stale(self) -> bool:
        """True once the usage numbers are old enough to warn about."""
        return self.usage_age > 420

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

    @property
    def signed_in(self) -> bool:
        """Identity is confirmed. Usage may still be missing: a busy account
        rate limits the usage endpoint, and that is not a login problem."""
        return self.email is not None


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


# --------------------------------------------------------------------------- usage cache

USAGE_CACHE = os.path.join(ACCOUNTS_DIR, ".usage-cache.json")
_BACKOFF_MIN, _BACKOFF_MAX = 300, 900


def _cache_read(path: str = "") -> dict:
    try:
        with open(path or USAGE_CACHE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _cache_write(store: dict, path: str = "") -> None:
    path = path or USAGE_CACHE
    try:
        os.makedirs(ACCOUNTS_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(store, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _usage(name: str, token: str, force: bool = False) -> tuple[list[Limit], float, Optional[str]]:
    """Usage for one account: cached, and backed off after a 429.

    /api/oauth/usage is rate limited per account, and every running Claude Code
    session polls it too. So the account doing the most work is exactly the one
    whose row we cannot fetch, and a 429 must never blank it. Serve the last
    payload instead and stop asking for a while. Reset times in the payload are
    absolute, so a cached row still counts down correctly and still rolls a
    finished window to 0%.

    Returns (limits, when they were fetched, error). An error means there is no
    cached payload either.
    """
    store = _cache_read()
    entry = store.get(name) or {}
    cached, at = entry.get("data"), entry.get("at", 0.0)
    now = time.time()
    if cached and not force and now < entry.get("retry_after", 0):
        return _parse_limits(cached), at, None
    try:
        data = _get("/api/oauth/usage", token)
    except Exception as e:
        code = getattr(e, "code", None)
        if code == 429:
            wait = min(max(entry.get("backoff", 0) * 2, _BACKOFF_MIN), _BACKOFF_MAX)
            store[name] = {**entry, "backoff": wait, "retry_after": now + wait}
            _cache_write(store)
        if cached:
            return _parse_limits(cached), at, None
        return [], 0.0, f"usage HTTP {code}" if code else str(e)[:60]
    store[name] = {"data": data, "at": now, "backoff": 0, "retry_after": 0}
    _cache_write(store)
    return _parse_limits(data), now, None


# --------------------------------------------------------------------------- accounts

def slot_dir(name: str) -> str:
    return os.path.join(ACCOUNTS_DIR, name)


def account_names() -> list[str]:
    try:
        return sorted(n for n in os.listdir(ACCOUNTS_DIR)
                      if os.path.isdir(os.path.join(ACCOUNTS_DIR, n))
                      and not n.startswith(".") and not n.endswith(".lock"))
    except OSError:
        return []


class UnknownAccount(Exception):
    pass


def resolve_account(query: str) -> str:
    """Accept a short nickname for an account slot.

    Exact name wins, then a unique prefix, then a unique substring, so "rr"
    finds "rryanhuu", "200" finds "ryanhu200" and "callie" finds
    "ryantrycallie" without anyone maintaining an alias table.
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
        if blob and (identity(cand, blob)[0].get("email") or "").lower() == email.lower():
            return blob
    return None


def adopt(config_dir: str, blob: dict) -> bool:
    """Write a credential into a config dir under Claude Code's locks."""
    try:
        with locks.credentials(config_dir):
            keychain.write_credentials(config_dir, blob)
        return True
    except (locks.LockBusy, RuntimeError):
        return False


def load_account(name: str, with_usage: bool = True, force: bool = False) -> Account:
    slot = slot_dir(name)
    acct = Account(name=name, slot=slot, checked_at=time.time())
    blob = live_blob(slot)
    info, err = identity(slot, blob)
    email = info.get("email")
    if not email and err in ("no_credential", "rejected"):
        # Positively refused, so this slot is stranded rather than signed out:
        # the live successor is sitting in whichever dir did the rotating.
        healed = find_live_blob(recorded_email(slot))
        if healed:
            adopt(slot, healed)
            blob = healed
            info, err = identity(slot, healed)
            email = info.get("email") or recorded_email(slot)
    if not email:
        raw = keychain.read_credentials(slot)
        # "expired" is a claim about the login and needs the server to have
        # said so. A rate limit or a dead network proves nothing about it.
        acct.error = ("not signed in" if not raw
                      else "login expired" if err in ("rejected", "no_credential")
                      else "can't reach Anthropic")
        return acct
    acct.email = email
    acct.plan = info.get("plan") or blob.get("subscriptionType")
    if with_usage:
        acct.limits, acct.usage_at, acct.error = _usage(name, blob["accessToken"], force)
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


CHIP_FILE = os.path.join(ACCOUNTS_DIR, ".chips.json")


def chip_index(name: str, palette_size: int = 8) -> int:
    """A stable, distinct colour slot per account.

    Hashing the name is stateless but collides, which defeats the point of
    colour-coding, so assignments are remembered: an account keeps its colour
    for good, and a new one takes the lowest free slot.
    """
    try:
        with open(CHIP_FILE) as f:
            table = json.load(f)
    except (OSError, ValueError):
        table = {}
    if name in table:
        return int(table[name]) % palette_size
    used = {int(v) for v in table.values()}
    idx = next((i for i in range(palette_size) if i not in used), len(table) % palette_size)
    table[name] = idx
    try:
        os.makedirs(ACCOUNTS_DIR, exist_ok=True)
        with open(CHIP_FILE, "w") as f:
            json.dump(table, f, indent=2)
    except OSError:
        pass
    return idx


def set_chip_index(name: str, index: int) -> None:
    try:
        with open(CHIP_FILE) as f:
            table = json.load(f)
    except (OSError, ValueError):
        table = {}
    table[name] = int(index)
    try:
        os.makedirs(ACCOUNTS_DIR, exist_ok=True)
        with open(CHIP_FILE, "w") as f:
            json.dump(table, f, indent=2)
    except OSError:
        pass


def rename_account(old: str, new: str) -> tuple[bool, str]:
    """Rename a slot, moving its credentials with it.

    The keychain service name is derived from the slot's path, so a rename is
    a move of both: write the login under the new path's service, then drop the
    old item. The credentials are read first, and nothing is deleted until the
    new copy is in place.
    """
    import shutil
    new = "".join(c for c in new.strip() if c.isalnum() or c in "-_")
    if not new:
        return False, "name must contain letters, digits, - or _"
    if new == old:
        return True, "unchanged"
    if new in account_names():
        return False, f"{new} already exists"
    old_slot, new_slot = slot_dir(old), slot_dir(new)
    blob = keychain.read_credentials(old_slot) or find_live_blob(recorded_email(old_slot))
    if not blob:
        return False, f"{old} has no login to move"
    os.makedirs(new_slot, exist_ok=True)
    for entry in os.listdir(old_slot):
        try:
            shutil.move(os.path.join(old_slot, entry), os.path.join(new_slot, entry))
        except (OSError, shutil.Error):
            pass
    try:
        keychain.write_credentials(new_slot, blob)
    except RuntimeError as e:
        return False, str(e)
    keychain.delete(keychain.service_for(old_slot))
    shutil.rmtree(old_slot, ignore_errors=True)
    try:                                     # keep its colour through the rename
        with open(CHIP_FILE) as f:
            table = json.load(f)
        if old in table:
            table[new] = table.pop(old)
            with open(CHIP_FILE, "w") as f:
                json.dump(table, f, indent=2)
    except (OSError, ValueError):
        pass
    return True, f"{old} is now {new}"


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
        return (identity(self.path, blob)[0].get("email") if blob else "") or recorded_email(self.path)


def context_owners(paths: Iterable[str], accts: Iterable[Account]) -> dict[str, str]:
    """Which account each context is signed in as, without asking the API.

    A context holds a copy of an account's credential, so equal refresh tokens
    already identify it. Only a context matching no account costs a request,
    which keeps the panel honest while the API is rate limiting us: a failed
    lookup used to just drop the "in use by" mark.
    """
    by_fp: dict[str, str] = {}
    for a in accts:
        fp = fingerprint(keychain.read_credentials(a.slot))
        if fp and a.email:
            by_fp[fp] = a.email
    out: dict[str, str] = {}
    for path in paths:
        blob = keychain.read_credentials(path)
        if not blob:
            out[path] = ""
            continue
        out[path] = (by_fp.get(fingerprint(blob))
                     or identity(path, blob)[0].get("email")
                     or recorded_email(path))
    return out


def _routes() -> tuple[dict[str, str], list[tuple[str, str]], dict[str, str]]:
    """The routing table: named contexts, per-path rules, per-terminal pins."""
    named: dict[str, str] = {}
    paths: list[tuple[str, str]] = []
    terms: dict[str, str] = {}
    try:
        for line in open(ROUTES_FILE):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("path:"):
                pth, dst = line[5:].split("=", 1)
                paths.append((pth.rstrip("/"), dst))
            elif line.startswith("term:"):
                tid, dst = line[5:].split("=", 1)
                terms[tid] = dst
            else:
                k, v = line.split("=", 1)
                named[k] = v
    except OSError:
        pass
    return named, paths, terms


def contexts() -> list[Context]:
    named, paths, _ = _routes()
    out = [Context(name=k, path=v) for k, v in sorted(named.items())]
    if not out:
        out = [Context(name="default", path=DEFAULT_CONFIG)]
    seen = {os.path.abspath(c.path) for c in out}
    for _, dst in paths:
        if os.path.abspath(dst) not in seen:
            out.append(Context(name=os.path.basename(dst.rstrip("/")), path=dst))
            seen.add(os.path.abspath(dst))
    # A dir reached through a path alias has its own login and appears in no
    # rule, so anything with a session running now counts as a context too.
    for d in sessions.discover_config_dirs(HOME):
        if os.path.abspath(d) not in seen:
            out.append(Context(name=_ctx_name(d).lstrip("."), path=d))
            seen.add(os.path.abspath(d))
    try:
        for n in sorted(os.listdir(CTX_DIR)):
            d = os.path.join(CTX_DIR, n)
            # Claude Code's legacy credential lock is a sibling directory
            # (`<config dir>.lock`), so it briefly looks like another context.
            if n.endswith(".lock") or n.startswith("."):
                continue
            if os.path.isdir(d) and os.path.abspath(d) not in seen:
                out.append(Context(name=n, path=d))
                seen.add(os.path.abspath(d))
    except OSError:
        pass
    return out


def context_for(cwd: str, term_id: str = "") -> Context:
    """Which context a session runs in.

    A terminal pin wins outright: it is the one rule the user set for exactly
    this session. Otherwise the longest matching path rule wins, then the
    named defaults.
    """
    named, paths, terms = _routes()
    if term_id and term_id in terms:
        return Context(name=_ctx_name(terms[term_id]), path=terms[term_id])
    cands = {os.path.abspath(cwd), os.path.realpath(cwd)}
    best, best_len = None, -1
    for pth, dst in paths:
        if any((c + "/").startswith(pth + "/") for c in cands) and len(pth) > best_len:
            best, best_len = dst, len(pth)
    if best:
        return Context(name=os.path.basename(best.rstrip("/")), path=best)
    work = named.get("work")
    if work and any((c + "/").startswith(os.path.join(HOME, "Desktop", "callie")) for c in cands):
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


def _ctx_name(path: str) -> str:
    return os.path.basename(path.rstrip("/")) or path


def _write_rule(key: str, context_path: Optional[str]) -> None:
    lines: list[str] = []
    try:
        with open(ROUTES_FILE) as f:
            lines = [l.rstrip("\n") for l in f]
    except OSError:
        pass
    lines = [l for l in lines if not l.startswith(key)]
    if context_path:
        lines.append(key + context_path)
    os.makedirs(os.path.dirname(ROUTES_FILE), exist_ok=True)
    with open(ROUTES_FILE, "w") as f:
        f.write("\n".join(l for l in lines if l.strip()) + "\n")


def _write_route(root: str, context_path: Optional[str]) -> None:
    _write_rule(f"path:{root}=", context_path)


def _seed_context(ctx_path: str, blob: Optional[dict]) -> None:
    """Create a context dir that shares config and history with ~/.claude.

    Settings, commands and transcripts are symlinked back, so a session that
    moves here keeps every one of them and `claude -c` still finds the
    conversation it was in. Only the login differs, which is the whole point.
    """
    os.makedirs(ctx_path, exist_ok=True)
    for item in SHARED_ITEMS:
        target = os.path.join(DEFAULT_CONFIG, item)
        link = os.path.join(ctx_path, item)
        if os.path.exists(target) and not os.path.lexists(link):
            os.symlink(target, link)
    if blob:
        adopt(ctx_path, blob)


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
    blob = keychain.read_credentials(current.path)
    _seed_context(ctx_path, blob)                       # start where it already was
    _write_route(root, ctx_path)
    who = identity(current.path, blob)[0].get("email") if blob else None
    return True, (f"{root}\n  own context: {ctx_path.replace(HOME, '~')}"
                  f"\n  account    : {who or 'unknown'}")


# --------------------------------------------------------------------------- session pins

def term_pins() -> dict[str, str]:
    return _routes()[2]


def pin_dir(term_id: str) -> str:
    """The context dir backing one terminal's pin.

    Keyed by the terminal's own id rather than a pid or a tty number, so
    restarting Claude Code in that tab lands back on the same account and a
    recycled pid can never inherit somebody else's pin.
    """
    return os.path.join(CTX_DIR, "term-" + term_id.replace("-", "")[:8].lower())


def pin(term_id: str, account: str, seed_from: Optional[str] = None) -> tuple[bool, str]:
    """Give one session its own account, without moving the rest of its project.

    A login belongs to a config dir, so a session can only differ from its
    neighbours by having a config dir of its own. That dir is created here,
    sharing settings and transcripts with ~/.claude, and the routing table
    sends this terminal to it from now on.
    """
    if not term_id:
        return False, "this session has no terminal id, so it cannot be pinned"
    try:
        account = resolve_account(account)
    except UnknownAccount as e:
        return False, str(e)
    ctx_path = pin_dir(term_id)
    fresh = not os.path.isdir(ctx_path)
    if fresh:
        seed = keychain.read_credentials(seed_from) if seed_from else None
        _seed_context(ctx_path, seed)
    _write_rule(f"term:{term_id}=", ctx_path)
    ok, msg = swap(account, Context(name=_ctx_name(ctx_path), path=ctx_path))
    if not ok:
        if fresh:
            _write_rule(f"term:{term_id}=", None)       # leave no half-made pin
        return False, msg
    return True, msg


def unpin(term_id: str) -> tuple[bool, str]:
    """Send a session back to its project's account on its next start."""
    if term_id not in term_pins():
        return False, "this session is not pinned"
    _write_rule(f"term:{term_id}=", None)
    return True, "pin removed; this session follows its project again"


def prune_pins(live_term_ids: Iterable[str], max_age: float = 14 * 24 * 3600) -> list[str]:
    """Drop pins for terminals that are gone.

    A terminal id disappears for good when its tab closes, so a pin nothing has
    used in a fortnight is dead weight. Anything with a session running now is
    kept whatever its age.
    """
    alive, dropped, cutoff = set(live_term_ids), [], time.time() - max_age
    for tid, path in term_pins().items():
        if tid in alive:
            continue
        try:
            if os.path.getmtime(path) > cutoff:
                continue
        except OSError:
            pass                       # dir already gone: the rule is pure litter
        _write_rule(f"term:{tid}=", None)
        dropped.append(tid)
    return dropped


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
    slot = slot_dir(account)
    blob = live_blob(slot)
    email = identity(slot, blob)[0].get("email")
    if not email:
        healed = find_live_blob(recorded_email(slot))
        if healed:
            blob, email = healed, identity(slot, healed)[0].get("email")
    if not blob or not email:
        return False, f"{account} has no usable login (add it again)"
    before = context.email or "unknown"
    # Under Claude Code's own locks: a swap that lands inside a session's
    # refresh window is overwritten by the OLD account's refreshed token, and
    # the swap looks like it silently did nothing.
    try:
        with locks.credentials(context.path):
            keychain.write_credentials(context.path, blob)
    except locks.LockBusy:
        return False, "Claude Code is refreshing credentials right now; try again in a few seconds"
    except RuntimeError as e:
        return False, str(e)
    try:
        with open(SWAP_LOG, "a") as f:
            f.write(f"{_dt.datetime.now().isoformat(timespec='seconds')}  "
                    f"{context.path.replace(HOME, '~')}  {before} -> {email}  via=menubar\n")
    except OSError:
        pass
    return True, f"{context.name} now uses {email}"
