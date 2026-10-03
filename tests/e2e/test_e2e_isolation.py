"""Tripwires: the sandbox cannot reach the real keychain, home, hosts or launchd."""
import os
import pwd
import shutil
import subprocess

from e2e.harness import TOOLS

PRODUCTION = {
    "API": "https://api.anthropic.com",
    "TOKEN_URLS": ("https://platform.claude.com/v1/oauth/token",
                   "https://console.anthropic.com/v1/oauth/token"),
    "AUTHORIZE_URL": "https://claude.com/cai/oauth/authorize",
    "CODEX_AUTHORIZE_URL": "https://auth.openai.com/oauth/authorize",
    "CODEX_TOKEN_URL": "https://auth.openai.com/oauth/token",
    "CODEX_USAGE_URL": "https://chatgpt.com/backend-api/wham/usage",
    "CODEX_RESET_CREDITS_URL": "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits",
}
PRINT_URLS = (
    "from claude_code_accounts import codex, core, oauth\n"
    "print(repr({'API': core.API, 'TOKEN_URLS': core.TOKEN_URLS,"
    " 'AUTHORIZE_URL': oauth.AUTHORIZE_URL, 'CODEX_AUTHORIZE_URL': codex.AUTHORIZE_URL,"
    " 'CODEX_TOKEN_URL': codex.TOKEN_URL, 'CODEX_USAGE_URL': codex.USAGE_URL,"
    " 'CODEX_RESET_CREDITS_URL': codex.RESET_CREDITS_URL}))"
)
UNSET = {k: "" for k in ("CCM_API_BASE", "CCM_TOKEN_URL", "CCM_AUTHORIZE_URL",
                         "CCM_CODEX_AUTH_BASE", "CCM_CODEX_API_BASE")}


def test_the_sandbox_is_self_contained(sandbox, tmp_path):
    env = sandbox.env
    real_home = pwd.getpwuid(os.getuid()).pw_dir
    assert env["HOME"].startswith(str(tmp_path)) and not env["HOME"].startswith(real_home)
    for key, value in env.items():
        if key.startswith("CCM_") and key.endswith(("_BASE", "_URL")):
            assert value.startswith("http://127.0.0.1:"), (key, value)
    assert env["PATH"].split(":")[0] == sandbox.bin
    for tool in ("security", *TOOLS):
        found = shutil.which(tool, path=env["PATH"])
        assert found == os.path.join(sandbox.bin, tool), (tool, found)
    assert env["https_proxy"].startswith("http://127.0.0.1:")
    assert env["no_proxy"] == "127.0.0.1,localhost"


def test_every_url_ccm_reads_points_at_the_fake(sandbox, fake_server):
    r = sandbox.python(PRINT_URLS)
    assert r.returncode == 0, r.stderr
    urls = eval(r.stdout)  # noqa: S307 - our own repr of a dict of strings
    for key, value in urls.items():
        for one in (value if isinstance(value, tuple) else (value,)):
            assert one.startswith(fake_server.url), (key, one)


def test_production_urls_are_unchanged_without_the_overrides(sandbox):
    r = sandbox.python(PRINT_URLS, env=UNSET)
    assert r.returncode == 0, r.stderr
    assert eval(r.stdout) == PRODUCTION  # noqa: S307 - our own repr


def test_a_real_host_is_unreachable_from_the_sandbox(sandbox):
    """Even with the overrides gone, a request for a real host dies on loopback."""
    r = sandbox.python(
        "import urllib.error\n"
        "from claude_code_accounts import core\n"
        "try:\n"
        "    core._get('/api/oauth/profile', 'not-a-token')\n"
        "except urllib.error.HTTPError as e:\n"
        "    print('REACHED', e.code)\n"
        "except urllib.error.URLError as e:\n"
        "    print('BLOCKED', e.reason)\n",
        env=UNSET, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("BLOCKED"), r.stdout


def test_keychain_calls_name_only_sandbox_items(sandbox, fake_server, run_ccm):
    from claude_code_accounts import keychain

    sandbox.seed_claude("a", "a@example.com")
    assert run_ccm("list").returncode == 0
    calls = sandbox.keychain_log()
    assert calls, "ccm read no credential at all, so the fake was not what it used"
    allowed = {keychain.service_for(p) for p in (sandbox.slot("a"), sandbox.default_config)}
    allowed.add(keychain.LEGACY_SERVICE)
    for call in calls:
        argv = call["argv"]
        service = argv[argv.index("-s") + 1]
        assert service in allowed, call
        if "-a" in argv:
            assert argv[argv.index("-a") + 1] == "e2e"
    assert set(sandbox.keychain()) <= allowed
    assert sandbox.tripwire() == []


def test_the_fake_security_refuses_to_run_outside_a_sandbox(sandbox):
    env = {k: v for k, v in sandbox.env.items() if k != "CCM_E2E_KEYCHAIN_FILE"}
    r = subprocess.run(["security", "find-generic-password", "-s", "x", "-w"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 2 and "CCM_E2E_KEYCHAIN_FILE" in r.stderr
    assert not sandbox.keychain_log()


def test_launchd_is_a_tripwire(sandbox, run_ccm):
    """Reaching launchctl fails the call and leaves a trace, instead of installing anything."""
    r = run_ccm("menubar", "install")
    assert r.returncode != 0 and "launchctl bootstrap failed" in r.stdout
    assert [t["argv"][0] for t in sandbox.tripwire()] == ["bootstrap"]
    # The plist it wrote first went into the sandbox's home, nowhere else.
    agents = os.path.join(sandbox.home, "Library", "LaunchAgents")
    assert os.listdir(agents) == ["com.claude-code-accounts.plist"]
    with open(os.path.join(agents, "com.claude-code-accounts.plist")) as f:
        assert sandbox.home in f.read()
