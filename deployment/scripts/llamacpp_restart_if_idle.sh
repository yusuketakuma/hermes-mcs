#!/bin/bash
# Daily restart only after a valid, idle /slots observation within 15 minutes.
exec /usr/bin/python3 - "__DATA__/llamacpp-restart.log" <<'PY'
import datetime
import json
import os
import selectors
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

LOG = Path(sys.argv[1])
MAX_RESPONSE = 65536


def log(message):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(f"{stamp} {message}\n")


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def busy_slots(deadline):
    """Return a strict busy count, or None when the observation is unknown."""
    request_deadline = min(deadline, time.monotonic() + 5)
    proc = subprocess.Popen(
        ["/usr/bin/curl", "--disable", "--noproxy", "*", "--max-redirs", "0", "--fail",
         "--silent", "--max-time", "5", "http://127.0.0.1:8080/slots"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        data = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while True:
                remaining = request_deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    return None
                chunk = os.read(proc.stdout.fileno(), MAX_RESPONSE + 1 - len(data))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_RESPONSE:
                    return None
        remaining = request_deadline - time.monotonic()
        if remaining <= 0 or proc.wait(timeout=remaining) != 0:
            return None
        slots = json.loads(data, object_pairs_hook=unique_keys)
        if not isinstance(slots, list) or not slots:
            return None
        if any(not isinstance(slot, dict) or type(slot.get("is_processing")) is not bool
               for slot in slots):
            return None
        return sum(slot["is_processing"] for slot in slots)
    except (OSError, ValueError, RecursionError, subprocess.TimeoutExpired):
        return None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()


def main():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    log("checking loaded llamacpp agent before idle wait")
    # Preserve loaded-label precedence; --no-llm needs no slot probe.
    label = ""
    for candidate in ("ai.hermes.llamacpp", "ai.mcs.llamaserver"):
        result = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{candidate}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
        )
        if result.returncode == 0:
            label = candidate
            break
    if not label:
        log("no llamacpp launchd agent loaded — restart skipped")
        return 0

    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        busy = busy_slots(deadline)
        if busy == 0 and time.monotonic() < deadline:
            # The server has no atomic idle-and-restart API; keep this check next to kickstart.
            result = subprocess.run(
                ["/bin/launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
                timeout=5,
            )
            if result.returncode == 0:
                log(f"idle — restarted {label} (kickstart -k)")
            else:
                log(f"kickstart -k {label} FAILED rc={result.returncode}")
                print(f"llamacpp restart: kickstart -k {label} failed (rc={result.returncode})")
            return result.returncode
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(10, remaining))
    log("slots busy or unknown after 15m — restart skipped")
    return 0


try:
    sys.exit(main())
except (OSError, subprocess.TimeoutExpired) as exc:
    with suppress(OSError):
        log(f"restart FAILED ({type(exc).__name__})")
    print(f"llamacpp restart: failed ({type(exc).__name__})")
    sys.exit(1)
PY
