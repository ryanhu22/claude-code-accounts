"""Exercise the real-mode guards using only fake credentials and temporary files."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_code_manager import codex, core, keychain, sessions, transcripts
from fakes import sign_in


@pytest.fixture
def bench():
    path = Path(__file__).resolve().parents[1] / "scripts" / "bench.py"
    spec = importlib.util.spec_from_file_location("ccm_bench", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_bench_cannot_refresh_heal_or_save(
        bench, fake_keychain, fake_api, no_git, monkeypatch, capsys):
    slot = sign_in("a", "a@example.com", fake_api, fresh=False)
    Path(slot, ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": "a@example.com"}}))
    sign_in("rejected", "rejected@example.com", fake_api)
    cx = Path(codex.ensure_account_dir("cx"))
    (cx / "auth.json").write_text('{"tokens": {}}')
    transcript = Path(core.HOME, "transcript.jsonl")
    transcript.write_text(json.dumps({"type": "assistant", "message": {
        "usage": {"input_tokens": 12}}}) + "\n")
    live = sessions.Session(pid=10001, config_dir=slot, env_config_dir=slot, cwd="/repo")
    scans = []

    def scan(dirs, with_env=True, with_git=False, with_transcript=False):
        scans.append((with_env, with_git, with_transcript))
        if with_transcript:
            assert transcripts.lifetime(str(transcript)).input == 12
        return [live]

    monkeypatch.setattr(sessions, "live", scan)
    monkeypatch.setattr(core, "refresh", bench.forbidden)
    monkeypatch.setattr(codex, "refresh", bench.forbidden)
    monkeypatch.setattr(core, "sync_credentials", bench.forbidden)
    monkeypatch.setattr(core, "apply_now", bench.forbidden)
    monkeypatch.setattr(core, "gc_session_dirs", bench.forbidden)
    before_files = {str(p): p.read_bytes() for p in Path(core.HOME).rglob("*") if p.is_file()}
    before_store = dict(fake_keychain.store)
    fake_keychain.reset()
    rows = bench.run_real(SimpleNamespace(runs=2, security_ms=15))
    assert fake_keychain.reads > 0
    assert fake_keychain.writes == fake_keychain.deletes == 0
    assert fake_keychain.store == before_store
    after_files = {str(p): p.read_bytes() for p in Path(core.HOME).rglob("*") if p.is_file()}
    assert after_files == before_files
    assert {(False, False, False), (True, False, False), (True, True, False),
            (True, True, True)} <= set(scans)
    assert {name for name, _, _ in rows} >= {"poll fingerprints", "dirs_to_accounts"}
    assert "est. real ms" in capsys.readouterr().out
    assert fake_api.refresh_calls == 0


def test_security_counter_refuses_mutations(bench, fake_keychain, monkeypatch):
    calls = []

    def fake_run(args, stdin=None):
        calls.append(args)
        raw = fake_keychain.read_raw("bench-service")
        return subprocess.CompletedProcess(args, 0 if raw else 44, raw or "", "")

    monkeypatch.setattr(keychain, "_run", fake_run)
    counter = bench.SecurityCalls()
    assert counter.run(["security", "find-generic-password"]).returncode == 44
    assert counter.reads == 1 and len(counter.durations) == 1
    for args, stdin in ((["security", "delete-generic-password"], None),
                        (["security", "-i"], "add-generic-password")):
        with pytest.raises(AssertionError):
            counter.run(args, stdin)
    assert len(calls) == 1


@pytest.mark.parametrize(("label", "reads"), [
    ("poll owners cold", 5), ("poll owners warm", 0), ("full refresh", 5),
])
def test_fake_poll_and_refresh_budgets(
        bench, fake_keychain, fake_api, no_git, monkeypatch, label, reads):
    from claude_code_manager import profiles
    from fakes import session
    from test_moves import budget

    names = ["a", "b"]
    blobs = {}
    for name in names:
        slot = sign_in(name, f"{name}@example.com", fake_api)
        blobs[name] = keychain.read_credentials(slot)
    core.save_rules(profiles.Rules(default_account="a"))
    live = [session(f"term{i}", "/repo", "a") for i in range(3)]
    accts = core.all_accounts(with_usage=True)
    monkeypatch.setattr(bench, "seed", lambda *args, **kwargs: (
        fake_keychain, fake_api, names, blobs, live, accts))
    move, _, _ = bench.fake_move(label, core.HOME, 2, 3)
    # A cold pass reads 2 slots + 3 copies once. A warm poll reads nothing.
    with budget(fake_keychain, reads):
        result = move()
    owners = result[0] if label.startswith("poll") else result
    assert owners == {s.config_dir: "a" for s in live}
