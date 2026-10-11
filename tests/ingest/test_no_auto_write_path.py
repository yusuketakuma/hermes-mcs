"""No automatic press/post path into MCS (ROADMAP 1.1.0 #28 acceptance:
"自動押下経路が無いことをテストで固定する"; stamps.md §7: "自動経路から
POSTを呼べないことをテストで固定する").

Today the whole MCS surface is read-only: every API call is a GET, read
state clears only as a side effect of the snapshot-gated message GET, and
the only DOM interaction is the login form in ``_auto_login`` (behind ``auto_login``). These tests
pin that, so a future human-approved write (1.1.x) has to arrive as a
deliberate, reviewed change to this file rather than slipping in through
a scheduled job, a card button, or a delivery adapter.

Static AST checks over the production tree plus a recording fake worker —
no network, no real MCS."""

import ast
import base64
import json
from pathlib import Path

import pytest

import mcs_adapter

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = ("mcs", "adapters", "hermes_plugin", "mcs_standalone",
                    "lineworks_adapter")
ADAPTER = Path("mcs/ingest/mcs_adapter.py")
# Unrelated HTTP clients that define their own ``_request`` (LINE WORKS
# Bot REST, Slack SDK shim). Neither can reach the MCS API origin.
OWN_REQUEST_CLIENTS = {Path("adapters/lineworks/client.py"),
                       Path("adapters/slack/standalone.py")}
# Surfaces a human or a schedule triggers outside collection: card
# buttons, delivery workers, standalone host, LINE WORKS callback. They may
# only link to MCS (project_url), never hold a client for it.
DELIVERY_SURFACES = ("mcs/notify", "adapters", "hermes_plugin",
                     "mcs_standalone", "lineworks_adapter")
DELIVERY_ALLOWED_ADAPTER_NAMES = {"project_url"}
NETWORK_MODULES = {"mcs_adapter", "mcs_worker", "run_check", "init_data",
                   "cross_lists", "project_metadata"}


def _production_files():
    for top in PRODUCTION_ROOTS:
        for path in sorted((ROOT / top).rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path.relative_to(ROOT)


def _parse(rel):
    return ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=str(rel))


def _docstring_nodes(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def _string_constants(tree):
    """(node, text) of every str literal that is code, not a docstring."""
    docs = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docs:
            yield node, node.value


def _method_of(call, position):
    """The HTTP method a call passes, as a literal, or None if not literal."""
    for kw in call.keywords:
        if kw.arg == "method":
            return kw.value.value if isinstance(kw.value, ast.Constant) else None
    if len(call.args) > position and isinstance(call.args[position], ast.Constant):
        return call.args[position].value
    return None


def _calls_to(attr):
    for rel in _production_files():
        if rel in OWN_REQUEST_CLIENTS:
            continue
        for node in ast.walk(_parse(rel)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == attr:
                yield rel, node


def test_every_mcs_request_call_is_a_literal_get():
    calls = list(_calls_to("_request"))
    assert calls, "MCSAdapter._request callers vanished — re-point this test"
    bad = [f"{rel}:{node.lineno}" for rel, node in calls
           if _method_of(node, 0) != "GET"]
    assert not bad, f"non-GET (or non-literal) MCS request: {bad}"


def test_every_api_worker_call_is_a_literal_get():
    calls = [(rel, node) for rel, node in _calls_to("_io")
             if node.args and isinstance(node.args[0], ast.Constant)
             and node.args[0].value == "api"]
    assert len(calls) >= 2, "api worker callers vanished — re-point this test"
    adapter_tree = _parse(ADAPTER)
    bad = []
    for rel, node in calls:
        if _method_of(node, 99) == "GET":
            continue
        # _request forwards its own method, pinned to GET by every caller
        # in test_every_mcs_request_call_is_a_literal_get.
        fn = _enclosing_function(adapter_tree, node) if rel == ADAPTER else None
        forwarded = any(kw.arg == "method" and isinstance(kw.value, ast.Name)
                        and kw.value.id == "method" for kw in node.keywords)
        if fn is not None and fn.name == "_request" and forwarded:
            continue
        bad.append(f"{rel}:{node.lineno}")
    assert not bad, f"non-GET (or non-literal) api worker call: {bad}"
    # Every other _io operation is local (CDP on loopback) or a download.
    ops = {node.args[0].value for _rel, node in _calls_to("_io")
           if node.args and isinstance(node.args[0], ast.Constant)}
    assert ops <= {"api", "cdp_eval", "cdp_json", "download"}, ops


def test_no_mcs_write_route_is_named_in_code():
    """mark_as_read (retired) and reaction removal stay out of code;
    docstrings may still explain why they are not called."""
    hits = []
    for rel in _production_files():
        for node, text in _string_constants(_parse(rel)):
            if "mark_as_read" in text or "reactions/delete" in text:
                hits.append(f"{rel}:{node.lineno}")
    assert not hits, hits


def test_mcs_origin_literal_is_confined():
    """Only the adapter (network) and the view (link/provenance check)
    name the MCS origin, so no other module can build its own client."""
    found = set()
    for rel in _production_files():
        for _node, text in _string_constants(_parse(rel)):
            if "medical-care.net" in text:
                found.add(rel)
    assert found <= {ADAPTER, Path("mcs/views/mcs_view.py")}, sorted(map(str, found))


def test_delivery_and_action_surfaces_hold_no_mcs_client():
    """Card buttons (確認/担当/保留/依頼), delivery workers and the
    standalone/LINE WORKS hosts link to MCS but cannot call it."""
    bad = []
    for rel in _production_files():
        if not str(rel).startswith(DELIVERY_SURFACES):
            continue
        for node in ast.walk(_parse(rel)):
            if isinstance(node, ast.ImportFrom) and node.module == "mcs_adapter":
                extra = {a.name for a in node.names} - DELIVERY_ALLOWED_ADAPTER_NAMES
                if extra:
                    bad.append(f"{rel}:{node.lineno} {sorted(extra)}")
            elif isinstance(node, ast.ImportFrom) and node.module in NETWORK_MODULES:
                bad.append(f"{rel}:{node.lineno} from {node.module}")
            elif isinstance(node, ast.Import):
                names = {a.name for a in node.names} & NETWORK_MODULES
                if names:
                    bad.append(f"{rel}:{node.lineno} import {sorted(names)}")
    assert not bad, bad


def _enclosing_function(tree, target):
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.lineno <= target.lineno <= node.end_lineno:
            if best is None or node.lineno > best.lineno:
                best = node
    return best


def test_dom_clicks_exist_only_in_the_login_form():
    """The one scripted click is the login submit inside _auto_login (the
    body auto_login wraps with the login backoff); no other injected script presses, submits or dispatches anything."""
    owners = []
    for rel in _production_files():
        tree = _parse(rel)
        for node, text in _string_constants(tree):
            if any(tok in text for tok in (".click(", ".submit(", "dispatchEvent(")):
                fn = _enclosing_function(tree, node)
                owners.append((rel, fn.name if fn else None))
    assert owners, "auto_login script vanished — re-point this test"
    assert set(owners) == {(ADAPTER, "_auto_login")}, owners


def test_cdp_only_opens_the_unreads_and_login_pages():
    tree = _parse(ADAPTER)
    pages = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts = [v.value for v in node.values if isinstance(v, ast.Constant)]
            if parts and parts[0].startswith("/json/new?"):
                pages.add("".join(parts[1:]))
    assert pages == {"/unreads", "/authentication/login"}, pages


class _RecordingWorker:
    def __init__(self, replies):
        self.calls, self._replies = [], list(replies)

    def __call__(self, payload, timeout=None, deadline=None):
        self.calls.append(payload)
        status, body = self._replies.pop(0)
        return {"status": status, "headers": {},
                "body": base64.b64encode(json.dumps(body).encode()).decode()}


_CLEARED = {"project": {"is_archived": False, "oldest_unread_message": None}}


@pytest.mark.parametrize("fallback", [False, True], ids=["strict", "fallback_plain"])
def test_mark_read_is_gets_only_on_the_wire(fallback):
    """The one state-changing operation (read clearing) sends only GETs,
    gated on the snapshot timestamp — no POST reaches the worker."""
    replies = ([(500, {})] * 3 if fallback else []) + [
        (200, {"messages": [], "paginate": {"has_next": False}}), (200, _CLEARED)]
    worker = _RecordingWorker(replies)
    adapter = mcs_adapter.MCSAdapter(worker=worker)
    adapter._token = "synthetic-token"
    adapter._sleep_bounded = lambda _s: None
    adapter.mark_patient_read(7, 1_700_000_000, fallback_plain=fallback)
    assert worker.calls and all(c["operation"] == "api" for c in worker.calls)
    assert {c["method"] for c in worker.calls} == {"GET"}
    assert all(c["data"] is None for c in worker.calls)
    assert all("timestamp=1700000000" in c["url"] for c in worker.calls[:-1])
