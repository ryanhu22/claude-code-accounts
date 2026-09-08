"""Automatic start sends only due weekly requests and remembers each attempt."""

import io
import threading
import urllib.error
from dataclasses import replace
from types import SimpleNamespace

import pytest

from claude_code_accounts import cli, core, keychain
from fakes import sign_in

pytestmark = pytest.mark.usefixtures("fake_keychain", "fake_api", "no_git")

RUNNING = "2100-01-03T00:00:00Z"


def payload(session=RUNNING, weekly=None, fable=RUNNING):
    return {"limits": [
        {"kind": "session", "percent": 0, "resets_at": session},
        {"kind": "weekly_all", "percent": 0, "resets_at": weekly},
        {"kind": "weekly_scoped", "percent": 0, "resets_at": fable,
         "scope": {"model": {"display_name": "Fable"}}},
    ]}


def known(name, api, data=None):
    email = f"{name}@example.com"
    slot = sign_in(name, email, api)
    core.identity(slot, keychain.read_credentials(slot))
    api.usage[email] = data if data is not None else payload()
    return core.load_account(name)


@pytest.fixture
def requests(monkeypatch, fake_api):
    recorder = SimpleNamespace(calls=[], fail=False)

    def one_poke(blob, model):
        recorder.calls.append((fake_api.emails[blob["accessToken"]], model))
        if recorder.fail:
            raise urllib.error.HTTPError(
                core.API + "/v1/messages", 429, "Too Many Requests", {},
                io.BytesIO(b'{"error":{"message":"busy"}}'))

    monkeypatch.setattr(core, "_one_poke", one_poke)
    return recorder


@pytest.mark.parametrize("data,weekly_only,models,message", [
    (payload(), True, [core.POKE_MODEL], "1 window group(s) started"),
    (payload(weekly=RUNNING, fable=None), True,
     [core.POKE_MODEL_SCOPED["fable"]], "1 window group(s) started"),
    (payload(fable=None), True,
     [core.POKE_MODEL_SCOPED["fable"]], "1 window group(s) started"),
    (payload(session=None, weekly=RUNNING), True, [],
     "every weekly window is already running"),
    (payload(session=None, weekly=RUNNING), False,
     [core.POKE_MODEL], "1 window group(s) started"),
])
def test_poke_weekly_model_selection(fake_api, requests, data, weekly_only, models, message):
    known("a", fake_api, data)
    if weekly_only:
        result = core.poke("a", weekly_only=True)
    else:
        result = core.poke("a")
    assert result == (True, message)
    assert requests.calls == [("a@example.com", model) for model in models]


def test_stopped_weekly_ignores_ineligible_accounts(fake_api):
    acct = known("a", fake_api, payload(session=None, fable=None))
    assert [lim.label for lim in core.stopped_weekly(acct)] == ["7d", "fable"]
    assert core.stopped_weekly(replace(acct, provider="codex")) == []
    assert core.stopped_weekly(replace(acct, email=None)) == []
    assert core.stopped_weekly(replace(acct, limits=[])) == []
    running = known("b", fake_api, payload(weekly=RUNNING))
    assert core.stopped_weekly(running) == []


def test_auto_start_due_obeys_hourly_cap(fake_api):
    acct = known("a", fake_api)
    running = known("b", fake_api, payload(weekly=RUNNING))
    now = 10000.0
    attempts = {"a": now - core.AUTO_START_RETRY + 1}
    assert core.auto_start_due(iter([acct, running]), now, attempts) == []
    assert attempts == {"a": now - core.AUTO_START_RETRY + 1}
    assert core.auto_start_due([acct, running], now + 1, attempts) == ["a"]
    assert core.auto_start_due([acct, running], now, {}) == ["a"]


def test_auto_start_defaults_off(fake_api, requests):
    acct = known("a", fake_api)
    assert core.auto_start([acct]) == []
    assert requests.calls == []
    assert core.auto_start_attempts() == {}


def test_auto_start_remembers_success_and_allows_usage_fetch(fake_api, requests, monkeypatch):
    now = core.time.time()
    monkeypatch.setattr(core.time, "time", lambda: now)
    due = known("a", fake_api)
    recent = known("b", fake_api)
    running = known("c", fake_api, payload(weekly=RUNNING))
    core.note_auto_start("b", now - 1)
    core.set_pref(core.AUTO_START_PREF, True)

    assert core.auto_start(iter([due, recent, running])) == [
        ("a", True, "1 window group(s) started")]
    assert requests.calls == [("a@example.com", core.POKE_MODEL)]
    assert core.auto_start_attempts() == {"a": now, "b": now - 1}
    assert core._cache_read()["a"]["tried_at"] == 0
    assert core.auto_start([due]) == []

    fetches = fake_api.usage_calls
    core.load_account("a", force=True)
    assert fake_api.usage_calls == fetches + 1
    assert core.auto_start_attempts()["a"] == now
    assert core.auto_start([due]) == []
    assert len(requests.calls) == 1


def test_auto_start_retries_failure_after_one_hour(fake_api, requests, monkeypatch):
    now = core.time.time()
    monkeypatch.setattr(core.time, "time", lambda: now)
    acct = known("a", fake_api)
    core.set_pref(core.AUTO_START_PREF, True)
    requests.fail = True

    results = core.auto_start([acct])
    assert len(results) == 1
    name, ok, message = results[0]
    assert name == "a" and not ok and "busy" in message
    assert core.auto_start_attempts() == {"a": now}
    assert core.auto_start([acct]) == []
    now += core.AUTO_START_RETRY - 1
    assert core.auto_start([acct]) == []
    assert len(requests.calls) == 1

    now += 1
    requests.fail = False
    assert core.auto_start([acct]) == [("a", True, "1 window group(s) started")]
    assert len(requests.calls) == 2
    assert core.auto_start_attempts() == {"a": now}


def test_auto_start_records_before_request_and_continues_after_exception(fake_api, monkeypatch):
    accounts = [known(name, fake_api) for name in ("a", "b")]
    core.set_pref(core.AUTO_START_PREF, True)

    def poke(name, *, weekly_only=False):
        assert weekly_only
        assert name in core.auto_start_attempts()
        if name == "a":
            raise RuntimeError("busy")
        return True, "1 window group(s) started"

    monkeypatch.setattr(core, "poke", poke)
    assert core.auto_start(accounts) == [
        ("a", False, "busy"), ("b", True, "1 window group(s) started")]


def test_auto_start_handles_attempt_read_failure(fake_api, requests, monkeypatch):
    acct = known("a", fake_api)
    core.set_pref(core.AUTO_START_PREF, True)

    def fail():
        raise OSError("cannot read attempts")

    monkeypatch.setattr(core, "auto_start_attempts", fail)
    assert core.auto_start([acct]) == []
    assert requests.calls == []


def test_note_auto_start_preserves_cache_and_survives_restart():
    entry = {"data": payload(), "at": 100.0, "who": "a@example.com",
             "retry_after": 300.0, "tried_at": 90.0}
    core._cache_write({"a": entry, "b": {"at": 50.0}})
    core.note_auto_start("a", 200.0)
    assert core._cache_read() == {
        "a": {**entry, "auto_start_at": 200.0}, "b": {"at": 50.0}}
    assert core.auto_start_attempts() == {"a": 200.0}
    core.note_auto_start("new", 250.0)
    assert core._cache_read()["new"] == {"auto_start_at": 250.0}
    assert core.auto_start_attempts() == {"a": 200.0, "new": 250.0}


def test_cli_auto_start(capsys):
    assert cli.main(["auto-start"]) == 0
    assert capsys.readouterr().out == (
        "automatic start of weekly windows is off\n"
        "It runs from the menu bar app; the CLI only sets the preference.\n")
    assert cli.main(["auto-start", "on"]) == 0
    assert capsys.readouterr().out == (
        "automatic start of weekly windows is on\n"
        "This sends about 22 input tokens per stopped window, "
        "at most once an hour per account.\n")
    assert core.pref(core.AUTO_START_PREF, False) is True
    assert cli.main(["auto-start"]) == 0
    assert capsys.readouterr().out == (
        "automatic start of weekly windows is on\n"
        "It runs from the menu bar app; the CLI only sets the preference.\n")
    assert cli.main(["auto-start", "off"]) == 0
    assert capsys.readouterr().out == "automatic start of weekly windows is off\n"
    assert core.pref(core.AUTO_START_PREF, False) is False


def test_cli_poke_weekly(monkeypatch, capsys):
    calls = []

    def poke(name, **kwargs):
        calls.append((name, kwargs))
        return True, "1 window group(s) started"

    monkeypatch.setattr(core, "poke", poke)
    assert cli.main(["poke", "a", "--weekly"]) == 0
    assert calls == [("a", {"weekly_only": True})]
    assert capsys.readouterr().out == "a: 1 window group(s) started\n"


@pytest.fixture
def menu_app(monkeypatch):
    from claude_code_accounts.menubar import ManagerApp

    app = SimpleNamespace(_busy=True, _again=False, _again_force=False,
                          _lock=threading.Lock(), _done=[], _pending=None)
    app._later = lambda fn: ManagerApp._later(app, fn)
    app.flashes, app.refreshes, app.delayed, app.rechecks = [], [], [], []
    app._report = lambda ok, message: app.flashes.append((ok, message))
    app._on_refresh_tick = lambda _, force=False: app.refreshes.append(force)
    app._poke_again = app.rechecks.append

    def timer(seconds, callback):
        return SimpleNamespace(start=lambda: app.delayed.append((seconds, callback)))

    monkeypatch.setattr(threading, "Timer", timer)
    return app


def test_worker_reports_on_main_thread_and_forces_followup(menu_app, monkeypatch):
    from claude_code_accounts.menubar import ManagerApp

    app = menu_app
    snapshot = SimpleNamespace(accounts=[])
    app._collect = lambda force: snapshot
    monkeypatch.setattr(core, "auto_start", lambda accts: [
        ("a", True, "1 window group(s) started"),
        ("b", False, "busy"),
        ("c", True, "2 window group(s) started"),
    ])
    ManagerApp._worker(app)
    assert app._pending is snapshot
    assert app.flashes == []
    assert app.refreshes == [True]
    assert not app._busy and not app._again and not app._again_force
    for callback in app._done:
        callback()
    assert app.flashes == [
        (True, "a: weekly windows started automatically"),
        (False, "b: could not start the weekly window. busy"),
        (True, "c: weekly windows started automatically"),
    ]
    app._done.clear()
    assert [seconds for seconds, _ in app.delayed] == [12.0, 12.0]
    for _, callback in app.delayed:
        callback()
    assert app.rechecks == []
    for callback in app._done:
        callback()
    assert app.rechecks == ["a", "c"]


def test_worker_keeps_snapshot_when_auto_start_raises(menu_app, monkeypatch):
    from claude_code_accounts.menubar import ManagerApp

    snapshot = SimpleNamespace(accounts=[])
    menu_app._collect = lambda force: snapshot

    def fail(accts):
        raise RuntimeError("busy")

    monkeypatch.setattr(core, "auto_start", fail)
    ManagerApp._worker(menu_app)
    assert menu_app._pending is snapshot
    assert not menu_app._busy
    assert menu_app.refreshes == []
    assert menu_app.delayed == []


def test_busy_refresh_remembers_force(menu_app):
    from claude_code_accounts.menubar import ManagerApp

    ManagerApp._on_refresh_tick(menu_app, None, force=True)
    ManagerApp._on_refresh_tick(menu_app, None)
    assert menu_app._again and menu_app._again_force


def test_menu_toggle_refreshes_only_when_enabled(menu_app):
    from claude_code_accounts.menubar import ManagerApp

    rebuilds = []
    menu_app._rebuild = lambda: rebuilds.append(True)
    ManagerApp._toggle_auto_start(menu_app, None)
    assert core.pref(core.AUTO_START_PREF, False) is True
    assert menu_app.refreshes == [True]
    ManagerApp._toggle_auto_start(menu_app, None)
    assert core.pref(core.AUTO_START_PREF, False) is False
    assert menu_app.refreshes == [True]
    assert len(rebuilds) == 2
