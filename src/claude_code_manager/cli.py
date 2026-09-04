"""Command line interface: `ccm`."""
from __future__ import annotations

import argparse
import os
import sys

from . import core, projects, sessions

G, Y, R, D, X = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def _bar(pct: float, width: int = 20) -> str:
    filled = max(0, min(width, round(pct / 100 * width)))
    color = G if pct < 60 else Y if pct < 85 else R
    return f"{color}{'█' * filled}{D}{'░' * (width - filled)}{X}"


def cmd_list(_args) -> int:
    ctxs = core.contexts()
    accts = [core.load_account(n) for n in core.account_names()]
    emails = core.context_owners([c.path for c in ctxs], accts)
    for acct in accts:
        used = [c.name for c in ctxs if emails.get(c.path, "").lower() == (acct.email or "").lower()]
        head = f"\033[1m{acct.name}{X}"
        if acct.signed_in:
            head += f"  {D}{acct.email} · {acct.plan}{X}"
            if used:
                head += f"  {G}<- in use by {', '.join(used)}{X}"
            if acct.stale:
                head += f"  {Y}(usage {int(acct.usage_age // 60)}m old){X}"
        if acct.error:
            head += f"  {Y}{acct.error}{X}"
        print(head)
        if not acct.signed_in:
            print(f"  (ccm add {acct.name})\n")
            continue
        for lim in acct.limits:
            when = f"{D}resets {lim.resets_in}{X}" if lim.resets_at else f"{D}idle{X}"
            print(f"  {lim.label:>6}  {_bar(lim.percent)} {lim.percent:5.1f}%  {when}")
        print()
    return 0


def cmd_swap(args) -> int:
    ctx = core.context_for(os.getcwd()) if not args.context else \
        next((c for c in core.contexts() if c.name == args.context), None)
    if ctx is None:
        print(f"unknown context: {args.context}", file=sys.stderr)
        return 2
    ok, msg = core.swap(args.account, ctx)
    print(msg)
    if ok:
        print("New sessions use it now. A running session keeps its account until it")
        print("restarts: ctrl+C twice, then `claude -c`.")
    return 0 if ok else 1


def cmd_poke(args) -> int:
    ok, msg = core.poke(args.account)
    print(f"{args.account}: {msg}")
    return 0 if ok else 1


def cmd_where(_args) -> int:
    term = os.environ.get("TERM_SESSION_ID", "")
    ctx = core.context_for(os.getcwd(), term)
    print(os.getcwd())
    print(f"  context : {ctx.path.replace(core.HOME, '~')} ({ctx.name})")
    print(f"  account : {ctx.email or 'unknown'}")
    if term and term in core.term_pins():
        print(f"  {G}pinned  : this terminal only{X}")
    return 0


def _term_or_die() -> str:
    term = os.environ.get("TERM_SESSION_ID", "")
    if not term:
        print("this terminal sets no TERM_SESSION_ID, so it cannot be pinned",
              file=sys.stderr)
        raise SystemExit(1)
    return term


def cmd_sessions(_args) -> int:
    live = sessions.live(core.credential_dirs())
    accts = [core.load_account(n, with_usage=False) for n in core.account_names()]
    owners = core.context_owners({s.env_config_dir for s in live}, accts)
    pins = core.term_pins()
    if not live:
        print("no Claude Code sessions running")
        return 0
    for s in live:
        mark = "\u25cf" if s.term_id in pins else " "
        where = s.cwd.replace(core.HOME, "~")
        print(f"{mark} {s.label[:24]:<25} {s.status or s.kind:<7} {where[:46]:<47} "
              f"{owners.get(s.env_config_dir) or '?'}")
    print(f"\n{D}\u25cf = pinned to one account. `ccm pin <account>` pins the terminal "
          f"you run it in.{X}")
    return 0


def cmd_pin(args) -> int:
    term = _term_or_die()
    here = core.context_for(os.getcwd())
    ok, msg = core.pin(term, args.account, seed_from=here.path)
    print(msg)
    if ok:
        print(f"{D}This terminal only. Restart Claude Code here to pick it up: "
              f"ctrl+C twice, then `claude -c`.{X}")
    return 0 if ok else 1


def cmd_unpin(_args) -> int:
    ok, msg = core.unpin(_term_or_die())
    print(msg)
    return 0 if ok else 1


def cmd_projects(args) -> int:
    for p in projects.recent(args.minutes):
        ctx_email = p.context.email or "?"
        print(f"{p.name:42} {p.ago:>10}  {p.context.name:8} {ctx_email}")
    return 0


def cmd_isolate(_args) -> int:
    ok, msg = core.isolate(os.getcwd())
    print(msg)
    if ok:
        print("`ccm swap` here now changes only this project. Undo: ccm unroute")
    return 0 if ok else 1


def cmd_unroute(_args) -> int:
    ok, msg = core.unroute(os.getcwd())
    print(msg)
    return 0 if ok else 1


def cmd_add(args) -> int:
    print(core.add_account_command(args.account))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ccm", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="usage for every subscription").set_defaults(func=cmd_list)
    p = sub.add_parser("swap", help="point a context at another account")
    p.add_argument("account")
    p.add_argument("--context", help="context name (default: the one for this directory)")
    p.set_defaults(func=cmd_swap)
    p = sub.add_parser("poke", help="spend one token to start an account's 5h window")
    p.add_argument("account")
    p.set_defaults(func=cmd_poke)
    sub.add_parser("where", help="which context and account this directory uses").set_defaults(func=cmd_where)
    p = sub.add_parser("projects", help="projects with recent Claude Code activity")
    p.add_argument("--minutes", type=int, default=60)
    p.set_defaults(func=cmd_projects)
    sub.add_parser("sessions", help="every running Claude Code session").set_defaults(func=cmd_sessions)
    p = sub.add_parser("pin", help="give THIS terminal its own account")
    p.add_argument("account")
    p.set_defaults(func=cmd_pin)
    sub.add_parser("unpin", help="drop this terminal's pin").set_defaults(func=cmd_unpin)
    sub.add_parser("isolate", help="give this project its own context").set_defaults(func=cmd_isolate)
    sub.add_parser("unroute", help="drop this project's routing override").set_defaults(func=cmd_unroute)
    p = sub.add_parser("add", help="print the command that signs an account in")
    p.add_argument("account")
    p.set_defaults(func=cmd_add)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
