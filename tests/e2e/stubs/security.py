"""A fake `security` command: the macOS keychain as one JSON file.

Implements exactly what src/claude_code_accounts/keychain.py uses, with the
real exit codes: `find-generic-password -s S [-a A] -w`, `-i` reading one
`add-generic-password -U -a A -s S -X HEX` line from stdin, the argv form
`add-generic-password -U -a A -s S -w VALUE`, and `delete-generic-password
-s S`. 44 is errSecItemNotFound.

The file is named by CCM_E2E_KEYCHAIN_FILE. Without it the fake refuses to
run, so nothing can quietly fall through to a real keychain. Every call is
appended to a `.log` beside the file, with secrets redacted. While a file
named `<keychain>.fail-writes` exists, every add fails the way a locked or
refusing keychain does, and reads keep working.
"""
import binascii
import fcntl
import json
import os
import shlex
import sys

NOT_FOUND = 44


def _redact(argv):
    out, skip = [], False
    for word in argv:
        if skip:
            out.append("<secret>")
            skip = False
            continue
        out.append(word)
        skip = word in ("-X", "-w") and argv[0] == "add-generic-password"
    return out


def _run(argv, items):
    """One subcommand against the item table. Returns (rc, stdout)."""
    if not argv:
        return 1, ""
    cmd, args = argv[0], argv[1:]
    # -w takes a value when adding and is a bare flag when finding.
    valued = ("-s", "-a", "-X", "-w") if cmd == "add-generic-password" else ("-s", "-a")
    opts, flags = {}, set()
    i = 0
    while i < len(args):
        a = args[i]
        if a in valued:
            opts[a] = args[i + 1]
            i += 2
        else:
            flags.add(a)
            i += 1
    service = opts.get("-s", "")
    if cmd == "find-generic-password":
        item = items.get(service)
        if item is None or ("-a" in opts and item["account"] != opts["-a"]):
            return NOT_FOUND, ""
        return 0, (item["secret"] + "\n") if "-w" in flags else ""
    if cmd == "add-generic-password":
        if "-X" in opts:
            secret = binascii.unhexlify(opts["-X"]).decode()
        elif "-w" in opts:
            secret = opts["-w"]
        else:
            return 1, ""
        if service in items and "-U" not in flags:
            return 45, ""           # errSecDuplicateItem
        items[service] = {"account": opts.get("-a", ""), "secret": secret}
        return 0, ""
    if cmd == "delete-generic-password":
        if service not in items:
            return NOT_FOUND, ""
        del items[service]
        return 0, ""
    return 1, ""


def main():
    path = os.environ.get("CCM_E2E_KEYCHAIN_FILE")
    if not path:
        sys.stderr.write("fake security: CCM_E2E_KEYCHAIN_FILE is not set\n")
        return 2
    argv = sys.argv[1:]
    stdin_line = ""
    if argv == ["-i"]:
        stdin_line = sys.stdin.readline()
        argv = shlex.split(stdin_line)
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path) as f:
                items = json.load(f)
        except (OSError, ValueError):
            items = {}
        if argv[:1] == ["add-generic-password"] and os.path.exists(path + ".fail-writes"):
            rc, out = 36, ""      # errSecNoSuchKeychain: the write never lands
            sys.stderr.write("security: SecKeychainItemCreateFromContent: "
                             "A keychain cannot be found to store.\n")
        else:
            rc, out = _run(argv, items)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(items, f)
        os.replace(tmp, path)
        with open(path + ".log", "a") as f:
            f.write(json.dumps({"argv": _redact(argv), "via_stdin": bool(stdin_line),
                                "rc": rc}) + "\n")
    sys.stdout.write(out)
    if rc == NOT_FOUND:
        sys.stderr.write("security: SecKeychainSearchCopyNext: The specified item could not "
                         "be found in the keychain.\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
