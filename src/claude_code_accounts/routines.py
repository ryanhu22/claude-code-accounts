"""Start weekly windows from the cloud, so a closed laptop cannot delay them.

A weekly window starts on the first request after it resets, and every reset
after that is a week from that request. ccm's automatic start sends the
request, but only while the Mac is awake: a reset at 21:00 with the lid shut
waits for the morning, and every later reset slides by the same hours.

A Claude Code routine runs on Anthropic's side on a schedule, and its run
counts against the account it belongs to. So each Claude account gets one
routine of ours, set for a couple of minutes after its next weekly reset. When
it fires it sends one tiny request and the new window starts, laptop or not.
The next time the app reads usage it moves the same routine to the next reset.

One routine per account, never a new one each week: the API this talks to can
create and edit routines but not delete them, so a duplicate is permanent
clutter in the user's list on claude.ai. Everything below leans that way. The
id we were given is the routine's identity whatever it is renamed to, a
routine is created only after every page of the list has been read, and a
create's id is written down before anything else can fail.

The endpoints are Claude Code's own and in research preview, so every failure
here is reported and retried, never fatal to the refresh that called it.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import fcntl
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import core

NAME = "Start weekly windows (ccm)"
PROMPT = "Reply with the single word ok. Do not use any tools."
# The routine for an account with no model-scoped weekly window. One with a
# Fable window gets the Fable model instead, because only a request to that
# model starts it, and that request starts the general weekly window as well.
MODEL = "claude-sonnet-5-5"
# A run can start a few minutes late but never early, and a run before the
# reset would count against the old window and start nothing.
LEAD = 120
RETRY = 3600.0
WEEK = 604800
BETA = "ccr-triggers-2026-01-30"
_PAGES = 50

# The refresh worker arms, and turning automatic start off disarms from its
# own thread, so one of them at a time in this process. The file lock does
# the same across processes: two menu bar apps listing an empty account at
# once would each create a routine.
_LOCK = threading.Lock()


class CloudStartError(Exception):
    """A routine could not be set, in words the account's menu can show."""


def plan_for(acct: core.Account) -> tuple[str, str] | None:
    """When this account's routine should run, and on which model.

    None while any weekly window has no clock: that window is waiting for its
    first request, which is the local automatic start's job, and the reset to
    aim at is only known once it runs.

    The reset is taken to the nearest minute before the lead is added. The
    server reports the same reset as 04:59:59.58 for one window and 05:00:00.08
    for the next, and rounding up after the lead turned those into two run
    times, which re-armed the routine on every refresh.
    """
    if acct.is_codex or not acct.signed_in or not acct.reading:
        return None
    weekly = [lim for lim in acct.limits if lim.span == WEEK]
    if not weekly or any(not lim.resets_at or lim.over for lim in weekly):
        return None
    ends = min(_parse(lim.resets_at).timestamp() for lim in weekly)
    model = next((core.POKE_MODEL_SCOPED[lim.label] for lim in weekly
                  if lim.label in core.POKE_MODEL_SCOPED), MODEL)
    return _iso(round(ends / 60) * 60 + LEAD), model


def status() -> dict[str, dict]:
    """What each account's routine is set to, by account name.

    An entry holds `trigger` (the routine's id), `run_at` and `model` (what it
    was last set to), `enabled` (True, False, or None when a request failed
    and the server's answer is unknown), `error` with `error_op` ("arm" or
    "off") when the last attempt failed, and `tried_at`. Anything in the file
    that is not that shape is dropped here, so the menu never draws from it.
    """
    store = core._cache_read(core.CLOUD_STARTS)
    if not isinstance(store, dict):
        return {}
    return {name: entry for name, entry in store.items()
            if isinstance(name, str) and isinstance(entry, dict)}


def sync(accts: list[core.Account]) -> list[tuple[str, bool, str]]:
    """Bring every account's routine to what the preference says it should be.

    On: each account whose next weekly reset is known has a routine set for
    just after it. Off: every routine ccm set is switched off, so turning the
    preference off stops the runs as well as the local starts. Safe to call on
    every refresh, since it only reaches the server for an account whose
    target moved, or whose last attempt failed more than RETRY ago.
    """
    with _held():
        if core.pref(core.AUTO_START_PREF, False):
            return _arm_all(accts)
        _disarm_all()
        return []


@contextlib.contextmanager
def _held():
    with _LOCK:
        os.makedirs(os.path.dirname(core.CLOUD_STARTS), exist_ok=True)
        with open(core.CLOUD_STARTS + ".lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield


def _arm_all(accts: list[core.Account]) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    store = status()
    for acct in accts:
        # Per account: a slow server ahead of this one must not let its usage
        # pass as fresh, nor its run time slip into the past unnoticed.
        now = time.time()
        entry = store.get(acct.name, {})

        def record(trigger: str, name: str = acct.name) -> None:
            store[name] = {**store.get(name, {}), "trigger": trigger, "enabled": None}
            _save(store)

        try:
            plan = _due(acct, entry, now)
            if plan is None:
                continue
            run_at, model = plan
            trigger = _arm(acct.name, run_at, model, entry.get("trigger"), record)
        except Exception as e:  # noqa: BLE001 - research preview: say what it said, retry later
            store[acct.name] = {**store.get(acct.name, {}), "enabled": None,
                                "error": _why(e), "error_op": "arm", "tried_at": now}
            results.append((acct.name, False, store[acct.name]["error"]))
        else:
            store[acct.name] = {"trigger": trigger, "run_at": run_at, "model": model,
                                "enabled": True, "error": None, "tried_at": now}
            core.log.info("cloud start %s %s %s", acct.name, run_at, trigger)
            results.append((acct.name, True, run_at))
        _save(store)
    return results


def _disarm_all() -> None:
    """Switch off every routine ccm set or may have set.

    Only a routine known to be off is left alone. One whose last request
    failed may be on, whatever the failed request was for, so it is looked up
    again: by its id, or by name when the create's reply never came back.
    """
    store = status()
    for name, entry in store.items():
        now = time.time()
        if entry.get("enabled") is False and not entry.get("error"):
            continue
        if entry.get("error_op") == "off" and now - (entry.get("tried_at") or 0) < RETRY:
            continue
        try:
            token = _token(name)
            found = _find(token, entry.get("trigger"))
            if found and found.get("enabled"):
                _call("POST", f"/v1/code/triggers/{found['id']}", token, {"enabled": False})
                core.log.info("cloud start off %s %s", name, found["id"])
        except Exception as e:  # noqa: BLE001 - it fires once more at worst; retry later
            store[name] = {**entry, "error": _why(e), "error_op": "off", "tried_at": now}
        else:
            store[name] = {"trigger": found["id"] if found else entry.get("trigger"),
                           "enabled": False, "error": None, "tried_at": now}
        _save(store)


def _due(acct: core.Account, entry: dict, now: float) -> tuple[str, str] | None:
    """The plan to send, when this account's routine is not already on it.

    Usage this fresh proves the network answered, as it does for the local
    start, so a wake with Wi-Fi still down does not count as a failed attempt.
    """
    plan = plan_for(acct)
    if plan is None or not acct.usage_at or now - acct.usage_at > core.AUTO_START_FRESH:
        return None
    if entry.get("error"):
        return plan if now - (entry.get("tried_at") or 0) >= RETRY else None
    if (entry.get("run_at"), entry.get("model"), entry.get("enabled")) == (*plan, True):
        return None
    return plan


def _arm(name: str, run_at: str, model: str, known: str | None, record) -> str:
    """Set the account's routine to run once at `run_at`, creating it if needed.

    A new routine is created switched off and only switched on as the last
    step, so one whose setup failed halfway never runs. Its id goes to
    `record` the moment it exists, so the retry finds it instead of making
    another.
    """
    token = _token(name)
    found = _find(token, known)
    if found is None:
        _still_ahead(run_at)
        found = _call("POST", "/v1/code/triggers", token, {
            "name": NAME, "run_once_at": run_at, "enabled": False,
            "job_config": _job(_environment(token), model)})["trigger"]
        record(found["id"])
    if found.get("mcp_connections"):
        # The server attaches every connector the account has to a new
        # routine. A one-word reply needs none of them, and each one is tool
        # definitions loaded into the run, and access to mail or files it has
        # no use for.
        _call("POST", f"/v1/code/triggers/{found['id']}", token, {"clear_mcp_connections": True})
    environment = ((found.get("job_config") or {}).get("ccr") or {}).get("environment_id")
    _still_ahead(run_at)
    # The whole job every time, not only the time: a routine found by name, or
    # edited on claude.ai, must run our prompt and nothing else.
    _call("POST", f"/v1/code/triggers/{found['id']}", token, {
        "run_once_at": run_at, "enabled": True,
        "job_config": _job(environment or _environment(token), model)})
    return found["id"]


def _find(token: str, known: str | None) -> dict | None:
    """Our routine on this account: by the id we kept, else by its name.

    The id wins even if the routine was renamed since, because a rename is
    not a reason for a second one. A miss by name only counts once the last
    page has been read; a list that stops early is an error, never "none".
    """
    if known:
        try:
            trigger = _call("GET", f"/v1/code/triggers/{known}", token).get("trigger")
            if trigger:
                return trigger
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    cursor = ""
    for _ in range(_PAGES):
        query = f"?cursor={urllib.parse.quote(cursor, safe='')}" if cursor else ""
        page = _call("GET", "/v1/code/triggers" + query, token)
        mine = next((t for t in page.get("data") or [] if t.get("name") == NAME), None)
        if mine:
            return mine
        if not page.get("has_more"):
            return None
        if not page.get("next_cursor") or page["next_cursor"] == cursor:
            raise CloudStartError("could not read the whole list of routines")
        cursor = page["next_cursor"]
    raise CloudStartError("could not read the whole list of routines")


def _still_ahead(run_at: str) -> None:
    if run_at <= _iso(time.time()):
        raise CloudStartError("the reset passed before the routine was set")


def _environment(token: str) -> str:
    """Where a routine of this account runs. An account that has never used
    Claude Code on the web has none, and nothing here can make one."""
    envs = _call("GET", "/v1/environment_providers", token).get("environments") or []
    env = next((e for e in envs if e.get("kind") == "anthropic_cloud"
                and e.get("state") == "active"), None)
    if not env:
        raise CloudStartError("no cloud environment on this account")
    return env["environment_id"]


def _job(environment_id: str, model: str) -> dict:
    return {"ccr": {
        "environment_id": environment_id,
        "session_context": {"model": model, "sources": [], "allowed_tools": []},
        "events": [{"data": {
            "uuid": str(uuid.uuid4()), "session_id": "", "type": "user",
            "parent_tool_use_id": None,
            "message": {"content": PROMPT, "role": "user"}}}]}}


def _token(name: str) -> str:
    blob = core.live_blob(core.slot_dir(name))
    if not blob or not blob.get("accessToken"):
        raise CloudStartError("not signed in")
    return blob["accessToken"]


def _call(method: str, path: str, token: str, body: dict | None = None) -> dict:
    headers = {**core.oauth_headers(), "Authorization": f"Bearer {token}",
               "anthropic-version": core.ANTHROPIC_VERSION,
               "Content-Type": "application/json"}
    headers["anthropic-beta"] = f"{headers['anthropic-beta']},{BETA}"
    req = urllib.request.Request(core.API + path, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _save(store: dict) -> None:
    core._cache_write(store, core.CLOUD_STARTS)


def _why(e: Exception) -> str:
    return str(e) if isinstance(e, CloudStartError) else core._describe(e)


def _parse(iso: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
