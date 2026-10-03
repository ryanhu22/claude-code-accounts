"""Stand-ins for the external tools ccm shells out to, one module for all.

The sandbox puts a wrapper for each name first on PATH that runs
`tools.py <name> ...`. Every call lands in `<sandbox>/tools.log` as one JSON
line, and the ones a test asks about get a file of their own:

- `open`: the URLs ccm asked a browser to open, in `open-urls.log`.
- `codex exec`: each poke, with its CODEX_HOME, in `codex-exec.log`; a file
  named `codex-exec.fail` makes the next run fail with that file's text.
- `launchctl`: a tripwire. ccm must never reach it from a test, so a call
  lands in `tripwire.log` and fails.
- `ps`: answers from the sandbox's `ps.json`
  (`{"PID": {"tty": "ttys001", "env": {...}, "command": "claude"}}`), the
  processes a test registered (`Sandbox.seed_session`, `lifecycle.Fleet`),
  so the real machine's processes never walk into the sandbox. That is how a
  stand-in process gets the environment of a Claude Code session. `lsof`
  answers with nothing.
- `claude`, `codex --version`: a version, so the User-Agent is deterministic.
- `open -a <app>`: fails for an app that is not Google Chrome or Safari, as
  macOS does for a browser that is not installed.
- `pmset`: a full wake, or a dark wake (no Graphics) while a file named
  `pmset.dark` exists in the sandbox. `osascript`: fails, as it would with
  no terminal app.
"""
import json
import os
import sys

BROWSERS = ("Google Chrome", "Safari")

CLAUDE_VERSION = "2.1.261"
CODEX_VERSION = "0.153.0"


def _sandbox() -> str:
    path = os.environ.get("CCM_E2E_SANDBOX")
    if not path:
        sys.stderr.write("stub tool: CCM_E2E_SANDBOX is not set\n")
        sys.exit(2)
    return path


def _log(name: str, line: dict) -> None:
    with open(os.path.join(_sandbox(), name), "a") as f:
        f.write(json.dumps(line) + "\n")


def _ps(argv: list[str]) -> int:
    """`ps` over the processes in `ps.json`: {pid: {env, tty, command, lstart}}.

    The three forms src/ runs: `ps eww -p PID` (one process, its environment
    on the line), `ps -axo pid=,command=` and `ps -axo pid=,lstart=,command=`.
    A process registered without a command is a Claude Code session.
    """
    try:
        with open(os.path.join(_sandbox(), "ps.json")) as f:
            procs = json.load(f)
    except (OSError, ValueError):
        procs = {}
    if argv[:1] == ["eww"] and argv[1:2] == ["-p"]:
        print("  PID TTY           TIME CMD")
        proc = procs.get(argv[2] if len(argv) > 2 else "")
        if proc is None:
            return 1
        env = " ".join(f"{k}={v}" for k, v in (proc.get("env") or {}).items())
        command = proc.get("command") or "claude"
        print(f"{argv[2]} {proc.get('tty') or '??'}  0:00.01 {command} {env}")
        return 0
    if argv[:1] == ["-axo"]:
        fields = argv[1].split(",") if len(argv) > 1 else []
        for pid, proc in procs.items():
            cols = []
            for field in fields:
                if field.startswith("pid"):
                    cols.append(pid)
                elif field.startswith("lstart"):
                    cols.append(proc.get("lstart") or "Thu Jan  1 00:00:00 2026")
                elif field.startswith("command"):
                    cols.append(proc.get("command") or "claude")
            print(" ".join(cols))
        return 0
    return 0


def main(tool: str, argv: list[str]) -> int:
    _log("tools.log", {"tool": tool, "argv": argv,
                       "env": {k: v for k, v in os.environ.items()
                               if k in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "HOME")}})
    if tool == "claude":
        if argv == ["--version"]:
            print(f"{CLAUDE_VERSION} (Claude Code)")
            return 0
        sys.stderr.write("stub claude: only --version is supported\n")
        return 1
    if tool == "codex":
        if argv == ["--version"]:
            print(f"codex-cli {CODEX_VERSION}")
            return 0
        if argv[:1] == ["exec"]:
            _log("codex-exec.log", {"argv": argv, "CODEX_HOME": os.environ.get("CODEX_HOME", ""),
                                    "cwd": os.getcwd()})
            fail = os.path.join(_sandbox(), "codex-exec.fail")
            if os.path.exists(fail):
                with open(fail) as f:
                    sys.stderr.write(f.read())
                return 1
            print("ok")
            return 0
        sys.stderr.write(f"stub codex: {' '.join(argv)} is not supported\n")
        return 1
    if tool == "open":
        if argv[:1] == ["-Ra"]:
            return 0 if argv[1:2] in [[b] for b in BROWSERS] else 1
        if argv[:1] == ["-a"] and argv[1:2] not in [[b] for b in BROWSERS]:
            sys.stderr.write(f"Unable to find application named '{argv[1:2] or ['']}'\n")
            return 1
        url = argv[-1] if argv else ""
        if url:
            with open(os.path.join(_sandbox(), "open-urls.log"), "a") as f:
                f.write(url + "\n")
        return 0
    if tool == "ps":
        return _ps(argv)
    if tool == "lsof":
        return 0
    if tool == "pmset":
        if os.path.exists(os.path.join(_sandbox(), "pmset.dark")):
            print(" Current System Capabilities: CPU Disk Network")
        else:
            print(" Current System Capabilities: CPU Disk Network Graphics Audio")
        return 0
    if tool == "osascript":
        sys.stderr.write("stub osascript: no terminal app in the sandbox\n")
        return 1
    if tool == "launchctl":
        _log("tripwire.log", {"tool": tool, "argv": argv})
        sys.stderr.write("stub launchctl: ccm must not reach launchd from a test\n")
        return 1
    sys.stderr.write(f"stub: no such tool {tool}\n")
    return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
