"""Two things that can rotate one login, and the app starting and stopping.

While the app is off, `ccm resolve` hands every session the whole login, and
a session then refreshes on its own exactly as Claude Code does: under its
locks, re-reading first. Once the app is back it must take the live lineage
from whichever copy holds it, never spend a token a session already spent,
and never write an older generation over a newer one. Quitting hands the
refresh tokens back, and the next start takes them off again.
"""
import threading
import time

from claude_code_accounts import core, keychain
from e2e.lifecycle import (
    check_invariants,
    claude_code_refresh,
    credential_log,
    expire_in,
    generation,
    one_account,
)


def refreshes(server) -> list:
    return [r for r in server.calls("/v1/oauth/token", "POST")
            if (r.json or {}).get("grant_type") == "refresh_token"]


def app_starts(monkeypatch) -> None:
    monkeypatch.setattr(core, "SOLE_REFRESHER", True)
    core.enable_file_log()


def test_a_session_that_rotated_while_the_app_was_off_is_promoted_not_raced(
        sandbox, fake_server, fleet, monkeypatch):
    """App off: a session spends the shared refresh token itself. The app's
    first look at the slot (the usage poll) must take that session's
    successor rather than send the spent token to the server."""
    one_account(sandbox, fake_server)
    live = [fleet.start("term-1"), fleet.start("term-2")]
    assert all(sandbox.blob(s.config_dir).get("refreshToken") for s in live)
    expire_in(sandbox, live[0].config_dir, 60)
    assert claude_code_refresh(fake_server.url, live[0].config_dir) == "rotated"
    rotated = sandbox.blob(live[0].config_dir)
    assert generation(fake_server, rotated) == 2 and rotated["refreshToken"]
    expire_in(sandbox, core.slot_dir("a"), 20 * 60)

    app_starts(monkeypatch)
    acct = core.load_account("a", with_usage=False)
    assert acct.error is None and acct.email == "a@example.com"
    slot = sandbox.blob(core.slot_dir("a"))
    assert slot["accessToken"] == rotated["accessToken"]
    assert slot["refreshToken"] == rotated["refreshToken"]
    assert fake_server.reused_refresh_tokens == []
    assert len(refreshes(fake_server)) == 1                   # the session's own
    assert "invalid_grant" not in "\n".join(credential_log(sandbox))
    # The sync pass then strips the session that rotated and brings the other
    # one up to the same generation.
    assert sorted(core.sync_credentials(live)) == sorted(s.config_dir for s in live)
    check_invariants(sandbox, fake_server, fleet, settled=True)
    assert generation(fake_server, sandbox.blob(live[1].config_dir)) == 2


def test_a_session_finds_the_app_already_rotated_and_skips(sandbox, fake_server, fleet,
                                                           monkeypatch):
    """The other order: the app rotates first and hands the successor to a
    session whose copy was still whole. Claude Code re-reads under its locks
    and finds a fresh token with nothing to rotate it with, so it sends
    nothing."""
    one_account(sandbox, fake_server)
    live = [fleet.start("term-1")]
    app_starts(monkeypatch)
    expire_in(sandbox, core.slot_dir("a"), 20 * 60)
    expire_in(sandbox, live[0].config_dir, 20 * 60)
    assert core.refresh_slots() == ["a"]
    assert claude_code_refresh(fake_server.url, live[0].config_dir) == "no_refresh_token"
    assert len(refreshes(fake_server)) == 1
    copy = sandbox.blob(live[0].config_dir)
    assert generation(fake_server, copy) == 2 and "refreshToken" not in copy
    check_invariants(sandbox, fake_server, fleet, settled=True)


def test_the_app_and_a_whole_copy_rotating_at_once_settle_on_one_lineage(
        sandbox, fake_server, fleet, monkeypatch):
    """A true race, on a copy the app has not stripped yet. Whoever reaches
    the server second gets invalid_grant; what matters is where things stand
    after the next pass: one lineage, every copy on it, nothing older written
    over anything newer."""
    one_account(sandbox, fake_server)
    live = [fleet.start("term-1"), fleet.start("term-2")]
    app_starts(monkeypatch)
    expire_in(sandbox, core.slot_dir("a"), 20 * 60)
    expire_in(sandbox, live[0].config_dir, 20 * 60)
    outcome = {}

    def session():
        outcome["session"] = claude_code_refresh(fake_server.url, live[0].config_dir, hold=0.5)

    t = threading.Thread(target=session)
    t.start()
    core.refresh_slots()
    t.join(timeout=30)
    assert outcome["session"] in ("rotated", "invalid_grant", "fresh"), outcome
    keychain.forget()
    core.sync_credentials(live)
    core.refresh_slots()
    core.sync_credentials(live)
    slot = sandbox.blob(core.slot_dir("a"))
    newest = max(generation(fake_server, sandbox.blob(d))
                 for d in [core.slot_dir("a"), *(s.config_dir for s in live)])
    assert generation(fake_server, slot) == newest and slot["refreshToken"]
    for s in live:
        copy = sandbox.blob(s.config_dir)
        assert copy["accessToken"] == slot["accessToken"], (s.term_id, outcome)
        assert "refreshToken" not in copy
    # The spent token can be sent twice only by the two racers for it, never
    # by a later pass.
    assert len(fake_server.reused_refresh_tokens) <= 1
    assert all(n <= 2 for n in fake_server.refresh_uses.values())


def test_a_copy_refused_while_a_peer_holds_the_successor(sandbox, fake_server, fleet,
                                                        monkeypatch):
    """App off: session 1 rotates, session 2 then tries with the same token
    and is signed out. The app's start takes session 1's lineage into the
    slot and writes it to session 2's dir as well."""
    one_account(sandbox, fake_server)
    live = [fleet.start("term-1"), fleet.start("term-2")]
    expire_in(sandbox, live[0].config_dir, 60)
    assert claude_code_refresh(fake_server.url, live[0].config_dir) == "rotated"
    expire_in(sandbox, live[1].config_dir, 60)
    assert claude_code_refresh(fake_server.url, live[1].config_dir) == "invalid_grant"
    assert sandbox.blob(live[1].config_dir)["accessToken"] == ""
    # That one reuse was the two sessions' doing, with no app to stop them.
    assert len(fake_server.reused_refresh_tokens) == 1
    expire_in(sandbox, core.slot_dir("a"), 20 * 60)
    app_starts(monkeypatch)
    assert core.load_account("a", with_usage=False).error is None
    assert all(n <= 2 for n in fake_server.refresh_uses.values())
    assert generation(fake_server, sandbox.blob(core.slot_dir("a"))) == 2
    assert sorted(core.sync_credentials(live)) == sorted(s.config_dir for s in live)
    for s in live:
        copy = sandbox.blob(s.config_dir)
        assert generation(fake_server, copy) == 2 and "refreshToken" not in copy
    assert len(refreshes(fake_server)) == 2            # the sessions' two, none from the app


def test_quit_hands_the_tokens_back_and_the_next_start_takes_them_again(
        sandbox, fake_server, fleet, monkeypatch):
    one_account(sandbox, fake_server)
    live = [fleet.start(f"term-{i}") for i in range(3)]
    app_starts(monkeypatch)
    assert sorted(core.sync_credentials(live)) == sorted(s.config_dir for s in live)
    check_invariants(sandbox, fake_server, fleet, settled=True)
    slot = sandbox.blob(core.slot_dir("a"))

    # Quit: every copy gets the whole login back, renewal date included.
    assert sorted(core.hand_back_refresh_tokens()) == sorted(s.config_dir for s in live)
    monkeypatch.setattr(core, "SOLE_REFRESHER", False)
    for s in live:
        assert sandbox.blob(s.config_dir) == slot
    assert "handback" in "\n".join(credential_log(sandbox))
    assert core.hand_back_refresh_tokens() == []
    check_invariants(sandbox, fake_server, fleet, app_running=False, settled=True)

    # While the app is off, one session renews itself, as it did before the
    # app existed. A new session started now gets a whole copy too.
    for path in [core.slot_dir("a"), *(s.config_dir for s in live)]:
        expire_in(sandbox, path, 4 * 60)
    assert claude_code_refresh(fake_server.url, live[1].config_dir) == "rotated"
    # `ccm resolve` for the new terminal looks at the slot, whose token that
    # session just spent. It must hand out the session's lineage, not send
    # the spent token.
    live.append(fleet.start("term-3"))
    assert sandbox.blob(live[3].config_dir).get("refreshToken")
    assert generation(fake_server, sandbox.blob(live[3].config_dir)) == 2
    assert fake_server.reused_refresh_tokens == []

    # Restart: the slot takes the live lineage, every copy is stripped again,
    # and the spent token is never sent.
    app_starts(monkeypatch)
    assert core.load_account("a", with_usage=False).error is None
    slot = sandbox.blob(core.slot_dir("a"))
    assert generation(fake_server, slot) == 2 and slot["refreshToken"]
    assert sorted(core.sync_credentials(live)) == sorted(s.config_dir for s in live)
    for s in live:
        copy = sandbox.blob(s.config_dir)
        assert copy["accessToken"] == slot["accessToken"] and "refreshToken" not in copy
    assert fake_server.reused_refresh_tokens == []
    check_invariants(sandbox, fake_server, fleet, settled=True)
    # Steady state: nothing moves, nothing is sent.
    sent = len(refreshes(fake_server))
    assert core.refresh_slots() == [] and core.sync_credentials(live) == []
    assert len(refreshes(fake_server)) == sent


def test_hand_back_leaves_a_copy_on_another_generation_alone(sandbox, fake_server, fleet,
                                                             monkeypatch):
    """A copy that missed the last rotation is not the slot's generation, so
    the slot's refresh token is not its to have."""
    one_account(sandbox, fake_server)
    live = [fleet.start("term-1"), fleet.start("term-2")]
    app_starts(monkeypatch)
    core.sync_credentials(live)
    # term-2 is busy (Claude Code holds its lock) through the rotation.
    expire_in(sandbox, core.slot_dir("a"), 20 * 60)
    stale = sandbox.blob(live[1].config_dir)
    from claude_code_accounts import locks
    with locks.credentials(live[1].config_dir):
        assert core.refresh_slots() == ["a"]
        given = core.hand_back_refresh_tokens()
    assert given == [live[0].config_dir]
    assert sandbox.blob(live[1].config_dir) == stale
    assert time.time() > 0
