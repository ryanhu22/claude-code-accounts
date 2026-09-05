import json
from pathlib import Path

from claude_code_manager import transcripts


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
