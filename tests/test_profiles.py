import os
import shutil
import subprocess
from pathlib import Path

import pytest

from claude_code_manager import profiles


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
