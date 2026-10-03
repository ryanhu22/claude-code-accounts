"""Signing an account in, the way Claude Code does.

An OAuth PKCE flow with its sign-in page on claude.com, the same page Claude
Code's "Claude account with subscription" option opens. The token endpoint
stays on platform.claude.com. The user's browser does the signing in; we
exchange the resulting code for a token pair and write it where that account
lives.

The one rule here is that a credential is never invented. Every field of the
stored blob either comes from the token response or from `/api/oauth/profile`,
and the result is checked against the live API before it is written. A blob
missing `subscriptionType` or `rateLimitTier` still authenticates, but Claude
Code then opens the session as "API Usage Billing" instead of the user's Max
plan, which is a confusing way to fail and worth refusing outright.
"""
from __future__ import annotations

import base64
import hashlib
import html
import http.server
import os
import secrets
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field

# The test harness points this at a page of its own (tests/e2e). Unset, it is
# the real sign-in page.
AUTHORIZE_URL = os.environ.get("CCM_AUTHORIZE_URL") or "https://claude.com/cai/oauth/authorize"
CALLBACK_URL = "https://platform.claude.com/oauth/code/callback"

# The scope set a real Claude Code login carries. Ask for exactly these: a
# narrower set produces a credential that runs sessions but cannot read the
# profile, which is how a login ends up unable to report its own plan.
SCOPES = ("user:profile", "user:inference", "user:sessions:claude_code",
          "user:mcp_servers", "user:file_upload")


_PAGE_STYLE = ("font:15px -apple-system,sans-serif;line-height:1.5;"
               "margin:4rem auto;max-width:28rem;padding:0 1rem")


def page(title: str, heading: str, *paragraphs: str) -> bytes:
    """A page the browser lands on: plain HTML, no script, nothing fetched.

    Text is escaped, because a paragraph may carry a message a server sent.
    `color-scheme` lets the browser draw the page in the colours of the
    system theme, so it reads the same in dark mode as in light.
    """
    body = "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)
    return (f"<!doctype html><meta charset=utf-8>"
            f"<meta name=\"color-scheme\" content=\"light dark\">"
            f"<title>{html.escape(title)}</title>"
            f"<body style=\"{_PAGE_STYLE}\">"
            f"<h2>{html.escape(heading)}</h2>{body}").encode()


_GO_BACK = "You can close this tab and go back to the app."
_TRY_AGAIN = "To try again, go back to the app and start the sign-in again."

DONE_PAGE = page("Signed in", "Signed in.", _GO_BACK)
WRONG_SIGN_IN_PAGE = page("Wrong sign-in", "This page does not belong to the sign-in in progress.",
                          "To sign in, go back to the app and start again.")
NOT_FOUND_PAGE = page("Not found", "Not found.")


def done_page(message: str) -> bytes:
    return page("Signed in", "Signed in.", message, _GO_BACK)


def failed_page(message: str) -> bytes:
    return page("Sign-in did not finish", "The sign-in did not finish.", message, _TRY_AGAIN)


def describe_error(error: str, description: str) -> str:
    """What the sign-in page said instead of a code, in a sentence."""
    if error == "access_denied":
        return "The sign-in page reported that the request was not allowed."
    detail = description or error
    return f"The sign-in page sent an error instead of a code: {detail}."


CALLBACK_PATH = "/callback"


class Callback:
    """A one-shot local server that catches the redirect.

    The token endpoint accepts `http://localhost:<port>/callback` for this
    client, so the browser can hand the code straight back and the user never
    copies anything. That also removes the step most likely to go wrong:
    switching accounts part-way through loses the page the code was on.

    The port is reachable by anything on this machine, not only the browser
    tab we opened: a favicon fetch, a prefetch, a page that scans localhost.
    So only `/callback` is served, a request has to carry the `state` of a
    sign-in, and the first real redirect wins. Once `expect()` has been
    called, a redirect for any other sign-in is refused without touching the
    one that is pending. Without it, `finish()` still rejects a code whose
    state does not match, so the worst a stray request can do is waste the
    attempt, not swap in a code of its own.

    The page the browser gets is the outcome, not a promise. With `finish`
    set (a callable taking the code and state and returning `(ok, message)`),
    the redirect runs the exchange before it is answered, so the tab says
    "Signed in." only once the credential is stored, and says what went wrong
    otherwise: a code the token endpoint refused, a profile that could not be
    read, or the sign-in page sending an error instead of a code. `result`
    then holds the same `(ok, message)` for the caller. Without `finish`, the
    redirect is only recorded in `code`, `state` and `error`.
    """

    def __init__(self, port: int = 0, path: str = CALLBACK_PATH) -> None:
        self.code = self.state = self.error = ""
        self.expected_state = ""
        self.finish: Callable[[str, str], tuple[bool, str]] | None = None
        self.result: tuple[bool, str] | None = None
        self.path = path
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            # A connection that never sends its request (a port scanner, a
            # stalled prefetch) is dropped, so `close()` never waits on it.
            timeout = 30

            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != outer.path:
                    self._reply(404, NOT_FOUND_PAGE)
                    return
                got = urllib.parse.parse_qs(parsed.query)
                state = (got.get("state") or [""])[0]
                # The authorize page always echoes the state, so a request
                # without one, or with someone else's, is not the redirect.
                if not state or (outer.expected_state and state != outer.expected_state):
                    self._reply(400, WRONG_SIGN_IN_PAGE)
                    return
                with outer._lock:
                    first = not outer._taken
                    outer._taken = True
                if not first:
                    # A reload of the tab, a second tab, or a late arrival.
                    # The caller may be reading `code` right now, so nothing
                    # is overwritten: this tab waits for the outcome the
                    # first request is producing and shows the same page.
                    outer._done.wait(120)
                    self._reply(*outer._outcome_page())
                    return
                try:
                    outer.code = (got.get("code") or [""])[0]
                    outer.state = state
                    error = (got.get("error") or [""])[0]
                    description = (got.get("error_description") or [""])[0]
                    outer.error = description or error
                    if outer.error or not outer.code:
                        outer.result = (False, describe_error(error, description) if error
                                        else "The sign-in page sent no code.")
                    elif outer.finish is not None:
                        try:
                            outer.result = outer.finish(outer.code, outer.state)
                        except Exception as e:  # the page must still answer
                            outer.result = (False, f"The sign-in could not be finished: {e}")
                finally:
                    # Whatever happens to this connection (the tab was closed
                    # or reloaded while the exchange ran), the caller learns
                    # the outcome; a reply that fails is only a lost page.
                    outer._done.set()
                self._reply(*outer._outcome_page())

            def _reply(self, status: int, page: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

            def log_message(self, *_a):
                pass

        # Threaded, so a second visit during the exchange is answered rather
        # than queued behind it and reset when the server closes. Handler
        # threads are not daemons: `close()` joins them, so a page in flight
        # is written before the process moves on.
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._server.daemon_threads = False
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._taken = False
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def _outcome_page(self) -> tuple[int, bytes]:
        """What the tab shows once the redirect has been handled."""
        if self.result is None:
            return 200, DONE_PAGE          # no `finish`: the caller exchanges the code
        ok, message = self.result
        return 200, (done_page(message) if ok else failed_page(message))

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.port}{self.path}"

    def expect(self, state: str) -> None:
        """Name the sign-in this server is waiting for.

        The attempt is created after the server, because the authorize URL
        needs the port, so the state arrives a moment later than the server
        does. Call this with `attempt.state` before the browser opens.
        """
        self.expected_state = state

    def wait(self, timeout: float = 300.0) -> bool:
        return self._done.wait(timeout)

    def close(self) -> None:
        # A tab still waiting for the outcome gets what there is: the caller
        # has stopped waiting, so nothing more is coming.
        self._done.set()
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass


@dataclass
class Attempt:
    """One sign-in in progress: what we sent, so the exchange can prove it."""
    verifier: str
    state: str
    account: str
    redirect_uri: str = CALLBACK_URL
    login_hint: str = ""
    started_at: float = field(default_factory=time.time)

    @property
    def hosted(self) -> bool:
        """Whether the code comes back through Anthropic's page for pasting.

        The only thing that tells the two flows apart is where the code is
        sent: the hosted callback page, or a port on this machine. Nothing
        else about an attempt differs, so this is the one place to ask.
        """
        return self.redirect_uri == CALLBACK_URL

    @property
    def url(self) -> str:
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip("=")
        query = urllib.parse.urlencode({
            # `code=true` asks the hosted page to show the code for pasting.
            # A loopback redirect has no page to show it on; the parameter
            # belongs to the hosted flow alone.
            **({"code": "true"} if self.hosted else {}),
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(SCOPES),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": self.state,
            # Land on the account this slot is for, rather than whichever one
            # the browser happens to be signed into. Switching accounts part
            # way through is what loses the code.
            **({"login_hint": self.login_hint} if self.login_hint else {}),
        })
        return f"{AUTHORIZE_URL}?{query}"


CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def begin(account: str, redirect_uri: str = CALLBACK_URL, login_hint: str = "") -> Attempt:
    """Start a sign-in. The verifier never leaves this process."""
    return Attempt(verifier=secrets.token_urlsafe(64), state=secrets.token_urlsafe(24),
                   account=account, redirect_uri=redirect_uri, login_hint=login_hint)


def split_code(pasted: str) -> tuple[str, str]:
    """The callback shows `code#state`; accept either that or a bare code."""
    pasted = pasted.strip()
    if "#" in pasted:
        code, _, state = pasted.partition("#")
        return code.strip(), state.strip()
    return pasted, ""


def finish(attempt: Attempt, pasted: str, post, profile_result) -> tuple[dict | None, str]:
    """Exchange a pasted code for a complete, verified credential.

    `post` and `profile_result` are passed in rather than imported so this
    module stays free of the http and keychain layers, and so a test can drive
    the whole flow without a network.

    Returns (blob, error). A blob comes back only when the API confirmed who it
    belongs to and what they are paying for.
    """
    code, state = split_code(pasted)
    if not code:
        return None, "no code was pasted"
    if state and state != attempt.state:
        return None, "that code belongs to a different sign-in; start again"
    if not state and not attempt.hosted:
        # A bare code is fine when the user pasted it from the hosted page.
        # On the loopback redirect the authorize page always echoes the
        # state, so a code with none did not come from it: anything on this
        # machine can reach the port and offer a code of its own.
        return None, "that code belongs to a different sign-in; start again"
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": attempt.redirect_uri,
        "client_id": CLIENT_ID,
        "code_verifier": attempt.verifier,
        "state": attempt.state,
    }
    try:
        resp = post(body)
    except Exception as e:
        detail = getattr(e, "reason", None) or str(e)
        return None, f"the code was refused ({detail}). Codes expire quickly; try again"
    access = resp.get("access_token")
    refresh = resp.get("refresh_token")
    if not access or not refresh:
        return None, "the response carried no token pair"

    info, err = profile_result(access)
    email = info.get("email")
    if not email:
        return None, ("signed in, but the profile could not be read"
                      + (f" ({err})" if err else "")
                      + ". Not saving a login that cannot name itself.")
    tier = info.get("tier") or ""
    plan = info.get("plan") or ""
    if not tier:
        return None, (f"signed in as {email}, but the plan could not be read. "
                      "Saving that would open sessions as API billing, so it is refused.")

    now = time.time()
    blob = {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": int((now + float(resp.get("expires_in") or 3600)) * 1000),
        "scopes": sorted((resp.get("scope") or " ".join(SCOPES)).split()),
        "subscriptionType": "max" if "max" in tier else "pro" if "pro" in tier else plan.lower(),
        "rateLimitTier": tier,
    }
    lifetime = resp.get("refresh_token_expires_in") or resp.get("refresh_expires_in")
    if lifetime:
        blob["refreshTokenExpiresAt"] = int((now + float(lifetime)) * 1000)
    return blob, email


# Browsers worth offering by name. The default is always first: signing in
# often needs an account the user is already logged into, and which browser
# holds that session is the whole reason this choice exists.
BROWSERS = (
    ("Default browser", ""),
    ("Google Chrome", "Google Chrome"),
    ("Safari", "Safari"),
    ("Arc", "Arc"),
    ("Firefox", "Firefox"),
    ("Microsoft Edge", "Microsoft Edge"),
    ("Brave", "Brave Browser"),
)


_INSTALLED: list[tuple[str, str]] | None = None


def installed_browsers() -> list[tuple[str, str]]:
    """The offerable browsers that are actually on this Mac.

    Asked once per process. Each answer costs an `open -Ra` subprocess, and
    the menu asks for one list per account row every time it is rebuilt, which
    spent about a quarter of a second per rebuild learning the same thing.
    Someone who installs a browser can restart the app.
    """
    global _INSTALLED
    if _INSTALLED is not None:
        return _INSTALLED
    import subprocess
    out = [BROWSERS[0]]
    for label, app in BROWSERS[1:]:
        try:
            r = subprocess.run(["open", "-Ra", app], capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            out.append((label, app))
    _INSTALLED = out
    return out


def open_in(url: str, app: str = "") -> str:
    """Open a URL, in a named browser or the default one. "" on success."""
    import subprocess
    cmd = ["open", url] if not app else ["open", "-a", app, url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)[:120]
    return "" if r.returncode == 0 else (r.stderr.strip()[:160] or "could not open a browser")
