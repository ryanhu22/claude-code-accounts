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
import re
import subprocess
import time
import urllib.error
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import codex, keychain, locks, profiles, sessions

if TYPE_CHECKING:
    from . import oauth

HOME = os.path.expanduser("~")
ACCOUNTS_DIR = os.environ.get("CCM_ACCOUNTS_DIR", os.path.join(HOME, ".claude-accts"))
DEFAULT_CONFIG = os.path.join(HOME, ".claude")
PREFS_FILE = os.path.join(profiles.CCM_HOME, "prefs.json")

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TOKEN_URLS = ("https://platform.claude.com/v1/oauth/token",
              "https://console.anthropic.com/v1/oauth/token")
API = "https://api.anthropic.com"
# The server gates new models on the Claude Code version the client claims, so
# a number pinned here goes stale and starts refusing models the installed CLI
# can use. Ask the CLI instead, and keep a recent one for when it cannot be
# found. Seen as: "Claude Code 2.1.236 does not support this model; version
# 2.1.251 or newer is required."
UA_FALLBACK_VERSION = "2.1.261"
ANTHROPIC_VERSION = "2023-06-01"
# Poking sends one request per window that has no clock. The five hour and the
# general weekly window start on any request; the Fable weekly window is scoped
# to that model and only starts on a request to it.
POKE_MODEL = "claude-haiku-4-5-20251001"
POKE_MODEL_SCOPED = {"fable": "claude-fable-5-1"}
AUTO_START_PREF = "auto_start_weekly"
AUTO_START_RETRY = 3600.0

_UA: str | None = None


def pref(key, default=None):
    """Read the whole file so another menu process's choices are visible."""
    try:
        with open(PREFS_FILE) as f:
            data = json.load(f)
        return data.get(key, default) if isinstance(data, dict) else default
    except (OSError, ValueError):
        return default


def set_pref(key, value):
    """Replace the file atomically so a reader never sees half a preference."""
    try:
        with open(PREFS_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[key] = value
    try:
        os.makedirs(os.path.dirname(PREFS_FILE), exist_ok=True)
        tmp = PREFS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, PREFS_FILE)
    except OSError:
        pass


def user_agent() -> str:
    """What to call ourselves, at the version of the CLI that is installed."""
    global _UA
    if _UA is None:
        version = UA_FALLBACK_VERSION
        try:
            out = subprocess.run(["claude", "--version"], capture_output=True,
                                 text=True, timeout=5).stdout
            found = re.search(r"(\d+\.\d+\.\d+)", out)
            if found:
                version = found.group(1)
        except Exception:
            pass          # not on PATH, or slow to answer: the fallback is fine
        _UA = f"claude-cli/{version} (external, cli)"
    return _UA


def oauth_headers() -> dict:
    return {"anthropic-beta": "oauth-2025-04-20", "User-Agent": user_agent()}


# --------------------------------------------------------------------------- http

def _post(url: str, body: dict, token: str | None = None, timeout: int = 30,
          extra_headers: dict | None = None) -> dict:
    import urllib.request

    headers = {"Content-Type": "application/json", **oauth_headers(), **(extra_headers or {})}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _get(path: str, token: str, timeout: int = 20) -> dict:
    import urllib.request

    req = urllib.request.Request(API + path, headers={
        "Authorization": f"Bearer {token}", **oauth_headers()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# --------------------------------------------------------------------------- tokens

def fingerprint(blob: dict | None) -> str | None:
    """Identity of a credential GENERATION, from its refresh token.

    Two config dirs showing the same fingerprint hold the same copy of one
    login, so a rotation in either one strands the other.
    """
    token = (blob or {}).get("refreshToken")
    return hashlib.sha256(token.encode()).hexdigest()[:16] if token else None


def expiring(blob: dict | None, margin: float = 120) -> bool:
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


def refresh(blob: dict) -> tuple[dict | None, str | None]:
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


def token_account(resp: dict) -> str | None:
    """The email a grant response names, when it names one.

    Identity for free, and from the same exchange that produced the token, so
    it cannot describe a different account than the one we just wrote.
    """
    acct = resp.get("account")
    if not isinstance(acct, dict):
        return None
    return acct.get("email_address") or acct.get("email")


def carry_identity(config_dir: str, old_fp: str | None, new_fp: str | None) -> None:
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


def propagate(spent: str | None, resp: dict, skip: str) -> list[str]:
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
        if fingerprint(keychain.read_credentials(d, max_age=keychain.RECENT)) != spent:
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


def _persist(config_dir: str, rotated: dict, spent: str | None) -> bool:
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


def _take_stash(config_dir: str, current: dict | None) -> dict | None:
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


def live_blob(config_dir: str, allow_refresh: bool = True) -> dict | None:
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


def plan_label(tier: str, account: dict | None = None) -> str:
    label = " ".join(w.title() if w.isalpha() else w
                     for w in tier.replace("default_claude_", "").split("_") if w)
    if not label:
        account = account or {}
        label = ("Max" if account.get("has_claude_max")
                 else "Pro" if account.get("has_claude_pro") else "")
    return label


def profile_result(token: str | None) -> tuple[dict, str | None]:
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
    label = plan_label(tier, account)
    return {"email": account.get("email"), "plan": label or None,
            "tier": tier or None,          # raw, for writing a credential
            "extra_usage": org.get("has_extra_usage_enabled")}, None


def profile(token: str | None) -> dict:
    """Live account facts for a token: identity and plan.

    Read from the API rather than from the stored credential blob. The blob's
    `subscriptionType` and `rateLimitTier` are snapshots taken at login and go
    stale: an account upgraded after signing in still reports its old tier
    there (observed: blob said max_5x while the account was really max_20x).
    """
    return profile_result(token)[0]


IDENTITY_CACHE = os.path.join(ACCOUNTS_DIR, ".identity.json")


def identity(config_dir: str, blob: dict | None) -> tuple[dict, str | None]:
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


def whoami(token: str | None) -> str | None:
    """The account a token belongs to. Identity is never inferred from a name."""
    return profile(token).get("email")


# --------------------------------------------------------------------------- model

@dataclass
class Limit:
    kind: str
    label: str
    percent: float
    resets_at: str | None
    span: int = 0              # window length in seconds, 0 when unknown
    scope: str = ""            # empty for an account-wide window

    @property
    def resets_in(self) -> str:
        return human_delta(self.resets_at)

    @property
    def over(self) -> bool:
        """True once this window's reset time has passed."""
        if not self.resets_at:
            return False
        try:
            when = _dt.datetime.fromisoformat(str(self.resets_at).replace("Z", "+00:00"))
        except ValueError:
            return False
        return when <= _dt.datetime.now(_dt.timezone.utc)

    @property
    def spent(self) -> float:
        """How much of this window is used, as of now.

        `percent` is what the server said when the payload was fetched. The
        menu repaints between fetches and the payload can be minutes old, so a
        window that has rolled over since then has to be read from the clock
        instead. Without this a spent window kept drawing its old bar, in its
        old colour, next to a countdown reading "now".
        """
        return 0.0 if self.over else self.percent


@dataclass
class Account:
    name: str
    slot: str
    email: str | None = None
    plan: str | None = None
    limits: list[Limit] = field(default_factory=list)
    error: str | None = None
    mismatch: str | None = None   # holds a different account than its name
    checked_at: float = 0.0
    usage_at: float = 0.0        # when the usage payload was fetched, 0 if never
    provider: str = "claude"
    extras: dict = field(default_factory=dict)

    @property
    def is_codex(self) -> bool:
        return self.provider == "codex"

    @property
    def reading(self) -> bool:
        """Whether the usage numbers on this account mean anything."""
        return has_reading(self.limits)

    @property
    def usage_age(self) -> float:
        return time.time() - self.usage_at if self.usage_at else 0.0

    @property
    def stale(self) -> bool:
        """True once the usage numbers are old enough to warn about.

        Above a rate limit plus a refresh cycle. The limit clears in 300
        seconds and the cycle is 180, so numbers can reach about 480 seconds
        old with nothing wrong at all, and a threshold under that reported the
        ordinary case as a fault.
        """
        return self.usage_age > 600

    def limit(self, kind: str) -> Limit | None:
        return next((lim for lim in self.limits if lim.kind == kind), None)

    @property
    def session_pct(self) -> float | None:
        lim = self.limit("session")
        return lim.spent if lim else None

    @property
    def weekly_pct(self) -> float | None:
        lim = self.limit("weekly_all")
        return lim.spent if lim else None

    @property
    def ok(self) -> bool:
        return self.email is not None and self.error is None

    @property
    def signed_in(self) -> bool:
        """Identity is confirmed. Usage may still be missing: a busy account
        rate limits the usage endpoint, and that is not a login problem."""
        return self.email is not None


def human_delta(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return ""
    mins = int((dt - _dt.datetime.now(_dt.timezone.utc)).total_seconds() // 60)
    if mins <= 0:
        return "now"
    if mins < 60:
        return f"{mins}m"
    if mins < 48 * 60:
        return f"{mins // 60}h {mins % 60}m"
    return f"{mins // 1440}d {(mins % 1440) // 60}h"


def has_reading(limits: list) -> bool:
    """Whether a usage payload actually says anything.

    The server either enumerates the windows or it does not. An enumerated
    window reading zero with no reset time is an answer: the window has not
    been opened, or it rolled over and nothing has been spent since. An
    account that has genuinely used nothing reads exactly that way, and
    calling it "no reading" hid a full allowance behind a dash.

    This once tested for a non-zero number instead, to catch an account that
    showed three running sessions and a whole allowance left. That was the
    wrong test: the zeros there were real, from windows that had rolled over.
    An absent answer is an empty list, which is what this returns False for.
    """
    return bool(limits)


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
        out.append(Limit(kind=kind, label=label, percent=pct, resets_at=resets,
                         span=18000 if kind == "session" else 604800,
                         scope="" if kind in ("session", "weekly_all") else label))
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
# The shortest gap between ordinary checks of one account. The poll runs every
# 180 seconds, so this never delays it; what it stops is everything else that
# asks for a collection. Starting the app is one, and restarting it eight times
# in a few minutes cost forty requests against a budget of about four per five
# minutes per account, which rate limited two accounts and left their rows
# reading "usage from 13m ago". A payload two minutes old is not worth a
# request that can park a row for five.
_MIN_AGE = 150


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


def _retry_after(err) -> float | None:
    """How long the server asked us to wait, when it says so in seconds."""
    try:
        return max(0.0, float(err.headers.get("Retry-After")))
    except (AttributeError, TypeError, ValueError):
        return None


def _waiting(entry: dict, now: float) -> str | None:
    """Why there is nothing to show for this account, counted from now.

    Only for the case where there is also no cached payload. A wait is not an
    error while there are numbers on screen: it clears itself within five
    minutes, which is sooner than the age at which a row calls itself stale,
    so in the ordinary case there is nothing to say and saying it is noise.

    The stored sentence is written once, when the 429 lands, so its countdown
    would be as old as that moment. The deadline behind it is absolute, so the
    sentence is rebuilt from that each time it is read.
    """
    left = (entry.get("retry_after") or 0) - now
    if left <= 0:
        return entry.get("last_error")
    return f"rate limited, retrying in {max(1, round(left / 60))}m"


def _usage(name: str, fetch, force: bool = False,
           who: str = "", parse=_parse_limits) -> tuple[list[Limit], float, str | None]:
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
    # Cached against whose usage it is, not just the account name. Signing a
    # slot in as somebody else and serving the old payload would show one
    # subscription's usage under another's name, which reads as two accounts
    # with identical bars rather than as stale data.
    #
    # The identity has to be the email and not the credential. This used to
    # compare the refresh token's fingerprint, but an ordinary refresh rotates
    # that token, so the check fired about hourly on an account that had not
    # changed hands: it blanked the row, dropped the wait a 429 had set, and
    # sent the retry back inside the server's window.
    if who and entry.get("who") and entry["who"] != who:
        entry = {}
    cached, at = entry.get("data"), entry.get("at", 0.0)
    now = time.time()
    # A forced check skips the wait, but not entirely: clicking refresh at a
    # rate limit should not add requests that can only prolong it.
    # A cached payload that says nothing counts as no payload here, so the
    # wait gets reported rather than swallowed: the row has no numbers to show
    # and the reason it has none is the only thing left to say.
    told = parse(cached) if cached else []
    speak = None if has_reading(told) else _waiting(entry, now)
    if force and now - (entry.get("tried_at") or 0) < _FORCE_FLOOR:
        return told, at, speak
    if cached and not force and now < entry.get("retry_after", 0):
        return told, at, speak
    if told and not force and now - at < _MIN_AGE:
        return told, at, speak
    try:
        data = fetch()
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
            return parse(cached), at, None
        return [], 0.0, f"usage HTTP {code}" if code else str(e)[:60]
    store[name] = {"data": data, "at": now, "retry_after": 0,
                   "who": who, "tried_at": now}
    # A usage refresh must not make automatic start due again.
    if "auto_start_at" in entry:
        store[name]["auto_start_at"] = entry["auto_start_at"]
    _cache_write(store)
    return parse(data), now, None


def forget_usage(name: str) -> None:
    """Clear the attempt floor so the refresh after a poke can fetch usage.

    A poke's own pre-check stamps the floor that the refresh after it then
    trips over, so the row stayed "unused" until the next poll. Preserve the
    cached payload and any retry deadline while allowing another attempt.
    """
    store = _cache_read()
    if name in store:
        store[name]["tried_at"] = 0
        _cache_write(store)


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

    Exact name wins, then a unique prefix, then a unique substring, so "wo"
    finds "work", "son" finds "personal" and "cm" finds
    "acme" without anyone maintaining an alias table.
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


def codex_account_names() -> list[str]:
    return codex.account_names()


def resolve_any(query: str) -> tuple[str, str]:
    """Accept the same nicknames across providers, refusing ambiguous ones."""
    names = [("claude", n) for n in account_names()] + [("codex", n) for n in codex_account_names()]
    q = query.lower()
    for pool in ([a for a in names if a[1] == query],
                 [a for a in names if a[1].lower().startswith(q)],
                 [a for a in names if q in a[1].lower()]):
        if len(pool) == 1:
            return pool[0]
        if len(pool) > 1:
            raise UnknownAccount(f"{query!r} matches {', '.join(n for _, n in pool)}")
    have = ", ".join(n for _, n in names) or "none"
    raise UnknownAccount(f"no account matches {query!r} (have: {have})")


def find_live_blob(email: str, prefer: str | None = None) -> dict | None:
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
        blob = (live_blob(cand) if os.path.abspath(cand) in slots
                else keychain.read_credentials(cand, max_age=keychain.RECENT))
        if blob and (identity(cand, blob)[0].get("email") or "").lower() == email.lower():
            return blob
    return None


def _cached_email(config_dir: str) -> str:
    """The last identity confirmed for a dir, whatever it holds now."""
    entry = _cache_read(IDENTITY_CACHE).get(os.path.abspath(config_dir)) or {}
    return entry.get("email") or ""


def is_account_dir(config_dir: str) -> bool:
    return os.path.dirname(os.path.abspath(config_dir).rstrip("/")) == ACCOUNTS_DIR


def adopt(config_dir: str, blob: dict, email: str = "", rebind: bool = False,
          keep_newer: bool = False) -> bool:
    """Write a credential into a config dir under Claude Code's locks.

    Putting a credential in a dir can change whose dir it is, so the cached
    identity for it is dropped unless the caller can name the account. Trusting
    a stale entry here is how one account's dir comes to be described as
    another's, which then spreads: the answer is used to decide what to copy
    where. With keep_newer and a known email, re-read under the lock so a
    session rotation cannot be replaced by an older copy of the same account.
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
            if keep_newer and email:
                have = keychain.read_credentials(config_dir, max_age=0)
                if fingerprint(have) == fingerprint(blob):
                    return False
                if (have and _cached_email(config_dir) == email.lower()
                        and (have.get("expiresAt") or 0) > (blob.get("expiresAt") or 0)):
                    return False
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


def hand_out(path: str, want: dict, email: str = "", *, account: str = "") -> bool:
    """Give a session an account's credential unless its copy is ahead.

    The memoized pre-read avoids taking Claude Code's locks for every session
    that is already current. Adopt checks again under the lock because a
    rotation can land between the pre-read and the write. Refresh tokens are
    single use, so the write must never clobber a newer same-account copy.
    Leave that copy for sync to promote. A different account means the dir
    has not caught up with a rule change; replace it at any age.

    Check the identity on every pass when the dir is known to hold this account's
    login, so a stale oauthAccount heals without waiting for a move.
    """
    have = keychain.read_credentials(path, max_age=keychain.RECENT)
    same = fingerprint(have) == fingerprint(want)
    wrote = False
    if not same:
        wrote = adopt(path, want, email=email, keep_newer=True)
        same = wrote or bool(email and _cached_email(path) == email.lower())
    if account and same:
        _sync_identity(path, account)
    return wrote


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
    # it just answers them about the wrong subscription, which reads as two
    # accounts reporting identical usage rather than as a fault. Say it.
    was = recorded_email(slot)
    if was and email and was.lower() != email.lower():
        # Its own field: the usage fetch below sets `error`, and would
        # otherwise clear this the moment usage came back fine, which it
        # does, because the credential works. It is just the wrong one.
        acct.mismatch = f"holds {email}, not {was}"
    if with_usage:
        acct.limits, acct.usage_at, acct.error = _usage(
            name, lambda: _get("/api/oauth/usage", blob["accessToken"]),
            force, (email or "").lower())
    return acct


def load_codex_account(name: str, with_usage: bool = True, force: bool = False) -> Account:
    slot = codex.slot_dir(name)
    acct = Account(provider="codex", name=name, slot=slot, checked_at=time.time())
    if not os.path.islink(slot):
        codex.ensure_account_dir(name)
    auth = codex.live_auth(slot)
    if auth is None:
        raw = codex.read_auth(slot)
        acct.error = "not signed in" if not raw else "login expired"
        return acct
    info = codex.identity(auth)
    if not info.get("email"):
        acct.error = "login unreadable"
        return acct
    acct.email, acct.plan = info["email"], info["plan"]
    if with_usage:
        key = f"codex:{name}"
        acct.limits, acct.usage_at, acct.error = _usage(
            key, lambda: codex.fetch_usage(auth), force, acct.email.lower(),
            parse=codex.parse_limits)
        entry = _cache_read().get(key) or {}
        data = entry.get("data")
        # A slot may have changed hands outside the manager. Never put the old
        # payload's identity back after the cache has refused its usage.
        if data and (not entry.get("who") or entry["who"] == acct.email.lower()):
            acct.extras = codex.extras(data)
            if data.get("plan_type"):
                acct.plan = codex.plan_label(data["plan_type"])
            if data.get("email"):
                acct.email = data["email"]
    return acct


_CODEX_ADOPTED = False


def all_accounts(with_usage: bool = True, force: bool = False) -> list[Account]:
    global _CODEX_ADOPTED
    if not _CODEX_ADOPTED:
        codex.adopt_default()
        _CODEX_ADOPTED = True
    return ([load_account(n, with_usage, force) for n in account_names()]
            + [load_codex_account(n, with_usage, force) for n in codex_account_names()])


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
    global _CHIPS
    import shutil
    new = "".join(c for c in new.strip() if c.isalnum() or c in "-_")
    if not new:
        return False, "name must contain letters, digits, - or _"
    if new == old:
        return True, "unchanged"
    if new in account_names() or new in codex_account_names():
        return False, f"{new} already exists"
    is_codex = old in codex_account_names()
    if is_codex:
        ok, msg = codex.rename_account(old, new)
        if not ok:
            return ok, msg
    else:
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
        keychain.delete_credentials(old_slot)
        shutil.rmtree(old_slot, ignore_errors=True)
        store = _cache_read(IDENTITY_CACHE)
        if os.path.abspath(old_slot) in store:
            store[os.path.abspath(new_slot)] = store.pop(os.path.abspath(old_slot))
            _cache_write(store, IDENTITY_CACHE)
        # Saving rules creates Claude slots, so rewrite only after their move lands.
        _replace_account_rules(old, new)
    store = _cache_read()
    old_key, new_key = (f"codex:{old}", f"codex:{new}") if is_codex else (old, new)
    if old_key in store:
        store[new_key] = store.pop(old_key)
        _cache_write(store)
    try:                                     # keep its colour through the rename
        with open(CHIP_FILE) as f:
            table = json.load(f)
        if old in table:
            table[new] = table.pop(old)
            with open(CHIP_FILE, "w") as f:
                json.dump(table, f, indent=2)
    except (OSError, ValueError):
        pass
    _CHIPS = (-1.0, {})
    return True, f"{old} is now {new}"


def sign_in_begin(name: str, redirect_uri: str = "") -> oauth.Attempt:
    """Start signing an account in. Returns the attempt to hand back later.

    The account this slot last held is offered to the sign-in page, so the
    browser lands on the right one instead of whichever it is already signed
    into. Switching accounts part way through is what loses the code.
    """
    from . import oauth

    hint = recorded_email(slot_dir(name)) or _cached_email(slot_dir(name))
    return oauth.begin(name, redirect_uri or oauth.CALLBACK_URL, login_hint=hint)


def sign_in_finish(attempt: oauth.Attempt, pasted: str) -> tuple[bool, str]:
    """Exchange a pasted code and store the credential for that account.

    Written only after the API has confirmed the identity and the plan. A
    credential that cannot report its own plan opens sessions as API billing,
    so it is refused rather than saved: a login that half works is harder to
    diagnose than one that never happened.
    """
    from . import oauth

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
                                          "plan": plan_label(blob.get("rateLimitTier") or "")
                                          or blob.get("subscriptionType"),
                                          "at": time.time()}}, IDENTITY_CACHE)
    try:
        _account_identity(attempt.account)
    except Exception:
        pass
    if before and before.lower() != email.lower():
        # The browser signs in as whoever it was already logged into, which is
        # how an account once ended up holding another one's token.
        return True, (f"“{attempt.account}” is now signed in as {email}, but it "
                      f"used to be {before}. If that is wrong, sign in again in a "
                      f"private window.")
    return True, f"“{attempt.account}” is signed in as {email}"


def sign_in_begin_codex(name: str) -> codex.Attempt:
    return codex.begin(name)


def sign_in_finish_codex(attempt: codex.Attempt, code: str, state: str) -> tuple[bool, str]:
    auth, result = codex.finish(attempt, code, state)
    if auth is None:
        return False, result
    email = result
    try:
        slot = codex.ensure_account_dir(attempt.account)
        before = codex.identity(codex.read_auth(slot) or {}).get("email") or ""
        codex.write_auth(slot, auth)
    except OSError:
        return False, "signed in, but the login could not be stored"
    store = _cache_read()
    key = f"codex:{attempt.account}"
    cached_email = (store.get(key) or {}).get("who") or ""
    if before.lower() != email.lower() or (cached_email and cached_email != email.lower()):
        store.pop(key, None)
        _cache_write(store)
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
    global _CHIPS
    if name in codex_account_names():
        ok = codex.remove_account(name)
        cache_key = f"codex:{name}"
    else:
        import shutil
        slot = slot_dir(name)
        ok = keychain.delete_credentials(slot)
        shutil.rmtree(slot, ignore_errors=True)
        _replace_account_rules(name, "")
        store = _cache_read(IDENTITY_CACHE)
        if os.path.abspath(slot) in store:
            store.pop(os.path.abspath(slot))
            _cache_write(store, IDENTITY_CACHE)
        _drop_stash(slot)
        cache_key = name
    store = _cache_read()
    if cache_key in store:
        store.pop(cache_key)
        _cache_write(store)
    table = dict(_chip_table())
    if name in table:
        table.pop(name)
        _cache_write(table, CHIP_FILE)
    _CHIPS = (-1.0, {})
    return ok


def _one_poke(blob: dict, model: str) -> None:
    """One minimal request, which is all it takes to start a window."""
    _post(f"{API}/v1/messages", {
        "model": model,
        "max_tokens": 1,
        "system": [{"type": "text",
                    "text": "You are Claude Code, Anthropic's official CLI for Claude."}],
        "messages": [{"role": "user", "content": "hi"}],
    }, token=blob["accessToken"], timeout=45,
        # The Messages API rejects a request without it; the OAuth
        # endpoints do not use it, which is why it is not in oauth_headers().
        extra_headers={"anthropic-version": ANTHROPIC_VERSION})


def poke(name: str, *, weekly_only: bool = False) -> tuple[bool, str]:  # noqa: D401
    """Spend a few tokens on an account to start every window that has no clock.

    A freshly reset account sits at 0% with no window running, so a countdown
    only starts on first use. This starts them deliberately, for about 22 input
    tokens each, so the windows are aligned with when you want them.

    There is more than one clock. The five hour window and the general weekly
    window start on any request. A model-scoped weekly window, which is what
    the Fable row is, only starts on a request to that model, so starting the
    others left it reading "unused" and the account looked half awake. Each
    window that still has no reset time gets the request that starts it.
    """
    try:
        if name in codex_account_names():
            return False, "Poking a Codex account is not supported"
        try:
            name = resolve_account(name)
        except UnknownAccount as original:
            # Claude nicknames keep their meaning even when Codex has a match.
            try:
                provider, _ = resolve_any(name)
            except UnknownAccount:
                raise original from None
            if provider == "codex":
                return False, "Poking a Codex account is not supported"
            raise original
    except UnknownAccount as e:
        return False, str(e)
    blob = live_blob(slot_dir(name))
    if not blob:
        return False, "not signed in"

    # Which windows are stopped, and the cheapest set of models that starts
    # them. A scoped window needs its own model; everything else rides along
    # with any request, so the general model is only sent when it is the only
    # thing that would start a window.
    limits, _, _ = _usage(name, lambda: _get("/api/oauth/usage", blob["accessToken"]), force=True)
    stopped = [lim for lim in limits if not lim.resets_at
               and (not weekly_only or lim.span == 604800)]
    if limits and not stopped:
        if weekly_only:
            return True, "every weekly window is already running"
        return True, "every window is already running"
    # A request to a scoped model starts that window AND the general ones, so
    # when a scoped window is stopped its own model is the whole job. The
    # general model is only needed when nothing scoped is going out.
    models: list[str] = []
    for lim in stopped:
        model = POKE_MODEL_SCOPED.get(lim.label)
        if model and model not in models:
            models.append(model)
    if not models:
        models = [POKE_MODEL]

    started, failed = [], []
    for model in models:
        try:
            _one_poke(blob, model)
            started.append(model)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode()).get("error", {}).get("message", "")
            except Exception:
                pass
            failed.append(f"{model}: {detail or f'HTTP {e.code}'}")
        except Exception as e:
            failed.append(f"{model}: {str(e)[:60]}")
    if started:
        forget_usage(name)
    if failed and not started:
        return False, "; ".join(failed)
    if failed:
        return False, f"started {len(started)} of {len(models)}. {'; '.join(failed)}"
    return True, f"{len(started)} window group(s) started"


def stopped_weekly(acct: Account) -> list[Limit]:
    """Find weekly windows that can start on this signed-in Claude account."""
    if acct.is_codex or not acct.signed_in or not acct.reading:
        return []
    return [lim for lim in acct.limits if lim.span == 604800 and not lim.resets_at]


def auto_start_due(accts: Iterable[Account], now: float,
                   attempts: dict[str, float]) -> list[str]:
    return [acct.name for acct in accts if stopped_weekly(acct)
            and now - attempts.get(acct.name, 0) >= AUTO_START_RETRY]


def auto_start_attempts() -> dict[str, float]:
    return {name: entry["auto_start_at"] for name, entry in _cache_read().items()
            if "auto_start_at" in entry}


def note_auto_start(name: str, now: float) -> None:
    store = _cache_read()
    store.setdefault(name, {})["auto_start_at"] = now
    _cache_write(store)


def auto_start(accts: Iterable[Account]) -> list[tuple[str, bool, str]]:
    """Start weekly windows before idle time pushes their next reset later.

    Starting a 5-hour window while idle gains nothing, so only stopped weekly
    windows count. The request starts the 5-hour window too. Attempts stay an
    hour apart per account so a failure does not send a request every refresh.
    This sends real requests on the user's behalf. Automatic start stays off
    unless they turn it on.
    """
    results: list[tuple[str, bool, str]] = []
    try:
        if not pref(AUTO_START_PREF, False):
            return results
        now = time.time()
        names = auto_start_due(accts, now, auto_start_attempts())
        for name in names:
            try:
                # Count the attempt even if the process stops during the request.
                note_auto_start(name, time.time())
                ok, msg = poke(name, weekly_only=True)
            except Exception as e:
                ok, msg = False, str(e)
            results.append((name, ok, msg))
    except Exception:
        pass          # a broken preference or cache is not a failed start
    return results


# --------------------------------------------------------------------------- contexts

def context_owners(paths: Iterable[str], accts: Iterable[Account]) -> dict[str, str]:
    """Which account each context is signed in as, without asking the API.

    A context holds a copy of an account's credential, so equal refresh tokens
    already identify it. Only a context matching no account costs a request,
    which keeps the panel honest while the API is rate limiting us: a failed
    lookup used to just drop the "in use by" mark.
    """
    accts = [a for a in accts if a.provider == "claude"]
    by_fp: dict[str, str] = {}
    for a in accts:
        fp = fingerprint(keychain.read_credentials(a.slot, max_age=keychain.RECENT))
        if fp and a.email:
            by_fp[fp] = a.email
    out: dict[str, str] = {}
    for path in paths:
        blob = keychain.read_credentials(path, max_age=keychain.RECENT)
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


# Volatile, and wrong the moment it is copied.
_CONFIG_SKIP = ("cachedUsageUtilization", "cachedExtraUsageDisabledReason")


def _seed_config(path: str, account: str) -> None:
    """Give a new session dir the Claude Code state its account already has.

    Claude Code keeps its first run state in .claude.json: whether onboarding
    is done, which account it belongs to, and which projects are trusted. A
    session dir built from an empty directory has none of it, so Claude Code
    ran onboarding and asked for a sign in on every new terminal, even with a
    good credential waiting in the keychain.

    Copied once, when the dir is made. After that _sync_identity keeps the
    identity in step on every hand-out. Everything else is left to the session.
    """
    dst = _config_json(path)
    if os.path.exists(dst):
        return
    src = _config_json(account_dir(account)) if account else ""
    fallback = False
    if not src or not os.path.exists(src):
        src, fallback = _config_json(DEFAULT_CONFIG), True
    try:
        with open(src) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    for key in _CONFIG_SKIP:
        data.pop(key, None)
    if fallback:
        # The default dir is signed in as somebody, and it is not necessarily
        # this account. Better to say nothing than to name the wrong one.
        data.pop("oauthAccount", None)
    tmp = dst + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dst)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _account_identity(account: str) -> dict | None:
    """Keep a profile identity for accounts signed in through ccm too."""
    path = account_dir(account)
    dst = _config_json(path)
    try:
        with open(dst) as f:
            data = json.load(f)
        identity = data.get("oauthAccount") if isinstance(data, dict) else None
        if isinstance(identity, dict) and identity:
            return identity
    except (OSError, ValueError):
        pass
    try:
        blob = live_blob(path)
        if not blob or not blob.get("accessToken"):
            return None
        profile = _get("/api/oauth/profile", blob["accessToken"])
        identity = {}
        for section, fields in (
            ("account", {
                "uuid": "accountUuid", "email": "emailAddress",
                "display_name": "displayName", "full_name": "fullName",
                "created_at": "accountCreatedAt",
            }),
            ("organization", {
                "uuid": "organizationUuid", "name": "organizationName",
                "organization_type": "organizationType", "billing_type": "billingType",
                "has_extra_usage_enabled": "hasExtraUsageEnabled",
                "subscription_created_at": "subscriptionCreatedAt",
                "rate_limit_tier": "organizationRateLimitTier", "seat_tier": "seatTier",
            }),
        ):
            source = profile.get(section) or {}
            for key, target in fields.items():
                if source.get(key) is not None:
                    identity[target] = source[key]
        if not identity.get("accountUuid") or not identity.get("emailAddress"):
            return None
    except Exception:
        return None
    try:
        with locks.config(path):
            # Re-read under the lock to keep changes made during the request.
            try:
                with open(dst) as f:
                    data = json.load(f)
            except FileNotFoundError:
                data = {}
            if not isinstance(data, dict):
                return identity
            current = data.get("oauthAccount")
            if isinstance(current, dict) and current:
                return current
            data["oauthAccount"] = identity
            tmp = dst + ".tmp"
            try:
                with open(tmp, "w") as f:
                    json.dump(data, f)
                os.chmod(tmp, 0o600)
                os.replace(tmp, dst)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except (locks.LockBusy, OSError, ValueError):
        pass
    return identity


def _sync_identity(path: str, account: str) -> bool:
    """Make a session dir's .claude.json name the account it now holds.

    The account's own file or profile supplies its identity. The default dir
    may name somebody else. Drop cached usage when the identity changes because
    those numbers belong to the previous account. Leave a busy file alone;
    a later hand-out can try again.
    """
    try:
        identity = _account_identity(account)
        if not identity:
            return False
        dst = _config_json(path)
        with locks.config(path):
            with open(dst) as f:
                data = json.load(f)
            if not isinstance(data, dict) or data.get("oauthAccount") == identity:
                return False
            data["oauthAccount"] = identity
            for key in _CONFIG_SKIP:
                data.pop(key, None)
            tmp = dst + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, dst)
    except (locks.LockBusy, OSError, ValueError):
        return False
    return True


def prepare_session(term_id: str, account: str) -> str:
    """The dir a terminal should launch in, holding `account`'s credential."""
    path = session_dir(term_id)
    _seed_dir(path)
    _seed_config(path, account)
    want = live_blob(account_dir(account)) if account else None
    if want:
        hand_out(path, want, email=_cached_email(account_dir(account)), account=account)
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


def sync_credentials(live: Iterable[sessions.Session]) -> list[str]:
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
    # account_names stays Claude-only; Codex homes never enter the keychain pass.
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
        master = keychain.read_credentials(home, max_age=keychain.RECENT)
        if not master or not master.get("refreshToken"):
            continue
        # Promote a session copy only if it is newer and provably this account.
        # A copy that is ahead of the master is one of three things, and the
        # difference decides whether it may be written over, so the answer is
        # worked out once here and kept for the pass below.
        owner = (identity(home, master)[0].get("email") or "").lower()
        ahead: dict[str, str] = {}          # path -> mine | theirs | unknown
        copies: dict[str, dict | None] = {}
        for path in session_paths:
            b = keychain.read_credentials(path, max_age=keychain.RECENT)
            copies[path] = b
            if not b or not b.get("refreshToken"):
                continue
            if (b.get("expiresAt") or 0) <= (master.get("expiresAt") or 0):
                continue
            email = (identity(path, b)[0].get("email") or "").lower()
            ahead[path] = ("unknown" if not email or not owner else
                           "mine" if email == owner else "theirs")
            if ahead[path] == "mine" and adopt(home, b, email=owner, keep_newer=True):
                master = b
                healed.append(home)
        want = (identity(home, master)[0].get("email") or "")
        best = fingerprint(master)
        for path in session_paths:
            if fingerprint(copies[path]) == best:
                continue
            # Never write an older token over a newer one that could not be
            # identified. The promotion above passed on it because the endpoint
            # could not be reached, and "cannot ask right now" does not mean
            # "belongs to somebody else". That copy may be the only one left
            # that can refresh, and a refresh token is single use, so writing
            # the spent master here would end the lineage. Leave it and ask
            # again next pass. A copy confirmed to be another account's is a
            # dir that has not caught up with a rule change, and is replaced.
            if ahead.get(path) == "unknown":
                continue
            if adopt(path, master, email=want, keep_newer=True):
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
        keychain.delete_credentials(path)
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


def _replace_account_rules(old: str, new: str) -> None:
    """Carry rules with a renamed account, or clear them when it is removed."""
    r = rules()
    before = r.to_dict()
    if r.default_account == old:
        r.default_account = new
    for prof in r.profiles:
        if prof.account == old:
            prof.account = new
    for entries in (r.projects, r.sessions):
        for key, account in list(entries.items()):
            if account == old:
                if new:
                    entries[key] = new
                else:
                    entries.pop(key)
    if r.to_dict() != before:
        save_rules(r)


def prune_session_rules(live_terms: Iterable[str]) -> list[str]:
    """Keep quiet terminals pinned until directory cleanup declares them dead."""
    keep = set(live_terms)
    r = rules()
    before = r.to_dict()
    gone = [term for term in r.sessions
            if term not in keep and not os.path.exists(session_dir(term))]
    for term in gone:
        r.sessions.pop(term)
    if r.to_dict() != before:
        save_rules(r)
    return gone


def resolve(cwd: str, term_id: str = "", r: profiles.Rules | None = None
            ) -> tuple[str, str]:
    """Which account a session in `cwd` should bill to, and why.

    A worktree resolves to its parent checkout first, so it inherits whatever
    rule covers the repository even when it lives outside the repo directory.
    """
    r = r if r is not None else rules()
    return r.account_for((os.path.abspath(cwd), project_root(cwd)), term_id)


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
        blob = keychain.read_credentials(slot_dir(name), max_age=keychain.RECENT)
        if blob and (identity(slot_dir(name), blob)[0].get("email") or "").lower() == email:
            return name
    return ""


def account_of_dir(config_dir: str, accts: Iterable[Account]) -> str:
    """Which account a config dir is signed in as, by name.

    An account's own directory answers by its name alone. Anything else, such
    as a directory a session was launched with before the rules existed, is
    matched on the credential it holds.
    """
    accts = [a for a in accts if a.provider == "claude"]
    if not config_dir:
        return ""
    base = os.path.basename(os.path.abspath(config_dir).rstrip("/"))
    if os.path.dirname(os.path.abspath(config_dir).rstrip("/")) == ACCOUNTS_DIR:
        if any(a.name == base for a in accts):
            return base
    blob = keychain.read_credentials(config_dir, max_age=keychain.RECENT)
    if not blob:
        return ""
    fp = fingerprint(blob)
    for a in accts:
        if fp and fingerprint(keychain.read_credentials(a.slot, max_age=keychain.RECENT)) == fp:
            return a.name
    email = identity(config_dir, blob)[0].get("email") or recorded_email(config_dir)
    return next((a.name for a in accts if (a.email or "").lower() == email.lower()), email)


def owners_now(dirs: Iterable[str], known: dict[str, str], prints: dict[str, str | None],
               accts: Iterable[Account], fresh: bool = False
               ) -> tuple[dict[str, str], dict[str, str | None]]:
    """Name the account behind each running session's config dir, cheaply.

    Fingerprints reveal a changed credential without looking up its identity
    on every poll. Account dirs already name their owner, so skip their reads.
    A rule change written by another process forces a fresh pass because that
    process may have switched a session while its old copy is still memoized.
    """
    accts = [a for a in accts if a.provider == "claude"]
    names = {a.name for a in accts}
    owners: dict[str, str] = {}
    current: dict[str, str | None] = {}
    stale = set()
    for path in dirs:
        if is_account_dir(path):
            name = os.path.basename(os.path.abspath(path).rstrip("/"))
            owners[path] = name if name in names else ""
            current[path] = None
            continue
        current[path] = fingerprint(keychain.read_credentials(
            path, max_age=0 if fresh else keychain.RECENT))
        # A remembered owner that is not a loaded account is no answer. The
        # first poll can run before the first refresh has loaded any
        # accounts, and naming a dir then yields its email; trusting that
        # afterwards kept the menu bar reading "?" for good.
        if (path not in known or known[path] not in names
                or current[path] != prints.get(path)):
            stale.add(path)
        else:
            owners[path] = known[path]
    if stale:
        owners.update(dirs_to_accounts(stale, accts))
    return owners, current


def dirs_to_accounts(dirs: Iterable[str], accts: Iterable[Account]) -> dict[str, str]:
    """Name the account behind each config dir, reading each credential once.

    Doing this a directory at a time re-read every account's credential to
    compare against, and each read is a `security` call: the cost was the
    number of directories times the number of accounts. Here every credential
    is read once and matched by fingerprint.
    """
    accts = [a for a in accts if a.provider == "claude"]
    by_fp: dict[str, str] = {}
    for a in accts:
        fp = fingerprint(keychain.read_credentials(a.slot, max_age=keychain.RECENT))
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
        blob = keychain.read_credentials(d, max_age=keychain.RECENT)
        if not blob:
            out[d] = ""
            continue
        name = by_fp.get(fingerprint(blob))
        if not name:
            email = (identity(d, blob)[0].get("email") or recorded_email(d) or "").lower()
            name = next((a.name for a in accts if (a.email or "").lower() == email), email)
        out[d] = name
    return out


ALL_SCOPES = ("default", "profile", "project", "session")


def rules_using(account: str, r: profiles.Rules | None = None,
                scopes: Iterable[str] = ALL_SCOPES) -> list[str]:
    """Every rule pointing at an account, described for a human.

    `scopes` narrows the answer. The menu bar draws profiles in a section of
    their own, so its account rows ask for the rest rather than say the same
    thing twice. A flat listing with no such section asks for all of it.
    """
    r = r or rules()
    want = set(scopes)
    out = []
    if "default" in want and r.default_account == account:
        out.append("default")
    if "profile" in want:
        out += [f"profile {p.name}" for p in r.profiles if p.account == account]
    if "project" in want:
        out += [f"project {os.path.basename(k.rstrip('/'))}"
                for k, v in r.projects.items() if v == account]
    if "session" in want:
        n = sum(1 for v in r.sessions.values() if v == account)
        if n:
            # "session" on its own means a Claude Code session that is running.
            # These are rules pinning one, which is a different thing and has
            # to read as a different thing next to "Running now".
            out.append(f"{n} session rule{'s' if n != 1 else ''}")
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


def _retire_pins_under(r: profiles.Rules, scope: str, key: str,
                       live: Iterable[sessions.Session]) -> list[str]:
    """Specificity alone is the wrong order when the broader rule is newer
    and is the one the user just chose on purpose. Retire older pins under it.
    """
    gone = []
    for sess in live:
        if sess.term_id not in r.sessions:
            continue
        paths = (project_root(sess.cwd), os.path.abspath(sess.cwd))
        projects = [r.project_rule_for(path) for path in paths]
        if scope == "project":
            covered = key in projects
        elif scope in ("profile", "default"):
            if any(project is not None for project in projects):
                continue
            profs = [r.profile_for(path) for path in paths]
            if scope == "profile":
                covered = any(prof is not None and prof.name == key for prof in profs)
            else:
                covered = all(prof is None for prof in profs)
        else:
            continue
        if covered:
            r.sessions.pop(sess.term_id)
            gone.append(sess.term_id)
    return gone


def assign(scope: str, key: str, account: str, cwd: str = "",
           live: Iterable[sessions.Session] | None = None,
           applied_out: dict | None = None) -> tuple[bool, str]:
    """Point one scope at an account. The scope decides how far it reaches.

    Broader choices retire older live session pins so the saved rules and
    running sessions both follow the account the user just chose.
    Project settings follow the project so a move does not re-ask for trust.
    """
    try:
        if account in codex_account_names():
            return False, (f"{account} is a Codex account. "
                           "Routing Codex accounts is not supported yet")
        try:
            account = resolve_account(account)
        except UnknownAccount as original:
            # Routing still belongs to Claude, so its nickname match wins.
            try:
                provider, name = resolve_any(account)
            except UnknownAccount:
                raise original from None
            if provider == "codex":
                return False, (f"{name} is a Codex account. "
                               "Routing Codex accounts is not supported yet")
            raise original
    except UnknownAccount as e:
        return False, str(e)
    r = rules()
    moved: list[str] = []
    if scope == "session":
        if not key:
            return False, "this session has no terminal id, so it cannot be pinned"
        # Where this session's project state lives now, which is its own pin
        # if it has one. Reading it without the terminal id moved the state
        # out of whichever dir the project rule named instead.
        before = account_dir(r.account_for(project_root(cwd or HOME), key)[0] or account)
        r.set_session(key, account)
        # A pin outlives the tab only until its dir is gone, so a tab pinned
        # before Claude Code ever ran in it needs the dir now, or the next
        # poll reads the missing dir as a dead terminal and drops the pin.
        _seed_dir(session_dir(key))
        moved = [project_root(cwd)] if cwd else []
        where = "this session"
    elif scope == "project":
        root = project_root(key or cwd)
        before = account_dir(r.account_for(root)[0] or account)
        r.set_project(root, account)
        key = profiles.tilde(root)
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
    where = f"{where} now uses {account}"
    if scope != "session":
        live = list(live if live is not None else sessions.live(credential_dirs()))
        retired = _retire_pins_under(r, scope, key, live)
        if retired:
            n = len(retired)
            where += f", releasing {n} pinned session{'s' if n != 1 else ''}"
    save_rules(r)
    for root in moved:
        carry_project_state(root, before, account_dir(account))
    return True, landed(where, live, applied_out)


def landed(where: str, live: Iterable[sessions.Session] | None = None,
           applied_out: dict | None = None) -> str:
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


def apply_now(live: Iterable[sessions.Session] | None = None
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
    r = rules()
    credentials: dict[str, tuple[dict | None, str]] = {}
    for sess in (live if live is not None else sessions.live(credential_dirs())):
        if not sess.term_id:
            continue
        path = session_dir(sess.term_id)
        if os.path.abspath(path) != os.path.abspath(sess.env_config_dir):
            continue                 # it is not reading this dir
        account, _ = resolve(sess.cwd, sess.term_id, r)
        if not account:
            continue
        if account not in credentials:
            home = account_dir(account)
            credentials[account] = live_blob(home), _cached_email(home)
        want, email = credentials[account]
        if want and hand_out(path, want, email=email, account=account):
            moved.append(sess.label)
            applied[path] = account
    return moved, applied


def clear(scope: str, key: str, cwd: str = "",
          live: Iterable[sessions.Session] | None = None,
          applied_out: dict | None = None) -> tuple[bool, str]:
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
                live: Iterable[sessions.Session] | None = None,
                applied_out: dict | None = None) -> tuple[bool, str]:
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
                   live: Iterable[sessions.Session] | None = None,
                   applied_out: dict | None = None) -> tuple[bool, str]:
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
                     live: Iterable[sessions.Session] | None = None,
                     applied_out: dict | None = None) -> tuple[bool, str]:
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
                        live: Iterable[sessions.Session] | None = None,
                        applied_out: dict | None = None) -> tuple[bool, str]:
    r = rules()
    if not r.profile(name):
        return False, f"no profile named {name}"
    r.remove_repo(name, project_root(path))
    save_rules(r)
    return True, landed(f"{os.path.basename(project_root(path))} left “{name}”",
                        live, applied_out)


# --------------------------------------------------------------------------- session pins
