"""The real menu bar app, driven in-process inside the e2e sandbox.

`ManagerApp` is built for real: rumps makes real NSMenuItems, the rows are
styled by AppKit, and every callback is the one a click would run. What is
taken away is only what needs a running application or a person: the run
loop timers (a test fires the ticks itself), the Dock and wake hooks, the
status item, and the dialogs, which are recorded and answered from a queue.

Accounts come from the sandbox (`sandbox.seed_claude`) through core, against
the fake server, and sessions come from registry files the test writes for
processes it starts.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
import rumps

from claude_code_accounts import core, focus, menubar, oauth, sessions

SETTLE_TIMEOUT = 30.0
# What a terminal tab's environment carries. The sandbox's `ps` answers with
# nothing, so a test registers each session's tab here instead.
TERMINAL = "Apple_Terminal"
# The provider mark in front of an account name: a text attachment.
CHIP = "\ufffc"


class Dialogs:
    """rumps' alert, notification and Window, recorded and answered from a queue.

    `answers` holds what the next dialog gets back: an int for an alert (1 is
    the OK button), a `(clicked, text)` pair for a Window. An empty queue
    answers OK, with the Window's default text.
    """

    def __init__(self) -> None:
        self.alerts: list[dict] = []
        self.notifications: list[dict] = []
        self.windows: list[dict] = []
        self.answers: list = []

    def alert(self, title=None, message="", ok=None, cancel=None, other=None,
              icon_path=None) -> int:
        self.alerts.append({"title": title, "message": message, "ok": ok, "cancel": cancel})
        return self.answers.pop(0) if self.answers else 1

    def notification(self, title, subtitle, message, *args, **kwargs) -> None:
        self.notifications.append({"title": title, "subtitle": subtitle, "message": message})

    def window(self, title="", message="", default_text="", ok=None, cancel=None,
               dimensions=(320, 160), secure=False):
        record = {"title": title, "message": message, "default_text": default_text,
                  "ok": ok, "cancel": cancel}
        self.windows.append(record)
        clicked, text = self.answers.pop(0) if self.answers else (1, default_text)
        return SimpleNamespace(run=lambda: SimpleNamespace(clicked=clicked, text=text))

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(rumps, "alert", self.alert)
        monkeypatch.setattr(rumps, "notification", self.notification)
        monkeypatch.setattr(rumps, "Window", self.window)


class Tabs:
    """The terminal tabs the sandbox's sessions run in, keyed by pid."""

    def __init__(self) -> None:
        self.env: dict[int, tuple[dict[str, str], str]] = {}

    def environ(self, pid: int, proc_start: str = "") -> tuple[dict[str, str], str]:
        return self.env.get(pid, ({}, ""))


@pytest.fixture
def dialogs(monkeypatch) -> Dialogs:
    d = Dialogs()
    d.install(monkeypatch)
    return d


@pytest.fixture
def tabs(monkeypatch) -> Tabs:
    t = Tabs()
    monkeypatch.setattr(sessions, "_environ", t.environ)
    monkeypatch.setattr(sessions, "_ENV_CACHE", {})
    return t


@pytest.fixture
def procs():
    """Processes that stand in for running sessions; all killed after the test."""
    started: list[subprocess.Popen] = []
    yield started
    for p in started:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)


class Menu:
    """One app, with the helpers a flow needs.

    `timers` are the run loop timers the app asked for, as (callback,
    seconds). `delayed` are the one-shot `threading.Timer`s it armed, as
    (seconds, function), left for the test to fire. `quits` counts the calls
    to rumps.quit_application, which would end the test process.
    """

    def __init__(self, app: menubar.ManagerApp, sandbox, tabs: Tabs, procs: list,
                 timers: list, delayed: list, quits: list, threads: list) -> None:
        self.app = app
        self.sandbox = sandbox
        self.tabs = tabs
        self.procs = procs
        self.timers = timers
        self.delayed = delayed
        self.quits = quits
        self.threads = threads

    def fire_delayed(self) -> None:
        """Run every one-shot timer the app armed, then settle."""
        armed, self.delayed[:] = list(self.delayed), []
        for _seconds, fn in armed:
            fn()
        self.settle()

    # ------------------------------------------------------------- driving

    def settle(self, timeout: float = SETTLE_TIMEOUT) -> None:
        """Run the main-thread ticks until every worker has reported in."""
        app = self.app
        deadline = time.monotonic() + timeout
        while True:
            app._on_sync_tick(None)
            quiet = (not app._busy and not app._syncing and not app._polling
                     and not app._applying and app._pending is None and not app._done
                     and app._fresh_sessions is None and not app._signing_in
                     and not any(t.is_alive() for t in self.threads))
            if quiet:
                return
            if time.monotonic() > deadline:
                raise TimeoutError("the menu bar app never settled")
            time.sleep(0.02)

    def refresh(self, force: bool = True) -> None:
        self.app._on_refresh_tick(None, force=force)
        self.settle()

    def poll_sessions(self) -> None:
        self.app._on_sessions_tick(None)
        self.settle()

    def open(self) -> None:
        """What the menu does when it is about to drop down."""
        self.app._on_menu_open()

    def close(self) -> None:
        self.app._on_menu_close()

    # ------------------------------------------------------------- sessions

    def start_session(self, account: str, term_id: str, cwd: str, *, name: str = "",
                      status: str = "idle", kind: str = "interactive", tty: str = "ttys001",
                      config_dir: str | None = None, updated_at: float | None = None) -> int:
        """A live Claude Code session: a process plus the registry file it writes.

        The session runs in the per-terminal config dir ccm would have given
        it (`core.prepare_session`), which is how a rule reaches it.
        """
        path = config_dir or core.prepare_session(term_id, account)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        os.makedirs(cwd, exist_ok=True)
        os.makedirs(os.path.join(path, "sessions"), exist_ok=True)
        now = int((updated_at or time.time()) * 1000)
        with open(os.path.join(path, "sessions", f"{proc.pid}.json"), "w") as f:
            json.dump({"pid": proc.pid, "sessionId": f"sess-{proc.pid}", "cwd": cwd,
                       "name": name or os.path.basename(cwd), "kind": kind,
                       "nameSource": "user" if name else "derived", "status": status,
                       "startedAt": now, "updatedAt": now, "entrypoint": "cli"}, f)
        self.tabs.env[proc.pid] = (
            {"TERM_SESSION_ID": term_id, "TERM_PROGRAM": TERMINAL, "CLAUDE_CONFIG_DIR": path},
            tty)
        return proc.pid

    # ------------------------------------------------------------- reading

    @property
    def menu(self) -> rumps.MenuItem:
        return self.app.menu

    def items(self, item=None) -> list[rumps.MenuItem]:
        """The rows of a menu (or of a section), separators left out.

        Read from the NSMenu, which is what the screen shows. rumps keeps a
        dict beside it keyed by each row's title at the time it was added,
        and two styled rows with the same text share one key there, so the
        dict can be a row short of the menu.
        """
        parent = self.app.menu if item is None else item
        if isinstance(parent, list):           # a section, already rows
            return list(parent)
        return [row for row in _rows(parent) if row is not SEPARATOR]

    def texts(self, item=None) -> list[str]:
        return [text(row) for row in self.items(item)]

    def find(self, needle: str, item=None) -> rumps.MenuItem:
        """The first row whose drawn text holds `needle`."""
        for row in self.items(item):
            if needle in text(row):
                return row
        raise KeyError(f"no row holding {needle!r} in {self.texts(item)}")

    def section(self, heading: str) -> list[rumps.MenuItem]:
        """The rows under one of the menu's headings, up to the next separator."""
        rows, inside = [], False
        for row in _rows(self.app.menu):
            if row is SEPARATOR:
                if inside:
                    break
                continue
            if inside:
                rows.append(row)
            elif text(row) == heading:
                inside = True
        return rows

    def account_row(self, name: str) -> rumps.MenuItem:
        for row in self.section("SUBSCRIPTIONS"):
            if f"{CHIP}{name} " in text(row):
                return row
        raise KeyError(f"no account row for {name!r} in {self.texts()}")

    def account_names(self) -> list[str]:
        """The accounts listed under SUBSCRIPTIONS, in order."""
        return [text(row).split(CHIP, 1)[1].split(" ", 1)[0]
                for row in self.section("SUBSCRIPTIONS")]

    def session_row(self, pid: int) -> rumps.MenuItem:
        return self.app._session_rows[pid][0]

    def flash(self) -> str:
        """The flash row: the result of the last action, or ""."""
        message, _tone, at = self.app._flash
        return message if message and time.time() - at <= menubar.FLASH_SECONDS else ""

    def title(self) -> str:
        """The menu bar text, as drawn when there is no status item to draw into."""
        return self.app.title or ""


SEPARATOR = object()


def _rows(parent) -> list:
    """The rows of an NSMenu in order, as their rumps objects; SEPARATOR for a line."""
    nsmenu = getattr(parent, "_menu", None)
    if nsmenu is None:
        return []
    registry = rumps.rumps.NSApp._ns_to_py_and_callback
    out = []
    for ns in nsmenu.itemArray():
        if ns.isSeparatorItem():
            out.append(SEPARATOR)
            continue
        entry = registry.get(ns)
        assert entry is not None, f"row {ns.title()!r} is not a rumps item"
        out.append(entry[0])
    return out


def same(rows, expected) -> bool:
    """Whether two lists hold the same menu items, by identity.

    A rumps MenuItem is a dict of its submenu, so == compares submenus and
    calls two plain rows equal.
    """
    return len(rows) == len(expected) and all(a is b for a, b in zip(rows, expected, strict=True))


def index_of(rows, item) -> int:
    return next(i for i, row in enumerate(rows) if row is item)


def text(item) -> str:
    """What a row shows: its attributed title when it has one, else its title."""
    try:
        styled = item._menuitem.attributedTitle()
    except AttributeError:
        return str(getattr(item, "title", ""))
    return str(styled.string()) if styled is not None else str(item.title)


def click(item: rumps.MenuItem):
    """Do what a click does: run the row's callback with the row as sender."""
    callback = item.callback
    assert callback is not None, f"row {item.title!r} has no action"
    return callback(item)


def enabled(item: rumps.MenuItem) -> bool:
    return item.callback is not None


def checked(item: rumps.MenuItem) -> bool:
    return bool(item._menuitem.state())


@pytest.fixture
def menu(sandbox, monkeypatch, dialogs, tabs, procs, tmp_path):
    """The real ManagerApp, settled after its first refresh."""
    timers: list[tuple] = []
    delayed: list[tuple] = []
    quits: list = []
    monkeypatch.setattr(menubar, "_start_timer",
                        lambda callback, seconds: timers.append((callback, seconds)))

    class Delayed:
        """A threading.Timer that waits for the test instead of the clock."""

        def __init__(self, seconds, fn):
            self.seconds, self.fn = seconds, fn

        def start(self):
            delayed.append((self.seconds, self.fn))

    threads: list[threading.Thread] = []

    class Tracked(threading.Thread):
        """A thread the app started, so `settle` can wait for it."""

        def start(self):
            threads.append(self)
            super().start()

    monkeypatch.setattr(menubar, "threading", SimpleNamespace(
        Thread=Tracked, Lock=threading.Lock, Timer=Delayed))
    monkeypatch.setattr(rumps, "quit_application", lambda sender=None: quits.append(sender))
    monkeypatch.setattr(menubar.ManagerApp, "_hide_from_dock", staticmethod(lambda: None))
    monkeypatch.setattr(menubar.ManagerApp, "_watch_stop", lambda self: None)
    monkeypatch.setattr(menubar.ManagerApp, "_watch_wake", lambda self: None)
    # rumps makes a folder in the real ~/Library/Application Support on init.
    support = tmp_path / "support"
    support.mkdir()
    monkeypatch.setattr(rumps.rumps, "application_support", lambda name: str(support / name))
    # No terminal is in front of a test, and asking would reach the real screen.
    monkeypatch.setattr(focus, "frontmost_bundle_id", lambda: "")
    monkeypatch.setattr(oauth, "_INSTALLED", None)
    monkeypatch.setattr(core, "SOLE_REFRESHER", False)
    app = menubar.ManagerApp()
    m = Menu(app, sandbox, tabs, procs, timers, delayed, quits, threads)
    m.settle()
    yield m
    for p in procs:
        if p.poll() is None:
            p.kill()
