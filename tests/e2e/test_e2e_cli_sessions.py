"""`ccm sessions`, with Claude Code sessions running in the sandbox."""
import json
import os
import shutil

from e2e.harness import TERM_ID, plain

FOOTER = ("● = has a rule of its own. `ccm use <account> --session` pins the terminal "
          "you run it in.")


def rows(text: str) -> list[list[str]]:
    body = plain(text).split("\n\n")[0]
    return [line.split() for line in body.splitlines()]


def test_rows_name_the_account_each_session_spends_and_why(sandbox, run_ccm):
    sandbox.seed_claude("work", "work@example.com")
    sandbox.seed_claude("personal", "personal@example.com")
    run_ccm("use", "work", "--default")
    # This terminal's own dir, holding work's login, as the wrapper makes one.
    own = run_ccm("resolve").stdout.strip()
    sandbox.seed_session(own, sandbox.home, name="work-ab12", status="busy")
    # A session in another terminal launched straight in the account's dir,
    # and a background one with no terminal at all, on personal's own dir.
    proj = os.path.join(sandbox.home, "proj")
    os.makedirs(proj)
    sandbox.seed_session(sandbox.slot("work"), proj, term_id="OTHER-0000")
    sandbox.seed_session(sandbox.slot("personal"), proj, term_id=None)
    r = run_ccm("sessions")
    assert r.returncode == 0, r.stderr
    assert plain(r.stdout).rstrip().endswith(FOOTER)
    assert rows(r.stdout) == [
        ["proj", "idle", "~/proj", "personal", "default", "->", "work", "on", "restart"],
        ["proj", "idle", "~/proj", "work", "default"],
        ["work-ab12", "busy", "~", "work", "default"]]
    # A pin lands in this terminal's dir within the command, and the row says so.
    r = run_ccm("pin", "personal")
    assert plain(r.stdout).splitlines()[0] == \
        "this session now uses personal. 1 running session switches within about 30 seconds"
    assert sandbox.blob(own)["accessToken"] == sandbox.blob(sandbox.slot("personal"))[
        "accessToken"]
    assert rows(run_ccm("sessions").stdout)[2] == \
        ["●", "work-ab12", "busy", "~", "personal", "session"]
    # A project rule reaches the sessions in it. The one launched in an
    # account dir has nowhere of its own to be moved, so it needs a restart.
    r = run_ccm("use", "personal", cwd=proj)
    assert plain(r.stdout).splitlines()[0] == "“proj” now uses personal"
    assert rows(run_ccm("sessions").stdout)[:2] == [
        ["proj", "idle", "~/proj", "personal", "project"],
        ["proj", "idle", "~/proj", "work", "project", "->", "personal", "on", "restart"]]
    r = run_ccm("unpin")
    assert plain(r.stdout).splitlines()[0] == \
        "this session follows its profile again. 1 running session switches within about 30 seconds"
    assert rows(run_ccm("sessions").stdout)[2] == ["work-ab12", "busy", "~", "work", "default"]
    assert sandbox.blob(own)["accessToken"] == sandbox.blob(sandbox.slot("work"))["accessToken"]


def test_a_broader_rule_releases_the_pins_under_it(sandbox, run_ccm):
    sandbox.seed_claude("work", "work@example.com")
    sandbox.seed_claude("personal", "personal@example.com")
    run_ccm("use", "work", "--default")
    own = run_ccm("resolve").stdout.strip()
    sandbox.seed_session(own, sandbox.home)
    run_ccm("pin", "personal")
    r = run_ccm("use", "work")
    assert plain(r.stdout).splitlines()[0] == \
        "“home” now uses work, releasing 1 pinned session. 1 running session switches " \
        "within about 30 seconds"
    with open(os.path.join(sandbox.home, ".claude-manager", "config.json")) as f:
        assert json.load(f)["sessions"] == {}
    assert sandbox.blob(own)["accessToken"] == sandbox.blob(sandbox.slot("work"))["accessToken"]


def test_a_pin_whose_terminal_is_gone_is_dropped(sandbox, run_ccm):
    sandbox.seed_claude("work", "work@example.com")
    run_ccm("pin", "work")
    config = os.path.join(sandbox.home, ".claude-manager", "config.json")
    with open(config) as f:
        assert json.load(f)["sessions"] == {TERM_ID: "work"}
    # The terminal closed and its dir was cleaned up.
    shutil.rmtree(os.path.join(sandbox.home, ".claude-ctx", "s-e2e0000000"))
    assert run_ccm("sessions").stdout.strip() == "no Claude Code or Codex sessions running"
    with open(config) as f:
        assert json.load(f)["sessions"] == {}
    assert "session" not in plain(run_ccm("profiles").stdout)
