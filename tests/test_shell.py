import pytest

from claude_code_accounts import core, profiles, shell


def test_zsh_init():
    snippet = shell.init(core.ACCOUNTS_DIR, "zsh")
    assert "claude()" in snippet
    assert profiles.RESOLVER in snippet


def test_zsh_init_wraps_codex_too():
    snippet = shell.init(core.ACCOUNTS_DIR, "zsh")
    assert "codex()" in snippet and "_cx_home" in snippet
    # The same resolver script, asked for the other provider.
    assert f'zsh "{profiles.RESOLVER}" codex' in snippet
    assert 'CODEX_HOME="${CODEX_HOME:-$(_cx_home)}" command codex "$@"' in snippet


def test_unsupported_shell():
    with pytest.raises(ValueError, match="unsupported shell"):
        shell.init(core.ACCOUNTS_DIR, "fish")
