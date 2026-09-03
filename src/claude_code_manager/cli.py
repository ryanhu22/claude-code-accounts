"""Command line interface: `ccm`."""
from __future__ import annotations

import argparse
import os
import sys

from . import core, projects

G, Y, R, D, X = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def _bar(pct: float, width: int = 20) -> str:
    filled = max(0, min(width, round(pct / 100 * width)))
    color = G if pct < 60 else Y if pct < 85 else R
    return f"{color}{'█' * filled}{D}{'░' * (width - filled)}{X}"


def cmd_list(_args) -> int:
    ctxs = core.contexts()
    emails = {c.path: (c.email or "") for c in ctxs}
    for name in core.account_names():
        acct = core.load_account(name)
        used = [c.name for c in ctxs if emails.get(c.path, "").lower() == (acct.email or "").lower()]
        head = f"\033[1m{acct.name}{X}"
        if acct.ok:
            head += f"  {D}{acct.email} · {acct.plan}{X}"
            if used:
                head += f"  {G}<- in use by {', '.join(used)}{X}"
        print(head)
        if not acct.ok:
            print(f"  {Y}{acct.error}{X}  (ccm add {acct.name})\n")
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
    ctx = core.context_for(os.getcwd())
    print(os.getcwd())
    print(f"  context : {ctx.path.replace(core.HOME, '~')} ({ctx.name})")
    print(f"  account : {ctx.email or 'unknown'}")
    return 0


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
    sub.add_parser("isolate", help="give this project its own context").set_defaults(func=cmd_isolate)
    sub.add_parser("unroute", help="drop this project's routing override").set_defaults(func=cmd_unroute)
    p = sub.add_parser("add", help="print the command that signs an account in")
    p.add_argument("account")
    p.set_defaults(func=cmd_add)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
