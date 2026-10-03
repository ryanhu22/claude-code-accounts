"""A working login that stops soon says so, and a revoked one stops looking healthy."""

import time
import urllib.error

import pytest

from claude_code_accounts import core, keychain
from fakes import sign_in

pytestmark = pytest.mark.usefixtures("fake_keychain", "fake_api", "fast_locks")

HOUR = 3600


def blob(**extra):
    return {"accessToken": "a", "refreshToken": "r",
            "expiresAt": int((time.time() + 5 * HOUR) * 1000), **extra}


def at(seconds):
    return int((time.time() + seconds) * 1000)


def test_no_warning_while_the_login_has_days_left():
    assert core.renewal_warning(blob(refreshTokenExpiresAt=at(10 * 86400))) == (None, None)
    assert core.renewal_warning(blob()) == (None, None)       # no end stated


def test_warns_three_days_before_anthropic_ends_the_login():
    row, note = core.renewal_warning(blob(refreshTokenExpiresAt=at(2 * 86400)))
    assert row.startswith("login ends on ")
    assert note.startswith("Anthropic ends this login on ") and "sign in again" in note


@pytest.mark.parametrize("state", [
    {"refreshTokenExpiresAt": at(-HOUR)},       # the end has passed
    {"refreshToken": None},                     # healed from a copy with no refresh token
])
def test_a_login_that_cannot_renew_names_when_it_stops(state):
    row, note = core.renewal_warning(blob(**state))
    assert row.startswith("sign in again by ")
    assert note.startswith("This login can't renew, and it stops ")


def test_refused_renewal_keeps_the_hours_that_are_left(fake_api):
    slot = sign_in("a", "a@example.com", fake_api)
    assert core.load_account("a", with_usage=False).email      # identity cached
    stored = keychain.read_credentials(slot)
    core._REFUSED[core.os.path.abspath(slot)] = core.fingerprint(stored)
    acct = core.load_account("a", with_usage=False)
    assert acct.signed_in and not acct.error
    assert acct.warning.startswith("sign in again by ")


def test_refused_renewal_after_the_token_expired_reads_as_expired(fake_api):
    slot = sign_in("a", "a@example.com", fake_api, fresh=False)
    stored = keychain.read_credentials(slot)
    core._REFUSED[core.os.path.abspath(slot)] = core.fingerprint(stored)
    assert core.load_account("a", with_usage=False).error == "login expired"


def test_revoked_login_stops_showing_its_cached_usage(fake_api):
    slot = sign_in("a", "a@example.com", fake_api)
    assert core.load_account("a").reading
    fake_api.rejected.add(keychain.read_credentials(slot)["accessToken"])
    core.forget_usage("a")
    acct = core.load_account("a", force=True)
    assert acct.error == "login expired" and not acct.signed_in and not acct.limits


def test_one_401_with_a_working_profile_keeps_the_cached_usage(fake_api, monkeypatch):
    sign_in("a", "a@example.com", fake_api)
    assert core.load_account("a").reading
    real = core._get

    def usage_401(path, token, timeout=20):
        if path.startswith("/api/oauth/usage"):
            raise urllib.error.HTTPError(core.API + path, 401, "Unauthorized", {}, None)
        return real(path, token, timeout)

    monkeypatch.setattr(core, "_get", usage_401)
    core.forget_usage("a")
    acct = core.load_account("a", force=True)
    assert acct.signed_in and acct.reading and acct.error is None


def test_a_rotation_during_the_check_is_not_a_revocation(fake_api, monkeypatch):
    slot = sign_in("a", "a@example.com", fake_api)
    assert core.load_account("a").reading
    token = keychain.read_credentials(slot)["accessToken"]
    fake_api.rejected.add(token)
    real = core.profile_result

    def rotate_then_answer(tok):
        keychain.write_credentials(slot, fake_api.blob("a@example.com", 2))
        return real(tok)

    monkeypatch.setattr(core, "profile_result", rotate_then_answer)
    core.forget_usage("a")
    assert core.load_account("a", force=True).error != "login expired"
