"""The pages the browser lands on after `ccm login`, through the real command.

The browser suite under e2e/ renders these in Chromium; this is the same
ground, without a browser, so a regression shows up in the ordinary test run.
"""
import re
import socket
import urllib.error

import pytest

from e2e.harness import fetch
from e2e.serve_signin import SignIn

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


needs_1455 = pytest.mark.skipif(not _port_free(1455), reason="port 1455 is in use")


@pytest.fixture
def signin(fake_server, sandbox):
    made = []

    def make(**kwargs):
        s = SignIn(server=fake_server, sandbox=sandbox, **kwargs)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def get(url: str) -> tuple[int, str]:
    try:
        return fetch(url)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def land(signin: SignIn, first: dict) -> str:
    """Press Authorize in the browser's place; the body is ccm's page."""
    _status, body = signin.sandbox.approve(first["authorize_url"])
    return body


def test_denied_sign_in_page_says_so_and_ccm_asks_for_no_code(signin):
    s = signin(account="work", deny=True)
    body = land(s, s.start())
    assert "<h2>The sign-in did not finish.</h2>" in body
    assert "The sign-in page reported that the request was not allowed." in body
    assert "Signed in" not in body
    outcome = s.outcome()
    assert outcome["returncode"] == 1 and not outcome["signed_in"]
    assert "was not allowed" in outcome["stderr"]
    assert "Paste the code" not in outcome["stdout"]


def test_refused_code_is_on_the_page_not_a_signed_in_claim(signin):
    s = signin(account="work", expired_code=True)
    body = land(s, s.start())
    assert "<h2>The sign-in did not finish.</h2>" in body
    assert "Authorization code expired" in body
    outcome = s.outcome()
    assert outcome["returncode"] == 1 and not outcome["signed_in"]


def test_success_page_names_the_account(signin):
    s = signin(account="work", email="work@example.com")
    body = land(s, s.start())
    assert "<h2>Signed in.</h2>" in body
    assert "“work” is signed in as work@example.com" in body
    assert "<script" not in body and "http" not in body and "\u2014" not in body
    assert '<meta name="color-scheme" content="light dark">' in body
    assert s.outcome()["signed_in"]
    assert "work  work@example.com" in ANSI.sub("", s.ccm("list")["stdout"])


def test_wrong_account_warning_reaches_the_page(signin):
    s = signin(account="work", email="work@example.com", seeded="work@example.com",
               browser="other@example.com")
    body = land(s, s.start())
    assert ("“work” is now signed in as other@example.com, but it used to be "
            "work@example.com." in body)
    assert "used to be work@example.com" in s.outcome()["stdout"]


def test_forged_state_is_refused_without_spending_the_attempt(signin, fake_server):
    s = signin(account="work", email="work@example.com")
    first = s.start()
    for query in ("?code=forged&state=other", "?code=forged", ""):
        status, body = get(first["callback_url"] + query)
        assert status == 400 and "does not belong to the sign-in in progress" in body
    assert not fake_server.calls("/v1/oauth/token")
    assert "<h2>Signed in.</h2>" in land(s, first)
    assert s.outcome()["signed_in"]


@needs_1455
def test_codex_wrong_account_warning(signin):
    s = signin(account="gpt", email="gpt@example.com", codex=True, seeded="gpt@example.com",
               browser="other@example.com")
    body = land(s, s.start())
    assert "<h2>Signed in.</h2>" in body
    assert ("“gpt” is now signed in as other@example.com, but it used to be "
            "gpt@example.com." in body)
    outcome = s.outcome()
    assert outcome["returncode"] == 0 and "used to be gpt@example.com" in outcome["stdout"]


@needs_1455
def test_codex_denied_sign_in_page(signin):
    s = signin(account="gpt", codex=True, deny=True)
    body = land(s, s.start())
    assert "<h2>The sign-in did not finish.</h2>" in body
    outcome = s.outcome()
    assert outcome["returncode"] == 1 and "was not allowed" in outcome["stderr"]


@needs_1455
def test_codex_busy_port_exits_before_a_browser(signin):
    s = signin(account="gpt", codex=True, busy_port=True)
    first = s.start()
    assert first["exited"] and first["returncode"] == 1
    assert "port 1455 is in use" in first["stderr"]


@needs_1455
def test_a_sign_in_that_cannot_start_leaves_no_sandbox():
    import glob
    import os
    import tempfile

    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "ccm-e2e-*")))
    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 1455))
    try:
        with pytest.raises(RuntimeError, match="1455 is already in use"):
            SignIn(account="gpt", codex=True, busy_port=True)
    finally:
        holder.close()
    assert set(glob.glob(os.path.join(tempfile.gettempdir(), "ccm-e2e-*"))) == before
