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

import datetime as _dt
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


# Menu rows are drawn as attributed strings so the three usage buckets line up
# in real columns. A proportional font cannot align with spaces, and a plain
# title cannot colour the bucket that is nearly spent.
BAR_W = 10
NAME_W = 14
FULL, EMPTY = "\u2588", "\u2591"          # █ ░


def _colors():
    import AppKit
    return {
        "ok": AppKit.NSColor.systemGreenColor(),
        "warn": AppKit.NSColor.systemOrangeColor(),
        "hot": AppKit.NSColor.systemRedColor(),
        "dim": AppKit.NSColor.secondaryLabelColor(),
        "text": AppKit.NSColor.labelColor(),
    }


def _tone(pct: Optional[float]) -> str:
    if pct is None:
        return "dim"
    return "ok" if pct < 60 else "warn" if pct < 85 else "hot"


def _styled(segments: list[tuple[str, str]], size: float = 12.0):
    """Build an NSAttributedString from (text, colour-name) runs."""
    import AppKit
    colors = _colors()
    font = AppKit.NSFont.monospacedSystemFontOfSize_weight_(size, AppKit.NSFontWeightRegular)
    out = AppKit.NSMutableAttributedString.alloc().init()
    for text, tone in segments:
        attrs = {AppKit.NSFontAttributeName: font,
                 AppKit.NSForegroundColorAttributeName: colors.get(tone, colors["text"])}
        out.appendAttributedString_(
            AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs))
    return out


def _apply_style(item: "rumps.MenuItem", segments: list[tuple[str, str]]) -> None:
    """Style a row, falling back silently to its plain title if AppKit balks."""
    try:
        item._menuitem.setAttributedTitle_(_styled(segments))
    except Exception:
        pass


def _compact_reset(iso: Optional[str]) -> str:
    """Short countdown for an inline row: 45m, 4h, 34h, 3d.

    Computed from the absolute reset timestamp on every render, so it stays
    right between refreshes instead of ageing with the fetch.
    """
    if not iso:
        return "idle"
    try:
        dt = _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return ""
    mins = round((dt - _dt.datetime.now(_dt.timezone.utc)).total_seconds() / 60)
    if mins <= 0:
        return "now"
    if mins < 60:
        return f"{mins}m"
    hours = round(mins / 60)
    if hours < 48:
        return f"{hours}h"
    return f"{round(hours / 24)}d"


def _bucket(label: str, lim: Optional[core.Limit], show_reset: bool = True) -> list[tuple[str, str]]:
    pct = lim.percent if lim else None
    tone = _tone(pct)
    if pct is None:
        return [(f"  {label:>5} ", "dim"), (" " * BAR_W, "dim"), ("    —", "dim"),
                *([("      ", "dim")] if show_reset else [])]
    filled = max(0, min(BAR_W, round(pct / 100 * BAR_W)))
    out = [
        (f"  {label:>5} ", "dim"),
        (FULL * filled, tone),
        (EMPTY * (BAR_W - filled), "dim"),
        (f" {pct:3.0f}%", tone),
    ]
    if show_reset:
        reset = _compact_reset(lim.resets_at if lim else None)
        # an idle window is worth noticing: that account can be poked
        out.append((f" \u21bb{reset:>4}", "ok" if reset == "idle" else "dim"))
    return out


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
            _apply_style(item, [(f"  {acct.name:<{NAME_W}}", "text"),
                                (f"  {acct.error}", "hot")])
            item.add(rumps.MenuItem("Sign in…", callback=self._make_add(acct.name)))
            return item
        used_by = [c.name for c in snap.contexts
                   if snap.context_email.get(c.path, "").lower() == (acct.email or "").lower()]
        in_use = bool(used_by)
        # plain title stays unique: rumps keys its callback registry by it
        head = f"{'●' if in_use else '○'} {acct.name} — {_pct(acct.session_pct)} 5h"
        item = rumps.MenuItem(head)

        fable = next((l for l in acct.limits
                      if l.kind not in ("session", "weekly_all")), None)
        segments: list[tuple[str, str]] = [
            ("● " if in_use else "○ ", "text" if in_use else "dim"),
            (f"{acct.name:<{NAME_W}}", "text"),
        ]
        segments += _bucket("5h", acct.limit("session"))
        segments += _bucket("7d", acct.limit("weekly_all"))
        segments += _bucket(fable.label if fable else "model", fable)
        if used_by:
            segments.append((f"   {', '.join(used_by)}", "dim"))
        _apply_style(item, segments)

        for lim in acct.limits:
            when = f"resets in {lim.resets_in}" if lim.resets_at else "idle, no window running"
            sub = rumps.MenuItem(f"{lim.label:>6}  {_bar(lim.percent)} {_pct(lim.percent)}  {when}",
                                 callback=None)
            _apply_style(sub, [(f"  {lim.label:<6}", "dim"),
                               *_bucket("", lim, show_reset=False)[1:],
                               (f"   {when}", "dim")])
            item.add(sub)
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
