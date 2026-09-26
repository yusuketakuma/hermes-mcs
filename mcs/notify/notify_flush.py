"""Outbox notifier — drains notify_outbox through `hermes send`.

Delivery is delegated to Hermes's standard send path: the destination
comes from ~/.mcs/config.json {notify_target} in `hermes send --to`
syntax (e.g. "slack", "slack:#mcs", "discord:1234"), and Hermes owns
platform connection, credentials, channel resolution and mentions
policy. When config sets "notify_bot_profile" (e.g. "cco"), sends run
under that Hermes profile so posts arrive under that bot's identity.
Attachments ride as MEDIA:<path> references in the message text; the
platform adapter owns upload limits.

Events carry message_ids; message content is looked up in the local
ledger at send time so the outbox payload itself stays tiny.

The send-time semantic policy gates (mode/generation re-checks, the
audited-summary block, provenance-preserving semantic chunking) live in
semantic_send_gate; this module keeps thin delegates so flush() stays
the single delivery driver.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

import mcs_signals
import semantic_send_gate
import structured_view
from mcs_util import html_to_text, load_config
# Re-exported send-gate verdicts — the canonical definitions live in
# semantic_send_gate (the send-time semantic policy layer); the legacy
# private names stay so existing tests and flush() catches are stable.
from semantic_send_gate import (
    DeferredSend as _DeferredSend,
    FreezeSend as _FreezeSend,
    StaleSend as _StaleSend,
)

CONF_PATH = os.path.expanduser("~/.mcs/config.json")
_MAX_LEN = 1900
# Per-notification file caps; oversized files stay local-only and are
# noted in the text instead of failing the whole send.
_MAX_FILES = 10
_MAX_FILE_BYTES = 24 * 1024 * 1024
_MAX_FILES_BYTES = 23 * 1024 * 1024


def _config() -> dict:
    return load_config(CONF_PATH)


def _hermes_exe(cfg: dict) -> str:
    """`hermes` binary for delivery. config {hermes_bin} overrides; the
    default falls back to the standard user-local install because the
    launchd PATH is minimal."""
    exe = cfg.get("hermes_bin")
    if isinstance(exe, str) and exe.strip():
        return exe.strip()
    return (shutil.which("hermes")
            or os.path.expanduser("~/.local/bin/hermes"))


def _target(cfg: dict, kind: str) -> str | None:
    """Delivery target in `hermes send --to` syntax. Patient-content
    events ONLY go to the explicitly configured target — a missing
    config must never spill bodies into a fallback destination
    (Oracle B15). System alerts may override via notify_system_target."""
    t = cfg.get("notify_target")
    if kind in ("session_expired", "run_failed", "update_notice"):
        st = cfg.get("notify_system_target")
        if isinstance(st, str) and st.strip():
            t = st
    return t.strip() if isinstance(t, str) and t.strip() else None


def _send_argv(cfg: dict, target: str) -> list[str]:
    """`hermes [-p profile] send --to <target> --quiet` argv."""
    argv = [_hermes_exe(cfg)]
    profile = cfg.get("notify_bot_profile")
    if isinstance(profile, str) and re.fullmatch(r"[a-z0-9_-]+", profile):
        argv += ["-p", profile]
    argv += ["send", "--to", target, "--quiet"]
    return argv


def _urgency(ledger, mid: int) -> str | None:
    for kind in ("extract_llm", "extract_v1"):
        d = structured_view.latest_artifact(ledger.db, kind, mid)
        if d and d.get("urgency") == "high":
            return "high"
    return None


def _attachments_map(ledger, mids: list[int]) -> dict:
    """message_id -> attachment rows (all states) for 📎 markers + files."""
    if not mids:
        return {}
    q = ("SELECT message_id,file_id,name,state,local_path,bytes,sha256 "
         f"FROM attachments WHERE message_id IN ({','.join('?' * len(mids))}) "
         "AND state != 'withdrawn' ORDER BY attachment_id")
    out: dict[int, list] = {}
    for r in ledger.db.execute(q, mids):
        out.setdefault(r["message_id"], []).append(r)
    return out


def _collect_files(att_map: dict, order: list[int]) -> list[tuple[str, str]]:
    """Downloaded attachment (filename, local_path) pairs for upload —
    message order, max _MAX_FILES, each <= _MAX_FILE_BYTES."""
    files = []
    total = 0
    for mid in order:
        for a in att_map.get(mid, []):
            if len(files) >= _MAX_FILES:
                return files
            if a["state"] != "downloaded" or not a["local_path"]:
                continue
            if not os.path.exists(a["local_path"]):
                continue
            try:
                size = os.path.getsize(a["local_path"])
            except OSError:
                continue
            if size > _MAX_FILE_BYTES or total + size > _MAX_FILES_BYTES:
                continue
            if not a["sha256"]:
                continue
            h = hashlib.sha256()
            try:
                with open(a["local_path"], "rb") as f:
                    for block in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(block)
            except OSError:
                continue
            if h.hexdigest() != a["sha256"]:
                continue
            fname = os.path.basename(str(a["name"] or a["file_id"] or "file"))
            fname = re.sub(r'["\\\r\n]', "_", fname) or "file"
            files.append((fname[:150], a["local_path"]))
            total += size
    return files


def _signal_text(ledger, payload: dict, latest: dict):
    """Render a review-candidate signal notice for delivery.

    The outbox payload keeps its frozen shape (ids + fixed note, never
    message bodies). At send time we resolve display context from the
    ledger: the patient's name into the location line, and the latest
    triggering mention (sender/time/snippet) as a quote so the notice
    says WHO and WHAT without a manual lookup. Degrades silently — a
    missing patient row or a deleted message never fails the send.
    """
    text = payload.get("text")
    if not isinstance(text, str) or not text:
        return text
    lines = text.split("\n")
    pid = payload.get("project_id")
    if type(pid) is int and len(lines) > 1:
        r = ledger.db.execute(
            "SELECT patient_name FROM patients WHERE project_id=?",
            (pid,)).fetchone()
        name = r["patient_name"].strip() if r and r["patient_name"] else ""
        if name:
            lines[1] = f"{name}（{lines[1]}）"
        ev = latest.get("evidence")
        mids = (ev.get("message_ids") if isinstance(ev, dict)
                else None) or []
        mid = (mids[-1] if mids and type(mids[-1]) is int
               else (ev.get("discharge_message_id")
                     if isinstance(ev, dict) else None))
        if type(mid) is int:
            m = ledger.db.execute(
                "SELECT sender_name, posted_at, body_text FROM messages"
                " WHERE message_id=? AND body_state IS NOT 'deleted'",
                (mid,)).fetchone()
            if m and m["body_text"]:
                snippet = " ".join(str(m["body_text"]).split())[:120]
                lines.append(
                    f"最新言及 {m['posted_at'] or '?'} "
                    f"{m['sender_name'] or '?'}: {snippet}")
        lines.append(f"確認: /mcs {{\"op\":\"timeline\",\"project_id\":{pid}}}")
    return "\n".join(lines)


def _signal_unit_text(ledger, sigs: list[dict]):
    """Render ONE review unit for delivery — a single signal, or a
    merged same-post med_change group. Display context (patient name,
    latest-mention quote, timeline link) is resolved at send time."""
    latest = sigs[0]
    merged = (mcs_signals.med_followup_group_notice(sigs)
              if len(sigs) > 1 else None)
    if len(sigs) > 1 and merged is None:
        # a unit that cannot render merged must still show EVERY
        # member — never silently collapse to the first signal
        return "\n\n".join(_signal_unit_text(ledger, [s])
                           for s in sigs)
    sig_view = latest
    if merged is not None:
        mids = sorted({m for s in sigs
                       for m in ((s.get("evidence") or {})
                                 .get("message_ids") or [])
                       if type(m) is int})
        sig_view = {"evidence": {"message_ids": mids}}
    return _signal_text(
        ledger,
        {"text": merged or mcs_signals.signal_notice_text(latest),
         "project_id": latest.get("project_id")},
        sig_view)


def _format_event(ledger, ev) -> tuple[str, list[tuple[str, str]]]:
    """Returns (content, files). files = [(filename, local_path)] to upload."""
    try:
        payload = json.loads(ev["payload"])
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError("payload_invalid") from e
    if not isinstance(payload, dict):
        raise ValueError("payload_invalid")
    if ev["kind"] == "session_expired":
        return ("[MCS] セッション失効 — 手動再ログインが必要です\n"
                f"run {payload.get('run_id')}: {payload.get('detail','')}"), []
    if ev["kind"] == "run_failed":
        return ("[MCS] チェック失敗 — アダプタを確認してください\n"
                f"run {payload.get('run_id')}: {payload.get('detail','')}"), []
    if ev["kind"] == "update_notice":
        # Frozen text like semantic_notice — sanitized at enqueue time
        # (mentions defused), the sender just relays it.
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("payload_invalid")
        return text, []
    if ev["kind"] == "semantic_notice":
        # audited/degraded semantic notice — the payload carries the
        # final text frozen at enqueue time; the sender never re-derives
        # it (INV-14). Two gates are re-checked at the last moment
        # (§18.4/§22.1): mode must still be enforce, and the audited
        # notice's input fingerprint must still be live — a degraded
        # notice is instead suppressed if its base event has since
        # delivered.
        _semantic_gate(ledger, ev, payload)
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("payload_invalid")
        return text, []
    if ev["kind"] == "signal":
        # Review-candidate notice: resolve the current explanation/evidence.
        # Last-moment gates like semantic_notice: the flag may have been
        # turned off, or the signal may have resolved while queued —
        # both are terminal drops, not retries.
        sig_cfg = _config().get("signals")
        if not (isinstance(sig_cfg, dict)
                and sig_cfg.get("notify") is True):
            raise _StaleSend("signals_notify_disabled")
        # Member keys: a merged same-post med intent carries
        # signal_keys[]; a legacy/single intent carries signal_key.
        skeys = payload.get("signal_keys")
        keys = ([k for k in skeys if type(k) is str and k]
                if isinstance(skeys, list) else [])
        skey = payload.get("signal_key")
        if not keys and type(skey) is str and skey:
            keys = [skey]
        # a member that resolved while queued drops out of a merged
        # notice; only a fully-resolved group cancels the send. Digest
        # payloads carry project_id=None, so the per-event archived
        # gate in flush() can't see them — members of an archived
        # patient drop out here instead.
        open_sigs = [s for _, s in mcs_signals.open_signal_rows(
            ledger.db, keys)
            if not (s.get("project_id")
                    and ledger.is_archived(s["project_id"]))]
        if not open_sigs:
            raise _StaleSend("signal_not_open")
        # Rebuild text at send time: notes can change while evidence
        # IDs stay the same, a digest re-groups its live members, and a
        # merged med notice shrinks to whoever is still open.
        if payload.get("digest") is True:
            units = mcs_signals.sig_units(
                [(mcs_signals.med_group_key(s), s) for s in open_sigs])
            parts = [_signal_unit_text(ledger, us) for us in units]
            text = (f"[MCS] レビュー候補ダイジェスト"
                    f"（{len(open_sigs)}件）\n\n" + "\n\n".join(parts))
        else:
            text = _signal_unit_text(ledger, open_sigs)
            if payload.get("urgent") is True:
                head, _, tail = text.partition("\n")
                text = f"{head} — 原投稿が urgency:high" \
                       + (f"\n{tail}" if tail else "")
        if not isinstance(text, str) or not text:
            raise ValueError("payload_invalid")
        return text, []
    if ev["kind"] == "attachment_followup":
        # a body notice went out before this file downloaded — deliver
        # just the file now (F11). If the file is no longer sendable
        # (pruned/withdrawn/never finished) the intent is terminal.
        aid = payload.get("attachment_id")
        if type(aid) is not int:
            raise ValueError("payload_invalid")
        a = ledger.db.execute(
            "SELECT a.message_id,a.file_id,a.name,a.state,a.local_path,a.bytes,a.sha256"
            " FROM attachments a JOIN messages m ON m.message_id=a.message_id"
            " WHERE a.attachment_id=? AND m.body_state IS NOT 'deleted'",
            (aid,)).fetchone()
        if not a or a["state"] != "downloaded" or not a["local_path"]:
            raise _StaleSend("attachment_not_ready")
        files = _collect_files({a["message_id"]: [a]}, [a["message_id"]])
        if not files:
            raise _StaleSend("attachment_unsendable")
        return (f"[MCS] 添付ファイル（後送）\n{a['name'] or 'file'}"), files
    ids = payload.get("message_ids") or []
    if (not isinstance(ids, list)
            or any(type(mid) is not int or mid <= 0 for mid in ids)):
        raise ValueError("payload_invalid")
    rows = []
    for mid in ids:
        r = ledger.db.execute("""
          SELECT m.*, p.patient_name FROM messages m
          LEFT JOIN patients p ON p.project_id = m.project_id
          WHERE m.message_id=? AND m.body_state IS NOT 'deleted'""",
            (mid,)).fetchone()
        if r:
            rows.append(r)
    src = payload.get("source", "unread")
    if not rows:
        raise _StaleSend("messages_unavailable")
    att_map = _attachments_map(ledger, [r["message_id"] for r in rows])
    # group replies under their parent when both are new in this event
    by_id = {r["message_id"]: r for r in rows}
    parents = [r for r in rows if not r["parent_id"]
               or r["parent_id"] not in by_id]
    kids = {}
    for r in rows:
        if r["parent_id"] and r["parent_id"] in by_id:
            kids.setdefault(r["parent_id"], []).append(r)

    def _fmt(r, indent=""):
        s_lines = structured_view.structured_lines(
            ledger.db, r["message_id"])
        urg = _urgency(ledger, r["message_id"])
        body = html_to_text(r["body_html"])
        cap = 500 if s_lines else 600
        if len(body) > cap:
            body = body[:cap] + "…"
        who = r["sender_name"] or "不明"
        meta = " / ".join(x for x in (r["profession"], r["organization"]) if x)
        parent = " (返信)" if r["parent_id"] and not indent else ""
        state = "" if r["body_state"] == "full" else f" [{r['body_state']}]"
        warn = " ⚠️" if urg == "high" else ""
        head = (f"{indent}**{r['patient_name'] or ev['project_id']}**"
                f"{parent}{state}{warn}\n"
                f"{indent}{who}{f' ({meta})' if meta else ''} — "
                f"{r['posted_at'][:16]}")
        atts = att_map.get(r["message_id"], [])
        att_line = ""
        if atts:
            marks = []
            for a in atts:
                nm = str(a["name"] or a["file_id"] or "file")
                if a["state"] == "downloaded":
                    if (a["bytes"] or 0) > _MAX_FILE_BYTES:
                        nm += " (25MB超·未送信)"
                else:
                    nm += " (未取得)"
                marks.append(nm)
            att_line = f"\n{indent}📎 {'、'.join(marks[:5])}"
        if s_lines:
            struct = "\n".join(f"{indent}・{ln}" for ln in s_lines)
            return (f"{head}\n{indent}📋 構造化\n{struct}\n"
                    f"{indent}───── 原文 ─────\n"
                    f"{indent}{body or '(本文なし)'}{att_line}")
        return f"{head}\n{indent}{body or '(本文なし)'}{att_line}"

    def _sem_block(r):
        """Audited summary section — the enforce-mode policy lives in
        semantic_send_gate.semantic_summary_block."""
        try:
            return semantic_send_gate.semantic_summary_block(
                ledger, r, _config())
        except Exception:
            return ""   # never let a summary render break delivery

    out = []
    order = []
    for r in parents:
        out.append(_fmt(r) + _sem_block(r))
        order.append(r["message_id"])
        for k in sorted(kids.get(r["message_id"], []),
                        key=lambda x: x["posted_at"]):
            out.append(_fmt(k, "↳ "))
            order.append(k["message_id"])
    head = f"[MCS {src}] 新着 {len(rows)} 件"
    return (head + "\n\n" + "\n\n".join(out),
            _collect_files(att_map, order))


# --- send-time semantic gates ------------------------------------------
# The policy layer lives in semantic_send_gate. These thin delegates keep
# the historic private names that tests monkeypatch and that flush()/
# _format_event() call; each delegate resolves this module's _config() so
# a patched config still reaches the gate (semantic_send_gate never
# imports back into this module — values come in as arguments).


def _semantic_gate(ledger, ev, payload: dict, in_progress: bool = False):
    """Re-export — implementation: semantic_send_gate.semantic_gate."""
    return semantic_send_gate.semantic_gate(
        ledger, ev, payload, _config(), in_progress=in_progress)


def _semantic_render_state(ledger, ev) -> tuple:
    """Re-export — implementation: semantic_send_gate.semantic_render_state."""
    return semantic_send_gate.semantic_render_state(ledger, ev, _config())


def _semantic_render_gate(ledger, ev, initial: tuple,
                          in_progress: bool = False):
    """Re-export — implementation: semantic_send_gate.semantic_render_gate."""
    return semantic_send_gate.semantic_render_gate(
        ledger, ev, initial, _config(), in_progress=in_progress)


def _semantic_chunks(content: str) -> list[str]:
    """Re-export — implementation: semantic_send_gate.semantic_chunks."""
    return semantic_send_gate.semantic_chunks(content, _MAX_LEN)


def _has_sent_progress(ev) -> bool:
    try:
        progress = json.loads(ev["progress"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    return (isinstance(progress, dict)
            and type(progress.get("next")) is int and progress["next"] > 0)


def _send_never_began(ev) -> bool:
    """No delivery could have happened: the progress receipt is absent
    entirely (the pre-send marker write precedes any subprocess) or it
    records no accepted AND no in-flight chunk. An UNPARSEABLE receipt
    is ambiguous — a corrupted record of a real send — so it fails
    safe (False)."""
    raw = ev["progress"]
    if not raw:
        return True
    try:
        progress = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(progress, dict):
        return False
    return (not progress.get("next") and progress.get("sending") is None
            and not progress.get("sent"))


def _hold_event(ledger, ev, cfg, proven_undelivered=False):
    """Quarantine an event; for an unsent digest payload, first move
    its still-open member keys into a fresh scheduled digest — a held
    intent stops covering its keys but nothing else would ever
    re-enqueue them (signals notify only at the open transition), so
    without this the members would go permanently silent. Salvage runs
    only when non-delivery is provable: an empty/no-send receipt, or a
    send path that refused before posting (proven_undelivered)."""
    # flush's row predates this attempt. Its current receipt may now
    # contain accepted chunks or an unresolved in-flight send.
    ev = ledger.db.execute("SELECT * FROM notify_outbox WHERE event_id=?",
                           (ev["event_id"],)).fetchone()
    if ev is None:
        return
    if (proven_undelivered or _send_never_began(ev)) \
            and not _has_sent_progress(ev):
        try:
            payload = json.loads(ev["payload"])
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict):
            rescuable = (
                payload.get("digest") is True
                or payload.get("signal_keys")
                or payload.get("signal_key")
                or (ev["kind"] == "new_messages"
                    and payload.get("rescue_of") is None
                    and any(type(m) is int
                            for m in (payload.get("message_ids") or []))))
            if rescuable:
                sc = cfg.get("signals")
                ih = sc.get("digest_interval_h") \
                    if isinstance(sc, dict) else None
                interval_h = (ih if type(ih) in (int, float) and ih > 0
                              else mcs_signals.DIGEST_INTERVAL_H)
                # rescue + hold in ONE commit — a crash between them
                # would leave both the old intent and the rescue live,
                # sending the same members twice
                with ledger.db:
                    if ev["kind"] == "new_messages":
                        # a held new_messages intent strands its ids the
                        # same way — notified_at was already consumed, so
                        # nothing can ever re-announce them. Proven
                        # undelivered, so re-enqueue the same id set —
                        # single-shot: a rescued intent that also
                        # quarantines does not respawn (rescue_of marks
                        # the lineage)
                        mids = [m for m in (payload.get("message_ids")
                                            or []) if type(m) is int]
                        ledger.outbox_add_tx(
                            "new_messages", ev["project_id"],
                            {"message_ids": mids,
                             "source": payload.get("source"),
                             "rescue_of": ev["event_id"]})
                    else:
                        mcs_signals.rescue_digest_members(
                            ledger, payload, time.time(), interval_h,
                            origin_id=ev["event_id"])
                    ledger.db.execute(
                        "UPDATE notify_outbox SET state='failed',"
                        "next_try=NULL,updated_at=? WHERE event_id=?",
                        (time.time(), ev["event_id"]))
                return
    ledger.outbox_hold(ev["event_id"])


class _SendFailed(OSError):
    """Delivery did not begin; the outbox may safely retry."""


class _SendUncertain(OSError):
    """Delivery began but acceptance is unknown; never retry automatically."""


class _SendUsage(Exception):
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
# genuine directives — see _compose_body.
_CONTROL_TAG_RE = re.compile(r"(?i)([`\"'*_]{0,3})MEDIA:")
_DOUBLE_BRACKET_RE = re.compile(
    r"\[\[(as_document|audio_as_voice)\]\]", re.IGNORECASE)


def _defuse_control_syntax(text: str) -> str:
    text = _CONTROL_TAG_RE.sub(r"\1MEDIA：", text)
    return _DOUBLE_BRACKET_RE.sub(r"[\1]", text)


def _compose_body(content: str,
                  files: list[tuple[str, str]] | None) -> str:
    body = _defuse_control_syntax(content)
    if files:
        body += "".join(f"\nMEDIA:{_media_path(fn, path)}"
                        for fn, path in files)
    return body


def _send(argv: list[str], content: str,
          files: list[tuple[str, str]] | None = None,
          deadline: float | None = None) -> None:
    """One chunk via `hermes send` (body on stdin; attachments as MEDIA:
    references — the adapter owns upload limits and mention policy)."""
    body = _compose_body(content, files)
    timeout = 180
    if deadline is not None:
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise _SendFailed("deadline_exceeded")
        timeout = min(timeout, remain)
    try:
        r = subprocess.run(argv, input=body, text=True,
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise _SendUncertain("hermes send timed out") from e
    except OSError as e:
        raise _SendFailed("hermes send could not start") from e
    detail = (r.stderr or r.stdout or "").strip()[:400]
    if r.returncode == 2:
        raise _SendUsage(detail or "hermes send usage error")
    if r.returncode != 0:
        # A failed child can have delivered text or some attachments
        # before losing its response. Its exit status is not a negative ACK.
        raise _SendUncertain(detail or f"hermes send exit {r.returncode}")


def _delivery_fingerprint(target: str, chunks: list[str],
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


def _send_marked(ledger, ev, i: int, sent_ids: list[str],
                 fingerprint: str, argv: list[str], chunk: str,
                 files: list[tuple[str, str]] | None,
                 deadline: float | None) -> None:
    """Retain the in-flight marker unless non-delivery is established."""
    ledger.outbox_progress(ev["event_id"], i, list(sent_ids),
                           fingerprint, i + 1)
    try:
        _send(argv, chunk, files, deadline=deadline)
    except (_SendFailed, _SendUsage):
        ledger.outbox_progress(ev["event_id"], len(sent_ids),
                               list(sent_ids), fingerprint)
        raise


def _progress(raw: str, count: int) -> tuple[int, list[str], str | None,
                                            int | None]:
    try:
        d = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        raise ValueError("progress_invalid") from e
    if not isinstance(d, dict) or type(d.get("next", 0)) is not int:
        raise ValueError("progress_invalid")
    next_chunk = d.get("next", 0)
    sent = d.get("sent", [])
    fingerprint = d.get("fingerprint")
    sending = d.get("sending")
    if (next_chunk < 0 or next_chunk > count or not isinstance(sent, list)
            or len(sent) != next_chunk
            or any(not isinstance(x, str) or not x.isdigit() for x in sent)
            or (fingerprint is not None and not isinstance(fingerprint, str))
            or (sending is not None and type(sending) is not int)):
        raise ValueError("progress_invalid")
    return next_chunk, sent, fingerprint, sending


def _route(ev) -> str:
    """Row/dict-safe route read — a fake outbox in tests (or an
    un-migrated caller) that lacks the column is a text event."""
    try:
        return ev["route"] or "text"
    except (KeyError, IndexError):
        return "text"


def _dispatch_interactive(ledger, ev, cfg, res) -> bool:
    """Card-route intent. Sealed batches stay card-owned even with the
    switch off — a text send here could double-deliver, and begins are
    denied until the switch returns. The kill switch converts an
    UNSEALED intent once (provably unsent) so it can flush as plain
    text; returns False in that case so the caller falls through."""
    import notify_cards
    sealed = ledger.db.execute(
        "SELECT 1 FROM notification_intent_batches "
        "WHERE event_id=?", (ev["event_id"],)).fetchone()
    if not sealed and not notify_cards.interactive_enabled(cfg):
        notify_cards.revert_to_text(ledger, ev["event_id"])
        return False
    try:
        outcome = notify_cards.dispatch_intent(ledger, ev, cfg)
        if outcome.get("error"):
            ledger.outbox_mark(ev["event_id"], "failed", retry_in=3600)
            res["failed"] += 1
        elif outcome.get("suppressed"):
            res["suppressed"] += 1
        elif not outcome.get("skipped"):
            res["dispatched"] = res.get("dispatched", 0) + 1
    except Exception:
        if ev["attempts"] >= 4:
            ledger.outbox_hold(ev["event_id"])
        else:
            ledger.outbox_mark(ev["event_id"], "failed", retry_in=3600)
        res["failed"] += 1
    return True


def _send_text(ledger, ev, cfg, argv, target, res, deadline) -> bool:
    """Format, chunk and send one text event — per-chunk progress is
    journaled so a crash mid-event resumes at the first unacknowledged
    chunk rather than duplicating accepted posts. Returns False when
    the deadline cut the chunk loop short (caller ends the flush)."""
    content, files = _format_event(ledger, ev)
    render_state = (_semantic_render_state(ledger, ev)
                    if ev["kind"] == "new_messages" else ())
    chunks = (_semantic_chunks(content)
              if ev["kind"] == "semantic_notice" else
              [content[i:i + _MAX_LEN]
               for i in range(0, len(content), _MAX_LEN)])
    # resume at the first unacknowledged chunk — a crash after a
    # partial send must not duplicate accepted chunks (B25)
    start, sent_ids, previous, sending = _progress(
        ev["progress"], len(chunks))
    if sending is not None and sending > start:
        # a previous attempt began chunk `sending` and died before
        # recording the ack — delivery is UNCERTAIN: resending
        # could duplicate a post that did go out. Hold it for
        # human reconciliation instead (F19)
        _hold_event(ledger, ev, cfg)
        res["uncertain"] = res.get("uncertain", 0) + 1
        return True
    fingerprint = _delivery_fingerprint(target, chunks, files)
    if start and previous != fingerprint:
        _hold_event(ledger, ev, cfg)
        res["failed"] += 1
        return True
    if not start:
        ledger.outbox_progress(ev["event_id"], 0, [], fingerprint)
    post_files = files
    for i in range(start, len(chunks)):
        if deadline is not None and time.monotonic() >= deadline:
            return False
        if ev["kind"] == "semantic_notice":
            try:
                payload = json.loads(ev["payload"])
            except (json.JSONDecodeError, TypeError) as e:
                raise ValueError("payload_invalid") from e
            _semantic_gate(ledger, ev, payload,
                           in_progress=bool(start or i))
        elif render_state:
            _semantic_render_gate(ledger, ev, render_state,
                                  in_progress=bool(start or i))
        # files ride the FIRST post only; on resume (start>0) they
        # were already delivered with chunk 0. A usage rejection
        # of the file-bearing send (exit 2 — never a delivery
        # failure where acceptance is unknown) drops the files
        # and retries text-only so a bad attachment can never
        # sink the notification itself
        try:
            _send_marked(ledger, ev, i, sent_ids, fingerprint,
                         argv, chunks[i],
                         post_files if i == 0 else None,
                         deadline)
        except _SendUsage:
            if i != 0 or not post_files:
                raise
            post_files = None
            if ev["kind"] == "semantic_notice":
                _semantic_gate(ledger, ev, payload,
                               in_progress=bool(start or i))
            elif render_state:
                _semantic_render_gate(ledger, ev, render_state,
                                      in_progress=bool(start or i))
            _send_marked(ledger, ev, i, sent_ids, fingerprint,
                         argv, chunks[i], None, deadline)
        sent_ids.append(str(i + 1))
        ledger.outbox_progress(ev["event_id"], i + 1, sent_ids,
                               fingerprint)
    ledger.outbox_mark(ev["event_id"], "accepted",
                       sent_ids[-1] if sent_ids else "")
    res["sent"] += 1
    return True


def _fail_event(ledger, ev, cfg, res, exc):
    """Per-event failure accounting — quarantine, park, suppress or
    backoff according to the exception class (_SendUncertain must be
    checked before OSError, which it subclasses)."""
    if isinstance(exc, _DeferredSend):
        # stays pending but re-checks hourly, not every flush. A
        # parked enforce intent is policy-blocked, not incomplete —
        # count it separately so run_check's notify_incomplete signal
        # doesn't flag every other tick while the mode gate is down
        if _has_sent_progress(ev):
            _hold_event(ledger, ev, cfg)
            res["failed"] += 1
        else:
            ledger.db.execute(
                "UPDATE notify_outbox SET next_try=?,updated_at=? "
                "WHERE event_id=?",
                (time.time() + 3600, time.time(), ev["event_id"]))
            ledger.db.commit()
            res["parked"] += 1
    elif isinstance(exc, _FreezeSend):
        # An accepted chunk is immutable. Keep its receipt/progress and
        # freeze the remaining chunks until an explicit retry decision.
        _hold_event(ledger, ev, cfg)
        res["failed"] += 1
    elif isinstance(exc, _StaleSend):
        if _has_sent_progress(ev):
            _hold_event(ledger, ev, cfg)
            res["failed"] += 1
        else:
            ledger.outbox_suppress(ev["event_id"])
            res["suppressed"] += 1
    elif isinstance(exc, _SendUsage):
        # invocation itself refused (exit 2) — a CLI/config contract
        # problem; retrying cannot fix it, quarantine the event.
        # The refusal is provably pre-delivery, so held digest
        # members may be salvaged.
        _hold_event(ledger, ev, cfg, proven_undelivered=True)
        res["failed"] += 1
    elif isinstance(exc, _SendUncertain):
        _hold_event(ledger, ev, cfg)
        res["uncertain"] = res.get("uncertain", 0) + 1
        res["failed"] += 1
    elif isinstance(exc, ValueError):
        _hold_event(ledger, ev, cfg)
        res["failed"] += 1
    elif isinstance(exc, (OSError, TimeoutError, KeyError)):
        backoff = min(3600, 60 * (2 ** ev["attempts"]))
        ledger.outbox_mark(ev["event_id"], "failed", retry_in=backoff)
        res["failed"] += 1
    else:
        # Per-event containment: an unexpected failure inside
        # _format_event/semantic_send_gate (broken import, sqlite error)
        # must not escape flush and starve every later due event.
        # Retry hourly first — a transient fault self-heals; a
        # deterministic bug quarantines after 5 attempts instead of
        # looping forever.
        if ev["attempts"] >= 4:
            _hold_event(ledger, ev, cfg)
        else:
            ledger.outbox_mark(ev["event_id"], "failed",
                               retry_in=3600)
        res["failed"] += 1


def flush(ledger, limit: int = 10, deadline: float | None = None) -> dict:
    cfg = _config()
    res = {"sent": 0, "failed": 0, "skipped": 0, "suppressed": 0,
           "parked": 0}
    due = ledger.outbox_due(limit)
    exe = _hermes_exe(cfg)
    exe_ok = os.path.isfile(exe) and os.access(exe, os.X_OK)
    if not exe_ok and not any(
            _route(e) == "interactive" for e in due):
        res["skipped"] = len(due)
        return res
    for event_index, ev in enumerate(due):
        if deadline is not None and time.monotonic() >= deadline:
            res["skipped"] += len(due) - event_index
            break
        if ev["project_id"] and ledger.is_archived(ev["project_id"]):
            # queued before the patient was archived — archived patient
            # events must never reach the channel; drop terminally, do
            # not count as sent OR as a retryable failure (Oracle F2)
            ledger.outbox_suppress(ev["event_id"])
            res["suppressed"] += 1
            continue
        if _route(ev) == "interactive" \
                and _dispatch_interactive(ledger, ev, cfg, res):
            continue
        if not exe_ok:
            # no hermes exe — text events can't send, but an interactive
            # event later in the queue still dispatches (cards don't
            # need the exe), so skip per-event rather than break
            res["skipped"] += 1
            continue
        target = _target(cfg, ev["kind"])
        if not target:
            ledger.outbox_mark(ev["event_id"], "failed", retry_in=3600)
            res["failed"] += 1
            continue
        argv = _send_argv(cfg, target)
        try:
            if not _send_text(ledger, ev, cfg, argv, target, res,
                              deadline):
                res["skipped"] += len(due) - event_index
                return res
        except Exception as exc:
            _fail_event(ledger, ev, cfg, res, exc)
    return res
