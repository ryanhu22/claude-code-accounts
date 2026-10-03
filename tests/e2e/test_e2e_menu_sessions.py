"""The running sessions section: rows, their submenus, and moving a session.

A session here is a real process with the registry file Claude Code writes,
living in the per-terminal config dir ccm would have given it. A rule change
made in the menu must reach that dir's keychain item, which is how a running
session moves without a restart.
"""
import json
import os
import time

from claude_code_accounts import core, keychain
from e2e.menu_harness import checked, click, enabled, index_of, text


def test_no_sessions_says_none(sandbox, fake_server, menu):
    sandbox.seed_claude("main", "main@example.com")
    menu.refresh()
    assert menu.texts(menu.section("RUNNING SESSIONS")) == ["  none"]


def test_a_session_row_and_its_menu(sandbox, fake_server, menu):
    sandbox.seed_claude("main", "main@example.com")
    sandbox.seed_claude("spare", "spare@example.com")
    cwd = os.path.join(sandbox.home, "repos", "acme")
    pid = menu.start_session("main", "T1", cwd, name="acme-triage", status="busy")
    menu.refresh()
    assert menu.texts()[menu.texts().index("RUNNING SESSIONS · 1") - 1] != "RUNNING SESSIONS"
    row = menu.session_row(pid)
    drawn = text(row)
    # Account chip, repository, the session's own name, status and age.
    assert "￼main " in drawn and " acme " in drawn and "triage" in drawn
    assert " busy " in drawn and drawn.rstrip().endswith("0m")
    said = menu.texts(row)
    assert said[0] == "  Spending main, by the default"
    assert said[1] == "  Usage" and said[2].startswith("5h\t58%")
    assert "  Use for this session" in said
    assert "  Use for project “acme”" in said
    assert "  Context and spend" in said and "  Open in Finder" in said
    assert said[-1] == "  ~/repos/acme"
    # The picker lists every signed-in Claude account, the current one ticked
    # and left enabled so its figures draw at full strength.
    pins = menu.find("Use for this session", row)
    picks = [r for r in menu.items(row) if text(r).startswith(" ￼")]
    assert [text(r).split(" ")[1] for r in picks[:2]] == ["￼main", "￼spare"]
    assert enabled(picks[0]) and enabled(picks[1])
    assert "58%" in text(picks[0]) and "7d" in text(picks[0]) and "fable" in text(picks[0])
    assert pins is not None
    # No rule of its own yet, so nothing to remove.
    assert not any("Remove this rule" in line for line in said)
    # The account row counts it, lit because it is busy.
    assert "● 1" in text(menu.account_row("main"))
    assert "Running now · 1" in "".join(menu.texts(menu.account_row("main")))


def test_pinning_a_session_hands_its_dir_the_other_login(sandbox, fake_server, menu):
    sandbox.seed_claude("main", "main@example.com")
    spare = sandbox.seed_claude("spare", "spare@example.com")
    cwd = os.path.join(sandbox.home, "repos", "acme")
    pid = menu.start_session("main", "T1", cwd)
    menu.refresh()
    own = core.session_dir("T1")
    assert sandbox.blob(own)["accessToken"].startswith("at-main@example.com")
    row = menu.session_row(pid)
    picks = [r for r in menu.items(row) if text(r).startswith(" ￼")]
    click(picks[1])
    # The rule is written and the menu redrawn before the credential moves:
    # until the apply pass lands, the row says what it still spends.
    assert core.rules().sessions["T1"] == "spare"
    assert menu.flash() == "this session now uses spare"
    assert "￼main " in text(menu.session_row(pid))
    assert menu.texts(menu.session_row(pid))[0] == "  Spending main; pinned here says spare"
    menu.settle()
    assert menu.flash() == ("this session now uses spare. 1 running session switches "
                            "within about 30 seconds")
    # The session's own dir now holds spare's login, as a copy with no refresh
    # token: the app is the only thing that rotates it.
    copy = sandbox.blob(own)
    assert copy["accessToken"] == spare["accessToken"]
    assert not copy.get("refreshToken")
    assert "￼spare " in text(menu.session_row(pid))
    said = menu.texts(menu.session_row(pid))
    assert said[0] == "  Spending spare, by pinned here"
    assert any(line.strip() == "Remove this rule" for line in said)
    # Dropping the pin sends it back to the default account.
    click(menu.find("Remove this rule", menu.session_row(pid)))
    assert "T1" not in core.rules().sessions
    assert menu.flash() == "this session follows its profile again"
    menu.settle()
    assert sandbox.blob(own)["accessToken"].startswith("at-main@example.com")
    assert menu.texts(menu.session_row(pid))[0] == "  Spending main, by the default"


def test_a_project_rule_from_a_session_row(sandbox, fake_server, menu):
    sandbox.seed_claude("main", "main@example.com")
    sandbox.seed_claude("spare", "spare@example.com")
    cwd = os.path.join(sandbox.home, "repos", "acme")
    pid = menu.start_session("main", "T1", cwd)
    menu.refresh()
    row = menu.session_row(pid)
    project = menu.find("Use for project “acme”", row)
    picks = [r for r in menu.items(row) if text(r).startswith(" ￼")]
    # The second picker is the project's: its rows follow the project heading.
    rows = menu.items(row)
    after = rows[index_of(rows, project) + 1:]
    assert after[0] is picks[2] and after[1] is picks[3]
    click(after[1])
    assert core.rules().projects == {"~/repos/acme": "spare"}
    menu.settle()
    assert menu.flash().startswith("“acme” now uses spare. 1 running session switches")
    assert menu.texts(menu.session_row(pid))[0] == "  Spending spare, by a project rule"
    assert sandbox.blob(core.session_dir("T1"))["accessToken"].startswith(
        "at-spare@example.com")
    # The project picker now ticks spare and offers to drop the rule.
    rows = menu.items(menu.session_row(pid))
    project = menu.find("Use for project “acme”", menu.session_row(pid))
    after = rows[index_of(rows, project) + 1:]
    assert checked(after[1]) and not checked(after[0])
    assert text(after[2]).strip() == "Remove this rule"


def test_a_session_started_outside_ccm_is_told_to_restart(sandbox, fake_server, menu):
    sandbox.seed_claude("main", "main@example.com")
    sandbox.seed_claude("spare", "spare@example.com")
    cwd = os.path.join(sandbox.home, "repos", "acme")
    # Running straight out of the account's own dir: no per-terminal dir.
    pid = menu.start_session("main", "T1", cwd, config_dir=core.ensure_account_dir("main"))
    menu.refresh()
    core.assign("session", "T1", "spare", cwd=cwd, live=[])
    menu.refresh()
    said = menu.texts(menu.session_row(pid))
    assert said[0] == "  Spending main; pinned here says spare"
    assert said[1] == "  This tab started Claude outside ccm, so no rule can reach it. Restart it:"
    assert said[2] == "  press ctrl+C twice, then run  exec zsh  then  claude -c"
    assert "  Take me to that tab" in said


def test_take_me_to_that_tab_reports_a_terminal_that_refuses(sandbox, fake_server, menu,
                                                              dialogs):
    sandbox.seed_claude("main", "main@example.com")
    cwd = os.path.join(sandbox.home, "repos", "acme")
    pid = menu.start_session("main", "T1", cwd)
    menu.refresh()
    running = menu.find(" acme ", menu.account_row("main"))
    click(menu.find("Take me to that tab", running))
    assert dialogs.alerts[-1]["message"].startswith("Could not open that tab: ")
    assert sandbox.tool_calls("osascript")
    # Open in Finder goes through `open`.
    click(menu.find("Open in Finder", menu.session_row(pid)))
    assert sandbox.tool_calls("open")[-1]["argv"] == [cwd]


def test_sessions_come_and_go_between_refreshes(sandbox, fake_server, menu):
    sandbox.seed_claude("main", "main@example.com")
    menu.refresh()
    cwd = os.path.join(sandbox.home, "repos", "acme")
    pid = menu.start_session("main", "T1", cwd)
    # The five second poll finds it without a usage refresh.
    requests = len(fake_server.requests)
    menu.poll_sessions()
    assert len(fake_server.requests) == requests
    assert pid in menu.app._session_rows
    assert "RUNNING SESSIONS · 1" in menu.texts()
    assert "○ 1" in text(menu.account_row("main"))
    # It ends: the row goes and the count with it.
    menu.procs[-1].kill()
    menu.procs[-1].wait(5)
    menu.poll_sessions()
    assert pid not in menu.app._session_rows
    assert menu.texts(menu.section("RUNNING SESSIONS")) == ["  none"]


def test_menu_open_notices_a_session_that_ended(sandbox, fake_server, menu):
    sandbox.seed_claude("main", "main@example.com")
    cwd = os.path.join(sandbox.home, "repos", "acme")
    pid = menu.start_session("main", "T1", cwd)
    menu.refresh()
    menu.procs[-1].kill()
    menu.procs[-1].wait(5)
    menu.open()
    assert pid not in menu.app._session_rows
    assert menu.texts(menu.section("RUNNING SESSIONS")) == ["  none"]
    menu.close()


def test_quit_hands_the_refresh_tokens_back(sandbox, fake_server, menu):
    sandbox.seed_claude("main", "main@example.com")
    cwd = os.path.join(sandbox.home, "repos", "acme")
    menu.start_session("main", "T1", cwd)
    menu.refresh()
    own = core.session_dir("T1")
    keychain.forget()
    assert not sandbox.blob(own).get("refreshToken")
    click(menu.find("Quit"))
    assert menu.quits == [None]
    assert sandbox.blob(own)["refreshToken"] == sandbox.blob(sandbox.slot("main"))["refreshToken"]


def test_refresh_now_asks_the_server_again(sandbox, fake_server, menu):
    sandbox.seed_claude("main", "main@example.com")
    menu.refresh()
    asked = len(fake_server.calls("/api/oauth/usage"))
    assert text(menu.find("Refresh now")) == "Refresh now   updated just now"
    # Inside the forced floor the click is honoured but adds no request.
    click(menu.find("Refresh now"))
    menu.settle()
    assert len(fake_server.calls("/api/oauth/usage")) == asked
    store = json.load(open(core.USAGE_CACHE))
    store["main"]["tried_at"] = time.time() - 60
    json.dump(store, open(core.USAGE_CACHE, "w"))
    click(menu.find("Refresh now"))
    menu.settle()
    assert len(fake_server.calls("/api/oauth/usage")) == asked + 1
