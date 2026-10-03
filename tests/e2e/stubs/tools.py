"""Stand-ins for the external tools ccm shells out to, one module for all.

The sandbox puts a wrapper for each name first on PATH that runs
`tools.py <name> ...`. Every call lands in `<sandbox>/tools.log` as one JSON
line, and the ones a test asks about get a file of their own:

- `open`: the URLs ccm asked a browser to open, in `open-urls.log`.
- `codex exec`: each poke, with its CODEX_HOME, in `codex-exec.log`; a file
  named `codex-exec.fail` makes the next run fail with that file's text.
- `launchctl`: a tripwire. ccm must never reach it from a test, so a call
  lands in `tripwire.log` and fails.
- `ps`, `lsof`: answer with nothing, so the real machine's processes never
  walk into the sandbox.
- `claude`, `codex --version`: a version, so the User-Agent is deterministic.
- `pmset`: a full wake. `osascript`: fails, as it would with no terminal app.
"""
import json
import os
import sys

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
            return 0 if argv[1:2] in (["Google Chrome"], ["Safari"]) else 1
        url = argv[-1] if argv else ""
        if url:
            with open(os.path.join(_sandbox(), "open-urls.log"), "a") as f:
                f.write(url + "\n")
        return 0
    if tool in ("ps", "lsof"):
        return 0
    if tool == "pmset":
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
