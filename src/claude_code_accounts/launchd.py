"""Install the menu bar app as a login item."""

import os
import shutil
import subprocess
import sys
from xml.sax.saxutils import escape

from . import core

LABEL = "com.claude-code-accounts"
PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude-code-accounts</string>
    <key>ProgramArguments</key>
    <array>
        <string>{program}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{bindir}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>StandardOutPath</key>
    <string>{home}/Library/Logs/claude-code-accounts.log</string>
    <key>StandardErrorPath</key>
    <string>{home}/Library/Logs/claude-code-accounts.log</string>
</dict>
</plist>
"""


def plist_path() -> str:
    return os.path.join(core.HOME, "Library", "LaunchAgents", LABEL + ".plist")


def find_menubar() -> str | None:
    for executable in (os.path.realpath(sys.argv[0]), sys.executable):
        program = os.path.join(os.path.dirname(executable), "ccm-menubar")
        if os.path.isfile(program) and os.access(program, os.X_OK):
            return os.path.abspath(program)
    program = shutil.which("ccm-menubar")
    return os.path.abspath(program) if program else None


def render(program: str) -> str:
    return PLIST_TEMPLATE.format(
        program=escape(program), bindir=escape(os.path.dirname(program)), home=escape(core.HOME),
    )


def install() -> tuple[bool, str]:
    program = find_menubar()
    if program is None:
        return False, (
            'ccm-menubar is not installed. Install the menubar extra: uv tool install '
            '"claude-code-accounts[menubar] @ git+https://github.com/ryanhu22/claude-code-accounts"'
        )
    path = plist_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(os.path.join(core.HOME, "Library", "Logs"), exist_ok=True)
    domain = f"gui/{os.getuid()}"
    if os.path.exists(path):
        # A stale agent must not block a reinstall.
        subprocess.run(
            ["launchctl", "bootout", f"{domain}/{LABEL}"],
            capture_output=True, text=True, timeout=30,
        )
    with open(path, "w", encoding="utf-8") as file:
        file.write(render(program))
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, path],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        return False, f"launchctl bootstrap failed: {(result.stderr or result.stdout).strip()}"
    return True, (
        "The menu bar app starts now and at every login. "
        "Remove it with: ccm menubar uninstall"
    )


def uninstall() -> tuple[bool, str]:
    path = plist_path()
    if not os.path.exists(path):
        return True, "The menu bar app was not installed as a login item"
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
        capture_output=True, text=True, timeout=30,
    )
    os.remove(path)
    return True, (
        "The menu bar app no longer starts at login. Quit the running one from its menu."
    )
