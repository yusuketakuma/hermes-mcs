"""No automatic press/post path into MCS (ROADMAP 1.1.0 #28 acceptance:
"自動押下経路が無いことをテストで固定する"; stamps.md §7: "自動経路から
POSTを呼べないことをテストで固定する").

Every API call is a GET except one human-approved write: the own-stamp
add/cancel POST in ``MCSAdapter.write_reaction``, reached only from
``stamp_write._execute`` after its receipt check (stamps.md). Read state
clears only as a side effect of the snapshot-gated message GET, and the
only DOM interaction is the login form in ``_auto_login`` (behind
``auto_login``). These tests pin that, so any further write has to arrive
as a deliberate, reviewed change to this file rather than slipping in
through a scheduled job, a card button, or a delivery adapter.

Static AST checks over the production tree plus a recording fake worker —
no network, no real MCS."""

import ast
import base64
import functools
import json
import re
import urllib.parse
from pathlib import Path

import pytest

import mcs_adapter
import mcs_contract

ROOT = Path(__file__).resolve().parents[2]
# deployment/ holds scheduled Python entrypoints (mcs_recover.py under
# launchd) and scripts/ the installer/upgrade helpers; both run unattended.
PRODUCTION_ROOTS = ("mcs", "adapters", "hermes_plugin", "mcs_standalone",
                    "lineworks_adapter", "deployment", "scripts")
ADAPTER = Path("mcs/ingest/mcs_adapter.py")
CONTRACT = Path("mcs/core/mcs_contract.py")
WORKER = Path("mcs/ingest/mcs_worker.py")
DOWNLOAD = Path("mcs/ingest/mcs_download.py")
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
NETWORK_MODULES = {"mcs_adapter", "mcs_worker", "mcs_cdp", "mcs_download", "run_check", "init_data",
                   "cross_lists", "project_metadata"}


# Scheduled launchers (LaunchAgents, cron-style shell wrappers) and the
# installer run without a human; they must not reach MCS on their own.
LAUNCHER_ROOTS = ("deployment", "scripts")
LAUNCHER_FILES = ("install.sh",)


@functools.cache
def _production_files():
    return tuple(path.relative_to(ROOT)
                 for top in PRODUCTION_ROOTS
                 for path in sorted((ROOT / top).rglob("*.py"))
                 if "__pycache__" not in path.parts)


@functools.cache
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


# The single human-approved write and its single caller (stamps.md).
STAMP_WRITE = (ADAPTER, "write_reaction")
STAMP_CALLER = (Path("mcs/stamps/stamp_write.py"), "_execute")


def _enclosing(rel, call):
    """Name of the innermost function whose body contains ``call``."""
    found = None
    for node in ast.walk(_parse(rel)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.lineno <= call.lineno <= node.end_lineno \
                and (found is None or node.lineno > found.lineno):
            found = node
    return found.name if found else None


def _keyword(call, name):
    value = next((kw.value for kw in call.keywords if kw.arg == name), None)
    return value.value if isinstance(value, ast.Constant) else ...


def test_every_mcs_request_call_is_a_literal_get():
    calls = list(_calls_to("_request"))
    assert calls, "MCSAdapter._request callers vanished — re-point this test"
    writes = [(rel, node) for rel, node in calls if _method_of(node, 0) != "GET"]
    bad = [f"{rel}:{node.lineno}" for rel, node in writes
           if (rel, _enclosing(rel, node)) != STAMP_WRITE]
    assert not bad, f"non-GET (or non-literal) MCS request: {bad}"
    # The stamp write is one literal POST that never retries or logs in.
    assert len(writes) == 1, [f"{rel}:{node.lineno}" for rel, node in writes]
    (_rel, post), = writes
    assert _method_of(post, 0) == "POST"
    assert (_keyword(post, "retries"), _keyword(post, "extend_session"),
            _keyword(post, "_cached_session_only")) == (0, False, True)


def test_stamp_write_is_reached_only_from_the_approved_executor():
    callers = [(rel, _enclosing(rel, node)) for rel, node in _calls_to("write_reaction")]
    assert callers == [STAMP_CALLER], callers
    rel, name = STAMP_CALLER
    executor = next(node for node in ast.walk(_parse(rel))
                    if isinstance(node, ast.FunctionDef) and node.name == name)
    # The receipt check is the executor's first branch; only local parsing precedes it.
    first = next(node for node in executor.body if not isinstance(node, ast.Assign))
    assert isinstance(first, ast.If) and any(
        isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_approved"
        for n in ast.walk(first.test)), "stamp executor lost its receipt check"


def test_request_forwards_its_method_unmodified():
    """_request passes its caller's literal method straight through; a
    reassignment inside it could turn a caller's GET into a POST."""
    fn = next(node for node in ast.walk(_parse(ADAPTER))
              if isinstance(node, ast.FunctionDef) and node.name == "_request")
    assert fn.args.args[1].arg == "method"
    rebinds = [node.lineno for node in ast.walk(fn)
               if isinstance(node, (ast.Name)) and node.id == "method"
               and isinstance(node.ctx, (ast.Store, ast.Del))]
    assert not rebinds, f"_request rebinds method at {rebinds}"


def _operation_of(call):
    """The _io operation as a literal, or None when it is not one."""
    for kw in call.keywords:
        if kw.arg == "operation":
            return kw.value.value if isinstance(kw.value, ast.Constant) else None
    if call.args and isinstance(call.args[0], ast.Constant):
        return call.args[0].value
    return None


def test_every_api_worker_call_is_a_literal_get():
    io_calls = list(_calls_to("_io"))
    dynamic = [f"{rel}:{node.lineno}" for rel, node in io_calls
               if _operation_of(node) is None]
    assert not dynamic, f"_io operation must be a literal: {dynamic}"
    calls = [(rel, node) for rel, node in io_calls
             if _operation_of(node) == "api"]
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
    ops = {_operation_of(node) for _rel, node in io_calls}
    assert ops <= {"api", "cdp_eval", "cdp_json", "download"}, ops


def test_no_mcs_write_route_is_named_in_code():
    """mark_as_read (retired) stays out of code; the reaction-removal suffix is
    named only inside the approved stamp write (and the worker's POST
    allowlist), so no other code can assemble that route piecewise. The add
    route shares its path with a GET read, so the method guards pin that one."""
    hits = []
    allowed = {STAMP_WRITE, (WORKER, None)}
    for rel in _production_files():
        for node, text in _string_constants(_parse(rel)):
            if "mark_as_read" in text:
                hits.append(f"{rel}:{node.lineno}")
            elif "/delete" in text:
                where = (rel, _enclosing(rel, node)) if rel != WORKER else (WORKER, None)
                if where not in allowed:
                    hits.append(f"{rel}:{node.lineno}")
    assert not hits, hits


# Cross-list acquisition now uses only the adapter's bounded wire owner.
# The current worker still prefix-checks the endpoint; its existing exact
# holder permission remains, without granting it to pure cross-list parsing.
ENDPOINT_IMPORTERS = {ADAPTER, WORKER}
ENDPOINT_ATTRIBUTE_READERS = set()
MCS_ENDPOINT_NAMES = {"API", "BASE"}
MCS_ENDPOINT_MODULES = {"mcs_adapter", "mcs_contract"}


def test_mcs_endpoint_constants_are_confined():
    """No module can build its own MCS client from the adapter's endpoint
    constants (e.g. urllib Request(API + ..., method="POST"))."""
    imports, attributes = set(), set()
    for rel in _production_files():
        for node in ast.walk(_parse(rel)):
            if isinstance(node, ast.ImportFrom) and node.module in MCS_ENDPOINT_MODULES \
                    and {a.name for a in node.names} & (MCS_ENDPOINT_NAMES | {"*"}):
                imports.add(rel)
            elif isinstance(node, ast.Attribute) and node.attr in MCS_ENDPOINT_NAMES \
                    and rel != ADAPTER:
                attributes.add(rel)
    assert imports == ENDPOINT_IMPORTERS, sorted(map(str, imports))
    assert attributes <= ENDPOINT_ATTRIBUTE_READERS, sorted(map(str, attributes))


def test_endpoint_holders_open_no_connection_of_their_own():
    """The endpoint holders reach MCS only via the adapter's worker; the
    worker's api branch is the single urllib Request for MCS."""
    for rel in ENDPOINT_IMPORTERS - {ADAPTER, WORKER} | {CONTRACT}:
        names = {node.attr if isinstance(node, ast.Attribute) else node.id
                 for node in ast.walk(_parse(rel))
                 if isinstance(node, (ast.Attribute, ast.Name))}
        assert not names & {"urlopen", "Request", "HTTPSConnection",
                            "HTTPConnection", "build_opener"}, rel


def test_download_builds_no_writing_request():
    """Attachment Requests, including signed redirects, remain bodiless GETs."""
    requests = [node for node in ast.walk(_parse(DOWNLOAD))
                if isinstance(node, ast.Call)
                and (getattr(node.func, "attr", None) == "Request"
                     or getattr(node.func, "id", None) == "Request")]
    assert requests, "download Request vanished — re-point this test"
    bad = [node.lineno for node in requests
           if len(node.args) > 1
           or any(kw.arg in ("data", "method") for kw in node.keywords)]
    assert not bad, f"download Request with a body or method at {bad}"


def test_mcs_origin_literal_is_confined():
    """Only the contract, login guard and attachment policy name MCS hosts."""
    found = set()
    for rel in _production_files():
        for _node, text in _string_constants(_parse(rel)):
            if "medical-care.net" in text:
                found.add(rel)
    assert found <= {ADAPTER, CONTRACT, DOWNLOAD}, sorted(map(str, found))


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


DOM_ACTION = re.compile(r"\.click\(|\.submit\(|\.requestSubmit\(|dispatchEvent\(")
BROWSER_NETWORK = re.compile(
    r"\bfetch\(|XMLHttpRequest|sendBeacon|new WebSocket|\.open\(['\"]POST")


def test_injected_scripts_use_no_browser_networking():
    """An injected script cannot write through the page's own network
    stack (fetch/XHR/beacon/socket) either."""
    hits = [f"{rel}:{node.lineno}" for rel in _production_files()
            for node, text in _string_constants(_parse(rel))
            if BROWSER_NETWORK.search(text)]
    assert not hits, hits


def test_dom_clicks_exist_only_in_the_login_form():
    """The one scripted click is the login submit inside _auto_login (the
    body auto_login wraps with the login backoff), and
    the only dispatched events are the two credential input events; no
    other injected script presses, submits or dispatches anything — not
    even another action added inside _auto_login itself."""
    actions = []
    for rel in _production_files():
        tree = _parse(rel)
        for node, text in _string_constants(tree):
            for line in text.splitlines():
                for _match in DOM_ACTION.finditer(line):
                    fn = _enclosing_function(tree, node)
                    actions.append((rel, fn.name if fn else None,
                                    " ".join(line.split())))
    assert sorted(actions) == sorted([
        (ADAPTER, "_auto_login",
         "idEl.dispatchEvent(new Event('input', {bubbles:true}));"),
        (ADAPTER, "_auto_login",
         "pwEl.dispatchEvent(new Event('input', {bubbles:true}));"),
        (ADAPTER, "_auto_login", "btn.click();"),
    ]), actions
    # The click target is the login form's submit button on the MCS origin.
    click_script = next(text for _n, text in _string_constants(_parse(ADAPTER))
                        if "btn.click()" in text)
    assert "location.origin !== 'https://www.medical-care.net'" in click_script
    assert "button[type=submit]" in click_script


def _cdp_new_target(arg):
    """'/unreads' for f"/json/new?{BASE}/unreads"; None for any other
    shape (plain string, other base, extra interpolation)."""
    if not isinstance(arg, ast.JoinedStr) or len(arg.values) != 3:
        return None
    head, base, tail = arg.values
    if not (isinstance(head, ast.Constant) and head.value == "/json/new?"
            and isinstance(base, ast.FormattedValue)
            and isinstance(base.value, ast.Name) and base.value.id == "BASE"
            and base.format_spec is None and base.conversion == -1
            and isinstance(tail, ast.Constant)):
        return None
    return tail.value


def test_cdp_only_opens_the_unreads_and_login_pages():
    opened, rejected = set(), []
    for rel in _production_files():
        for node in ast.walk(_parse(rel)):
            if not isinstance(node, ast.Call):
                continue
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                texts = [arg.value] if isinstance(arg, ast.Constant) else \
                    [v.value for v in getattr(arg, "values", [])
                     if isinstance(v, ast.Constant)]
                if not any(isinstance(t, str) and "/json/new" in t for t in texts):
                    continue
                page = _cdp_new_target(arg) if rel == ADAPTER else None
                if page is None:
                    rejected.append(f"{rel}:{node.lineno}")
                else:
                    opened.add(page)
    assert not rejected, f"unpinned CDP page open: {rejected}"
    assert opened == {"/unreads", "/authentication/login"}, opened


def test_every_cdp_json_target_is_resolvable():
    """No _cdp_json call takes a computed target, so the page allowlist
    above cannot be bypassed through a variable."""
    bad = []
    for rel, node in _calls_to("_cdp_json"):
        target = node.args[0] if node.args else None
        has_method = len(node.args) > 1 or any(
            kw.arg == "method" for kw in node.keywords)
        method = _method_of(node, 1) if has_method else "GET"
        if isinstance(target, ast.Constant) and method == "GET":
            continue
        if method == "PUT" and _cdp_new_target(target) is not None:
            continue
        if method == "GET" and isinstance(target, ast.JoinedStr) \
                and isinstance(target.values[0], ast.Constant) \
                and target.values[0].value == "/json/close/":
            continue
        bad.append(f"{rel}:{node.lineno}")
    assert not bad, f"unresolved CDP target or method: {bad}"


def test_launchers_never_address_mcs_directly():
    """Shell/launchd entrypoints run on a schedule; any MCS traffic must go
    through the Python adapter pinned above, never a curl in a wrapper."""
    hits = []
    paths = [ROOT / f for f in LAUNCHER_FILES]
    for top in LAUNCHER_ROOTS:
        paths += [p for p in (ROOT / top).rglob("*")
                  if p.is_file() and p.suffix != ".py"
                  and "__pycache__" not in p.parts]
    assert paths
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if "medical-care.net" in text or "/api/v2t" in text:
            hits.append(str(path.relative_to(ROOT)))
    assert not hits, hits


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
def test_mark_read_is_gets_only_on_the_wire(fallback, monkeypatch):
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
    for call in worker.calls[:-1]:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(call["url"]).query)
        assert query["timestamp"] == ["1700000000"], call["url"]
    # The same envelopes through the real worker sink leave as GETs.
    import mcs_util
    import mcs_worker

    sent = []

    class _Opener:
        def open(self, request, timeout=None):
            sent.append(request.get_method())
            raise mcs_worker.urllib.error.HTTPError(
                request.full_url, 404, "synthetic", {}, None)

    monkeypatch.setattr(mcs_util, "no_proxy_opener", lambda *_a: _Opener())
    for call in worker.calls:
        mcs_worker._execute(dict(call, timeout=1))
    assert sent == ["GET"] * len(worker.calls)


def test_worker_sends_the_callers_method_unchanged(monkeypatch):
    """The worker's api sink issues exactly the method the adapter put in
    the envelope — a GET envelope leaves the process as a bodiless GET."""
    import mcs_util
    import mcs_worker

    sent = []

    class _Opener:
        def open(self, request, timeout=None):
            sent.append((request.get_method(), request.data))
            raise mcs_worker.urllib.error.HTTPError(
                request.full_url, 404, "synthetic", {}, None)

    monkeypatch.setattr(mcs_util, "no_proxy_opener", lambda *_a: _Opener())
    result = mcs_worker._execute({
        "operation": "api", "timeout": 1, "method": "GET", "data": None,
        "url": mcs_contract.API + "/projects", "headers": {}})
    assert result["status"] == 404
    assert sent == [("GET", None)]


def test_worker_takes_every_method_straight_from_the_envelope():
    """Statically, too: every Request the worker builds takes method= from
    a name bound only to the envelope's own value — no URL-dependent pick."""
    fn = next(node for node in ast.walk(_parse(Path("mcs/ingest/mcs_worker.py")))
              if isinstance(node, ast.FunctionDef) and node.name == "_execute")
    binds = [node for node in ast.walk(fn)
             if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign,
                                  ast.NamedExpr))
             and any(isinstance(t, ast.Name) and t.id == "method"
                     for t in ast.walk(node) if isinstance(t, ast.Name)
                     and isinstance(t.ctx, ast.Store))]
    assert binds
    for node in binds:
        src = ast.unparse(node.value)
        assert src in ('envelope["method"]', "envelope['method']",
                       "envelope.get('method', 'GET')"), src
    requests = [node for node in ast.walk(fn) if isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "Request"]
    assert requests
    for node in requests:
        kw = [k.value for k in node.keywords if k.arg == "method"]
        assert len(kw) == 1 and isinstance(kw[0], ast.Name) \
            and kw[0].id == "method", ast.unparse(node)


def test_worker_boundary_never_rewrites_the_method():
    """bounded_call serializes the adapter's payload to the subprocess;
    it may add a timeout but never set or overwrite the method."""
    fn = next(node for node in ast.walk(_parse(Path("mcs/ingest/mcs_worker.py")))
              if isinstance(node, ast.FunctionDef) and node.name == "bounded_call")
    writes = [node.lineno for node in ast.walk(fn)
              if (isinstance(node, ast.keyword) and node.arg == "method")
              or (isinstance(node, ast.Dict) and any(
                  isinstance(k, ast.Constant) and k.value == "method"
                  for k in node.keys))
              or (isinstance(node, ast.Subscript)
                  and isinstance(node.ctx, ast.Store)
                  and isinstance(node.slice, ast.Constant)
                  and node.slice.value == "method")]
    assert not writes, f"bounded_call writes method at {writes}"
