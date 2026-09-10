"""User moves pin credential behavior and the cost of each keychain operation."""

import json
import os
import re
import time
import urllib.error
from contextlib import contextmanager
from pathlib import Path

import pytest

from claude_code_accounts import cli, codex, core, keychain, locks, oauth, profiles, sessions
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
    # 1 fresh slot + 1 locked session read + 1 profile token read + 1 write.
    with budget(fake_keychain, 3, 1):
        moved, applied = core.apply_now([live])
    assert moved == [live.label]
    assert applied == {live.config_dir: "b"}
    assert stored(fake_keychain, live.config_dir) == stored(fake_keychain, target)


@pytest.mark.parametrize("live_source", ["list", "iterator", "discovery"])
def test_project_rule_retires_pins_under_it(fake_keychain, fake_api, monkeypatch,
                                          live_source):
    known("a", "a@example.com", fake_api, fake_keychain)
    target = known("b", "b@example.com", fake_api, fake_keychain)
    core.save_rules(profiles.Rules(default_account="a", sessions={"t1": "a", "t3": "a"}))
    live = [session(term, cwd, "a") for term, cwd in
            (("t1", "/repo"), ("t2", "/repo"), ("t3", "/other"))]
    discovered = []

    def discover(dirs):
        discovered.append(dirs)
        return live

    monkeypatch.setattr(sessions, "live", discover)
    supplied = {"list": live, "iterator": iter(live), "discovery": None}[live_source]
    applied = {}
    ok, message = core.assign("project", "/repo", "b", live=supplied, applied_out=applied)
    assert ok and "releasing 1 pinned session" in message
    assert "2 running sessions switch within about 30 seconds" in message
    assert core.rules().sessions == {"t3": "a"}
    assert applied == {s.config_dir: "b" for s in live[:2]}
    assert all(stored(fake_keychain, s.config_dir) == stored(fake_keychain, target)
               for s in live[:2])
    assert len(discovered) == int(live_source == "discovery")


def test_known_sessions_retire_pins_without_applying(fake_keychain, fake_api):
    for name in ("a", "b"):
        known(name, f"{name}@example.com", fake_api, fake_keychain)
    core.save_rules(profiles.Rules(default_account="a", sessions={"t1": "a"}))
    live = [session(term, cwd, "a") for term, cwd in
            (("t1", "/repo"), ("t2", "/other"))]
    applied = {}
    with budget(fake_keychain, 0):
        ok, message = core.assign("project", "/repo", "b", live=[], known=live,
                                  applied_out=applied)
    assert ok and "releasing 1 pinned session" in message
    assert "running session" not in message
    assert core.rules().sessions == {}
    assert applied == {}


def test_profile_rule_retires_pins_but_keeps_project_rule(fake_keychain, fake_api):
    for name in ("a", "b"):
        known(name, f"{name}@example.com", fake_api, fake_keychain)
    core.save_rules(profiles.Rules(
        default_account="a", profiles=[profiles.Profile("work", "a", ["/repo", "/pinned"])],
        projects={"/pinned": "a"}, sessions={"t1": "a", "t2": "a", "t3": "a"}))
    live = [session(term, cwd, "a") for term, cwd in
            (("t1", "/repo"), ("t2", "/pinned"), ("t3", "/elsewhere"))]
    applied = {}
    ok, message = core.assign("profile", "work", "b", live=live, applied_out=applied)
    assert ok and "releasing 1 pinned session" in message
    r = core.rules()
    assert r.sessions == {"t2": "a", "t3": "a"}
    assert r.projects == {"/pinned": "a"}
    assert r.profile("work").account == "b"
    assert applied == {live[0].config_dir: "b"}


def test_default_rule_retires_only_unruled_pins(fake_keychain, fake_api):
    for name in ("a", "b"):
        known(name, f"{name}@example.com", fake_api, fake_keychain)
    core.save_rules(profiles.Rules(
        default_account="a", profiles=[profiles.Profile("work", "a", ["/repo"])],
        projects={"/pinned": "a"}, sessions={"t1": "a", "t2": "a", "t3": "a", "t4": "a"}))
    live = [session(term, cwd, "a") for term, cwd in
            (("t1", "/elsewhere"), ("t2", "/repo"), ("t3", "/pinned"), ("t4", "/other"))]
    applied = {}
    ok, message = core.assign("default", "", "b", live=live, applied_out=applied)
    assert ok and "releasing 2 pinned sessions" in message
    r = core.rules()
    assert r.default_account == "b"
    assert r.sessions == {"t2": "a", "t3": "a"}
    assert applied == {s.config_dir: "b" for s in (live[0], live[3])}


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
    # The pin has a dir from the start, so pruning cannot mistake it for dead.
    assert core.prune_session_rules([]) == []
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
    with budget(fake_keychain, 0):
        ok, error = core.assign("default", "", "missing", live=[])
    assert not ok and "no account matches" in error
    # A Codex account routes on its own side, leaving the Claude default alone.
    with budget(fake_keychain, 0):
        assert core.assign("default", "", "cx", live=[])[0]
    assert core.rules().codex_default_account == "cx"
    assert core.rules().default_account == "a"


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
    # 1 slot read + 3 locked session reads and writes. The second pass
    # reads only the slot because the session pre-reads now hit the memo.
    for writes in (3, 0):
        loads.clear()
        with budget(fake_keychain, 1 + writes, writes):
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
    # 1 slot + 1 locked session read; the pre-read hits the memo.
    # The fresh check keeps the session's live generation in both calls.
    with budget(fake_keychain, 2):
        assert core.apply_now([live]) == ([], {})
    with budget(fake_keychain, 2):
        assert core.prepare_session("term", "a") == live.config_dir
    assert stored(fake_keychain, live.config_dir) == newer
    assert fake_api.profile_calls == 0
    with budget(fake_keychain, 0):
        assert core.assign("default", "", "b", live=[])[0]
    # A different account replaces the session even when its expiry is earlier.
    # Its first identity sync also reads the live token for the profile.
    with budget(fake_keychain, 3, 1):
        assert core.apply_now([live]) == ([live.label], {live.config_dir: "b"})
    assert stored(fake_keychain, live.config_dir) == stored(fake_keychain, other)
    with budget(fake_keychain, 0):
        assert core.assign("default", "", "a", live=[])[0]
    # Restore the saved live copy to model a peer that still holds it.
    keychain.write_credentials(live.config_dir, newer)
    # The comparisons hit the memo. Promotion costs 1 locked slot read + 1 write.
    with budget(fake_keychain, 1, 1):
        assert core.sync_credentials([live]) == [slot]
    assert stored(fake_keychain, slot) == newer


def test_sync_reads_each_copy_once(fake_keychain, fake_api):
    slots = {n: known(n, f"{n}@example.com", fake_api, fake_keychain) for n in ("a", "b")}
    core.save_rules(profiles.Rules(projects={"/a": "a", "/b": "b"}))
    live = [session(f"{n}{i}", f"/{n}", n) for n in slots for i in range(2)]
    # Setup warmed both slots and all four copies, so comparisons are free.
    with budget(fake_keychain, 0):
        assert core.sync_credentials(live) == []
    keychain.write_credentials(
        live[0].config_dir, fake_api.blob("a@example.com", gen=0, expires_in=1800))
    # Memo hits make the second comparison free; 1 locked read + 1 write.
    with budget(fake_keychain, 1, 1):
        assert core.sync_credentials(live) == [live[0].config_dir]
    newer = fake_api.blob("a@example.com", gen=2, expires_in=7200)
    keychain.write_credentials(slots["a"], newer)
    # Comparisons still hit the memo; 2 locked reads + 2 writes for the peers.
    with budget(fake_keychain, 2, 2):
        assert core.sync_credentials(live) == [s.config_dir for s in live[:2]]
    assert all(stored(fake_keychain, s.config_dir) == newer for s in live[:2])


def test_refresh_leads_claude_code(fake_keychain, fake_api, monkeypatch):
    monkeypatch.setattr(core.time, "time", lambda: 2000000000.0)
    slot = known("a", "a@example.com", fake_api, fake_keychain)
    live = session("term", "/repo", "a")
    monkeypatch.setattr(sessions, "discover_config_dirs", lambda home: [live.config_dir])
    old = fake_api.blob("a@example.com", gen=1, expires_in=20 * 60)
    keychain.write_credentials(slot, old)
    keychain.write_credentials(live.config_dir, old)
    fake_api.reset()
    # The peer pre-check hits setup's memo; only its locked read costs a call.
    with budget(fake_keychain, 5, 2):
        rotated = core.live_blob(slot)
    assert fake_api.refresh_calls == 1
    assert rotated["refreshToken"] == "a@example.com-refresh2"
    assert rotated["expiresAt"] > old["expiresAt"]
    assert stored(fake_keychain, slot) == rotated
    assert stored(fake_keychain, live.config_dir) == rotated

    fresh = fake_api.blob("a@example.com", gen=3, expires_in=2 * 3600)
    keychain.write_credentials(slot, fresh)
    fake_api.reset()
    with budget(fake_keychain, 1):
        assert core.live_blob(slot) == fresh
    assert fake_api.refresh_calls == 0
    assert stored(fake_keychain, slot) == fresh


def test_live_blob_refresh_and_failures(fake_keychain, fake_api, monkeypatch):
    slot = known("a", "a@example.com", fake_api, fake_keychain, fresh=False)
    old = stored(fake_keychain, slot)
    path = core.session_dir("term")
    Path(path).mkdir(parents=True)
    keychain.write_credentials(path, old)
    monkeypatch.setattr(sessions, "discover_config_dirs", lambda home: [path])
    fake_api.reset()
    # 2 slot reads + 2 default fallbacks + 1 locked peer read, with 2 writes.
    # The peer pre-check hits the memo populated by setup.
    with budget(fake_keychain, 5, 2):
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
    # 1 initial slot + 1 candidate slot + 2 default fallbacks; 1 heal.
    # The peer read hits the memo populated by setup.
    with budget(fake_keychain, 4, 1):
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
    # 1 adopt write, plus a profile token read when the identity file is missing.
    with budget(fake_keychain, int(not previous_email), 1):
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
    assert fake_api.profile_calls == (1 if previous_email else 2)
    identity = json.loads(Path(slot, ".claude.json").read_text())["oauthAccount"]
    if not previous_email:
        assert identity == {
            "accountUuid": f"uuid-{email}", "emailAddress": email,
            "organizationRateLimitTier": "default_claude_max_5x",
        }
        assert Path(slot, ".claude.json").stat().st_mode & 0o777 == 0o600


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
    # 1 slot + 1 session pre-read + 1 locked session read + 1 initial write.
    with budget(fake_keychain, 3, 1):
        path = core.resolve_dir("/repo", "term")
    assert path == core.session_dir("term")
    assert stored(fake_keychain, path) == stored(fake_keychain, slot)
    assert json.loads(Path(path, ".claude.json").read_text()) == {
        k: v for k, v in data.items() if k not in core._CONFIG_SKIP}
    # The second launch reads only the slot; the session read hits the memo.
    with budget(fake_keychain, 1):
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


def test_prune_session_rules(fake_keychain, fake_api, monkeypatch):
    sign_in("a", "a@example.com", fake_api)
    session("live", "/repo", "a")
    session("quiet", "/repo", "a")
    core.save_rules(profiles.Rules(sessions={"live": "a", "dead": "a", "quiet": "a"}))
    assert not Path(core.session_dir("dead")).exists()
    with budget(fake_keychain, 0):
        assert core.prune_session_rules(["live"]) == ["dead"]
    assert core.rules().sessions == {"live": "a", "quiet": "a"}

    def unexpected_save(r):
        pytest.fail("Pruning unchanged rules must not rewrite the file")

    monkeypatch.setattr(core, "save_rules", unexpected_save)
    with budget(fake_keychain, 0):
        assert core.prune_session_rules(["live"]) == []


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
    # 1 account load; slot and session fingerprint reads hit the memo.
    with budget(fake_keychain, 1):
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
    # The account load and session setup warmed all five fingerprint reads.
    with budget(fake_keychain, 0):
        owners = core.dirs_to_accounts([s.config_dir for s in live], accts)
    assert owners == {s.config_dir: name for s, name in zip(live, ("a", "a", "b"), strict=True)}


@pytest.mark.parametrize(("cached", "email", "writes"), [
    ("a@example.com", "a@example.com", 0),
    ("a@example.com", "A@EXAMPLE.COM", 0),
    ("b@example.com", "a@example.com", 1),
    # An owner not yet known is not a licence to overwrite: the copy is ahead,
    # and it was a fresh sign-in the one time this wrote over it.
    ("", "a@example.com", 0),
    ("a@example.com", "", 1),
])
def test_hand_out_requires_known_same_account(fake_keychain, fake_api, cached, email, writes):
    path = core.session_dir("term")
    Path(path).mkdir(parents=True)
    have = fake_api.blob("a@example.com", gen=2, expires_in=7200)
    want = fake_api.blob("a@example.com", gen=1)
    keychain.write_credentials(path, have)
    core._cache_write({path: {"email": cached}}, core.IDENTITY_CACHE)
    # The pre-read hits the memo. An email adds 1 fresh read under the lock;
    # write only when the copy ahead is known to be another account's.
    with budget(fake_keychain, int(bool(email)), writes):
        assert core.hand_out(path, want, email) is bool(writes)
    assert stored(fake_keychain, path) == (want if writes else have)


@pytest.mark.parametrize("via", ["hand_out", "prepare_session", "apply_now"])
def test_hand_out_syncs_identity(fake_keychain, fake_api, via):
    known("a", "a@example.com", fake_api, fake_keychain)
    target = known("b", "b@example.com", fake_api, fake_keychain)
    live = session("term", "/repo", "a")
    identity = {"accountUuid": "uuid-b", "emailAddress": "b@example.com"}
    Path(target, ".claude.json").write_text(json.dumps({"oauthAccount": identity}))
    config = Path(live.config_dir, ".claude.json")
    kept = {"projects": {"/repo": {"hasTrustDialogAccepted": True}}, "numStartups": 7}
    config.write_text(json.dumps({
        **kept, "oauthAccount": {"accountUuid": "uuid-a"},
        "cachedUsageUtilization": {"accountUuid": "uuid-a", "fiveHour": 80},
        "cachedExtraUsageDisabledReason": "disabled",
    }))
    want = stored(fake_keychain, target)
    if via == "hand_out":
        assert core.hand_out(live.config_dir, want, "b@example.com", account="b")
    elif via == "prepare_session":
        assert core.prepare_session("term", "b") == live.config_dir
    else:
        core.save_rules(profiles.Rules(default_account="b"))
        assert core.apply_now([live]) == ([live.label], {live.config_dir: "b"})
    assert stored(fake_keychain, live.config_dir) == want
    assert json.loads(config.read_text()) == {**kept, "oauthAccount": identity}
    assert config.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("newer", [False, True])
def test_hand_out_without_write_syncs_identity(fake_keychain, fake_api, newer):
    slot = known("a", "a@example.com", fake_api, fake_keychain)
    live = session("term", "/repo", "a")
    identity = {"accountUuid": "a", "emailAddress": "a@example.com"}
    Path(slot, ".claude.json").write_text(json.dumps({"oauthAccount": identity}))
    config = Path(live.config_dir, ".claude.json")
    config.write_text(json.dumps({
        "oauthAccount": {"accountUuid": "old"},
        "cachedUsageUtilization": {"used": 80},
        "cachedExtraUsageDisabledReason": "disabled",
    }))
    if newer:
        keychain.write_credentials(
            live.config_dir, fake_api.blob("a@example.com", gen=2, expires_in=7200))
    have = stored(fake_keychain, live.config_dir)
    with budget(fake_keychain, int(newer)):
        assert not core.hand_out(live.config_dir, stored(fake_keychain, slot),
                                 "a@example.com", account="a")
    assert stored(fake_keychain, live.config_dir) == have
    assert json.loads(config.read_text()) == {"oauthAccount": identity}


@pytest.mark.parametrize(("cached", "email"), [
    ("b@example.com", "a@example.com"),
    ("", "a@example.com"),
    ("", ""),
])
def test_hand_out_refused_leaves_identity(fake_keychain, fake_api, monkeypatch, cached, email):
    slot = known("a", "a@example.com", fake_api, fake_keychain)
    known("b", "b@example.com", fake_api, fake_keychain)
    live = session("term", "/repo", "b")
    Path(slot, ".claude.json").write_text('{"oauthAccount": {"accountUuid": "a"}}')
    core._cache_write({live.config_dir: {"email": cached}}, core.IDENTITY_CACHE)
    config = Path(live.config_dir, ".claude.json")
    before = '{"oauthAccount": {"accountUuid": "b"}, "cachedUsageUtilization": {"used": 80}}'
    config.write_text(before)
    os.utime(config, ns=(1_000_000_000, 1_000_000_000))
    monkeypatch.setattr(core, "adopt", lambda *args, **kwargs: False)
    have = stored(fake_keychain, live.config_dir)
    assert not core.hand_out(live.config_dir, stored(fake_keychain, slot), email, account="a")
    assert stored(fake_keychain, live.config_dir) == have
    assert config.read_text() == before
    assert config.stat().st_mtime_ns == 1_000_000_000


@pytest.mark.parametrize("source", [None, "directory", "{}", "{", "[]", '{"oauthAccount": null}'])
def test_hand_out_without_account_identity(fake_keychain, fake_api, source):
    slot = known("a", "a@example.com", fake_api, fake_keychain)
    fake_api.profiles["a@example.com"] = {"account": {"email": "a@example.com"}}
    live = session("term", "/repo", "")
    if source == "directory":
        Path(slot, ".claude.json").mkdir()
    elif source is not None:
        Path(slot, ".claude.json").write_text(source)
    # A signed-in default must never supply another account's identity.
    Path(core._config_json(core.DEFAULT_CONFIG)).write_text(
        '{"oauthAccount": {"accountUuid": "default"}}')
    config = Path(live.config_dir, ".claude.json")
    before = '{"oauthAccount": {"accountUuid": "old"}, "cachedUsageUtilization": {"used": 80}}'
    config.write_text(before)
    os.utime(config, ns=(1_000_000_000, 1_000_000_000))
    assert core.hand_out(live.config_dir, stored(fake_keychain, slot),
                         "a@example.com", account="a")
    assert config.read_text() == before
    assert config.stat().st_mtime_ns == 1_000_000_000


def test_hand_out_without_account_skips_identity(fake_keychain, fake_api, monkeypatch):
    slot = known("a", "a@example.com", fake_api, fake_keychain)
    live = session("term", "/repo", "")

    def unexpected_sync(*args):
        pytest.fail("An email alone cannot select the source identity file")

    monkeypatch.setattr(core, "_sync_identity", unexpected_sync)
    want = stored(fake_keychain, slot)
    assert core.hand_out(live.config_dir, want, "a@example.com")
    assert stored(fake_keychain, live.config_dir) == want


def test_sync_identity_busy_lock(fake_keychain, fake_api, monkeypatch):
    slot = known("a", "a@example.com", fake_api, fake_keychain)
    Path(slot, ".claude.json").write_text('{"oauthAccount": {"accountUuid": "a"}}')
    live = session("term", "/repo", "a")
    config = Path(live.config_dir, ".claude.json")
    before = '{"oauthAccount": {"accountUuid": "old"}, "cachedUsageUtilization": {"used": 80}}'
    config.write_text(before)
    os.utime(config, ns=(1_000_000_000, 1_000_000_000))

    @contextmanager
    def busy(path):
        assert path == live.config_dir
        raise locks.LockBusy("held by Claude Code")
        yield

    monkeypatch.setattr(locks, "config", busy)
    assert not core._sync_identity(live.config_dir, "a")
    assert config.read_text() == before
    assert config.stat().st_mtime_ns == 1_000_000_000


def test_sync_identity_unchanged(fake_keychain, fake_api):
    slot = known("a", "a@example.com", fake_api, fake_keychain)
    Path(slot, ".claude.json").write_text('{"oauthAccount": {"accountUuid": "a"}}')
    live = session("term", "/repo", "a")
    config = Path(live.config_dir, ".claude.json")
    before = '{"cachedUsageUtilization": {"used": 80}, "oauthAccount": {"accountUuid": "a"}}'
    config.write_text(before)
    os.utime(config, ns=(1_000_000_000, 1_000_000_000))
    assert not core._sync_identity(live.config_dir, "a")
    assert config.read_text() == before
    assert config.stat().st_mtime_ns == 1_000_000_000


@pytest.mark.parametrize("content", [None, "{", "[]"])
def test_sync_identity_requires_session_config(fake_keychain, fake_api, content):
    slot = known("a", "a@example.com", fake_api, fake_keychain)
    Path(slot, ".claude.json").write_text('{"oauthAccount": {"accountUuid": "a"}}')
    path = core.session_dir("term")
    Path(path).mkdir(parents=True)
    config = Path(path, ".claude.json")
    if content is not None:
        config.write_text(content)
    assert not core._sync_identity(path, "a")
    if content is None:
        assert not config.exists()
    else:
        assert config.read_text() == content


def test_owners_now_reuses_fingerprints(fake_keychain, fake_api, monkeypatch):
    slots = {n: known(n, f"{n}@example.com", fake_api, fake_keychain) for n in ("a", "b")}
    live = [session(f"term{i}", "/repo", name) for i, name in enumerate(("a", "a", "b"))]
    paths = [s.config_dir for s in live] + [core.DEFAULT_CONFIG]
    keychain.write_credentials(core.DEFAULT_CONFIG, stored(fake_keychain, slots["b"]))
    accts = core.all_accounts(with_usage=False)
    for path in paths:
        keychain.forget(path)
    dirs = paths + [slots["a"]]
    expected = dict(zip(dirs, ("a", "a", "b", "b", "a"), strict=True))
    renamed = []
    original = core.dirs_to_accounts

    def name_dirs(dirs, accts):
        renamed.append(set(dirs))
        return original(dirs, accts)

    monkeypatch.setattr(core, "dirs_to_accounts", name_dirs)
    # 4 non-account dirs; the loaded slots and repeated mapping reads hit the memo.
    with budget(fake_keychain, 4):
        owners, prints = core.owners_now(dirs, {}, {}, accts)
    assert owners == expected and prints[slots["a"]] is None
    assert renamed == [set(paths)]
    assert {service for _, service in fake_keychain.log} == {
        keychain.service_for(path) for path in paths}
    renamed.clear()
    with budget(fake_keychain, 0):
        assert core.owners_now(dirs, owners, prints, accts) == (owners, prints)
    assert renamed == []
    keychain.forget()
    # Unchanged fingerprints cost 4 reads after expiry, without any slot reads.
    with budget(fake_keychain, 4):
        assert core.owners_now(dirs, owners, prints, accts) == (owners, prints)
    assert renamed == []
    keychain.write_credentials(paths[0], stored(fake_keychain, slots["b"]))
    keychain.forget(paths[0])
    # 1 switched dir + 2 cold slots for naming; the other session reads hit the memo.
    with budget(fake_keychain, 3):
        owners, prints = core.owners_now(dirs, owners, prints, accts)
    expected[paths[0]] = "b"
    assert owners == expected and renamed == [{paths[0]}]
    renamed.clear()
    # Model another process switching a copy while our memo still holds its old login.
    fake_keychain.store[keychain.service_for(paths[1])] = fake_keychain.store[
        keychain.service_for(slots["b"])]
    with budget(fake_keychain, 0):
        assert core.owners_now(dirs, owners, prints, accts) == (owners, prints)
    with budget(fake_keychain, 4):
        owners, prints = core.owners_now(dirs, owners, prints, accts, fresh=True)
    expected[paths[1]] = "b"
    assert owners == expected and renamed == [{paths[1]}]


def test_owners_now_never_reads_account_dirs(fake_keychain, fake_api):
    slot = known("a", "a@example.com", fake_api, fake_keychain)
    accts = core.all_accounts(with_usage=False)
    missing = core.slot_dir("removed")
    keychain.forget()
    with budget(fake_keychain, 0):
        owners, prints = core.owners_now([slot, missing], {}, {}, iter(accts), fresh=True)
    assert owners == {slot: "a", missing: ""}
    assert prints == {slot: None, missing: None}


@pytest.mark.parametrize("current", [False, True])
def test_hand_out_rechecks_after_lock(fake_keychain, fake_api, monkeypatch, current):
    known("a", "a@example.com", fake_api, fake_keychain)
    live = session("term", "/repo", "a")
    want = fake_api.blob("a@example.com", gen=2, expires_in=7200)
    newer = want if current else fake_api.blob("a@example.com", gen=3, expires_in=10800)
    original = locks.credentials

    @contextmanager
    def rotate_before_lock(path, **kwargs):
        # Another process rotates after the memoized pre-read, before we own the lock.
        fake_keychain.store[keychain.service_for(path)] = json.dumps({"claudeAiOauth": newer})
        with original(path, **kwargs):
            yield

    monkeypatch.setattr(locks, "credentials", rotate_before_lock)
    # The pre-read hits the memo; the fresh locked read prevents a stale write.
    with budget(fake_keychain, 1):
        assert not core.hand_out(live.config_dir, want, "a@example.com")
    assert stored(fake_keychain, live.config_dir) == newer


@pytest.mark.parametrize("promotion", [False, True])
def test_sync_rechecks_rotations_under_lock(fake_keychain, fake_api, monkeypatch, promotion):
    slot = known("a", "a@example.com", fake_api, fake_keychain)
    core.save_rules(profiles.Rules(default_account="a"))
    live = session("term", "/repo", "a")
    candidate = fake_api.blob("a@example.com", gen=2, expires_in=7200)
    newer = fake_api.blob("a@example.com", gen=3, expires_in=10800)
    keychain.write_credentials(live.config_dir if promotion else slot, candidate)
    destination = slot if promotion else live.config_dir
    original = locks.credentials

    @contextmanager
    def rotate_before_lock(path, **kwargs):
        if path == destination:
            fake_keychain.store[keychain.service_for(path)] = json.dumps({"claudeAiOauth": newer})
        with original(path, **kwargs):
            yield

    monkeypatch.setattr(locks, "credentials", rotate_before_lock)
    # Promotion checks the slot, then the newer peer; hand-out checks only the peer.
    with budget(fake_keychain, 2 if promotion else 1):
        assert core.sync_credentials([live]) == []
    assert stored(fake_keychain, destination) == newer
    assert stored(fake_keychain, live.config_dir if promotion else slot) == candidate


def test_owners_now_retries_owner_named_before_accounts_loaded(fake_keychain, fake_api):
    known_slot = known("a", "a@example.com", fake_api, fake_keychain)
    core.save_rules(profiles.Rules(default_account="a"))
    live = session("term", "/repo", "a")
    accts = core.all_accounts(with_usage=False)
    # The first poll ran with no accounts loaded and named the dir by email.
    early, prints = core.owners_now([live.config_dir], {}, {}, [])
    assert early == {live.config_dir: "a@example.com"}
    # Once accounts exist, that answer is re-derived rather than trusted.
    owners, _ = core.owners_now([live.config_dir], early, prints, accts)
    assert owners == {live.config_dir: "a"}
    assert core.fingerprint(stored(fake_keychain, known_slot))


def test_reset_windows_spends_the_soonest_credit(fake_keychain, fake_api, monkeypatch):
    import urllib.error
    from io import BytesIO
    known("a", "a@example.com", fake_api, fake_keychain)
    codex.ensure_account_dir("cx")
    monkeypatch.setattr(codex, "live_auth", lambda home: {"tokens": {"access_token": "t"}})
    details = {"credits": [
        {"id": "late", "status": "available", "expires_at": "2026-10-05T00:00:00Z"},
        {"id": "soon", "status": "available", "expires_at": "2026-09-21T00:00:00Z"},
    ]}
    monkeypatch.setattr(codex, "fetch_reset_credits", lambda auth: details)
    spent = []
    monkeypatch.setattr(codex, "consume_reset_credit",
                        lambda auth, cid=None: spent.append(cid) or {})
    assert core.reset_windows("a") == (
        False, "a is a Claude account, and only Codex accounts have reset credits")
    ok, msg = core.reset_windows("cx")
    assert ok and spent == ["soon"] and msg == "windows reset, 1 reset credit left"
    details["credits"] = details["credits"][:1]
    ok, msg = core.reset_windows("cx")
    assert ok and msg == "windows reset, that was the last reset credit"
    details["credits"] = []
    assert core.reset_windows("cx") == (False, "no reset credit to spend")
    details["credits"] = [{"id": "x", "status": "available"}]

    def refused(auth, cid=None):
        raise urllib.error.HTTPError("u", 402, "nope", {}, BytesIO(b'{"detail": "already used"}'))

    monkeypatch.setattr(codex, "consume_reset_credit", refused)
    assert core.reset_windows("cx") == (False, "the reset was refused (HTTP 402: already used)")
    assert core.reset_windows("nobody")[0] is False


def codex_account(name: str) -> Path:
    """A Codex account with a login of its own, as sign_in makes a Claude one."""
    slot = Path(codex.ensure_account_dir(name))
    (slot / "auth.json").write_text(json.dumps({"tokens": {"refresh_token": name}}))
    return slot


def test_codex_project_rule_leaves_the_claude_one_alone(fake_keychain, fake_api):
    sign_in("a", "a@example.com", fake_api)
    codex_account("cx")
    assert core.assign("project", "/repo", "a", live=[])[0]
    # Codex routing writes files and links; it never reads a credential.
    with budget(fake_keychain, 0):
        ok, message = core.assign("project", "/repo", "cx", live=[])
    assert ok and message == "“repo” now uses cx"
    r = core.rules()
    assert r.projects == {"/repo": "a"} and r.codex_projects == {"/repo": "cx"}
    assert core.resolve("/repo", provider="codex") == ("cx", "project")
    assert core.resolve("/repo") == ("a", "project")
    routes = Path(profiles.ROUTES).read_text()
    assert f"path:/repo={core.slot_dir('a')}" in routes
    assert f"codex-path:/repo={codex.slot_dir('cx')}" in routes


def test_codex_session_pin_makes_its_home(fake_keychain):
    codex_account("cx")
    with budget(fake_keychain, 0):
        assert core.assign("session", "term", "cx", live=[])[0]
    r = core.rules()
    assert r.codex_sessions == {"term": "cx"} and r.sessions == {}
    home = Path(codex.session_home("term"))
    assert home.joinpath("auth.json").resolve() == Path(codex.slot_dir("cx"), "auth.json")
    # The pin has a home from the start, so pruning cannot mistake it for dead.
    assert core.prune_session_rules([]) == []


def test_codex_profile_account_sits_beside_the_claude_one(fake_keychain, fake_api):
    sign_in("a", "a@example.com", fake_api)
    codex_account("cx")
    assert core.add_profile("work", live=[])[0]
    assert core.assign("profile", "work", "cx", live=[])[0]
    assert core.assign("profile", "work", "a", live=[])[0]
    prof = core.rules().profile("work")
    assert (prof.account, prof.codex_account) == ("a", "cx")


@pytest.mark.parametrize(("names", "expected"), [
    (["codex", "zed"], "codex"), (["alpha", "zed"], "alpha")])
def test_bootstrap_starts_the_codex_default(fake_keychain, fake_api, names, expected):
    sign_in("a", "a@example.com", fake_api)
    for name in names:
        codex_account(name)
    assert core.bootstrap().codex_default_account == expected
    assert core.rules().codex_default_account == expected


def test_codex_resolve_dir_prepares_a_home(fake_keychain):
    codex_account("cx")
    core.save_rules(profiles.Rules(codex_default_account="cx"))
    with budget(fake_keychain, 0):
        assert core.resolve_dir("/repo", provider="codex") == codex.slot_dir("cx")
        home = core.resolve_dir("/repo", "term", "codex")
    assert home == codex.session_home("term")
    assert Path(home, "auth.json").resolve() == Path(codex.slot_dir("cx"), "auth.json")
    # With no codex rule at all, launches land on the Codex CLI's own home.
    core.save_rules(profiles.Rules())
    assert core.resolve_dir("/repo", "term", "codex") == codex.DEFAULT_HOME


def test_prune_drops_a_codex_pin_whose_home_is_gone(fake_keychain):
    codex_account("cx")
    core.save_rules(profiles.Rules(codex_sessions={"live": "cx", "dead": "cx"}))
    codex.prepare_session("live", "cx")
    with budget(fake_keychain, 0):
        assert core.prune_session_rules([]) == ["dead"]
    assert core.rules().codex_sessions == {"live": "cx"}


def test_rules_using_reads_the_side_the_account_is_on(fake_keychain):
    codex_account("cx")
    core.save_rules(profiles.Rules(
        default_account="cx", profiles=[profiles.Profile("work", codex_account="cx")],
        codex_default_account="cx", codex_projects={"/repo": "cx"},
        codex_sessions={"term": "cx"}))
    # The claude default naming "cx" is not a codex rule and is not reported.
    assert core.rules_using("cx") == [
        "default", "profile work", "project repo", "1 session rule"]


def test_codex_cli_round_trip(fake_keychain, fake_api, monkeypatch, capsys, tmp_path):
    known("a", "a@example.com", fake_api, fake_keychain)
    codex_account("cx")
    core.save_rules(profiles.Rules(default_account="a"))
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("TERM_SESSION_ID", "term")
    monkeypatch.setattr(sessions, "live", lambda *args, **kwargs: [])
    assert cli.main(["use", "cx"]) == 0
    output = capsys.readouterr().out
    assert "now uses cx" in output
    assert "restart codex in that terminal" in output
    assert core.rules().codex_projects == {str(repo): "cx"}
    assert core.rules().projects == {}
    # Where prints the claude answer, then the same three lines for codex.
    assert cli.main(["where"]) == 0
    output = capsys.readouterr().out
    assert output.count("account :") == 2 and "codex" in output
    assert codex.slot_dir("cx").replace(core.HOME, "~") in output
    assert cli.main(["resolve", "--codex"]) == 0
    assert capsys.readouterr().out.strip() == codex.session_home("term")
    assert cli.main(["use", "cx", "--session"]) == 0
    assert core.rules().codex_sessions == {"term": "cx"}
    capsys.readouterr()
    assert cli.main(["unpin", "--codex"]) == 0
    assert core.rules().codex_sessions == {}
    assert "follows its profile again" in capsys.readouterr().out


def test_sessions_lists_a_codex_row_beside_the_claude_ones(fake_keychain, monkeypatch,
                                                           capsys):
    codex_account("cx")
    core.save_rules(profiles.Rules(codex_projects={"/repo": "cx"}))
    home = codex.prepare_session("term", "cx")
    live = sessions.Session(pid=4242, config_dir=home, provider="codex",
                            env_config_dir=home, term_id="term", cwd="/repo",
                            kind="bg", status="busy", title="run the suite")
    monkeypatch.setattr(sessions, "live", lambda *args, **kwargs: [])
    monkeypatch.setattr(core.codex_sessions, "live", lambda *args, **kwargs: [live])
    # A Codex row names its account from a file and its rule from the codex
    # side, so it adds no keychain call: the two are bootstrap looking at the
    # default config dir, which happens with no session running at all.
    with budget(fake_keychain, 2):
        assert cli.main(["sessions"]) == 0
    output = capsys.readouterr().out
    assert "repo" in output and "busy" in output and "cx" in output
    assert "project" in output


def test_profiles_table_marks_the_codex_lines(fake_keychain, capsys):
    codex_account("cx")
    core.save_rules(profiles.Rules(
        default_account="a", profiles=[profiles.Profile("work", "a", ["/repo"], "cx")],
        projects={"/repo": "a"}, sessions={"term": "a"},
        codex_default_account="cx", codex_projects={"/other": "cx"},
        codex_sessions={"term": "cx"}))
    assert cli.main(["profiles"]) == 0
    lines = [re.sub(r"\033\[\d+m", "", line) for line in capsys.readouterr().out.splitlines()]
    assert lines.count("everything else        a") == 1
    assert "everything else        cx codex" in lines
    assert "                       cx codex" in lines
    assert "project /other                       cx codex" in lines
    assert "session term                         cx codex" in lines


def test_every_command_renders_its_help(capsys):
    # argparse only expands help strings when it prints them on older Pythons,
    # and at parser build time on 3.14, so a stray "%" broke every command in
    # the installed tool while the suite stayed green.
    with pytest.raises(SystemExit) as top:
        cli.main(["--help"])
    assert top.value.code == 0
    commands = re.findall(r"\{([^}]+)\}", capsys.readouterr().out)[0].split(",")
    assert "reset" in commands
    for command in commands:
        with pytest.raises(SystemExit) as done:
            cli.main([command, "--help"])
        assert done.value.code == 0, command
        assert command in capsys.readouterr().out
