"""Exercise the demo with the same process and network blocks as the product tests."""

import importlib.util
import io
import os
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_code_accounts import cli, codex, core, focus, keychain, sessions, transcripts


@pytest.fixture
def demo(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts" / "demo.py"
    spec = importlib.util.spec_from_file_location("ccm_demo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # seed uses builtin setattr in a standalone process; tests must restore those fakes.
    for target, names in (
        (core, ("project_root", "_ROOTS", "_CODEX_ADOPTED", "_get", "_post")),
        (keychain, ("read_raw", "write_raw", "delete")),
        (codex, ("fetch_usage", "running_homes")),
        (sessions, ("alive", "_environ", "branch_of")),
        (transcripts, ("find", "digest", "lifetime", "flush")),
        (focus, ("frontmost_bundle_id", "selected_tty", "reveal_tab")),
    ):
        for name in names:
            monkeypatch.setattr(target, name, getattr(target, name))
    with patch.dict(os.environ):
        yield module


@pytest.fixture
def world(demo, tmp_path):
    return demo.seed(tmp_path / "home")


def test_accounts(world):
    accounts = core.all_accounts(with_usage=True)
    assert {a.name for a in accounts} == {"work", "personal", "client", "codex"}
    assert all(a.signed_in and a.limits for a in accounts)
    cx = next(a for a in accounts if a.name == "codex")
    assert cx.is_codex and "Pro" in cx.plan
    assert cx.extras["credits_balance"] == "12.40"
    assert [(lim.percent, lim.resets_at) for lim in cx.limits[2:]] == [(0, None), (0, None)]
    personal = next(a for a in accounts if a.name == "personal")
    assert personal.limit("session").resets_at is None
    assert core.pref("bar_account") == "work"
    assert not core.pref(core.AUTO_START_PREF, False)


def test_sessions(world):
    live = sessions.live(core.credential_dirs(), with_git=True, with_transcript=True)
    assert [(s.term_id, s.branch, s.title, s.context_pct) for s in live] == [
        ("demo-term-api", "main", "Add rate limiting to the webhook receiver", 42),
        ("demo-term-web", "checkout-flow", "Rebuild the checkout form", 71),
        ("demo-term-side", "main", "Migrate the cron jobs to launchd", 18),
        ("demo-term-notes", "main", "", 6),
    ]
    assert [s.spent for s in live] == [s.spent for s in world.sessions]
    assert [s.tty for s in live] == ["ttys001", "ttys002", "ttys003", "ttys004"]
    assert live[1].is_worktree
    picked = focus.pick(live, focus.frontmost_bundle_id(), focus.selected_tty("")[0])
    assert picked.session == live[0]


def test_rules(world):
    assert core.resolve(str(world.home / "src/acme-web/.claude/worktrees/checkout-flow")) == (
        "work", "profile:work")
    assert core.resolve(str(world.home / "src/notes"), world.pinned_term) == ("personal", "session")


def test_cli(world):
    output = io.StringIO()
    with redirect_stdout(output):
        assert cli.main(["list"]) == 0
    assert all(name in output.getvalue() for name in ("work", "personal", "client", "codex"))
    output = io.StringIO()
    with redirect_stdout(output):
        assert cli.main(["sessions"]) == 0
    rows, legend = output.getvalue().split("\n\n")
    assert len(rows.splitlines()) == 4
    assert all(s.name in rows for s in world.sessions)
    assert "pins the terminal" in legend


def test_ansi_to_html(demo):
    converted = demo.ansi_to_html("\x1b[1mwork\x1b[0m  \x1b[2mresets 3d\x1b[0m <b>")
    assert '<span style="font-weight:600">work</span>' in converted
    assert '<span style="color:#8e8e93">resets 3d</span>' in converted
    assert "&lt;b&gt;" in converted
    assert demo.ansi_to_html("\x1b[999m<&\x1b[0m") == "&lt;&amp;"


def test_rejects_populated_home(demo, tmp_path):
    home = tmp_path / "home"
    marker = home / "keep.txt"
    marker.write_text("keep")
    with pytest.raises(ValueError, match="empty throwaway"):
        demo.seed(home)
    assert marker.read_text() == "keep"


@pytest.mark.parametrize("chrome", [None, "/demo/chrome"])
def test_terminal_shots(demo, world, tmp_path, monkeypatch, capsys, chrome):
    out = tmp_path / "images"
    out.mkdir()
    monkeypatch.chdir(world.home / "src/acme-api")
    documents = []

    def fake_render(args, target):
        assert {"--use-mock-keychain", "--disable-background-networking"} <= set(args)
        assert f"--screenshot={target}" in args
        from urllib.parse import unquote, urlsplit

        page = Path(unquote(urlsplit(args[-1]).path))
        assert page.parent != out
        document = page.read_text()
        documents.append(document)
        lines = document.split("<pre>", 1)[1].split("</pre>", 1)[0].count("\n") + 1
        assert f"--window-size=900,{lines * 20 + 44}" in args
        Path(target).write_bytes(b"fake PNG")

    monkeypatch.setattr(demo, "render", fake_render)
    demo.cli_shots(out, chrome)
    extension = "png" if chrome else "html"
    assert {p.name for p in out.iterdir()} == {
        f"cli-{command}.{extension}" for command in ("list", "sessions", "where")}
    if not chrome:
        documents = [p.read_text() for p in sorted(out.iterdir())]
        assert "Chrome was not found" in capsys.readouterr().out
    assert all("$ ccm " in document and "#1c1c1e" in document for document in documents)
