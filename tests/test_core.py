import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_code_manager import __version__, cli, codex, core


@pytest.fixture
def payload():
    return {"limits": [
        {"kind": "session", "percent": 58, "resets_at": "2100-01-01T00:00:00Z"},
        {"kind": "weekly_all", "percent": 71, "resets_at": "2100-01-03T00:00:00Z"},
        {"kind": "weekly_scoped", "percent": 34, "resets_at": "2100-01-03T00:00:00Z",
         "scope": {"model": {"display_name": "Fable"}}},
    ]}


def test_parse_limits(payload):
    limits = core._parse_limits(payload)
    assert [(lim.kind, lim.label, lim.percent, lim.span, lim.scope) for lim in limits] == [
        ("session", "5h", 58, 18000, ""), ("weekly_all", "7d", 71, 604800, ""),
        ("weekly_scoped", "fable", 34, 604800, "fable"),
    ]
    assert [lim.resets_at for lim in limits] == [x["resets_at"] for x in payload["limits"]]


def test_passed_reset(payload):
    payload["limits"][0]["resets_at"] = "2000-01-01T00:00:00Z"
    limit = core._parse_limits(payload)[0]
    assert limit.percent == 0
    assert limit.resets_at is None


def test_has_reading():
    assert not core.has_reading([])
    assert core.has_reading([core.Limit("session", "5h", 0, None)])


def test_forget_usage_preserves_cached_payload_and_retry(payload):
    entry = {"data": payload, "at": 1.0, "tried_at": 5.0,
             "retry_after": 99.0, "who": "x"}
    core._cache_write({"work": entry})

    core.forget_usage("work")

    assert core._cache_read() == {"work": {**entry, "tried_at": 0}}


def test_forget_usage_unknown_name_is_noop(monkeypatch, payload):
    store = {"work": {"data": payload, "at": 1.0, "tried_at": 5.0,
                      "retry_after": 99.0, "who": "x"}}
    core._cache_write(store)

    def unexpected_write(*args):
        pytest.fail("forget_usage must not write the cache for an unknown name")

    monkeypatch.setattr(core, "_cache_write", unexpected_write)
    core.forget_usage("missing")

    assert core._cache_read() == store


@pytest.mark.parametrize(("minutes", "expected"), [
    (-1, "now"), (0, "now"), (15, "15m"), (185, "3h 5m"), (4380, "3d 1h"),
])
def test_human_delta(monkeypatch, minutes, expected):
    now = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(core, "_dt", SimpleNamespace(datetime=Clock, timezone=dt.timezone))
    assert core.human_delta((now + dt.timedelta(minutes=minutes)).isoformat()) == expected


@pytest.mark.parametrize(("resets_at", "over", "spent"), [
    ("2000-01-01T00:00:00Z", True, 0), ("2100-01-01T00:00:00Z", False, 58),
    (None, False, 58), ("invalid", False, 58),
])
def test_limit_over_and_spent(resets_at, over, spent):
    limit = core.Limit("session", "5h", 58, resets_at)
    assert limit.over is over
    assert limit.spent == spent


@pytest.fixture
def accounts():
    for name in ("work", "workbench", "personal", "acme", "hobby"):
        Path(core.slot_dir(name)).mkdir(parents=True)


@pytest.mark.parametrize(("query", "expected"), [
    ("work", "work"), ("per", "personal"), ("son", "personal"), ("ACM", "acme"),
])
def test_resolve_account(accounts, query, expected):
    assert core.resolve_account(query) == expected


@pytest.mark.parametrize(("query", "message"), [("wo", "matches"), ("missing", "no account")])
def test_resolve_account_refuses(accounts, query, message):
    with pytest.raises(core.UnknownAccount, match=message):
        core.resolve_account(query)


def test_resolve_any(accounts):
    Path(codex.slot_dir("studio")).mkdir(parents=True)
    assert core.resolve_any("work") == ("claude", "work")
    assert core.resolve_any("stu") == ("codex", "studio")
    assert core.resolve_any("udio") == ("codex", "studio")
    Path(codex.slot_dir("work")).mkdir()
    with pytest.raises(core.UnknownAccount, match="matches"):
        core.resolve_any("work")
    with pytest.raises(core.UnknownAccount, match="no account"):
        core.resolve_any("missing")


def test_assign_refuses_codex(monkeypatch):
    Path(codex.slot_dir("hobby")).mkdir(parents=True)
    monkeypatch.setattr(core, "save_rules", lambda *args: None)
    monkeypatch.setattr(core, "carry_project_state", lambda *args: None)
    ok, message = core.assign("default", "", "hobby")
    assert not ok
    assert "Routing Codex accounts is not supported" in message


def test_preferences():
    assert core.pref("pinned", "none") == "none"
    core.set_pref("pinned", "work")
    core.set_pref("visible", False)
    assert core.pref("pinned") == "work"
    assert core.pref("visible") is False
    core.set_pref("pinned", "personal")
    assert core.pref("pinned") == "personal"


def test_chip_indices(monkeypatch):
    work = core.chip_index("work")
    assert core.chip_index("personal") != work
    assert core.chip_index("work") == work
    monkeypatch.setattr(core, "_CHIPS", (-1.0, {}))
    assert core.chip_index("work") == work
    core.set_chip_index("work", 6)
    assert core.chip_index("work") == 6
    assert core.chip_index("work", palette_size=4) == 2


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as result:
        cli.main(["--version"])
    assert result.value.code == 0
    assert capsys.readouterr().out == f"ccm {__version__}\n"
