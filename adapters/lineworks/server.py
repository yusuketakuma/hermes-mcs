"""Authenticate and durably queue bounded LINE WORKS callbacks before acknowledgement."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from hermes_plugin.mcs_delivery.paths import atomic_write, fsync_dir

from .client import verify_signature

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}\Z")
MAX_BODY = 65536


def callback_result(path):
    """Read a bounded receipt; damaged or unreadable state remains unknown."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(257)
        if len(raw) > 256:
            return "unknown"
        value = json.loads(raw)
        if isinstance(value, dict) and value.get("result") in ("processed", "unknown"):
            return value["result"]
    except (OSError, ValueError, RecursionError):
        pass
    return "unknown"


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


class CallbackInbox:
    def __init__(self, directory, settings, bot_secret, *, clock=time.time):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.settings, self.bot_secret, self.clock = settings, bot_secret, clock
        self._lock = threading.Lock()

    def accept(self, body, signature, bot_id):
        if not isinstance(body, bytes) or not 0 < len(body) <= MAX_BODY:
            return 400
        if bot_id != self.settings["application_id"] or not verify_signature(
                body, signature, self.bot_secret):
            return 403
        try:
            event = json.loads(body.decode("utf-8"), object_pairs_hook=_object)
            if not isinstance(event, dict):
                return 400
            source = event.get("source")
            if not isinstance(source, dict):
                return 400
            domain = source.get("domainId")
            if type(domain) not in (int, str) or str(domain) != self.settings["team_id"]:
                return 403
            user = source.get("userId")
            if not isinstance(user, str) or not ID.fullmatch(user) \
                    or user not in self.settings["allowed_user_ids"]:
                return 403
            channel = source.get("channelId")
            if channel is not None and channel != self.settings["channel_id"]:
                return 403
            issued = event.get("issuedTime")
            if not isinstance(issued, str) or len(issued) > 40:
                return 400
            stamp = datetime.fromisoformat(issued.replace("Z", "+00:00"))
            if stamp.tzinfo is None or not -60 <= self.clock() - stamp.timestamp() <= 600:
                return 403
            if event.get("type") not in ("message", "postback"):
                return 200
            content = event.get("content")
            if event["type"] == "message" and (
                    not isinstance(content, dict) or content.get("type") != "text"
                    or not isinstance(content.get("text"), str)
                    or len(content["text"]) > 2000):
                return 400
        except (ValueError, RecursionError, UnicodeError, OverflowError):
            return 400
        digest = hashlib.sha256(body).hexdigest()
        with self._lock:
            if any((self.directory / (digest + suffix)).exists()
                   for suffix in (".json", ".working", ".done")):
                return 200
            if sum(1 for _ in self.directory.glob("*.json")) >= 250:
                return 503
            atomic_write(str(self.directory / (digest + ".json")), body, mode=0o600)
        return 200

    def pending(self):
        return sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime)[:32]

    def take(self, path):
        working = path.with_suffix(".working")
        with self._lock:
            os.replace(path, working)
            fsync_dir(str(self.directory))
        return working, json.loads(working.read_bytes().decode("utf-8"))

    def finish(self, working, result):
        # Persist only status after processing; do not retain patient input.
        atomic_write(str(working.with_suffix(".done")),
                     json.dumps({"result": result}).encode(), mode=0o600)
        working.unlink()

    def expire(self):
        with self._lock:
            for path in self.directory.iterdir():
                if self.clock() - path.stat().st_mtime <= 1200:
                    continue
                if path.suffix == ".working":
                    # Crash-fenced input: discard contents, retain a content-free unknown.
                    self.finish(path, "unknown")
                elif path.suffix == ".done":
                    if callback_result(path) != "unknown":
                        path.unlink()
                elif path.suffix == ".json":
                    self.finish(path, "unknown")


def callback_server(inbox, *, host="127.0.0.1", port=8788):
    """Loopback HTTP behind a separately configured public HTTPS reverse proxy."""
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("callback_bind_must_be_loopback")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log request contents or callback metadata.

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def do_POST(self):
            status = 400
            try:
                lengths = self.headers.get_all("Content-Length") or []
                signatures = self.headers.get_all("X-WORKS-Signature") or []
                bots = self.headers.get_all("X-WORKS-BotId") or []
                length = int(lengths[0]) if len(lengths) == 1 else 0
                if self.path != "/lineworks/callback":
                    status = 404
                elif (self.headers.get("Transfer-Encoding") or len(signatures) != 1
                      or len(bots) != 1 or not 0 < length <= MAX_BODY
                      or self.headers.get_content_type() != "application/json"):
                    status = 400
                else:
                    body = self.rfile.read(length)
                    status = inbox.accept(body, signatures[0], bots[0]) \
                        if len(body) == length else 400
            except (ValueError, OSError, TimeoutError):
                status = 503
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

    return HTTPServer((host, port), Handler)
