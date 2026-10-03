"""The first run: no account signed in yet, and every command still answers."""
import json
import os

from claude_code_accounts import __version__
from e2e.harness import plain


def test_no_command_and_an_unknown_one_print_usage(run_ccm):
    r = run_ccm()
    assert r.returncode == 2 and "usage: ccm" in r.stderr
    assert "the following arguments are required: cmd" in r.stderr
    r = run_ccm("nope")
    assert r.returncode == 2 and "invalid choice: 'nope'" in r.stderr
    r = run_ccm("--version")
    assert r.returncode == 0 and r.stdout.strip() == f"ccm {__version__}"


def test_where_resolve_profiles_and_sessions_with_nothing_set_up(sandbox, run_ccm):
    r = run_ccm("where")
    assert r.returncode == 0, r.stderr
    out = plain(r.stdout)
    assert out.splitlines()[0] == "~"
    assert "account : none" in out
    assert "because : no rule covers it, so the default applies" in out
    assert "dir     : -" in out
    assert "codex" not in out, "no Codex accounts, so no Codex block"
    # The shell still gets a directory to launch with: the tools' own.
    assert run_ccm("resolve").stdout.strip() == sandbox.default_config
    assert run_ccm("resolve", "--codex").stdout.strip() == os.path.join(sandbox.home, ".codex")
    r = run_ccm("profiles")
    assert r.returncode == 0
    out = plain(r.stdout)
    assert "No profiles yet." in out and "`ccm profile new work`" in out
    assert "everything else        not set" in out
    r = run_ccm("sessions")
    assert r.returncode == 0 and r.stdout.strip() == "no Claude Code or Codex sessions running"
    r = run_ccm("log")
    assert r.returncode == 0 and r.stdout.strip() == "no credential events yet"
    assert sandbox.keychain() == {}
    assert sandbox.tripwire() == []


def test_naming_an_account_that_does_not_exist(sandbox, fake_server, run_ccm):
    for args in (("use", "work"), ("swap", "work"), ("pin", "work")):
        r = run_ccm(*args)
        assert r.returncode == 1, args
        assert r.stdout.strip() == "no account matches 'work' (have: none)", args
    for args in (("poke", "work"), ("reset", "work", "-y")):
        r = run_ccm(*args)
        assert r.returncode == 1, args
        assert r.stdout.strip() == "work: no account matches 'work' (have: none)", args
    r = run_ccm("unpin")
    assert r.returncode == 1 and r.stdout.strip() == "this session has no rule of its own"
    r = run_ccm("unpin", "--codex")
    assert r.returncode == 1 and r.stdout.strip() == "this session has no rule of its own"
    r = run_ccm("use", "work", "--session", env={"TERM_SESSION_ID": ""})
    assert r.returncode == 1
    assert "this terminal sets no TERM_SESSION_ID, so it cannot be pinned" in r.stderr
    assert fake_server.requests == [], "nothing to ask a server about"
    assert not os.path.exists(os.path.join(sandbox.home, ".claude-manager", "config.json")), \
        "no rule was written"


def test_reset_asks_first_and_an_empty_answer_spends_nothing(fake_server, run_ccm):
    r = run_ccm("reset", "work", input="")
    assert r.returncode == 1
    assert "Use one reset on work? Its limits go back to 0%. [y/N]" in r.stdout
    assert r.stdout.rstrip().endswith("nothing spent")
    assert fake_server.resets == []


def test_add_prints_the_sign_in_command_for_each_tool(sandbox, run_ccm):
    r = run_ccm("add", "work")
    assert r.returncode == 0
    slot = sandbox.slot("work")
    assert r.stdout.strip() == (
        f"mkdir -p '{slot}' && CLAUDE_CONFIG_DIR='{slot}' command claude "
        "# then type /login, sign in as work, and /exit")
    r = run_ccm("add", "gpt", "--codex")
    home = sandbox.codex_slot("gpt")
    assert r.stdout.strip() == f"mkdir -p {home} && CODEX_HOME={home} codex login"
    # Printing the command signs nothing in and makes no directory.
    assert not os.path.exists(slot) and not os.path.exists(home)


def test_auto_start_is_a_preference_the_app_reads(sandbox, run_ccm):
    prefs = os.path.join(sandbox.home, ".claude-manager", "prefs.json")
    r = run_ccm("auto-start")
    assert r.returncode == 0
    assert r.stdout.splitlines() == [
        "automatic start of weekly windows is off",
        "It runs from the menu bar app; the CLI only sets the preference."]
    r = run_ccm("auto-start", "on")
    assert r.stdout.splitlines() == [
        "automatic start of weekly windows is on",
        "This sends about 22 input tokens per stopped window, at most once an hour per account."]
    with open(prefs) as f:
        assert json.load(f)["auto_start_weekly"] is True
    r = run_ccm("auto-start", "off")
    assert r.stdout.splitlines() == ["automatic start of weekly windows is off"]
    with open(prefs) as f:
        assert json.load(f)["auto_start_weekly"] is False
    r = run_ccm("auto-start", "maybe")
    assert r.returncode == 2 and "invalid choice: 'maybe'" in r.stderr


def test_flags_that_do_not_go_together(run_ccm):
    r = run_ccm("login", "x", "--codex", "--paste")
    assert r.returncode == 1 and r.stderr.strip() == "Codex sign-in has no paste flow"
    r = run_ccm("shell-init", "fish")
    assert r.returncode == 2 and "invalid choice: 'fish'" in r.stderr
    r = run_ccm("use", "x", "--session", "--default")
    assert r.returncode == 2 and "not allowed with argument" in r.stderr
    r = run_ccm("profile", "rename", "p")
    assert r.returncode == 1
    assert r.stderr.strip() == "usage: ccm profile rename <old> <new-name>"


def test_menubar_uninstall_with_nothing_installed_touches_no_launchd(sandbox, run_ccm):
    r = run_ccm("menubar", "uninstall")
    assert r.returncode == 0
    assert r.stdout.strip() == "The menu bar app was not installed as a login item"
    assert sandbox.tripwire() == []
