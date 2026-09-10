import os
import shutil
import subprocess
from pathlib import Path

import pytest

from claude_code_accounts import profiles


def test_rule_precedence(tmp_path):
    repo = str(tmp_path / "repo")
    rules = profiles.Rules(
        default_account="hobby",
        profiles=[profiles.Profile("work", "acme", [repo])],
        projects={repo: "work"}, sessions={"terminal": "personal"},
    )
    assert rules.account_for(repo, "terminal") == ("personal", "session")
    assert rules.account_for(repo, "other") == ("work", "project")
    rules.projects.clear()
    assert rules.account_for(repo) == ("acme", "profile:work")
    assert rules.account_for(str(tmp_path / "elsewhere")) == ("hobby", "default")


def test_project_precedes_profile_across_paths(tmp_path):
    checkout = str(tmp_path / "checkout")
    worktree = str(tmp_path / "worktree")
    rules = profiles.Rules(
        profiles=[profiles.Profile("work", "acme", [worktree])],
        projects={checkout: "personal"},
    )
    assert rules.account_for([worktree, checkout]) == ("personal", "project")


def test_profile_covers(tmp_path):
    repo = str(tmp_path / "repo")
    profile = profiles.Profile("work", "acme", [repo, repo + "/nested"])
    assert profile.covers(repo) == repo
    assert profile.covers(repo + "/src") == repo
    assert profile.covers(repo + "/nested/src") == repo + "/nested"
    assert profile.covers(repo + "-other") is None


def test_rules_round_trip(tmp_path):
    rules = profiles.Rules(
        default_account="personal",
        profiles=[profiles.Profile("work", "acme", [str(tmp_path / "repo")])],
        projects={str(tmp_path / "project"): "work"}, sessions={"terminal": "hobby"},
    )
    data = rules.to_dict()
    assert data["version"] == 1
    assert profiles.Rules.from_dict(data) == rules
    assert profiles.Rules.from_dict(data).to_dict() == data


def test_codex_rules_round_trip(tmp_path):
    rules = profiles.Rules(
        default_account="personal",
        profiles=[profiles.Profile("work", "acme", [str(tmp_path / "repo")], "cx-work")],
        projects={str(tmp_path / "project"): "work"}, sessions={"terminal": "hobby"},
        codex_default_account="cx", codex_projects={str(tmp_path / "project"): "cx-repo"},
        codex_sessions={"terminal": "cx-pin"},
    )
    data = rules.to_dict()
    assert data["codex_default_account"] == "cx"
    assert data["profiles"][0]["codex_account"] == "cx-work"
    assert profiles.Rules.from_dict(data) == rules
    assert profiles.Rules.from_dict(data).to_dict() == data


def test_rules_without_a_codex_side_still_load(tmp_path):
    old = {"version": 1, "default_account": "personal", "projects": {"/repo": "work"},
           "sessions": {"terminal": "hobby"},
           "profiles": [{"name": "work", "account": "acme", "repos": []}]}
    loaded = profiles.Rules.from_dict(old)
    assert loaded.default_account == "personal" and loaded.projects == {"/repo": "work"}
    assert loaded.codex_default_account == ""
    assert loaded.codex_projects == {} and loaded.codex_sessions == {}
    assert loaded.profile("work").codex_account == ""


def test_each_provider_reads_only_its_own_rules(tmp_path):
    repo = str(tmp_path / "repo")
    rules = profiles.Rules(
        default_account="hobby",
        profiles=[profiles.Profile("work", "acme", [repo], "cx-work")],
        projects={repo: "personal"}, sessions={"terminal": "pinned"},
        codex_default_account="cx", codex_projects={repo: "cx-repo"},
        codex_sessions={"terminal": "cx-pin"},
    )
    assert rules.account_for(repo, "terminal", "codex") == ("cx-pin", "session")
    rules.codex_sessions.clear()
    assert rules.account_for(repo, "terminal", "codex") == ("cx-repo", "project")
    rules.codex_projects.clear()
    assert rules.account_for(repo, "terminal", "codex") == ("cx-work", "profile:work")
    rules.profiles[0].codex_account = ""
    assert rules.account_for(repo, "terminal", "codex") == ("cx", "default")
    # Emptying the codex side moved nothing on the Claude side.
    assert rules.account_for(repo, "terminal") == ("pinned", "session")
    assert rules.account_for(repo) == ("personal", "project")
    assert rules.default("codex") == "cx" and rules.default() == "hobby"


def test_each_provider_edits_only_its_own_rules(tmp_path):
    repo = str(tmp_path / "repo")
    rules = profiles.Rules(profiles=[profiles.Profile("work")])
    rules.set_project(repo, "cx", "codex")
    rules.set_session("terminal", "cx", "codex")
    rules.set_profile_account("work", "cx", "codex")
    rules.set_default("cx", "codex")
    assert rules.codex_projects == {repo: "cx"} and rules.codex_sessions == {"terminal": "cx"}
    assert rules.profile("work").codex_account == "cx"
    assert (rules.default_account, rules.projects, rules.sessions) == ("", {}, {})
    assert rules.profile("work").account == ""
    assert rules.project_rule_for(repo + "/src", "codex") == repo
    assert rules.project_rule_for(repo + "/src") is None


def test_write_routes(tmp_path):
    repo = str(tmp_path / "repo")
    nested = repo + "/nested"
    rules = profiles.Rules(
        default_account="personal", profiles=[profiles.Profile("work", "acme", [repo])],
        projects={nested: "work"}, sessions={"terminal": "hobby"},
    )
    table = {name: str(tmp_path / "slots" / name) for name in ("personal", "acme", "work", "hobby")}
    profiles.write_routes(rules, table.__getitem__)
    lines = [line for line in Path(profiles.ROUTES).read_text().splitlines()
             if line and not line.startswith("#")]
    assert lines == [
        f"default={table['personal']}", f"path:{repo}={table['acme']}",
        f"path:{nested}={table['work']}", f"term:terminal={table['hobby']}",
    ]


def _resolver_path(tmp_path) -> str:
    """A PATH holding the text tools the fallback greps with, and no ccm.

    The fallback is the whole point of these tests, so ccm must be
    unreachable; grep, tail and cut must not be, because the terminal and
    default rules are read with them.
    """
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir(exist_ok=True)
    for tool in ("grep", "tail", "cut"):
        found = shutil.which(tool)
        if found and not (empty_bin / tool).exists():
            os.symlink(found, empty_bin / tool)
    assert shutil.which("ccm", path=str(empty_bin)) is None
    return str(empty_bin)


def test_write_routes_codex_side(tmp_path):
    repo = str(tmp_path / "repo")
    nested = repo + "/nested"
    rules = profiles.Rules(
        default_account="personal", profiles=[profiles.Profile("work", "acme", [repo], "cx-work")],
        codex_default_account="cx", codex_projects={nested: "cx-repo"},
        codex_sessions={"terminal": "cx-pin"},
    )
    slots = {name: str(tmp_path / "slots" / name) for name in ("personal", "acme")}
    homes = {name: str(tmp_path / "homes" / name)
             for name in ("cx", "cx-work", "cx-repo", "cx-pin")}

    def lines() -> list[str]:
        return [line for line in Path(profiles.ROUTES).read_text().splitlines()
                if line and not line.startswith("#")]

    profiles.write_routes(rules, slots.__getitem__, homes.__getitem__)
    assert lines() == [
        f"default={slots['personal']}", f"path:{repo}={slots['acme']}",
        f"codex-default={homes['cx']}", f"codex-path:{repo}={homes['cx-work']}",
        f"codex-path:{nested}={homes['cx-repo']}", f"codex-term:terminal={homes['cx-pin']}",
    ]
    # With no homes to write, the table is the one a Claude-only user gets.
    profiles.write_routes(rules, slots.__getitem__)
    assert lines() == [f"default={slots['personal']}", f"path:{repo}={slots['acme']}"]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_resolver_reads_the_codex_side(tmp_path):
    zsh = shutil.which("zsh")
    repo = tmp_path / "repo"
    cwd = repo / "nested" / "src"
    cwd.mkdir(parents=True)
    slots = {name: str(tmp_path / "slots" / name) for name in ("work", "personal")}
    homes = {name: str(tmp_path / "homes" / name) for name in ("cx", "cx-repo", "cx-pin")}
    rules = profiles.Rules(
        default_account="personal", projects={str(repo): "work"},
        codex_default_account="cx", codex_projects={str(repo): "cx-repo"},
        codex_sessions={"terminal": "cx-pin"},
    )
    profiles.write_routes(rules, slots.__getitem__, homes.__getitem__)
    profiles.write_resolver()
    env = {**os.environ, "PATH": _resolver_path(tmp_path), "ZDOTDIR": str(tmp_path)}
    env.pop("TERM_SESSION_ID", None)

    def resolve(*args, where=cwd, **environment):
        result = subprocess.run([zsh, profiles.RESOLVER, *args], cwd=where,
                                env={**env, **environment}, capture_output=True,
                                text=True, check=True, timeout=3)
        return result.stdout.strip()

    assert resolve("codex", TERM_SESSION_ID="terminal") == homes["cx-pin"]
    assert resolve("codex") == homes["cx-repo"]
    assert resolve("codex", where=tmp_path) == homes["cx"]
    # The same script with no argument still answers for Claude Code.
    assert resolve() == slots["work"]
    assert resolve(where=tmp_path) == slots["personal"]
    # No codex rule at all lands on the home the Codex CLI uses by itself.
    profiles.write_routes(profiles.Rules(default_account="personal"),
                          slots.__getitem__, homes.__getitem__)
    assert resolve("codex", where=tmp_path) == os.path.join(os.environ["HOME"], ".codex")


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_resolver_path_fallback(tmp_path):
    zsh = shutil.which("zsh")
    repo = tmp_path / "repo"
    cwd = repo / "nested" / "src"
    cwd.mkdir(parents=True)
    table = {name: str(tmp_path / "slots" / name) for name in ("work", "personal", "hobby")}
    rules = profiles.Rules(
        default_account="personal", projects={str(repo): "work"},
        sessions={"terminal": "hobby"},
    )
    profiles.write_routes(rules, table.__getitem__)
    profiles.write_resolver()
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    env = {**os.environ, "PATH": str(empty_bin), "ZDOTDIR": str(tmp_path)}
    env.pop("TERM_SESSION_ID", None)
    assert shutil.which("ccm", path=env["PATH"]) is None
    result = subprocess.run([zsh, profiles.RESOLVER], cwd=cwd, env=env,
                            capture_output=True, text=True, check=True, timeout=3)
    assert result.stdout.strip() == table["work"]


def test_add_repo_makes_membership_exclusive(tmp_path):
    repo = str(tmp_path / "repo")
    rules = profiles.Rules(profiles=[profiles.Profile("one", repos=[repo]),
                                    profiles.Profile("two")])
    rules.add_repo("two", repo)
    assert rules.profile("one").repos == []
    assert rules.profile("two").paths == [repo]
    rules.add_repo("two", repo)
    assert rules.profile("two").paths == [repo]


def test_deeper_project_rule_wins(tmp_path):
    root = str(tmp_path)
    deep = str(tmp_path / "repo")
    rules = profiles.Rules(projects={root: "shallow", deep: "deep"})
    assert rules.account_for(deep + "/src") == ("deep", "project")
