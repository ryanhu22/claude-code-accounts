"""Accounts signed in through ccm supply their own session identity."""

import json
import urllib.error
from contextlib import contextmanager
from pathlib import Path

import pytest

from claude_code_accounts import core, locks, oauth
from fakes import sign_in

pytestmark = pytest.mark.usefixtures("fake_keychain", "fake_api", "fast_locks")


def session_config():
    path = core.session_dir("term")
    Path(path).mkdir(parents=True)
    config = Path(path, ".claude.json")
    config.write_text('{"oauthAccount": {"accountUuid": "old"}, "numStartups": 7}')
    return config


@pytest.mark.parametrize("existing", [None, {"theme": "dark", "numStartups": 3}])
def test_sync_identity_from_profile(fake_api, existing):
    slot = sign_in("a", "a@example.com", fake_api)
    source = Path(slot, ".claude.json")
    if existing is not None:
        source.write_text(json.dumps(existing))
    config = session_config()
    expected = {
        "accountUuid": "uuid-a@example.com", "emailAddress": "a@example.com",
        "organizationRateLimitTier": "default_claude_max_5x",
    }
    assert core._sync_identity(str(config.parent), "a")
    assert json.loads(source.read_text()) == {**(existing or {}), "oauthAccount": expected}
    assert source.stat().st_mode & 0o777 == 0o600
    assert json.loads(config.read_text()) == {"numStartups": 7, "oauthAccount": expected}
    assert config.stat().st_mode & 0o777 == 0o600
    before = config.stat().st_mtime_ns
    assert not core._sync_identity(str(config.parent), "a")
    assert config.stat().st_mtime_ns == before
    assert fake_api.profile_calls == 1


def test_account_identity_maps_profile_fields(fake_api):
    sign_in("a", "a@example.com", fake_api)
    fake_api.profiles["a@example.com"] = {
        "account": {
            "uuid": "uuid-a", "email": "a@example.com", "full_name": "Alice Example",
            "display_name": "Alice", "created_at": "2025-01-01", "has_claude_max": True,
            "has_claude_pro": False,
        },
        "organization": {
            "uuid": "org-a", "name": "Personal", "organization_type": "individual",
            "billing_type": "stripe", "has_extra_usage_enabled": False,
            "subscription_created_at": "2025-02-01", "rate_limit_tier": "max",
            "seat_tier": "premium", "subscription_status": "active",
        },
    }
    expected = {
        "accountUuid": "uuid-a", "emailAddress": "a@example.com",
        "fullName": "Alice Example", "displayName": "Alice", "accountCreatedAt": "2025-01-01",
        "organizationUuid": "org-a", "organizationName": "Personal",
        "organizationType": "individual", "billingType": "stripe",
        "hasExtraUsageEnabled": False, "subscriptionCreatedAt": "2025-02-01",
        "organizationRateLimitTier": "max", "seatTier": "premium",
    }
    assert core._account_identity("a") == expected


@pytest.mark.parametrize("status", [None, 401, 429])
def test_sync_identity_profile_failure(fake_api, monkeypatch, status):
    slot = sign_in("a", "a@example.com", fake_api)
    config = session_config()
    before = config.read_bytes()
    original = core._get

    def transient(*args, **kwargs):
        if status is None:
            raise OSError("offline")
        raise urllib.error.HTTPError(core.API, status, "transient", {}, None)

    monkeypatch.setattr(core, "_get", transient)
    assert not core._sync_identity(str(config.parent), "a")
    assert not Path(slot, ".claude.json").exists()
    assert config.read_bytes() == before
    monkeypatch.setattr(core, "_get", original)
    assert core._sync_identity(str(config.parent), "a")


@pytest.mark.parametrize("missing", ["uuid", "email"])
@pytest.mark.parametrize("value", [None, "", "absent"])
def test_sync_identity_incomplete_profile(fake_api, missing, value):
    slot = sign_in("a", "a@example.com", fake_api)
    account = {"uuid": "uuid-a", "email": "a@example.com"}
    if value == "absent":
        account.pop(missing)
    else:
        account[missing] = value
    fake_api.profiles["a@example.com"] = {"account": account}
    config = session_config()
    before = config.read_bytes()
    assert not core._sync_identity(str(config.parent), "a")
    assert not Path(slot, ".claude.json").exists()
    assert config.read_bytes() == before


def test_account_identity_omits_null_fields(fake_api):
    sign_in("a", "a@example.com", fake_api)
    fake_api.profiles["a@example.com"] = {
        "account": {"uuid": "uuid-a", "email": "a@example.com", "display_name": None},
        "organization": {"seat_tier": None, "has_extra_usage_enabled": False},
    }
    assert core._account_identity("a") == {
        "accountUuid": "uuid-a", "emailAddress": "a@example.com", "hasExtraUsageEnabled": False,
    }


@pytest.mark.parametrize("failure", ["busy", "write"])
def test_sync_identity_account_write_failure(fake_api, monkeypatch, failure):
    slot = sign_in("a", "a@example.com", fake_api)
    config = session_config()
    original_lock = locks.config
    original_replace = core.os.replace

    @contextmanager
    def busy(path):
        if path == slot:
            raise locks.LockBusy("held by Claude Code")
        with original_lock(path):
            yield

    def replace(src, dst):
        if dst == str(Path(slot, ".claude.json")):
            raise OSError("write failed")
        original_replace(src, dst)

    if failure == "busy":
        monkeypatch.setattr(locks, "config", busy)
    else:
        monkeypatch.setattr(core.os, "replace", replace)
    assert core._sync_identity(str(config.parent), "a")
    assert not Path(slot, ".claude.json").exists()
    assert not Path(slot, ".claude.json.tmp").exists()
    assert json.loads(config.read_text())["oauthAccount"]["emailAddress"] == "a@example.com"


def test_sign_in_finish_ignores_identity_failure(fake_api, monkeypatch):
    fake_api.login_email = "a@example.com"

    def failed(account):
        raise OSError("offline")

    monkeypatch.setattr(core, "_account_identity", failed)
    attempt = oauth.Attempt("verifier", "state", "a", "http://localhost:12345/callback")
    assert core.sign_in_finish(attempt, "code#state")[0]
    assert core.live_blob(core.account_dir("a"))["accessToken"]
