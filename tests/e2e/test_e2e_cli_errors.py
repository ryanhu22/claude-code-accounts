"""Error paths: signed-out slots, a revoked login, server faults, no network,
and sign-ins that go wrong."""
import json
import os
import re
import socket
from urllib.error import HTTPError

import pytest

from e2e.harness import fetch, plain

DEAD = "http://127.0.0.1:9"
NO_NETWORK = {"CCM_API_BASE": DEAD, "CCM_TOKEN_URL": DEAD + "/v1/oauth/token",
              "CCM_CODEX_API_BASE": DEAD, "CCM_CODEX_AUTH_BASE": DEAD}


def test_slots_with_no_login_say_how_to_sign_in(sandbox, fake_server, run_ccm):
    os.makedirs(sandbox.slot("ghost"))
    os.makedirs(sandbox.codex_slot("cghost"))
    sandbox.seed_claude("work", "work@example.com")
    out = plain(run_ccm("list").stdout)
    assert "ghost  not signed in\n  (ccm add ghost)\n" in out
    assert "cghost codex  not signed in\n  (ccm login cghost --codex)\n" in out
    assert "work  work@example.com · Max 5x\n" in out


def test_a_login_the_server_revoked_reads_as_expired(sandbox, fake_server, run_ccm):
    sandbox.seed_claude("old", "old@example.com")
    fake_server.revoke("old@example.com")
    out = plain(run_ccm("list").stdout)
    assert "old  login expired\n  (ccm add old)\n" in out
    assert fake_server.calls("/api/oauth/profile")[-1].path == "/api/oauth/profile"
    r = run_ccm("poke", "old")
    assert r.returncode == 1
    assert r.stdout.strip() == "old: claude-haiku-4-5-20251001: Invalid authentication"
    r = run_ccm("reset", "old", "-y")
    assert r.returncode == 1
    assert r.stdout.strip().startswith("old: could not read the resets (HTTP 401")


def test_a_server_fault_is_reported_once_and_served_from_cache_after(sandbox, fake_server,
                                                                     run_ccm):
    sandbox.seed_claude("work", "work@example.com")
    fake_server.script("/api/oauth/usage", 500, {"error": "boom"})
    out = plain(run_ccm("list").stdout)
    assert "work  work@example.com · Max 5x  <- default  usage HTTP 500\n" in out
    assert "58.0%" not in out
    out = plain(run_ccm("list").stdout)
    assert "58.0%" in out and "HTTP 500" not in out
    # A later fault keeps the numbers on screen rather than blanking the row.
    # The payload is aged past the point where ccm asks again.
    cache = os.path.join(sandbox.home, ".claude-accts", ".usage-cache.json")
    with open(cache) as f:
        store = json.load(f)
    store["work"]["at"] -= 3600
    with open(cache, "w") as f:
        json.dump(store, f)
    fake_server.script("/api/oauth/usage", 500, {"error": "boom"})
    out = plain(run_ccm("list").stdout)
    assert "58.0%" in out and "HTTP 500" not in out
    assert "(usage 60m old)" in out
    assert len(fake_server.calls("/api/oauth/usage")) == 3


def test_a_codex_401_reads_as_expired(sandbox, fake_server, run_ccm):
    sandbox.seed_codex("gpt", "gpt@example.com")
    fake_server.script("/backend-api/wham/usage", 401, {"detail": "Unauthorized"})
    out = plain(run_ccm("list").stdout)
    assert "gpt codex  gpt@example.com · Pro Lite  <- default  usage HTTP 401\n" in out


def test_no_network(sandbox, fake_server, run_ccm):
    sandbox.seed_claude("work", "work@example.com")
    sandbox.seed_claude("fresh", "fresh@example.com")
    sandbox.seed_codex("gpt", "gpt@example.com")
    # work and gpt have been read once; fresh never.
    run_ccm("list")
    os.remove(os.path.join(sandbox.home, ".claude-accts", ".usage-cache.json"))
    assert "fresh@example.com" in plain(run_ccm("list").stdout)
    sandbox.seed_claude("never", "never@example.com")
    r = run_ccm("list", env=NO_NETWORK)
    assert r.returncode == 0, r.stderr
    out = plain(r.stdout)
    assert "never  can't reach Anthropic\n  (ccm add never)\n" in out
    assert "work  work@example.com · Max 5x\n" in out and "58.0%" in out
    # Known accounts with no usage on hand say the same thing, in the same words.
    os.remove(os.path.join(sandbox.home, ".claude-accts", ".usage-cache.json"))
    out = plain(run_ccm("list", env=NO_NETWORK).stdout)
    assert "work  work@example.com · Max 5x  can't reach Anthropic\n" in out
    assert "gpt codex  gpt@example.com · Pro Lite  <- default  can't reach OpenAI\n" in out
    assert "urlopen" not in out
    r = run_ccm("reset", "work", "-y", env=NO_NETWORK)
    assert r.returncode == 1
    assert r.stdout.strip() == "work: could not read the resets ([Errno 61] Connection refused)"
    r = run_ccm("reset", "gpt", "-y", env=NO_NETWORK)
    assert r.returncode == 1
    assert r.stdout.strip() == \
        "gpt: could not read the reset credits ([Errno 61] Connection refused)"
    assert fake_server.resets == []


def test_log_records_a_refresh_with_fingerprints_and_no_tokens(sandbox, fake_server, run_ccm):
    from claude_code_accounts import keychain

    fake_server.add_claude("r@example.com")
    before = sandbox.seed_claude("r", "r@example.com")
    sandbox._put_item(keychain.service_for(sandbox.slot("r")),
                      json.dumps({"claudeAiOauth": {**before, "expiresAt": 1000}}))
    assert run_ccm("list").returncode == 0
    after = sandbox.blob(sandbox.slot("r"))
    r = run_ccm("log")
    assert r.returncode == 0
    (line,) = r.stdout.splitlines()
    assert re.search(r" refresh r [0-9a-f]{8}->[0-9a-f]{8} refresh-token-life 30\.0d$", line), line
    for secret in (before["accessToken"], before["refreshToken"], after["accessToken"],
                   after["refreshToken"]):
        assert secret not in line
    assert run_ccm("log", "-n", "1").stdout == r.stdout


def test_a_denied_sign_in_stores_nothing(sandbox, fake_server):
    fake_server.add_claude("work@example.com")
    fake_server.deny_next_authorize = True
    r = sandbox.sign_in("work")
    assert r.returncode == 1
    assert "The user denied the request" in plain(r.stderr)
    assert "no code given; nothing changed" in r.stderr
    assert sandbox.blob(sandbox.slot("work")) is None
    assert not fake_server.calls("/v1/oauth/token")


def test_pasting_a_bad_or_empty_code(sandbox, fake_server, run_ccm):
    fake_server.add_claude("work@example.com")
    r = run_ccm("login", "work", "--paste", input="garbage\n")
    assert r.returncode == 1
    assert "Paste the code the page shows." in plain(r.stdout)
    assert plain(r.stderr).strip() == ("the code was refused (Invalid authorization code). "
                                       "Codes expire quickly; try again")
    r = run_ccm("login", "work", "--paste", input="")
    assert r.returncode == 1 and r.stderr.strip() == "no code given; nothing changed"
    assert sandbox.blob(sandbox.slot("work")) is None


def test_a_browser_that_is_not_installed_leaves_the_url_to_open(sandbox, fake_server):
    fake_server.add_claude("work@example.com")
    proc = sandbox.popen("login", "work", "--browser", "Firefox")
    lines = [proc.stderr.readline() for _ in range(3)]
    assert lines[0].startswith("could not open a browser: Unable to find application")
    assert lines[1] == "\n" and lines[2] == "Open this yourself:\n"
    url = proc.stderr.readline().strip()
    assert url.startswith(fake_server.url + "/cai/oauth/authorize?")
    assert sandbox.opened_urls() == []
    sandbox.approve(url)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert "“work” is signed in as work@example.com" in out
    assert sandbox.blob(sandbox.slot("work"))


def test_the_callback_takes_only_its_own_sign_in(sandbox, fake_server):
    fake_server.add_claude("work@example.com")
    proc = sandbox.popen("login", "work")
    url = sandbox.wait_for_url(proc, 0, 30)
    from urllib.parse import parse_qs, urlsplit
    callback = parse_qs(urlsplit(url).query)["redirect_uri"][0]
    with pytest.raises(HTTPError) as err:
        fetch(callback + "?code=stray&state=someone-else")
    assert err.value.code == 400 and b"does not belong to the sign-in" in err.value.read()
    with pytest.raises(HTTPError) as err:
        fetch(callback + "?code=stray")
    assert err.value.code == 400
    with pytest.raises(HTTPError) as err:
        fetch(callback.replace("/callback", "/favicon.ico"))
    assert err.value.code == 404
    status, page = sandbox.approve(url)
    assert status == 200 and "Signed in." in page
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert "“work” is signed in as work@example.com" in out


def test_codex_sign_in_refuses_a_busy_port(sandbox, fake_server, run_ccm):
    fake_server.add_codex("c@example.com")
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", 1455))
        except OSError:
            pass                      # busy already, which is the case under test
        s.listen(1)
        r = run_ccm("login", "gpt", "--codex")
    assert r.returncode == 1
    assert r.stderr.strip() == ("port 1455 is in use (is another sign-in or `codex login` "
                                "running?)")
    assert sandbox.opened_urls() == []
