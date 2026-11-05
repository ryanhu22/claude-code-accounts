"""When an account's cloud routine should run, and on which model."""
from claude_code_accounts import core, routines

WEEK = 604800


def account(*limits, provider="claude", email="a@example.com"):
    return core.Account(name="a", slot="/x", email=email, limits=list(limits),
                        provider=provider)


def weekly(resets_at, label="7d", kind="weekly_all", scope=""):
    return core.Limit(kind=kind, label=label, percent=10, resets_at=resets_at,
                      span=WEEK, scope=scope)


FIVE_HOURS = core.Limit(kind="session", label="5h", percent=3,
                        resets_at="2100-01-01T00:00:00Z", span=18000)


def test_runs_two_minutes_after_the_reset_on_a_whole_minute():
    acct = account(FIVE_HOURS, weekly("2100-01-03T04:59:59.584721+00:00"))
    assert routines.plan_for(acct) == ("2100-01-03T05:02:00Z", routines.MODEL)


def test_aims_at_the_earliest_weekly_reset_and_starts_fable_with_fable():
    acct = account(weekly("2100-01-03T05:00:00Z"),
                   weekly("2100-01-02T08:00:00Z", label="fable", kind="weekly_scoped",
                          scope="fable"))
    assert routines.plan_for(acct) == ("2100-01-02T08:02:00Z", "claude-fable-5-1")


def test_leaves_a_stopped_or_ended_weekly_window_to_the_local_start():
    assert routines.plan_for(account(weekly(None))) is None
    assert routines.plan_for(account(weekly("2000-01-01T00:00:00Z"))) is None
    # One stopped window is enough: the reset to aim at is not known yet.
    assert routines.plan_for(account(
        weekly("2100-01-03T05:00:00Z"),
        weekly(None, label="fable", kind="weekly_scoped", scope="fable"))) is None


def test_no_plan_without_a_reading_a_login_or_a_claude_account():
    assert routines.plan_for(account()) is None
    assert routines.plan_for(account(weekly("2100-01-03T05:00:00Z"), email=None)) is None
    assert routines.plan_for(account(weekly("2100-01-03T05:00:00Z"),
                                     provider="codex")) is None
    assert routines.plan_for(account(FIVE_HOURS)) is None
