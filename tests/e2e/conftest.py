"""Fixtures for running the real `ccm` command in a sandbox.

Every test here is marked `e2e` (select with `-m e2e`, skip with `-m "not
e2e"`). The parent conftest's `isolated_home` is replaced: that one forbids
every subprocess and socket, and these tests exist to run ccm as a
subprocess against a loopback server. What stays is the rule that nothing
reaches a real host, a real keychain or a real home directory.
"""
import os
import socket

import pytest

from claude_code_accounts import codex_sessions, core
from e2e.fake_server import FakeServer
from e2e.harness import Sandbox
from fakes import redirect_home

LOOPBACK = ("127.0.0.1", "localhost", "::1")


def pytest_collection_modifyitems(items):
    here = os.path.dirname(__file__)
    for item in items:
        if str(item.path).startswith(here):
            item.add_marker(pytest.mark.e2e)


@pytest.fixture
def fake_server():
    server = FakeServer().start()
    yield server
    server.close()


@pytest.fixture
def sandbox(tmp_path, fake_server):
    return Sandbox(tmp_path / "sandbox", fake_server)


@pytest.fixture
def run_ccm(sandbox):
    """`run_ccm("list")` -> CompletedProcess of the real entry point, inside the sandbox."""
    return sandbox.run


@pytest.fixture
def fleet(sandbox):
    """The Claude Code sessions a test starts (`lifecycle.Fleet`); each one is
    a real process, killed after the test."""
    from e2e.lifecycle import Fleet

    fleet = Fleet(sandbox)
    yield fleet
    fleet.stop()
    # A refresh token that went to the server twice strands a copy of the
    # login on the real server. A test that stages that on purpose says so.
    reused = sandbox.server.reused_refresh_tokens
    if reused and not getattr(sandbox.server, "allow_reuse", False):
        pytest.fail(f"a refresh token was sent twice: {reused}")


@pytest.fixture
def app_running(monkeypatch):
    """The menu bar app is up: this process is the only thing that may refresh,
    and the credential log is written, as the app writes it."""
    monkeypatch.setattr(core, "SOLE_REFRESHER", True)
    core.enable_file_log()


@pytest.fixture(autouse=True)
def isolated_home(sandbox, monkeypatch):
    """This process lives in the sandbox too, for code run in-process.

    Replaces tests/conftest.py's fixture of the same name. Sockets may reach
    loopback and nothing else; subprocesses are allowed, and find the stubs
    first on PATH because the sandbox environment is applied to this
    process as well.
    """
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)
    sandbox.apply_in_process(monkeypatch.setenv, monkeypatch.setattr)
    redirect_home(sandbox.home, monkeypatch.setattr)
    monkeypatch.setattr(core, "ROTATION_PAUSED", False)
    monkeypatch.setattr(core, "_REFUSED", {})
    monkeypatch.setattr(core, "_CODEX_ADOPTED", False)
    monkeypatch.setattr(codex_sessions, "_FILES", {})
    monkeypatch.setattr(codex_sessions, "_digests", {})
    connect = socket.socket.connect

    def loopback_only(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in LOOPBACK:
            pytest.fail(f"e2e tests must not contact {host!r}; only loopback is allowed")
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
