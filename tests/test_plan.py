"""A login's renewal date and plan stay current after sign-in."""

import json
import time
from pathlib import Path

import pytest

from claude_code_accounts import core, keychain
from fakes import session, sign_in

pytestmark = pytest.mark.usefixtures("fake_keychain", "fake_api", "fast_locks")


def test_apply_takes_new_refresh_lifetime():
    stale = {"accessToken": "a", "refreshToken": "r", "refreshTokenExpiresAt": 1}
    out = core._apply(stale, {"access_token": "b", "refresh_token": "s", "expires_in": 3600,
                              "refresh_token_expires_in": 30 * 86400})
    assert out["refreshTokenExpiresAt"] / 1000 == pytest.approx(time.time() + 30 * 86400, abs=5)


def test_apply_drops_lifetime_the_response_does_not_state():
    stale = {"accessToken": "a", "refreshToken": "r", "refreshTokenExpiresAt": 1}
    out = core._apply(stale, {"access_token": "b", "refresh_token": "s", "expires_in": 3600})
    assert "refreshTokenExpiresAt" not in out


def age_plan(slot, hours=7):
    store = core._cache_read(core.IDENTITY_CACHE)
    entry = store[core.os.path.abspath(slot)]
    entry["at"] = entry["plan_at"] = time.time() - hours * 3600
    core._cache_write(store, core.IDENTITY_CACHE)


def upgrade(fake_api, email, tier):
    fake_api.profiles[email] = {"account": {"uuid": f"uuid-{email}", "email": email},
                                "organization": {"rate_limit_tier": tier}}


def test_plan_change_reaches_menu_slot_and_sessions(fake_api, tmp_path, monkeypatch):
    monkeypatch.setattr(core, "SOLE_REFRESHER", True)
    sign_in("a", "a@example.com", fake_api)
    s = session("term", str(tmp_path), "a", fake_api)
    assert core.load_account("a", with_usage=False).plan == "Max 5x"
    Path(core.account_dir("a"), ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "a@example.com",
                                     "organizationRateLimitTier": "default_claude_max_5x"}}))

    upgrade(fake_api, "a@example.com", "default_claude_max_20x")
    assert core.load_account("a", with_usage=False).plan == "Max 5x"   # still fresh
    age_plan(core.slot_dir("a"))
    assert core.load_account("a", with_usage=False).plan == "Max 20x"

    blob = keychain.read_credentials(core.slot_dir("a"))
    assert blob["rateLimitTier"] == "default_claude_max_20x"
    assert blob["subscriptionType"] == "max"
    # The session is not running here, so the per-pass sync is what reaches it.
    assert core._replan(s.config_dir, core.slot_dir("a"))
    blob = keychain.read_credentials(s.config_dir)
    assert blob["rateLimitTier"] == "default_claude_max_20x"
    assert "refreshToken" not in blob
    config = json.loads(Path(core.account_dir("a"), ".claude.json").read_text())
    assert config["oauthAccount"]["organizationRateLimitTier"] == "default_claude_max_20x"


def test_plan_recheck_failure_keeps_last_answer(fake_api, monkeypatch):
    sign_in("a", "a@example.com", fake_api)
    assert core.load_account("a", with_usage=False).plan == "Max 5x"
    age_plan(core.slot_dir("a"))
    monkeypatch.setattr(core, "profile_result", lambda token: ({}, "transient"))
    acct = core.load_account("a", with_usage=False)
    assert acct.plan == "Max 5x" and not acct.error


def test_sync_heals_a_copy_the_retier_missed(fake_api, tmp_path, monkeypatch):
    monkeypatch.setattr(core, "SOLE_REFRESHER", True)
    slot = sign_in("a", "a@example.com", fake_api)
    s = session("term", str(tmp_path), "a", fake_api)
    stale = keychain.read_credentials(s.config_dir)
    keychain.write_credentials(slot, {**keychain.read_credentials(slot),
                                      "rateLimitTier": "default_claude_max_20x"})
    assert core._replan(s.config_dir, slot)
    blob = keychain.read_credentials(s.config_dir)
    assert blob == {**stale, "rateLimitTier": "default_claude_max_20x"}
    assert not core._replan(s.config_dir, slot)          # nothing left to do


def test_replan_leaves_another_generation_alone(fake_api, tmp_path, monkeypatch):
    monkeypatch.setattr(core, "SOLE_REFRESHER", True)
    slot = sign_in("a", "a@example.com", fake_api)
    s = session("term", str(tmp_path), "a", fake_api)
    keychain.write_credentials(slot, {**fake_api.blob("a@example.com", 2),
                                      "rateLimitTier": "default_claude_max_20x"})
    before = keychain.read_credentials(s.config_dir)
    assert not core._replan(s.config_dir, slot)
    assert keychain.read_credentials(s.config_dir) == before


def test_failed_recheck_waits_a_full_ttl(fake_api, monkeypatch):
    sign_in("a", "a@example.com", fake_api)
    core.load_account("a", with_usage=False)
    age_plan(core.slot_dir("a"))
    monkeypatch.setattr(core, "profile_result", lambda token: ({}, "transient"))
    core.load_account("a", with_usage=False)
    calls = []
    monkeypatch.setattr(core, "profile_result", lambda token: calls.append(1) or ({}, "transient"))
    core.load_account("a", with_usage=False)
    assert not calls
