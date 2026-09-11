"""Restarting a moved Codex session, with every AppleScript recorded and none run."""

from types import SimpleNamespace

import pytest

from claude_code_accounts import focus

ITERM = "com.googlecode.iterm2"
TERMINAL = "com.apple.Terminal"
THREAD = "0199c0f4-4c4b-7b52-9e1e-f0b1b4d0a4e1"


@pytest.fixture
def scripts(monkeypatch):
    """Every script the module would run, in order, answering "ok" to each.

    The wait between quitting the TUI and typing the resume is recorded in the
    same list, because where it falls is the whole point of it.
    """
    ran: list[str] = []

    def run(argv, **kwargs):
        ran.append(argv[-1])
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(focus.subprocess, "run", run)
    monkeypatch.setattr(focus.time, "sleep", lambda seconds: ran.append(f"waited {seconds}"))
    monkeypatch.setattr(focus, "frontmost_bundle_id", lambda: TERMINAL)
    return ran


def test_iterm_quits_and_resumes_in_the_session_holding_the_tty(scripts):
    assert focus.restart_codex(ITERM, "ttys003", THREAD, busy=False) == ""
    reveal, quit_it, waited, resume = scripts
    assert 'tell application "iTerm2"' in reveal and "select t" in reveal
    assert 'if tty of s is "/dev/ttys003"' in quit_it
    assert "tell s to write text (ASCII character 4) newline false" in quit_it
    assert waited == f"waited {focus.RESTART_WAIT}"
    assert f'tell s to write text "codex resume {THREAD}"' in resume


def test_terminal_types_the_resume_through_system_events(scripts):
    assert focus.restart_codex(TERMINAL, "ttys003", THREAD, busy=False) == ""
    reveal, quit_it, waited, resume = scripts
    assert 'tell application "Terminal"' in reveal and '"/dev/ttys003"' in reveal
    assert quit_it == 'tell application "System Events" to key code 2 using control down'
    assert waited == f"waited {focus.RESTART_WAIT}"
    assert resume == ('tell application "System Events" to '
                      f'keystroke "codex resume {THREAD}" & return')


def test_a_busy_session_is_left_alone_after_the_reveal(scripts):
    assert focus.restart_codex(TERMINAL, "ttys003", THREAD, busy=True) == (
        "that session is in the middle of a turn; wait for it to finish")
    assert len(scripts) == 1 and 'tell application "Terminal"' in scripts[0]


@pytest.mark.parametrize("thread_id", [
    "", "not-a-uuid", "; rm -rf /", "0199c0f4-4c4b-7b52-9e1e-f0b1b4d0a4e",
    '0199c0f4-4c4b-7b52-9e1e-f0b1b4d0a4"e',
])
def test_a_thread_id_that_is_not_a_uuid_runs_nothing(scripts, thread_id):
    assert focus.restart_codex(TERMINAL, "ttys003", thread_id, busy=False) == (
        "that session has no thread to resume")
    assert scripts == []


def test_a_terminal_that_cannot_be_scripted_says_so(scripts):
    assert focus.restart_codex("com.mitchellh.ghostty", "ttys003", THREAD, busy=False) == (
        "that terminal cannot be scripted")
    assert scripts == []


def test_a_tab_that_is_gone_stops_at_the_reveal(monkeypatch, scripts):
    monkeypatch.setattr(focus.subprocess, "run", lambda argv, **kwargs: SimpleNamespace(
        returncode=0, stdout=scripts.append(argv[-1]) or "no tab", stderr=""))
    assert focus.restart_codex(ITERM, "ttys003", THREAD, busy=False) == "that tab is gone"
    assert len(scripts) == 1


def test_terminal_refuses_to_type_when_it_is_not_in_front(monkeypatch, scripts):
    monkeypatch.setattr(focus, "frontmost_bundle_id", lambda: "com.apple.Safari")
    assert focus.restart_codex(TERMINAL, "ttys003", THREAD, busy=False) == (
        "the terminal did not come to the front")
    assert len(scripts) == 1


def test_terminal_types_nothing_once_another_app_takes_the_front(monkeypatch, scripts):
    """System Events types into whatever is in front, so the front is checked twice."""
    fronts = iter([TERMINAL, "com.apple.Safari"])
    monkeypatch.setattr(focus, "frontmost_bundle_id", lambda: next(fronts))
    assert focus.restart_codex(TERMINAL, "ttys003", THREAD, busy=False) == (
        "the terminal did not stay in front, so nothing was typed")
    assert [line for line in scripts if "keystroke" in line] == []


def test_a_refused_script_reports_its_last_line(monkeypatch):
    monkeypatch.setattr(focus.subprocess, "run", lambda argv, **kwargs: SimpleNamespace(
        returncode=1, stdout="", stderr="osascript:\nnot allowed to send keystrokes"))
    assert focus.reveal_tab(TERMINAL, "ttys003") == "not allowed to send keystrokes"
