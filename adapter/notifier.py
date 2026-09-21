"""Discord notifier — drains notify_outbox to a Discord channel.

Credentials: bot token is read at send time; it is never printed, logged,
or stored elsewhere. When ~/.mcs/config.json sets "notify_bot_profile"
(e.g. "cco"), the token comes from ~/.hermes/profiles/<name>/.env so posts
arrive under that bot's identity; otherwise ~/.hermes/.env is used.
Channel id comes from ~/.mcs/config.json {discord_channel_id} falling
back to DISCORD_HOME_CHANNEL in ~/.hermes/.env.

Events carry message_ids; message content is looked up in the local ledger
at send time so the outbox payload itself stays tiny.
"""
import hashlib
import json
import math
import mimetypes
import os
import re
import time
import urllib.request
import urllib.error
import uuid

from mcs_util import NoRedirect, html_to_text, load_config, no_proxy_opener

ENV_PATH = os.path.expanduser("~/.hermes/.env")
CONF_PATH = os.path.expanduser("~/.mcs/config.json")
API = "https://discord.com/api/v10"
_MAX_LEN = 1900
# Discord per-message file caps; oversized files stay local-only and are
# noted in the text instead of failing the whole send.
_MAX_FILES = 10
_MAX_FILE_BYTES = 24 * 1024 * 1024
_MAX_FILES_BYTES = 23 * 1024 * 1024  # leaves room for JSON + multipart framing
_MAX_REQUEST_BYTES = 24 * 1024 * 1024


def _env(key: str, path: str = ENV_PATH) -> str | None:
    try:
        for line in open(path, encoding="utf-8"):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


_PROFILE_ENV = os.path.expanduser("~/.hermes/profiles/{}/.env")


class _DeferredSend(Exception):
    """Not deliverable under CURRENT policy (e.g. semantic mode left
    enforce) — the committed intent stays pending for a later flush;
    it is not destroyed (INV-23) and never sent past its gate."""


class _StaleSend(Exception):
    """The source generation this intent was built for has moved on —
    suppress terminally instead of publishing stale results (§18.4)."""


def _config() -> dict:
    return load_config(CONF_PATH)


def _token() -> str | None:
    """Bot token for sends. `notify_bot_profile` in config.json pins a
    dedicated bot identity (e.g. "cco"); when set, its token MUST resolve —
    falling back to another bot would post under the wrong identity."""
    profile = _config().get("notify_bot_profile")
    if isinstance(profile, str) and profile:
        if not re.fullmatch(r"[a-z0-9_-]+", profile):
            return None
        return _env("DISCORD_BOT_TOKEN", _PROFILE_ENV.format(profile))
    return _env("DISCORD_BOT_TOKEN")


def _channel_id(kind: str) -> str | None:
    """Patient-content events ONLY go to the explicitly configured MCS
    channel — a missing config must never spill bodies into a fallback
    channel (Oracle B15). System alerts may use the home channel."""
    cid = _config().get("discord_channel_id")
    if cid and str(cid).isdigit():
        return str(cid)
    if kind in ("session_expired", "run_failed"):
        cid = _env("DISCORD_HOME_CHANNEL")
        return cid if cid and cid.isdigit() else None
    return None


def _artifact(ledger, kind: str, mid: int) -> dict | None:
    r = ledger.db.execute(
        "SELECT a.content FROM artifacts a JOIN messages m "
        "ON m.message_id=a.message_id WHERE a.kind=? AND a.message_id=? "
        "AND CASE WHEN json_valid(a.meta) THEN "
        "json_extract(a.meta,'$.error') IS NOT 1 AND "
        "json_extract(a.meta,'$.hash')=m.content_hash ELSE 0 END "
        "AND CASE WHEN json_valid(a.content) THEN "
        "json_type(a.content)='object' ELSE 0 END "
        "ORDER BY a.artifact_id DESC LIMIT 1", (kind, mid)).fetchone()
    if not r:
        return None
    try:
        d = json.loads(r["content"])
    except (json.JSONDecodeError, TypeError):
        return None
    return d if isinstance(d, dict) else None


_RX_LABEL = {"start": "開始", "stop": "中止", "change": "変更",
             "no_change": "変更なし", "increase": "増量", "decrease": "減量"}
_REQ_LABEL = {"confirm": "確認", "contact": "連絡", "share": "共有",
              "ask": "質問", "request": "依頼", "report": "報告"}
_EVT_LABEL = {"admission": "入院", "discharge": "退院", "exam": "受診/検査",
              "visit": "訪問", "medication": "投薬", "adherence": "服薬",
              "care": "介護", "eol": "看取り", "media_ref": "添付"}


def _structured_lines(ledger, mid: int) -> list[str]:
    """Compact structured summary from extract_v1/extract_llm artifacts.
    Returns [] when nothing usable exists (caller falls back to raw only)."""
    v1 = _artifact(ledger, "extract_v1", mid) or {}
    llm = _artifact(ledger, "extract_llm", mid) or {}
    if not v1 and not llm:
        return []
    lines: list[str] = []
    if (llm.get("summary") or "").strip():
        lines.append(f"要約: {llm['summary'].strip()[:80]}")
    pts = [str(p).strip() for p in (llm.get("points") or [])
           if isinstance(p, str) and p.strip()]
    if pts:
        lines.append("要点: " + " / ".join(p[:40] for p in pts[:3]))
    evs = [e for e in (llm.get("events") or v1.get("events") or [])
           if e in _EVT_LABEL]
    if evs:
        lines.append("区分: " + "・".join(_EVT_LABEL[e] for e in evs[:5]))
    vit = llm.get("vitals") if isinstance(llm.get("vitals"), dict) else None
    if not vit and isinstance(v1.get("vitals"), dict):
        vit = v1["vitals"]
    if vit:
        parts = []
        if vit.get("sbp") is not None:
            parts.append(f"BP {vit['sbp']}/{vit.get('dbp')}")
        for k, lab in (("bt", "BT"), ("hr", "HR"), ("rr", "RR"),
                       ("spo2", "SpO2"), ("bs", "BS")):
            if vit.get(k) is not None:
                parts.append(f"{lab} {vit[k]}")
        if parts:
            lines.append("バイタル: " + "  ".join(parts))
    syms, neg, seen, neg_seen = [], [], set(), set()
    for s in llm.get("symptoms") or []:
        if isinstance(s, dict) and s.get("text"):
            if s.get("negated"):
                if s["text"] not in neg_seen:
                    neg_seen.add(s["text"]); neg.append(s["text"])
            elif s["text"] not in seen:
                seen.add(s["text"]); syms.append(s["text"])
    for s in v1.get("symptoms") or []:
        contradicted = any(n in s or s in n for n in neg_seen)
        if s and s not in seen and not contradicted:
            syms.append(s)
    if syms or neg:
        line = "症状: " + "、".join(syms[:6])
        if neg:
            line += ("　" if syms else "") + "、".join(
                f"{n}なし" for n in neg[:4])
        lines.append(line)
    meds = []
    for m in llm.get("meds") or []:
        if isinstance(m, dict) and m.get("name"):
            d = str(m["name"]) + (f" {m['dose']}" if m.get("dose") else "")
            if m.get("action") in _RX_LABEL:
                d += f"[{_RX_LABEL[m['action']]}]"
            meds.append(d)
    if not meds:
        for m in v1.get("medications") or []:
            if isinstance(m, dict) and m.get("name"):
                meds.append(str(m["name"]) +
                            (f" {m['dose']}" if m.get("dose") else ""))
    rx = [f"{_RX_LABEL[a['action']]}:{a['ctx'][:18]}"
          for a in v1.get("rx_actions") or []
          if isinstance(a, dict) and a.get("action") in _RX_LABEL
          and a.get("ctx")]
    if meds or rx:
        lines.append("薬剤: " + "、".join((meds + rx)[:6]))
    reqs = []
    for r in llm.get("requests") or []:
        if isinstance(r, dict) and (r.get("to") or r.get("action")):
            to = str(r.get("to") or "")
            to = "" if to in ("不明", "unknown", "-") else f"{to}へ"
            reqs.append(to + str(r.get("action") or "")[:30])
    if not reqs:
        for r in v1.get("requests") or []:
            if isinstance(r, dict) and r.get("ctx"):
                reqs.append(f"{_REQ_LABEL.get(r.get('kind'), '依頼')}:"
                            f"{r['ctx'][:24]}")
    if reqs:
        lines.append("依頼: " + " / ".join(reqs[:3]))
    if v1.get("med_periods"):
        mp = v1["med_periods"][0]
        if isinstance(mp, dict) and mp.get("start"):
            lines.append(f"服薬期間: {mp['start']}〜{mp.get('end') or '?'}")
    if v1.get("next_planned"):
        lines.append(f"次回予定: {v1['next_planned']}")
    return lines


def _urgency(ledger, mid: int) -> str | None:
    for kind in ("extract_llm", "extract_v1"):
        d = _artifact(ledger, kind, mid)
        if d and d.get("urgency") == "high":
            return "high"
    return None


def _attachments_map(ledger, mids: list[int]) -> dict:
    """message_id -> attachment rows (all states) for 📎 markers + files."""
    if not mids:
        return {}
    q = ("SELECT message_id,file_id,name,state,local_path,bytes,sha256 "
         f"FROM attachments WHERE message_id IN ({','.join('?' * len(mids))}) "
         "ORDER BY attachment_id")
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
    if ev["kind"] == "semantic_notice":
        # audited/degraded semantic notice — the payload carries the
        # final text frozen at enqueue time; the sender never re-derives
        # it (INV-14). Two gates are re-checked at the last moment
        # (§18.4/§22.1): mode must still be enforce, and the audited
        # notice's input fingerprint must still be live — a degraded
        # notice is instead suppressed if its base event has since
        # delivered.
        try:
            import semantic as _sem
        except Exception:
            # a queued intent whose renderer cannot load is parked,
            # not dropped — flush() must keep serving other events
            raise _DeferredSend("semantic_import_failed")
        try:
            scfg, _errs = _sem.semantic_config(_config())
        except Exception:
            raise _DeferredSend("semantic_config_failed")
        if scfg["mode"] != "enforce":
            raise _DeferredSend("semantic_not_enforce")
        if payload.get("degraded"):
            src = payload.get("src_event_id")
            if type(src) is int:
                row = ledger.db.execute(
                    "SELECT state FROM notify_outbox WHERE event_id=?",
                    (src,)).fetchone()
                if row is None or row["state"] in ("accepted",
                                                   "suppressed"):
                    raise _StaleSend("base_delivered")
        else:
            fp = payload.get("fingerprint")
            root = payload.get("root_id")
            src = payload.get("src_event_id")
            if not isinstance(fp, str) or type(root) is not int \
                    or type(src) is not int:
                raise ValueError("payload_invalid")
            # the notice descends from a stored arrival intent — if that
            # origin event was suppressed or no longer exists, the
            # follow-up must not deliver either (INV-20, AT-055)
            srow = ledger.db.execute(
                "SELECT state FROM notify_outbox WHERE event_id=? "
                "AND kind='new_messages'", (src,)).fetchone()
            if srow is None or srow["state"] == "suppressed":
                raise _StaleSend("src_event_ineligible")
            b = _sem.thread_bundle(ledger, ev["project_id"], root)
            if b is None or b["source_fingerprint"] != fp:
                raise _StaleSend("stale_generation")
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("payload_invalid")
        return text, []
    ids = payload.get("message_ids") or []
    if (not isinstance(ids, list)
            or any(type(mid) is not int or mid <= 0 for mid in ids)):
        raise ValueError("payload_invalid")
    rows = []
    for mid in ids:
        r = ledger.db.execute("""
          SELECT m.*, p.patient_name FROM messages m
          LEFT JOIN patients p ON p.project_id = m.project_id
          WHERE m.message_id=?""", (mid,)).fetchone()
        if r:
            rows.append(r)
    src = payload.get("source", "unread")
    if not rows:
        return f"[MCS] 新着 {len(ids)} 件 (project {ev['project_id']})", []
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
        s_lines = _structured_lines(ledger, r["message_id"])
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
        """Enforce-mode: audited summary section appended to this post.
        Only the newest semantic_summary with audit_status PASS on the
        CURRENT generation qualifies — stale/failed/unaudited ones are
        silently absent, and in shadow/assist/off the notification stays
        byte-identical to pre-Phase-J (INV-16, spec §20.2)."""
        try:
            import semantic as _sem
            scfg, _errs = _sem.semantic_config(_config())
            if scfg["mode"] != "enforce":
                return ""
            art = ledger.db.execute(
                "SELECT content,meta FROM artifacts "
                "WHERE kind='semantic_summary' AND message_id=? "
                "ORDER BY artifact_id DESC LIMIT 1",
                (r["message_id"],)).fetchone()
            if not art:
                return ""
            meta = json.loads(art["meta"] or "{}")
            if meta.get("audit_status") != "PASS" or meta.get("stale"):
                return ""
            # the audited summary must describe THIS revision — a body
            # edit after PASS leaves the old artifact on record but it
            # may no longer be attached (INV-15)
            if meta.get("target_revision") != r["content_hash"]:
                return ""
            summ = json.loads(art["content"])
            lines = [str(c.get("text", ""))[:160]
                     for c in summ.get("claims", [])
                     if isinstance(c, dict) and c.get("text")][:6]
            if not lines:
                return ""
            body = "\n".join(f"・{x}" for x in lines)
            lims = [str(x)[:80] for x in (summ.get("limitations") or [])
                    if isinstance(x, str)]
            if lims:
                body += "\n・（原文確認）" + "；".join(lims[:2])
            return f"\n───── 要約（自動検査済） ─────\n{body}"
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


# The bot token rides the Authorization header — a redirect anywhere
# (even same-host path change is unneeded for this API) must never be
# followed with credentials attached (Oracle B16). NoRedirect + the
# no-proxy opener come from mcs_util.
_OPENER = no_proxy_opener(NoRedirect)


def _multipart(payload: dict, files: list[tuple[str, str]]) -> tuple[bytes, str]:
    """multipart/form-data body for Discord file upload. payload_json carries
    the JSON body; files[i] parts carry raw bytes. Original (possibly
    non-ASCII) filename goes in payload_json attachments; the Content-
    Disposition filename is an ASCII-safe stand-in."""
    boundary = f"mcs-{uuid.uuid4().hex}"
    out = bytearray()
    out += (f"--{boundary}\r\nContent-Disposition: form-data; "
            f"name=\"payload_json\"\r\nContent-Type: application/json"
            f"\r\n\r\n").encode()
    out += json.dumps(payload).encode() + b"\r\n"
    for i, (fname, path) in enumerate(files):
        ext = os.path.splitext(fname)[1] or ".bin"
        ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        out += (f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"files[{i}]\"; filename=\"file{i}{ext}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n").encode()
        with open(path, "rb") as f:
            data = f.read(_MAX_FILE_BYTES + 1)
        if len(data) > _MAX_FILE_BYTES:
            raise ValueError("file_too_large")
        out += data
        out += b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    if len(out) > _MAX_REQUEST_BYTES:
        raise ValueError("multipart_too_large")
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _post(token: str, channel: str, content: str,
          files: list[tuple[str, str]] | None = None) -> str:
    """Returns Discord message id. Raises on failure."""
    payload = {"content": content,
               # message text must never ping @everyone/users
               "allowed_mentions": {"parse": []}}
    if files:
        payload["attachments"] = [{"id": i, "filename": fn}
                                  for i, (fn, _) in enumerate(files)]
        body, ctype = _multipart(payload, files)
        headers = {"Authorization": f"Bot {token}",
                   "User-Agent": "DiscordBot (mcs-adapter, 1.0)",
                   "Content-Type": ctype}
        timeout = 60
    else:
        body = json.dumps(payload).encode()
        headers = {"Authorization": f"Bot {token}",
                   "User-Agent": "DiscordBot (mcs-adapter, 1.0)",
                   "Content-Type": "application/json"}
        timeout = 15
    req = urllib.request.Request(
        f"{API}/channels/{channel}/messages",
        data=body, headers=headers, method="POST")
    with _OPENER.open(req, timeout=timeout) as res:
        reply = json.load(res)
    mid = reply.get("id") if isinstance(reply, dict) else None
    if ((type(mid) is int and mid > 0)
            or (isinstance(mid, str) and mid.isdigit())):
        return str(mid)
    raise ValueError("discord_receipt_invalid")


def _retry_after(e: urllib.error.HTTPError) -> float:
    """Discord 429 carries the real wait; default bounded backoff."""
    try:
        d = json.loads(e.read() or b"{}")
        wait = float(d.get("retry_after", 0))
        return wait + 1 if math.isfinite(wait) and wait >= 0 else 60.0
    except (ValueError, TypeError):
        return 60.0


def _delivery_fingerprint(channel: str, chunks: list[str],
                          files: list[tuple[str, str]]) -> str:
    h = hashlib.sha256(channel.encode())
    for chunk in chunks:
        h.update(b"\0text\0"); h.update(chunk.encode())
    for name, path in files:
        h.update(b"\0file\0"); h.update(name.encode())
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block)
    return h.hexdigest()


def _progress(raw: str, count: int) -> tuple[int, list[str], str | None]:
    try:
        d = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        raise ValueError("progress_invalid") from e
    if not isinstance(d, dict) or type(d.get("next", 0)) is not int:
        raise ValueError("progress_invalid")
    next_chunk = d.get("next", 0)
    sent = d.get("sent", [])
    fingerprint = d.get("fingerprint")
    if (next_chunk < 0 or next_chunk > count or not isinstance(sent, list)
            or len(sent) != next_chunk
            or any(not isinstance(x, str) or not x.isdigit() for x in sent)
            or (fingerprint is not None and not isinstance(fingerprint, str))):
        raise ValueError("progress_invalid")
    return next_chunk, sent, fingerprint


def flush(ledger, limit: int = 10, deadline: float | None = None) -> dict:
    token = _token()
    res = {"sent": 0, "failed": 0, "skipped": 0, "suppressed": 0,
           "parked": 0}
    due = ledger.outbox_due(limit)
    if not token:
        res["skipped"] = len(due)
        return res
    for event_index, ev in enumerate(due):
        if deadline is not None and time.monotonic() >= deadline:
            res["skipped"] += len(due) - event_index
            break
        if ev["project_id"] and ledger.is_archived(ev["project_id"]):
            # queued before the patient was archived — archived patient
            # events must never reach Discord; drop terminally, do not
            # count as sent OR as a retryable failure (Oracle F2)
            ledger.outbox_suppress(ev["event_id"])
            res["suppressed"] += 1
            continue
        channel = _channel_id(ev["kind"])
        if not channel:
            ledger.outbox_mark(ev["event_id"], "failed", retry_in=3600)
            res["failed"] += 1
            continue
        try:
            content, files = _format_event(ledger, ev)
            chunks = [content[i:i + _MAX_LEN]
                      for i in range(0, len(content), _MAX_LEN)]
            # resume at the first unacknowledged chunk — a crash after a
            # partial send must not duplicate accepted chunks (B25)
            start, sent_ids, previous = _progress(ev["progress"], len(chunks))
            fingerprint = _delivery_fingerprint(channel, chunks, files)
            if start and previous != fingerprint:
                ledger.outbox_hold(ev["event_id"])
                res["failed"] += 1
                continue
            if not start:
                ledger.outbox_progress(ev["event_id"], 0, [], fingerprint)
            mid = ""
            post_files = files
            for i in range(start, len(chunks)):
                if deadline is not None and time.monotonic() >= deadline:
                    res["skipped"] += len(due) - event_index
                    return res
                # files ride the FIRST post only; on resume (start>0) they
                # were already delivered with chunk 0. A definitive
                # rejection of the file-bearing post (local size/format
                # error, or Discord 4xx — never 429/5xx/network where
                # acceptance is unknown) drops the files and retries
                # text-only so a bad attachment can never sink the
                # notification itself
                try:
                    mid = _post(token, channel, chunks[i],
                                post_files if i == 0 else None)
                except ValueError as e:
                    # only errors raised while BUILDING the body — a
                    # post-send ValueError (receipt_invalid) may mean
                    # the message already reached Discord
                    if (i != 0 or not post_files or str(e) not in
                            ("file_too_large", "multipart_too_large")):
                        raise
                    post_files = None
                    mid = _post(token, channel, chunks[i])
                except urllib.error.HTTPError as e:
                    if (i != 0 or not post_files or e.code == 429
                            or e.code < 400 or e.code >= 500):
                        raise
                    post_files = None
                    mid = _post(token, channel, chunks[i])
                sent_ids.append(mid)
                ledger.outbox_progress(ev["event_id"], i + 1, sent_ids,
                                       fingerprint)
            ledger.outbox_mark(ev["event_id"], "accepted",
                               sent_ids[-1] if sent_ids else "")
            res["sent"] += 1
        except _DeferredSend:
            # stays pending but re-checks hourly, not every flush. A
            # parked enforce intent is policy-blocked, not incomplete —
            # count it separately so run_check's notify_incomplete signal
            # doesn't flag every other tick while the mode gate is down
            ledger.db.execute(
                "UPDATE notify_outbox SET next_try=?,updated_at=? "
                "WHERE event_id=?",
                (time.time() + 3600, time.time(), ev["event_id"]))
            ledger.db.commit()
            res["parked"] += 1
        except _StaleSend:
            ledger.outbox_suppress(ev["event_id"])
            res["suppressed"] += 1
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry = _retry_after(e)
            elif e.code >= 500:
                retry = 60
            elif e.code in (401, 403, 404):
                retry = 3600  # config/permission issue — slow retry
            else:
                retry = min(3600, 60 * (2 ** ev["attempts"]))
            ledger.outbox_mark(ev["event_id"], "failed", retry_in=retry)
            res["failed"] += 1
            if e.code == 429:
                break
        except ValueError:
            ledger.outbox_hold(ev["event_id"])
            res["failed"] += 1
        except (urllib.error.URLError, TimeoutError, KeyError, OSError):
            backoff = min(3600, 60 * (2 ** ev["attempts"]))
            ledger.outbox_mark(ev["event_id"], "failed", retry_in=backoff)
            res["failed"] += 1
    return res
