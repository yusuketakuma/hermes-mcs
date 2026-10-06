#!/usr/bin/env python3
"""Copy the MCS password from Keychain into ~/.mcs/.env as
MCS_PASSWORD — the reboot fallback that lets auto_login re-login while
the login keychain is still locked (see mcs_adapter._login_password).
The secret moves keychain→file without touching argv, logs, or stdout.
Requires the keychain to be unlocked once (`security unlock-keychain`
or a GUI login). Re-runnable: _env_write merges without disturbing
other keys."""
import subprocess
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(
    os.path.abspath(__file__)), "..", "mcs"))
import _mcs_path  # noqa: F401  registers every subdir as import root

from mcs_setup import _env_write, ENV_PATH, KEYCHAIN_SERVICE


def main() -> int:
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-w"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        print("keychain read failed — check access and retry", file=sys.stderr)
        return 1
    pw = r.stdout.strip()
    if r.returncode != 0 or not pw:
        print("keychain read failed — unlock first: "
              "security unlock-keychain", file=sys.stderr)
        return 1
    _env_write(ENV_PATH, {"MCS_PASSWORD": pw})
    print(f"MCS_PASSWORD written to {ENV_PATH} (0600)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
