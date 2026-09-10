import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from claude_code_accounts import codex


@pytest.fixture
def payload():
    return {
        "email": "a@b.c", "plan_type": "prolite",
        "rate_limit": {
            "allowed": True, "limit_reached": False,
            "primary_window": {
                "used_percent": 62, "limit_window_seconds": 604800,
                "reset_after_seconds": 240764, "reset_at": 4102444800,
            },
            "secondary_window": None,
        },
        "additional_rate_limits": [{
            "limit_name": "GPT-5.3-Codex-Spark", "metered_feature": "codex_bengalfox",
            "rate_limit": {
                "allowed": True, "limit_reached": False,
                "primary_window": {
                    "used_percent": 0, "limit_window_seconds": 18000,
                    "reset_after_seconds": 18000, "reset_at": 4102444800,
                },
                "secondary_window": {
                    "used_percent": 0, "limit_window_seconds": 604800,
                    "reset_after_seconds": 604800, "reset_at": 4102444800,
                },
            },
            "normal_model_slug": None,
        }],
        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
        "rate_limit_reset_credits": {"available_count": 2, "applicable_available_count": 0},
    }


def test_parse_limits(payload):
    limits = codex.parse_limits(payload)
    assert [(lim.kind, lim.span, lim.scope, lim.percent) for lim in limits] == [
        ("weekly_all", 604800, "", 62),
        ("scoped_session", 18000, "spark", 0),
        ("scoped_weekly", 604800, "spark", 0),
    ]
    assert limits[0].resets_at == "2100-01-01T00:00:00+00:00"
    assert [lim.resets_at for lim in limits[1:]] == [None, None]
    assert [lim.label for lim in limits] == ["7d", "spark", "spark"]


def test_passed_reset_has_no_clock(payload):
    payload["rate_limit"]["primary_window"]["reset_at"] = 1
    limit = codex.parse_limits(payload)[0]
    assert limit.percent == 0
    assert limit.resets_at is None


def test_window_order_follows_span(payload):
    payload["rate_limit"]["secondary_window"] = {
        "used_percent": 10, "limit_window_seconds": 18000, "reset_at": 4102444800,
    }
    scoped = payload["additional_rate_limits"][0]["rate_limit"]
    scoped["primary_window"], scoped["secondary_window"] = (
        scoped["secondary_window"], scoped["primary_window"]
    )
    assert [lim.kind for lim in codex.parse_limits(payload)] == [
        "session", "weekly_all", "scoped_session", "scoped_weekly",
    ]
    assert codex.extras(payload)["has_5h"] is True


def test_extras(payload):
    assert codex.extras(payload) == {
        "has_5h": False, "credits_balance": "0", "has_credits": False,
        "unlimited_credits": False, "reset_credits": 2,
        "reset_credits_applicable": 0, "limit_reached": False,
    }


@pytest.mark.parametrize(("key", "label"), [
    ("prolite", "Pro Lite"), ("plus", "Plus"), ("pro", "Pro"),
    ("self_serve_business_prolite", "Business Pro Lite"), ("ent26", "Enterprise"),
    ("unknown", ""), ("future_plan", "Future Plan"),
])
def test_plan_label(key, label):
    assert codex.plan_label(key) == label


def test_short_name():
    assert codex.short_name("GPT-5.3-Codex-Spark") == "spark"


def jwt(payload):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'none'})}.{encode(payload)}.sig"


def test_identity():
    auth = {"tokens": {
        "id_token": jwt({"email": "a@b.c", "https://api.openai.com/auth": {
            "chatgpt_plan_type": "prolite", "chatgpt_account_id": "account-123",
        }}),
        "access_token": jwt({"exp": 4102444800}),
    }}
    assert codex.identity(auth) == {
        "email": "a@b.c", "plan": "Pro Lite", "plan_key": "prolite",
        "account_id": "account-123", "exp": 4102444800,
    }


@pytest.mark.parametrize(("exp", "expected"), [(4102444800, False), (1, True)])
def test_expiring(exp, expected):
    assert codex.expiring({"tokens": {"access_token": jwt({"exp": exp})}}) is expected


def test_attempt_url():
    attempt = codex.Attempt("verifier", "state", "work")
    url = urlsplit(attempt.url)
    assert f"{url.scheme}://{url.netloc}{url.path}" == codex.AUTHORIZE_URL
    challenge = base64.urlsafe_b64encode(hashlib.sha256(b"verifier").digest())
    assert parse_qs(url.query) == {key: [value] for key, value in {
        "response_type": "code", "client_id": codex.CLIENT_ID,
        "redirect_uri": "http://localhost:1455/auth/callback", "scope": codex.SCOPES,
        "code_challenge": challenge.decode().rstrip("="), "code_challenge_method": "S256",
        "id_token_add_organizations": "true", "codex_cli_simplified_flow": "true",
        "state": "state", "originator": "codex_cli_rs",
    }.items()}


@pytest.mark.parametrize(("code", "state", "message"), [
    ("", "state", "no code"), ("code", "wrong", "different sign-in"),
    ("code", "", "different sign-in"),
])
def test_finish_refuses_invalid_callback(monkeypatch, code, state, message):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid callbacks must not exchange a code")
    monkeypatch.setattr(codex, "_post", unexpected)
    blob, error = codex.finish(codex.Attempt("verifier", "state", "work"), code, state)
    assert blob is None
    assert message in error


def test_ensure_account_dir():
    home = Path(codex.DEFAULT_HOME)
    home.mkdir()
    for name in ("auth.json", "config.toml", "history.jsonl", "auth.json.lock"):
        (home / name).write_text("fixture")
    (home / "sessions").mkdir()
    (home / "refresh.lock").mkdir()
    slot = Path(codex.ensure_account_dir("work"))
    assert {p.name for p in slot.iterdir()} == {"config.toml", "history.jsonl", "sessions"}
    for name in ("config.toml", "history.jsonl", "sessions"):
        assert (slot / name).is_symlink()
        assert (slot / name).resolve() == home / name
    (slot / "auth.json").write_text("own login")
    (home / "new.toml").write_text("new setting")
    codex.ensure_account_dir("work")
    assert (slot / "new.toml").is_symlink()
    assert (slot / "auth.json").read_text() == "own login"


def test_adopt_default_once():
    home = Path(codex.DEFAULT_HOME)
    home.mkdir()
    (home / "auth.json").write_text("{}")
    assert codex.adopt_default() == "codex"
    slot = Path(codex.slot_dir("codex"))
    assert slot.is_symlink()
    assert slot.resolve() == home
    stamp = slot.lstat().st_ino
    assert codex.adopt_default() is None
    assert slot.lstat().st_ino == stamp
    assert codex.account_names() == ["codex"]


def test_remove_symlink_leaves_target():
    target = Path(codex.DEFAULT_HOME)
    target.mkdir()
    (target / "auth.json").write_text("keep")
    slot = Path(codex.slot_dir("work"))
    slot.parent.mkdir()
    slot.symlink_to(target, target_is_directory=True)
    assert codex.remove_account("work") is True
    assert not slot.is_symlink()
    assert (target / "auth.json").read_text() == "keep"


def _credit(cid, expires, status="available", supported=True):
    return {"id": cid, "status": status, "is_supported_by_plan": supported,
            "expires_at": expires, "reset_type": "codex_rate_limits"}


def test_spendable_credits_soonest_first():
    details = {"credits": [
        _credit("late", "2026-10-05T00:00:00Z"),
        _credit("spent", "2026-09-01T00:00:00Z", status="redeemed"),
        _credit("soon", "2026-09-21T00:00:00Z"),
        _credit("wrong-plan", "2026-09-10T00:00:00Z", supported=False),
    ]}
    assert [c["id"] for c in codex.spendable_credits(details)] == ["soon", "late"]
    assert codex.spendable_credits({}) == []


def test_consume_sends_the_cli_request(monkeypatch):
    calls = []

    def call(auth, url, body=None):
        calls.append((url, body))
        return {"ok": True}

    monkeypatch.setattr(codex, "_call", call)
    auth = {"tokens": {"access_token": "t", "account_id": "acct"}}
    codex.consume_reset_credit(auth, "RateLimitResetCredit_1")
    codex.consume_reset_credit(auth)
    assert calls[0][0] == codex.RESET_CREDITS_URL + "/consume"
    assert calls[0][1]["credit_id"] == "RateLimitResetCredit_1"
    assert set(calls[1][1]) == {"redeem_request_id"}
    # A fresh idempotency id per spend: reusing one would make the second
    # click a no-op instead of a second reset.
    assert calls[0][1]["redeem_request_id"] != calls[1][1]["redeem_request_id"]
