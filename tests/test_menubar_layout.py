"""Check menu updates and column arithmetic without loading AppKit."""

import importlib.util
import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from claude_code_accounts import core, focus, glyphs, oauth, profiles, sessions, transcripts

CODEX_HOME = "/homes/codex"
THREAD = "0199c0f4-4c4b-7b52-9e1e-f0b1b4d0a4e1"


@pytest.fixture
def menubar(monkeypatch):
    monkeypatch.setitem(sys.modules, "rumps", SimpleNamespace(App=object))
    monkeypatch.setitem(sys.modules, "AppKit", None)
    path = Path(__file__).resolve().parents[1] / "src/claude_code_accounts/menubar.py"
    spec = importlib.util.spec_from_file_location("claude_code_accounts._menu_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def app(menubar):
    app = menubar.ManagerApp.__new__(menubar.ManagerApp)
    app._snapshot = menubar.Snapshot()
    app._lock = threading.Lock()
    app._session_poll_lock = threading.Lock()
    app._fresh_sessions = None
    app._owner_prints = {}
    app._rules_stamp = 0.0
    app._menu_open = False
    app._rebuild_pending = False
    app._session_rows = {}
    app._tracker = SimpleNamespace(update_sessions=Mock(), focus=SimpleNamespace(session=None))
    app._repaint = Mock()
    return app


class Item:
    """A menu item, reduced to what the menu builder actually touches.

    The styled title is kept as the segments it was built from, because that
    is what says which chip a row carries, and an attributed string needs
    AppKit to exist.
    """

    def __init__(self, title, callback=None):
        self.title = title
        self.callback = callback
        self.rows = []
        self.segments = []
        self.state = 0
        self._menuitem = SimpleNamespace(
            setImage_=lambda image: None, setState_=self._state,
            setToolTip_=lambda text: None, setAttributedTitle_=lambda title: None)

    def _state(self, value):
        self.state = value

    def add(self, row):
        self.rows.append(row)

    def __getitem__(self, key):
        return next(row for row in self.rows if row.title == key)


SEPARATOR = Item("---")


@pytest.fixture
def rows(menubar, monkeypatch):
    """Build real menu rows without AppKit, keeping each row's segments."""
    monkeypatch.setattr(menubar, "rumps",
                        SimpleNamespace(MenuItem=Item, separator=SEPARATOR,
                                        quit_application=lambda sender=None: None))
    monkeypatch.setattr(menubar, "_apply_style",
                        lambda item, segments, **kw: setattr(item, "segments", list(segments)))
    monkeypatch.setattr(menubar, "_set_icon", lambda item, name: None)
    monkeypatch.setattr(menubar, "_rows_that_fit", lambda: 30)
    monkeypatch.setattr(menubar, "_styled",
                        lambda *args, **kw: SimpleNamespace(
                            size=lambda: SimpleNamespace(width=0.0)))
    monkeypatch.setattr(glyphs, "template", lambda provider, size=13.0: None)
    monkeypatch.setattr(oauth, "installed_browsers", lambda: [("Default browser", "")])
    return menubar


@pytest.fixture
def snap(rows, monkeypatch):
    """One account and one session per provider, as the menu sees them."""
    snapshot = rows.Snapshot()
    snapshot.accounts = [
        core.Account(name="fable", slot="/slots/fable", email="fable@example.com"),
        core.Account(name="cx", slot="/slots/cx", email="cx@example.com", provider="codex"),
    ]
    claude = sessions.Session(
        pid=21, config_dir="/dirs/fable", env_config_dir="/dirs/fable", term_id="T1",
        cwd="/repos/acme", kind="interactive", status="idle", term_program="Apple_Terminal",
        tty="ttys001", name="acme", name_source="user")
    codex = sessions.Session(
        pid=22, config_dir=CODEX_HOME, env_config_dir=CODEX_HOME, provider="codex",
        term_id="T2", cwd="/repos/acme", kind="bg", status="busy",
        term_program="Apple_Terminal", tty="ttys002", name="fixtures", name_source="user",
        context_tokens=20_000, context_window=258_400,
        spent=transcripts.Totals(20_000, 0, 250_000, 20_000, 12))
    snapshot.sessions = [claude, codex]
    snapshot.running_on = {"/dirs/fable": "fable", CODEX_HOME: "cx"}
    snapshot.rules = profiles.Rules(default_account="fable", codex_default_account="cx")
    monkeypatch.setattr(rows, "_CODEX_NAMES", {"cx"})
    return snapshot


@pytest.fixture
def picker(app):
    """An app with the state a row needs, and no rule change reaching disk."""
    app._signing_in = {}
    app._account_rows = {}
    app._pick_tabs = None
    app._flash = ("", "", 0.0)
    app._tracker.enabled = False
    app._tracker.focus = SimpleNamespace(session=None, exact=True)
    app._did = Mock()
    return app


def titles(item):
    return [row.title for row in item.rows]


def words(item):
    return "".join(text for text, *_ in item.segments)


@pytest.fixture
def budget_snap(snap, monkeypatch):
    """Three known windows leave no fetch note competing with the columns."""
    monkeypatch.setattr(core, "pref", lambda name, default=None: default)
    for acct in snap.accounts:
        acct.limits = [
            core.Limit("session", "5h", 0, None, span=18000),
            core.Limit("weekly_all", "7d", 0, None, span=604800),
            core.Limit("weekly_scoped", "spark" if acct.is_codex else "fable",
                       0, None, span=604800, scope="model"),
        ]
    snap.sessions = [replace(sess, name="Investigate the account routing rules",
                             context_pct=42, status="busy")
                     for sess in snap.sessions]
    return snap


def cells(segments):
    return sum(2 if tone == "glyph" else len(text) for text, tone, *_ in segments)


@pytest.mark.parametrize("width", [1080.0, 1728.0, 1440.0])
def test_menu_budget_leaves_room_for_the_gutters(menubar, width):
    assert menubar._menu_budget(width) == 0.6 * width - 52


def test_row_width_measures_the_styled_row(menubar, monkeypatch):
    segments = [("  ", "dim"), *menubar._chip("ryantrycallie", menubar.NAME_W)]
    styled = Mock(return_value=SimpleNamespace(
        size=lambda: SimpleNamespace(width=cells(segments) * 7.418)))
    monkeypatch.setattr(menubar, "_styled", styled)
    assert menubar._row_width(segments) == cells(segments) * 7.418
    styled.assert_called_once_with(segments)


def test_menu_budget_falls_back_to_the_main_screen(menubar, monkeypatch):
    screen = SimpleNamespace(frame=lambda: SimpleNamespace(size=SimpleNamespace(width=1080)))
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(
        NSScreen=SimpleNamespace(mainScreen=lambda: screen)))
    assert menubar._menu_budget() == 0.6 * 1080 - 52


@pytest.mark.parametrize("width", [None, "no screen width"])
def test_menu_budget_keeps_every_column_when_measurement_fails(menubar, width):
    assert menubar._menu_budget(width) == 1e9


@pytest.mark.parametrize("family", ["accounts", "sessions", "both"])
def test_layout_for_checks_the_widest_full_row(rows, picker, budget_snap, monkeypatch, family):
    monkeypatch.setattr(rows, "_row_width", cells)
    snap = budget_snap
    accts = snap.accounts if family != "sessions" else []
    live = snap.sessions if family != "accounts" else []
    segments = [rows.ManagerApp._account_segments(picker, acct, snap) for acct in accts]
    name = "fable" if accts else ""
    segments += [[("  ", "dim"), *rows._session_segments(sess, name)] for sess in live]
    widest = max(cells(row) for row in segments)
    # Measuring a new screen must not mistake yesterday's compact rows for full ones.
    monkeypatch.setattr(rows, "_LAYOUT", rows.COMPACT)
    assert rows._layout_for(widest, accts, live) is rows.FULL
    assert rows._layout_for(widest - 1, accts, live) is rows.COMPACT
    assert rows._LAYOUT is rows.COMPACT


def test_layout_for_an_empty_menu_is_full(menubar, monkeypatch):
    monkeypatch.setattr(menubar, "_row_width", cells)
    monkeypatch.setattr(menubar, "_LAYOUT", menubar.COMPACT)
    assert menubar._layout_for(0, [], []) is menubar.FULL
    assert menubar._LAYOUT is menubar.COMPACT


def test_layout_for_restores_the_layout_if_a_builder_fails(rows, snap, monkeypatch):
    monkeypatch.setattr(rows, "_row_width", cells)
    monkeypatch.setattr(rows, "_LAYOUT", rows.COMPACT)
    monkeypatch.setattr(rows, "_session_segments", Mock(side_effect=RuntimeError("no row")))
    with pytest.raises(RuntimeError, match="no row"):
        rows._layout_for(83, [], snap.sessions)
    assert rows._LAYOUT is rows.COMPACT


@pytest.mark.parametrize("when", ["unused", "45m", "4h", "34h", "3d", ""])
def test_compact_buckets_keep_their_columns(rows, monkeypatch, when):
    monkeypatch.setattr(rows, "_LAYOUT", rows.COMPACT)
    monkeypatch.setattr(rows, "_compact_reset", lambda iso: when)
    monkeypatch.setattr(rows, "_gauge", Mock(side_effect=AssertionError("compact gauge")))
    lim = core.Limit("session", "5h", 42, None)
    normal = rows._bucket("5h", lim)
    assert normal[0] == ("     5h ", "dim")
    assert normal[1] == (" 42%", "ok")
    reset = "new" if when == "unused" else when
    assert normal[-1][0] == f" {reset:<3}"
    assert len(normal[-1][0][1:]) == rows.COMPACT.reset_w
    variants = [normal, rows._bucket("5h", lim, show_reset=False),
                rows._blank_bucket("5h"), rows._bucket("5h", None)]
    assert {sum(len(text) for text, *_ in row) for row in variants} == {16}
    assert all("[" not in text and "]" not in text for row in variants for text, *_ in row)


def test_compact_bucket_text_fits_sixteen_cells(rows, monkeypatch):
    monkeypatch.setattr(rows, "_LAYOUT", rows.COMPACT)
    unused = core.Limit("session", "5h", 0, None)
    assert "".join(text for text, *_ in rows._bucket("5h", unused)) == "     5h   0% new"
    assert "".join(text for text, *_ in rows._bucket("5h", None)) == "     5h    -    "
    assert "".join(text for text, *_ in rows._blank_bucket("5h")) == "     5h none    "
    monkeypatch.setattr(rows, "_compact_reset", lambda iso: "2h")
    lim = core.Limit("session", "5h", 78, None)
    assert "".join(text for text, *_ in rows._bucket("5h", lim)) == "     5h  78% 2h "
    lim.percent = 100
    assert "".join(text for text, *_ in rows._bucket("5h", lim)) == "     5h 100% 2h "


def test_compact_account_row_leaves_four_cells_of_headroom(rows, budget_snap, monkeypatch):
    monkeypatch.setattr(rows, "_LAYOUT", rows.COMPACT)
    acct = replace(budget_snap.accounts[0], name="ryantrycallie")
    row = [("  ", "dim"), *rows._chip(acct.name, rows.NAME_W),
           ("  ", "dim"), *rows._lamps([]), *rows._account_buckets(acct)]
    assert cells(row) == 73
    assert cells(row) <= 76


def test_compact_session_row_leaves_four_cells_of_headroom(rows, budget_snap, monkeypatch):
    monkeypatch.setattr(rows, "_LAYOUT", rows.COMPACT)
    sess = replace(budget_snap.sessions[0], cwd="/repos/twelve-chars")
    assert len(sess.repo) == 12
    assert len(sess.detail) > rows.COMPACT.detail_w
    row = [("  ", "dim"), *rows._session_segments(sess, "ryantrycallie")]
    assert cells(row) == 74
    assert cells(row) <= 76


def test_full_bucket_keeps_its_original_segments(rows, monkeypatch):
    monkeypatch.setattr(rows, "_LAYOUT", rows.FULL)
    lim = core.Limit("session", "5h", 0, None)
    assert rows._bucket("5h", lim) == [
        ("     5h ", "dim"), ("[", "dim"), ("", "ok"),
        (rows.TICK * 5, "dim"), ("]", "dim"), ("   0%", "ok"), (" (unused)", "dim")]
    assert rows._blank_bucket("5h") == [
        ("     5h ", "dim"), (" " * 7, "dim"), (" none", "dim"), (" " * 9, "dim")]


def test_compact_session_keeps_context_status_and_age(rows, budget_snap, monkeypatch):
    sess = budget_snap.sessions[1]
    full = rows._session_segments(sess, "cx")
    detail = sess.detail or sess.label
    assert (rows._fit(detail, 35), "text") in full
    assert rows._spent_cell(sess) in full
    monkeypatch.setattr(rows, "_LAYOUT", rows.COMPACT)
    compact = rows._session_segments(sess, "cx")
    assert (rows._fit(detail, 16), "text") in compact
    assert rows._spent_cell(sess) not in compact
    assert "tok" not in "".join(text for text, *_ in compact)
    assert "ctx " not in "".join(text for text, *_ in compact)
    assert compact[-2] == ("  busy ", rows._status_tone(sess.status, sess.logged_out))
    assert compact[-1] == full[-1]
    context = rows._context_bar(sess, named=False)
    assert compact[-2 - len(context):-2] == context


@pytest.mark.parametrize("status_screen", [1080, 1728, None, "missing"])
def test_rebuild_uses_the_status_items_screen(rows, picker, budget_snap, monkeypatch,
                                            status_screen):
    picker._snapshot = budget_snap
    picker.menu = Menu()
    picker._refresh_item = None
    screen = (SimpleNamespace(frame=lambda: SimpleNamespace(
        size=SimpleNamespace(width=status_screen))) if isinstance(status_screen, int) else None)
    if status_screen != "missing":
        picker._nsapp = SimpleNamespace(nsstatusitem=SimpleNamespace(button=lambda:
            SimpleNamespace(window=lambda: SimpleNamespace(screen=lambda: screen))))
    monkeypatch.setattr(rows, "_LAYOUT", rows.FULL)
    monkeypatch.setattr(rows, "_row_width", lambda segments: cells(segments) * 7.418)
    budget = Mock(return_value=596 if status_screen == 1080 else 1e9)
    monkeypatch.setattr(rows, "_menu_budget", budget)
    rows.ManagerApp._rebuild(picker)
    budget.assert_called_once_with(status_screen if isinstance(status_screen, int) else None)
    assert rows._LAYOUT is (rows.COMPACT if status_screen == 1080 else rows.FULL)
    if status_screen == 1080:
        assert all("tok" not in words(item) for item, _ in picker._session_rows.values())
        widths = {pid: len(words(item)) for pid, (item, _) in picker._session_rows.items()}
        # A poll paints fresh readings into the open menu, using its existing columns.
        budget.reset_mock()
        budget.return_value = 1e9
        picker._menu_open = True
        picker._fresh_sessions = (
            [replace(sess, context_pct=85, status="idle") for sess in budget_snap.sessions],
            budget_snap.running_on)
        rows.ManagerApp._take_sessions(picker)
        rows.ManagerApp._repaint(picker)
        budget.assert_not_called()
        assert rows._LAYOUT is rows.COMPACT
        assert {pid: len(words(item)) for pid, (item, _) in picker._session_rows.items()} == widths


def test_codex_session_row_reads_the_codex_rules(rows, picker, snap):
    snap.rules.codex_sessions["T2"] = "cx-night"
    item = rows.ManagerApp._session_item(picker, snap.sessions[1], snap)
    said = titles(item)
    # A `codex exec` run cannot be resumed, so it is never asked to restart.
    assert "This run keeps the login it started with. The next run follows the rule." in said
    assert not any("codex resume" in line or "claude -c" in line for line in said)
    assert "restart:22" not in said and "tab:22" in said
    # Only Codex accounts, and the pin it already has can be dropped.
    assert [line for line in said if line.startswith("session:T2:")] == ["session:T2:cx"]
    assert "clear:session:T2:codex" in said


def test_interactive_codex_row_restarts_in_its_own_tab(rows, picker, snap, monkeypatch):
    """A Codex TUI is quit and resumed in place, on the thread it already holds."""
    sess = replace(snap.sessions[1], pid=24, kind="interactive", status="idle",
                   session_id=THREAD)
    snap.sessions = [snap.sessions[0], sess]
    snap.rules.codex_sessions["T2"] = "cx-night"
    item = rows.ManagerApp._session_item(picker, sess, snap)
    said = titles(item)
    assert "Codex reads its login when it starts, so restart it in this tab:" in said
    assert "press ctrl+C, then run  codex resume --last" in said
    assert said.index("restart:24") < said.index("tab:24")
    assert words(item["restart:24"]).strip() == "Restart Codex in that tab now"

    calls = []
    monkeypatch.setattr(focus, "restart_codex", lambda *args: calls.append(args) or "")
    monkeypatch.setattr(rows.threading, "Thread",
                        lambda target, args, daemon: SimpleNamespace(
                            start=lambda: target(*args)))
    picker._done, picker._lock = [], threading.Lock()
    picker._report = Mock()
    item["restart:24"].callback(None)
    assert calls == [("com.apple.Terminal", "ttys002", THREAD, False)]
    for follow_up in picker._done:
        follow_up()
    assert picker._report.call_args_list[-1].args == (
        True, "fixtures: Codex is starting again on that thread")


def test_a_restart_that_fails_interrupts(rows, picker, monkeypatch, snap):
    sess = replace(snap.sessions[1], kind="interactive", session_id=THREAD)
    picker._notify = Mock()
    rows.ManagerApp._restarted(picker, sess, "that tab is gone")
    assert picker._notify.call_args.args == (
        "Could not restart Codex in that tab.\n\nthat tab is gone",)


def test_claude_session_row_keeps_its_own_restart_line(rows, picker, snap):
    snap.rules.sessions["T1"] = "sonnet"
    item = rows.ManagerApp._session_item(picker, snap.sessions[0], snap)
    said = titles(item)
    assert "press ctrl+C twice, then run  claude -c" in said
    assert not any("codex resume" in line for line in said)
    assert [line for line in said if line.startswith("session:T1:")] == ["session:T1:fable"]


def test_logged_out_claude_row_says_so_instead_of_promising_a_switch(rows, picker, snap):
    """It holds a working login and cannot see it, so the row says what to do."""
    sess = replace(snap.sessions[0], logged_out=True)
    snap.rules.sessions["T1"] = "sonnet"
    item = rows.ManagerApp._session_item(picker, sess, snap)
    said = titles(item)
    assert words(item["out:21"]).strip() == (
        "Logged out. It holds a working login now, but stopped looking: "
        "run /login in that tab, or ctrl+C twice and  claude -c")
    assert said.index("out:21") < said.index("tab:21")
    assert said.count("tab:21") == 1
    assert "press ctrl+C twice, then run  claude -c" not in said
    # Neither working nor waiting, and the one state here worth a colour.
    assert ("    out  ", "hot") in rows._session_segments(sess, "fable")


def test_a_logged_out_session_without_a_login_is_sent_to_its_account(rows, picker, snap):
    sess = replace(snap.sessions[0], env_config_dir="/dirs/gone", logged_out=True)
    item = rows.ManagerApp._session_item(picker, sess, snap)
    assert words(item["out:21"]).strip() == (
        "Logged out, and its account has no login. Sign the account in from its row above.")


def test_an_ordinary_claude_row_is_untouched(rows, picker, snap):
    sess = snap.sessions[0]
    item = rows.ManagerApp._session_item(picker, sess, snap)
    assert "out:21" not in titles(item)
    assert ("    idle ", "dim") in rows._session_segments(sess, "fable")


def test_credential_tick_after_a_sleep_runs_the_wake_burst(rows, app, monkeypatch):
    """A gap longer than two intervals is a wake, notification or not."""
    order, armed = [], []
    monkeypatch.setattr(core, "refresh_slots", lambda: order.append("refresh") or [])
    monkeypatch.setattr(core, "sync_credentials", lambda live: order.append("sync") or [])
    monkeypatch.setattr(core, "sync_answers", lambda: order.append("answers") or [])
    monkeypatch.setattr(rows, "threading", SimpleNamespace(
        Thread=lambda target, daemon: SimpleNamespace(start=target),
        Timer=lambda delay, fn: armed.append((delay, fn)) or SimpleNamespace(
            start=lambda: None)))
    app._syncing = False
    app._done = []
    app._last_tick = time.time() - 200
    rows.ManagerApp._on_credential_tick(app, None)
    # Rotate first, hand out second: the other order strands the copies.
    assert order == ["refresh", "sync"]
    assert [delay for delay, _ in armed] == list(rows.WAKE_BURST) == [5.0, 10.0, 20.0]
    # The usage numbers are as old as the sleep, so a forced refresh follows.
    assert len(app._done) == 1
    # Each burst step is the same pass, and one already running is not doubled.
    order.clear()
    armed[0][1]()
    assert order == ["refresh", "sync"]
    app._syncing = True
    order.clear()
    armed[1][1]()
    assert order == []


def test_the_sleep_notification_rotates_everything_with_hours_left(rows, app, monkeypatch):
    """A Mac that sleeps on fresh tokens cannot wake past their expiry."""
    asked = []
    monkeypatch.setattr(core, "refresh_slots", lambda ahead: asked.append(ahead) or [])
    monkeypatch.setattr(rows, "threading", SimpleNamespace(
        Thread=lambda target, daemon: SimpleNamespace(start=target)))
    rows.ManagerApp._on_sleep(app)
    assert asked == [7 * 3600] == [rows.SLEEP_AHEAD]


def test_quit_gives_the_refresh_tokens_back_first(rows, picker, snap, monkeypatch):
    """Nothing else refreshes these logins while the app runs, so it undoes that."""
    picker._snapshot = snap
    picker.menu = Menu()
    picker._sessions_heading = "RUNNING SESSIONS"
    picker._refresh_item = None
    monkeypatch.setattr(core, "pref", lambda name, default=None: default)
    order = []
    monkeypatch.setattr(core, "hand_back_refresh_tokens",
                        lambda: order.append("handback") or [])
    monkeypatch.setattr(rows.rumps, "quit_application",
                        lambda sender=None: order.append("quit"))
    rows.ManagerApp._rebuild(picker)
    picker.menu["Quit"].callback(None)
    assert order == ["handback", "quit"]


def test_an_ordinary_credential_tick_syncs_the_answers_too(rows, app, monkeypatch):
    order, armed = [], []
    monkeypatch.setattr(core, "refresh_slots", lambda: order.append("refresh") or [])
    monkeypatch.setattr(core, "sync_credentials", lambda live: order.append("sync") or [])
    monkeypatch.setattr(core, "sync_answers", lambda: order.append("answers") or [])
    monkeypatch.setattr(rows, "threading", SimpleNamespace(
        Thread=lambda target, daemon: SimpleNamespace(start=target),
        Timer=lambda delay, fn: armed.append(delay) or SimpleNamespace(start=lambda: None)))
    app._syncing = False
    app._done = []
    app._last_tick = time.time() - 45
    rows.ManagerApp._on_credential_tick(app, None)
    assert order == ["refresh", "sync", "answers"]
    assert armed == [] and app._done == []


def test_fresh_codex_row_draws_an_empty_bar(rows, snap):
    """A TUI that has not taken a turn yet reports no context and no spend."""
    fresh = sessions.Session(pid=23, config_dir=CODEX_HOME, env_config_dir=CODEX_HOME,
                             provider="codex", term_id="T3", cwd="/repos/acme",
                             kind="interactive", status="idle")
    drawn = "".join(text for text, *_ in rows._session_segments(fresh, "cx"))
    assert "[  -   ]" in drawn and "tok" not in drawn
    spent = "".join(text for text, *_ in [rows._spent_cell(snap.sessions[1])])
    assert spent.strip() == "290K tok"


def test_scope_rows_list_both_providers_under_one_heading(rows, picker, snap, monkeypatch):
    built = rows.ManagerApp._scope_rows(
        picker, "Use for every project with no rule", "default", "", snap,
        current={"claude": "fable", "codex": "cx"}, cwd="",
        clearable={"claude", "codex"})
    assert [row.title for row in built] == [
        "sc:default:", "default::fable", "sc:default::codex", "default::cx",
        "clear:default::claude", "clear:default::codex"]
    assert [row.state for row in built[1:4:2]] == [1, 1]
    assert words(built[2]).strip() == "Codex"
    assert words(built[4]).strip() == "Remove this rule"
    assert words(built[5]).strip() == "Remove the Codex rule"
    cleared = []
    monkeypatch.setattr(core, "clear",
                        lambda *args, **kw: (cleared.append(kw["provider"]), (True, "done"))[1])
    for row in built[4:]:
        row.callback(None)
    assert cleared == ["claude", "codex"]


def test_scope_rows_for_one_provider_list_only_its_accounts(rows, picker, snap):
    built = rows.ManagerApp._scope_rows(
        picker, "Use for this session", "session", "T2", snap,
        current="cx", cwd="/repos/acme", clearable=False, provider="codex")
    assert [row.title for row in built] == ["sc:session:T2", "session:T2:cx"]
    assert built[1].state == 1


def test_profile_and_default_rows_show_the_codex_account(rows, picker, snap):
    prof = profiles.Profile("work", "fable", ["~/repos/acme"], codex_account="cx")
    snap.rules.profiles = [prof]
    row = words(rows.ManagerApp._profile_item(picker, prof, snap))
    assert row.index("cx ") > row.index("project")
    assert "fable" in row
    assert words(rows.ManagerApp._default_item(picker, snap)).count("cx ") == 1


def test_profile_row_leads_with_codex_when_it_is_the_only_account(rows, picker, snap):
    prof = profiles.Profile("codex-only", "", ["~/repos/acme"], codex_account="cx")
    snap.rules.profiles = [prof]
    row = words(rows.ManagerApp._profile_item(picker, prof, snap))
    assert "unassigned" not in row and row.index("cx ") < row.index("codex-only")


def test_codex_account_submenu_lists_its_sessions(rows, picker, snap):
    item = rows.ManagerApp._account_item(picker, snap.accounts[1], snap)
    said = titles(item)
    assert "runhead:acct:cx" not in said
    assert "lg:run:acct:cx" in said and "run:acct:cx:22" in said
    assert "run:acct:cx:21" not in said


def test_codex_account_row_offers_to_start_its_stopped_windows(rows, picker, snap):
    """The general windows only: the model-scoped one has no model to ask for."""
    acct = snap.accounts[1]
    acct.limits = [
        core.Limit(kind="weekly_all", label="7d", percent=0, resets_at=None, span=604800),
        core.Limit(kind="scoped_weekly", label="spark", percent=0, resets_at=None,
                   span=604800, scope="spark"),
    ]
    item = rows.ManagerApp._account_item(picker, acct, snap)
    assert words(item["poke:cx"]).strip() == "Start the 7d window now"


def test_rebuilt_menu_counts_sessions_of_both_tools(rows, picker, snap, monkeypatch):
    picker._snapshot = snap
    picker.menu = Menu()
    picker._sessions_heading = "RUNNING SESSIONS"
    picker._refresh_item = None
    monkeypatch.setattr(core, "pref", lambda name, default=None: default)
    rows.ManagerApp._rebuild(picker)
    assert "RUNNING SESSIONS · 2" in picker.menu
    assert set(picker._session_rows) == {21, 22}
    assert set(picker._account_rows) == {"fable", "cx"}


@pytest.mark.parametrize("width", [0, 40, 80.5, 81])
def test_picker_tabs_keep_baseline_for_narrow_chips(menubar, width):
    assert menubar._picker_tabs(width) == menubar.PICK_TABS


@pytest.mark.parametrize("width", [81.5, 108, 140, 250])
def test_picker_tabs_shift_every_stop_for_wide_chips(menubar, width):
    stops = menubar._picker_tabs(width)
    assert [how for how, _ in stops] == [how for how, _ in menubar.PICK_TABS]
    shifts = [where - base for (_, where), (_, base) in zip(stops, menubar.PICK_TABS, strict=True)]
    assert shifts == [width - 81] * len(stops)


def test_live_pids_reads_only_valid_live_registry_pids(menubar, monkeypatch, tmp_path):
    dirs = [tmp_path / "first", tmp_path / "second"]
    for directory in dirs:
        (directory / "sessions").mkdir(parents=True)
    for i, data in enumerate([{"pid": 21}, {"pid": 22}, {"pid": "23"}, {}, [], None]):
        (dirs[0] / "sessions" / f"{i}.json").write_text(json.dumps(data))
    (dirs[0] / "sessions/broken.json").write_text("{")
    (dirs[1] / "sessions/duplicate.json").write_text('{"pid": 21}')
    (dirs[1] / "sessions/other.json").write_text('{"pid": 24}')
    monkeypatch.setattr(core, "credential_dirs", lambda: dirs)
    monkeypatch.setattr(sessions, "alive", lambda pid: pid in {21, 24})
    monkeypatch.setattr(sessions, "live", Mock(side_effect=AssertionError("full scan")))
    monkeypatch.setattr(sessions, "_environ", Mock(side_effect=AssertionError("environment")))
    assert menubar._live_pids() == {21, 24}


def test_menu_open_keeps_unchanged_sessions_fast(menubar, app, monkeypatch):
    app._snapshot.sessions = [sessions.Session(pid=21, config_dir="")]
    monkeypatch.setattr(menubar, "_live_pids", lambda: {21})
    app._poll_sessions = Mock(side_effect=AssertionError("unexpected poll"))
    app._rebuild = Mock(side_effect=AssertionError("unexpected rebuild"))
    app._on_menu_open()
    assert app._menu_open
    app._repaint.assert_called_once()


@pytest.mark.parametrize("pids", [[22, 21], []])
def test_menu_open_polls_changed_sessions_before_drawing(menubar, app, monkeypatch, pids):
    app._snapshot.sessions = [sessions.Session(pid=21, config_dir="")]
    live = [sessions.Session(pid=pid, config_dir="") for pid in pids]
    monkeypatch.setattr(menubar, "_live_pids", lambda: set(pids))
    monkeypatch.setattr(core, "credential_dirs", lambda: [])
    scan = Mock(return_value=live)
    monkeypatch.setattr(sessions, "live", scan)
    monkeypatch.setattr(core, "owners_now", lambda *args, **kwargs: ({}, {}))
    drawn = []
    app._rebuild = lambda: drawn.append((app._menu_open, list(app._snapshot.sessions)))
    app._on_menu_open()
    scan.assert_called_once_with([], with_git=True, with_transcript=True)
    assert drawn == [(False, live)]
    assert app._menu_open
    app._repaint.assert_called_once()


def test_menu_open_applies_pending_rebuild(menubar, app, monkeypatch):
    monkeypatch.setattr(menubar, "_live_pids", set)
    app._rebuild_pending = True
    drawn = []
    app._rebuild = lambda: drawn.append(app._menu_open)
    app._on_menu_open()
    assert drawn == [False]


def test_menu_open_still_protects_rows_when_poll_fails(menubar, app, monkeypatch):
    monkeypatch.setattr(menubar, "_live_pids", lambda: {21})
    app._poll_sessions = Mock(side_effect=OSError("registry changed"))
    app._on_menu_open()
    assert app._menu_open
    app._repaint.assert_called_once()


class Menu(dict):
    def add(self, item):
        # A separator has no title of its own, and every one of them has to
        # keep its place, so it is keyed by where it landed.
        self[getattr(item, "title", None) or f"sep{len(self)}"] = item

    def insert_after(self, key, item):
        pairs = list(self.items())
        index = list(self).index(key) + 1
        pairs.insert(index, (item.title, item))
        self.clear()
        self.update(pairs)


@pytest.mark.parametrize(("old", "live", "expected"), [
    ([30, 10], [40, 30, 20, 10, 5], [40, 30, 20, 10, 5]),
    ([], [30, 20], [30, 20]),
    ([30, 20, 10], [40, 30, 10], [40, 30, 20, 10]),
])
def test_open_menu_inserts_sessions_and_defers_removals(
        menubar, app, monkeypatch, old, live, expected):
    app._menu_open = True
    app._sessions_heading = "RUNNING SESSIONS"
    app.menu = Menu({app._sessions_heading: object()})
    built = []

    def build(sess, snap):
        item = SimpleNamespace(title=str(sess.pid), submenu=object(), callback=object())
        app._session_rows[sess.pid] = (item, [])
        built.append(sess.pid)
        return item

    app._session_item = build
    app._snapshot.sessions = [sessions.Session(pid=pid, config_dir="") for pid in old]
    for sess in app._snapshot.sessions:
        item = build(sess, app._snapshot)
        app.menu[item.title] = item
    originals = dict(app.menu)
    if not old:
        app.menu["  none"] = object()
    app.menu["PROFILES"] = object()
    built.clear()
    monkeypatch.setattr(menubar, "_apply_style", Mock())
    monkeypatch.setattr(menubar, "_session_segments", lambda *args: [])
    app._fresh_sessions = ([sessions.Session(pid=pid, config_dir="") for pid in live], {})
    app._take_sessions()
    assert list(app.menu) == [app._sessions_heading, *map(str, expected), "PROFILES"]
    assert built == [pid for pid in live if pid not in old]
    assert all(app.menu[key] is item for key, item in originals.items())
    assert all(app.menu[str(pid)] is app._session_rows[pid][0] for pid in live)
    assert app._rebuild_pending
    app._rebuild = Mock()
    app._alerts = []
    app._on_menu_close()
    app._rebuild.assert_called_once()
    assert not app._menu_open


def test_tab_style_clips_picker_and_spec_rows(menubar, monkeypatch):
    para = Mock()
    appkit = SimpleNamespace(
        NSMutableParagraphStyle=Mock(), NSTextTab=Mock(), NSLineBreakByClipping=2,
        NSTextAlignmentRight=1, NSTextAlignmentLeft=0)
    appkit.NSMutableParagraphStyle.alloc.return_value.init.return_value = para
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    for tabs in (menubar._picker_tabs(140), menubar.SPEC_TABS):
        menubar._tab_style(tabs)
        para.setLineBreakMode_.assert_called_with(appkit.NSLineBreakByClipping)
        para.setFirstLineHeadIndent_.assert_called_with(16.0)
        para.setHeadIndent_.assert_called_with(16.0)
        assert len(para.setTabStops_.call_args.args[0]) == len(tabs)
