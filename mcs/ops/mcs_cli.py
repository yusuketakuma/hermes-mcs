"""Unified install, update, setup and local doctor entrypoint."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Final

REPO: Final = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "mcs"))
import _mcs_path  # noqa: E402, F401
import mcs_runtime  # noqa: E402
from mcs_util import load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    """Route arguments without shells, retaining downstream contracts."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=("install", "update", "setup", "doctor", "drug"))
    forwarded = list(sys.argv[1:] if argv is None else argv)
    args = ap.parse_args(forwarded[:1])
    rest = forwarded[1:]
    if args.command == "install":
        os.execv("/bin/sh", ["/bin/sh", str(REPO / "install.sh"), *rest])

    # The bootstrap can start on either installed runtime. Configuration
    # selects the one that owns the jobs, never whichever Python is on PATH.
    # Installer shims pin their actual interpreter, including custom Hermes homes.
    if not os.environ.get("MCS_LIFECYCLE_PINNED"):
        try:
            python = mcs_runtime.python_executable(load_config())
        except ValueError:
            python = sys.executable  # doctor must explain an invalid mode
        if os.path.abspath(python) != os.path.abspath(sys.executable):
            if not os.access(python, os.X_OK):
                if args.command != "doctor":
                    print("Selected runtime interpreter missing; run mcs install.",
                          file=sys.stderr)
                    return 1
            else:
                os.execv(python, [python, str(Path(__file__).resolve()),
                                 args.command, *rest])

    match args.command:
        case "drug":
            import mcs_drug
            return mcs_drug.main(rest)
        case "setup":
            import mcs_setup
            # services/check are explicit; init owns its final check and secrets.
            command = (rest[0] if rest and rest[0] in ("init", "services", "check")
                       else "init")
            forwarded = rest[1:] if rest and rest[0] == command else rest
            return mcs_setup.main([command, *forwarded])
        case "doctor":
            import mcs_setup
            return mcs_setup.main(["doctor", *rest])
        case "update":
            update = argparse.ArgumentParser(prog="mcs update")
            update.add_argument("phase", choices=("plan", "apply", "rollback"))
            update.add_argument("--to")
            update.add_argument("--no-fetch", action="store_true")
            update.add_argument("--reinstall", action="store_true")
            update.add_argument("--install-arg", action="append", default=[],
                                choices=("--no-llm", "--no-brew", "--no-plugin",
                                         "--no-recovery"))
            opts = update.parse_args(rest)
            if opts.to and not re.fullmatch(r"v?\d+\.\d+\.\d+", opts.to):
                update.error("--to must be a release tag")
            if opts.phase != "apply" and (opts.reinstall or opts.install_arg):
                update.error("--reinstall/--install-arg require apply")
            if opts.phase == "rollback":
                if opts.to or opts.no_fetch:
                    update.error("rollback does not accept --to/--no-fetch")
                os.execv(sys.executable, [sys.executable,
                         str(REPO / "mcs/ops/mcs_update.py"), "rollback"])
            cmd = [sys.executable, str(REPO / "scripts/mcs_upgrade.py"),
                   "--repo", str(REPO)]
            if opts.phase == "plan" or opts.no_fetch:
                cmd.append("--no-fetch")
            cmd.append(opts.phase)
            if opts.to:
                cmd += ["--to", opts.to]
            if opts.reinstall:
                cmd.append("--reinstall")
            cmd += [f"--install-arg={value}" for value in opts.install_arg]
            if opts.phase == "plan":
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True,
                                            timeout=600)
                except subprocess.TimeoutExpired:
                    print("Update plan timed out; check network and retry.",
                          file=sys.stderr)
                    return 1
                print(result.stdout, end="")
                print(result.stderr, end="", file=sys.stderr)
                if result.returncode:
                    return result.returncode
                try:
                    report = json.loads(result.stdout)
                    if not isinstance(report, dict):
                        raise ValueError
                except (ValueError, RecursionError):
                    print("Update plan did not return JSON.", file=sys.stderr)
                    return 1
                return 1 if report.get("route") == "blocked" else 0
            os.execv(sys.executable, cmd)
        case _:
            ap.error("unsupported command")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
