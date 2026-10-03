"""The subscriptions section: account rows, their submenus, and the menu bar title.

Every test drives the real ManagerApp against the sandbox: the accounts are
loaded through core from the fake server, the rows are real AppKit menu
items, and a click runs the row's own callback.
"""
import json
import os
import threading
import time
from types import SimpleNamespace

from claude_code_accounts import core, keychain, menubar
from e2e.menu_harness import checked, click, text


def test_account_rows_show_identity_plan_and_windows(sandbox, fake_server, menu):
    sandbox.seed_claude("main", "main@example.com", tier="default_claude_max_20x")
    sandbox.seed_codex("gpt", "gpt@example.com", plan="plus")
    menu.refresh()
    rows = menu.texts()
    assert rows[0] == "SUBSCRIPTIONS"
    main = text(menu.account_row("main"))
    # The three windows with their percentages, and the account marked as the
    # one the menu bar shows (it is the default account).
    assert main.startswith("▸ ") and "main" in main
    assert "5h" in main and " 58%" in main and "7d" in main and " 71%" in main
    assert "fable" in main and " 34%" in main
    gpt = text(menu.account_row("gpt"))
    # Pro Lite and Plus have no 5h window: the slot is kept and says so.
    assert "5h" in gpt and "none" in gpt and "7d" in gpt and " 62%" in gpt
    assert not gpt.startswith("▸")
    # The submenu names the slot, the email and the plan.
    assert menu.texts(menu.account_row("main"))[0].strip().endswith(
        "main    main@example.com   Max 20x")
    assert menu.texts(menu.account_row("gpt"))[0].strip().endswith(
        "gpt    gpt@example.com   Plus")
    # The usage block spells each window out with a countdown and a clock time.
    usage = menu.texts(menu.account_row("main"))
    assert "Usage" in usage[1]
    assert usage[2].startswith("5h\t58%\t") and "resets in" in usage[2]
    assert usage[4].startswith("fable 7d\t34%\t")
    # No request left the sandbox that was not for the fake server.
    assert all(r.service in ("anthropic", "openai") for r in fake_server.requests)


def test_menu_bar_title_follows_the_pinned_account(sandbox, fake_server, menu):
    sandbox.seed_claude("main", "main@example.com")
    sandbox.seed_claude("spare", "spare@example.com", tier="default_claude_pro")
    menu.refresh()
    # The default account is drawn in the menu bar.
    assert menu.title().startswith("⇄ main 5h 58%")
    assert text(menu.account_row("main")).startswith("▸")
    assert not text(menu.account_row("spare")).startswith("▸")
    # Pinning another account moves the title and the mark, and unticks the
    # "front tab" toggle, which the pin overrides.
    bar = menu.find("Show this account in the menu bar", menu.account_row("spare"))
    assert not checked(bar)
    click(bar)
    assert core.pref("bar_account") == "spare"
    assert menu.title().startswith("⇄ spare 5h 58%")
    assert text(menu.account_row("spare")).startswith("▸")
    assert not text(menu.account_row("main")).startswith("▸")
    assert checked(menu.find("Show this account in the menu bar", menu.account_row("spare")))
    assert not checked(menu.find("Show the front tab's account"))
    # Turning the front tab toggle back on drops the pin.
    click(menu.find("Show the front tab's account"))
    assert checked(menu.find("Show the front tab's account"))
    assert core.pref("bar_account") == ""
    assert menu.title().startswith("⇄ main 5h 58%")


def test_menu_bar_image_states(sandbox, fake_server, menu, monkeypatch):
    """What the status item draws: the shown account's batteries, dimmed when
    its numbers cannot be trusted, and only the windows its plan has."""
    sandbox.seed_claude("main", "main@example.com")
    sandbox.seed_codex("gpt", "gpt@example.com", plan="plus")
    drawn: list = []
    images: list = []
    real = menubar.gauge.status_image

    def spy(sections):
        drawn.append(sections)
        return real(sections)

    monkeypatch.setattr(menubar.gauge, "status_image", spy)
    item = SimpleNamespace(setTitle_=lambda t: None,
                           button=lambda: SimpleNamespace(setImage_=images.append))
    monkeypatch.setattr(menu.app, "_nsapp", SimpleNamespace(nsstatusitem=item), raising=False)
    menu.refresh()
    (section,) = drawn[-1]
    assert (section.provider, section.name, section.dim) == ("claude", "main", False)
    assert [(c.caption, c.used, c.reset) for c in section.cells] == [
        ("5h", 58.0, "26752d"), ("7d", 71.0, "26754d"), ("fable", 34.0, "26754d")]
    assert images[-1] is not None
    # A Codex plan without a 5h window draws two batteries, not a blank third.
    click(menu.find("Show this account in the menu bar", menu.account_row("gpt")))
    (section,) = drawn[-1]
    assert (section.provider, section.name) == ("codex", "gpt")
    assert [(c.caption, c.used) for c in section.cells] == [("7d", 62.0)]
    # Rate limited with nothing cached: an empty, dimmed battery, not a full one.
    fake_server.add_claude("busy@example.com")
    sandbox.seed_claude("busy", "busy@example.com")
    fake_server.script("/api/oauth/usage", 429, {"error": "rate_limited"},
                       headers={"Retry-After": "300"})
    click(menu.find("Show the front tab's account"))
    core.set_pref("bar_account", "busy")
    menu.refresh()
    (section,) = drawn[-1]
    assert (section.name, section.dim) == ("busy", True)
    assert [(c.caption, c.used) for c in section.cells] == [("5h", None), ("7d", None)]


def test_an_empty_slot_offers_a_first_sign_in(sandbox, fake_server, menu):
    sandbox.seed_claude("main", "main@example.com")
    os.makedirs(sandbox.slot("ghost"))
    menu.refresh()
    row = menu.account_row("ghost")
    assert text(row).strip().endswith("not signed in")
    # Styled like every other row of the menu, not a bare title.
    assert "  Sign in" in menu.texts(row) and "Sign in again" not in "".join(menu.texts(row))
    signin = menu.find("Sign in", row)
    assert signin._menuitem.image() is not None
    assert menu.texts(signin)[0] == "Default browser"


def test_rate_limited_account_keeps_its_row_and_says_why(sandbox, fake_server, menu):
    sandbox.seed_claude("busy", "busy@example.com")
    fake_server.script("/api/oauth/usage", 429, {"error": "rate_limited"},
                       headers={"Retry-After": "300"})
    menu.refresh()
    row = text(menu.account_row("busy"))
    assert "rate limited, retrying in 5m" in row
    assert "58%" not in row
    # Still signed in: the submenu offers the ordinary actions, not a sign-in.
    sub = menu.texts(menu.account_row("busy"))
    assert sub[0].strip().endswith("busy    busy@example.com   Max 5x")
    assert "Usage" not in "".join(sub)
    assert any("Rename" in line for line in sub)
    # The menu bar does not pretend to know the numbers.
    assert menu.title() == "⇄ busy 5h - 7d -"
    # The limit clears: the next timed refresh reads normally. A forced one
    # inside the floor would not add a request to a limit it just hit.
    menu.refresh(force=True)
    assert "rate limited" in text(menu.account_row("busy"))
    assert len(fake_server.calls("/api/oauth/usage")) == 1
    menu.refresh(force=False)
    assert " 58%" in text(menu.account_row("busy"))
    assert "rate limited" not in text(menu.account_row("busy"))


def test_stale_usage_is_dated_on_the_row(sandbox, fake_server, menu):
    sandbox.seed_claude("old", "old@example.com")
    menu.refresh()
    store = json.load(open(core.USAGE_CACHE))
    store["old"]["at"] = time.time() - 15 * 60
    json.dump(store, open(core.USAGE_CACHE, "w"))
    fake_server.script("/api/oauth/usage", 429, {"error": "rate_limited"},
                       headers={"Retry-After": "300"})
    menu.refresh()
    row = text(menu.account_row("old"))
    assert " 58%" in row and "usage from 15m ago" in row


def test_expired_login_offers_sign_in_again(sandbox, fake_server, menu):
    before = sandbox.seed_claude("gone", "gone@example.com")
    menu.refresh()
    assert " 58%" in text(menu.account_row("gone"))
    # The token expires and the server refuses to renew it.
    sandbox._put_item(keychain.service_for(sandbox.slot("gone")),
                      json.dumps({"claudeAiOauth": {**before, "expiresAt": 1000}}))
    fake_server.revoke("gone@example.com")
    keychain.forget()
    menu.refresh()
    row = menu.account_row("gone")
    assert text(row).strip().endswith("gone            login expired")
    assert "%" not in text(row)
    sub = menu.texts(row)
    assert "  Sign in again" in sub
    assert not any("Rename" in line or "Remove account" in line for line in sub)
    browsers = menu.texts(menu.find("Sign in again", row))
    assert browsers[0] == "Default browser"
    assert menu.title() == "⇄ gone 5h - 7d -"


def test_a_slot_holding_another_login_says_so(sandbox, fake_server, menu):
    sandbox.seed_claude("main", "main@example.com")
    fake_server.add_claude("other@example.com")
    # The keychain item under "main" belongs to somebody else.
    sandbox._put_item(keychain.service_for(sandbox.slot("main")),
                      json.dumps({"claudeAiOauth": fake_server.blob("other@example.com")}))
    menu.refresh()
    row = menu.account_row("main")
    assert "holds other@example.com, not main@example.com" in text(row)
    sub = menu.texts(row)
    assert "  This is not main. Sign in again to fix it." in sub
    assert "  Sign in again" in sub


def test_sign_in_again_from_the_row_signs_the_account_in(sandbox, fake_server, menu):
    before = sandbox.seed_claude("gone", "gone@example.com")
    menu.refresh()
    sandbox._put_item(keychain.service_for(sandbox.slot("gone")),
                      json.dumps({"claudeAiOauth": {**before, "expiresAt": 1000}}))
    fake_server.revoke("gone@example.com")
    keychain.forget()
    menu.refresh()
    row = menu.account_row("gone")
    seen = len(sandbox.opened_urls())
    click(menu.find("Sign in again", row)["Default browser"])
    # The click is acknowledged in the menu and the row says it is waiting.
    assert menu.flash() == "Signing in as “gone”…"
    assert "signing in" in text(menu.account_row("gone"))
    assert "  Signing in…   finish in the browser" in menu.texts(menu.account_row("gone"))
    url = sandbox.wait_for_url(_Never(), seen, 5)
    assert url.startswith(fake_server.url + "/cai/oauth/authorize?")
    assert "login_hint=gone%40example.com" in url
    sandbox.approve(url)
    menu.settle()
    assert menu.flash() == "“gone” is signed in as gone@example.com"
    assert "signing in" not in text(menu.account_row("gone"))
    assert " 58%" in text(menu.account_row("gone"))
    assert sandbox.blob(sandbox.slot("gone"))["accessToken"] != before["accessToken"]
    assert menu.app._signing_in == {}


def test_a_refused_sign_in_interrupts_and_clears_the_mark(sandbox, fake_server, menu, dialogs):
    sandbox.seed_claude("main", "main@example.com")
    menu.refresh()
    seen = len(sandbox.opened_urls())
    fake_server.deny_next_authorize = True
    click(menu.find("Sign in again", menu.account_row("main"))["Default browser"])
    sandbox.approve(sandbox.wait_for_url(_Never(), seen, 5))
    menu.settle()
    assert dialogs.alerts[-1]["message"] == "Sign-in was refused: The user denied the request"
    assert menu.flash() == dialogs.alerts[-1]["message"]
    assert "signing in" not in text(menu.account_row("main"))
    assert menu.app._signing_in == {}


def test_add_a_claude_account_from_the_menu(sandbox, fake_server, menu, dialogs):
    sandbox.seed_claude("main", "main@example.com")
    fake_server.add_claude("new@example.com")
    fake_server.browser = "new@example.com"
    menu.refresh()
    seen = len(sandbox.opened_urls())
    dialogs.answers.append((1, "second"))
    click(menu.find("Add a Claude account…"))
    assert dialogs.windows[-1]["title"] == "Add a Claude account"
    url = sandbox.wait_for_url(_Never(), seen, 5)
    assert url.startswith(fake_server.url + "/cai/oauth/authorize?")
    sandbox.approve(url)
    menu.settle()
    assert menu.flash() == "“second” is signed in as new@example.com"
    assert sandbox.blob(sandbox.slot("second"))
    assert "new@example.com" in "".join(menu.texts(menu.account_row("second")))


def test_cancelling_the_add_dialog_opens_nothing(sandbox, fake_server, menu, dialogs):
    sandbox.seed_claude("main", "main@example.com")
    menu.refresh()
    dialogs.answers.append((0, "second"))
    click(menu.find("Add a Claude account…"))
    assert sandbox.opened_urls() == []
    assert not os.path.exists(sandbox.slot("second"))


def test_rename_moves_the_login_and_redraws_at_once(sandbox, fake_server, menu, dialogs):
    sandbox.seed_claude("main", "main@example.com")
    sandbox.seed_claude("spare", "spare@example.com")
    menu.refresh()
    assert core.rules().default_account == "main"
    dialogs.answers.append((1, "office"))
    click(menu.find("Rename…", menu.account_row("main")))
    assert dialogs.windows[-1]["title"] == "Rename main"
    assert menu.flash() == "Renaming “main”…"
    menu.settle()
    assert dialogs.notifications[-1]["subtitle"] == "Renamed"
    assert dialogs.notifications[-1]["message"] == "main is now office"
    assert menu.account_names() == ["office", "spare"]
    assert sandbox.blob(sandbox.slot("office"))
    assert sandbox.blob(sandbox.slot("main")) is None
    assert not os.path.exists(sandbox.slot("main"))
    assert core.rules().default_account == "office"
    assert menu.title().startswith("⇄ office")
    assert " 58%" in text(menu.account_row("office"))


def test_rename_to_a_taken_name_fails_without_moving_anything(
        sandbox, fake_server, menu, dialogs):
    sandbox.seed_claude("main", "main@example.com")
    sandbox.seed_claude("spare", "spare@example.com")
    menu.refresh()
    dialogs.answers.append((1, "spare"))
    click(menu.find("Rename…", menu.account_row("main")))
    menu.settle()
    assert dialogs.notifications[-1]["subtitle"] == "Rename failed"
    assert dialogs.notifications[-1]["message"] == "spare already exists"
    assert menu.flash() == "spare already exists"
    assert menu.account_names() == ["main", "spare"]
    assert sandbox.blob(sandbox.slot("main")) and sandbox.blob(sandbox.slot("spare"))


def test_remove_deletes_the_login_after_a_confirmation(sandbox, fake_server, menu, dialogs):
    sandbox.seed_claude("main", "main@example.com")
    sandbox.seed_claude("spare", "spare@example.com")
    menu.refresh()
    dialogs.answers.append(0)
    click(menu.find("Remove account…", menu.account_row("spare")))
    assert dialogs.alerts[-1]["title"] == "Remove spare?"
    assert "deletes its stored login from the keychain" in dialogs.alerts[-1]["message"]
    menu.settle()
    assert sandbox.blob(sandbox.slot("spare"))
    dialogs.answers.append(1)
    click(menu.find("Remove account…", menu.account_row("spare")))
    menu.settle()
    assert sandbox.blob(sandbox.slot("spare")) is None
    assert not os.path.exists(sandbox.slot("spare"))
    assert menu.account_names() == ["main"]


def test_rename_and_remove_keep_the_keychain_off_the_drawing_thread(
        sandbox, fake_server, menu, dialogs, monkeypatch):
    """A `security` call can take seconds on a slow keychain, and AppKit
    draws on the thread that would be waiting for it."""
    sandbox.seed_claude("main", "main@example.com")
    sandbox.seed_claude("spare", "spare@example.com")
    menu.refresh()
    on_main: list[bool] = []
    run = keychain._run

    def spy(args, stdin=None):
        on_main.append(threading.current_thread() is threading.main_thread())
        return run(args, stdin)

    monkeypatch.setattr(keychain, "_run", spy)
    dialogs.answers.append((1, "office"))
    click(menu.find("Rename…", menu.account_row("spare")))
    assert menu.flash() == "Renaming “spare”…"
    menu.settle()
    assert on_main and not any(on_main)
    assert menu.account_names() == ["main", "office"]
    assert sandbox.blob(sandbox.slot("office")) and sandbox.blob(sandbox.slot("spare")) is None
    assert menu.flash() == "spare is now office"
    assert dialogs.notifications[-1]["subtitle"] == "Renamed"
    on_main.clear()
    dialogs.answers.append(1)
    click(menu.find("Remove account…", menu.account_row("office")))
    assert menu.flash() == "Removing “office”…"
    # Gone from the menu at once, before the poll that would confirm it.
    assert menu.account_names() == ["main"]
    menu.settle()
    assert on_main and not any(on_main)
    assert sandbox.blob(sandbox.slot("office")) is None
    assert menu.flash() == "“office” removed"
    assert menu.account_names() == ["main"]


def test_remove_a_codex_account(sandbox, fake_server, menu, dialogs):
    sandbox.seed_codex("gpt", "gpt@example.com")
    menu.refresh()
    dialogs.answers.append(1)
    click(menu.find("Remove account…", menu.account_row("gpt")))
    assert dialogs.alerts[-1]["title"] == "Remove gpt?"
    assert "deletes its login file" in dialogs.alerts[-1]["message"]
    menu.settle()
    assert not os.path.lexists(sandbox.codex_slot("gpt"))
    assert menu.account_names() == []


def test_colour_picks_write_the_chip_table(sandbox, fake_server, menu):
    sandbox.seed_claude("main", "main@example.com")
    menu.refresh()
    palette = menu.find("Colour", menu.account_row("main"))
    before = core.chip_index("main", 12)
    ticked = [i for i, row in enumerate(menu.items(palette)) if "✓" in text(row)]
    assert ticked == [before]
    pick = (before + 3) % 12
    click(menu.items(palette)[pick])
    assert core.chip_index("main", 12) == pick
    palette = menu.find("Colour", menu.account_row("main"))
    assert [i for i, row in enumerate(menu.items(palette)) if "✓" in text(row)] == [pick]


class _Never:
    """A process stand-in that never exits, for `Sandbox.wait_for_url`."""

    def poll(self):
        return None

    def kill(self):
        pass


def test_the_add_account_dialogs_say_a_browser_opens(sandbox, fake_server, menu, dialogs):
    """Both sign-ins go through the browser, so neither dialog may promise a Terminal."""
    sandbox.seed_claude("main", "main@example.com")
    menu.refresh()
    for label in ("Add a Claude account…", "Add a Codex account…"):
        dialogs.answers.append((0, ""))
        click(menu.find(label))
        asked = dialogs.windows[-1]
        assert asked["title"] == label.rstrip("…")
        assert "browser opens" in asked["message"]
        assert "Terminal" not in asked["message"] and "/login" not in asked["message"]
        assert asked["ok"] == "Open browser"
    assert sandbox.opened_urls() == []
