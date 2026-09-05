import pytest

from claude_code_manager import core, profiles, shell


def test_zsh_init():
    snippet = shell.init(core.ACCOUNTS_DIR, "zsh")
    assert "claude()" in snippet
    assert profiles.RESOLVER in snippet


def test_unsupported_shell():
    with pytest.raises(ValueError, match="unsupported shell"):
        shell.init(core.ACCOUNTS_DIR, "fish")
