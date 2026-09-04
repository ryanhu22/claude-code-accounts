"""Signing an account in, the way Claude Code does.

An OAuth PKCE flow against platform.claude.com. The user's browser does the
signing in; we exchange the resulting code for a token pair and write it where
that account lives.

The one rule here is that a credential is never invented. Every field of the
stored blob either comes from the token response or from `/api/oauth/profile`,
and the result is checked against the live API before it is written. A blob
missing `subscriptionType` or `rateLimitTier` still authenticates, but Claude
Code then opens the session as "API Usage Billing" instead of the user's Max
plan, which is a confusing way to fail and worth refusing outright.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

AUTHORIZE_URL = "https://platform.claude.com/oauth/authorize"
CALLBACK_URL = "https://platform.claude.com/oauth/code/callback"

# The scope set a real Claude Code login carries. Ask for exactly these: a
# narrower set produces a credential that runs sessions but cannot read the
# profile, which is how a login ends up unable to report its own plan.
SCOPES = ("user:profile", "user:inference", "user:sessions:claude_code",
          "user:mcp_servers", "user:file_upload")


@dataclass
class Attempt:
    """One sign-in in progress: what we sent, so the exchange can prove it."""
    verifier: str
    state: str
    account: str
    started_at: float = field(default_factory=time.time)

    @property
    def url(self) -> str:
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip("=")
        query = urllib.parse.urlencode({
            "code": "true",
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": CALLBACK_URL,
            "scope": " ".join(SCOPES),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": self.state,
        })
        return f"{AUTHORIZE_URL}?{query}"


CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def begin(account: str) -> Attempt:
    """Start a sign-in. The verifier never leaves this process."""
    return Attempt(verifier=secrets.token_urlsafe(64), state=secrets.token_urlsafe(24),
                   account=account)


def split_code(pasted: str) -> tuple[str, str]:
    """The callback shows `code#state`; accept either that or a bare code."""
    pasted = pasted.strip()
    if "#" in pasted:
        code, _, state = pasted.partition("#")
        return code.strip(), state.strip()
    return pasted, ""


def finish(attempt: Attempt, pasted: str, post, profile_result) -> tuple[Optional[dict], str]:
    """Exchange a pasted code for a complete, verified credential.

    `post` and `profile_result` are passed in rather than imported so this
    module stays free of the http and keychain layers, and so a test can drive
    the whole flow without a network.

    Returns (blob, error). A blob comes back only when the API confirmed who it
    belongs to and what they are paying for.
    """
    code, state = split_code(pasted)
    if not code:
        return None, "no code was pasted"
    if state and state != attempt.state:
        return None, "that code belongs to a different sign-in; start again"
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CALLBACK_URL,
        "client_id": CLIENT_ID,
        "code_verifier": attempt.verifier,
        "state": attempt.state,
    }
    try:
        resp = post(body)
    except Exception as e:
        detail = getattr(e, "reason", None) or str(e)
        return None, f"the code was refused ({detail}). Codes expire quickly; try again"
    access = resp.get("access_token")
    refresh = resp.get("refresh_token")
    if not access or not refresh:
        return None, "the response carried no token pair"

    info, err = profile_result(access)
    email = info.get("email")
    if not email:
        return None, ("signed in, but the profile could not be read"
                      + (f" ({err})" if err else "")
                      + ". Not saving a login that cannot name itself.")
    tier = info.get("tier") or ""
    plan = info.get("plan") or ""
    if not tier:
        return None, (f"signed in as {email}, but the plan could not be read. "
                      "Saving that would open sessions as API billing, so it is refused.")

    now = time.time()
    blob = {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": int((now + float(resp.get("expires_in") or 3600)) * 1000),
        "scopes": sorted((resp.get("scope") or " ".join(SCOPES)).split()),
        "subscriptionType": "max" if "max" in tier else "pro" if "pro" in tier else plan.lower(),
        "rateLimitTier": tier,
    }
    if resp.get("refresh_expires_in"):
        blob["refreshTokenExpiresAt"] = int(
            (now + float(resp["refresh_expires_in"])) * 1000)
    return blob, email


# Browsers worth offering by name. The default is always first: signing in
# often needs an account the user is already logged into, and which browser
# holds that session is the whole reason this choice exists.
BROWSERS = (
    ("Default browser", ""),
    ("Google Chrome", "Google Chrome"),
    ("Safari", "Safari"),
    ("Arc", "Arc"),
    ("Firefox", "Firefox"),
    ("Microsoft Edge", "Microsoft Edge"),
    ("Brave", "Brave Browser"),
)


def installed_browsers() -> list[tuple[str, str]]:
    """The offerable browsers that are actually on this Mac."""
    import subprocess
    out = [BROWSERS[0]]
    for label, app in BROWSERS[1:]:
        try:
            r = subprocess.run(["open", "-Ra", app], capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            out.append((label, app))
    return out


def open_in(url: str, app: str = "") -> str:
    """Open a URL, in a named browser or the default one. "" on success."""
    import subprocess
    cmd = ["open", url] if not app else ["open", "-a", app, url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)[:120]
    return "" if r.returncode == 0 else (r.stderr.strip()[:160] or "could not open a browser")
