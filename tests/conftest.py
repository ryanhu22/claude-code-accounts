"""Keep tests away from the user's accounts, processes and network."""

import os
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest

from claude_code_manager import core, locks, profiles
from fakes import FakeApi, FakeKeychain, redirect_home


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "TERM_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    redirect_home(str(home), monkeypatch.setattr)

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


@pytest.fixture
def fake_keychain(monkeypatch):
    fake = FakeKeychain()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def fake_api(monkeypatch):
    fake = FakeApi()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def no_git(monkeypatch):
    monkeypatch.setattr(core, "project_root", lambda cwd: os.path.abspath(cwd))
    monkeypatch.setattr(core, "_ROOTS", {})


@pytest.fixture
def fast_locks(monkeypatch):
    monkeypatch.setattr(locks, "TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(locks, "TOUCH_SECONDS", 0.05)
