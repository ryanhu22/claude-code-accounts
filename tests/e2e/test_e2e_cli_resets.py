"""`ccm reset` (Claude limit resets and Codex reset credits) and `ccm poke`."""
import os

import pytest

from e2e.harness import plain

GRANT = {"id": "launch", "label": "Launch reset", "resets_total": 2, "resets_left": 2,
         "ends_at": "2100-02-01T00:00:00Z", "clears": ["five_hour", "seven_day"],
         "paused": False, "usable_now": True, "use_requires_limit": False}
PROMPT = "Use one reset on work? Its limits go back to 0%. [y/N]"


@pytest.fixture
def work(sandbox, fake_server):
    acct = fake_server.add_claude("work@example.com", grants=[dict(GRANT)])
    sandbox.seed_claude("work", "work@example.com")
    return acct


def test_claude_reset_asks_then_spends_the_grant_the_server_names(sandbox, fake_server, work,
                                                                  run_ccm):
    out = plain(run_ccm("list").stdout)
    assert "resets: 2 · expires 2100-02-01" in out
    r = run_ccm("reset", "work", input="n\n")
    assert r.returncode == 1 and PROMPT in r.stdout and r.stdout.rstrip().endswith("nothing spent")
    assert fake_server.resets == []
    r = run_ccm("reset", "work", input="y\n")
    assert r.returncode == 0, r.stderr
    assert r.stdout.rstrip().endswith("work: limits reset, 1 reset left")
    (sent,) = fake_server.resets
    assert sent["program"] == "cedar_ember" and sent["grant_id"] == "launch"
    assert sent["org"] == work.org_uuid and len(sent["request_id"]) == 32
    assert all(lim["percent"] == 0 for lim in work.limits)
    # The second one goes without a prompt and is the last.
    r = run_ccm("reset", "work", "-y")
    assert r.returncode == 0 and r.stdout.strip() == "work: limits reset, that was the last reset"
    assert len(fake_server.resets) == 2
    assert fake_server.resets[1]["request_id"] != sent["request_id"]
    r = run_ccm("reset", "work", "-y")
    assert r.returncode == 1 and r.stdout.strip() == "work: no reset to use"
    assert len(fake_server.resets) == 2, "nothing is sent when there is no grant"


def test_claude_reset_refusals_say_why(sandbox, fake_server, work, run_ccm):
    work.grants[0]["use_requires_limit"] = True
    r = run_ccm("reset", "work", "-y")
    assert r.returncode == 1 and r.stdout.strip() == "work: this reset can only be used at a limit"
    assert work.grants[0]["resets_left"] == 2
    work.grants[0]["use_requires_limit"] = False
    fake_server.script("/api/oauth/usage", 500, {"error": "boom"})
    r = run_ccm("reset", "work", "-y")
    assert r.returncode == 1
    assert r.stdout.strip() == "work: could not read the resets (HTTP 500: boom)"
    fake_server.script("/api/oauth/usage", 401, {"error": {"message": "revoked"}})
    r = run_ccm("reset", "work", "-y")
    assert r.returncode == 1
    assert r.stdout.strip() == "work: could not read the resets (HTTP 401: revoked)"
    fake_server.script("/api/organizations/", 403, {"error": "forbidden"}, method="POST")
    r = run_ccm("reset", "work", "-y")
    assert r.returncode == 1
    assert r.stdout.strip() == "work: the reset was refused (HTTP 403: forbidden)"
    assert work.grants[0]["resets_left"] == 2
    fake_server.script("/api/organizations/", 200, {"result": "cooldown"}, method="POST")
    r = run_ccm("reset", "work", "-y")
    assert r.returncode == 1
    assert r.stdout.strip() == "work: resets are cooling down, try again later"
    assert "reset work refused: cooldown" in run_ccm("log").stdout


def test_codex_reset_spends_the_credit_that_expires_first(sandbox, fake_server, run_ccm):
    acct = fake_server.add_codex("gpt@example.com", plan="plus", credits=[
        {"id": "c-late", "status": "available", "is_supported_by_plan": True,
         "expires_at": "2100-03-01T00:00:00Z"},
        {"id": "c-soon", "status": "available", "is_supported_by_plan": True,
         "expires_at": "2100-02-01T00:00:00Z"},
        {"id": "c-gone", "status": "redeemed", "is_supported_by_plan": True,
         "expires_at": "2100-01-01T00:00:00Z"}])
    sandbox.seed_codex("gpt", "gpt@example.com")
    out = plain(run_ccm("list").stdout)
    assert "gpt codex  gpt@example.com · Plus" in out
    assert "credits: none · reset credits: 2" in out
    r = run_ccm("reset", "gpt", input="\n")
    assert r.returncode == 1 and r.stdout.rstrip().endswith("nothing spent")
    r = run_ccm("reset", "gpt", input="yes\n")
    assert r.returncode == 0, r.stderr
    assert r.stdout.rstrip().endswith("gpt: windows reset, 1 reset credit left")
    (sent,) = fake_server.calls("/backend-api/wham/rate-limit-reset-credits/consume")
    assert sent.json["credit_id"] == "c-soon" and sent.json["redeem_request_id"]
    assert acct.usage["rate_limit"]["primary_window"]["used_percent"] == 0
    r = run_ccm("reset", "gpt", "-y")
    assert r.returncode == 0
    assert r.stdout.strip() == "gpt: windows reset, that was the last reset credit"
    r = run_ccm("reset", "gpt", "-y")
    assert r.returncode == 1 and r.stdout.strip() == "gpt: no reset credit to spend"
    fake_server.script("/backend-api/wham/rate-limit-reset-credits", 500, {"detail": "down"})
    r = run_ccm("reset", "gpt", "-y")
    assert r.returncode == 1
    assert r.stdout.strip() == "gpt: could not read the reset credits (HTTP 500: down)"
    acct.credits.append({"id": "c-new", "status": "available", "is_supported_by_plan": True,
                         "expires_at": "2100-02-01T00:00:00Z"})
    fake_server.script("/backend-api/wham/rate-limit-reset-credits/consume", 400,
                       {"detail": "No reset credit available"})
    r = run_ccm("reset", "gpt", "-y")
    assert r.returncode == 1
    assert r.stdout.strip() == "gpt: the reset was refused (HTTP 400: No reset credit available)"


def test_reset_and_poke_need_a_signed_in_account(sandbox, fake_server, run_ccm):
    os.makedirs(sandbox.slot("ghost"))
    os.makedirs(sandbox.codex_slot("cghost"))
    for name in ("ghost", "cghost"):
        r = run_ccm("reset", name, "-y")
        assert r.returncode == 1 and r.stdout.strip() == f"{name}: not signed in"
        r = run_ccm("poke", name)
        assert r.returncode == 1 and r.stdout.strip() == f"{name}: not signed in"
    assert fake_server.requests == []


def test_poke_sends_one_request_per_stopped_window_group(sandbox, fake_server, run_ccm):
    def later():
        # A poke within half a minute of a fetch reuses that fetch's payload,
        # so the server's new state is only seen once that floor has passed.
        os.remove(os.path.join(sandbox.home, ".claude-accts", ".usage-cache.json"))

    acct = fake_server.add_claude("idle@example.com")
    for lim in acct.limits:
        lim["percent"], lim["resets_at"] = 0, None
    sandbox.seed_claude("idle", "idle@example.com")
    out = plain(run_ccm("list").stdout)
    assert out.count("idle\n") == 3, "three windows with no clock"
    r = run_ccm("poke", "idle")
    assert r.returncode == 0 and r.stdout.strip() == "idle: 1 window group(s) started"
    assert [p["model"] for p in fake_server.pokes] == ["claude-fable-5-1"]
    assert all(lim["resets_at"] for lim in acct.limits)
    r = run_ccm("poke", "idle")
    assert r.returncode == 0 and r.stdout.strip() == "idle: every window is already running"
    assert len(fake_server.pokes) == 1
    # Only the 5-hour window stops: --weekly leaves it alone, a plain poke
    # starts it with the general model.
    acct.limits[0]["resets_at"] = None
    later()
    r = run_ccm("poke", "idle", "--weekly")
    assert r.returncode == 0
    assert r.stdout.strip() == "idle: every weekly window is already running"
    assert len(fake_server.pokes) == 1
    later()
    r = run_ccm("poke", "idle")
    assert r.returncode == 0 and r.stdout.strip() == "idle: 1 window group(s) started"
    assert fake_server.pokes[-1]["model"] == "claude-haiku-4-5-20251001"
    # Only the general weekly window stops: --weekly sends the general model.
    acct.limits[1]["resets_at"] = None
    later()
    r = run_ccm("poke", "idle", "--weekly")
    assert r.returncode == 0 and r.stdout.strip() == "idle: 1 window group(s) started"
    assert fake_server.pokes[-1]["model"] == "claude-haiku-4-5-20251001"
    assert len(fake_server.pokes) == 3
    # The server refusing the request is the answer, and the exit code.
    acct.limits[0]["resets_at"] = None
    later()
    fake_server.script("/v1/messages", 529, {"type": "error", "error": {
        "type": "overloaded_error", "message": "Overloaded"}})
    r = run_ccm("poke", "idle")
    assert r.returncode == 1 and r.stdout.strip() == "idle: claude-haiku-4-5-20251001: Overloaded"


def test_poke_starts_a_codex_window_through_the_codex_cli(sandbox, fake_server, run_ccm):
    acct = fake_server.add_codex("c@example.com", plan="plus")
    acct.usage["rate_limit"]["primary_window"].update(reset_at=None, reset_after_seconds=None)
    sandbox.seed_codex("gpt", "c@example.com")
    assert "idle" in plain(run_ccm("list").stdout)
    with open(os.path.join(sandbox.root, "codex-exec.fail"), "w") as f:
        f.write("You've hit your usage limit.\n")
    r = run_ccm("poke", "gpt")
    assert r.returncode == 1 and r.stdout.strip() == "gpt: You've hit your usage limit."
    os.remove(os.path.join(sandbox.root, "codex-exec.fail"))
    r = run_ccm("poke", "gpt")
    assert r.returncode == 0 and r.stdout.strip() == "gpt: 1 window group(s) started"
    (first, second) = sandbox.codex_execs()
    assert second["CODEX_HOME"] == sandbox.codex_slot("gpt")
    assert second["argv"][:2] == ["exec", "-C"] and "read-only" in second["argv"]
    work = second["argv"][2]
    assert work.startswith(os.path.join(sandbox.root, "tmp", "ccm-poke-"))
    assert not os.path.exists(work), "the throwaway directory is removed after"
    acct.usage["rate_limit"]["primary_window"].update(reset_at=4102444800,
                                                      reset_after_seconds=604800)
    r = run_ccm("poke", "gpt", "--weekly")
    assert r.returncode == 0
    assert r.stdout.strip() == "gpt: every weekly window is already running"
    assert len(sandbox.codex_execs()) == 2


def test_list_right_after_a_reset_or_a_poke_shows_the_new_state(sandbox, fake_server, work,
                                                                 run_ccm):
    out = plain(run_ccm("list").stdout)
    assert "58.0%" in out and "resets: 2" in out
    assert run_ccm("reset", "work", "-y").returncode == 0
    out = plain(run_ccm("list").stdout)
    assert "0.0%" in out and "58.0%" not in out
    assert "resets: 1 · expires 2100-02-01" in out
    # The same after a poke: the windows it started are running, not idle.
    for lim in work.limits:
        lim["resets_at"] = None
    os.remove(os.path.join(sandbox.home, ".claude-accts", ".usage-cache.json"))
    assert plain(run_ccm("list").stdout).count("idle\n") == 3
    assert run_ccm("poke", "work").returncode == 0
    out = plain(run_ccm("list").stdout)
    assert "idle" not in out and out.count("resets ") == 3
