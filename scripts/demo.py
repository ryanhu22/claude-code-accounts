#!/usr/bin/env python3
"""Show the CLI and menu bar with fake accounts in a throwaway home."""

import argparse
import base64
import html
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from contextlib import ExitStack, contextmanager, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from claude_code_accounts import (  # noqa: E402
    cli,
    codex,
    core,
    focus,
    keychain,
    profiles,
    sessions,
    transcripts,
)
from fakes import FakeApi, FakeKeychain, redirect_home, session, sign_in  # noqa: E402


class Setter:
    setattr = staticmethod(setattr)


@dataclass
class World:
    home: Path
    api: FakeApi
    keychain: FakeKeychain
    sessions: list[sessions.Session]
    pinned_term: str = "demo-term-notes"


def seed(home) -> World:
    home = Path(home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    if any(home.iterdir()):
        raise ValueError("demo home must be an empty throwaway directory")
    os.environ["HOME"] = str(home)
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "TERM_SESSION_ID"):
        os.environ.pop(name, None)
    redirect_home(str(home), setattr)
    for name, path in (("CCM_HOME", profiles.CCM_HOME), ("CCM_ACCOUNTS_DIR", core.ACCOUNTS_DIR),
                       ("CCM_CODEX_ACCOUNTS_DIR", codex.ACCOUNTS_DIR),
                       ("CCM_SESSION_DIRS", core.SESSION_DIRS)):
        os.environ[name] = path
    core.project_root = lambda path: os.path.abspath(
        os.fspath(path).split("/.claude/worktrees/")[0])
    core._ROOTS = {}
    core._CODEX_ADOPTED = False
    fake, api = FakeKeychain(), FakeApi()
    fake.install(Setter())
    api.install(Setter())
    now = datetime.now(timezone.utc)
    for i, (name, percents, session_reset, weekly_reset) in enumerate((
        ("work", (62, 41, 88), timedelta(hours=1, minutes=48), timedelta(days=2, hours=9)),
        ("personal", (0, 17, 9), None, timedelta(days=5, hours=2)),
        ("client", (95, 73, 100), timedelta(minutes=23), timedelta(days=4, hours=16)),
    )):
        email = f"{name}@example.com"
        limits = []
        for kind, percent, delay in zip(("session", "weekly_all", "weekly_scoped"), percents,
                                        (session_reset, weekly_reset, weekly_reset), strict=True):
            limit = {"kind": kind, "percent": percent}
            if delay is not None:
                limit["resets_at"] = (now + delay).isoformat()
            if kind == "weekly_scoped":
                limit["scope"] = {"model": {"display_name": "Fable"}}
            limits.append(limit)
        api.usage[email] = {"limits": limits}
        slot = sign_in(name, email, api)
        core.set_chip_index(name, i)
        core.identity(slot, keychain.read_credentials(slot))

    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    claims = {"email": "codex@example.com", "exp": int((now + timedelta(days=365)).timestamp()),
              "https://api.openai.com/auth": {
                  "chatgpt_plan_type": "pro", "chatgpt_account_id": "acct_demo"}}
    jwt = f"{encode({'alg': 'none'})}.{encode(claims)}.ZGVtbw"
    Path(codex.DEFAULT_HOME).mkdir()
    codex.write_auth(codex.DEFAULT_HOME, {"tokens": {
        "access_token": jwt, "id_token": jwt, "refresh_token": "demo-refresh",
        "account_id": "acct_demo"}, "last_refresh": now.isoformat()})

    def window(span, percent, delay):
        return {"limit_window_seconds": span, "used_percent": percent,
                "reset_after_seconds": delay, "reset_at": int(now.timestamp()) + delay}

    usage = {"email": "codex@example.com", "plan_type": "pro", "rate_limit": {
        "allowed": True, "limit_reached": False,
        "primary_window": window(18000, 34, 7500),
        "secondary_window": window(604800, 58, 298800)},
        "additional_rate_limits": [{"limit_name": "GPT-5.3-Codex-Spark",
            "metered_feature": "codex_bengalfox", "normal_model_slug": None,
            "rate_limit": {"allowed": True, "limit_reached": False,
                "primary_window": window(18000, 0, 18000),
                "secondary_window": window(604800, 0, 604800)}}],
        "credits": {"has_credits": True, "unlimited": False, "balance": "12.40"},
        "rate_limit_reset_credits": {"available_count": 1, "applicable_available_count": 1}}
    codex.fetch_usage = lambda auth: usage
    codex.running_homes = set

    world = World(home, api, fake, [])
    r = core.rules()
    r.default_account = "personal"
    r.profiles = [profiles.Profile("work", "work", [str(home / "src" / repo)
                                                  for repo in ("acme-api", "acme-web")])]
    r.set_project(str(home / "src/side-project"), "client")
    r.set_session(world.pinned_term, "personal")
    for directory in (*r.profiles[0].repos, str(home / "src/side-project")):
        os.makedirs(directory, exist_ok=True)
    core.save_rules(r)
    digests, totals, environments, branches = {}, {}, {}, {}
    for i, (term, account, repo, name, source, status, branch, title, pct, spent) in enumerate((
        ("api", "work", "acme-api", "acme-api-3f", "derived", "busy", "main",
         "Add rate limiting to the webhook receiver", 0.42,
         (18_400, 96_000, 2_950_000, 41_200, 57)),
        ("web", "work", "acme-web/.claude/worktrees/checkout-flow", "acme-web-9c", "derived",
         "busy", "checkout-flow", "Rebuild the checkout form", 0.71,
         (9_800, 210_000, 6_120_000, 88_900, 131)),
        ("side", "client", "side-project", "side-project-1e", "derived", "idle", "main",
         "Migrate the cron jobs to launchd", 0.18, (4_100, 32_000, 640_000, 12_300, 22)),
        ("notes", "personal", "notes", "weekly review", "user", "idle", "main", "", 0.06,
         (900, 8_000, 71_000, 3_400, 6)),
    )):
        cwd = str(home / "src" / repo)
        os.makedirs(cwd, exist_ok=True)
        s = session(f"demo-term-{term}", cwd, account)
        s.pid, s.session_id = 40001 + i, str(40001 + i)
        s.name, s.name_source, s.status = name, source, status
        s.tty, s.branch, s.title = f"ttys{i + 1:03d}", branch, title
        s.entrypoint, s.term_program, s.model = "cli", "Apple_Terminal", "claude-fable-5-1"
        # Digest reports percentage points against the model's actual context window.
        s.context_tokens, s.context_pct = int(pct * transcripts.window_for(s.model)), pct * 100
        s.spent = transcripts.Totals(*spent)
        s.started_at = now.timestamp() - (i % 3 + 1) * 3600
        s.updated_at = now.timestamp() - 5 - i * 45
        registry = Path(s.config_dir, "sessions", f"{s.pid}.json")
        registry.parent.mkdir(exist_ok=True)
        registry.write_text(json.dumps({"pid": s.pid, "sessionId": s.session_id, "cwd": cwd,
            "name": name, "nameSource": source, "kind": "interactive", "entrypoint": "cli",
            "status": status, "startedAt": int(s.started_at * 1000),
            "updatedAt": int(s.updated_at * 1000)}))
        environments[s.pid] = ({"TERM_SESSION_ID": s.term_id, "TERM_PROGRAM": s.term_program,
                               "CLAUDE_CONFIG_DIR": s.config_dir}, s.tty)
        branches[cwd] = branch
        digests[s.session_id] = transcripts.Digest(title, s.context_tokens, s.model)
        totals[s.session_id] = s.spent
        world.sessions.append(s)
    original_alive = sessions.alive
    sessions.alive = lambda pid: pid in environments or original_alive(pid)
    sessions._environ = lambda pid, proc_start="": environments.get(pid, ({}, ""))
    sessions.branch_of = lambda cwd: branches.get(cwd, "")
    transcripts.find = lambda session_id, roots: session_id
    transcripts.digest = lambda path: digests.get(path, transcripts.Digest())
    transcripts.lifetime = lambda path, save=True: totals.get(path, transcripts.Totals())
    transcripts.flush = lambda: None
    focus.frontmost_bundle_id = lambda: "com.apple.Terminal"
    focus.selected_tty = lambda bundle_id: ("ttys001", "")
    focus.reveal_tab = lambda bundle_id, tty: ""
    core.set_pref("bar_account", "work")
    core.bootstrap()
    core.all_accounts(with_usage=True)
    return world


@contextmanager
def offline(allowed=()):
    def blocked(*args, **kwargs):
        raise OSError("external access is disabled in the demo")

    original = subprocess.Popen

    def launch(args, *positional, **kwargs):
        if not isinstance(args, list) or args[0] not in allowed or kwargs.get("shell"):
            return blocked()
        return original(args, *positional, **kwargs)

    with ExitStack() as stack:
        for module, name, replacement in (
            (urllib.request, "urlopen", blocked), (socket, "create_connection", blocked),
            (socket.socket, "connect", blocked), (socket.socket, "connect_ex", blocked),
            (subprocess, "Popen", launch),
        ):
            stack.enter_context(patch.object(module, name, replacement))
        yield


def ansi_to_html(text):
    styles = {"1": ("weight", "font-weight:600"), "2": ("color", "color:#8e8e93"),
              "31": ("color", "color:#ff453a"), "32": ("color", "color:#30d158"),
              "33": ("color", "color:#ffd60a")}
    active, out = {}, []
    for i, part in enumerate(re.split(r"\x1b\[([0-9;]*)m", text)):
        if i % 2:
            for code in (part or "0").split(";"):
                if code == "0":
                    active.clear()
                elif code in styles:
                    key, style = styles[code]
                    active[key] = style
        elif part:
            escaped = html.escape(part)
            out.append(f'<span style="{";".join(active.values())}">{escaped}</span>'
                       if active else escaped)
    return "".join(out)


def render(command, target):
    """Run headless Chrome until the screenshot lands, then stop it.

    Chrome writes the file within a second or two and then, on some machines,
    never exits on its own. Waiting on the file rather than the process keeps
    the run short either way.
    """
    Path(target).unlink(missing_ok=True)
    proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline, seen = time.monotonic() + 60, -1
        while time.monotonic() < deadline:
            size = Path(target).stat().st_size if Path(target).is_file() else -1
            if size > 0 and size == seen:
                return
            seen = size
            if proc.poll() is not None and size > 0:
                return
            time.sleep(0.3)
        raise RuntimeError(f"Chrome did not write {target} within 60 seconds")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()


def cli_shots(out, chrome):
    with tempfile.TemporaryDirectory(prefix="ccm-demo-html-") as directory:
        for command in ("list", "sessions", "where"):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                cli.main([command])
            output = buffer.getvalue().rstrip("\n")
            height = (len(output.splitlines()) + 1) * 20 + 44
            body = ansi_to_html(f"\x1b[2m$ ccm {command}\x1b[0m\n" + output)
            document = ('<!doctype html><meta charset="utf-8">'
                        '<meta http-equiv="Content-Security-Policy" '
                        'content="default-src \'none\'; style-src \'unsafe-inline\'">'
                        '<style>html{background:transparent}body{margin:0}pre{box-sizing:border-box;'
                        'margin:0;background:#1c1c1e;color:#e6e6e6;padding:22px 26px;'
                        'border-radius:12px;font:13px/20px "SF Mono",Menlo,monospace;'
                        'font-variant-numeric:tabular-nums;overflow:hidden}</style>'
                        f'<pre>{body}</pre>')
            page = Path(directory, f"cli-{command}.html")
            page.write_text(document)
            target = out / f"cli-{command}.png"
            if chrome:
                render([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--force-device-scale-factor=2", f"--window-size=900,{height}",
                    "--disable-background-networking", "--disable-component-update",
                    "--disable-sync", "--use-mock-keychain", "--password-store=basic",
                    "--no-first-run", "--host-resolver-rules=MAP * ~NOTFOUND",
                    f"--user-data-dir={directory}/chrome", f"--screenshot={target}", page.as_uri()],
                    target)
            else:
                target = out / page.name
                shutil.copyfile(page, target)
            print(target, flush=True)
    if not chrome:
        print("Chrome was not found; wrote terminal HTML instead of PNGs.", flush=True)


def load_menubar(home):
    try:
        import AppKit  # noqa: F401
        import rumps.rumps

        from claude_code_accounts import menubar
    except ImportError as error:
        raise SystemExit("The demo menu bar needs the menubar extra (rumps and AppKit).") from error
    support = home / "Library/Application Support/Claude"
    support.mkdir(parents=True, exist_ok=True)
    # Cocoa's directory lookup can bypass HOME, so keep rumps' files here too.
    rumps.rumps.application_support = lambda name: str(support)
    return menubar


def capture(path, window):
    """Save one window of this process by its id, without its shadow.

    A window capture does not care which display the window is on, or
    whether a full-screen app is hiding the menu bar, which a region capture
    of the screen does. It also keeps the window's own transparency.
    """
    path.unlink(missing_ok=True)
    try:
        result = subprocess.run(["screencapture", "-x", "-o", "-l", str(window), str(path)],
                                timeout=60)
        if result.returncode == 0 and path.is_file() and path.stat().st_size:
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    print(f"Could not write {path}; give the terminal Screen Recording permission.", flush=True)
    return False


def windows_of(pid):
    """Every on-screen window this process owns, from the window server."""
    import objc

    bundle = objc.loadBundle("CoreGraphics", {},
                             bundle_path="/System/Library/Frameworks/CoreGraphics.framework")
    functions = {}
    objc.loadBundleFunctions(bundle, functions, [("CGWindowListCopyWindowInfo", b"@II")])
    on_screen_only = 1
    return [dict(w) for w in functions["CGWindowListCopyWindowInfo"](on_screen_only, 0)
            if w.get("kCGWindowOwnerPID") == pid]


def save_image(image, path, scale=2) -> bool:
    """Write an NSImage as a PNG at Retina scale."""
    import AppKit

    if image is None:
        return False
    size = image.size()
    wide, high = int(size.width * scale), int(size.height * scale)
    rep = AppKit.NSBitmapImageRep.alloc(
    ).initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(  # noqa: E501
        None, wide, high, 8, 4, True, False, AppKit.NSCalibratedRGBColorSpace, 0, 0)
    rep.setSize_(size)
    AppKit.NSGraphicsContext.saveGraphicsState()
    try:
        AppKit.NSGraphicsContext.setCurrentContext_(
            AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
        image.drawInRect_fromRect_operation_fraction_(
            ((0, 0), (size.width, size.height)), ((0, 0), (0, 0)),
            AppKit.NSCompositingOperationSourceOver, 1.0)
    finally:
        AppKit.NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    return bool(data and data.writeToFile_atomically_(str(path), True))


def strip(item, target, chrome):
    """Put the captured menu bar item on a dark strip, the way the menu bar shows it.

    The item is drawn for a dark menu bar, so on its own it is white on
    transparent and vanishes on a light page.
    """
    data = base64.b64encode(item.read_bytes()).decode()
    with tempfile.TemporaryDirectory(prefix="ccm-demo-strip-") as directory:
        width, height = 360, 44
        page = Path(directory, "menubar.html")
        page.write_text(
            '<!doctype html><meta charset="utf-8"><style>html{background:transparent}'
            'body{margin:0}div{box-sizing:border-box;display:flex;align-items:center;'
            f'justify-content:flex-end;width:{width}px;height:{height}px;padding:0 14px;'
            'background:linear-gradient(#2c2c2e,#242426);border-radius:10px}'
            'img{height:22px;width:auto}</style>'
            f'<div><img src="data:image/png;base64,{data}"></div>')
        render([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                "--force-device-scale-factor=2", f"--window-size={width},{height}",
                "--disable-background-networking", "--disable-component-update",
                "--disable-sync", "--use-mock-keychain", "--password-store=basic",
                "--no-first-run", "--host-resolver-rules=MAP * ~NOTFOUND",
                f"--user-data-dir={directory}/chrome", f"--screenshot={target}", page.as_uri()],
               target)


def menu_shots(app, out, chrome):
    import AppKit

    def worker():
        try:
            deadline = time.monotonic() + 30
            while not (app._snapshot.accounts and app._snapshot.sessions):
                if time.monotonic() >= deadline:
                    raise TimeoutError("demo menu did not load within 30 seconds")
                time.sleep(0.2)
            time.sleep(1.0)
            ready, found = threading.Event(), {}

            def measure():
                try:
                    button = app._nsapp.nsstatusitem.button()
                    found["item"] = button.window().windowNumber()
                    found["image"] = button.image()
                finally:
                    ready.set()

            queue = AppKit.NSOperationQueue.mainQueue()
            queue.addOperationWithBlock_(measure)
            if not ready.wait(30):
                raise TimeoutError("could not find the demo menu bar item")
            # The status item's window cannot be captured, so save the image
            # the app drew into it. It is the same pixels the menu bar shows.
            with tempfile.TemporaryDirectory(prefix="ccm-demo-item-") as directory:
                item = Path(directory, "item.png")
                if save_image(found["image"], item):
                    if chrome:
                        strip(item, out / "menubar.png", chrome)
                    else:
                        shutil.copyfile(item, out / "menubar.png")
                    print(out / "menubar.png", flush=True)
            # performClick_ stays inside the menu loop until it closes, so
            # nothing else may need the main thread after it.
            queue.addOperationWithBlock_(
                lambda: app._nsapp.nsstatusitem.button().performClick_(None))
            menus, deadline = [], time.monotonic() + 15
            while not menus and time.monotonic() < deadline:
                time.sleep(0.5)
                menus = [w for w in windows_of(os.getpid())
                         if w.get("kCGWindowNumber") != found["item"]]
            if not menus:
                raise RuntimeError("the demo menu did not open")
            time.sleep(1.0)          # let the rows finish drawing
            biggest = max(menus, key=lambda w: w["kCGWindowBounds"]["Width"]
                          * w["kCGWindowBounds"]["Height"])
            if capture(out / "menu.png", biggest["kCGWindowNumber"]):
                print(out / "menu.png", flush=True)
            os._exit(0)
        except Exception as error:
            print(f"Demo screenshots failed: {error}", file=sys.stderr, flush=True)
            os._exit(1)

    threading.Thread(target=worker, daemon=True).start()
    app.run()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("cli", "menubar", "shots"):
        p = sub.add_parser(command)
        p.add_argument("--home", type=Path, help="an empty throwaway directory")
        if command == "cli":
            p.add_argument("args", nargs=argparse.REMAINDER)
        elif command == "shots":
            p.add_argument("--out", type=Path, default=Path("docs/images"))
    args = parser.parse_args(argv)
    home = (args.home or Path(tempfile.mkdtemp(prefix="ccm-demo-"))).resolve()
    print(f"Demo home: {home}", file=sys.stderr, flush=True)
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    chrome = chrome if Path(chrome).is_file() else shutil.which("google-chrome") or shutil.which(
        "chromium")
    out = args.out.resolve() if args.command == "shots" else None
    with offline((chrome, "screencapture") if out else ()):
        try:
            world = seed(home)
        except ValueError as error:
            parser.error(str(error))
        os.chdir(world.home / "src/acme-api")
        os.environ["TERM_SESSION_ID"] = "demo-term-api"
        if args.command == "cli":
            raise SystemExit(cli.main(args.args))
        if out:
            out.mkdir(parents=True, exist_ok=True)
            cli_shots(out, chrome)
        menubar = load_menubar(world.home)
        if out:
            app = menubar.ManagerApp()
            menu_shots(app, out, chrome)
        else:
            menubar.main()


if __name__ == "__main__":
    main()
