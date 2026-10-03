"""Start a sandboxed `ccm login` and hand its sign-in page to a browser test.

Prints one JSON line as soon as ccm has asked for a browser:

    {"authorize_url": ..., "fake_server": ..., "callback_url": ...,
     "sandbox": ..., "account": ..., "email": ..., "pid": ...}

A browser (Playwright, a person) opens `authorize_url`, presses the
Authorize button, and lands on ccm's own "Signed in." page at
`callback_url`. When ccm exits, or `--timeout` seconds pass, a second JSON
line reports the outcome:

    {"done": true, "returncode": 0, "signed_in": true, "stdout": ..., "stderr": ...}

The exit status is ccm's. The sandbox is removed unless `--keep` is given.
Nothing here touches a real account: see tests/e2e/README.md.

    uv run python tests/e2e/serve_signin.py --account work --email work@example.com
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from e2e.fake_server import FakeServer  # noqa: E402
from e2e.harness import Sandbox  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--account", default="work", help="the ccm account name to sign in")
    p.add_argument("--email", default="work@example.com",
                   help="the account the fake browser session belongs to")
    p.add_argument("--tier", default="default_claude_max_5x")
    p.add_argument("--timeout", type=float, default=300.0,
                   help="seconds to wait for the sign-in to finish")
    p.add_argument("--keep", action="store_true", help="leave the sandbox directory behind")
    args = p.parse_args(argv)

    server = FakeServer().start()
    server.add_claude(args.email, tier=args.tier)
    sandbox = Sandbox(tempfile.mkdtemp(prefix="ccm-e2e-"), server)
    proc = sandbox.popen("login", args.account)
    try:
        url = sandbox.wait_for_url(proc, 0, timeout=30)
        callback = parse_qs(urlsplit(url).query).get("redirect_uri", [""])[0]
        print(json.dumps({"authorize_url": url, "fake_server": server.url,
                          "callback_url": callback, "sandbox": sandbox.root,
                          "account": args.account, "email": args.email, "pid": proc.pid}),
              flush=True)
        deadline = time.monotonic() + args.timeout
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.2)
        if proc.poll() is None:
            proc.kill()
        out, err = proc.communicate()
        blob = sandbox.blob(sandbox.slot(args.account))
        print(json.dumps({"done": True, "returncode": proc.returncode,
                          "signed_in": bool(blob) and proc.returncode == 0,
                          "stdout": out, "stderr": err}), flush=True)
        return proc.returncode if proc.returncode is not None else 1
    except KeyboardInterrupt:
        proc.kill()
        return 130
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        server.close()
        if not args.keep:
            sandbox.remove()


if __name__ == "__main__":
    sys.exit(main())
