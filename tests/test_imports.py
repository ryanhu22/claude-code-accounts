"""Shell launches keep network and sign-in imports off the resolve path."""

import subprocess
import sys

import conftest


def test_cli_import_leaves_network_and_oauth_unloaded(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", conftest.ORIGINAL_POPEN)
    result = subprocess.run([sys.executable, "-c", """
import sys
import claude_code_manager.core
import claude_code_manager.cli
assert 'urllib.request' not in sys.modules
assert 'claude_code_manager.oauth' not in sys.modules
"""], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
