"""Check menu updates and column arithmetic without loading AppKit."""

import importlib.util
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from claude_code_accounts import core, sessions


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
