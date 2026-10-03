"""A stand-in for Anthropic's and OpenAI's servers, on a loopback port.

One HTTP server plays both hosts, told apart by path: the Claude sign-in page,
the Claude token endpoint and API, and the Codex sign-in, token and usage
endpoints. Every request is recorded, and a test can script what the next
request to a path gets back (a 429 with Retry-After, a 401, a different plan).

Payload shapes mirror what src/ reads and what tests/fakes.py serves. Nothing
here is reachable from outside this machine: the server binds 127.0.0.1 on a
port the kernel picks.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

# Where the hosted Claude flow sends the code for pasting. A redirect there
# would leave this machine, so the fake shows the code on a page instead.
HOSTED_CALLBACK = "https://platform.claude.com/oauth/code/callback"

CLAUDE_LIMITS = [
    {"kind": "session", "percent": 58, "resets_at": "2100-01-01T00:00:00Z"},
    {"kind": "weekly_all", "percent": 71, "resets_at": "2100-01-03T00:00:00Z"},
    {"kind": "weekly_scoped", "percent": 34, "resets_at": "2100-01-03T00:00:00Z",
     "scope": {"model": {"display_name": "Fable"}}},
]

CODEX_USAGE = {
    "plan_type": "prolite",
    "rate_limit": {
        "allowed": True, "limit_reached": False,
        "primary_window": {
            "used_percent": 62, "limit_window_seconds": 604800,
            "reset_after_seconds": 240764, "reset_at": 4102444800,
        },
        "secondary_window": None,
    },
    "additional_rate_limits": [],
    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
}

_PAGE_STYLE = "font:15px -apple-system,sans-serif;margin:4rem auto;max-width:28rem"


def jwt(payload: dict) -> str:
    """An unsigned JWT: ccm reads the claims and never checks a signature."""
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(payload)}.sig"


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


@dataclass
class ClaudeAccount:
    email: str
    tier: str = "default_claude_max_5x"
    display_name: str = ""
    org_uuid: str = ""
    has_extra_usage: bool = False
    limits: list[dict] = field(default_factory=lambda: json.loads(json.dumps(CLAUDE_LIMITS)))
    # Limit resets (Claude Code's /limit-reset): the `cedar_ember` block.
    grants: list[dict] = field(default_factory=list)
    eligible: bool = True

    def profile(self) -> dict:
        return {
            "account": {"uuid": "acct-" + hashlib.sha1(self.email.encode()).hexdigest()[:12],
                        "email": self.email,
                        "display_name": self.display_name or self.email.split("@")[0],
                        "full_name": self.display_name or self.email.split("@")[0],
                        "created_at": "2025-01-01T00:00:00Z"},
            "organization": {"uuid": self.org_uuid, "name": f"{self.email}'s Organization",
                             "organization_type": "claude_max",
                             "billing_type": "stripe_subscription",
                             "rate_limit_tier": self.tier,
                             "has_extra_usage_enabled": self.has_extra_usage,
                             "subscription_created_at": "2025-01-01T00:00:00Z",
                             "seat_tier": None},
        }

    def usage(self, with_resets: bool) -> dict:
        data = {"limits": json.loads(json.dumps(self.limits))}
        if with_resets:
            grants = json.loads(json.dumps(self.grants))
            data["cedar_ember"] = {"eligible": self.eligible, "grants": grants,
                                   "next_grant_id": grants[0]["id"] if grants else None}
        return data


@dataclass
class CodexAccount:
    email: str
    plan: str = "prolite"
    account_id: str = ""
    usage: dict = field(default_factory=lambda: json.loads(json.dumps(CODEX_USAGE)))
    credits: list[dict] = field(default_factory=list)

    def usage_payload(self) -> dict:
        data = json.loads(json.dumps(self.usage))
        data["email"] = self.email
        data["plan_type"] = self.plan
        available = [c for c in self.credits if c.get("status") == "available"]
        data["rate_limit_reset_credits"] = {
            "available_count": len(available),
            "applicable_available_count": len(
                [c for c in available if c.get("is_supported_by_plan", True)]),
        }
        return data


@dataclass
class Recorded:
    """One request, as the server saw it."""
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes
    service: str

    @property
    def json(self) -> dict | None:
        try:
            data = json.loads(self.body)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    @property
    def form(self) -> dict[str, str]:
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.body.decode()).items()}

    @property
    def token(self) -> str:
        auth = self.headers.get("authorization", "")
        return auth[len("Bearer "):] if auth.startswith("Bearer ") else ""


@dataclass
class Scripted:
    method: str | None
    prefix: str
    status: int
    headers: dict[str, str]
    body: bytes
    times: int


class FakeServer:
    """Both fake hosts. Start with `start()`, stop with `close()`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.requests: list[Recorded] = []
        self._scripted: list[Scripted] = []
        self.claude: dict[str, ClaudeAccount] = {}
        self.codex: dict[str, CodexAccount] = {}
        # Who the "browser" is signed in as on each side. None means: whoever
        # the sign-in page was asked for (login_hint), else the only account.
        self.browser: str | None = None
        self.codex_browser: str | None = None
        self.deny_next_authorize = False
        self.authorizations: list[dict] = []
        self._codes: dict[str, dict] = {}
        self._access: dict[str, str] = {}             # access token -> email
        self._refresh: dict[str, tuple[str, bool]] = {}   # refresh token -> (email, spent)
        self._codex_access: dict[str, str] = {}
        self._codex_refresh: dict[str, tuple[str, bool]] = {}
        self._gen: dict[str, int] = {}
        self.access_lifetime = 3600
        self.refresh_lifetime = 30 * 86400
        self.pokes: list[dict] = []
        self.resets: list[dict] = []
        self._server: http.server.ThreadingHTTPServer | None = None

    # ------------------------------------------------------------- lifecycle

    def start(self) -> FakeServer:
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802
                outer._handle(self)

            def do_POST(self):  # noqa: N802
                outer._handle(self)

            def log_message(self, *_a):
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()
        return self

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def port(self) -> int:
        assert self._server is not None, "server not started"
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def env(self) -> dict[str, str]:
        """The CCM_* variables that point ccm at this server."""
        return {
            "CCM_API_BASE": self.url,
            "CCM_TOKEN_URL": self.url + "/v1/oauth/token",
            "CCM_AUTHORIZE_URL": self.url + "/cai/oauth/authorize",
            "CCM_CODEX_AUTH_BASE": self.url,
            "CCM_CODEX_API_BASE": self.url,
        }

    # ------------------------------------------------------------- accounts

    def add_claude(self, email: str, **fields) -> ClaudeAccount:
        acct = ClaudeAccount(email=email, **fields)
        if not acct.org_uuid:
            d = hashlib.sha1(email.encode()).hexdigest()
            acct.org_uuid = f"{d[:8]}-{d[8:12]}-{d[12:16]}-{d[16:20]}-{d[20:32]}"
        with self._lock:
            self.claude[email] = acct
        return acct

    def add_codex(self, email: str, **fields) -> CodexAccount:
        acct = CodexAccount(email=email, **fields)
        if not acct.account_id:
            acct.account_id = "account-" + hashlib.sha1(email.encode()).hexdigest()[:10]
        with self._lock:
            self.codex[email] = acct
        return acct

    def issue(self, email: str) -> dict:
        """A fresh Claude token pair for an account, the way the token endpoint grants one."""
        with self._lock:
            if email not in self.claude:
                raise KeyError(f"no Claude account {email}")
            gen = self._gen[email] = self._gen.get(email, 0) + 1
            access, refresh = f"at-{email}-{gen}-{secrets.token_hex(4)}", \
                f"rt-{email}-{gen}-{secrets.token_hex(4)}"
            self._access[access] = email
            self._refresh[refresh] = (email, False)
        return {"access_token": access, "refresh_token": refresh,
                "token_type": "Bearer", "expires_in": self.access_lifetime,
                "refresh_token_expires_in": self.refresh_lifetime,
                "scope": "user:profile user:inference user:sessions:claude_code "
                         "user:mcp_servers user:file_upload",
                "account": {"uuid": self.claude[email].profile()["account"]["uuid"],
                            "email_address": email},
                "organization": {"uuid": self.claude[email].org_uuid}}

    def blob(self, email: str, expires_in: int | None = None) -> dict:
        """A keychain credential for an account, for seeding a signed-in slot."""
        grant = self.issue(email)
        tier = self.claude[email].tier
        now = time.time()
        life = self.access_lifetime if expires_in is None else expires_in
        return {"accessToken": grant["access_token"], "refreshToken": grant["refresh_token"],
                "expiresAt": int((now + life) * 1000),
                "refreshTokenExpiresAt": int((now + self.refresh_lifetime) * 1000),
                "scopes": sorted(grant["scope"].split()),
                "subscriptionType": "max" if "max" in tier else "pro" if "pro" in tier else "",
                "rateLimitTier": tier}

    def issue_codex(self, email: str) -> dict:
        """A fresh Codex token set: id token with the claims ccm reads."""
        with self._lock:
            acct = self.codex[email]
            gen = self._gen[f"codex:{email}"] = self._gen.get(f"codex:{email}", 0) + 1
            exp = int(time.time()) + self.access_lifetime
            claims = {"https://api.openai.com/auth": {"chatgpt_plan_type": acct.plan,
                                                      "chatgpt_account_id": acct.account_id}}
            access = jwt({**claims, "exp": exp, "sub": email, "gen": gen,
                          "nonce": secrets.token_hex(4)})
            refresh = f"crt-{email}-{gen}-{secrets.token_hex(4)}"
            self._codex_access[access] = email
            self._codex_refresh[refresh] = (email, False)
        return {"access_token": access, "refresh_token": refresh,
                "id_token": jwt({**claims, "email": email, "exp": exp}),
                "token_type": "Bearer", "expires_in": self.access_lifetime}

    def codex_auth(self, email: str) -> dict:
        """An auth.json for an account, as the Codex CLI writes one."""
        tokens = self.issue_codex(email)
        return {"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
                "tokens": {"id_token": tokens["id_token"], "access_token": tokens["access_token"],
                           "refresh_token": tokens["refresh_token"],
                           "account_id": self.codex[email].account_id},
                "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime())}

    def revoke(self, email: str) -> None:
        """Every token of a Claude account stops working: 401s and invalid_grant."""
        with self._lock:
            for token in [t for t, e in self._access.items() if e == email]:
                del self._access[token]
            for token, (owner, _spent) in list(self._refresh.items()):
                if owner == email:
                    self._refresh[token] = (owner, True)

    # ------------------------------------------------------------- scripting

    def script(self, prefix: str, status: int, body: dict | bytes | str | None = None,
               headers: dict[str, str] | None = None, method: str | None = None,
               times: int = 1) -> None:
        """Answer the next `times` requests whose path starts with `prefix` this way."""
        if body is None:
            raw = b""
        elif isinstance(body, bytes):
            raw = body
        elif isinstance(body, str):
            raw = body.encode()
        else:
            raw = json.dumps(body).encode()
        with self._lock:
            self._scripted.append(Scripted(method, prefix, status, dict(headers or {}), raw, times))

    def calls(self, prefix: str = "", method: str | None = None) -> list[Recorded]:
        with self._lock:
            return [r for r in self.requests if r.path.startswith(prefix)
                    and (method is None or r.method == method)]

    def clear(self) -> None:
        with self._lock:
            self.requests.clear()

    # ------------------------------------------------------------- dispatch

    def _handle(self, h: http.server.BaseHTTPRequestHandler) -> None:
        parsed = urllib.parse.urlparse(h.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length) if length else b""
        path = parsed.path
        service = "openai" if (path.startswith("/oauth/") or path.startswith("/backend-api/")) \
            else "anthropic"
        rec = Recorded(h.command, path, query, {k.lower(): v for k, v in h.headers.items()},
                       body, service)
        with self._lock:
            self.requests.append(rec)
            scripted = next((s for s in self._scripted if s.prefix and path.startswith(s.prefix)
                             and (s.method is None or s.method == h.command)), None)
            if scripted is not None:
                scripted.times -= 1
                if scripted.times <= 0:
                    self._scripted.remove(scripted)
        if scripted is not None:
            self._reply(h, scripted.status, scripted.body, scripted.headers)
            return
        try:
            route = self._route(h.command, path)
            if route is None:
                self._json(h, 404, {"error": "not found", "path": path})
            else:
                route(h, rec)
        except Exception as e:  # noqa: BLE001 - a bug in the fake must show as a 500, not a hang
            self._json(h, 500, {"error": f"fake server: {e!r}"})

    def _route(self, method: str, path: str):
        table = {
            ("GET", "/cai/oauth/authorize"): self._authorize_page,
            ("GET", "/cai/oauth/approve"): self._approve,
            ("POST", "/v1/oauth/token"): self._claude_token,
            ("GET", "/api/oauth/profile"): self._profile,
            ("GET", "/api/oauth/usage"): self._usage,
            ("POST", "/v1/messages"): self._messages,
            ("GET", "/oauth/authorize"): self._authorize_page,
            ("GET", "/oauth/approve"): self._approve,
            ("POST", "/oauth/token"): self._codex_token,
            ("GET", "/backend-api/wham/usage"): self._codex_usage,
            ("GET", "/backend-api/wham/rate-limit-reset-credits"): self._codex_credits,
            ("POST", "/backend-api/wham/rate-limit-reset-credits/consume"): self._codex_consume,
        }
        if (method, path) in table:
            return table[(method, path)]
        if method == "POST" and path.startswith("/api/organizations/") \
                and path.endswith("/reset_rate_limits"):
            return self._reset_rate_limits
        return None

    # ------------------------------------------------------------- replies

    def _reply(self, h, status: int, body: bytes, headers: dict[str, str] | None = None,
               content_type: str = "application/json") -> None:
        h.send_response(status)
        sent = {k.lower() for k in (headers or {})}
        for key, value in (headers or {}).items():
            h.send_header(key, value)
        if "content-type" not in sent:
            h.send_header("Content-Type", content_type)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(body)

    def _json(self, h, status: int, data: dict, headers: dict[str, str] | None = None) -> None:
        self._reply(h, status, json.dumps(data).encode(), headers)

    def _html(self, h, status: int, body: str) -> None:
        self._reply(h, status, body.encode(), content_type="text/html; charset=utf-8")

    def _unauthorized(self, h) -> None:
        self._json(h, 401, {"type": "error", "error": {"type": "authentication_error",
                                                      "message": "Invalid authentication"}})

    def _claude_email(self, h, rec: Recorded) -> str | None:
        with self._lock:
            email = self._access.get(rec.token)
        if not email:
            self._unauthorized(h)
        return email

    def _codex_email(self, h, rec: Recorded) -> str | None:
        with self._lock:
            email = self._codex_access.get(rec.token)
        if not email:
            self._json(h, 401, {"detail": "Unauthorized"})
        return email

    # ------------------------------------------------------------- sign-in

    def _authorize_page(self, h, rec: Recorded) -> None:
        """The sign-in page: one button, which approves the request.

        A browser (or a test) lands here from the URL ccm opened. The button
        goes to /approve, which issues the code and sends it to the
        redirect_uri the request named, exactly as the real page would.
        """
        codex = rec.path.startswith("/oauth/")
        q = rec.query
        accounts = self.codex if codex else self.claude
        hint = q.get("login_hint", "")
        with self._lock:
            chosen = self.codex_browser if codex else self.browser
            if not chosen:
                chosen = hint if hint in accounts else next(iter(accounts), "")
            sid = secrets.token_urlsafe(12)
            self.authorizations.append({
                "id": sid, "service": "openai" if codex else "anthropic", "params": dict(q),
                "redirect_uri": q.get("redirect_uri", ""), "state": q.get("state", ""),
                "challenge": q.get("code_challenge", ""),
                "method": q.get("code_challenge_method", ""), "login_hint": hint,
                "email": chosen, "approved": False, "code": "",
            })
        missing = [k for k in ("client_id", "redirect_uri", "state", "code_challenge")
                   if not q.get(k)]
        if missing:
            self._html(h, 400, f"<!doctype html><title>Bad request</title>"
                               f"<p>missing {', '.join(missing)}")
            return
        approve = ("/oauth/approve" if codex else "/cai/oauth/approve") + "?sid=" + sid
        who = chosen or "nobody (no account on the fake server)"
        self._html(h, 200, (
            "<!doctype html><meta charset=utf-8>"
            f"<title>{'OpenAI' if codex else 'Claude'} sign in (fake)</title>"
            f"<body style=\"{_PAGE_STYLE}\">"
            f"<h2>Authorize {'Codex' if codex else 'Claude Code'}?</h2>"
            f"<p>Signed in as <b id=\"who\">{who}</b>.</p>"
            f"<p><a id=\"authorize\" href=\"{approve}\" "
            "style=\"display:inline-block;padding:.6rem 1.2rem;background:#000;color:#fff;"
            "border-radius:6px;text-decoration:none\">Authorize</a></p>"
        ))

    def _approve(self, h, rec: Recorded) -> None:
        sid = rec.query.get("sid", "")
        with self._lock:
            auth = next((a for a in self.authorizations if a["id"] == sid), None)
            if auth is None:
                self._html(h, 404, "<!doctype html><title>Unknown sign-in</title>")
                return
            deny = self.deny_next_authorize
            self.deny_next_authorize = False
            if deny:
                params = {"error": "access_denied",
                          "error_description": "The user denied the request",
                          "state": auth["state"]}
            elif not auth["email"]:
                params = {"error": "server_error",
                          "error_description": "No account is signed in on the fake server",
                          "state": auth["state"]}
            else:
                code = "code-" + secrets.token_urlsafe(16)
                auth.update(approved=True, code=code)
                self._codes[code] = {**auth, "used": False}
                params = {"code": code, "state": auth["state"]}
        target = auth["redirect_uri"]
        if target == HOSTED_CALLBACK:
            # The hosted page shows `code#state` for pasting. The fake does
            # the same rather than leaving the machine.
            shown = f"{params.get('code', '')}#{params['state']}" if "code" in params \
                else params.get("error_description", "denied")
            self._html(h, 200, (
                "<!doctype html><meta charset=utf-8><title>Your code</title>"
                f"<body style=\"{_PAGE_STYLE}\"><h2>Paste this code into ccm</h2>"
                f"<p><code id=\"code\">{shown}</code></p>"))
            return
        location = target + ("&" if "?" in target else "?") + urllib.parse.urlencode(params)
        self._reply(h, 302, b"", {"Location": location})

    def _claude_token(self, h, rec: Recorded) -> None:
        body = rec.json or rec.form
        grant = body.get("grant_type")
        if grant == "authorization_code":
            with self._lock:
                pending = self._codes.get(body.get("code", ""))
                if pending is None or pending["used"]:
                    self._json(h, 400, {"error": "invalid_grant",
                                        "error_description": "Invalid authorization code"})
                    return
                if (pending["redirect_uri"] != body.get("redirect_uri")
                        or _challenge(body.get("code_verifier", "")) != pending["challenge"]
                        or body.get("state", pending["state"]) != pending["state"]):
                    self._json(h, 400, {"error": "invalid_grant",
                                        "error_description": "PKCE verification failed"})
                    return
                pending["used"] = True
                email = pending["email"]
            self._json(h, 200, self.issue(email))
            return
        if grant == "refresh_token":
            with self._lock:
                owner = self._refresh.get(body.get("refresh_token", ""))
                if owner is None or owner[1]:
                    self._json(h, 400, {"error": "invalid_grant",
                                        "error_description": "Refresh token is invalid or expired"})
                    return
                self._refresh[body["refresh_token"]] = (owner[0], True)
            self._json(h, 200, self.issue(owner[0]))
            return
        self._json(h, 400, {"error": "unsupported_grant_type"})

    def _codex_token(self, h, rec: Recorded) -> None:
        body = rec.form if rec.headers.get("content-type", "").startswith(
            "application/x-www-form-urlencoded") else (rec.json or {})
        grant = body.get("grant_type")
        if grant == "authorization_code":
            with self._lock:
                pending = self._codes.get(body.get("code", ""))
                if pending is None or pending["used"] or pending["service"] != "openai":
                    self._json(h, 400, {"error": "invalid_grant"})
                    return
                if (pending["redirect_uri"] != body.get("redirect_uri")
                        or _challenge(body.get("code_verifier", "")) != pending["challenge"]):
                    self._json(h, 400, {"error": "invalid_grant"})
                    return
                pending["used"] = True
                email = pending["email"]
            self._json(h, 200, self.issue_codex(email))
            return
        if grant == "refresh_token":
            with self._lock:
                owner = self._codex_refresh.get(body.get("refresh_token", ""))
                if owner is None or owner[1]:
                    self._json(h, 400, {"error": "invalid_grant"})
                    return
                self._codex_refresh[body["refresh_token"]] = (owner[0], True)
            self._json(h, 200, self.issue_codex(owner[0]))
            return
        self._json(h, 400, {"error": "unsupported_grant_type"})

    # ------------------------------------------------------------- claude api

    def _profile(self, h, rec: Recorded) -> None:
        email = self._claude_email(h, rec)
        if email:
            self._json(h, 200, self.claude[email].profile())

    def _usage(self, h, rec: Recorded) -> None:
        email = self._claude_email(h, rec)
        if email:
            self._json(h, 200, self.claude[email].usage(rec.query.get("cedar_ember") == "1"))

    def _messages(self, h, rec: Recorded) -> None:
        """The poke. Starts every window that has no clock, as a real request would."""
        email = self._claude_email(h, rec)
        if not email:
            return
        body = rec.json or {}
        if rec.headers.get("anthropic-version") is None:
            self._json(h, 400, {"type": "error", "error": {
                "type": "invalid_request_error",
                "message": "anthropic-version header is required"}})
            return
        with self._lock:
            self.pokes.append({"email": email, "model": body.get("model")})
            now = time.time()
            for lim in self.claude[email].limits:
                if not lim.get("resets_at"):
                    span = 18000 if lim.get("kind") == "session" else 604800
                    lim["resets_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + span))
        self._json(h, 200, {"id": "msg_" + secrets.token_hex(6), "type": "message",
                            "role": "assistant", "model": body.get("model"),
                            "content": [{"type": "text", "text": "ok"}],
                            "stop_reason": "max_tokens", "stop_sequence": None,
                            "usage": {"input_tokens": 22, "output_tokens": 1}})

    def _reset_rate_limits(self, h, rec: Recorded) -> None:
        email = self._claude_email(h, rec)
        if not email:
            return
        acct = self.claude[email]
        org = rec.path.split("/")[3]
        body = rec.json or {}
        with self._lock:
            self.resets.append({"email": email, "org": org, **body})
            if org != acct.org_uuid:
                self._json(h, 403, {"error": "forbidden"})
                return
            grant = next((g for g in acct.grants if g.get("id") == body.get("grant_id")), None)
            if grant is None or int(grant.get("resets_left") or 0) <= 0:
                self._json(h, 200, {"result": "already_used"})
                return
            if grant.get("use_requires_limit") and not any(
                    float(lim.get("percent") or 0) >= 100 for lim in acct.limits):
                self._json(h, 200, {"result": "not_limited"})
                return
            grant["resets_left"] = int(grant["resets_left"]) - 1
            for lim in acct.limits:
                lim["percent"] = 0
            left = sum(int(g.get("resets_left") or 0) for g in acct.grants)
        self._json(h, 200, {"result": "reset", "resets_left": left})

    # ------------------------------------------------------------- codex api

    def _codex_usage(self, h, rec: Recorded) -> None:
        email = self._codex_email(h, rec)
        if email:
            self._json(h, 200, self.codex[email].usage_payload())

    def _codex_credits(self, h, rec: Recorded) -> None:
        email = self._codex_email(h, rec)
        if email:
            self._json(h, 200, {"credits": json.loads(json.dumps(self.codex[email].credits))})

    def _codex_consume(self, h, rec: Recorded) -> None:
        email = self._codex_email(h, rec)
        if not email:
            return
        body = rec.json or {}
        acct = self.codex[email]
        with self._lock:
            self.resets.append({"email": email, **body})
            wanted = body.get("credit_id")
            credit = next((c for c in acct.credits if c.get("status") == "available"
                           and (not wanted or c.get("id") == wanted)), None)
            if credit is None:
                self._json(h, 400, {"detail": "No reset credit available"})
                return
            credit["status"] = "redeemed"
            rate = acct.usage.get("rate_limit") or {}
            for key in ("primary_window", "secondary_window"):
                if rate.get(key):
                    rate[key]["used_percent"] = 0
        self._json(h, 200, {"status": "ok", "credit_id": credit.get("id")})
