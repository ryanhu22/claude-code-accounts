"""Command line interface: `ccm`."""
from __future__ import annotations

import argparse
import os
import shlex
import sys

from . import codex, core, oauth, sessions, shell

G, Y, R, D, X = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def _bar(pct: float, width: int = 20) -> str:
    filled = max(0, min(width, round(pct / 100 * width)))
    color = G if pct < 60 else Y if pct < 85 else R
    return f"{color}{'█' * filled}{D}{'░' * (width - filled)}{X}"


def cmd_list(_args) -> int:
    r = core.bootstrap()
    accts = core.all_accounts()
    for acct in accts:
        used = [] if acct.is_codex else core.rules_using(acct.name, r)
        head = f"\033[1m{acct.name}{X}"
        if acct.is_codex:
            head += f"{D} codex{X}"
        if acct.signed_in:
            head += f"  {D}{acct.email} · {acct.plan}{X}"
            if used:
                head += f"  {G}<- {', '.join(used)}{X}"
            if acct.stale:
                head += f"  {Y}(usage {int(acct.usage_age // 60)}m old){X}"
        if acct.mismatch:
            head += f"  {R}{acct.mismatch}{X}"
        elif acct.error:
            head += f"  {Y}{acct.error}{X}"
        print(head)
        if not acct.signed_in:
            hint = f"ccm login {acct.name} --codex" if acct.is_codex else f"ccm add {acct.name}"
            print(f"  ({hint})\n")
            continue
        for lim in acct.limits:
            when = f"{D}resets {lim.resets_in}{X}" if lim.resets_at else f"{D}idle{X}"
            span = {18000: "5h", 604800: "7d"}.get(lim.span, "")
            label = f"{lim.scope} {span}".strip() if lim.scope else span or lim.label
            print(f"  {label:>6}  {_bar(lim.percent)} {lim.percent:5.1f}%  {when}")
        if acct.extras:
            balance = acct.extras.get("credits_balance") or "none"
            if balance == "0":
                balance = "none"
            detail = f"credits: {balance}"
            resets = acct.extras.get("reset_credits") or 0
            if resets > 0:
                detail += f" · reset credits: {resets}"
            print(f"  {D}{detail}{X}")
        print()
    return 0


def cmd_poke(args) -> int:
    ok, msg = core.poke(args.account)
    print(f"{args.account}: {msg}")
    return 0 if ok else 1


_WHY = {"session": "pinned to this terminal", "project": "a rule for this project",
        "default": "no rule covers it, so the default applies"}


def cmd_where(_args) -> int:
    core.bootstrap()
    term = os.environ.get("TERM_SESSION_ID", "")
    cwd = os.getcwd()
    account, reason = core.resolve(cwd, term)
    why = _WHY.get(reason) or f"the “{reason.split(':', 1)[-1]}” profile"
    acct = core.load_account(account, with_usage=False) if account else None
    print(cwd)
    print(f"  account : {G}{account or 'none'}{X}" + (f"  {D}{acct.email}{X}" if acct and acct.email else ""))
    print(f"  because : {D}{why}{X}")
    print(f"  dir     : {D}{core.account_dir(account).replace(core.HOME, '~') if account else '-'}{X}")
    return 0


def _rule_table(r, accts) -> None:
    """Print the rules from least to most specific, the way they resolve."""
    plan = {a.name: a for a in accts}
    def chip(name: str) -> str:
        a = plan.get(name)
        return f"{name}" + (f" {D}({a.email}){X}" if a and a.email else "")
    print(f"{'everything else':<22} {chip(r.default_account) if r.default_account else Y + 'not set' + X}")
    for prof in r.profiles:
        n = len(prof.repos)
        print(f"\n{prof.name:<22} {chip(prof.account) if prof.account else D + 'no account' + X}"
              f"  {D}{n} repo{'s' if n != 1 else ''}{X}")
        for repo in prof.repos:
            print(f"  {D}{repo}{X}")
    if r.projects:
        print()
        for path, account in r.projects.items():
            print(f"{D}project{X} {path:<28} {chip(account)}")
    if r.sessions:
        print()
        for tid, account in r.sessions.items():
            print(f"{D}session{X} {tid[:13]:<28} {chip(account)}")


def cmd_shell_init(args) -> int:
    """Print the shell wrapper to eval from an rc file."""
    print(shell.init(core.ACCOUNTS_DIR, args.shell), end="")
    return 0


def cmd_profiles(_args) -> int:
    r = core.bootstrap()
    accts = [core.load_account(n, with_usage=False) for n in core.account_names()]
    if not r.profiles and not r.projects:
        print(f"{D}No profiles yet. A profile is a named group of repos that share")
        print(f"an account: `ccm profile new work`, then `ccm profile add work` in a repo.{X}\n")
    _rule_table(r, accts)
    return 0


def cmd_profile(args) -> int:
    action = args.action
    if action == "new":
        ok, msg = core.add_profile(args.name, args.account or "")
    elif action == "rm":
        ok, msg = core.remove_profile(args.name)
    elif action == "rename":
        if not args.account:
            print("usage: ccm profile rename <old> <new-name>", file=sys.stderr)
            return 1
        ok, msg = core.rename_profile(args.name, args.account)
    elif action == "add":
        ok, msg = core.profile_add_repo(args.name, args.path or os.getcwd())
    elif action == "drop":
        ok, msg = core.profile_remove_repo(args.name, args.path or os.getcwd())
    else:
        print(f"unknown action {action}", file=sys.stderr)
        return 1
    print(msg)
    return 0 if ok else 1


def cmd_use(args) -> int:
    """Point one scope at an account: session, project, profile or default."""
    core.bootstrap()
    cwd = os.getcwd()
    if args.default:
        scope, key = "default", ""
    elif args.profile:
        scope, key = "profile", args.profile
    elif args.session:
        scope, key = "session", _term_or_die()
    else:
        scope, key = "project", cwd
    ok, msg = core.assign(scope, key, args.account, cwd=cwd)
    print(msg)
    if ok:
        print(f"{D}Running sessions keep their account until they restart: "
              f"ctrl+C twice, then `claude -c`.{X}")
    return 0 if ok else 1


def _term_or_die() -> str:
    term = os.environ.get("TERM_SESSION_ID", "")
    if not term:
        print("this terminal sets no TERM_SESSION_ID, so it cannot be pinned",
              file=sys.stderr)
        raise SystemExit(1)
    return term


def cmd_sessions(_args) -> int:
    r = core.bootstrap()
    live = sessions.live(core.credential_dirs())
    accts = [core.load_account(n, with_usage=False) for n in core.account_names()]
    if not live:
        print("no Claude Code sessions running")
        return 0
    owners = core.dirs_to_accounts({s.env_config_dir for s in live}, accts)
    for s in live:
        pinned = s.term_id and s.term_id in r.sessions
        running_on = owners.get(s.env_config_dir, "")
        wanted, reason = core.resolve(s.cwd, s.term_id)
        drift = f"  {Y}-> {wanted} on restart{X}" if wanted and wanted != running_on else ""
        print(f"{'\u25cf' if pinned else ' '} {s.label[:24]:<25} {s.status or s.kind:<7} "
              f"{s.cwd.replace(core.HOME, '~')[:44]:<45} {running_on or '?':<15}"
              f"{D}{reason}{X}{drift}")
    print(f"\n{D}\u25cf = has a rule of its own. `ccm use <account> --session` pins the "
          f"terminal you run it in.{X}")
    return 0


def cmd_pin(args) -> int:
    ok, msg = core.assign("session", _term_or_die(), args.account, cwd=os.getcwd())
    print(msg)
    if ok:
        print(f"{D}This terminal only. Restart Claude Code here to pick it up: "
              f"ctrl+C twice, then `claude -c`.{X}")
    return 0 if ok else 1


def cmd_unpin(_args) -> int:
    ok, msg = core.clear("session", _term_or_die())
    print(msg)
    return 0 if ok else 1


def cmd_resolve(_args) -> int:
    """Print the config dir this shell should launch Claude Code with.

    Called by the generated resolver on every launch, so it stays quiet and
    fast and never fails loudly: the shell has a pure-text fallback for when
    this cannot answer.
    """
    print(core.resolve_dir(os.getcwd(), os.environ.get("TERM_SESSION_ID", "")))
    return 0


def cmd_login(args) -> int:
    """Sign an account in through the browser.

    The browser returns the code to a local port, so there is nothing to copy.
    If nothing can listen, or the wait times out, fall back to pasting it.
    """
    if args.codex:
        if args.paste:
            print("Codex sign-in has no paste flow", file=sys.stderr)
            return 1
        try:
            cb = oauth.Callback(port=codex.CALLBACK_PORT, path=codex.CALLBACK_PATH)
        except OSError:
            print("port 1455 is in use (is another sign-in or `codex login` running?)", file=sys.stderr)
            return 1
        try:
            attempt = core.sign_in_begin_codex(args.account)
            cb.expect(attempt.state)
            err = oauth.open_in(attempt.url, args.browser or "")
            if err:
                print(f"could not open a browser: {err}\n\nOpen this yourself:\n{attempt.url}",
                      file=sys.stderr)
            else:
                print(f"Signing in as “{args.account}”. A browser is opening.")
            print(f"{D}Waiting for the browser…{X}")
            if cb.wait(300) and cb.code:
                ok, msg = core.sign_in_finish_codex(attempt, cb.code, cb.state)
            else:
                ok, msg = False, cb.error or "no code returned within five minutes; try signing in again"
        finally:
            cb.close()
        print(msg if ok else f"{Y}{msg}{X}", file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    cb = None
    if not args.paste:
        try:
            cb = oauth.Callback()
        except OSError:
            cb = None
    attempt = core.sign_in_begin(args.account, cb.redirect_uri if cb else "")
    if cb:
        # The server exists before the attempt, because the authorize URL needs
        # the port, so tell it now which sign-in it is waiting for. Anything on
        # this machine can reach that port.
        cb.expect(attempt.state)
    err = oauth.open_in(attempt.url, args.browser or "")
    if err:
        print(f"could not open a browser: {err}\n\nOpen this yourself:\n{attempt.url}",
              file=sys.stderr)
    else:
        print(f"Signing in as “{args.account}”. A browser is opening.")
    pasted = ""
    if cb:
        print(f"{D}Waiting for the browser…{X}")
        if cb.wait(300) and cb.code:
            pasted = f"{cb.code}#{cb.state}"
        elif cb.error:
            print(f"{Y}{cb.error}{X}", file=sys.stderr)
        cb.close()
    if not pasted:
        print(f"{D}Paste the code the page shows.{X}")
        try:
            pasted = input("code: ")
        except EOFError:
            print("\nno code given; nothing changed", file=sys.stderr)
            return 1
    ok, msg = core.sign_in_finish(attempt, pasted)
    print(msg if ok else f"{Y}{msg}{X}", file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def cmd_add(args) -> int:
    if args.codex:
        slot = shlex.quote(codex.slot_dir(args.account))
        print(f"mkdir -p {slot} && CODEX_HOME={slot} codex login")
    else:
        print(core.add_account_command(args.account))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ccm", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="usage for every subscription").set_defaults(func=cmd_list)
    for verb in ("use", "swap"):
        p = sub.add_parser(verb, help="point a scope at another account")
        p.add_argument("account")
        g = p.add_mutually_exclusive_group()
        g.add_argument("--session", action="store_true", help="this terminal only")
        g.add_argument("--profile", metavar="NAME", help="every repo in a profile")
        g.add_argument("--default", action="store_true", help="everything with no rule")
        p.set_defaults(func=cmd_use)
    p = sub.add_parser("shell-init", help="print the shell wrapper to eval in your rc file")
    p.add_argument("shell", nargs="?", default="zsh", choices=("zsh", "bash"))
    p.set_defaults(func=cmd_shell_init)
    sub.add_parser("profiles", help="show every rule, least specific first").set_defaults(func=cmd_profiles)
    p = sub.add_parser("profile", help="create and edit profiles")
    p.add_argument("action", choices=("new", "rm", "rename", "add", "drop"))
    p.add_argument("name")
    p.add_argument("account", nargs="?", help="account for new, new name for rename")
    p.add_argument("--path", help="repository (default: this directory)")
    p.set_defaults(func=cmd_profile)
    p = sub.add_parser("poke", help="spend one token to start an account's 5h window")
    p.add_argument("account")
    p.set_defaults(func=cmd_poke)
    sub.add_parser("where", help="which context and account this directory uses").set_defaults(func=cmd_where)
    sub.add_parser("sessions", help="every running Claude Code session").set_defaults(func=cmd_sessions)
    p = sub.add_parser("pin", help="give THIS terminal its own account")
    p.add_argument("account")
    p.set_defaults(func=cmd_pin)
    sub.add_parser("unpin", help="drop this terminal's pin").set_defaults(func=cmd_unpin)
    sub.add_parser("resolve", help="print the config dir for this shell").set_defaults(func=cmd_resolve)
    p = sub.add_parser("login", help="sign an account in through the browser (add --codex for an OpenAI Codex account)")
    p.add_argument("account")
    p.add_argument("--browser", help='e.g. "Google Chrome", "Safari"')
    p.add_argument("--paste", action="store_true", help="paste the code instead of listening")
    p.add_argument("--codex", action="store_true", help="sign in to OpenAI Codex")
    p.set_defaults(func=cmd_login)
    p = sub.add_parser("add", help="print the command that signs an account in")
    p.add_argument("account")
    p.add_argument("--codex", action="store_true", help="print the Codex sign-in command")
    p.set_defaults(func=cmd_add)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
