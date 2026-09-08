import json
from pathlib import Path

from claude_code_accounts import transcripts


def record(input_tokens, cache_write, cache_read, output):
    return json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": input_tokens, "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cache_read, "output_tokens": output,
    }}}) + "\n"


def test_lifetime_resumes_after_restart(monkeypatch, tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(record(10, 20, 100, 5) + record(30, 40, 200, 15))
    expected = transcripts.Totals(input=40, cache_write=60, cache_read=300, output=20, turns=2)
    assert transcripts.lifetime(str(path)) == expected
    assert expected.total == 420
    assert expected.fresh == 120
    cache = json.loads(Path(transcripts.TOKEN_CACHE).read_text())
    assert cache[str(path)]["offset"] == path.stat().st_size
    monkeypatch.setattr(transcripts, "_tokens", None)
    assert transcripts.lifetime(str(path)) == expected
    with path.open("a") as f:
        f.write(record(2, 3, 4, 5))
    assert transcripts.lifetime(str(path)) == transcripts.Totals(42, 63, 304, 25, 3)
    cache = json.loads(Path(transcripts.TOKEN_CACHE).read_text())
    assert cache[str(path)]["offset"] == path.stat().st_size


def test_lifetime_waits_for_complete_line(tmp_path):
    path = tmp_path / "session.jsonl"
    first, second = record(10, 20, 100, 5), record(30, 40, 200, 15)
    path.write_text(first + second[:-1])
    assert transcripts.lifetime(str(path)) == transcripts.Totals(10, 20, 100, 5, 1)
    cache = json.loads(Path(transcripts.TOKEN_CACHE).read_text())
    assert cache[str(path)]["offset"] == len(first.encode())
    with path.open("a") as f:
        f.write("\n")
    assert transcripts.lifetime(str(path)) == transcripts.Totals(40, 60, 300, 20, 2)


def test_live_saves_all_grown_transcripts_once(monkeypatch, tmp_path):
    from claude_code_accounts import sessions

    config = tmp_path / "config"
    registry = config / "sessions"
    registry.mkdir(parents=True)
    paths = {}
    for pid in (1, 2, 3):
        (registry / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": str(pid)}))
        if pid < 3:
            path = tmp_path / f"{pid}.jsonl"
            path.write_text(record(10, 20, 100, 5))
            paths[str(pid)] = str(path)
    monkeypatch.setattr(sessions, "alive", lambda pid: True)
    monkeypatch.setattr(transcripts, "find", lambda session_id, roots: paths.get(session_id, ""))
    saves = []
    monkeypatch.setattr(transcripts, "_tokens_save", lambda: saves.append(True))
    for turns in (1, 2):
        saves.clear()
        live = sessions.live([str(config)], with_env=False, with_transcript=True)
        assert len(saves) == 1
        assert [s.spent.turns for s in sorted(live, key=lambda s: s.pid)] == [turns, turns, 0]
        assert not transcripts._dirty
        saves.clear()
        sessions.live([str(config)], with_env=False, with_transcript=True)
        assert saves == []
        for path in paths.values():
            with Path(path).open("a") as stream:
                stream.write(record(10, 20, 100, 5))


def test_lifetime_alone_saves_immediately(monkeypatch, tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(record(10, 20, 100, 5))
    saves = []
    monkeypatch.setattr(transcripts, "_tokens_save", lambda: saves.append(True))
    assert transcripts.lifetime(str(path)).turns == 1
    assert saves == [True]
    assert not transcripts._dirty


def test_live_flushes_pending_tokens_with_empty_transcript(monkeypatch, tmp_path):
    from claude_code_accounts import sessions

    path = tmp_path / "session.jsonl"
    path.write_text(record(10, 20, 100, 5))
    saves = []
    monkeypatch.setattr(transcripts, "_tokens_save", lambda: saves.append(True))
    transcripts.lifetime(str(path), save=False)
    assert not saves and transcripts._dirty
    sessions.live([], with_env=False, with_transcript=True)
    assert saves == [True]
    assert not transcripts._dirty
