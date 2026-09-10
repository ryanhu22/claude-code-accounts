"""The answers Claude Code asks once, shared across every config directory."""

import json
import os
from pathlib import Path

import pytest

from claude_code_accounts import core, locks
from fakes import session, sign_in

pytestmark = pytest.mark.usefixtures("fake_keychain", "fake_api", "no_git", "fast_locks")


def write(config_dir, data):
    """Put a .claude.json in a dir the way Claude Code would have left it."""
    os.makedirs(config_dir, exist_ok=True)
    path = Path(core._config_json(config_dir))
    path.write_text(json.dumps(data))
    return path


def read(config_dir):
    return json.loads(Path(core._config_json(config_dir)).read_text())


def trusts(config_dir, *projects):
    entries = read(config_dir).get("projects") or {}
    return all((entries.get(p) or {}).get("hasTrustDialogAccepted") is True for p in projects)


def test_trust_is_unioned(fake_api):
    slot = sign_in("a", "a@example.com", fake_api)
    write(slot, {"projects": {"/repo": {"hasTrustDialogAccepted": True}}})
    live = session("t1", "/repo", "a")
    write(live.config_dir, {"projects": {
        "/repo": {"hasTrustDialogAccepted": True},
        "/other": {"hasTrustDialogAccepted": True, "allowedTools": ["Bash"]},
    }})
    write(core.DEFAULT_CONFIG, {"projects": {"/third": {"hasTrustDialogAccepted": True}}})

    core.sync_answers()

    for path in (slot, live.config_dir, core.DEFAULT_CONFIG):
        assert trusts(path, "/repo", "/other", "/third")
    # Tools, servers and history stay with the dir that earned them.
    assert read(live.config_dir)["projects"]["/other"]["allowedTools"] == ["Bash"]
    for path in (slot, core.DEFAULT_CONFIG):
        assert "allowedTools" not in read(path)["projects"]["/other"]


def test_chrome_flags_follow_the_user(fake_api):
    slot = sign_in("a", "a@example.com", fake_api)
    one = session("t1", "/repo", "a").config_dir
    two = session("t2", "/repo", "a").config_dir
    write(slot, {"claudeInChromeDefaultEnabled": True})
    write(one, {"claudeInChromeDefaultEnabled": True})
    write(two, {"claudeInChromeDefaultEnabled": False})
    dirs = [slot, one, two]

    # No cache yet, so the odd one out is a dir that was seeded before the
    # answer, not a user turning it off. The majority wins.
    core.sync_answers()
    assert [read(d)["claudeInChromeDefaultEnabled"] for d in dirs] == [True, True, True]
    cache = json.loads(Path(core.ANSWERS_CACHE).read_text())
    assert all(cache[os.path.abspath(d)] is True for d in dirs)

    # A dir that moves since the last pass is where the user just answered.
    write(two, {"claudeInChromeDefaultEnabled": False})
    core.sync_answers()
    assert [read(d)["claudeInChromeDefaultEnabled"] for d in dirs] == [False, False, False]

    write(one, {"claudeInChromeDefaultEnabled": True})
    core.sync_answers()
    assert [read(d)["claudeInChromeDefaultEnabled"] for d in dirs] == [True, True, True]

    write(two, {"claudeInChromeDefaultEnabled": True,
                "hasCompletedClaudeInChromeOnboarding": True})
    core.sync_answers()
    assert all(read(d)["hasCompletedClaudeInChromeOnboarding"] is True for d in dirs)


def test_new_session_starts_with_all_answers(fake_api):
    slot = sign_in("a", "a@example.com", fake_api)
    write(slot, {"projects": {"/repo": {"hasTrustDialogAccepted": True}}})
    write(session("t1", "/other", "a").config_dir, {
        "projects": {"/other": {"hasTrustDialogAccepted": True}},
        "hasCompletedClaudeInChromeOnboarding": True,
        "claudeInChromeDefaultEnabled": True,
        "cachedChromeExtensionInstalled": True,
    })

    fresh = core.prepare_session("t9", "a")

    assert trusts(fresh, "/repo", "/other")
    data = read(fresh)
    assert data["hasCompletedClaudeInChromeOnboarding"] is True
    assert data["claudeInChromeDefaultEnabled"] is True
    assert data["cachedChromeExtensionInstalled"] is True


def test_sync_answers_is_quiet_when_nothing_changes(fake_api):
    slot = sign_in("a", "a@example.com", fake_api)
    write(slot, {"projects": {"/repo": {"hasTrustDialogAccepted": True}},
                 "claudeInChromeDefaultEnabled": False})
    live = session("t1", "/repo", "a").config_dir
    write(live, {"projects": {"/other": {"hasTrustDialogAccepted": True}}})

    assert sorted(core.sync_answers()) == sorted(os.path.abspath(d) for d in (slot, live))
    stamps = {d: os.stat(core._config_json(d)).st_mtime_ns for d in (slot, live)}

    assert core.sync_answers() == []
    assert {d: os.stat(core._config_json(d)).st_mtime_ns for d in (slot, live)} == stamps


def test_sync_answers_skips_busy_dir(fake_api, monkeypatch):
    # A real lock, held for real, but with the wait cut down: locks.config
    # bakes its nine second default in at import, so the suite would sit
    # through the whole timeout otherwise.
    held = locks.config
    monkeypatch.setattr(locks, "config", lambda path, timeout=0.05: held(path, timeout))
    slot = sign_in("a", "a@example.com", fake_api)
    busy = session("t1", "/other", "a").config_dir
    quiet = session("t2", "/repo", "a").config_dir
    before = {"projects": {"/other": {"hasTrustDialogAccepted": True}}}
    write(slot, {"projects": {"/repo": {"hasTrustDialogAccepted": True}}})
    write(busy, before)
    write(quiet, {})

    with locks.config(busy):
        changed = core.sync_answers()

    assert os.path.abspath(busy) not in changed
    assert read(busy) == before            # left exactly as Claude Code had it
    assert trusts(slot, "/repo", "/other")
    assert trusts(quiet, "/repo", "/other")
