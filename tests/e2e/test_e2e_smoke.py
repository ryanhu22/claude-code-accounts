"""The harness, proven on whole user flows through the real `ccm` command."""
import json
import os
import re
import socket
from urllib.parse import parse_qs, urlsplit

import pytest

from e2e.harness import fetch

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    return ANSI.sub("", text)


def test_login_through_the_browser_then_list(sandbox, fake_server, run_ccm):
    fake_server.add_claude("work@example.com", tier="default_claude_max_20x")
    r = sandbox.sign_in("work")
    assert r.returncode == 0, r.stderr
    assert "“work” is signed in as work@example.com" in r.stdout
    # The page ccm opened was the fake sign-in page, carrying PKCE and the
    # loopback redirect, and the code went back through that redirect.
    (url,) = sandbox.opened_urls()
    assert url.startswith(fake_server.url + "/cai/oauth/authorize?")
    query = parse_qs(urlsplit(url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"][0].startswith("http://localhost:")
    (auth,) = fake_server.authorizations
    assert auth["approved"] and auth["email"] == "work@example.com"
    (grant,) = fake_server.calls("/v1/oauth/token")
    assert grant.json["grant_type"] == "authorization_code"
    # The credential came from the token response, and the login's identity
    # was written where Claude Code keeps its own.
    blob = sandbox.blob(sandbox.slot("work"))
    assert blob["accessToken"] in fake_server._access
    assert blob["rateLimitTier"] == "default_claude_max_20x"
    assert blob["subscriptionType"] == "max"
    with open(os.path.join(sandbox.slot("work"), ".claude.json")) as f:
        assert json.load(f)["oauthAccount"]["emailAddress"] == "work@example.com"

    r = run_ccm("list")
    assert r.returncode == 0, r.stderr
    out = plain(r.stdout)
    assert "work  work@example.com · Max 20x" in out
    assert "5h" in out and "7d" in out and "fable 7d" in out
    assert "58.0%" in out and "71.0%" in out and "34.0%" in out
    usage = fake_server.calls("/api/oauth/usage")
    assert usage and usage[-1].query == {"cedar_ember": "1", "skip_spend": "1"}


def test_paste_flow_uses_the_hosted_code(sandbox, fake_server):
    fake_server.add_claude("paste@example.com")
    r = sandbox.sign_in("pasted", paste=True)
    assert r.returncode == 0, r.stderr
    assert "signed in as paste@example.com" in r.stdout
    assert sandbox.blob(sandbox.slot("pasted"))


def test_seeded_account_is_the_fast_path(sandbox, fake_server, run_ccm):
    sandbox.seed_claude("a", "a@example.com", tier="default_claude_pro")
    r = run_ccm("list")
    assert r.returncode == 0, r.stderr
    assert "a  a@example.com · Pro" in plain(r.stdout)
    assert not fake_server.calls("/v1/oauth/token")


def test_a_rate_limit_is_reported_not_treated_as_a_dead_login(sandbox, fake_server, run_ccm):
    sandbox.seed_claude("busy", "busy@example.com")
    fake_server.script("/api/oauth/usage", 429, {"error": "rate_limited"},
                       headers={"Retry-After": "300"}, times=2)
    out = plain(run_ccm("list").stdout)
    assert "busy@example.com" in out and "rate limited, retrying in 5m" in out
    # With nothing cached the row asks again, and a second 429 does not push
    # the deadline later than the first one set.
    assert "rate limited, retrying in 5m" in plain(run_ccm("list").stdout)
    assert len(fake_server.calls("/api/oauth/usage")) == 2
    # The limit clears: the same login, never declared expired, reads fine.
    out = plain(run_ccm("list").stdout)
    assert "58.0%" in out and "expired" not in out


def test_poke_starts_the_windows_that_have_no_clock(sandbox, fake_server, run_ccm):
    acct = fake_server.add_claude("idle@example.com")
    for lim in acct.limits:
        lim["percent"], lim["resets_at"] = 0, None
    sandbox.seed_claude("idle", "idle@example.com")
    assert "idle" in plain(run_ccm("list").stdout)
    r = run_ccm("poke", "idle")
    assert r.returncode == 0, r.stderr
    assert "1 window group(s) started" in r.stdout
    # The scoped Fable window needs its own model, and that request starts
    # the general windows too.
    assert [p["model"] for p in fake_server.pokes] == ["claude-fable-5-1"]
    assert all(lim["resets_at"] for lim in acct.limits)


def test_expiring_token_is_refreshed_once_and_the_old_grant_is_spent(sandbox, fake_server,
                                                                     run_ccm):
    fake_server.add_claude("r@example.com")
    before = sandbox.seed_claude("r", "r@example.com")
    # Push the stored expiry inside the refresh margin, as a stale token would be.
    from claude_code_accounts import keychain
    blob = {**before, "expiresAt": 1000}
    sandbox._put_item(keychain.service_for(sandbox.slot("r")),
                      json.dumps({"claudeAiOauth": blob}))
    assert run_ccm("list").returncode == 0
    grants = fake_server.calls("/v1/oauth/token")
    assert [g.json["grant_type"] for g in grants] == ["refresh_token"]
    after = sandbox.blob(sandbox.slot("r"))
    assert after["accessToken"] != before["accessToken"]
    assert after["refreshToken"] != before["refreshToken"]
    # The spent refresh token is single use on the fake, as on the real server.
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        fake_server.url + "/v1/oauth/token",
        data=json.dumps({"grant_type": "refresh_token",
                         "refresh_token": before["refreshToken"]}).encode(),
        headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req)
    assert err.value.code == 400 and json.load(err.value)["error"] == "invalid_grant"


def test_codex_account_lists_plan_usage_and_credits(sandbox, fake_server, run_ccm):
    acct = fake_server.add_codex("c@example.com", plan="plus", credits=[
        {"id": "credit-1", "status": "available", "is_supported_by_plan": True,
         "expires_at": "2100-01-01T00:00:00Z", "reset_type": "codex_rate_limits"}])
    sandbox.seed_codex("gpt", "c@example.com")
    out = plain(run_ccm("list").stdout)
    assert "gpt codex  c@example.com · Plus" in out
    assert "62.0%" in out and "reset credits: 1" in out
    r = run_ccm("reset", "gpt", "-y")
    assert r.returncode == 0, r.stderr
    assert "windows reset, that was the last reset credit" in r.stdout
    (consumed,) = fake_server.calls("/backend-api/wham/rate-limit-reset-credits/consume")
    assert consumed.json["credit_id"] == "credit-1" and consumed.json["redeem_request_id"]
    # A window with no clock is started through the Codex CLI's own request.
    acct.usage["rate_limit"]["primary_window"] = {
        "used_percent": 0, "limit_window_seconds": 604800,
        "reset_after_seconds": 604800, "reset_at": 4102444800}
    r = run_ccm("poke", "gpt")
    assert r.returncode == 0, r.stderr
    (ran,) = sandbox.codex_execs()
    assert ran["CODEX_HOME"] == sandbox.codex_slot("gpt") and ran["argv"][0] == "exec"


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        # As ccm's callback server binds: a port in TIME_WAIT is free to it.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


@pytest.mark.skipif(not _port_free(1455), reason="port 1455 is in use (a Codex sign-in?)")
def test_codex_login_through_the_browser(sandbox, fake_server):
    fake_server.add_codex("new@example.com", plan="prolite")
    r = sandbox.sign_in_codex("gpt")
    assert r.returncode == 0, r.stderr
    assert "“gpt” is signed in as new@example.com" in r.stdout
    with open(os.path.join(sandbox.codex_slot("gpt"), "auth.json")) as f:
        auth = json.load(f)
    assert auth["tokens"]["access_token"] in fake_server._codex_access
    (grant,) = fake_server.calls("/oauth/token")
    assert grant.form["grant_type"] == "authorization_code"


def test_a_sign_in_on_the_wrong_account_is_said(sandbox, fake_server):
    sandbox.seed_claude("work", "work@example.com")
    fake_server.add_claude("other@example.com")
    fake_server.browser = "other@example.com"
    r = sandbox.sign_in("work")
    assert r.returncode == 0, r.stderr
    assert "is now signed in as other@example.com, but it used to be work@example.com" in r.stdout


def test_the_fake_sign_in_page_is_a_page(fake_server):
    """What a browser test sees: a page with one Authorize button."""
    status, page = fetch(fake_server.url + "/cai/oauth/authorize?client_id=x&redirect_uri="
                         "http%3A%2F%2Flocalhost%3A1%2Fcallback&state=s&code_challenge=c"
                         "&code_challenge_method=S256&login_hint=nobody%40example.com")
    assert status == 200
    assert 'id="authorize"' in page and "<title>Claude sign in (fake)</title>" in page
