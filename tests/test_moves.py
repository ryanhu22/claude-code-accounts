"""User moves pin credential behavior and the cost of each keychain operation."""

import json
import os
import time
import urllib.error
from contextlib import contextmanager
from pathlib import Path

import pytest

from claude_code_manager import cli, codex, core, keychain, locks, oauth, profiles, sessions
from fakes import session, sign_in

pytestmark = pytest.mark.usefixtures("fake_keychain", "fake_api", "no_git", "fast_locks")


@contextmanager
def budget(fake, reads, writes=0, deletes=0):
    fake.reset()
    yield
    assert (fake.reads, fake.writes, fake.deletes) == (reads, writes, deletes)


def stored(fake, path):
    return json.loads(fake.store[keychain.service_for(path)])["claudeAiOauth"]


def known(name, email, api, fake, **kwargs):
    path = sign_in(name, email, api, **kwargs)
    core.identity(path, stored(fake, path))
    return path


def all_rules(name, other="b"):
    return profiles.Rules(name, [profiles.Profile("work", name, ["/repo"])],
                          {"/repo": name, "/keep": other}, {"term": name, "keep": other})


def test_assign_project_then_apply(fake_keychain, fake_api):
    known("a", "a@example.com", fake_api, fake_keychain)
    target = known("b", "b@example.com", fake_api, fake_keychain)
    core.save_rules(profiles.Rules(default_account="a"))
    live = session("term", "/repo", "a")
    # Rule edits with no immediate application make no keychain calls.
    with budget(fake_keychain, 0):
        assert core.assign("project", "/repo", "b", live=[])[0]
    assert f"path:/repo={target}" in Path(profiles.ROUTES).read_text()
    # 1 slot read + 1 session read + 1 session write.
    with budget(fake_keychain, 2, 1):
        moved, applied = core.apply_now([live])
    assert moved == [live.label]
    assert applied == {live.config_dir: "b"}
    assert stored(fake_keychain, live.config_dir) == stored(fake_keychain, target)


def test_assign_and_clear_scopes(fake_keychain, fake_api):
    sign_in("a", "a@example.com", fake_api)
    codex.ensure_account_dir("cx")
    # Every scope edit is file work when there are no sessions to apply.
    with budget(fake_keychain, 0):
        assert core.assign("default", "", "a", live=[])[0]
    assert core.rules_using("a") == ["default"]
    with budget(fake_keychain, 0):
        assert core.add_profile("work", live=[])[0]
    with budget(fake_keychain, 0):
        assert core.profile_add_repo("work", "/repo", live=[])[0]
    with budget(fake_keychain, 0):
        assert core.assign("profile", "work", "a", live=[])[0]
    assert core.rules().account_for("/repo") == ("a", "profile:work")
    assert core.rules_using("a") == ["default", "profile work"]
    with budget(fake_keychain, 0):
        assert core.assign("session", "term", "a", live=[])[0]
    assert core.rules().sessions == {"term": "a"}
    assert "1 session rule" in core.rules_using("a")
    with budget(fake_keychain, 0):
        assert core.clear("session", "term", live=[])[0]
    assert core.rules().sessions == {}
    assert "1 session rule" not in core.rules_using("a")
    with budget(fake_keychain, 0):
        assert core.assign("project", "/repo", "a", live=[])[0]
    assert "project repo" in core.rules_using("a")
    with budget(fake_keychain, 0):
        assert core.clear("project", "/repo", live=[])[0]
    assert core.rules().projects == {}
    assert "project repo" not in core.rules_using("a")
    for name, message in (("cx", "not supported"), ("missing", "no account matches")):
        with budget(fake_keychain, 0):
            ok, error = core.assign("default", "", name, live=[])
        assert not ok and message in error


def test_apply_reads_each_account_once(fake_keychain, fake_api, monkeypatch):
    slot = known("a", "a@example.com", fake_api, fake_keychain)
    core.save_rules(profiles.Rules(default_account="a"))
    live = [session(f"term{i}", "/repo", "a") for i in range(3)]
    newer = fake_api.blob("a@example.com", gen=2, expires_in=7200)
    keychain.write_credentials(slot, newer)
    loads = []
    original = core.rules

    def rules():
        loads.append(True)
        return original()

    monkeypatch.setattr(core, "rules", rules)
    # 1 slot read + 3 session reads + 3 session writes, then no writes when current.
    for writes in (3, 0):
        loads.clear()
        with budget(fake_keychain, 4, writes):
            moved, applied = core.apply_now(live)
        assert len(loads) == 1
        assert len(moved) == writes
        assert applied == ({s.config_dir: "a" for s in live} if writes else {})
        assert all(stored(fake_keychain, s.config_dir) == newer for s in live)


def test_newer_session_survives_until_sync(fake_keychain, fake_api):
    slot = known("a", "a@example.com", fake_api, fake_keychain)
    other = known("b", "b@example.com", fake_api, fake_keychain)
    core.save_rules(profiles.Rules(default_account="a"))
    live = session("term", "/repo", "a")
    newer = fake_api.blob("a@example.com", gen=2, expires_in=7200)
    keychain.write_credentials(live.config_dir, newer)
    fake_api.reset()
    # 1 slot read + 1 session read; the session owns the live generation.
    with budget(fake_keychain, 2):
        assert core.apply_now([live]) == ([], {})
    with budget(fake_keychain, 2):
        assert core.prepare_session("term", "a") == live.config_dir
    assert stored(fake_keychain, live.config_dir) == newer
    assert fake_api.profile_calls == 0
    with budget(fake_keychain, 0):
        assert core.assign("default", "", "b", live=[])[0]
    # A different account replaces the session even when its expiry is earlier.
    with budget(fake_keychain, 2, 1):
        assert core.apply_now([live]) == ([live.label], {live.config_dir: "b"})
    assert stored(fake_keychain, live.config_dir) == stored(fake_keychain, other)
    with budget(fake_keychain, 0):
        assert core.assign("default", "", "a", live=[])[0]
    # Restore the saved live copy to model a peer that still holds it.
    keychain.write_credentials(live.config_dir, newer)
    # 1 slot read + 1 session read + 1 promotion write.
    with budget(fake_keychain, 2, 1):
        assert core.sync_credentials([live]) == [slot]
    assert stored(fake_keychain, slot) == newer


def test_sync_reads_each_copy_once(fake_keychain, fake_api):
    slots = {n: known(n, f"{n}@example.com", fake_api, fake_keychain) for n in ("a", "b")}
    core.save_rules(profiles.Rules(projects={"/a": "a", "/b": "b"}))
    live = [session(f"{n}{i}", f"/{n}", n) for n in slots for i in range(2)]
    # 2 slot reads + 4 session reads, with no writes for current copies.
    with budget(fake_keychain, 6):
        assert core.sync_credentials(live) == []
    keychain.write_credentials(
        live[0].config_dir, fake_api.blob("a@example.com", gen=0, expires_in=1800))
    # The same 6 reads + 1 write for the single copy behind.
    with budget(fake_keychain, 6, 1):
        assert core.sync_credentials(live) == [live[0].config_dir]
    newer = fake_api.blob("a@example.com", gen=2, expires_in=7200)
    keychain.write_credentials(slots["a"], newer)
    # The same 6 reads + 2 writes for the rotated master's sessions.
    with budget(fake_keychain, 6, 2):
        assert core.sync_credentials(live) == [s.config_dir for s in live[:2]]
    assert all(stored(fake_keychain, s.config_dir) == newer for s in live[:2])


def test_live_blob_refresh_and_failures(fake_keychain, fake_api, monkeypatch):
    slot = known("a", "a@example.com", fake_api, fake_keychain, fresh=False)
    old = stored(fake_keychain, slot)
    path = core.session_dir("term")
    Path(path).mkdir(parents=True)
    keychain.write_credentials(path, old)
    monkeypatch.setattr(sessions, "discover_config_dirs", lambda home: [path])
    fake_api.reset()
    # 2 slot reads + 2 default fallbacks + 2 peer reads, with 2 rotation writes.
    with budget(fake_keychain, 6, 2):
        rotated = core.live_blob(slot)
    assert fake_api.refresh_calls == 1
    assert rotated["refreshToken"] != old["refreshToken"]
    assert stored(fake_keychain, slot) == rotated
    assert core.fingerprint(stored(fake_keychain, path)) == core.fingerprint(rotated)
    # A warm account load reads its slot once and uses the carried identity.
    with budget(fake_keychain, 1):
        assert core.load_account("a", with_usage=False).email == "a@example.com"
    assert fake_api.profile_calls == 0
    keychain.write_credentials(slot, old)
    # 2 slot reads for a rejected refresh; no successor is written.
    with budget(fake_keychain, 2):
        assert core.live_blob(slot) is None
    assert fake_api.refresh_calls == 2

    def offline(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(core, "_post", offline)
    # 2 slot reads for a transient failure; the stored copy remains usable.
    with budget(fake_keychain, 2):
        assert core.live_blob(slot) == old
    assert stored(fake_keychain, slot) == old


def test_load_account_heals_rejected_slot(fake_keychain, fake_api, monkeypatch):
    slot = sign_in("a", "a@example.com", fake_api)
    Path(slot, ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": "a@example.com"}}))
    fake_api.rejected.add(stored(fake_keychain, slot)["accessToken"])
    path = core.session_dir("term")
    Path(path).mkdir(parents=True)
    newer = fake_api.blob("a@example.com", gen=2, expires_in=7200)
    keychain.write_credentials(path, newer)
    monkeypatch.setattr(sessions, "discover_config_dirs", lambda home: [path])
    # 1 initial slot + 1 candidate slot + 2 default fallbacks + 1 peer read; 1 heal.
    with budget(fake_keychain, 5, 1):
        acct = core.load_account("a", with_usage=False)
    assert acct.email == "a@example.com"
    assert stored(fake_keychain, slot) == newer


def test_usage_backoff(fake_keychain, fake_api, monkeypatch):
    now = [2000000000.0]
    monkeypatch.setattr(core.time, "time", lambda: now[0])
    blob = fake_api.blob("a@example.com")

    def fetch():
        return fake_api.get("/api/oauth/usage", blob["accessToken"])

    # Usage uses the API and disk cache, with no keychain calls.
    with budget(fake_keychain, 0):
        first = core._usage("a", fetch)
    assert len(first[0]) == 3 and fake_api.usage_calls == 1
    assert core._cache_read()["a"]["data"] == fake_api.usage["a@example.com"]
    now[0] += core._MIN_AGE + 1
    fake_api.fail_next_usage = urllib.error.HTTPError(
        core.API, 429, "Too Many Requests", {"Retry-After": "120"}, None)
    with budget(fake_keychain, 0):
        assert core._usage("a", fetch) == first
    entry = core._cache_read()["a"]
    assert entry["retry_after"] == now[0] + 120
    assert fake_api.usage_calls == 2
    with budget(fake_keychain, 0):
        assert core._usage("a", fetch) == first
    with budget(fake_keychain, 0):
        assert core._usage("a", fetch, force=True) == first
    assert fake_api.usage_calls == 2
    with budget(fake_keychain, 0):
        core.forget_usage("a")
    assert core._cache_read()["a"]["tried_at"] == 0
    with budget(fake_keychain, 0):
        assert core._usage("a", fetch, force=True)[0] == first[0]
    assert fake_api.usage_calls == 3


def test_remove_account_scrubs_rules_and_caches(fake_keychain, fake_api):
    slot = known("a", "a@example.com", fake_api, fake_keychain)
    known("b", "b@example.com", fake_api, fake_keychain)
    core.save_rules(all_rules("a"))
    core._cache_write({"a": {"data": {}}, "b": {"keep": True}})
    core.set_chip_index("a", 3)
    core.set_chip_index("b", 4)
    stash = Path(core._stash_path(slot))
    stash.parent.mkdir(parents=True)
    stash.write_text("{}")
    # Removal deletes one item; rules and caches need no credential reads.
    with budget(fake_keychain, 0, deletes=1):
        assert core.remove_account("a")
    assert keychain.service_for(slot) not in fake_keychain.store
    assert not Path(slot).exists() and not stash.exists()
    r = core.rules()
    assert not core.rules_using("a", r)
    assert r.default_account == "" and r.profile("work").account == ""
    assert r.projects == {"/keep": "b"} and r.sessions == {"keep": "b"}
    assert slot not in Path(profiles.ROUTES).read_text()
    assert core._cache_read() == {"b": {"keep": True}}
    assert slot not in core._cache_read(core.IDENTITY_CACHE)
    assert core._chip_table() == {"b": 4}
    with budget(fake_keychain, 0):
        assert core.assign("project", "/repo", "b", live=[])[0]
    assert core.account_names() == ["b"]


def test_remove_codex_symlink(fake_keychain):
    target = Path(codex.DEFAULT_HOME)
    target.mkdir()
    (target / "auth.json").write_text("{}")
    slot = Path(codex.slot_dir("cx"))
    slot.parent.mkdir()
    slot.symlink_to(target, target_is_directory=True)
    core._cache_write({"codex:cx": {"data": {}}, "keep": {}})
    core.set_chip_index("cx", 2)
    core.save_rules(profiles.Rules(default_account="a"))
    before = core.rules().to_dict()
    # Codex keeps credentials in files, so removal makes no keychain calls.
    with budget(fake_keychain, 0):
        assert core.remove_account("cx")
    assert not slot.is_symlink() and (target / "auth.json").exists()
    assert core._cache_read() == {"keep": {}}
    assert "cx" not in core._chip_table()
    assert core.rules().to_dict() == before


def test_rename_account_carries_rules_and_identity(fake_keychain, fake_api):
    slot = known("a", "a@example.com", fake_api, fake_keychain)
    core.save_rules(all_rules("a"))
    core._cache_write({"a": {"keep": True}})
    core.set_chip_index("a", 3)
    fake_api.reset()
    # 1 old slot read + 1 new slot write + 1 old service delete.
    with budget(fake_keychain, 1, 1, 1):
        assert core.rename_account("a", "renamed")[0]
    new = core.slot_dir("renamed")
    assert not Path(slot).exists() and Path(new).is_dir()
    assert keychain.service_for(slot) not in fake_keychain.store
    assert keychain.service_for(new) in fake_keychain.store
    assert core.rules().to_dict() == all_rules("renamed").to_dict()
    routes = Path(profiles.ROUTES).read_text()
    assert slot not in routes and f"path:/repo={new}" in routes
    assert core._cache_read() == {"renamed": {"keep": True}}
    assert slot not in core._cache_read(core.IDENTITY_CACHE)
    assert core._chip_table() == {"renamed": 3}
    # The moved identity makes a load cost only 1 slot read and no profile fetch.
    with budget(fake_keychain, 1):
        assert core.load_account("renamed", with_usage=False).email == "a@example.com"
    assert fake_api.profile_calls == 0


def test_rename_codex_carries_cache_and_rules(fake_keychain):
    old = Path(codex.ensure_account_dir("cx"))
    (old / "auth.json").write_text("{}")
    core._cache_write({"codex:cx": {"keep": True}})
    core.set_chip_index("cx", 2)
    core.save_rules(all_rules("a"))
    before = core.rules().to_dict()
    names = core.account_names()
    # Codex renames files and makes no keychain calls.
    with budget(fake_keychain, 0):
        assert core.rename_account("cx", "newcx")[0]
    assert not old.exists() and Path(codex.slot_dir("newcx"), "auth.json").exists()
    assert core._cache_read() == {"codex:newcx": {"keep": True}}
    assert core._chip_table() == {"newcx": 2}
    assert core.rules().to_dict() == before
    assert core.account_names() == names
    assert not Path(core.ACCOUNTS_DIR, "cx").exists()
    assert not Path(core.ACCOUNTS_DIR, "newcx").exists()


@pytest.mark.parametrize(("tier", "account", "expected"), [
    ("default_claude_max_5x", None, "Max 5x"),
    ("default_claude_max_20x", None, "Max 20x"),
    ("default_claude_pro", None, "Pro"),
    ("", {"has_claude_max": True}, "Max"),
    ("", {"has_claude_pro": True}, "Pro"),
    ("", {}, ""),
])
def test_plan_label(tier, account, expected):
    assert core.plan_label(tier, account) == expected


@pytest.mark.parametrize("previous_email", ["", "first@example.com"])
def test_sign_in_finish(fake_keychain, fake_api, monkeypatch, previous_email):
    monkeypatch.setattr(time, "time", lambda: 2000000000)
    attempt = oauth.Attempt("verifier", "state", "work", "http://localhost:12345/callback")
    slot = core.slot_dir("work")
    if previous_email:
        fake_api.login_email = previous_email
        assert core.sign_in_finish(attempt, "code#state")[0]
        Path(slot, ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": previous_email}}))
    email = fake_api.login_email = "next@example.com"
    fake_api.reset()
    # 0 reads + 1 adopt write: rebind skips identity; recorded_email reads only JSON.
    with budget(fake_keychain, 0, 1):
        ok, message = core.sign_in_finish(attempt, "code#state")
    assert ok and email in message
    if previous_email:
        assert "used to be" in message and previous_email in message
    assert Path(slot).is_dir()
    blob = stored(fake_keychain, slot)
    assert blob == {
        "accessToken": f"{email}-gen1", "refreshToken": f"{email}-refresh1",
        "expiresAt": 2000003600000, "refreshTokenExpiresAt": 2000086400000,
        "scopes": ["user:inference", "user:profile"],
        "subscriptionType": "max", "rateLimitTier": "default_claude_max_5x",
    }
    assert fake_api.emails[blob["accessToken"]] == email
    entry = core._cache_read(core.IDENTITY_CACHE)[slot]
    assert entry["email"] == email and entry["plan"] == "Max 5x"
    assert entry["fp"] == core.fingerprint(blob)
    assert fake_api.profile_calls == 1


def test_registry_live_and_prune(fake_keychain, tmp_path, monkeypatch):
    dirs = [str(tmp_path / n) for n in ("first", "second")]
    now = time.time()
    files = []
    for directory, pid in ((dirs[0], 101), (dirs[0], 102), (dirs[1], 101), (dirs[1], 103)):
        path = Path(directory, "sessions", f"{pid}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "pid": pid, "sessionId": f"id{pid}", "cwd": "/repo", "kind": "interactive",
            "status": "idle", "startedAt": now * 1000, "updatedAt": now * 1000,
            "name": f"name{pid}", "nameSource": "user", "procStart": "start",
        }))
        os.utime(path, (now - sessions.STALE_AFTER - 1,) * 2)
        files.append(path)
    monkeypatch.setattr(sessions, "alive", lambda pid: {101: True, 102: False, 103: True}[pid])
    monkeypatch.setattr(sessions, "_environ", lambda pid, start: (
        {"TERM_SESSION_ID": f"term{pid}", "CLAUDE_CONFIG_DIR": dirs[1]}, "ttys001"))
    # Registry discovery and pruning read files, not credentials.
    with budget(fake_keychain, 0):
        live = sessions.live(dirs)
    assert {s.pid for s in live} == {101, 103} and len(live) == 2
    assert all(s.env_config_dir == dirs[1] and s.tty == "ttys001" for s in live)
    assert {s.term_id for s in live} == {"term101", "term103"}
    with budget(fake_keychain, 0):
        assert sessions.prune(dirs) == 1
    assert not files[1].exists() and all(p.exists() for p in (files[0], *files[2:]))


def test_resolve_dir_seeds_config(fake_keychain, fake_api):
    slot = known("a", "a@example.com", fake_api, fake_keychain)
    data = {"oauthAccount": {"emailAddress": "a@example.com"}, "hasCompletedOnboarding": True,
            **dict.fromkeys(core._CONFIG_SKIP, "volatile")}
    Path(slot, ".claude.json").write_text(json.dumps(data))
    core.save_rules(profiles.Rules(default_account="a"))
    # Without a terminal, resolution does file work and never reads credentials.
    with budget(fake_keychain, 0):
        assert core.resolve_dir("/repo") == slot
    assert not Path(core.SESSION_DIRS).exists()
    # 1 slot read + 1 session read + 1 initial write.
    with budget(fake_keychain, 2, 1):
        path = core.resolve_dir("/repo", "term")
    assert path == core.session_dir("term")
    assert stored(fake_keychain, path) == stored(fake_keychain, slot)
    assert json.loads(Path(path, ".claude.json").read_text()) == {
        k: v for k, v in data.items() if k not in core._CONFIG_SKIP}
    # A warm launch still reads the slot and session but writes neither.
    with budget(fake_keychain, 2):
        assert core.resolve_dir("/repo", "term") == path


def test_gc_session_dirs(fake_keychain, fake_api):
    sign_in("a", "a@example.com", fake_api)
    live, old, young = [session(t, "/repo", "a") for t in ("live", "old", "young")]
    for s in (live, old):
        os.utime(s.config_dir, (time.time() - 8 * 86400,) * 2)
    # Only the old abandoned terminal costs a keychain delete.
    with budget(fake_keychain, 0, deletes=1):
        assert core.gc_session_dirs(["live"]) == [old.config_dir]
    assert not Path(old.config_dir).exists()
    assert keychain.service_for(old.config_dir) not in fake_keychain.store
    assert all(Path(s.config_dir).exists() for s in (live, young))


def test_credential_locks(fake_keychain, tmp_path):
    directory = str(tmp_path / "locked")
    primary, legacy = map(Path, locks.lock_dirs(directory))
    primary.mkdir(parents=True)
    # Lock acquisition touches only lock directories, not the keychain.
    with budget(fake_keychain, 0), pytest.raises(locks.LockBusy):
        with locks.credentials(directory, timeout=locks.TIMEOUT_SECONDS):
            pytest.fail("a fresh lock must remain held")
    assert primary.exists() and not legacy.exists()
    os.utime(primary, (time.time() - locks.STALE_SECONDS - 1,) * 2)
    with budget(fake_keychain, 0):
        with locks.credentials(directory, timeout=locks.TIMEOUT_SECONDS):
            assert primary.is_dir() and legacy.is_dir()
    assert not primary.exists() and not legacy.exists()


def test_cli_round_trip(fake_keychain, fake_api, monkeypatch, capsys, tmp_path):
    known("a", "a@example.com", fake_api, fake_keychain)
    core.save_rules(profiles.Rules(default_account="a", profiles=[profiles.Profile("work", "a")]))
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sessions, "live", lambda *args, **kwargs: [])
    # A CLI rule edit with no live sessions makes no keychain calls.
    with budget(fake_keychain, 0):
        assert cli.main(["use", "a"]) == 0
    assert "now uses a" in capsys.readouterr().out
    assert core.rules().account_for(str(repo)) == ("a", "project")
    # Where and profiles each load the one account once.
    with budget(fake_keychain, 1):
        assert cli.main(["where"]) == 0
    output = capsys.readouterr().out
    assert "account :" in output and "a@example.com" in output
    with budget(fake_keychain, 1):
        assert cli.main(["profiles"]) == 0
    assert "work" in capsys.readouterr().out
    # Invalid unpin and add's printed command require no credentials.
    with budget(fake_keychain, 0), pytest.raises(SystemExit) as error:
        cli.main(["unpin"])
    assert error.value.code == 1
    capsys.readouterr()
    with budget(fake_keychain, 0):
        assert cli.main(["add", "new"]) == 0
    assert core.slot_dir("new") in capsys.readouterr().out
    live = session("term", str(repo), "a")
    monkeypatch.setattr(sessions, "live", lambda *args, **kwargs: [live])
    # 1 account load + 1 slot fingerprint + 1 session fingerprint.
    with budget(fake_keychain, 3):
        assert cli.main(["sessions"]) == 0
    output = capsys.readouterr().out
    assert live.label in output and "idle" in output and "a" in output


@pytest.mark.parametrize("with_usage", [False, True])
def test_all_accounts_and_directory_mapping(fake_keychain, fake_api, with_usage):
    for name in ("a", "b"):
        known(name, f"{name}@example.com", fake_api, fake_keychain)
    live = [session(f"term{i}", "/repo", name) for i, name in enumerate(("a", "a", "b"))]
    core.all_accounts(with_usage=True)
    fake_api.reset()
    # 2 slot reads; cached usage and identity require no API calls.
    with budget(fake_keychain, 2):
        accts = core.all_accounts(with_usage=with_usage)
    assert [a.email for a in accts] == ["a@example.com", "b@example.com"]
    assert all(bool(a.limits) == with_usage for a in accts)
    assert fake_api.profile_calls == fake_api.usage_calls == 0
    # 2 slot reads + 3 session reads match each copy once.
    with budget(fake_keychain, 5):
        owners = core.dirs_to_accounts([s.config_dir for s in live], accts)
    assert owners == {s.config_dir: name for s, name in zip(live, ("a", "a", "b"), strict=True)}


@pytest.mark.parametrize(("cached", "email", "writes"), [
    ("a@example.com", "a@example.com", 0),
    ("a@example.com", "A@EXAMPLE.COM", 0),
    ("b@example.com", "a@example.com", 1),
    ("", "a@example.com", 1),
    ("a@example.com", "", 1),
])
def test_hand_out_requires_known_same_account(fake_keychain, fake_api, cached, email, writes):
    path = core.session_dir("term")
    Path(path).mkdir(parents=True)
    have = fake_api.blob("a@example.com", gen=2, expires_in=7200)
    want = fake_api.blob("a@example.com", gen=1)
    keychain.write_credentials(path, have)
    core._cache_write({path: {"email": cached}}, core.IDENTITY_CACHE)
    # 1 session read, plus a write unless a known matching account is ahead.
    with budget(fake_keychain, 1, writes):
        assert core.hand_out(path, want, email) is bool(writes)
    assert stored(fake_keychain, path) == (want if writes else have)
