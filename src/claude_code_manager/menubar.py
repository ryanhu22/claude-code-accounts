"""macOS menu bar app for managing Claude Code subscriptions.

Shows every subscription's usage, which projects are running on which account,
swaps a project's account in one click, pokes an idle account to start its
5-hour window, and adds or removes accounts.

Threading model: a background worker fetches usage (network), and a fast timer
on the main thread applies the result and rebuilds the menu only when
something changed. AppKit is not thread-safe, so no menu object is ever
touched off the main thread.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
import webbrowser
from typing import Optional

import rumps

from . import core, projects

REFRESH_SECONDS = 180      # usage is not fast-moving; stay light on the API
PROJECT_WINDOW_MIN = 60
ICON = "⇄"


def _pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.0f}%"


def _bar(pct: Optional[float], width: int = 10) -> str:
    if pct is None:
        return " " * width
    filled = max(0, min(width, round(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def _short(email: Optional[str]) -> str:
    return (email or "?").split("@")[0]


class Snapshot:
    def __init__(self) -> None:
        self.accounts: list[core.Account] = []
        self.projects: list[projects.ProjectActivity] = []
        self.contexts: list[core.Context] = []
        self.context_email: dict[str, str] = {}
        self.taken_at: float = 0.0


class ManagerApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Claude", title=f"{ICON} …", quit_button=None)
        self._snapshot = Snapshot()
        self._pending: Optional[Snapshot] = None
        self._lock = threading.Lock()
        self._busy = False
        self._hide_from_dock()
        self.refresh_now(None)
        rumps.Timer(self._on_refresh_tick, REFRESH_SECONDS).start()
        rumps.Timer(self._on_sync_tick, 1).start()

    # ------------------------------------------------------------------ plumbing

    @staticmethod
    def _hide_from_dock() -> None:
        """Accessory policy: menu bar only, no Dock icon, no Cmd-Tab entry."""
        try:
            import AppKit
            AppKit.NSApplication.sharedApplication().setActivationPolicy_(
                AppKit.NSApplicationActivationPolicyAccessory)
        except Exception:
            pass

    def _collect(self) -> Snapshot:
        snap = Snapshot()
        snap.accounts = [core.load_account(n) for n in core.account_names()]
        snap.contexts = core.contexts()
        snap.context_email = {c.path: (c.email or "") for c in snap.contexts}
        snap.projects = projects.recent(PROJECT_WINDOW_MIN)
        snap.taken_at = time.time()
        return snap

    def _worker(self) -> None:
        try:
            snap = self._collect()
            with self._lock:
                self._pending = snap
        finally:
            self._busy = False

    def _on_refresh_tick(self, _timer) -> None:
        if self._busy:
            return
        self._busy = True
        threading.Thread(target=self._worker, daemon=True).start()

    def _on_sync_tick(self, _timer) -> None:
        with self._lock:
            snap, self._pending = self._pending, None
        if snap is not None:
            self._snapshot = snap
            self._rebuild()

    def refresh_now(self, _sender) -> None:
        self._on_refresh_tick(None)

    # ------------------------------------------------------------------ menu

    def _rebuild(self) -> None:
        snap = self._snapshot
        self.title = self._title_text(snap)
        self.menu.clear()

        self.menu.add(rumps.MenuItem("SUBSCRIPTIONS", callback=None))
        for acct in snap.accounts:
            self.menu.add(self._account_item(acct, snap))
        self.menu.add(rumps.separator)

        label = f"ACTIVE PROJECTS · last {PROJECT_WINDOW_MIN}m"
        self.menu.add(rumps.MenuItem(label, callback=None))
        if not snap.projects:
            self.menu.add(rumps.MenuItem("  none", callback=None))
        for proj in snap.projects[:12]:
            self.menu.add(self._project_item(proj, snap))
        self.menu.add(rumps.separator)

        manage = rumps.MenuItem("Manage accounts")
        manage.add(rumps.MenuItem("Add an account…", callback=self._add_account))
        remove = rumps.MenuItem("Remove an account")
        for acct in snap.accounts:
            remove.add(rumps.MenuItem(acct.name, callback=self._make_remove(acct.name)))
        manage.add(remove)
        self.menu.add(manage)

        age = int(time.time() - snap.taken_at) if snap.taken_at else 0
        self.menu.add(rumps.MenuItem(f"Refresh now (updated {age}s ago)", callback=self.refresh_now))
        self.menu.add(rumps.MenuItem("Quit", callback=rumps.quit_application))

    def _title_text(self, snap: Snapshot) -> str:
        """Title tracks the default context: the account most sessions use."""
        default = next((c for c in snap.contexts if c.name == "default"), None)
        email = snap.context_email.get(default.path) if default else None
        acct = next((a for a in snap.accounts if (a.email or "").lower() == (email or "").lower()), None)
        if not acct:
            return f"{ICON} {_short(email) if email else '?'}"
        return f"{ICON} {_short(acct.email)} {_pct(acct.session_pct)}·{_pct(acct.weekly_pct)}"

    def _account_item(self, acct: core.Account, snap: Snapshot) -> rumps.MenuItem:
        if not acct.ok:
            item = rumps.MenuItem(f"  {acct.name} — {acct.error}")
            item.add(rumps.MenuItem("Sign in…", callback=self._make_add(acct.name)))
            return item
        used_by = [c.name for c in snap.contexts
                   if snap.context_email.get(c.path, "").lower() == (acct.email or "").lower()]
        mark = "●" if used_by else "○"
        head = f"{mark} {acct.name} — {_pct(acct.session_pct)} 5h · {_pct(acct.weekly_pct)} 7d"
        if used_by:
            head += f"   [{', '.join(used_by)}]"
        item = rumps.MenuItem(head)
        for lim in acct.limits:
            when = f"  resets {lim.resets_in}" if lim.resets_at else "  idle"
            item.add(rumps.MenuItem(f"{lim.label:>6}  {_bar(lim.percent)} {_pct(lim.percent)}{when}",
                                    callback=None))
        item.add(rumps.separator)
        session = acct.limit("session")
        if session and not session.resets_at:
            item.add(rumps.MenuItem("Poke to start the 5h window",
                                    callback=self._make_poke(acct.name)))
        else:
            item.add(rumps.MenuItem("Poke (window already running)",
                                    callback=self._make_poke(acct.name)))
        item.add(rumps.separator)
        for ctx in snap.contexts:
            same = snap.context_email.get(ctx.path, "").lower() == (acct.email or "").lower()
            entry = rumps.MenuItem(f"Use for “{ctx.name}”" + ("  ✓" if same else ""),
                                   callback=None if same else self._make_swap(acct.name, ctx))
            item.add(entry)
        return item

    def _project_item(self, proj: projects.ProjectActivity,
                      snap: Snapshot) -> rumps.MenuItem:
        email = snap.context_email.get(proj.context.path, "")
        head = f"  {proj.name} — {_short(email)} ({proj.context.name}) · {proj.ago}"
        item = rumps.MenuItem(head)
        item.add(rumps.MenuItem(f"{proj.sessions} session(s) · {proj.path}", callback=None))
        item.add(rumps.separator)
        item.add(rumps.MenuItem(f"Switch “{proj.context.name}” to:", callback=None))
        for acct in snap.accounts:
            if not acct.ok:
                continue
            same = (acct.email or "").lower() == email.lower()
            item.add(rumps.MenuItem(
                f"   {acct.name} ({_pct(acct.session_pct)} 5h)" + ("  ✓" if same else ""),
                callback=None if same else self._make_swap(acct.name, proj.context)))
        item.add(rumps.separator)
        item.add(rumps.MenuItem("Open in Finder", callback=self._make_open(proj.path)))
        return item

    # ------------------------------------------------------------------ actions

    def _make_swap(self, account: str, ctx: core.Context):
        def handler(_sender):
            ok, msg = core.swap(account, ctx)
            rumps.notification("Claude Code Manager", "Swapped" if ok else "Swap failed", msg)
            if ok:
                rumps.alert(title="Account swapped", message=(
                    f"{msg}\n\nNew sessions in “{ctx.name}” use it right away. "
                    "Sessions already running keep their old account until they "
                    "restart: ctrl+C twice, then `claude -c`."), ok="Got it")
            self.refresh_now(None)
        return handler

    def _make_poke(self, account: str):
        def handler(_sender):
            ok, msg = core.poke(account)
            rumps.notification("Claude Code Manager",
                               f"Poked {account}" if ok else f"Could not poke {account}", msg)
            self.refresh_now(None)
        return handler

    def _make_remove(self, account: str):
        def handler(_sender):
            confirm = rumps.alert(
                title=f"Remove {account}?",
                message="This deletes its stored login from the keychain. Your "
                        "subscription is untouched, and you can add it back with a sign-in.",
                ok="Remove", cancel="Cancel")
            if confirm == 1:
                core.remove_account(account)
                self.refresh_now(None)
        return handler

    def _make_add(self, name: str = ""):
        def handler(_sender):
            self._add_account(None, preset=name)
        return handler

    def _add_account(self, _sender, preset: str = "") -> None:
        win = rumps.Window(
            title="Add a Claude account",
            message="Name this account slot (letters, digits, dashes).\n"
                    "A Terminal window opens so you can run /login as that account.",
            default_text=preset or "", ok="Open Terminal", cancel="Cancel", dimensions=(240, 22))
        resp = win.run()
        if resp.clicked != 1:
            return
        name = "".join(ch for ch in resp.text.strip() if ch.isalnum() or ch in "-_")
        if not name:
            return
        slot = core.slot_dir(name)
        script = (f'mkdir -p {slot}; echo "Type /login, sign in as {name}, then /exit"; '
                  f'CLAUDE_CONFIG_DIR={slot} claude')
        subprocess.run(["osascript", "-e",
                        f'tell application "Terminal" to do script "{script}"',
                        "-e", 'tell application "Terminal" to activate'], check=False)

    @staticmethod
    def _make_open(path: str):
        def handler(_sender):
            subprocess.run(["open", path], check=False)
        return handler


def main() -> None:
    ManagerApp().run()


if __name__ == "__main__":
    main()
