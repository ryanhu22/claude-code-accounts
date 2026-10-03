import base64
import hashlib
import socket
from urllib.parse import parse_qs, urlsplit

import pytest

from claude_code_accounts import oauth

# Captured before conftest's autouse fixture replaces it: the callback tests
# talk to a server of their own on loopback, and nothing else.
_CREATE_CONNECTION = socket.create_connection
_CONNECT = socket.socket.connect


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
    assert oauth.AUTHORIZE_URL.startswith("https://claude.com/")
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


# ----------------------------------------------------------------- the callback server

def _get(url: str) -> tuple[int, str]:
    import urllib.error
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


@pytest.fixture
def callback(monkeypatch):
    def loopback_only(sock, address):
        assert address[0] in ("127.0.0.1", "::1"), address
        return _CONNECT(sock, address)

    monkeypatch.setattr(socket, "create_connection", _CREATE_CONNECTION)
    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    cb = oauth.Callback()
    cb.expect("state")
    yield cb
    cb.close()


def test_callback_page_says_signed_in_only_once_the_exchange_succeeded(callback):
    exchanged = []

    def finish(code, state):
        exchanged.append((code, state))
        return True, "“work” is signed in as work@example.com"

    callback.finish = finish
    status, body = _get(f"{callback.redirect_uri}?code=abc&state=state")
    assert status == 200
    assert exchanged == [("abc", "state")]
    assert "<h2>Signed in.</h2>" in body
    assert "“work” is signed in as work@example.com" in body
    assert callback.wait(1) and callback.result == (True, "“work” is signed in as work@example.com")
    # A reload shows the same outcome and does not run the exchange again.
    assert _get(f"{callback.redirect_uri}?code=abc&state=state") == (200, body)
    assert len(exchanged) == 1


def test_callback_page_reports_a_refused_code(callback):
    callback.finish = lambda code, state: (False, "the code was refused (expired). Try again")
    status, body = _get(f"{callback.redirect_uri}?code=old&state=state")
    assert status == 200
    assert "<h2>The sign-in did not finish.</h2>" in body
    assert "the code was refused (expired). Try again" in body
    assert "Signed in" not in body
    assert callback.result == (False, "the code was refused (expired). Try again")


def test_callback_page_reports_a_denied_sign_in_without_exchanging(callback):
    def unexpected(code, state):
        pytest.fail("a denied sign-in has no code to exchange")

    callback.finish = unexpected
    status, body = _get(f"{callback.redirect_uri}?error=access_denied"
                        "&error_description=The+user+denied+the+request&state=state")
    assert status == 200
    assert "<h2>The sign-in did not finish.</h2>" in body
    assert "was not allowed" in body
    assert "Signed in" not in body
    ok, message = callback.result
    assert not ok and "not allowed" in message
    assert callback.error == "The user denied the request"


def test_callback_escapes_what_a_server_sent(callback):
    callback.finish = lambda code, state: (False, "<b>bold</b> & co")
    _status, body = _get(f"{callback.redirect_uri}?code=x&state=state")
    assert "&lt;b&gt;bold&lt;/b&gt; &amp; co" in body
    assert "<b>" not in body


def test_callback_refuses_another_sign_in_and_other_paths(callback):
    callback.finish = lambda code, state: pytest.fail("must not run")
    assert _get(f"{callback.redirect_uri}?code=x&state=other")[0] == 400
    assert _get(f"{callback.redirect_uri}?code=x")[0] == 400
    assert _get(f"http://127.0.0.1:{callback.port}/favicon.ico")[0] == 404
    assert not callback.wait(0.1) and callback.result is None


def test_callback_without_finish_only_records_the_redirect(callback):
    status, body = _get(f"{callback.redirect_uri}?code=abc&state=state")
    assert status == 200 and body == oauth.DONE_PAGE.decode()
    assert (callback.code, callback.state, callback.result) == ("abc", "state", None)


def test_callback_survives_an_exchange_that_raises(callback):
    def boom(code, state):
        raise RuntimeError("keychain is locked")

    callback.finish = boom
    status, body = _get(f"{callback.redirect_uri}?code=x&state=state")
    assert status == 200 and "keychain is locked" in body
    assert callback.result == (False, "The sign-in could not be finished: keychain is locked")


@pytest.mark.parametrize("page", [
    oauth.DONE_PAGE, oauth.WRONG_SIGN_IN_PAGE, oauth.NOT_FOUND_PAGE,
    oauth.done_page("“a” is signed in as a@example.com"), oauth.failed_page("refused"),
])
def test_pages_are_plain_self_contained_html(page):
    text = page.decode()
    assert text.startswith("<!doctype html><meta charset=utf-8>")
    assert '<meta name="color-scheme" content="light dark">' in text
    assert "<script" not in text and "http" not in text and "—" not in text


def test_callback_answers_a_second_visit_during_the_exchange_with_the_same_outcome(callback):
    import threading
    import time

    started, exchanged = threading.Event(), []

    def slow_finish(code, state):
        started.set()
        time.sleep(0.5)
        exchanged.append(code)
        return True, "“work” is signed in as work@example.com"

    callback.finish = slow_finish
    url = f"{callback.redirect_uri}?code=abc&state=state"
    pages: list[tuple[int, str]] = []
    first = threading.Thread(target=lambda: pages.append(_get(url)))
    first.start()
    assert started.wait(2)
    # A reload while the exchange runs: answered with the outcome, not reset,
    # and the code is exchanged once.
    second = _get(url)
    first.join(5)
    assert pages and pages[0] == second
    assert second[0] == 200 and "“work” is signed in as work@example.com" in second[1]
    assert exchanged == ["abc"]


def test_callback_reports_done_even_when_the_tab_went_away(callback):
    import time

    callback.finish = lambda code, state: (time.sleep(0.2), (True, "stored"))[1]
    # The browser drops the connection before the page is written (the user
    # closed or reloaded the tab mid-exchange). The caller still learns the
    # outcome instead of waiting out the five minutes.
    with socket.create_connection(("127.0.0.1", callback.port)) as s:
        s.sendall(b"GET /callback?code=abc&state=state HTTP/1.0\r\nHost: localhost\r\n\r\n")
    assert callback.wait(5)
    assert callback.result == (True, "stored")
