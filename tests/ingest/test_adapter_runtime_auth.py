"""Keychain login, isolated adapter workers and atomic attachment downloads."""
import mcs_authentication
import mcs_contract
from contextlib import closing
import errno
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
import mcs_adapter
import mcs_download
from ingest_testkit import _ledger, _message


@pytest.fixture(autouse=True)
def _single_login_account(monkeypatch):
    # Exercise authentication without depending on the runner's login accounts.
    monkeypatch.setattr(mcs_authentication, "_login_accounts",
                        lambda: [SimpleNamespace(pw_uid=os.getuid(), pw_shell="/bin/zsh")])


def test_keychain_password_returns_secret(monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="pw123\n",
                                        stderr=""))
    assert a._keychain_password() == "pw123"


def test_token_cache_never_overwrites_predictable_staging_link(tmp_path):
    cache = tmp_path / "cache" / "token.json"
    cache.parent.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("preserve")
    Path(str(cache) + ".tmp").symlink_to(unrelated)
    adapter = mcs_adapter.MCSAdapter(token_cache=str(cache))
    adapter._write_cache("synthetic-token")
    assert unrelated.read_text() == "preserve"
    assert adapter._read_cache() == "synthetic-token"
    assert cache.stat().st_mode & 0o777 == 0o600


def test_download_uses_private_staging_without_touching_existing_part_link(tmp_path):
    destination = tmp_path / "file"
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("preserve")
    Path(str(destination) + ".part").symlink_to(unrelated)

    def worker(payload, **kwargs):
        Path(payload["partial"]).write_bytes(b"synthetic")
        return {"bytes": 9, "sha256": hashlib.sha256(b"synthetic").hexdigest()}

    adapter = mcs_adapter.MCSAdapter(worker=worker)
    adapter._token = "synthetic-token"
    adapter.download("https://www.medical-care.net/f", str(destination))
    assert unrelated.read_text() == "preserve"
    assert destination.read_bytes() == b"synthetic"
    assert not destination.is_symlink()
    assert destination.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("sync_failed", [False, True])
def test_download_syncs_renamed_file_before_recording_downloaded(tmp_path, monkeypatch, sync_failed):
    import collection_stages
    import health_report
    import safe_io

    payload = b"synthetic attachment"
    events = []
    replace = mcs_adapter.os.replace
    sync = safe_io.fsync_dir

    def worker(envelope, **kwargs):
        Path(envelope["partial"]).write_bytes(payload)
        return {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}

    adapter = mcs_adapter.MCSAdapter(worker=worker)
    adapter._token = "synthetic-token"
    monkeypatch.setattr(collection_stages, "ATTACH_DIR", str(tmp_path))
    monkeypatch.setattr(health_report, "attachment_free_mb", lambda *_: None)
    with closing(_ledger(tmp_path)) as store:
        message = _message()
        message.attachments = [mcs_contract.Attachment(
            "synthetic-file", "synthetic.pdf", "https://www.medical-care.net/f")]
        store.save_messages([message])

        def renamed(source, destination):
            replace(source, destination)
            events.append("renamed")

        def synced(directory):
            assert directory == str(tmp_path)
            assert events == ["renamed"]
            row = store.db.execute("SELECT attachment_id,state FROM attachments").fetchone()
            assert row["state"] == "pending"
            assert (tmp_path / str(row["attachment_id"])).read_bytes() == payload
            events.append("directory_synced")
            if sync_failed:
                raise OSError(errno.EIO, "synthetic directory sync failure")
            sync(directory)

        monkeypatch.setattr(mcs_adapter.os, "replace", renamed)
        monkeypatch.setattr(mcs_adapter, "fsync_dir", synced, raising=False)
        result = {"errors": []}
        collection_stages.stage_attachments(adapter, store, result, time.monotonic() + 60)
        assert events == ["renamed", "directory_synced"]
        row = store.db.execute("SELECT state,local_path,sha256 FROM attachments").fetchone()
        assert row["state"] == ("pending" if sync_failed else "downloaded")
        assert (row["local_path"] is None) is sync_failed
        assert (row["sha256"] is None) is sync_failed
        assert bool(result["errors"]) is sync_failed
        assert not list(tmp_path.glob(".download-*.part"))


@pytest.mark.parametrize("operation", ["api", "cdp_json"])
def test_worker_rejects_oversized_json_response(monkeypatch, operation):
    import io
    import mcs_worker
    import mcs_util

    class Response(io.BytesIO):
        status = 200
        headers = {}

    response = Response(b'"' + b"x" * 64 + b'"')
    monkeypatch.setattr(mcs_worker, "MAX_JSON_BYTES", 64)
    monkeypatch.setattr(mcs_util, "no_proxy_opener",
                        lambda *args: SimpleNamespace(open=lambda *a, **k: response))
    with pytest.raises(mcs_worker.WorkerError) as error:
        mcs_worker._execute({"operation": operation, "timeout": 1,
                             "url": mcs_contract.API + "/projects" if operation == "api"
                             else "http://127.0.0.1:9333/json/list",
                             "method": "GET", "headers": {}})
    assert error.value.kind == "response_too_large"
    assert response.closed


def test_keychain_password_missing_returns_none(monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=44, stdout="",
                                        stderr="could not be found"))
    assert a._keychain_password() is None


def test_keychain_password_locked_raises(monkeypatch, tmp_path):
    """A locked keychain (rc 36 / interaction-not-allowed) must surface as
    KeychainLocked — it is recoverable by unlock, NOT a missing entry."""
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=36, stdout="", stderr=""))
    with pytest.raises(mcs_adapter.KeychainLocked):
        a._keychain_password()


def test_keychain_password_locked_by_stderr_text(monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: SimpleNamespace(
            returncode=1, stdout="",
            stderr="User interaction is not allowed"))
    with pytest.raises(mcs_adapter.KeychainLocked):
        a._keychain_password()


def test_auto_login_reports_keychain_locked(monkeypatch, tmp_path):
    """auto_login must distinguish a locked keychain from a missing
    credential — the run alert then names the real recovery action."""
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(mcs_adapter.time, "sleep", lambda s: None)
    monkeypatch.setattr(a, "_ensure_chrome", lambda *a, **k: None)
    monkeypatch.setattr(a, "_login_page",
                        lambda: {"webSocketDebuggerUrl": "ws://x"})
    monkeypatch.setattr(a, "_cdp_eval", lambda ws, expr: "need_both")

    def locked(*a, **k):
        raise mcs_adapter.KeychainLocked("mcs-adapter")
    monkeypatch.setattr(a, "_keychain_password", locked)
    monkeypatch.setattr(a, "_recover_session", lambda: False)
    assert a.auto_login() == "keychain_locked"


def test_auto_login_reports_manual_required_when_entry_missing(
        monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(mcs_adapter.time, "sleep", lambda s: None)
    monkeypatch.setattr(a, "_ensure_chrome", lambda *a, **k: None)
    monkeypatch.setattr(a, "_login_page",
                        lambda: {"webSocketDebuggerUrl": "ws://x"})
    monkeypatch.setattr(a, "_cdp_eval", lambda ws, expr: "need_both")
    monkeypatch.setattr(a, "_keychain_password", lambda *a, **k: None)
    monkeypatch.setattr(a, "_recover_session", lambda: False)
    assert a.auto_login() == "manual_required"


def test_auto_login_recovers_live_session_without_form(
        monkeypatch, tmp_path):
    """Token expiry is not logout — when the browser session is still
    alive, a fresh localStorage token + check_session ends the recovery
    before any form fill is attempted."""
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(a, "_ensure_chrome", lambda *a, **k: None)
    monkeypatch.setattr(a, "_token_via_cdp", lambda: "tok-12345678")
    monkeypatch.setattr(a, "check_session", lambda: True)
    monkeypatch.setattr(
        a, "_login_page",
        lambda: pytest.fail("login form must not be touched"))
    assert a.auto_login() == "ok"
    assert a._token == "tok-12345678"


def test_auto_login_no_form_means_redirected_when_session_live(
        monkeypatch, tmp_path):
    """A logged-in app redirects /authentication/login back home, so a
    missing form is evidence of a session — not of failure. Recover via
    the second live-session check instead of alerting."""
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(mcs_adapter.time, "sleep", lambda s: None)
    monkeypatch.setattr(a, "_ensure_chrome", lambda *a, **k: None)
    monkeypatch.setattr(a, "_login_page",
                        lambda: {"webSocketDebuggerUrl": "ws://x"})
    monkeypatch.setattr(a, "_cdp_eval", lambda ws, expr: "no_form")
    seq = iter([False, True])      # pre-check fails, post-no_form ok
    monkeypatch.setattr(a, "_recover_session", lambda: next(seq))
    assert a.auto_login() == "ok"


def test_auto_login_no_form_and_dead_session_is_manual(
        monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(mcs_adapter.time, "sleep", lambda s: None)
    monkeypatch.setattr(a, "_ensure_chrome", lambda *a, **k: None)
    monkeypatch.setattr(a, "_login_page",
                        lambda: {"webSocketDebuggerUrl": "ws://x"})
    monkeypatch.setattr(a, "_cdp_eval", lambda ws, expr: "no_form")
    monkeypatch.setattr(a, "_recover_session", lambda: False)
    assert a.auto_login() == "manual_required:no_form"


@pytest.mark.parametrize("operation", ["api", "download", "bootstrap"])
def test_adapter_worker_deadline_reaps_slow_reader(tmp_path, monkeypatch, operation):
    """A blocked read cannot retain a worker or a partial attachment."""
    import mcs_worker
    script = tmp_path / "slow_worker.py"
    started_file = tmp_path / "reader-started"
    module_root = Path(mcs_adapter.__file__).resolve().parents[1]
    script.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(module_root)!r})\n"
        "import _mcs_path, mcs_download, mcs_worker, mcs_util\n"
        "class SlowResponse:\n"
        "    status = 200\n"
        "    headers = {}\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *args): pass\n"
        "    def read(self, *args):\n"
        f"        with open({str(started_file)!r}, 'w') as f: f.write('started')\n"
        "        while True: time.sleep(0.02)\n"
        "class Opener:\n"
        "    def open(self, *args, **kwargs): return SlowResponse()\n"
        "mcs_download.no_proxy_opener = lambda *args: Opener()\n"
        "mcs_util.no_proxy_opener = lambda *args: Opener()\n"
        "raise SystemExit(mcs_worker.worker_main())\n", encoding="utf-8")
    monkeypatch.setattr(mcs_worker, "_worker_command",
                        lambda: [sys.executable, str(script)])
    processes = []
    original_popen = subprocess.Popen

    def spawn(command, **kwargs):
        assert "synthetic-bearer" not in repr(command)
        assert "SYNTHETIC_PASSWORD" not in kwargs["env"]
        process = original_popen(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setenv("SYNTHETIC_PASSWORD", "synthetic-only")
    monkeypatch.setattr(mcs_worker.subprocess, "Popen", spawn)
    adapter = mcs_adapter.MCSAdapter()
    adapter._token = None if operation == "bootstrap" else "synthetic-bearer"
    # room for a loaded runner to start the worker and reach the read;
    # the reader then blocks forever, so only the deadline can end it
    adapter.set_deadline(time.monotonic() + 5)
    destination = tmp_path / "downloaded"
    destination.write_bytes(b"previous-complete-file")
    started = time.monotonic()
    if operation == "download":
        def call():
            adapter.download("https://www.medical-care.net/f",
                             str(destination))
    else:
        def call():
            adapter._request("GET", "/projects", retries=0)
    with pytest.raises(mcs_contract.MCSError) as error:
        call()
    assert error.value.kind == "deadline_exceeded"
    assert time.monotonic() - started < 20
    assert started_file.exists()
    assert len(processes) == 1 and processes[0].poll() is not None
    assert destination.read_bytes() == b"previous-complete-file"
    assert not Path(str(destination) + ".part").exists()
    assert not list(tmp_path.glob(".download-*.part"))


def test_adapter_worker_preserves_api_form_and_http_status(monkeypatch):
    import base64
    import io
    import urllib.error
    import urllib.parse
    import mcs_worker
    import mcs_util

    observed = []

    class Response(io.BytesIO):
        status = 200
        headers = {}

    class Opener:
        def open(self, request, timeout):
            observed.append(request)
            body = (b'{"project":{"is_archived":false}}'
                    if request.full_url.endswith("/projects/1")
                    else b'{"messages":[],"paginate":{}}')
            return Response(body)

    def opener(*handlers):
        assert handlers == (mcs_util.NoRedirect,)
        return Opener()

    monkeypatch.setattr(mcs_util, "no_proxy_opener", opener)
    adapter = mcs_adapter.MCSAdapter(worker=lambda payload, timeout, deadline:
        mcs_worker._execute(dict(payload, timeout=timeout)))
    adapter._token = "synthetic-bearer"
    assert adapter.mark_patient_read(1, 123)["project"]["is_archived"] is False
    # the mark is a GET on the message list — no body, Bearer header only —
    # carrying the same unread/timestamp filter the fetch used
    mark = observed[0]
    assert mark.data is None
    assert mark.get_method() == "GET"
    assert mark.get_header("Authorization") == "Bearer synthetic-bearer"
    q = urllib.parse.parse_qs(urllib.parse.urlparse(mark.full_url).query)
    assert q["unread"] == ["1"] and q["timestamp"] == ["123"]

    class ErrorBody:
        def read(self, *args):
            raise AssertionError("error body must never be read")

        def close(self):
            pass

    def denied(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "synthetic", {}, ErrorBody())

    monkeypatch.setattr(mcs_util, "no_proxy_opener",
                        lambda *handlers: SimpleNamespace(open=denied))
    with pytest.raises(mcs_contract.SessionExpired) as error:
        adapter._request("GET", "/projects", retries=0)
    assert error.value.status == 401

    # The explicit worker seam keeps retry policy in the parent adapter.
    statuses = iter([503, 200])
    ok_body = b'{"ok":true}'
    adapter._worker = lambda payload, **kwargs: {
        "status": next(statuses), "headers": {},
        "body": base64.b64encode(ok_body).decode("ascii")}
    monkeypatch.setattr(adapter, "_sleep_bounded", lambda seconds: None)
    assert adapter._request("GET", "/projects")[1] == ok_body


def test_attachment_worker_keeps_redirect_and_atomic_file_contract(tmp_path, monkeypatch):
    import io
    import urllib.error
    import mcs_worker
    urls = []
    authorizations = []

    class Opener:
        def open(self, request, timeout):
            urls.append(request.full_url)
            authorizations.append(request.get_header("Authorization"))
            if len(urls) == 1:
                raise urllib.error.HTTPError(request.full_url, 302, "synthetic", {
                    "location": "https://files.medical-care.net/signed"}, None)
            return io.BytesIO(b"synthetic-attachment")

    monkeypatch.setattr(mcs_download, "no_proxy_opener", lambda *handlers: Opener())
    destination = tmp_path / "file"
    destination.write_bytes(b"old")

    def worker(payload, timeout, deadline):
        result = mcs_worker._execute(dict(payload, timeout=timeout))
        assert destination.read_bytes() == b"old"
        assert Path(payload["partial"]).read_bytes() == b"synthetic-attachment"
        return result

    adapter = mcs_adapter.MCSAdapter(worker=worker)
    adapter._token = "synthetic-bearer"
    result = adapter.download("https://www.medical-care.net/f", str(destination))
    assert authorizations == ["Bearer synthetic-bearer", None]
    assert destination.read_bytes() == b"synthetic-attachment"
    assert result == {"bytes": len(b"synthetic-attachment"),
                      "sha256": hashlib.sha256(b"synthetic-attachment").hexdigest()}
    assert not Path(str(destination) + ".part").exists()

    monkeypatch.setattr(mcs_download, "_MAX_DOWNLOAD_BYTES", 2)
    with pytest.raises(mcs_contract.MCSError) as error:
        adapter.download("https://www.medical-care.net/f", str(destination))
    assert error.value.kind == "download_too_large"
    assert not Path(str(destination) + ".part").exists()
    assert destination.read_bytes() == b"synthetic-attachment"
    with pytest.raises(mcs_contract.MCSError) as error:
        adapter.download("https://outside.invalid/f", str(destination))
    assert error.value.kind == "url_not_allowed"


def test_bootstrap_and_keychain_use_remaining_budget(monkeypatch):
    observed = []

    def worker(payload, timeout, deadline):
        observed.append((payload["operation"], timeout))
        assert 0 < timeout <= 1
        if payload["operation"] == "cdp_json":
            return {"value": [{"type": "page", "url": "https://www.medical-care.net/",
                              "webSocketDebuggerUrl": "ws://127.0.0.1:9333/x"}]}
        return {"value": '"synthetic-token"'}

    adapter = mcs_adapter.MCSAdapter(worker=worker)
    adapter.set_deadline(time.monotonic() + 1)
    assert adapter.bootstrap_token() == "synthetic-token"
    assert [operation for operation, _ in observed] == ["cdp_json", "cdp_eval"]

    def keychain(command, **kwargs):
        assert 0 < kwargs["timeout"] <= 1
        return SimpleNamespace(returncode=0, stdout="synthetic-password", stderr="")

    monkeypatch.setattr(subprocess, "run", keychain)
    assert adapter._keychain_password() == "synthetic-password"
    adapter.set_deadline(time.monotonic() - 1)
    with pytest.raises(mcs_contract.MCSError) as error:
        adapter.bootstrap_token()
    assert error.value.kind == "deadline_exceeded"
    assert len(observed) == 2


def test_keychain_timeout_preserves_fallback_only_with_run_budget(monkeypatch):
    adapter = mcs_adapter.MCSAdapter()

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    monkeypatch.setattr(mcs_authentication, "env_value", lambda key: "synthetic-fallback")
    adapter.set_deadline(time.monotonic() + 60)
    assert adapter._login_password() == ("synthetic-fallback", True)
    adapter.set_deadline(time.monotonic() - 1)
    with pytest.raises(mcs_contract.MCSError) as error:
        adapter._login_password()
    assert error.value.kind == "deadline_exceeded"


def test_auto_login_resets_per_attempt_journal_facts(monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    a.healed_tabs, a.chrome_restarted = 3, True
    monkeypatch.setattr(a, "_ensure_chrome", lambda *a, **k: None)
    monkeypatch.setattr(a, "_recover_session", lambda: True)
    assert a.auto_login() == "ok"
    assert (a.healed_tabs, a.chrome_restarted) == (0, False)


# Synthetic regressions: auto_login returns a state, download bootstraps.

def _net_down(payload, timeout, deadline):
    raise mcs_contract.MCSError("network_error", retryable=True)


def test_auto_login_expired_deadline_returns_failed():
    a = mcs_adapter.MCSAdapter(worker=_net_down, token_cache="/nonexistent/x")
    a.set_deadline(time.monotonic() - 1)
    assert a.auto_login("/x", "/x") == "failed"


def test_auto_login_poll_check_session_error_returns_failed(monkeypatch):
    a = mcs_adapter.MCSAdapter(worker=_net_down, token_cache="/nonexistent/x")
    monkeypatch.setattr(a, "_ensure_chrome", lambda *_: None)
    monkeypatch.setattr(a, "_recover_session", lambda: False)
    monkeypatch.setattr(a, "_login_page",
                        lambda: {"webSocketDebuggerUrl": "ws://127.0.0.1/x"})
    monkeypatch.setattr(a, "_sleep_bounded", lambda s: None)
    monkeypatch.setattr(a, "_cdp_eval",
                        lambda ws, js: "ready" if "no_form" in js else "clicked")
    monkeypatch.setattr(a, "_token_via_cdp", lambda: "t" * 32)

    def boom():
        raise mcs_contract.MCSError("network_error", retryable=True)
    monkeypatch.setattr(a, "check_session", boom)
    assert a.auto_login("/x", "/x", wait_s=5) == "failed"


def test_download_bootstraps_token(monkeypatch, tmp_path):
    calls = []

    def worker(payload, timeout, deadline):
        calls.append(payload["token"])
        with open(payload["partial"], "wb") as f:
            f.write(b"x")
        return {"bytes": 1}
    a = mcs_adapter.MCSAdapter(worker=worker, token_cache=None)
    monkeypatch.setattr(a, "_token_via_cdp", lambda: "t" * 32)
    dest = tmp_path / "f.bin"
    a.download("https://www.medical-care.net/files/1", str(dest))
    assert calls == ["t" * 32] and dest.read_bytes() == b"x"
