"""Send sealed notification chunks through the existing configured CLI without ledger access."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import mcs_runtime
import mcs_util

MAX_FILE_BYTES = 24 * 1024 * 1024


SEND_MIN_BUDGET_S = 30


SYSTEM_ALERT_KINDS = frozenset({
    "session_expired", "session_recovered", "run_failed", "update_notice",
    "health_write_failed",
})


def _channel_target(value, cfg):
    transport, _, channel = value.partition(':')
    if transport == 'slack' and re.fullmatch(r'[CG][A-Z0-9]{8,}', channel):
        return transport, channel
    if transport == 'discord' and channel.isdecimal():
        try:
            return transport, str(int(channel))
        except ValueError:
            return None
    if transport == 'lineworks' and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}', channel):
        notify = cfg.get('notify')
        scope = notify.get('lineworks') if isinstance(notify, dict) else None
        if isinstance(scope, dict) and channel == scope.get('channel_id'):
            return transport, channel
    return None


def target(cfg: dict, kind: str) -> str | None:
    """Delivery target in `hermes send --to` syntax. Patient-content
    events ONLY go to the explicitly configured target — a missing
    config must never spill bodies into a fallback destination
    (Oracle B15). Slack system alerts require a separate configured destination."""
    t = cfg.get("notify_target")
    if kind in SYSTEM_ALERT_KINDS:
        st = cfg.get("notify_system_target")
        st = st.strip() if isinstance(st, str) else ''
        patient = t.strip() if isinstance(t, str) else ''
        patients = [patient]
        notify = cfg.get('notify')
        if isinstance(notify, dict) and notify.get('interactive') in ('slack', 'discord', 'lineworks'):
            transport = notify['interactive']
            scope = notify.get(transport)
            channel = scope.get('channel_id') if isinstance(scope, dict) else None
            patients.append(transport + ':' + (channel.strip() if isinstance(channel, str) else ''))
        if any(p.partition(':')[0] == 'slack' for p in patients) and st != 'local':
            # Keep typed separation in sync with the checkout-independent recovery copy.
            system = _channel_target(st, cfg)
            if (system is None or any(
                    p.partition(':')[0] == system[0]
                    and _channel_target(p, cfg) in (None, system)
                    for p in patients)):
                return None
        if st:
            t = st
    return t.strip() if isinstance(t, str) and t.strip() else None


def hermes_free(cfg: dict, target: str | None) -> bool:
    """Targets delivered by MCS's own sender instead of `hermes send`."""
    return mcs_runtime.standalone(cfg) or (target or "").startswith("lineworks:")


def send_argv(cfg: dict, target: str, config_path: str) -> list[str]:
    """`hermes [-p profile] send --to <target> --quiet` argv, or the
    independent sender's equivalent (same sealed stdin contract).
    config_path is the caller-owned path identity, passed without reading it."""
    if hermes_free(cfg, target):
        lineworks = target.startswith("lineworks:")
        package = "lineworks_adapter" if lineworks else "mcs_standalone"
        entry = Path(__file__).resolve().parents[2] / package / "__main__.py"
        # the Slack/Discord SDKs exist only in the standalone venv — a
        # manual run on another Python must still send through it
        python = mcs_runtime.python_executable(cfg, root=Path(config_path).parent)
        if lineworks or not os.access(python, os.X_OK):
            python = sys.executable
        return [python, str(entry), "send", "--root",
                str(Path(config_path).parent), "--to", target, "--quiet"]
    argv = [mcs_util.hermes_exe(cfg)]
    profile = cfg.get("notify_bot_profile")
    if isinstance(profile, str) and re.fullmatch(r"[a-z0-9_-]+", profile):
        argv += ["-p", profile]
    argv += ["send", "--to", target, "--quiet"]
    return argv


class SendFailed(OSError):
    """Delivery did not begin; the outbox may safely retry.

    ``code`` is the direct sender's final stderr token (empty when the
    failure was local); it only selects the hold policy, never whether
    delivery may have begun."""

    code = ""


class SendBudget(SendFailed):
    """Insufficient send time; defer without consuming an attempt."""


def send_budget(deadline: float | None) -> float:
    """Check before reserving in-flight and again before starting transport."""
    remain = 180 if deadline is None else deadline - time.monotonic()
    if remain < SEND_MIN_BUDGET_S:
        raise SendBudget("send_budget_insufficient")
    return min(180, remain)


class SendUncertain(OSError):
    """Delivery began but acceptance is unknown; never retry automatically."""


class SendUsage(Exception):
    """`hermes send` refused the invocation itself (exit 2) — a CLI/
    config contract problem that retrying cannot fix → outbox_hold."""


def _media_path(name: str, path: str) -> str:
    """MEDIA: delivery path for a stored attachment. Downloaded files are
    saved extensionless (attachments/<id>); platforms derive the upload
    filename from the path basename, so a bare id posts as an
    extensionless blob Discord won't render inline. Alias the file to
    `<path><ext>` (hardlink, copy fallback) carrying the original
    extension so the upload keeps a renderable filename."""
    ext = os.path.splitext(name)[1].lower() if name else ""
    if (not ext or not re.fullmatch(r"\.[a-z0-9]{1,8}", ext)
            or os.path.splitext(path)[1]):
        return path
    alias = path + ext
    try:
        if (not os.path.islink(alias) and os.path.exists(alias)
                and os.path.samefile(path, alias)):
            return alias
        # A re-download atomically replaces the source inode. Reusing its
        # old alias would bypass the source's hash check and upload stale
        # bytes; replacing the alias also avoids following a planted link.
        with tempfile.TemporaryDirectory(
                prefix=".media-", dir=os.path.dirname(os.path.abspath(path))) as work:
            prepared = os.path.join(work, "attachment")
            try:
                os.link(path, prepared)
            except OSError:
                shutil.copy2(path, prepared)
            os.replace(prepared, alias)
    except OSError:
        return path
    return alias


# `hermes send` parses MEDIA:/voice/document directives from the WHOLE
# stdin stream — untrusted content (post bodies, sender names, LLM
# output) must never form one, or a crafted post could attach any
# readable local file to the notification. The keyword is defused to a
# fullwidth form (visible, non-parsing); the [[...]] directives get
# single brackets. Verified attachments are appended afterwards as
# genuine directives — see notify_sender._compose_body.


_CONTROL_TAG_RE = re.compile(r"(?i)([`\"'*_]{0,3})MEDIA:")


_DOUBLE_BRACKET_RE = re.compile(
    r"\[\[(as_document|audio_as_voice)\]\]", re.IGNORECASE)


def _defuse_control_syntax(text: str) -> str:
    text = _CONTROL_TAG_RE.sub(r"\1MEDIA：", text)
    return _DOUBLE_BRACKET_RE.sub(r"[\1]", text)


def _compose_body(content: str,
                  files: list[tuple[str, str]] | None) -> str:
    # Hermes parses mentions/links in every field, including file followups.
    body = (_defuse_control_syntax(content).replace("<", "＜").replace(">", "＞")
            .replace("@", "＠").replace("](", "］("))
    if files:
        body += "".join(f"\nMEDIA:{_media_path(fn, path)}"
                        for fn, path in files)
    return body


# entry points that post directly (no hermes CLI between MCS and the platform)
_DIRECT_ENTRIES = ("/lineworks_adapter/__main__.py", "/mcs_standalone/__main__.py")


def send(argv: list[str], content: str,
          files: list[tuple[str, str]] | None = None,
          deadline: float | None = None,
          attachment_pins: dict[str, str] | None = None) -> None:
    """One chunk via `hermes send` (body on stdin; attachments as MEDIA:
    references — the adapter owns upload limits and mention policy)."""
    direct = len(argv) > 1 and argv[1].endswith(_DIRECT_ENTRIES)
    if direct:
        sealed = []
        for name, path in files or []:
            size = 0
            hashed = hashlib.sha256()
            try:
                with open(path, "rb") as handle:
                    while size <= MAX_FILE_BYTES:
                        block = handle.read(min(1 << 20, MAX_FILE_BYTES + 1 - size))
                        if not block:
                            break
                        size += len(block)
                        hashed.update(block)
            except OSError:
                raise SendFailed("attachment_unavailable") from None
            if size > MAX_FILE_BYTES:
                raise SendFailed("attachment_too_large")
            digest = hashed.hexdigest()
            if attachment_pins is not None and attachment_pins.get(path) != digest:
                raise SendFailed("attachment_mismatch")
            sealed.append({"name": name, "path": path, "bytes": size,
                           "sha256": digest})
        body = json.dumps({"text": content, "files": sealed}, ensure_ascii=False)
    else:
        body = _compose_body(content, files)
    timeout = send_budget(deadline)
    try:
        r = subprocess.run(argv, input=body, text=True,
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise SendUncertain("hermes send timed out") from e
    except OSError as e:
        raise SendFailed("hermes send could not start") from e
    detail = (r.stderr or r.stdout or "").strip()[:400]
    if r.returncode == 2:
        raise SendUsage(detail or "hermes send usage error")
    if r.returncode == 75 and direct:
        # EX_TEMPFAIL from a direct sender: the platform provably
        # accepted nothing (failed before the post, or a definitive reject).
        exc = SendFailed(detail or "send not accepted")
        # The sender prints its fixed code last; earlier lines may be logs.
        lines = (r.stderr or "").strip().splitlines()
        words = lines[-1].split() if lines else []
        exc.code = words[-1] if words and len(words[-1]) <= 64 else ""
        raise exc
    if r.returncode != 0:
        # A failed child can have delivered text or some attachments
        # before losing its response. Its exit status is not a negative ACK.
        raise SendUncertain(detail or f"hermes send exit {r.returncode}")


def delivery_fingerprint(target: str, chunks: list[str],
                          files: list[tuple[str, str]]) -> str:
    h = hashlib.sha256(target.encode())
    for chunk in chunks:
        h.update(b"\0text\0")
        h.update(chunk.encode())
    for name, path in files:
        h.update(b"\0file\0")
        h.update(name.encode())
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block)
    return h.hexdigest()
