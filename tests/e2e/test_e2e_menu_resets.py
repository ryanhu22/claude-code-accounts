"""Starting windows and spending resets from an account's menu.

Each click here sends a real request to the fake server, off the main
thread, and reports back into the flash row, a notification, or a dialog.
"""
from claude_code_accounts import core, menubar
from e2e.menu_harness import checked, click, text

GRANT = {"id": "grant-1", "resets_left": 1, "usable_now": True, "paused": False,
         "ends_at": "2100-02-01T12:00:00Z", "clears": ["five_hour", "seven_day"],
         "use_requires_limit": False}
CREDIT = {"id": "credit-1", "status": "available", "is_supported_by_plan": True,
          "expires_at": "2100-03-01T12:00:00Z", "reset_type": "codex_rate_limits"}


def idle_account(fake_server, email):
    acct = fake_server.add_claude(email)
    for lim in acct.limits:
        lim["percent"], lim["resets_at"] = 0, None
    return acct


def test_poke_starts_the_stopped_windows(sandbox, fake_server, menu):
    acct = idle_account(fake_server, "main@example.com")
    sandbox.seed_claude("main", "main@example.com")
    menu.refresh()
    row = menu.account_row("main")
    assert "(unused)" in text(row)
    poke = menu.find("Start the 5h, 7d, fable windows now", row)
    click(poke)
    assert menu.flash() == "main: starting its windows…"
    menu.settle()
    assert menu.flash() == "main: 1 window group(s) started"
    # One request to the scoped model starts every window at once.
    assert [p["model"] for p in fake_server.pokes] == ["claude-fable-5-1"]
    assert all(lim["resets_at"] for lim in acct.limits)
    # The forced refresh after it already shows the clocks running, and a
    # second look is armed for what the usage endpoint reports late.
    assert "(unused)" not in text(menu.account_row("main"))
    assert [seconds for seconds, _fn in menu.delayed] == [12.0]
    menu.fire_delayed()


def test_a_poke_that_fails_interrupts(sandbox, fake_server, menu, dialogs):
    idle_account(fake_server, "main@example.com")
    sandbox.seed_claude("main", "main@example.com")
    menu.refresh()
    fake_server.script("/v1/messages", 500, {"type": "error",
                                             "error": {"message": "overloaded"}})
    click(menu.find("Start the", menu.account_row("main")))
    menu.settle()
    assert dialogs.alerts[-1]["message"] == (
        "Could not start the windows for main.\n\nclaude-fable-5-1: overloaded")
    assert menu.flash() == "main: claude-fable-5-1: overloaded"
    assert menu.delayed == []


def test_automatic_weekly_start_is_a_toggle_that_pokes_at_once(sandbox, fake_server, menu):
    acct = idle_account(fake_server, "main@example.com")
    sandbox.seed_claude("main", "main@example.com")
    menu.refresh()
    toggle = menu.find("Start weekly windows automatically")
    assert not checked(toggle) and not core.pref(core.AUTO_START_PREF, False)
    assert fake_server.pokes == []
    click(toggle)
    assert core.pref(core.AUTO_START_PREF) is True
    assert checked(menu.find("Start weekly windows automatically"))
    menu.settle()
    assert menu.flash() == "main: weekly windows started automatically"
    assert [p["model"] for p in fake_server.pokes] == ["claude-fable-5-1"]
    assert all(lim["resets_at"] for lim in acct.limits)
    # Once an hour per account at most: another refresh sends nothing.
    menu.refresh()
    assert len(fake_server.pokes) == 1
    click(menu.find("Start weekly windows automatically"))
    assert core.pref(core.AUTO_START_PREF) is False
    assert not checked(menu.find("Start weekly windows automatically"))


def test_claude_reset_submenu_and_a_spent_reset(sandbox, fake_server, menu, dialogs):
    acct = fake_server.add_claude("main@example.com", grants=[dict(GRANT)])
    sandbox.seed_claude("main", "main@example.com")
    menu.refresh()
    row = menu.account_row("main")
    said = menu.texts(row)
    assert f"resets\t1\t  expires {menubar._day(GRANT['ends_at'])}" in said
    reset = menu.find("Reset your limits now…", row)
    assert menu.texts(reset) == [
        "  Use your reset now",
        "  The 5h and 7d limits go back to 0%.",
        f"  Use it by {menubar._day(GRANT['ends_at'])}, or it expires.",
    ]
    click(menu.find("Use your reset now", reset))
    assert menu.flash() == "main: using a reset…"
    menu.settle()
    (sent,) = fake_server.resets
    assert sent["grant_id"] == "grant-1" and sent["program"] == "cedar_ember"
    assert sent["org"] == acct.org_uuid and sent["request_id"]
    assert menu.flash() == "main: limits reset, that was the last reset"
    assert dialogs.notifications[-1] == {"title": "Claude Code Accounts", "subtitle": "main",
                                         "message": "limits reset, that was the last reset"}
    assert dialogs.alerts == []
    # The windows read 0% and the spent reset is gone from the menu.
    drawn = text(menu.account_row("main"))
    assert "  0%" in drawn and " 58%" not in drawn


def test_a_claude_reset_that_needs_a_limit_says_so(sandbox, fake_server, menu, dialogs):
    fake_server.add_claude("main@example.com", grants=[{**GRANT, "resets_left": 2,
                                                         "use_requires_limit": True}])
    sandbox.seed_claude("main", "main@example.com")
    menu.refresh()
    reset = menu.find("Reset your limits now…", menu.account_row("main"))
    assert menu.texts(reset)[:2] == ["  Use 1 of 2 resets now",
                                     "  The 5h and 7d limits go back to 0%."]
    assert "  It works only after you reach a limit." in menu.texts(reset)
    click(menu.find("Use 1 of 2 resets now", reset))
    menu.settle()
    assert dialogs.alerts[-1]["message"] == (
        "Could not reset the limits for main.\n\nthis reset can only be used at a limit")
    assert dialogs.notifications == []
    assert len(fake_server.resets) == 1
    assert " 58%" in text(menu.account_row("main"))


def test_codex_reset_credit_submenu_and_a_spent_credit(sandbox, fake_server, menu, dialogs):
    acct = fake_server.add_codex("gpt@example.com", plan="plus", credits=[dict(CREDIT)])
    sandbox.seed_codex("gpt", "gpt@example.com")
    menu.refresh()
    row = menu.account_row("gpt")
    assert "credits\tnone\t  1 reset credit" in menu.texts(row)
    reset = menu.find("Reset every window now…", row)
    assert menu.texts(reset) == [
        "  Use 1 of 1 reset credit now",
        "  Every window of this account goes back to 0%.",
        f"  The one that expires first goes, on {menubar._day(CREDIT['expires_at'])}.",
    ]
    click(menu.find("Use 1 of 1 reset credit now", reset))
    assert menu.flash() == "gpt: using a reset…"
    menu.settle()
    (sent,) = fake_server.calls("/backend-api/wham/rate-limit-reset-credits/consume")
    assert sent.json["credit_id"] == "credit-1"
    assert acct.credits[0]["status"] == "redeemed"
    assert menu.flash() == "gpt: windows reset, that was the last reset credit"
    assert dialogs.notifications[-1]["subtitle"] == "gpt"
    drawn = text(menu.account_row("gpt"))
    assert "  0%" in drawn and " 62%" not in drawn


def test_a_codex_reset_the_server_refuses_interrupts(sandbox, fake_server, menu, dialogs):
    fake_server.add_codex("gpt@example.com", plan="plus", credits=[dict(CREDIT)])
    sandbox.seed_codex("gpt", "gpt@example.com")
    menu.refresh()
    fake_server.script("/backend-api/wham/rate-limit-reset-credits/consume", 400,
                       {"detail": "Credit is not applicable to this plan"})
    reset = menu.find("Reset every window now…", menu.account_row("gpt"))
    click(menu.find("Use 1 of 1 reset credit now", reset))
    menu.settle()
    assert dialogs.alerts[-1]["message"] == (
        "Could not reset the limits for gpt.\n\n"
        "the reset was refused (HTTP 400: Credit is not applicable to this plan)")
    assert " 62%" in text(menu.account_row("gpt"))


def test_a_codex_account_offers_to_start_its_weekly_window(sandbox, fake_server, menu):
    acct = fake_server.add_codex("gpt@example.com", plan="plus")
    acct.usage["rate_limit"]["primary_window"] = {
        "used_percent": 0, "limit_window_seconds": 604800,
        "reset_after_seconds": 604800, "reset_at": 4102444800}
    sandbox.seed_codex("gpt", "gpt@example.com")
    menu.refresh()
    click(menu.find("Start the 7d window now", menu.account_row("gpt")))
    menu.settle()
    assert menu.flash() == "gpt: 1 window group(s) started"
    (ran,) = sandbox.codex_execs()
    assert ran["CODEX_HOME"] == sandbox.codex_slot("gpt")
