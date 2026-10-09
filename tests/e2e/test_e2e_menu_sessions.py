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
from e2e.menu_harness import checked, click, enabled, index_of, same, text


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


def test_a_rule_click_lands_when_a_rebuild_waited_for_the_menu(sandbox, fake_server, menu):
    """The second project moved to an account took two clicks.

    The first move's hand-out lands while the menu is open again, which puts
    off a rebuild until it closes. AppKit closes the menu before it sends the
    clicked row its action, so a rebuild at close forgot the row in rumps'
    registry and the click found no callback.
    """
    import rumps

    sandbox.seed_claude("main", "main@example.com")
    sandbox.seed_claude("spare", "spare@example.com")
    menu.start_session("main", "T1", os.path.join(sandbox.home, "repos", "acme"))
    beta = menu.start_session("main", "T2", os.path.join(sandbox.home, "repos", "beta"),
                              tty="ttys002")
    menu.refresh()
    menu.open()
    menu.app._rebuild()          # what the first move's hand-out does on landing
    assert menu.app._rebuild_pending
    rows = menu.items(menu.session_row(beta))
    project = menu.find("Use for project “beta”", menu.session_row(beta))
    pick = next(r for r in rows[index_of(rows, project) + 1:] if "\ufffcspare " in text(r))
    # AppKit's order: the menu closes, then the row's action is looked up.
    menu.close()
    registry = rumps.rumps.NSApp._ns_to_py_and_callback
    assert pick._menuitem in registry
    sender, callback = registry[pick._menuitem]
    callback(sender)
    assert core.rules().projects == {"~/repos/beta": "spare"}
    menu.settle()
    assert not menu.app._rebuild_pending
    assert menu.texts(menu.session_row(beta))[0] == "  Spending spare, by a project rule"


def test_a_codex_session_row_moves_between_codex_accounts(sandbox, fake_server, menu):
    """A Codex row is offered Codex accounts alone, and a pin is a Codex rule."""
    sandbox.seed_claude("main", "main@example.com")
    sandbox.seed_codex("gpt", "gpt@example.com", plan="plus")
    sandbox.seed_codex("night", "night@example.com", plan="plus")
    cwd = os.path.join(sandbox.home, "repos", "acme")
    pid = menu.start_codex_session("gpt", "T2", cwd, name="acme-fixtures")
    menu.refresh()
    assert "RUNNING SESSIONS · 1" in menu.texts()
    row = menu.session_row(pid)
    assert "￼gpt " in text(row) and "fixtures" in text(row)
    assert menu.texts(row)[0] == "  Spending gpt, by the default"
    picks = [r for r in menu.items(row) if text(r).startswith(" ￼")]
    assert [text(r).split(" ")[1] for r in picks] == ["￼gpt", "￼night"] * 2
    assert "￼main" not in "".join(menu.texts(row))
    assert "○ 1" in text(menu.account_row("gpt"))
    click(picks[1])
    rules = core.rules()
    assert rules.codex_sessions["T2"] == "night" and rules.sessions == {}
    assert menu.flash() == ("this session now uses night. Codex reads its login when it "
                            "starts, so restart codex in that terminal")
    menu.settle()
    # The home now links to night's login, for the next Codex that starts there.
    assert os.path.realpath(os.path.join(core.codex.session_home("T2"), "auth.json")) == \
        os.path.realpath(os.path.join(sandbox.codex_slot("night"), "auth.json"))
    said = menu.texts(menu.session_row(pid))
    assert said[0] == "  Spending gpt; pinned here says night"
    assert said[1] == "  Codex reads its login when it starts, so restart it in this tab:"
    assert said[2] == "  press ctrl+C, then run  codex resume --last"
    assert "  Restart Codex in that tab now" in said and "  Take me to that tab" in said
    assert "Spending night" not in "".join(said)


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


def test_a_session_that_starts_while_the_menu_is_open_is_inserted(sandbox, fake_server, menu):
    """Rows are added under the pointer, even after the rows above were repainted."""
    sandbox.seed_claude("main", "main@example.com")
    cwd = os.path.join(sandbox.home, "repos", "acme")
    first = menu.start_session("main", "T1", cwd, name="acme-first")
    menu.refresh()
    menu.open()
    # The first session goes busy: its row is repainted in place.
    path = os.path.join(core.session_dir("T1"), "sessions", f"{first}.json")
    data = json.load(open(path))
    data["status"], data["updatedAt"] = "busy", int(time.time() * 1000)
    json.dump(data, open(path, "w"))
    menu.poll_sessions()
    assert " busy " in text(menu.session_row(first))
    assert not menu.app._rebuild_pending
    # A second one, started earlier, is only now in the registry: it sorts
    # after the first, so its row goes in under the repainted one.
    second = menu.start_session("main", "T2", os.path.join(sandbox.home, "repos", "beta"),
                                name="beta-second", tty="ttys002",
                                updated_at=time.time() - 60)
    menu.poll_sessions()
    assert same(menu.section("RUNNING SESSIONS · 2"),
                [menu.session_row(first), menu.session_row(second)])
    assert "\u25cf 2" in text(menu.account_row("main"))
    assert menu.app._rebuild_pending
    menu.close()
    menu.settle()                # the rebuild waits for the tick after the click
    assert not menu.app._rebuild_pending
    assert same(menu.section("RUNNING SESSIONS · 2"),
                [menu.session_row(first), menu.session_row(second)])


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
