import os
import plistlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_code_accounts import cli, core, launchd


@pytest.fixture
def commands(monkeypatch):
    recorded = []

    def run(*args, **kwargs):
        assert kwargs == {"capture_output": True, "text": True, "timeout": 30}
        recorded.append(args[0])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    return recorded


@pytest.fixture
def program(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    executable = bindir / "ccm-menubar"
    executable.touch()
    executable.chmod(0o755)
    monkeypatch.setattr(launchd, "find_menubar", lambda: str(executable))
    return executable


def test_install(commands, program):
    ok, message = launchd.install()

    path = Path(core.HOME) / "Library" / "LaunchAgents" / f"{launchd.LABEL}.plist"
    assert ok
    assert "starts now and at every login" in message
    assert launchd.plist_path() == str(path)
    text = path.read_text()
    assert str(program) in text
    assert f"<string>{program.parent}:/opt/homebrew/bin:" in text
    assert "$HOME" not in text
    assert plistlib.loads(text.encode())["ProgramArguments"][0] == str(program)
    assert (Path(core.HOME) / "Library" / "Logs").is_dir()
    assert commands == [["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)]]


def test_reinstall(commands, program):
    assert launchd.install()[0]
    commands.clear()

    assert launchd.install()[0]
    assert commands == [
        ["launchctl", "bootout", f"gui/{os.getuid()}/{launchd.LABEL}"],
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", launchd.plist_path()],
    ]


def test_uninstall(commands, program):
    launchd.install()
    commands.clear()

    assert launchd.uninstall() == (
        True, "The menu bar app no longer starts at login. Quit the running one from its menu.",
    )
    assert commands == [["launchctl", "bootout", f"gui/{os.getuid()}/{launchd.LABEL}"]]
    assert not Path(launchd.plist_path()).exists()
    commands.clear()
    assert launchd.uninstall() == (
        True, "The menu bar app was not installed as a login item",
    )
    assert commands == []


def test_missing_menubar(monkeypatch, commands):
    monkeypatch.setattr(launchd, "find_menubar", lambda: None)
    ok, message = launchd.install()
    assert not ok
    assert "[menubar]" in message
    assert not Path(launchd.plist_path()).exists()
    assert commands == []


def test_render_matches_packaging():
    template = Path(__file__).resolve().parents[1] / "packaging" / f"{launchd.LABEL}.plist"
    program = os.path.join(core.HOME, ".local", "bin", "ccm-menubar")
    text = launchd.render(program)
    assert text.encode() == template.read_bytes().replace(b"$HOME", core.HOME.encode())
    assert plistlib.loads(text.encode())["ProgramArguments"][0] == program


def test_render_escapes_paths(monkeypatch, tmp_path):
    home = str(tmp_path / "home & accounts")
    monkeypatch.setattr(core, "HOME", home)
    program = os.path.join(home, "tools <local>", "ccm-menubar")
    plist = plistlib.loads(launchd.render(program).encode())
    assert plist["ProgramArguments"] == [program]
    assert plist["EnvironmentVariables"]["PATH"].startswith(os.path.dirname(program) + ":")
    assert plist["StandardErrorPath"] == home + "/Library/Logs/claude-code-accounts.log"


@pytest.mark.parametrize(("stderr", "stdout", "detail"), [
    (" denied\n", "fallback", "denied"), ("", " failed\n", "failed"),
])
def test_bootstrap_failure(monkeypatch, program, stderr, stdout, detail):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stdout=stdout, stderr=stderr,
    ))
    assert launchd.install() == (False, f"launchctl bootstrap failed: {detail}")


def test_stale_agent_does_not_block_reinstall_or_uninstall(monkeypatch, commands, program):
    launchd.install()
    run = subprocess.run

    def stale(*args, **kwargs):
        result = run(*args, **kwargs)
        if args[0][1] == "bootout":
            result.returncode = 1
        return result

    monkeypatch.setattr(subprocess, "run", stale)
    assert launchd.install()[0]
    assert launchd.uninstall()[0]
    assert not Path(launchd.plist_path()).exists()


@pytest.mark.parametrize("available", ["ccm", "python", "path", None])
def test_find_menubar(monkeypatch, tmp_path, available):
    candidates = {}
    for name in ("ccm", "python", "path"):
        bindir = tmp_path / name
        bindir.mkdir()
        candidate = bindir / "ccm-menubar"
        candidate.touch()
        candidate.chmod(0o755 if name == available else 0o644)
        candidates[name] = str(candidate)
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "ccm" / "ccm")])
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python" / "python"))
    monkeypatch.setattr(launchd.shutil, "which", lambda name: (
        candidates["path"] if available else None
    ))
    assert launchd.find_menubar() == (candidates[available] if available else None)


@pytest.mark.parametrize("action", ["install", "uninstall"])
@pytest.mark.parametrize("ok", [True, False])
def test_cli_menubar(monkeypatch, capsys, action, ok):
    monkeypatch.setattr(launchd, action, lambda: (ok, "result message"))
    assert cli.main(["menubar", action]) == (0 if ok else 1)
    assert capsys.readouterr().out == "result message\n"
