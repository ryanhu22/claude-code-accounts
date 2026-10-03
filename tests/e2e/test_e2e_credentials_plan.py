"""A plan changed on claude.ai reaches every place that shows or uses it.

The slot's credential is rewritten with the new tier, every same-generation
copy follows on the next sync pass (session dirs still without a refresh
token, ~/.claude still with one), the slot's oauthAccount names the tier,
and the sessions' own .claude.json picks it up through the apply pass. A
copy on another generation is never touched, and a failed re-check keeps
the last answer.
"""
import json
import os
import time

from claude_code_accounts import core, keychain
from e2e.lifecycle import (
    check_invariants,
    claude_code_refresh,
    expire_in,
    generation,
    one_account,
)


def age_plan(slot: str, hours: float = 7) -> None:
    store = core._cache_read(core.IDENTITY_CACHE)
    entry = store[os.path.abspath(slot)]
    entry["at"] = entry["plan_at"] = time.time() - hours * 3600
    core._cache_write(store, core.IDENTITY_CACHE)


def oauth_account(path: str) -> dict:
    with open(path) as f:
        return json.load(f).get("oauthAccount") or {}


def default_dir_signed_in_as(sandbox, email: str) -> None:
    with open(os.path.join(sandbox.home, ".claude.json"), "w") as f:
        json.dump({"oauthAccount": {"emailAddress": email}}, f)


def test_an_upgrade_then_a_downgrade_reach_slot_sessions_and_default_dir(
        sandbox, fake_server, fleet, app_running):
    acct = one_account(sandbox, fake_server, tier="default_claude_max_5x") and \
        fake_server.claude["a@example.com"]
    live = [fleet.start("term-1"), fleet.start("term-2")]
    default_dir_signed_in_as(sandbox, "a@example.com")
    core.sync_credentials(live)
    assert core.load_account("a", with_usage=False).plan == "Max 5x"
    sent = len(fake_server.calls("/v1/oauth/token"))
    for tier, label in (("default_claude_max_20x", "Max 20x"), ("default_claude_pro", "Pro")):
        acct.tier = tier
        assert core.load_account("a", with_usage=False).plan != label   # still trusted
        age_plan(core.slot_dir("a"))
        assert core.load_account("a", with_usage=False).plan == label
        slot = sandbox.blob(core.slot_dir("a"))
        assert slot["rateLimitTier"] == tier and slot["refreshToken"]
        assert slot["subscriptionType"] == ("pro" if "pro" in tier else "max")
        assert oauth_account(os.path.join(core.slot_dir("a"), ".claude.json"))[
            "organizationRateLimitTier"] == tier
        # The retier reaches the running copies itself, and the sync pass
        # would catch any it missed. Neither adds a refresh token.
        core.sync_credentials(live)
        for s in live:
            copy = sandbox.blob(s.config_dir)
            assert copy["rateLimitTier"] == tier and copy["subscriptionType"] == \
                slot["subscriptionType"]
            assert "refreshToken" not in copy and copy["accessToken"] == slot["accessToken"]
        default = sandbox.blob(sandbox.default_config)
        assert default["rateLimitTier"] == tier and default["refreshToken"]
        # The sessions' own .claude.json follows through the apply pass.
        core.apply_now(live)
        for s in live:
            assert oauth_account(os.path.join(s.config_dir, ".claude.json"))[
                "organizationRateLimitTier"] == tier
        check_invariants(sandbox, fake_server, fleet, settled=True)
    # A plan change spends no refresh token and moves no generation.
    assert len(fake_server.calls("/v1/oauth/token")) == sent
    assert generation(fake_server, sandbox.blob(core.slot_dir("a"))) == 1


def test_a_copy_on_a_newer_generation_keeps_its_own_plan_fields(sandbox, fake_server, fleet,
                                                                monkeypatch):
    """App off, a session rotated itself: the slot's plan is for the slot's
    generation, so that copy is left alone, and the app then takes the copy's
    generation rather than writing the slot's older one over it."""
    acct = one_account(sandbox, fake_server) and fake_server.claude["a@example.com"]
    live = [fleet.start("term-1"), fleet.start("term-2")]
    core.sync_credentials(live)       # the app was on once: copies are stripped
    monkeypatch.setattr(core, "SOLE_REFRESHER", True)
    assert core.load_account("a", with_usage=False).plan == "Max 5x"
    core.hand_back_refresh_tokens()
    monkeypatch.setattr(core, "SOLE_REFRESHER", False)
    expire_in(sandbox, live[0].config_dir, 60)
    assert claude_code_refresh(fake_server.url, live[0].config_dir) == "rotated"
    ahead = sandbox.blob(live[0].config_dir)
    acct.tier = "default_claude_max_20x"
    age_plan(core.slot_dir("a"))
    monkeypatch.setattr(core, "SOLE_REFRESHER", True)
    assert core.load_account("a", with_usage=False).plan == "Max 20x"
    assert sandbox.blob(core.slot_dir("a"))["rateLimitTier"] == "default_claude_max_20x"
    assert sandbox.blob(live[1].config_dir)["rateLimitTier"] == "default_claude_max_20x"
    assert sandbox.blob(live[0].config_dir) == ahead                 # another generation
    core.sync_credentials(live)
    slot = sandbox.blob(core.slot_dir("a"))
    assert generation(fake_server, slot) == 2 and slot["refreshToken"] == ahead["refreshToken"]
    for s in live:
        copy = sandbox.blob(s.config_dir)
        assert copy["accessToken"] == slot["accessToken"] and "refreshToken" not in copy
    assert fake_server.reused_refresh_tokens == []
    check_invariants(sandbox, fake_server, fleet, settled=True)


def test_a_rate_limited_recheck_keeps_the_last_plan(sandbox, fake_server, fleet, app_running):
    acct = one_account(sandbox, fake_server) and fake_server.claude["a@example.com"]
    live = [fleet.start("term-1")]
    core.sync_credentials(live)
    assert core.load_account("a", with_usage=False).plan == "Max 5x"
    acct.tier = "default_claude_max_20x"
    age_plan(core.slot_dir("a"))
    fake_server.script("/api/oauth/profile", 429, {"error": "rate_limited"},
                       headers={"Retry-After": "60"})
    a = core.load_account("a", with_usage=False)
    assert a.plan == "Max 5x" and a.error is None
    assert sandbox.blob(core.slot_dir("a"))["rateLimitTier"] == "default_claude_max_5x"
    assert sandbox.blob(live[0].config_dir)["rateLimitTier"] == "default_claude_max_5x"
    # Asked again after a full interval, not on the next poll.
    assert core.load_account("a", with_usage=False).plan == "Max 5x"
    age_plan(core.slot_dir("a"))
    assert core.load_account("a", with_usage=False).plan == "Max 20x"
    core.sync_credentials(live)
    assert sandbox.blob(live[0].config_dir)["rateLimitTier"] == "default_claude_max_20x"
    check_invariants(sandbox, fake_server, fleet, settled=True)


def test_a_plan_change_and_a_rotation_on_the_same_pass(sandbox, fake_server, fleet,
                                                       app_running):
    """The plan is re-read on the usage poll, which also rotates an expiring
    slot. The new tier must ride the new generation to every copy."""
    acct = one_account(sandbox, fake_server) and fake_server.claude["a@example.com"]
    live = [fleet.start("term-1"), fleet.start("term-2")]
    core.sync_credentials(live)
    assert core.load_account("a", with_usage=False).plan == "Max 5x"
    acct.tier = "default_claude_max_20x"
    age_plan(core.slot_dir("a"))
    expire_in(sandbox, core.slot_dir("a"), 20 * 60)
    assert core.load_account("a", with_usage=False).plan == "Max 20x"
    core.sync_credentials(live)
    slot = sandbox.blob(core.slot_dir("a"))
    assert generation(fake_server, slot) == 2
    assert slot["rateLimitTier"] == "default_claude_max_20x"
    for s in live:
        copy = sandbox.blob(s.config_dir)
        assert copy["accessToken"] == slot["accessToken"]
        assert copy["rateLimitTier"] == "default_claude_max_20x"
        assert "refreshToken" not in copy
    keychain.forget()
    check_invariants(sandbox, fake_server, fleet, settled=True)
