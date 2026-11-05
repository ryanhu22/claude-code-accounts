"""Starting weekly windows from the cloud, through the menu's automatic start.

With automatic start on, each Claude account keeps one routine of ours on the
fake server, set for just after its next weekly reset, and the account's menu
says when that is. The server cannot delete a routine, so most of these are
about never ending up with two.
"""
import datetime as dt
import json
import os

from claude_code_accounts import core, routines
from e2e.menu_harness import click, text

TOGGLE = "Start weekly windows automatically"
EMAIL = "main@example.com"


def ours(fake_server, email=EMAIL):
    return [t for t in fake_server.claude[email].triggers.values()
            if t["name"] == routines.NAME]


def turn(menu):
    click(menu.find(TOGGLE))
    menu.settle()


def armed(menu):
    """The signed-in account, read once, then automatic start turned on."""
    menu.refresh()
    turn(menu)


def weekly_reset(fake_server, email=EMAIL) -> str:
    return next(lim["resets_at"] for lim in fake_server.claude[email].limits
                if lim["kind"] == "weekly_all")


def two_minutes_after(iso: str) -> str:
    at = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    at = (at + dt.timedelta(seconds=30)).replace(second=0, microsecond=0)
    return (at + dt.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")


def move_weekly_resets(fake_server, iso, name="main", email=EMAIL):
    for lim in fake_server.claude[email].limits:
        if lim["kind"] != "session":
            lim["resets_at"] = iso
    # The app reads usage every three minutes; a test cannot wait for that.
    core.forget_usage(name)


def test_turning_on_sets_one_routine_per_account(sandbox, fake_server, menu):
    sandbox.seed_claude("main", EMAIL)
    sandbox.seed_claude("side", "side@example.com")
    menu.refresh()
    assert fake_server.calls("/v1/code") == []
    turn(menu)
    for email in (EMAIL, "side@example.com"):
        [trigger] = ours(fake_server, email)
        assert trigger["enabled"] and trigger["run_once_at"] == "2100-01-03T00:02:00Z"
        ccr = trigger["job_config"]["ccr"]
        # The fake account has a Fable window, which only a Fable request starts.
        assert ccr["session_context"]["model"] == "claude-fable-5-1"
        assert ccr["environment_id"] == "env_default"
        assert ccr["events"][0]["data"]["message"]["content"] == routines.PROMPT
        assert trigger["mcp_connections"] == []
    assert "cloud start" in text(menu.find("cloud start", menu.account_row("main")))
    # Nothing moved, so another refresh leaves the server alone.
    sent = len(fake_server.calls("/v1/code"))
    menu.refresh()
    assert len(fake_server.calls("/v1/code")) == sent


def test_a_fired_routine_starts_the_week_and_moves_to_the_next_reset(sandbox, fake_server,
                                                                   menu):
    sandbox.seed_claude("main", EMAIL)
    armed(menu)
    [trigger] = ours(fake_server)
    # The week ends with the Mac asleep: nothing local sends a request.
    for lim in fake_server.claude[EMAIL].limits:
        if lim["kind"] != "session":
            lim["percent"], lim["resets_at"] = 0, None
    fake_server.fire(EMAIL, trigger["id"])
    assert all(lim["resets_at"] for lim in fake_server.claude[EMAIL].limits)
    assert fake_server.pokes == []
    core.forget_usage("main")
    menu.refresh()
    [moved] = ours(fake_server)
    assert moved["id"] == trigger["id"] and moved["enabled"]
    assert moved["run_once_at"] == two_minutes_after(weekly_reset(fake_server))


def test_a_routine_renamed_on_the_web_is_still_ours(sandbox, fake_server, menu):
    sandbox.seed_claude("main", EMAIL)
    armed(menu)
    [trigger] = ours(fake_server)
    trigger["name"] = "keep my windows going"
    move_weekly_resets(fake_server, "2100-01-10T00:00:00Z")
    menu.refresh()
    [only] = fake_server.claude[EMAIL].triggers.values()
    assert only["id"] == trigger["id"] and only["run_once_at"] == "2100-01-10T00:02:00Z"


def test_a_routine_is_found_by_name_when_its_id_is_lost(sandbox, fake_server, menu):
    sandbox.seed_claude("main", EMAIL)
    fake_server.trigger_page = 2
    for n in range(3):
        fake_server.claude[EMAIL].triggers[f"trig_000{n}"] = {
            "id": f"trig_000{n}", "name": f"someone else's routine {n}", "enabled": True}
    armed(menu)
    assert len(ours(fake_server)) == 1
    # Ours is on the second page, and nothing local remembers its id.
    os.remove(core.CLOUD_STARTS)
    move_weekly_resets(fake_server, "2100-01-10T00:00:00Z")
    menu.refresh()
    [trigger] = ours(fake_server)
    assert trigger["run_once_at"] == "2100-01-10T00:02:00Z"
    assert len(fake_server.claude[EMAIL].triggers) == 4


def test_a_create_whose_reply_was_lost_is_found_instead_of_made_again(
        sandbox, fake_server, menu, monkeypatch):
    sandbox.seed_claude("main", EMAIL)
    fake_server.lost_create_replies = 1
    armed(menu)
    # It exists but was never switched on, so it cannot run half set up.
    [trigger] = ours(fake_server)
    assert trigger["enabled"] is False
    assert "off" in text(menu.find("cloud start", menu.account_row("main")))
    monkeypatch.setattr(routines, "RETRY", 0.0)
    menu.refresh()
    [again] = ours(fake_server)
    assert again["id"] == trigger["id"] and again["enabled"]
    assert again["mcp_connections"] == []


def test_a_routine_whose_setup_failed_is_finished_later(sandbox, fake_server, menu,
                                                         monkeypatch):
    sandbox.seed_claude("main", EMAIL)
    # The create works and the next edit, clearing its connectors, fails.
    fake_server.script("/v1/code/triggers/", 500, {"error": "boom"}, method="POST")
    armed(menu)
    [trigger] = ours(fake_server)
    assert trigger["enabled"] is False and trigger["mcp_connections"]
    monkeypatch.setattr(routines, "RETRY", 0.0)
    menu.refresh()
    [done] = ours(fake_server)
    assert done["id"] == trigger["id"]
    assert done["enabled"] and done["mcp_connections"] == []


def test_an_account_with_no_cloud_environment_says_so(sandbox, fake_server, menu):
    sandbox.seed_claude("main", EMAIL, environments=[])
    armed(menu)
    assert ours(fake_server) == []
    row = menu.find("cloud start", menu.account_row("main"))
    assert "off" in text(row) and "no cloud environment on this account" in text(row)
    # Asked again an hour later, not on every refresh.
    asked = len(fake_server.calls("/v1/environment_providers"))
    menu.refresh()
    assert len(fake_server.calls("/v1/environment_providers")) == asked


def test_turning_off_switches_the_routines_off(sandbox, fake_server, menu):
    sandbox.seed_claude("main", EMAIL)
    armed(menu)
    turn(menu)
    assert core.pref(core.AUTO_START_PREF) is False
    [trigger] = ours(fake_server)
    assert trigger["enabled"] is False
    assert "cloud start" not in " ".join(menu.texts(menu.account_row("main")))
    # Off is remembered, so refreshes while it is off send nothing.
    sent = len(fake_server.calls("/v1/code"))
    menu.refresh()
    assert len(fake_server.calls("/v1/code")) == sent
    # Back on, the same routine is set again.
    turn(menu)
    [again] = ours(fake_server)
    assert again["id"] == trigger["id"] and again["enabled"]


def test_turning_off_right_after_a_failed_edit_still_switches_it_off(sandbox, fake_server,
                                                                      menu):
    sandbox.seed_claude("main", EMAIL)
    armed(menu)
    fake_server.script("/v1/code/triggers/", 500, {"error": "boom"}, method="POST")
    move_weekly_resets(fake_server, "2100-01-10T00:00:00Z")
    menu.refresh()
    assert "off" in text(menu.find("cloud start", menu.account_row("main")))
    # The failed edit's hour of waiting is for arming. Off goes out at once.
    turn(menu)
    [trigger] = ours(fake_server)
    assert trigger["enabled"] is False


def test_a_renamed_account_can_still_switch_its_routine_off(sandbox, fake_server, menu):
    sandbox.seed_claude("main", EMAIL)
    armed(menu)
    ok, why = core.rename_account("main", "work")
    assert ok, why
    menu.refresh()
    turn(menu)
    [trigger] = ours(fake_server)
    assert trigger["enabled"] is False
    assert set(routines.status()) == {"work"}


def test_a_damaged_state_file_does_not_break_the_menu(sandbox, fake_server, menu):
    sandbox.seed_claude("main", EMAIL)
    for damage in ("[]", json.dumps({"main": None, "side": 3})):
        os.makedirs(os.path.dirname(core.CLOUD_STARTS), exist_ok=True)
        with open(core.CLOUD_STARTS, "w") as f:
            f.write(damage)
        menu.refresh()
        assert menu.account_row("main")
    turn(menu)
    assert len(ours(fake_server)) == 1


def test_a_stopped_window_starts_locally_then_gets_a_routine(sandbox, fake_server, menu):
    sandbox.seed_claude("main", EMAIL)
    for lim in fake_server.claude[EMAIL].limits:
        lim["percent"], lim["resets_at"] = 0, None
    armed(menu)
    menu.fire_delayed()
    # The local start sent the request, and the routine aims at the reset it made.
    assert [p["model"] for p in fake_server.pokes] == ["claude-fable-5-1"]
    [trigger] = ours(fake_server)
    assert trigger["run_once_at"] == two_minutes_after(weekly_reset(fake_server))
