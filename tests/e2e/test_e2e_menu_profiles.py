"""The profiles section: profiles, their projects, and the default rule."""
import os

from claude_code_accounts import core
from e2e.menu_harness import checked, click, index_of, text


def seed(sandbox, menu, with_session=True):
    sandbox.seed_claude("main", "main@example.com")
    sandbox.seed_claude("spare", "spare@example.com")
    sandbox.seed_codex("gpt", "gpt@example.com", plan="plus")
    pid = None
    if with_session:
        pid = menu.start_session("main", "T1", os.path.join(sandbox.home, "repos", "acme"))
    menu.refresh()
    return pid


def profile_row(menu, name):
    return menu.find(f" {name} ", menu.section("PROFILES"))


def picks(menu, item):
    """The account rows of every picker in a menu, in order."""
    return [r for r in menu.items(item) if text(r).startswith(" ￼")]


def test_new_profile_from_the_actions(sandbox, fake_server, menu, dialogs):
    seed(sandbox, menu)
    dialogs.answers.append((1, "client"))
    click(menu.find("New profile…"))
    assert dialogs.windows[-1]["title"] == "New profile"
    assert core.rules().profile("client").account == "main"
    assert menu.flash() == "profile “client” created"
    row = profile_row(menu, "client")
    drawn = text(row)
    assert "￼main " in drawn and " client " in drawn and "0 projects" in drawn
    said = menu.texts(row)
    assert said[0] == "  Use for every project in “client”"
    # Both providers are offered, the Codex one under its own label.
    accounts = picks(menu, row)
    assert [text(r).split(" ")[1] for r in accounts] == ["￼main", "￼spare", "￼gpt"]
    assert checked(accounts[0]) and not checked(accounts[1])
    assert "    Codex" in said
    assert "  No sessions are running in these projects" in said
    assert "    No projects yet" in said
    assert "  Add a project" in said and "    ~/repos/acme" in said
    assert not any("Remove a project" in line for line in said)
    assert said[-2:] == ["  Rename…", "  Remove profile…"]


def test_cancelling_new_profile_makes_nothing(sandbox, fake_server, menu, dialogs):
    seed(sandbox, menu, with_session=False)
    dialogs.answers.append((0, "client"))
    click(menu.find("New profile…"))
    dialogs.answers.append((1, "   "))
    click(menu.find("New profile…"))
    assert core.rules().profiles == []
    assert menu.flash() == ""


def test_a_project_joins_and_leaves_a_profile(sandbox, fake_server, menu, dialogs):
    pid = seed(sandbox, menu)
    dialogs.answers.append((1, "client"))
    click(menu.find("New profile…"))
    # From the session row: the profiles it can join, then a new one.
    join = menu.find("Add “acme” to profile", menu.session_row(pid))
    assert menu.texts(join) == [" ￼ client", " ￼ New profile…"]
    click(menu.find("client", join))
    assert core.rules().profile("client").repos == ["~/repos/acme"]
    menu.settle()
    assert menu.flash() == "acme joined “client”"
    # The session row now names its profile, and the profile row counts it.
    said = menu.texts(menu.session_row(pid))
    assert "  In profile “client” (1 project), which uses main" in said
    assert "Add “acme” to profile" not in "".join(said)
    row = profile_row(menu, "client")
    assert "1 project " in text(row) and "○ 1" in text(row)
    said = menu.texts(row)
    assert "  Running now · 1" in said
    assert "  Projects" in said and "    ~/repos/acme" in said
    assert not any("Add a project" in line for line in said)
    # And can be dropped again.
    drop = menu.find("Remove a project", row)
    assert menu.texts(drop) == [" ~/repos/acme"]
    click(menu.items(drop)[0])
    assert core.rules().profile("client").repos == []
    menu.settle()
    assert menu.flash() == "acme left “client”"
    assert "0 projects" in text(profile_row(menu, "client"))


def test_adding_a_project_from_the_profile_row(sandbox, fake_server, menu, dialogs):
    seed(sandbox, menu)
    dialogs.answers.append((1, "client"))
    click(menu.find("New profile…"))
    click(menu.find("~/repos/acme", profile_row(menu, "client")))
    assert core.rules().profile("client").repos == ["~/repos/acme"]


def test_a_profile_moves_its_sessions_to_another_account(sandbox, fake_server, menu, dialogs):
    pid = seed(sandbox, menu)
    dialogs.answers.append((1, "client"))
    click(menu.find("New profile…"))
    click(menu.find("~/repos/acme", profile_row(menu, "client")))
    menu.settle()
    click(picks(menu, profile_row(menu, "client"))[1])
    assert core.rules().profile("client").account == "spare"
    menu.settle()
    assert menu.flash() == ("profile “client” (1 repo) now uses spare. "
                            "1 running session switches within about 30 seconds")
    assert sandbox.blob(core.session_dir("T1"))["accessToken"].startswith(
        "at-spare@example.com")
    assert menu.texts(menu.session_row(pid))[0] == "  Spending spare, by profile “client”"
    assert "￼spare " in text(profile_row(menu, "client"))
    # A Codex account for the same profile sits beside it, and leaves the
    # Claude side alone.
    click(picks(menu, profile_row(menu, "client"))[2])
    prof = core.rules().profile("client")
    assert (prof.account, prof.codex_account) == ("spare", "gpt")
    menu.settle()
    drawn = text(profile_row(menu, "client"))
    assert drawn.index("￼spare ") < drawn.index("￼gpt ")


def test_rename_and_remove_a_profile(sandbox, fake_server, menu, dialogs):
    seed(sandbox, menu, with_session=False)
    dialogs.answers += [(1, "client"), (1, "other")]
    click(menu.find("New profile…"))
    click(menu.find("New profile…"))
    dialogs.answers.append((1, "other"))
    click(menu.find("Rename…", profile_row(menu, "client")))
    assert dialogs.windows[-1]["title"] == "Rename profile"
    assert dialogs.alerts[-1]["message"] == "pick a name that is not already taken"
    assert [p.name for p in core.rules().profiles] == ["client", "other"]
    dialogs.answers.append((1, "acme-client"))
    click(menu.find("Rename…", profile_row(menu, "client")))
    assert [p.name for p in core.rules().profiles] == ["acme-client", "other"]
    assert menu.flash() == "“client” is now “acme-client”"
    assert profile_row(menu, "acme-client")
    dialogs.answers.append(0)
    click(menu.find("Remove profile…", profile_row(menu, "other")))
    assert dialogs.alerts[-1]["title"] == "Remove “other”?"
    assert [p.name for p in core.rules().profiles] == ["acme-client", "other"]
    dialogs.answers.append(1)
    click(menu.find("Remove profile…", profile_row(menu, "other")))
    assert [p.name for p in core.rules().profiles] == ["acme-client"]
    menu.settle()
    assert menu.flash() == "profile “other” removed; its repos follow the default again"
    assert len(menu.section("PROFILES")) == 2        # one profile and everything else


def test_the_default_rule_names_one_account_per_provider(sandbox, fake_server, menu):
    pid = seed(sandbox, menu)
    row = menu.find("everything else", menu.section("PROFILES"))
    drawn = text(row)
    assert drawn.index("￼main ") < drawn.index("everything else") < drawn.index("￼gpt ")
    assert "○ 1" in drawn
    said = menu.texts(row)
    assert said[0] == "  Running now · 1"
    assert "  Use for every project with no rule" in said
    accounts = picks(menu, row)
    assert [checked(r) for r in accounts] == [True, False, True]
    click(accounts[1])
    assert core.rules().default_account == "spare"
    assert core.rules().codex_default_account == "gpt"
    menu.settle()
    assert menu.flash() == ("everything with no rule now uses spare. "
                            "1 running session switches within about 30 seconds")
    assert menu.texts(menu.session_row(pid))[0] == "  Spending spare, by the default"
    assert menu.title().startswith("⇄ spare")
    row = menu.find("everything else", menu.section("PROFILES"))
    assert [checked(r) for r in picks(menu, row)] == [False, True, True]


def test_new_profile_from_a_session_row_takes_the_project_along(
        sandbox, fake_server, menu, dialogs):
    pid = seed(sandbox, menu)
    join = menu.find("Add “acme” to profile", menu.session_row(pid))
    rows = menu.items(join)
    dialogs.answers.append((1, "fresh"))
    click(rows[index_of(rows, menu.find("New profile…", join))])
    prof = core.rules().profile("fresh")
    assert prof.account == "main" and prof.repos == ["~/repos/acme"]
    menu.settle()
    assert menu.flash() == "profile “fresh” created, acme joined “fresh”"
