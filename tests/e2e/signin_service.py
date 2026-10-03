"""Hand sandboxed sign-ins to a browser suite, over HTTP on loopback.

The browser suite under e2e/ starts this as its app: one process for the
run, which the runner stops when the run ends. Each test asks it for a
fresh `ccm login` (a `SignIn` from serve_signin.py, with its own sandbox
and fake server), drives the browser through the URLs it gets back, then
asks how ccm ended. Nothing here touches a real account.

    uv run python tests/e2e/signin_service.py --port 0

    GET    /                       {"ok": true, "signins": N}
    POST   /signins                body: SignIn's keywords as JSON, plus
                                   "sandbox_of": an id whose sandbox and
                                   fake server this sign-in shares
                                   -> {"id": ..., "authorize_url": ..., ...}
                                   or {"id": ..., "exited": true, ...}
    GET    /signins/<id>?wait=S    -> {"done": true, "returncode": ..., ...}
                                   or {"done": false} after S seconds
    POST   /signins/<id>/ccm       body: {"args": ["list"]}
                                   -> {"returncode": ..., "stdout": ..., "stderr": ...}
    DELETE /signins/<id>           kill ccm, drop the sandbox

The process ends with its parent: if the runner dies without stopping it,
it notices within a couple of seconds and cleans every sandbox up itself.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import secrets
import signal
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from e2e.serve_signin import SignIn  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")
KEYWORDS = {"account", "email", "tier", "plan", "codex", "deny", "browser", "seeded",
            "expired_code", "slow_token", "busy_port"}


class Service:
    def __init__(self) -> None:
        self.signins: dict[str, SignIn] = {}
        self.lock = threading.Lock()

    # ------------------------------------------------------------- the API

    def create(self, body: dict) -> dict:
        unknown = set(body) - KEYWORDS - {"sandbox_of"}
        if unknown:
            raise ValueError(f"unknown keys: {sorted(unknown)}")
        kwargs = {k: v for k, v in body.items() if k in KEYWORDS}
        shared = body.get("sandbox_of")
        if shared:
            with self.lock:
                owner = self.signins.get(shared)
            if owner is None:
                raise KeyError(shared)
            kwargs.update(server=owner.server, sandbox=owner.sandbox)
        signin = SignIn(**kwargs)
        sid = secrets.token_urlsafe(8)
        with self.lock:
            self.signins[sid] = signin
        started = signin.start()
        return {"id": sid, **(self._plain(started) if started.get("exited") else started)}

    def status(self, sid: str, wait: float) -> dict:
        signin = self._get(sid)
        if signin.wait(wait):
            return self._plain(signin.outcome())
        return {"done": False}

    def ccm(self, sid: str, args: list[str]) -> dict:
        return self._plain(self._get(sid).ccm(*args))

    def delete(self, sid: str) -> dict:
        with self.lock:
            signin = self.signins.pop(sid, None)
        if signin is None:
            raise KeyError(sid)
        signin.close()
        return {"deleted": sid}

    def close(self) -> None:
        with self.lock:
            signins, self.signins = list(self.signins.values()), {}
        # Shared sandboxes are removed by their owner, which was created first.
        for signin in reversed(signins):
            signin.close()

    def _get(self, sid: str) -> SignIn:
        with self.lock:
            signin = self.signins.get(sid)
        if signin is None:
            raise KeyError(sid)
        return signin

    @staticmethod
    def _plain(result: dict) -> dict:
        """ccm's output with the colours stripped, next to the raw text."""
        return {**result, "plain": ANSI.sub("", result.get("stdout", "")),
                "plain_err": ANSI.sub("", result.get("stderr", ""))}


def serve(port: int) -> int:
    service = Service()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802
            self._handle("GET")

        def do_POST(self):  # noqa: N802
            self._handle("POST")

        def do_DELETE(self):  # noqa: N802
            self._handle("DELETE")

        def _handle(self, method: str) -> None:
            url = urllib.parse.urlsplit(self.path)
            query = dict(urllib.parse.parse_qsl(url.query))
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else {}
                self._json(200, self._route(method, url.path, query, body))
            except KeyError as e:
                self._json(404, {"error": f"no such sign-in: {e}"})
            except (ValueError, TypeError) as e:
                self._json(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001 - the suite must see the reason, not a hang
                self._json(500, {"error": repr(e)})

        def _route(self, method: str, path: str, query: dict, body: dict) -> dict:
            parts = [p for p in path.split("/") if p]
            if method == "GET" and not parts:
                return {"ok": True, "signins": len(service.signins)}
            if parts[:1] != ["signins"]:
                raise KeyError(path)
            if method == "POST" and len(parts) == 1:
                return service.create(body)
            if method == "GET" and len(parts) == 2:
                return service.status(parts[1], float(query.get("wait") or 0))
            if method == "POST" and len(parts) == 3 and parts[2] == "ccm":
                return service.ccm(parts[1], list(body.get("args") or []))
            if method == "DELETE" and len(parts) == 2:
                return service.delete(parts[1])
            raise KeyError(path)

        def _json(self, status: int, data: dict) -> None:
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    stop = threading.Event()

    def shutdown(*_a) -> None:
        stop.set()

    def watch_parent() -> None:
        parent = os.getppid()
        while not stop.wait(2):
            if os.getppid() != parent:
                stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=watch_parent, daemon=True).start()
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                     daemon=True).start()
    print(json.dumps({"url": f"http://127.0.0.1:{server.server_address[1]}"}), flush=True)
    try:
        while not stop.is_set():
            time.sleep(0.2)
    finally:
        server.shutdown()
        server.server_close()
        service.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--port", type=int, default=0, help="loopback port to serve on; 0 picks one")
    args = p.parse_args(argv)
    return serve(args.port)


if __name__ == "__main__":
    sys.exit(main())
