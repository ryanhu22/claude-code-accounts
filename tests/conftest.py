"""Keep tests away from the user's accounts, processes and network."""

import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest

from claude_code_manager import codex, core, profiles, transcripts


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "TERM_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    for module in (core, codex, profiles):
        monkeypatch.setattr(module, "HOME", str(home))
    paths = {
        core: {
            "ACCOUNTS_DIR": home / ".claude-accts",
            "DEFAULT_CONFIG": home / ".claude",
            "USAGE_CACHE": home / ".claude-accts/.usage-cache.json",
            "IDENTITY_CACHE": home / ".claude-accts/.identity.json",
            "CHIP_FILE": home / ".claude-accts/.chips.json",
            "PREFS_FILE": home / ".claude-manager/prefs.json",
            "STASH_DIR": home / ".claude-manager/pending",
            "SESSION_DIRS": home / ".claude-ctx",
        },
        codex: {
            "ACCOUNTS_DIR": home / ".codex-accts",
            "DEFAULT_HOME": home / ".codex",
        },
        profiles: {
            "CCM_HOME": home / ".claude-manager",
            "CONFIG": home / ".claude-manager/config.json",
            "ROUTES": home / ".claude-manager/routes.conf",
            "RESOLVER": home / ".claude-manager/resolve.zsh",
        },
        transcripts: {"TOKEN_CACHE": home / ".claude-accts/.tokens.json"},
    }
    for module, constants in paths.items():
        for name, path in constants.items():
            monkeypatch.setattr(module, name, str(path))
    monkeypatch.setattr(core, "_CHIPS", (-1.0, {}))
    monkeypatch.setattr(transcripts, "_tokens", None)
    monkeypatch.setattr(transcripts, "_paths", {})
    monkeypatch.setattr(transcripts, "_digests", {})

    def blocked(*args, **kwargs):
        pytest.fail("Tests must not contact the network or launch external tools")

    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    original_popen = subprocess.Popen

    def resolver_only(args, *positional, **kwargs):
        # The one shell test needs only zsh builtins, with no tools on PATH.
        env = kwargs.get("env") or {}
        if (isinstance(args, list) and len(args) == 2
                and Path(args[0]).name == "zsh" and args[1] == profiles.RESOLVER
                and env.get("PATH") == str(tmp_path / "empty-bin")
                and not env.get("TERM_SESSION_ID")):
            return original_popen(args, *positional, **kwargs)
        blocked()

    monkeypatch.setattr(subprocess, "Popen", resolver_only)
