import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest

from claude_code_manager import oauth


@pytest.mark.parametrize(("pasted", "expected"), [
    ("code", ("code", "")), (" code \n", ("code", "")),
    (" code # state ", ("code", "state")), ("code#state#tail", ("code", "state#tail")),
    ("", ("", "")), ("#state", ("", "state")),
])
def test_split_code(pasted, expected):
    assert oauth.split_code(pasted) == expected


@pytest.mark.parametrize("redirect", [oauth.CALLBACK_URL, "http://localhost:12345/callback"])
@pytest.mark.parametrize("hint", ["", "a@b.c"])
def test_attempt_url(redirect, hint):
    attempt = oauth.Attempt("verifier", "state", "work", redirect, hint)
    url = urlsplit(attempt.url)
    assert f"{url.scheme}://{url.netloc}{url.path}" == oauth.AUTHORIZE_URL
    challenge = base64.urlsafe_b64encode(hashlib.sha256(b"verifier").digest())
    expected = {
        "client_id": [oauth.CLIENT_ID], "response_type": ["code"],
        "redirect_uri": [redirect], "scope": [" ".join(oauth.SCOPES)],
        "code_challenge": [challenge.decode().rstrip("=")],
        "code_challenge_method": ["S256"], "state": ["state"],
    }
    if redirect == oauth.CALLBACK_URL:
        expected["code"] = ["true"]
    if hint:
        expected["login_hint"] = [hint]
    assert parse_qs(url.query) == expected


@pytest.mark.parametrize(("redirect", "pasted"), [
    ("http://localhost:12345/callback", "code"),
    ("http://localhost:12345/callback", "code#wrong"),
    (oauth.CALLBACK_URL, "code#wrong"),
])
def test_finish_refuses_invalid_state(redirect, pasted):
    def unexpected(*args, **kwargs):
        pytest.fail("An invalid callback must not reach token or profile requests")
    attempt = oauth.Attempt("verifier", "state", "work", redirect)
    blob, message = oauth.finish(attempt, pasted, unexpected, unexpected)
    assert blob is None
    assert "different sign-in" in message


@pytest.mark.parametrize(("tier", "plan", "subscription"), [
    ("default_claude_max_5x", "Max 5x", "max"), ("default_claude_pro", "Pro", "pro"),
])
def test_finish_builds_verified_blob(monkeypatch, tier, plan, subscription):
    monkeypatch.setattr(oauth.time, "time", lambda: 2000000000)
    attempt = oauth.Attempt("verifier", "state", "work", "http://localhost:12345/callback")
    calls = []

    def post(body):
        calls.append(body)
        return {
            "access_token": "access", "refresh_token": "refresh", "expires_in": 3600,
            "refresh_expires_in": 86400, "scope": "user:profile user:inference",
        }

    def profile_result(access):
        assert access == "access"
        return {"email": "a@b.c", "tier": tier, "plan": plan}, None

    blob, email = oauth.finish(attempt, "code#state", post, profile_result)
    assert calls == [{
        "grant_type": "authorization_code", "code": "code", "redirect_uri": attempt.redirect_uri,
        "client_id": oauth.CLIENT_ID, "code_verifier": "verifier", "state": "state",
    }]
    assert email == "a@b.c"
    assert blob == {
        "accessToken": "access", "refreshToken": "refresh", "expiresAt": 2000003600000,
        "refreshTokenExpiresAt": 2000086400000,
        "scopes": ["user:inference", "user:profile"],
        "subscriptionType": subscription, "rateLimitTier": tier,
    }
