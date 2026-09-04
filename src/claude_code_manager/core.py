"""Accounts, their credentials, and their usage.

Every account owns exactly one config directory, ``~/.claude-accts/<name>``,
holding that subscription's login. Sessions run in it directly, and shared
settings and transcripts are symlinked back to ``~/.claude`` so every account
sees the same commands, skills and history.

One directory per account is what makes the rules in `profiles` cheap: pointing
a project at another subscription rewrites a rule, and never copies a
credential. Nothing here mints one either. The only credentials that exist are
the ones Claude Code wrote at login, which is why a session started this way
keeps its real `subscriptionType`, scopes and rate-limit tier.
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

from . import keychain, locks, oauth, profiles, sessions

HOME = os.path.expanduser("~")
ACCOUNTS_DIR = os.environ.get("CCM_ACCOUNTS_DIR", os.path.join(HOME, ".claude-accts"))
DEFAULT_CONFIG = os.path.join(HOME, ".claude")

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TOKEN_URLS = ("https://platform.claude.com/v1/oauth/token",
              "https://console.anthropic.com/v1/oauth/token")
API = "https://api.anthropic.com"
UA = "claude-cli/2.1.236 (external, cli)"
OAUTH_HEADERS = {"anthropic-beta": "oauth-2025-04-20", "User-Agent": UA}
ANTHROPIC_VERSION = "2023-06-01"
POKE_MODEL = "claude-haiku-4-5-20251001"


# --------------------------------------------------------------------------- http

def _post(url: str, body: dict, token: Optional[str] = None, timeout: int = 30,
          extra_headers: Optional[dict] = None) -> dict:
    headers = {"Content-Type": "application/json", **OAUTH_HEADERS, **(extra_headers or {})}
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
    """Every config dir that may hold a login.

    One per account, the default dir Claude Code falls back to, and any other
    dir a session is running in: a session started before the rules existed, or
    launched through a path alias, still bills real work and has to be counted.
    """
    seen, out = set(), []
    candidates = ([slot_dir(n) for n in account_names()] + [DEFAULT_CONFIG]
                  + sessions.discover_config_dirs(HOME))
    for d in candidates:
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


STASH_DIR = os.path.join(profiles.CCM_HOME, "pending")


def _stash_path(config_dir: str) -> str:
    digest = hashlib.sha256(os.path.abspath(config_dir).encode()).hexdigest()[:12]
    return os.path.join(STASH_DIR, digest + ".json")


def _persist(config_dir: str, rotated: dict, spent: Optional[str]) -> bool:
    """Store a rotated credential, and never lose it if the keychain refuses.

    A refresh token is single use. Once the exchange succeeds the stored one is
    already dead, so a failed write does not leave things as they were: it
    leaves a credential that can never be refreshed again. The successor is
    written to disk instead, keyed to the generation it replaced, and picked up
    on the next read. This is how an account silently "expires" while nothing
    is wrong with it.
    """
    try:
        keychain.write_credentials(config_dir, rotated)
    except RuntimeError:
        try:
            os.makedirs(STASH_DIR, mode=0o700, exist_ok=True)
            tmp = _stash_path(config_dir) + ".tmp"
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                json.dump({"replaces": spent, "blob": rotated, "at": time.time()}, f)
            os.replace(tmp, _stash_path(config_dir))
        except OSError:
            pass
        return False
    _drop_stash(config_dir)
    return True


def _take_stash(config_dir: str, current: Optional[dict]) -> Optional[dict]:
    """A successor left behind by a write that failed, if it fits what is stored."""
    try:
        with open(_stash_path(config_dir)) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return None
    if saved.get("replaces") != fingerprint(current):
        _drop_stash(config_dir)      # describes some older generation
        return None
    return saved.get("blob") or None


def _drop_stash(config_dir: str) -> None:
    try:
        os.remove(_stash_path(config_dir))
    except OSError:
        pass


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
            # A successor from a write that failed earlier: the stored token is
            # already spent, so use it before trying to exchange it again.
            saved = _take_stash(config_dir, current)
            if saved and _persist(config_dir, saved, fingerprint(current)):
                if not expiring(saved):
                    return saved
                current = saved
            if not expiring(current):
                return current            # somebody else refreshed while we waited
            spent = fingerprint(current)
            resp, err = refresh(current)
            if not resp:
                # invalid_grant means this copy is stranded, not that the
                # account is gone: a peer dir may hold the live successor.
                return None if err == "invalid_grant" else current
            rotated = _apply(current, resp)
            if not _persist(config_dir, rotated, spent):
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
            "tier": tier or None,          # raw, for writing a credential
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
    mismatch: Optional[str] = None   # holds a different account than its name
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
# The endpoint sends Retry-After: 0, which says nothing, so the wait is ours to
# choose. Start short because a brief limit is the common case and the row is
# blank until it clears; escalate only if the limiting is sustained.
# Measured against the live endpoint: about four requests per five minutes per
# account, and a 429 carries Retry-After: 300. So the window is fixed and the
# server states it. Guessing a longer one only keeps a row stale for longer
# than being asked to.
_RETRY_FALLBACK = 300        # used only when a 429 arrives with no Retry-After
_FORCE_FLOOR = 30            # shortest gap between forced checks of one account


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


def _retry_after(err) -> Optional[float]:
    """How long the server asked us to wait, when it says so in seconds."""
    try:
        return max(0.0, float(err.headers.get("Retry-After")))
    except (AttributeError, TypeError, ValueError):
        return None


def _waiting(entry: dict, now: float) -> Optional[str]:
    """Why this account's usage is not being fetched, counted from now.

    The stored message is written once, when the 429 lands, so its countdown
    would be as old as that moment. The deadline it was written from is
    absolute, so the sentence is rebuilt from that each time it is read.
    """
    left = (entry.get("retry_after") or 0) - now
    if left <= 0:
        return entry.get("last_error") if not entry.get("data") else None
    return f"rate limited, retrying in {max(1, round(left / 60))}m"


def _usage(name: str, token: str, force: bool = False,
           fp: Optional[str] = None) -> tuple[list[Limit], float, Optional[str]]:
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
    # Cached against the credential, not just the account name. Signing an
    # account in again gives it a different login, and serving the old payload
    # then shows one subscription's usage under another's name — which reads as
    # two accounts with identical bars rather than as stale data.
    if fp and entry.get("fp") and entry["fp"] != fp:
        entry = {}
    cached, at = entry.get("data"), entry.get("at", 0.0)
    now = time.time()
    # A forced check skips the wait, but not entirely: clicking refresh at a
    # rate limit should not add requests that can only prolong it.
    if force and now - (entry.get("tried_at") or 0) < _FORCE_FLOOR:
        return (_parse_limits(cached) if cached else []), at, _waiting(entry, now)
    if cached and not force and now < entry.get("retry_after", 0):
        # Say why the row is old. Reporting no error here left the menu with
        # nothing to show but the age of the numbers, which states a fact and
        # withholds the reason for it.
        return _parse_limits(cached), at, _waiting(entry, now)
    try:
        data = _get("/api/oauth/usage", token)
    except Exception as e:
        code = getattr(e, "code", None)
        if code == 429:
            deadline = now + (_retry_after(e) or _RETRY_FALLBACK)
            # Never move the deadline later than it already was. The window is
            # fixed, so asking again inside it must not cost more waiting than
            # staying quiet would have. That holds whether Retry-After counts
            # down with the window or restates its full length every time.
            prior = entry.get("retry_after") or 0
            if prior > now:
                deadline = min(deadline, prior)
            note = f"rate limited, retrying in {max(1, round((deadline - now) / 60))}m"
            store[name] = {**entry, "retry_after": deadline,
                           "tried_at": now, "last_error": note}
            _cache_write(store)
            if not cached:
                return [], 0.0, note
        if cached:
            return _parse_limits(cached), at, None
        return [], 0.0, f"usage HTTP {code}" if code else str(e)[:60]
    store[name] = {"data": data, "at": now, "retry_after": 0,
                   "fp": fp, "tried_at": now}
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
    candidates = ([prefer] if prefer else []) + credential_dirs()
    for cand in dict.fromkeys(c for c in candidates if c):
        # never refresh a context: a session may be running there
        blob = live_blob(cand) if os.path.abspath(cand) in slots else keychain.read_credentials(cand)
        if blob and (identity(cand, blob)[0].get("email") or "").lower() == email.lower():
            return blob
    return None


def _cached_email(config_dir: str) -> str:
    """The last identity confirmed for a dir, whatever it holds now."""
    entry = _cache_read(IDENTITY_CACHE).get(os.path.abspath(config_dir)) or {}
    return entry.get("email") or ""


def is_account_dir(config_dir: str) -> bool:
    return os.path.dirname(os.path.abspath(config_dir).rstrip("/")) == ACCOUNTS_DIR


def adopt(config_dir: str, blob: dict, email: str = "", rebind: bool = False) -> bool:
    """Write a credential into a config dir under Claude Code's locks.

    Putting a credential in a dir can change whose dir it is, so the cached
    identity for it is dropped unless the caller can name the account. Trusting
    a stale entry here is how one account's dir comes to be described as
    another's, which then spreads: the answer is used to decide what to copy
    where.
    """
    # An account's own directory may only ever hold that account's login. It is
    # named for one subscription and everything else treats it as the truth
    # about that subscription, so writing another account's credential there
    # does not just misreport it: the wrong login then gets copied outward to
    # every session the account owns. Claude Code keeps its own record of who a
    # directory last authenticated as, which makes an independent check.
    if is_account_dir(config_dir) and not rebind:
        expected = recorded_email(config_dir) or _cached_email(config_dir)
        actual = (email or identity(config_dir, blob)[0].get("email") or "").lower()
        if expected and actual and expected.lower() != actual.lower():
            return False
    try:
        with locks.credentials(config_dir):
            keychain.write_credentials(config_dir, blob)
    except (locks.LockBusy, RuntimeError):
        return False
    store = _cache_read(IDENTITY_CACHE)
    key = os.path.abspath(config_dir)
    if email:
        store[key] = {"fp": fingerprint(blob), "email": email,
                      "plan": (store.get(key) or {}).get("plan"), "at": time.time()}
    else:
        store.pop(key, None)
    _cache_write(store, IDENTITY_CACHE)
    return True


# Verdicts that mean the login itself is gone, rather than unreachable. Claude
# Code empties the token fields in place when it finds a login it cannot use,
# so a credential record can still exist with nothing usable inside it.
GONE = ("no_credential", "rejected", "no_token", "no_refresh_token")


def load_account(name: str, with_usage: bool = True, force: bool = False) -> Account:
    slot = slot_dir(name)
    acct = Account(name=name, slot=slot, checked_at=time.time())
    blob = live_blob(slot)
    info, err = identity(slot, blob)
    email = info.get("email")
    if not email and err in GONE:
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
        # "expired" is a claim about the login and needs evidence: the server
        # refusing the token, or there being no token left to send. A rate
        # limit or a dead network proves nothing about it.
        acct.error = ("not signed in" if not raw
                      else "login expired" if err in GONE
                      else "can't reach Anthropic")
        return acct
    acct.email = email
    acct.plan = info.get("plan") or blob.get("subscriptionType")
    # An account holding somebody else's login still answers every question,
    # it just answers them about the wrong subscription — which reads as two
    # accounts reporting identical usage rather than as a fault. Say it.
    was = recorded_email(slot)
    if was and email and was.lower() != email.lower():
        # Its own field: the usage fetch below sets `error`, and would
        # otherwise clear this the moment usage came back fine — which it
        # does, because the credential works. It is just the wrong one.
        acct.mismatch = f"holds {email}, not {was}"
    if with_usage:
        acct.limits, acct.usage_at, acct.error = _usage(
            name, blob["accessToken"], force, fingerprint(blob))
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


_CHIPS: tuple[float, dict] = (-1.0, {})


def _chip_table() -> dict:
    """The colour table, re-read only when the file behind it changes.

    Every chip drawn asks for its colour, and a menu redraw draws hundreds, so
    this is read far more often than it is written.
    """
    global _CHIPS
    try:
        stamp = os.path.getmtime(CHIP_FILE)
    except OSError:
        return {}
    if stamp != _CHIPS[0]:
        try:
            with open(CHIP_FILE) as f:
                _CHIPS = (stamp, json.load(f))
        except (OSError, ValueError):
            _CHIPS = (stamp, {})
    return _CHIPS[1]


def chip_index(name: str, palette_size: int = 8) -> int:
    """A stable, distinct colour slot per account.

    Hashing the name is stateless but collides, which defeats the point of
    colour-coding, so assignments are remembered: an account keeps its colour
    for good, and a new one takes the lowest free slot.
    """
    table = _chip_table()
    if name in table:
        return int(table[name]) % palette_size
    used = {int(v) for v in table.values()}
    idx = next((i for i in range(palette_size) if i not in used), len(table) % palette_size)
    set_chip_index(name, idx)
    return idx


def set_chip_index(name: str, index: int) -> None:
    global _CHIPS
    table = dict(_chip_table())
    table[name] = int(index)
    _CHIPS = (-1.0, {})
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


def sign_in_begin(name: str, redirect_uri: str = "") -> oauth.Attempt:
    """Start signing an account in. Returns the attempt to hand back later.

    The account this slot last held is offered to the sign-in page, so the
    browser lands on the right one instead of whichever it is already signed
    into. Switching accounts part way through is what loses the code.
    """
    hint = recorded_email(slot_dir(name)) or _cached_email(slot_dir(name))
    return oauth.begin(name, redirect_uri or oauth.CALLBACK_URL, login_hint=hint)


def sign_in_finish(attempt: oauth.Attempt, pasted: str) -> tuple[bool, str]:
    """Exchange a pasted code and store the credential for that account.

    Written only after the API has confirmed the identity and the plan. A
    credential that cannot report its own plan opens sessions as API billing,
    so it is refused rather than saved: a login that half works is harder to
    diagnose than one that never happened.
    """
    def post(body: dict) -> dict:
        last: Exception = RuntimeError("no token endpoint answered")
        for url in TOKEN_URLS:
            try:
                return _post(url, body, timeout=30)
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = json.loads(e.read()).get("error_description") or ""
                except Exception:
                    pass
                last = RuntimeError(detail or f"HTTP {e.code}")
            except Exception as e:
                last = e
        raise last

    blob, result = oauth.finish(attempt, pasted, post, profile_result)
    if not blob:
        return False, result
    email, slot = result, ensure_account_dir(attempt.account)
    before = recorded_email(slot) or ""
    if not adopt(slot, blob, email=email, rebind=True):
        return False, "signed in, but the keychain refused to store it"
    _cache_write({**_cache_read(IDENTITY_CACHE),
                  os.path.abspath(slot): {"fp": fingerprint(blob), "email": email,
                                          "plan": profile_result(blob["accessToken"])[0].get("plan"),
                                          "at": time.time()}}, IDENTITY_CACHE)
    if before and before.lower() != email.lower():
        # The browser signs in as whoever it was already logged into, which is
        # how an account once ended up holding another one's token.
        return True, (f"“{attempt.account}” is now signed in as {email}, but it "
                      f"used to be {before}. If that is wrong, sign in again in a "
                      f"private window.")
    return True, f"“{attempt.account}” is signed in as {email}"


def add_account_command(name: str) -> str:
    """Shell command that signs an account into its own directory.

    `command claude` on purpose: the shell wrapper picks a directory from the
    rules, and going through it would sign this account into whichever one the
    current directory routes to, overwriting that account's login.
    """
    slot = slot_dir(name)
    return (f'mkdir -p {slot!r} && CLAUDE_CONFIG_DIR={slot!r} command claude '
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
            "model": POKE_MODEL,
            "max_tokens": 1,
            "system": [{"type": "text",
                        "text": "You are Claude Code, Anthropic's official CLI for Claude."}],
            "messages": [{"role": "user", "content": "hi"}],
        }, token=blob["accessToken"], timeout=45,
            # The Messages API rejects a request without it; the OAuth
            # endpoints do not use it, which is why it is not in OAUTH_HEADERS.
            extra_headers={"anthropic-version": ANTHROPIC_VERSION})
        return True, "window started"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)[:80]


# --------------------------------------------------------------------------- contexts

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


SHARED_ITEMS = ("CLAUDE.md", "commands", "agents", "skills", "hooks", "workflows",
                "bin", "automations", "settings.json", "design", "appstore",
                "plugins", "projects")


_ROOTS: dict[str, str] = {}


def project_root(cwd: str) -> str:
    """Main checkout for a directory: a worktree resolves to its parent repo.

    Answers are remembered for the life of the process: this shells out to git,
    and a redraw asks about the same handful of directories over and over.

    A path that no longer exists still answers, as itself: rules outlive the
    directories they were written for, and one deleted worktree must not stop
    the whole table from resolving.
    """
    import subprocess as _sp
    if not cwd or not os.path.isdir(cwd):
        return os.path.abspath(cwd or HOME)
    if cwd in _ROOTS:
        return _ROOTS[cwd]
    try:
        r = _sp.run(["git", "rev-parse", "--git-common-dir"], cwd=cwd,
                    capture_output=True, text=True, timeout=5)
    except (OSError, _sp.SubprocessError):
        return os.path.abspath(cwd)
    if r.returncode == 0 and r.stdout.strip():
        gitdir = r.stdout.strip()
        if not os.path.isabs(gitdir):
            gitdir = os.path.join(cwd, gitdir)
        root = os.path.dirname(os.path.abspath(gitdir)) or cwd
    else:
        root = os.path.abspath(cwd)
    _ROOTS[cwd] = root
    return root


def _ctx_name(path: str) -> str:
    return os.path.basename(path.rstrip("/")) or path


# --------------------------------------------------------------------------- rules

def account_dir(name: str) -> str:
    """The config dir an account owns.

    One directory per account, which is what makes a rule change free: the
    login already lives there, so pointing a project somewhere else copies no
    credential and cannot leave one half written.
    """
    return slot_dir(name)


def ensure_account_dir(name: str) -> str:
    """Make an account's directory usable as a working config dir.

    Settings, commands, agents and transcripts are symlinked back to ~/.claude
    so every account shares one set of them and `claude -c` finds the same
    history whichever subscription is paying.
    """
    path = account_dir(name)
    os.makedirs(path, exist_ok=True)
    for item in SHARED_ITEMS:
        target = os.path.join(DEFAULT_CONFIG, item)
        link = os.path.join(path, item)
        if os.path.exists(target) and not os.path.lexists(link):
            try:
                os.symlink(target, link)
            except OSError:
                pass
    return path


SESSION_DIRS = os.environ.get("CCM_SESSION_DIRS", os.path.join(HOME, ".claude-ctx"))


def session_dir(term_id: str) -> str:
    """The config dir belonging to one terminal.

    A session reads its credential from the dir it launched with, and re-reads
    it every half minute, so a dir per session is what makes one session
    switchable on its own and without a restart. Keyed on the terminal's uuid,
    which survives restarting Claude Code in that tab and is never recycled.
    """
    return os.path.join(SESSION_DIRS, "s-" + term_id.replace("-", "")[:10].lower())


def _seed_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    for item in SHARED_ITEMS:
        target = os.path.join(DEFAULT_CONFIG, item)
        link = os.path.join(path, item)
        if os.path.exists(target) and not os.path.lexists(link):
            try:
                os.symlink(target, link)
            except OSError:
                pass


def prepare_session(term_id: str, account: str) -> str:
    """The dir a terminal should launch in, holding `account`'s credential."""
    path = session_dir(term_id)
    _seed_dir(path)
    want = live_blob(account_dir(account)) if account else None
    if want and fingerprint(keychain.read_credentials(path)) != fingerprint(want):
        adopt(path, want)
    return path


def resolve_dir(cwd: str, term_id: str = "") -> str:
    """The config dir to launch Claude Code with, prepared and ready.

    Sessions that have no terminal of their own (background runs, editors) fall
    back to the account's own dir: they cannot be switched individually anyway,
    and giving them a dir would leave one behind on every run.
    """
    account, _ = resolve(cwd, term_id)
    if not account:
        return DEFAULT_CONFIG
    if not term_id:
        return ensure_account_dir(account)
    return prepare_session(term_id, account)


def account_dirs_for(account: str, live_terms: Iterable[str]) -> list[str]:
    """Every dir that should be holding one account's login right now."""
    out = [account_dir(account)]
    r = rules()
    for term in live_terms:
        path = session_dir(term)
        if os.path.isdir(path) and r.account_for("", term)[0] == account:
            out.append(path)
    return out


def sync_credentials(live: Iterable["sessions.Session"]) -> list[str]:
    """Give every copy of an account's login the freshest one of ITS lineage.

    Copies of a single refresh token cannot all refresh: the token is single
    use, so whichever session gets there first spends it for the rest. Rather
    than race Claude Code for it, this hands the newest credential to whoever
    is behind. A session left holding a spent one recovers by itself, since it
    re-reads its keychain item about every thirty seconds.

    The account's own dir is authoritative. A session dir is only promoted over
    it when it is genuinely newer AND the API confirms it belongs to that same
    account, because a dir that has not caught up with a rule change is holding
    somebody else's login, and copying that around would mix two accounts up.

    Keychain work only, apart from that one confirmation, which is cached.
    """
    groups: dict[str, list[str]] = {n: [] for n in account_names()}
    for sess in live:
        if not sess.term_id:
            continue
        path = session_dir(sess.term_id)
        if not os.path.isdir(path):
            continue
        account, _ = resolve(sess.cwd, sess.term_id)
        if account in groups and path not in groups[account]:
            groups[account].append(path)

    healed: list[str] = []
    for account, session_paths in groups.items():
        if not session_paths:
            continue
        home = account_dir(account)
        master = keychain.read_credentials(home)
        if not master or not master.get("refreshToken"):
            continue
        # Promote a session copy only if it is newer and provably this account.
        owner = (identity(home, master)[0].get("email") or "").lower()
        for path in session_paths:
            b = keychain.read_credentials(path)
            if not b or not b.get("refreshToken"):
                continue
            if (b.get("expiresAt") or 0) <= (master.get("expiresAt") or 0):
                continue
            email = (identity(path, b)[0].get("email") or "").lower()
            if email and owner and email == owner and adopt(home, b, email=owner):
                master = b
                healed.append(home)
        want = (identity(home, master)[0].get("email") or "")
        best = fingerprint(master)
        for path in session_paths:
            if fingerprint(keychain.read_credentials(path)) == best:
                continue
            if adopt(path, master, email=want):
                healed.append(path)
    return healed


def gc_session_dirs(live_terms: Iterable[str], max_age: float = 7 * 24 * 3600) -> list[str]:
    """Remove dirs for terminals that are gone and were not used recently."""
    keep = {session_dir(t) for t in live_terms if t}
    gone, cutoff = [], time.time() - max_age
    try:
        entries = os.listdir(SESSION_DIRS)
    except OSError:
        return gone
    for name in entries:
        path = os.path.join(SESSION_DIRS, name)
        if not name.startswith("s-") or path in keep or not os.path.isdir(path):
            continue
        try:
            if os.path.getmtime(path) > cutoff:
                continue
        except OSError:
            continue
        keychain.delete(keychain.service_for(path))
        import shutil
        shutil.rmtree(path, ignore_errors=True)
        gone.append(path)
    return gone


def rules() -> profiles.Rules:
    return profiles.load()


def save_rules(r: profiles.Rules) -> None:
    for name in {r.default_account, *(p.account for p in r.profiles),
                 *r.projects.values(), *r.sessions.values()}:
        if name:
            ensure_account_dir(name)
    profiles.save(r, account_dir)


def resolve(cwd: str, term_id: str = "") -> tuple[str, str]:
    """Which account a session in `cwd` should bill to, and why.

    A worktree resolves to its parent checkout first, so it inherits whatever
    rule covers the repository even when it lives outside the repo directory.
    """
    r = rules()
    for path in (os.path.abspath(cwd), project_root(cwd)):
        account, reason = r.account_for(path, term_id)
        if reason != "default":
            return account, reason
    return r.default_account, "default"


def carry_project_state(project: str, src_dir: str, dst_dir: str) -> bool:
    """Move one project's Claude Code settings to another config dir.

    `.claude.json` keeps per-project trust, allowed tools and MCP servers under
    the project's path. Sending a project to a different account would
    otherwise drop all of it and re-ask for trust, so the entry is copied
    across under Claude Code's own config lock.
    """
    src, dst = _config_json(src_dir), _config_json(dst_dir)
    if os.path.abspath(src) == os.path.abspath(dst):
        return False
    try:
        with open(src) as f:
            entry = (json.load(f).get("projects") or {}).get(project)
    except (OSError, ValueError):
        return False
    if not entry:
        return False
    try:
        with locks.config(dst_dir):
            try:
                with open(dst) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            projects_map = data.setdefault("projects", {})
            if project in projects_map:
                return False               # already knows this project
            projects_map[project] = entry
            tmp = dst + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, dst)
    except (locks.LockBusy, OSError):
        return False
    return True


def _config_json(config_dir: str) -> str:
    """Claude Code's config file for a dir. The default dir keeps it in $HOME."""
    if os.path.abspath(config_dir) == os.path.abspath(DEFAULT_CONFIG):
        return os.path.join(HOME, ".claude.json")
    return os.path.join(config_dir, ".claude.json")


def account_for_email(email: str) -> str:
    """The account name signed in as an email, if we have one."""
    email = (email or "").lower()
    if not email:
        return ""
    for name in account_names():
        blob = keychain.read_credentials(slot_dir(name))
        if blob and (identity(slot_dir(name), blob)[0].get("email") or "").lower() == email:
            return name
    return ""


def account_of_dir(config_dir: str, accts: Iterable["Account"]) -> str:
    """Which account a config dir is signed in as, by name.

    An account's own directory answers by its name alone. Anything else, such
    as a directory a session was launched with before the rules existed, is
    matched on the credential it holds.
    """
    if not config_dir:
        return ""
    base = os.path.basename(os.path.abspath(config_dir).rstrip("/"))
    accts = list(accts)
    if os.path.dirname(os.path.abspath(config_dir).rstrip("/")) == ACCOUNTS_DIR:
        if any(a.name == base for a in accts):
            return base
    blob = keychain.read_credentials(config_dir)
    if not blob:
        return ""
    fp = fingerprint(blob)
    for a in accts:
        if fp and fingerprint(keychain.read_credentials(a.slot)) == fp:
            return a.name
    email = identity(config_dir, blob)[0].get("email") or recorded_email(config_dir)
    return next((a.name for a in accts if (a.email or "").lower() == email.lower()), email)


def dirs_to_accounts(dirs: Iterable[str], accts: Iterable["Account"]) -> dict[str, str]:
    """Name the account behind each config dir, reading each credential once.

    Doing this a directory at a time re-read every account's credential to
    compare against, and each read is a `security` call: the cost was the
    number of directories times the number of accounts. Here every credential
    is read once and matched by fingerprint.
    """
    accts = list(accts)
    by_fp: dict[str, str] = {}
    for a in accts:
        fp = fingerprint(keychain.read_credentials(a.slot))
        if fp:
            by_fp[fp] = a.name
    out: dict[str, str] = {}
    for d in dirs:
        if not d:
            continue
        base = os.path.basename(os.path.abspath(d).rstrip("/"))
        if is_account_dir(d) and any(a.name == base for a in accts):
            out[d] = base
            continue
        blob = keychain.read_credentials(d)
        if not blob:
            out[d] = ""
            continue
        name = by_fp.get(fingerprint(blob))
        if not name:
            email = (identity(d, blob)[0].get("email") or recorded_email(d) or "").lower()
            name = next((a.name for a in accts if (a.email or "").lower() == email), email)
        out[d] = name
    return out


def rules_using(account: str, r: Optional[profiles.Rules] = None) -> list[str]:
    """Every rule pointing at an account, described for a human."""
    r = r or rules()
    out = []
    if r.default_account == account:
        out.append("default")
    out += [f"profile {p.name}" for p in r.profiles if p.account == account]
    out += [f"project {os.path.basename(k.rstrip('/'))}" for k, v in r.projects.items() if v == account]
    n = sum(1 for v in r.sessions.values() if v == account)
    if n:
        out.append(f"{n} session{'s' if n != 1 else ''}")
    return out


def bootstrap() -> profiles.Rules:
    """Start a rule set for someone who has none.

    Everything goes to whichever account the default config dir is already
    signed in as, and no profiles are made up: a profile is a statement about
    how someone organises their repositories, which only they can make.
    """
    r = rules()
    if r.default_account or r.profiles or r.projects:
        return r
    blob = keychain.read_credentials(DEFAULT_CONFIG)
    guess = account_for_email(identity(DEFAULT_CONFIG, blob)[0].get("email") or "")
    r.default_account = guess or (account_names() or [""])[0]
    if r.default_account:
        save_rules(r)
    return r


def assign(scope: str, key: str, account: str, cwd: str = "",
           live: Optional[Iterable["sessions.Session"]] = None,
           applied_out: Optional[dict] = None) -> tuple[bool, str]:
    """Point one scope at an account. The scope decides how far it reaches.

    Nothing is copied and no session is disturbed: this rewrites the rules and
    the table the shell reads, and takes effect the next time a session starts.
    Project settings follow the project so a move does not re-ask for trust.
    """
    try:
        account = resolve_account(account)
    except UnknownAccount as e:
        return False, str(e)
    r = rules()
    moved: list[str] = []
    if scope == "session":
        if not key:
            return False, "this session has no terminal id, so it cannot be pinned"
        before = account_dir(r.account_for(project_root(cwd or HOME))[0] or account)
        r.set_session(key, account)
        moved = [project_root(cwd)] if cwd else []
        where = "this session"
    elif scope == "project":
        root = project_root(key or cwd)
        before = account_dir(r.account_for(root)[0] or account)
        r.set_project(root, account)
        moved, where = [root], f"“{os.path.basename(root)}”"
    elif scope == "profile":
        prof = r.profile(key)
        if not prof:
            return False, f"no profile named {key}"
        before = account_dir(prof.account or account)
        r.set_profile_account(key, account)
        moved = prof.paths
        where = f"profile “{key}” ({len(prof.repos)} repo{'s' if len(prof.repos) != 1 else ''})"
    elif scope == "default":
        before = account_dir(r.default_account or account)
        r.default_account = account
        where = "everything with no rule"
    else:
        return False, f"unknown scope {scope}"
    save_rules(r)
    for root in moved:
        carry_project_state(root, before, account_dir(account))
    return True, landed(f"{where} now uses {account}", live, applied_out)


def landed(where: str, live: Optional[Iterable["sessions.Session"]] = None,
           applied_out: Optional[dict] = None) -> str:
    """Push the rules that were just saved to live sessions, and report.

    Every rule edit ends the same way, so the sentence a user reads is written
    once. `applied_out` gives the caller what each directory was handed, so a
    menu can redraw from it instead of reading the directories back.
    """
    moved, applied = apply_now(live)
    if applied_out is not None:
        applied_out.update(applied)
    if not moved:
        return where
    n = len(moved)
    return (f"{where}. {n} running session{'s' if n != 1 else ''} "
            f"switch{'' if n != 1 else 'es'} within about 30 seconds")


def apply_now(live: Optional[Iterable["sessions.Session"]] = None
              ) -> tuple[list[str], dict[str, str]]:
    """Hand the current rules to every live session that has a dir of its own.

    This is what makes a rule change land without a restart: the session re-reads
    its keychain item about every thirty seconds, so writing the new credential
    into the dir it is already running in moves it, and nothing else.

    A session started before it had a dir of its own is skipped; there is
    nowhere to write that only it would see.

    Returns the sessions that moved and, for the caller that wants to redraw
    without reading anything back, what each directory was given.
    """
    moved: list[str] = []
    applied: dict[str, str] = {}
    for sess in (live if live is not None else sessions.live(credential_dirs())):
        if not sess.term_id:
            continue
        path = session_dir(sess.term_id)
        if os.path.abspath(path) != os.path.abspath(sess.env_config_dir):
            continue                 # it is not reading this dir
        account, _ = resolve(sess.cwd, sess.term_id)
        want = live_blob(account_dir(account)) if account else None
        if want and fingerprint(keychain.read_credentials(path)) != fingerprint(want):
            if adopt(path, want):
                moved.append(sess.label)
                applied[path] = account
    return moved, applied


def clear(scope: str, key: str, cwd: str = "",
          live: Optional[Iterable["sessions.Session"]] = None,
          applied_out: Optional[dict] = None) -> tuple[bool, str]:
    """Drop a rule so the level above it decides again."""
    r = rules()
    if scope == "session":
        if key not in r.sessions:
            return False, "this session has no rule of its own"
        r.set_session(key, None)
        where = "this session"
    elif scope == "project":
        root = project_root(key or cwd)
        found = r.project_rule_for(root)
        if not found:
            return False, "this project has no rule of its own"
        r.projects.pop(found, None)
        where = f"“{os.path.basename(root)}”"
    else:
        return False, f"unknown scope {scope}"
    save_rules(r)
    return True, landed(f"{where} follows its profile again", live, applied_out)


def add_profile(name: str, account: str = "",
                live: Optional[Iterable["sessions.Session"]] = None,
                applied_out: Optional[dict] = None) -> tuple[bool, str]:
    name = name.strip()
    if not name:
        return False, "a profile needs a name"
    r = rules()
    if r.profile(name):
        return False, f"there is already a profile named {name}"
    if account:
        try:
            account = resolve_account(account)
        except UnknownAccount as e:
            return False, str(e)
    r.profiles.append(profiles.Profile(name=name, account=account or r.default_account))
    save_rules(r)
    return True, landed(f"profile “{name}” created", live, applied_out)


def remove_profile(name: str,
                   live: Optional[Iterable["sessions.Session"]] = None,
                   applied_out: Optional[dict] = None) -> tuple[bool, str]:
    r = rules()
    if not r.profile(name):
        return False, f"no profile named {name}"
    r.profiles = [p for p in r.profiles if p.name != name]
    save_rules(r)
    return True, landed(f"profile “{name}” removed; its repos follow the default again",
                        live, applied_out)


def rename_profile(old: str, new: str) -> tuple[bool, str]:
    r = rules()
    prof = r.profile(old)
    new = new.strip()
    if not prof:
        return False, f"no profile named {old}"
    if not new or r.profile(new):
        return False, "pick a name that is not already taken"
    prof.name = new
    save_rules(r)
    return True, f"“{old}” is now “{new}”"


def profile_add_repo(name: str, path: str,
                     live: Optional[Iterable["sessions.Session"]] = None,
                     applied_out: Optional[dict] = None) -> tuple[bool, str]:
    r = rules()
    prof = r.profile(name)
    if not prof:
        return False, f"no profile named {name}"
    root = project_root(path)
    before = account_dir(r.account_for(root)[0] or prof.account)
    r.add_repo(name, root)
    save_rules(r)
    if prof.account:
        carry_project_state(root, before, account_dir(prof.account))
    return True, landed(f"{os.path.basename(root)} joined “{name}”",
                        live, applied_out)


def profile_remove_repo(name: str, path: str,
                        live: Optional[Iterable["sessions.Session"]] = None,
                        applied_out: Optional[dict] = None) -> tuple[bool, str]:
    r = rules()
    if not r.profile(name):
        return False, f"no profile named {name}"
    r.remove_repo(name, project_root(path))
    save_rules(r)
    return True, landed(f"{os.path.basename(project_root(path))} left “{name}”",
                        live, applied_out)


# --------------------------------------------------------------------------- session pins

