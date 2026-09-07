"""The shared harness models call costs and single-use refresh grants."""

import json
import time
import urllib.error

import pytest

from claude_code_manager import core, keychain


def test_keychain_counts_calls_and_preserves_store_on_reset(fake_keychain):
    keychain.write_raw("service", "raw")
    assert keychain.read_raw("service") == "raw"
    assert keychain.read_raw("missing") is None
    assert keychain.delete("missing") is True
    assert (fake_keychain.reads, fake_keychain.writes, fake_keychain.deletes) == (2, 1, 1)
    assert fake_keychain.log == [("write", "service"), ("read", "service"),
                                 ("read", "missing"), ("delete", "missing")]
    fake_keychain.reset()
    assert fake_keychain.store == {"service": "raw"} and fake_keychain.log == []
    assert keychain.delete("service") and fake_keychain.store == {}


def test_fake_authorization_code_requires_login_email(fake_api):
    body = {"grant_type": "authorization_code", "code": "code"}
    with pytest.raises(urllib.error.HTTPError) as error:
        core._post(core.TOKEN_URLS[0], body)
    assert error.value.code == 400
    assert json.loads(error.value.read()) == {"error": "invalid_grant"}
    fake_api.login_email = "a@example.com"
    response = core._post(core.TOKEN_URLS[0], body)
    rotated = core._post(core.TOKEN_URLS[0], {
        "grant_type": "refresh_token", "refresh_token": response["refresh_token"]})
    assert rotated["access_token"] == "a@example.com-gen2"


def test_fake_refresh_is_single_use(fake_api):
    blob = fake_api.blob("a@example.com", gen=3, fresh=False)
    assert blob["expiresAt"] < time.time() * 1000 and core.expiring(blob)
    body = {"grant_type": "refresh_token", "refresh_token": blob["refreshToken"]}
    response = core._post(core.TOKEN_URLS[0], body)
    assert response["access_token"] == "a@example.com-gen4"
    assert response["refresh_token"] == "a@example.com-refresh4"
    assert fake_api.emails[response["access_token"]] == "a@example.com"
    for refresh in (blob["refreshToken"], "unknown"):
        with pytest.raises(urllib.error.HTTPError) as error:
            core._post(core.TOKEN_URLS[0], {**body, "refresh_token": refresh})
        assert error.value.code == 400
        assert json.loads(error.value.read()) == {"error": "invalid_grant"}
    next_response = core._post(core.TOKEN_URLS[0], {
        **body, "refresh_token": response["refresh_token"]})
    assert next_response["access_token"] == "a@example.com-gen5"
    assert fake_api.refresh_calls == 4
    fake_api.reset()
    assert fake_api.refresh_calls == fake_api.profile_calls == fake_api.usage_calls == 0


def test_fake_api_rejection_and_one_shot_failure(fake_api):
    token = fake_api.blob("a@example.com")["accessToken"]
    assert core._get("/api/oauth/profile", token)["account"]["email"] == "a@example.com"
    fake_api.usage["a@example.com"] = {"limits": []}
    fake_api.fail_next_usage = OSError("offline")
    with pytest.raises(OSError, match="offline"):
        core._get("/api/oauth/usage", token)
    assert core._get("/api/oauth/usage", token) == {"limits": []}
    fake_api.rejected.add(token)
    for value in (token, "unknown"):
        with pytest.raises(urllib.error.HTTPError) as error:
            core._get("/api/oauth/profile", value)
        assert error.value.code == 401
    assert fake_api.profile_calls == 3 and fake_api.usage_calls == 2
