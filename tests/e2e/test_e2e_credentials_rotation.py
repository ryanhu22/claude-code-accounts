"""One login, many copies, one rotation: the app's ordinary day.

The real `ccm resolve` prepares every session, the real `security` stub holds
the keychain, and the fake server spends refresh tokens the way Anthropic
does. The three invariants are checked after every scenario: no session copy
holds a refresh token while the app runs, no refresh token is sent twice, and
no copy falls behind its slot.
"""
import json
import os
import time

import pytest

from claude_code_accounts import core, keychain
from e2e.lifecycle import check_invariants, credential_log, expire_in, generation, one_account

PENDING = ".claude-manager/pending"


def refreshes(server) -> list:
    return [r for r in server.calls("/v1/oauth/token", "POST")
            if (r.json or {}).get("grant_type") == "refresh_token"]


def test_nine_sessions_follow_one_rotation(sandbox, fake_server, fleet, app_running):
    one_account(sandbox, fake_server)
    live = [fleet.start(f"term-{i}") for i in range(9)]
    assert len(live) == 9 and len({s.config_dir for s in live}) == 9
    # `ccm resolve` runs with no app, so it hands out whole copies. The app's
    # first pass takes the refresh tokens back off all nine.
    assert all(sandbox.blob(s.config_dir).get("refreshToken") for s in live)
    assert sorted(core.sync_credentials(live)) == sorted(s.config_dir for s in live)
    check_invariants(sandbox, fake_server, fleet, settled=True)
    assert not refreshes(fake_server)

    # The token ages into the rotation margin. One pass: one refresh, and the
    # successor reaches all nine copies before the sync even looks, because
    # their dirs are found through the sessions running in them.
    before = expire_in(sandbox, core.slot_dir("a"), 20 * 60)
    assert core.refresh_slots() == ["a"]
    assert core.sync_credentials(live) == []
    assert len(refreshes(fake_server)) == 1
    slot = sandbox.blob(core.slot_dir("a"))
    assert generation(fake_server, slot) == 2
    assert slot["refreshToken"] != before["refreshToken"]
    assert slot["refreshTokenExpiresAt"] / 1000 == pytest.approx(time.time() + 30 * 86400, abs=10)
    assert slot["expiresAt"] / 1000 == pytest.approx(time.time() + 3600, abs=10)
    for s in live:
        copy = sandbox.blob(s.config_dir)
        assert copy["accessToken"] == slot["accessToken"]
        # Each copy is stamped from the grant at its own write, so the expiry
        # agrees to within the pass, not to the millisecond.
        assert copy["expiresAt"] == pytest.approx(slot["expiresAt"], abs=5000)
        assert "refreshToken" not in copy and "refreshTokenExpiresAt" not in copy
    check_invariants(sandbox, fake_server, fleet, settled=True)
    log = credential_log(sandbox)
    assert sum("refresh a " in line for line in log) == 1
    assert sum(" propagate s-term" in line for line in log) == 9
    # Nothing moves on the next pass.
    assert core.refresh_slots() == [] and core.sync_credentials(live) == []
    assert len(refreshes(fake_server)) == 1


def test_the_default_dir_is_a_full_copy_and_follows_too(sandbox, fake_server, fleet,
                                                        app_running):
    one_account(sandbox, fake_server)
    live = [fleet.start("term-1")]
    core.sync_credentials(live)
    # ~/.claude signed in as the same account, on an older token of its own.
    with open(os.path.join(sandbox.home, ".claude.json"), "w") as f:
        json.dump({"oauthAccount": {"emailAddress": "a@example.com"}}, f)
    old = fake_server.blob("a@example.com", expires_in=60)
    sandbox._put_item(keychain.service_for(sandbox.default_config),
                      json.dumps({"claudeAiOauth": old}))
    keychain.forget()
    assert core.sync_credentials(live) == [sandbox.default_config]
    default = sandbox.blob(sandbox.default_config)
    slot = sandbox.blob(core.slot_dir("a"))
    assert default == slot and default["refreshToken"]       # not a session dir
    # The old token of ~/.claude was never spent: the slot is the one lineage.
    assert not refreshes(fake_server)
    expire_in(sandbox, core.slot_dir("a"), 20 * 60)
    assert core.refresh_slots() == ["a"]
    core.sync_credentials(live)
    slot = sandbox.blob(core.slot_dir("a"))
    default = sandbox.blob(sandbox.default_config)
    assert default["accessToken"] == slot["accessToken"]
    assert default["refreshToken"] == slot["refreshToken"]
    assert default["refreshTokenExpiresAt"] == pytest.approx(slot["refreshTokenExpiresAt"],
                                                             abs=5000)
    assert "refreshToken" not in sandbox.blob(live[0].config_dir)
    check_invariants(sandbox, fake_server, fleet, settled=True)


@pytest.mark.parametrize("lifetime", [30 * 86400, None, "1209600"],
                         ids=["present", "absent", "string"])
def test_the_renewal_date_follows_what_the_grant_states(sandbox, fake_server, fleet,
                                                        app_running, lifetime):
    one_account(sandbox, fake_server)
    live = [fleet.start("term-1")]
    core.sync_credentials(live)
    fake_server.refresh_lifetime = lifetime
    expire_in(sandbox, core.slot_dir("a"), 20 * 60)
    assert core.refresh_slots() == ["a"]
    core.sync_credentials(live)
    slot = sandbox.blob(core.slot_dir("a"))
    assert generation(fake_server, slot) == 2
    if lifetime is None:
        # Claude Code drops the date when a grant states none; a date left
        # over from sign-in would count down while the login keeps renewing.
        assert "refreshTokenExpiresAt" not in slot
    else:
        assert slot["refreshTokenExpiresAt"] / 1000 == pytest.approx(
            time.time() + float(lifetime), abs=10)
    copy = sandbox.blob(live[0].config_dir)
    assert copy["accessToken"] == slot["accessToken"]
    assert "refreshToken" not in copy and "refreshTokenExpiresAt" not in copy
    check_invariants(sandbox, fake_server, fleet, settled=True)
    log = credential_log(sandbox)
    assert any("refresh-token-life " + ("unstated" if lifetime is None else
                                        f"{float(lifetime) / 86400:.1f}d") in line
               for line in log), log


@pytest.mark.parametrize("failure", ["429", "500", "network"])
def test_a_failed_refresh_keeps_the_token_and_tries_again(sandbox, fake_server, fleet,
                                                           app_running, monkeypatch, failure):
    one_account(sandbox, fake_server)
    live = [fleet.start("term-1"), fleet.start("term-2")]
    core.sync_credentials(live)
    before = expire_in(sandbox, core.slot_dir("a"), 20 * 60)
    if failure == "429":
        fake_server.script("/v1/oauth/token", 429, {"error": "rate_limited"},
                           headers={"Retry-After": "60"})
    elif failure == "500":
        fake_server.script("/v1/oauth/token", 500, {"error": "internal"})
    else:
        monkeypatch.setattr(core, "TOKEN_URLS", ("http://127.0.0.1:9/v1/oauth/token",))
    # Nothing moved, nothing was declared dead, the copies still work.
    assert core.refresh_slots() == []
    assert core.sync_credentials(live) == []
    assert sandbox.blob(core.slot_dir("a")) == before
    assert not core._REFUSED
    assert "invalid_grant" not in "\n".join(credential_log(sandbox))
    check_invariants(sandbox, fake_server, fleet, settled=True)
    # The next pass finds the server back and the same token still good.
    if failure == "network":
        monkeypatch.setattr(core, "TOKEN_URLS", (fake_server.url + "/v1/oauth/token",))
    assert core.refresh_slots() == ["a"]
    core.sync_credentials(live)
    assert generation(fake_server, sandbox.blob(core.slot_dir("a"))) == 2
    assert len(refreshes(fake_server)) == (2 if failure != "network" else 1)
    acct = core.load_account("a", with_usage=False)
    assert acct.error is None and acct.email == "a@example.com"
    check_invariants(sandbox, fake_server, fleet, settled=True)


def test_a_keychain_that_refuses_the_write_keeps_the_successor(sandbox, fake_server, fleet,
                                                               app_running):
    """A refresh token is spent the moment the grant succeeds, so a write that
    fails afterwards must not lose the successor: it is stashed and lands on
    the next pass, and the spent token is never sent again."""
    one_account(sandbox, fake_server)
    live = [fleet.start("term-1"), fleet.start("term-2")]
    core.sync_credentials(live)
    before = expire_in(sandbox, core.slot_dir("a"), 20 * 60)
    fail = sandbox.keychain_file + ".fail-writes"
    open(fail, "w").close()
    try:
        rotated = core.live_blob(core.slot_dir("a"))
        assert generation(fake_server, rotated) == 2        # the grant went through
        assert sandbox.blob(core.slot_dir("a")) == before    # the keychain did not
        stash = os.listdir(os.path.join(sandbox.home, PENDING))
        assert len(stash) == 1
        with open(os.path.join(sandbox.home, PENDING, stash[0])) as f:
            saved = json.load(f)
        assert saved["replaces"] == core.fingerprint(before)
        assert saved["blob"]["refreshToken"] == rotated["refreshToken"]
        # A second pass while the keychain still refuses: the stored token is
        # spent, so it must not go to the server again, and the stash must
        # survive for the pass that can write.
        core.refresh_slots()
        assert core.sync_credentials(live) == []
        assert fake_server.reused_refresh_tokens == []
        assert core.load_account("a", with_usage=False).error is None
        assert not core._REFUSED
        assert sandbox.blob(core.slot_dir("a")) == before
        assert os.listdir(os.path.join(sandbox.home, PENDING)) == stash
    finally:
        os.remove(fail)
    # The keychain is back: the stash lands, and every copy follows.
    assert core.refresh_slots() == ["a"]
    assert core.sync_credentials(live) == [s.config_dir for s in live]
    slot = sandbox.blob(core.slot_dir("a"))
    assert slot["refreshToken"] == rotated["refreshToken"]
    assert os.listdir(os.path.join(sandbox.home, PENDING)) == []
    assert len(refreshes(fake_server)) == 1
    check_invariants(sandbox, fake_server, fleet, settled=True)
