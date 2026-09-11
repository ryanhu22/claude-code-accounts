"""Which Claude Code session the user is looking at right now.

The menu bar follows the front terminal tab, so the usage it shows is the
account that tab is spending. Two facts pin that down:

- The front application, from NSWorkspace. Cheap, no permission needed.
- The selected tab's tty, which Terminal and iTerm2 answer over AppleScript.
  Every process knows its own tty, so the tty names the session exactly, even
  with a dozen tabs open on the same repo.

A terminal that cannot be scripted (Ghostty, Warp, VS Code, WezTerm) still
tells us which app the tab belongs to, through TERM_PROGRAM in the session's
environment. There the most recently active session of that app is the best
guess, and the caller is told it is a guess.

AppleScript needs the user's one-time Automation consent. Until it is granted
the query fails fast, and this backs off rather than prompting every second.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import sessions

# bundle id -> (TERM_PROGRAM value, AppleScript for the selected tab's tty)
TERMINALS: dict[str, tuple[str, str | None]] = {
    "com.apple.Terminal": (
        "Apple_Terminal",
        'tell application "Terminal" to get tty of selected tab of front window'),
    "com.googlecode.iterm2": (
        "iTerm.app",
        'tell application "iTerm2" to tell current session of current window to get tty'),
    "com.mitchellh.ghostty": ("ghostty", None),
    "dev.warp.Warp-Stable": ("WarpTerminal", None),
    "dev.warp.Warp": ("WarpTerminal", None),
    "com.microsoft.VSCode": ("vscode", None),
    "com.microsoft.VSCodeInsiders": ("vscode", None),
    "com.todesktop.230313mzl4w4u92": ("vscode", None),     # Cursor
    "com.github.wez.wezterm": ("WezTerm", None),
}

TTY_POLL = 1.5          # how often to ask the front terminal which tab is up
DENIED_BACKOFF = 30.0   # after AppleScript fails, wait this long before asking again


@dataclass
class Focus:
    session: sessions.Session | None
    exact: bool = True        # tty match, as opposed to "newest tab of that app"
    note: str = ""            # why there is no exact answer, for the menu


def frontmost_bundle_id() -> str:
    try:
        import AppKit
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(app.bundleIdentifier() or "") if app else ""
    except Exception:
        return ""


def selected_tty(bundle_id: str) -> tuple[str, str]:
    """(tty, error) for the front tab of a scriptable terminal.

    The tty comes back as `/dev/ttys003`; sessions carry the bare `ttys003`,
    so the prefix is dropped here.
    """
    script = TERMINALS.get(bundle_id, ("", None))[1]
    if not script:
        return "", "not scriptable"
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return "", "osascript failed"
    if r.returncode != 0:
        err = r.stderr.strip()
        if "-1743" in err or "not allowed" in err.lower():
            return "", "needs Automation permission for Terminal"
        return "", err.splitlines()[-1] if err else "no front tab"
    return r.stdout.strip().rsplit("/", 1)[-1], ""


def pick(live: list[sessions.Session], bundle_id: str, tty: str) -> Focus:
    """The session the front tab hosts, or the best stand-in."""
    if tty:
        hits = [s for s in live if s.tty == tty]
        if hits:
            hits.sort(key=lambda s: (not s.interactive, -s.updated_at))
            return Focus(hits[0], exact=True)
        return Focus(None, exact=True, note="this tab has no session")
    program = TERMINALS.get(bundle_id, ("", None))[0]
    if not program:
        # No terminal has been in front yet. The newest session is the best
        # stand-in: it is the one the user most likely just left.
        hits = [s for s in live if s.interactive]
        if not hits:
            return Focus(None, exact=False, note="no session is running")
        hits.sort(key=lambda s: -s.updated_at)
        return Focus(hits[0], exact=False, note="newest session; click a Terminal tab to follow it")
    hits = [s for s in live if s.term_program == program and s.interactive]
    if not hits:
        return Focus(None, exact=False, note=f"no session in {program}")
    hits.sort(key=lambda s: -s.updated_at)
    return Focus(hits[0], exact=False, note="newest tab of this app; it cannot say which is front")


class Tracker:
    """Polls from the main-thread timer; the AppleScript round trip runs off it.

    `poll` is cheap and returns the current focus. It launches at most one
    background query at a time and only while a terminal is in front, so a
    browser or editor in front costs nothing. The last known session is kept
    when the user switches to a non-terminal app: the menu bar should keep
    describing the tab they came from, not go blank.
    """

    def __init__(self, on_change: Callable[[], None]) -> None:
        self._on_change = on_change
        self._lock = threading.Lock()
        self._live: list[sessions.Session] = []
        self._bundle = ""
        self._tty = ""
        self._error = ""
        self._asked_at = 0.0
        self._failed_at = 0.0
        self._busy = False
        self._pending: tuple[str, str, str] | None = None   # bundle, tty, error
        self.focus = Focus(None, exact=True)
        self.enabled = True

    def update_sessions(self, live: list[sessions.Session]) -> None:
        self._live = live
        self._resolve()

    def poll(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            pending, self._pending = self._pending, None
        if pending:
            self._bundle, self._tty, self._error = pending
            self._resolve()
        bundle = frontmost_bundle_id()
        if bundle not in TERMINALS:
            return
        if not TERMINALS[bundle][1]:
            if bundle != self._bundle:
                self._bundle, self._tty, self._error = bundle, "", ""
                self._resolve()
            return
        now = time.time()
        if self._busy or now - self._asked_at < TTY_POLL:
            return
        if self._error and now - self._failed_at < DENIED_BACKOFF:
            return
        self._busy = True
        self._asked_at = now
        threading.Thread(target=self._query, args=(bundle,), daemon=True).start()

    def _query(self, bundle: str) -> None:
        try:
            tty, err = selected_tty(bundle)
            if err:
                self._failed_at = time.time()
            with self._lock:
                self._pending = (bundle, tty, err)
        finally:
            self._busy = False

    def _resolve(self) -> None:
        before = self.focus.session.pid if self.focus.session else None
        if self._error:
            focus = pick(self._live, self._bundle, "")
            if not focus.session:
                focus.note = self._error
        else:
            focus = pick(self._live, self._bundle, self._tty)
        # A tab with no session keeps the previous session on screen: the user
        # glanced away to a shell, the account they care about has not changed.
        if focus.session is None and self.focus.session is not None:
            still = next((s for s in self._live if s.pid == self.focus.session.pid), None)
            if still is not None:
                self.focus = Focus(still, self.focus.exact, self.focus.note)
                return
        self.focus = focus
        after = focus.session.pid if focus.session else None
        if before != after:
            self._on_change()


REVEAL = {
    "com.apple.Terminal": '''
tell application "Terminal"
  activate
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is "{tty}" then
        set selected of t to true
        set frontmost of w to true
        return "ok"
      end if
    end repeat
  end repeat
end tell
return "no tab"''',
    "com.googlecode.iterm2": '''
tell application "iTerm2"
  activate
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is "{tty}" then
          select t
          select w
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
end tell
return "no tab"''',
}


# Quitting the Codex TUI and starting it again on the same thread. ctrl+D on
# an empty prompt quits at once, where ctrl+C only asks for a second ctrl+C.
#
# iTerm2 writes into the session that holds the tty, which needs no focus at
# all. Terminal has no such command, so its two lines go through System Events,
# which types into whatever is in front: the tab has to be there first, and
# still be there a moment later, or the resume lands in another app.
ITERM_QUIT = '''
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is "{tty}" then
          tell s to write text (ASCII character 4) newline false
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
end tell
return "no tab"'''

ITERM_RESUME = '''
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is "{tty}" then
          tell s to write text "codex resume {thread}"
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
end tell
return "no tab"'''

TERMINAL_QUIT = 'tell application "System Events" to key code 2 using control down'

TERMINAL_RESUME = ('tell application "System Events" to '
                   'keystroke "codex resume {thread}" & return')

# Codex thread ids are uuids out of Codex's own database. Nothing else is ever
# typed into a terminal: the shape is checked before the id reaches a script.
THREAD_ID = re.compile(r"^[0-9a-f-]{36}$")

# How long the TUI gets to leave before the resume line is typed. Typing into
# a Codex that is still shutting down would put the line nowhere.
RESTART_WAIT = 1.5


def bundle_for_program(term_program: str) -> str:
    """The bundle id behind a TERM_PROGRAM value, so a session names its app."""
    for bundle, (program, _script) in TERMINALS.items():
        if program and program == term_program:
            return bundle
    return ""


def _run_script(script: str, wants_ok: bool = False) -> str:
    """One AppleScript: "" when it worked, else why not, in the menu's words."""
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "could not reach the terminal"
    if r.returncode != 0:
        return (r.stderr.strip().splitlines() or ["AppleScript refused"])[-1][:120]
    if wants_ok and r.stdout.strip() != "ok":
        return "that tab is gone"
    return ""


def _tty_path(tty: str) -> str:
    return tty if tty.startswith("/dev/") else "/dev/" + tty


def reveal_tab(bundle_id: str, tty: str) -> str:
    """Bring the terminal tab running on `tty` to the front.

    A running session can only change account by restarting, and the slow part
    of that is finding the right tab among a dozen. This puts the user in it.
    Returns "" on success, else why not.
    """
    script = REVEAL.get(bundle_id)
    if not script or not tty:
        return "that terminal cannot be scripted"
    return _run_script(script.format(tty=_tty_path(tty)), wants_ok=True)


def restart_codex(bundle_id: str, tty: str, thread_id: str, busy: bool) -> str:
    """Quit the Codex in a tab and start it again on the same thread.

    A running Codex keeps the login it read at start, so a rule that names
    another account only reaches it through a restart. The thread is what
    makes that cheap: `codex resume` comes back to the same conversation, on
    the account the rule now names. Returns "" on success, else why not, the
    way `reveal_tab` does.
    """
    if not THREAD_ID.match(thread_id or ""):
        return "that session has no thread to resume"
    err = reveal_tab(bundle_id, tty)
    if err:
        return err
    if busy:
        # ctrl+D in the middle of a turn throws away what the turn has done.
        # The row stays in the menu, so waiting costs one more click.
        return "that session is in the middle of a turn; wait for it to finish"
    if bundle_id == "com.googlecode.iterm2":
        err = _run_script(ITERM_QUIT.format(tty=_tty_path(tty)), wants_ok=True)
        if err:
            return err
        time.sleep(RESTART_WAIT)
        return _run_script(
            ITERM_RESUME.format(tty=_tty_path(tty), thread=thread_id), wants_ok=True)
    if bundle_id == "com.apple.Terminal":
        if frontmost_bundle_id() != "com.apple.Terminal":
            return "the terminal did not come to the front"
        err = _run_script(TERMINAL_QUIT)
        if err:
            return err
        time.sleep(RESTART_WAIT)
        if frontmost_bundle_id() != "com.apple.Terminal":
            return "the terminal did not stay in front, so nothing was typed"
        return _run_script(TERMINAL_RESUME.format(thread=thread_id))
    return "that terminal cannot be scripted"
