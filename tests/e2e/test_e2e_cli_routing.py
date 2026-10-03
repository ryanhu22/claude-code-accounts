"""Routing rules through the command line: use, swap, pin, unpin, profiles,
where, resolve, and the shell wrapper that `shell-init` prints, run in zsh."""
import json
import os
import subprocess

import pytest

from e2e.harness import TERM_ID, plain

RESTART_HINT = ("A session started before it had a directory of its own keeps its account "
                "until it restarts: ctrl+C twice, then `claude -c`.")
CODEX_HINT = "Codex reads its login when it starts: restart codex in that terminal."
GIT_ENV = {"GIT_AUTHOR_NAME": "e2e", "GIT_AUTHOR_EMAIL": "e2e@example.com",
           "GIT_COMMITTER_NAME": "e2e", "GIT_COMMITTER_EMAIL": "e2e@example.com"}


def rules(sandbox) -> dict:
    with open(os.path.join(sandbox.home, ".claude-manager", "config.json")) as f:
        return json.load(f)


def routes(sandbox) -> list[str]:
    with open(os.path.join(sandbox.home, ".claude-manager", "routes.conf")) as f:
        return [line for line in f.read().splitlines() if line and not line.startswith("#")]


def git_repo(sandbox, name: str, worktree: str | None = None) -> tuple[str, str]:
    """A repository under the sandbox home, with a worktree when asked."""
    repo = os.path.join(sandbox.home, name)
    os.makedirs(repo)
    env = {**sandbox.env, **GIT_ENV}
    subprocess.run(["git", "init", "-q", repo], check=True, env=env)
    subprocess.run(["git", "-C", repo, "commit", "-q", "--allow-empty", "-m", "start"],
                   check=True, env=env)
    wt = ""
    if worktree:
        wt = os.path.join(repo, ".claude", "worktrees", worktree)
        subprocess.run(["git", "-C", repo, "worktree", "add", "-q", wt], check=True, env=env)
    return repo, wt


@pytest.fixture
def two_claude(sandbox):
    sandbox.seed_claude("work", "work@example.com", tier="default_claude_max_20x")
    sandbox.seed_claude("personal", "personal@example.com", tier="default_claude_pro")


def test_a_project_rule_by_nickname_then_swap(sandbox, two_claude, run_ccm):
    r = run_ccm("use", "wo")
    assert r.returncode == 0, r.stderr
    assert plain(r.stdout).splitlines() == ["“home” now uses work", RESTART_HINT]
    assert rules(sandbox)["projects"] == {"~": "work"}
    assert f"path:{sandbox.home}={sandbox.slot('work')}" in routes(sandbox)
    out = plain(run_ccm("where").stdout)
    assert "account : work  work@example.com" in out
    assert "because : a rule for this project" in out
    assert "dir     : ~/.claude-accts/work" in out
    assert run_ccm("resolve", env={"TERM_SESSION_ID": ""}).stdout.strip() == sandbox.slot("work")
    # swap is the same verb.
    r = run_ccm("swap", "personal")
    assert r.returncode == 0 and plain(r.stdout).splitlines()[0] == "“home” now uses personal"
    assert rules(sandbox)["projects"] == {"~": "personal"}
    out = plain(run_ccm("list").stdout)
    assert "personal  personal@example.com · Pro  <- default, project ~" in out
    assert "work  work@example.com · Max 20x\n" in out


def test_every_scope_in_order_of_specificity(sandbox, two_claude, run_ccm):
    repo, _ = git_repo(sandbox, "repo")
    r = run_ccm("use", "work", "--default")
    assert plain(r.stdout).splitlines()[0] == "everything with no rule now uses work"
    assert plain(run_ccm("where", cwd=repo).stdout).count("no rule covers it") == 1
    r = run_ccm("profile", "new", "team", "personal")
    assert r.returncode == 0 and plain(r.stdout) == "profile “team” created\n"
    r = run_ccm("profile", "add", "team", cwd=repo)
    assert r.returncode == 0 and plain(r.stdout) == "repo joined “team”\n"
    out = plain(run_ccm("where", cwd=repo).stdout)
    assert "account : personal" in out and "because : the “team” profile" in out
    r = run_ccm("use", "work", "--profile", "team")
    assert plain(r.stdout).splitlines()[0] == "profile “team” (1 repo) now uses work"
    assert "account : work" in plain(run_ccm("where", cwd=repo).stdout)
    # A project rule beats the profile, a pin beats the project.
    run_ccm("use", "personal", cwd=repo)
    assert "because : a rule for this project" in plain(run_ccm("where", cwd=repo).stdout)
    r = run_ccm("pin", "work", cwd=repo)
    assert plain(r.stdout).splitlines() == ["this session now uses work", RESTART_HINT]
    out = plain(run_ccm("where", cwd=repo).stdout)
    assert "account : work" in out and "because : pinned to this terminal" in out
    # Another terminal is not pinned.
    out = plain(run_ccm("where", cwd=repo, env={"TERM_SESSION_ID": "other"}).stdout)
    assert "account : personal" in out and "a rule for this project" in out
    out = plain(run_ccm("profiles").stdout)
    assert "everything else        work (work@example.com)" in out
    assert "team                   work (work@example.com)  1 repo\n  ~/repo" in out
    assert "project ~/repo                       personal (personal@example.com)" in out
    assert f"session {TERM_ID[:13]}                work (work@example.com)" in out
    r = run_ccm("unpin", cwd=repo)
    assert r.returncode == 0 and plain(r.stdout) == "this session follows its profile again\n"
    assert "a rule for this project" in plain(run_ccm("where", cwd=repo).stdout)
    assert rules(sandbox) == {
        "version": 1, "default_account": "work",
        "profiles": [{"name": "team", "account": "work", "repos": ["~/repo"],
                      "codex_account": ""}],
        "projects": {"~/repo": "personal"}, "sessions": {},
        "codex_default_account": "", "codex_projects": {}, "codex_sessions": {}}
    assert routes(sandbox) == [
        f"default={sandbox.slot('work')}",
        f"path:{repo}={sandbox.slot('work')}",
        f"path:{repo}={sandbox.slot('personal')}"]


def test_a_worktree_follows_its_checkout(sandbox, two_claude, run_ccm):
    repo, wt = git_repo(sandbox, "repo", worktree="wt1")
    run_ccm("use", "work", "--default")
    r = run_ccm("use", "personal", cwd=repo)
    assert plain(r.stdout).splitlines()[0] == "“repo” now uses personal"
    out = plain(run_ccm("where", cwd=wt).stdout)
    assert out.splitlines()[0] == "~/repo/.claude/worktrees/wt1"
    assert "account : personal" in out and "because : a rule for this project" in out
    # A rule set from inside the worktree lands on the checkout too.
    r = run_ccm("use", "work", cwd=wt)
    assert plain(r.stdout).splitlines()[0] == "“repo” now uses work"
    assert rules(sandbox)["projects"] == {"~/repo": "work"}
    # Without a terminal the shell gets the account's own dir; with one, a dir
    # of its own holding that account's login.
    assert run_ccm("resolve", cwd=wt, env={"TERM_SESSION_ID": ""}).stdout.strip() \
        == sandbox.slot("work")
    session_dir = run_ccm("resolve", cwd=wt).stdout.strip()
    assert session_dir == os.path.join(sandbox.home, ".claude-ctx", "s-e2e0000000")
    assert sandbox.blob(session_dir)["accessToken"] == sandbox.blob(sandbox.slot("work"))[
        "accessToken"]


def test_nicknames_must_be_unique(sandbox, two_claude, run_ccm):
    sandbox.seed_claude("workshop", "workshop@example.com")
    sandbox.seed_codex("gpt", "gpt@example.com")
    r = run_ccm("use", "wor")
    assert r.returncode == 1 and r.stdout.strip() == "'wor' matches work, workshop"
    r = run_ccm("use", "work")
    assert r.returncode == 0, "the exact name wins over the prefix"
    r = run_ccm("use", "shop")
    assert r.returncode == 0 and plain(r.stdout).startswith("“home” now uses workshop")
    r = run_ccm("use", "zzz")
    assert r.returncode == 1
    assert r.stdout.strip() == "no account matches 'zzz' (have: personal, work, workshop, gpt)"
    for args in (("pin", "wor"), ("poke", "wor"), ("reset", "wor", "-y")):
        r = run_ccm(*args)
        assert r.returncode == 1 and r.stdout.strip().endswith("'wor' matches work, workshop")
    assert rules(sandbox)["projects"] == {"~": "workshop"}


def test_profile_edits_and_their_refusals(sandbox, two_claude, run_ccm):
    repo, _ = git_repo(sandbox, "repo")
    assert run_ccm("profile", "new", "team").returncode == 0
    r = run_ccm("profile", "new", "team", "personal")
    assert r.returncode == 1 and r.stdout.strip() == "there is already a profile named team"
    r = run_ccm("profile", "new", "lost", "nobody")
    assert r.returncode == 1 and r.stdout.strip().startswith("no account matches 'nobody'")
    r = run_ccm("profile", "add", "nope", "--path", repo)
    assert r.returncode == 1 and r.stdout.strip() == "no profile named nope"
    r = run_ccm("profile", "add", "team", "--path", repo)
    assert r.returncode == 0 and plain(r.stdout) == "repo joined “team”\n"
    assert rules(sandbox)["profiles"][0]["repos"] == ["~/repo"]
    r = run_ccm("profile", "rename", "team", "crew")
    assert r.returncode == 0 and plain(r.stdout) == "“team” is now “crew”\n"
    r = run_ccm("profile", "rename", "crew", "crew")
    assert r.returncode == 1 and r.stdout.strip() == "pick a name that is not already taken"
    r = run_ccm("profile", "drop", "crew", "--path", repo)
    assert r.returncode == 0 and plain(r.stdout) == "repo left “crew”\n"
    assert rules(sandbox)["profiles"][0]["repos"] == []
    r = run_ccm("use", "work", "--profile", "nope")
    assert r.returncode == 1 and r.stdout.strip() == "no profile named nope"
    r = run_ccm("profile", "rm", "crew")
    assert r.returncode == 0
    assert plain(r.stdout) == "profile “crew” removed; its repos follow the default again\n"
    r = run_ccm("profile", "rm", "crew")
    assert r.returncode == 1 and r.stdout.strip() == "no profile named crew"
    assert rules(sandbox)["profiles"] == []


def test_codex_accounts_route_beside_claude_ones(sandbox, two_claude, run_ccm):
    sandbox.seed_codex("gpt", "gpt@example.com", plan="plus")
    sandbox.seed_codex("gpt2", "gpt2@example.com", plan="prolite")
    run_ccm("use", "work", "--default")
    out = plain(run_ccm("where").stdout)
    assert out.split("\n\n")[1].splitlines() == [
        "  codex", "  account : gpt", "  because : no rule covers it, so the default applies",
        "  dir     : ~/.codex-accts/gpt"]
    r = run_ccm("use", "gpt2")
    assert r.returncode == 0
    assert plain(r.stdout).splitlines() == ["“home” now uses gpt2", CODEX_HINT]
    saved = rules(sandbox)
    assert saved["codex_projects"] == {"~": "gpt2"} and saved["codex_default_account"] == "gpt"
    assert saved["projects"] == {} and saved["default_account"] == "work", "Claude untouched"
    assert f"codex-path:{sandbox.home}={sandbox.codex_slot('gpt2')}" in routes(sandbox)
    assert f"codex-default={sandbox.codex_slot('gpt')}" in routes(sandbox)
    out = plain(run_ccm("where").stdout)
    assert "account : work" in out
    assert "  account : gpt2\n  because : a rule for this project" in out
    # The Codex side of the shell gets a home of its own that links to the login.
    home = run_ccm("resolve", "--codex").stdout.strip()
    assert home == os.path.join(sandbox.home, ".codex-ctx", "s-e2e0000000")
    assert os.readlink(os.path.join(home, "auth.json")) \
        == os.path.join(sandbox.codex_slot("gpt2"), "auth.json")
    assert run_ccm("resolve", "--codex", env={"TERM_SESSION_ID": ""}).stdout.strip() \
        == sandbox.codex_slot("gpt2")
    r = run_ccm("pin", "gpt")
    assert plain(r.stdout).splitlines()[0].startswith("this session now uses gpt")
    assert rules(sandbox)["codex_sessions"] == {TERM_ID: "gpt"}
    assert "  account : gpt\n  because : pinned to this terminal" in plain(run_ccm("where").stdout)
    assert os.readlink(os.path.join(home, "auth.json")) \
        == os.path.join(sandbox.codex_slot("gpt"), "auth.json")
    r = run_ccm("unpin")
    assert r.returncode == 1, "the Claude side has no pin"
    r = run_ccm("unpin", "--codex")
    assert r.returncode == 0 and plain(r.stdout) == "this session follows its profile again\n"
    assert rules(sandbox)["codex_sessions"] == {}
    out = plain(run_ccm("profiles").stdout)
    assert "everything else        work (work@example.com)\n" \
           "everything else        gpt codex\n" in out
    assert "project ~                            gpt2 codex" in out
    out = plain(run_ccm("list").stdout)
    assert "gpt codex  gpt@example.com · Plus  <- default" in out
    assert "gpt2 codex  gpt2@example.com · Pro Lite  <- project ~" in out


def test_the_shell_wrapper_launches_each_tool_in_the_resolved_dir(sandbox, two_claude, run_ccm):
    sandbox.seed_codex("gpt", "gpt@example.com")
    repo, wt = git_repo(sandbox, "repo", worktree="wt1")
    run_ccm("use", "work", "--default")
    run_ccm("use", "personal", cwd=repo)
    r = run_ccm("shell-init")
    assert r.returncode == 0, r.stderr
    snippet = r.stdout
    resolver = os.path.join(sandbox.home, ".claude-manager", "resolve.zsh")
    assert f'zsh "{resolver}"' in snippet and os.access(resolver, os.X_OK)
    assert run_ccm("shell-init", "bash").stdout == snippet

    def launch(cwd: str, script: str = "claude --probe\ncodex --probe\n",
               env: dict | None = None) -> list[dict]:
        before = len(sandbox.tool_calls())
        subprocess.run(["/bin/zsh", "-c", snippet + "\n" + script], capture_output=True,
                       text=True, cwd=cwd, env={**sandbox.env, **(env or {})}, timeout=60)
        return [c["env"] for c in sandbox.tool_calls()[before:] if "--probe" in c["argv"]]

    session_dir = os.path.join(sandbox.home, ".claude-ctx", "s-e2e0000000")
    codex_home = os.path.join(sandbox.home, ".codex-ctx", "s-e2e0000000")
    for cwd, account in ((sandbox.home, "work"), (repo, "personal"), (wt, "personal")):
        claude, codex = launch(cwd)
        assert claude["CLAUDE_CONFIG_DIR"] == session_dir, cwd
        assert sandbox.blob(session_dir)["accessToken"] \
            == sandbox.blob(sandbox.slot(account))["accessToken"], cwd
        assert codex["CODEX_HOME"] == codex_home
    assert os.readlink(os.path.join(codex_home, "auth.json")) \
        == os.path.join(sandbox.codex_slot("gpt"), "auth.json")
    # An explicit directory wins, which is what signing in through Claude Code relies on.
    (claude,) = launch(repo, "CLAUDE_CONFIG_DIR=/explicit claude --probe\n")
    assert claude["CLAUDE_CONFIG_DIR"] == "/explicit"
    # The aliases and helpers exist.
    r = subprocess.run(["/bin/zsh", "-c", snippet + "\nalias; type ccadd ccresume ccpick"],
                       capture_output=True, text=True, cwd=repo, env=sandbox.env, timeout=60)
    for name in ("subs='ccm list'", "ccwhoami='ccm where'", "ccsessions='ccm sessions'",
                 "ccprofiles='ccm profiles'", "ccuse='ccm use'", "ccpin='ccm use --session'",
                 "ccunpin='ccm unpin'", "ccadd is a shell function", "ccresume is a shell function",
                 "ccpick is a shell function"):
        assert name in r.stdout, name
    # With ccm gone the resolver's plain-text table still routes, to the
    # account's own dir: right, only not per terminal.
    os.rename(os.path.join(sandbox.bin, "ccm"), os.path.join(sandbox.bin, "ccm.off"))
    try:
        for cwd, account in ((sandbox.home, "work"), (repo, "personal"), (wt, "personal")):
            claude, codex = launch(cwd)
            assert claude["CLAUDE_CONFIG_DIR"] == sandbox.slot(account), cwd
            assert codex["CODEX_HOME"] == sandbox.codex_slot("gpt"), cwd
    finally:
        os.rename(os.path.join(sandbox.bin, "ccm.off"), os.path.join(sandbox.bin, "ccm"))
