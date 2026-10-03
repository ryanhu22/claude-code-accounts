"""A Mac that sleeps and wakes, with the app's own handlers doing the work.

No timer runs while a Mac sleeps, so every copy of a login can wake expired
at once. The app rotates on the way down, pauses, and rotates again on the
way up. These run the real sleep, wake and credential tick handlers of the
menu bar app in-process, on real session dirs, against the fake server, and
the `pmset` stub says whether a wake is a dark one.
"""
import os
import time

import pytest

from claude_code_accounts import core, menubar
from e2e.lifecycle import (
    check_invariants,
    credential_log,
    expire_in,
    generation,
    menubar_app,
    one_account,
    wait_pass,
)


@pytest.fixture
def no_wake_burst(monkeypatch):
    """The wake handler's 5, 10 and 20 second follow-ups would outlive a test."""
    monkeypatch.setattr(menubar, "WAKE_BURST", ())


def refreshes(server) -> list:
    return [r for r in server.calls("/v1/oauth/token", "POST")
            if (r.json or {}).get("grant_type") == "refresh_token"]


def test_sleep_rotates_what_has_hours_left_and_wake_rotates_what_expired(
        sandbox, fake_server, fleet, app_running, no_wake_burst):
    one_account(sandbox, fake_server)
    fake_server.add_claude("b@example.com")
    sandbox.seed_claude("b", "b@example.com")
    live = [fleet.start(f"term-{i}") for i in range(4)]
    core.sync_credentials(live)
    app = menubar_app(live)
    # a has two hours left, b has ten. Only a is worth rotating before a sleep.
    expire_in(sandbox, core.slot_dir("a"), 2 * 3600)
    expire_in(sandbox, core.slot_dir("b"), 10 * 3600)
    app._on_sleep()
    assert core.ROTATION_PAUSED
    assert len(refreshes(fake_server)) == 1
    assert generation(fake_server, sandbox.blob(core.slot_dir("a"))) == 2
    assert generation(fake_server, sandbox.blob(core.slot_dir("b"))) == 1
    for s in live:
        assert sandbox.blob(s.config_dir)["accessToken"] == \
            sandbox.blob(core.slot_dir("a"))["accessToken"]
    check_invariants(sandbox, fake_server, fleet, settled=True)

    # The Mac sleeps past the new token's expiry: every copy wakes expired.
    for path in [core.slot_dir("a"), *(s.config_dir for s in live)]:
        expire_in(sandbox, path, -60)
    app._on_wake()
    wait_pass(app)
    assert not core.ROTATION_PAUSED
    assert len(refreshes(fake_server)) == 2             # once down, once up
    slot = sandbox.blob(core.slot_dir("a"))
    assert generation(fake_server, slot) == 3
    assert slot["expiresAt"] / 1000 > time.time() + 3000
    for s in live:
        copy = sandbox.blob(s.config_dir)
        assert copy["accessToken"] == slot["accessToken"] and "refreshToken" not in copy
    check_invariants(sandbox, fake_server, fleet, settled=True)
    log = "\n".join(credential_log(sandbox))
    assert "sleep" in log and "wake" in log
    assert log.count("refresh a ") == 2 and "refresh b " not in log


def test_a_dark_wake_tick_rotates_nothing(sandbox, fake_server, fleet, app_running,
                                         no_wake_burst):
    """A maintenance wake fires the timers with the lid closed. A refresh sent
    then can lose its reply to the next sleep, so nothing may be sent."""
    one_account(sandbox, fake_server)
    live = [fleet.start("term-1"), fleet.start("term-2")]
    core.sync_credentials(live)
    app = menubar_app(live)
    expire_in(sandbox, core.slot_dir("a"), 2 * 3600)
    app._on_sleep()
    assert len(refreshes(fake_server)) == 1
    # Asleep, then a dark wake, with every copy expired by now.
    for path in [core.slot_dir("a"), *(s.config_dir for s in live)]:
        expire_in(sandbox, path, -60)
    open(os.path.join(sandbox.root, "pmset.dark"), "w").close()
    app._last_tick = time.time() - 600
    app._on_credential_tick(None)
    assert core.ROTATION_PAUSED and not app._syncing
    assert len(refreshes(fake_server)) == 1
    # The next tick inside the same dark wake is an ordinary one: it still
    # must not rotate, and it may still hand copies around.
    app._last_tick = time.time()
    app._on_credential_tick(None)
    wait_pass(app)
    assert core.ROTATION_PAUSED
    assert len(refreshes(fake_server)) == 1
    assert generation(fake_server, sandbox.blob(core.slot_dir("a"))) == 2
    # The real wake: one rotation, every copy follows.
    os.remove(os.path.join(sandbox.root, "pmset.dark"))
    app._on_wake()
    wait_pass(app)
    assert not core.ROTATION_PAUSED
    assert len(refreshes(fake_server)) == 2
    assert generation(fake_server, sandbox.blob(core.slot_dir("a"))) == 3
    check_invariants(sandbox, fake_server, fleet, settled=True)
    assert "dark wake" in "\n".join(credential_log(sandbox))


def test_a_missed_wake_notification_is_caught_by_the_gap(sandbox, fake_server, fleet,
                                                        app_running, no_wake_burst):
    one_account(sandbox, fake_server)
    live = [fleet.start("term-1")]
    core.sync_credentials(live)
    app = menubar_app(live)
    expire_in(sandbox, core.slot_dir("a"), 10 * 3600)
    app._on_sleep()
    assert core.ROTATION_PAUSED and not refreshes(fake_server)   # 10h left: not worth it
    for path in [core.slot_dir("a"), live[0].config_dir]:
        expire_in(sandbox, path, -60)
    # No notification came, but the tick is ten minutes late and pmset says
    # the Mac is really awake. The pause must end here, or nothing would ever
    # rotate again.
    app._last_tick = time.time() - 600
    app._on_credential_tick(None)
    wait_pass(app)
    assert not core.ROTATION_PAUSED
    assert len(refreshes(fake_server)) == 1
    assert generation(fake_server, sandbox.blob(core.slot_dir("a"))) == 2
    check_invariants(sandbox, fake_server, fleet, settled=True)


def test_a_refresh_reply_lost_to_the_sleep_is_tried_once_more_and_then_left(
        sandbox, fake_server, fleet, app_running, no_wake_burst):
    """The Mac sleeps with a refresh in flight: the server spent the token and
    the reply never arrived. The client cannot tell that from a request that
    never got out, so one more attempt on wake is right. The server's
    invalid_grant then settles it: no further attempt, the sessions keep the
    access token they have, and the account says so."""
    one_account(sandbox, fake_server)
    live = [fleet.start("term-1"), fleet.start("term-2")]
    core.sync_credentials(live)
    app = menubar_app(live)
    before = expire_in(sandbox, core.slot_dir("a"), 2 * 3600)
    fake_server.lost_refresh_replies = 1
    fake_server.allow_reuse = True
    app._on_sleep()
    assert fake_server.lost_replies == [before["refreshToken"]]
    assert sandbox.blob(core.slot_dir("a")) == before          # nothing to store
    assert core.ROTATION_PAUSED
    # A long sleep: everything is expired when the Mac comes back.
    for path in [core.slot_dir("a"), *(s.config_dir for s in live)]:
        expire_in(sandbox, path, -60)
    app._on_wake()
    wait_pass(app)
    assert fake_server.refresh_uses[before["refreshToken"]] == 2
    assert "invalid_grant a" in "\n".join(credential_log(sandbox))
    # From here on the spent token stays home, whatever the passes do.
    app._last_tick = time.time()
    for _ in range(3):
        app._on_credential_tick(None)
        wait_pass(app)
    acct = core.load_account("a", with_usage=False)
    assert fake_server.refresh_uses[before["refreshToken"]] == 2
    assert acct.email == "a@example.com"
    for s in live:
        copy = sandbox.blob(s.config_dir)
        assert copy["accessToken"] == before["accessToken"] and "refreshToken" not in copy
    # The one reuse here is the lost reply; nothing else was sent twice.
    assert fake_server.reused_refresh_tokens == [before["refreshToken"]]
