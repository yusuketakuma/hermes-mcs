"""Attachment text uses synthetic files, a temporary ledger, and a stub OCR runtime."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace
import sys

import pytest

import attachment_ocr as ocr


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "attachments").mkdir(parents=True)
    path = root / "attachments" / "1"
    blob = b"\x89PNG\r\n\x1a\nsynthetic image bytes"
    path.write_bytes(blob)
    db = sqlite3.connect(root / "ledger.db")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE messages(message_id INTEGER PRIMARY KEY,project_id INTEGER,body_state TEXT,parent_id INTEGER,body_text TEXT,posted_at_ts INTEGER);
        CREATE TABLE patients(project_id INTEGER PRIMARY KEY,is_archived INTEGER);
        CREATE TABLE notification_meta(singleton INTEGER PRIMARY KEY,notify_dirty INTEGER);
        CREATE TABLE attachments(attachment_id INTEGER PRIMARY KEY,message_id INTEGER,state TEXT,sha256 TEXT,bytes INTEGER,local_path TEXT);
        CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY,kind TEXT,project_id INTEGER,message_id INTEGER,content TEXT,model TEXT,meta TEXT,created_at REAL);
        INSERT INTO messages VALUES(10,20,'full',NULL,'chat stays separate',1);
    """)
    db.execute("INSERT INTO attachments VALUES(1,10,'downloaded',?,?,?)",
               (hashlib.sha256(blob).hexdigest(), len(blob), str(path)))
    db.commit()
    ledger = SimpleNamespace(db=db, path=str(root / "ledger.db"))
    monkeypatch.setattr(ocr.os, "access", lambda *args: True)
    monkeypatch.setattr(ocr.os.path, "isfile", lambda path: True)
    monkeypatch.setattr(ocr.writer_lock, "updater_state", lambda: (False, 1))
    pages = [{"page": 1, "engine": "yomitoku", "text": "合成資料", "text_hash": ocr._hash("合成資料")}]
    calls = []

    def runtime(executable, snapshot, output, timeout):
        assert Path(snapshot).name == "source.png"
        assert Path(snapshot).read_bytes() == blob
        assert Path(snapshot).stat().st_mode & 0o777 == 0o600
        assert not ledger.db.in_transaction
        # The shared lock is available during the expensive runtime.
        fd = ocr.writer_lock.acquire_run_lock(str(root / "run.lock"))
        assert fd is not None
        os.close(fd)
        calls.append(str(snapshot))
        return pages

    monkeypatch.setattr(ocr, "_runtime", runtime)
    cfg = {"attachment_ocr": {"enabled": True, "executable": "/synthetic/venv/bin/yomitoku"}}
    yield ledger, cfg, pages, calls, path
    db.close()


def test_run_separate_source_deduplicates_and_detail_only(setup):
    ledger, cfg, pages, calls, _ = setup
    assert ocr.run_pending(ledger, cfg)["complete"] == 1
    assert ocr.run_pending(ledger, cfg)["complete"] == 0
    assert len(calls) == 1
    assert not Path(calls[0]).exists()
    doc = ocr.current(ledger.db, 1, project_id=20)
    assert doc["pages"] == pages
    assert (doc["project_id"], doc["message_id"], doc["artifact_id"]) == (20, 10, 1)
    assert ocr.current(ledger.db, 1, project_id=21) is None
    assert "pages" not in ocr.view(ledger.db, 1)
    assert "合成資料" not in json.dumps(ocr.view(ledger.db, 1), ensure_ascii=False)
    assert ocr.view(ledger.db, 1, include_text=True)["pages"] == pages
    assert ledger.db.execute("SELECT body_text FROM messages").fetchone()[0] == "chat stays separate"


@pytest.mark.parametrize("state,usable", [("pruned", True), ("withdrawn", False), ("failed", False), ("pending", False)])
def test_current_retention_and_withdrawal(setup, state, usable):
    ledger, cfg, _, _, path = setup
    ocr.run_pending(ledger, cfg)
    path.unlink()
    ledger.db.execute("UPDATE attachments SET state=?", (state,))
    ledger.db.commit()
    doc = ocr.current(ledger.db, 1)
    assert bool(doc) is usable
    if usable:
        assert doc["original_available"] is False


@pytest.mark.parametrize("mutation", ["UPDATE messages SET body_state='deleted'", "UPDATE attachments SET sha256='changed'",
                                      "UPDATE attachments SET bytes=1", "UPDATE messages SET project_id=21"])
def test_source_change_during_processing_rejects_result(setup, monkeypatch, mutation):
    ledger, cfg, pages, _, _ = setup

    def runtime(*args):
        ledger.db.execute(mutation)
        ledger.db.commit()
        return pages

    monkeypatch.setattr(ocr, "_runtime", runtime)
    assert ocr.run_pending(ledger, cfg)["deferred"] == 1
    assert ledger.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_empty_is_completed_not_unprocessed(setup):
    ledger, cfg, pages, _, _ = setup
    pages[0].update(text="", text_hash=ocr._hash(""))
    assert ocr.run_pending(ledger, cfg)["empty"] == 1
    assert ocr.current(ledger.db, 1)["state"] == "empty"


def test_detail_medical_categories_use_exact_source_and_aggregate_is_text_free(setup):
    ledger, cfg, pages, _, _ = setup
    text = "【傷病名】\n合成疾患\n【PCAポンプ】\n残量: 12.5 mL\n"
    pages[0].update(text=text, text_hash=ocr._hash(text))
    ocr.run_pending(ledger, cfg)
    before = ledger.db.execute("SELECT content FROM artifacts").fetchone()[0]
    detail = ocr.view(ledger.db, 1, include_text=True)
    assert detail["pages"][0]["text"] == text
    medical = detail["medical"]
    assert any(section["category"] == "diagnosis" for section in medical["sections"])
    candidate = medical["pump_candidates"][0]
    start, end = candidate["evidence_span"]
    assert text[start:end] == candidate["evidence"]
    assert candidate["number"] == "12.5" and candidate["unit"] == "mL"
    assert candidate["status"] == "literal_candidate"
    aggregate = ocr.view(ledger.db, 1, include_text=False)
    assert "medical" not in aggregate and "pages" not in aggregate
    assert "合成疾患" not in json.dumps(aggregate, ensure_ascii=False)
    assert ledger.db.execute("SELECT content FROM artifacts").fetchone()[0] == before


def test_timeout_is_sanitized_and_backed_off(setup, monkeypatch):
    ledger, cfg, _, _, _ = setup

    def runtime(*args):
        raise subprocess.TimeoutExpired("private command", 1, output="never save this")

    monkeypatch.setattr(ocr, "_runtime", runtime)
    assert ocr.run_pending(ledger, cfg)["failed"] == 1
    assert ocr.current(ledger.db, 1) is None
    assert ocr.view(ledger.db, 1)["state"] == "failed"
    assert ocr.run_pending(ledger, cfg)["failed"] == 0
    raw = ledger.db.execute("SELECT content FROM artifacts").fetchone()[0]
    assert "never save this" not in raw and "private command" not in raw


def test_unsupported_magic_does_not_start_runtime(setup):
    ledger, cfg, _, calls, path = setup
    path.write_bytes(b"synthetic unsupported")
    raw = path.read_bytes()
    ledger.db.execute("UPDATE attachments SET bytes=?,sha256=?", (len(raw), hashlib.sha256(raw).hexdigest()))
    ledger.db.commit()
    assert ocr.run_pending(ledger, cfg)["unsupported"] == 1
    assert not calls
    assert ocr.run_pending(ledger, cfg)["unsupported"] == 0


def test_hash_mismatch_never_starts_runtime(setup):
    ledger, cfg, _, calls, path = setup
    path.write_bytes(b"changed bytes")
    assert ocr.run_pending(ledger, cfg)["failed"] == 1
    assert not calls


def test_disabled_and_unavailable_do_not_scan_or_write(setup, monkeypatch):
    ledger, cfg, _, calls, _ = setup
    assert ocr.run_pending(None, {})["reason"] == "disabled"
    monkeypatch.setattr(ocr.os.path, "isfile", lambda path: False)
    assert ocr.run_pending(ledger, cfg)["reason"] == "runtime_unavailable"
    assert ocr.run_pending(ledger, cfg)["reason"] == "runtime_unavailable"
    assert not calls and ledger.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_newer_failure_or_tamper_never_falls_back(setup):
    ledger, cfg, _, _, _ = setup
    ocr.run_pending(ledger, cfg)
    ledger.db.execute("UPDATE artifacts SET content=json_set(content,'$.pages[0].text','changed')")
    ledger.db.commit()
    assert ocr.current(ledger.db, 1) is None
    assert ocr.run_pending(ledger, cfg)["complete"] == 1
    assert ocr.current(ledger.db, 1)


def test_yomitoku_json_paragraphs_tables_figures_and_pdf_text():
    value = {"engine_version": ocr.ENGINE_VERSION, "pages": [
        {"page": 1, "engine": "pdf_text", "text": "合成PDF本文\r\n"},
        {"page": 2, "engine": "yomitoku", "document": {
            "paragraphs": [{"order": 0, "contents": "段落"}],
            "tables": [{"order": 1, "cells": [{"row": 1, "col": 0, "contents": "下段"},
                                                     {"row": 0, "col": 0, "contents": "上段"}]}],
            "figures": [{"order": 2, "paragraphs": [{"order": None, "contents": "図内の文字"}]}]}}]}
    pages = ocr._parse_output(json.dumps(value).encode())
    assert pages[0]["text"] == "合成PDF本文\r\n"
    assert pages[1]["text"] == "段落\n上段\n下段\n図内の文字"
    assert all(page["text_hash"] == ocr._hash(page["text"]) for page in pages)


def test_document_table_preserves_sparse_rows_columns_and_empty_cells():
    cells = [{"row": 0, "col": 0, "contents": "項目"},
             {"row": 0, "col": 2, "contents": "用量"},
             {"row": 1, "col": 0, "contents": "合成A"},
             {"row": 1, "col": 15, "contents": "5 mg"},
             {"row": 3, "col": 1, "contents": None}]
    text = ocr._document_text({"paragraphs": [], "tables": [{"cells": list(reversed(cells))}]})
    assert text == "項目\t\t用量\n合成A" + "\t" * 15 + "5 mg\n\n\t"
    assert text.splitlines()[1].split("\t")[2] == ""


def test_document_table_spans_reserve_blank_positions_without_repeating_text():
    cells = [{"row": 0, "col": 0, "contents": "合成見出し", "row_span": 2, "col_span": 2},
             {"row": 0, "col": 2, "contents": "用量"},
             {"row": 1, "col": 2, "contents": "5 mg"}]
    assert ocr._document_text({"paragraphs": [], "tables": [{"cells": cells}]}) == "合成見出し\t\t用量\n\t\t5 mg"


@pytest.mark.parametrize("cell", [
    {"row": -1}, {"col": -1}, {"row": True}, {"col": False},
    {"row": 10 ** 100}, {"col": 10 ** 100},
    {"row_span": 0}, {"col_span": -1}, {"row_span": True}, {"col_span": 1.5},
    {"row_span": 10 ** 100}, {"col_span": 10 ** 100},
])
def test_document_table_rejects_invalid_indices_and_spans(cell):
    with pytest.raises(ValueError, match="output_schema"):
        ocr._document_text({"paragraphs": [], "tables": [{"cells": [{"row": 0, "col": 0, **cell}]}]})


@pytest.mark.parametrize("first,second", [
    ({}, {}), ({"row_span": 2}, {"row": 1}), ({"col_span": 2}, {"col": 1}),
])
def test_document_table_rejects_duplicate_or_overlapping_cells(first, second):
    cells = [{"row": 0, "col": 0, **first}, {"row": 0, "col": 0, **second}]
    with pytest.raises(ValueError, match="output_schema"):
        ocr._document_text({"paragraphs": [], "tables": [{"cells": cells}]})


def test_document_table_bounds_padding_and_combined_blocks(monkeypatch):
    monkeypatch.setattr(ocr, "MAX_OUTPUT", 16)
    document = {"paragraphs": [], "tables": [{"cells": [{"row": 0, "col": 16, "contents": "x"}]}]}
    with pytest.raises(ValueError, match="output_schema"):
        ocr._document_text(document)
    document["tables"][0]["cells"][0]["col"] = 15
    assert ocr._document_text(document) == "\t" * 15 + "x"
    document["paragraphs"] = [{"contents": "x"}]
    with pytest.raises(ValueError, match="output_schema"):
        ocr._document_text(document)


@pytest.mark.parametrize("value", [{}, {"engine_version": "0.5.1", "pages": []},
                                    {"engine_version": ocr.ENGINE_VERSION, "pages": [{"page": 2, "engine": "pdf_text", "text": "x"}]}])
def test_rejects_invalid_runtime_output(value):
    with pytest.raises(ValueError):
        ocr._parse_output(json.dumps(value))


def test_runtime_timeout_kills_group_without_capturing_private_output(tmp_path, monkeypatch):
    killed = []
    process = SimpleNamespace(pid=1234, returncode=None)

    def wait(timeout):
        raise subprocess.TimeoutExpired("stub", timeout)

    process.wait = wait

    def popen(command, **kwargs):
        assert command[0] == "/usr/bin/sandbox-exec" and "(deny network*)" in command[2]
        assert "(deny file-read*)" in command[2]
        assert kwargs["stdout"] == subprocess.DEVNULL and kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        assert "TOKEN" not in kwargs["env"] and kwargs["start_new_session"]
        return process

    monkeypatch.setattr(ocr.subprocess, "Popen", popen)
    monkeypatch.setattr(ocr.os, "killpg", lambda *args: killed.append(args))
    monkeypatch.setattr(ocr, "reap_worker", lambda process: None)
    with pytest.raises(subprocess.TimeoutExpired):
        ocr._runtime("/synthetic/bin/yomitoku", tmp_path / "source.pdf", tmp_path / "out.json", 1)
    assert killed


def test_runtime_rejects_symlink_output(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.write_text("{}")
    output = tmp_path / "out.json"
    output.symlink_to(target)
    process = SimpleNamespace(pid=1234, returncode=0, wait=lambda **kwargs: None)
    monkeypatch.setattr(ocr.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(ocr.os, "killpg", lambda *args: None)
    with pytest.raises(OSError):
        ocr._runtime("/synthetic/bin/yomitoku", tmp_path / "source.pdf", output, 1)


@pytest.mark.parametrize("config", [{"enabled": "yes"}, {"enabled": True, "executable": "relative"},
                                    {"timeout_seconds": float("nan")}, {"timeout_seconds": 601}, {"unknown": 1}])
def test_invalid_configuration(config):
    assert ocr.config_error({"attachment_ocr": config})


def test_bootstrap_is_valid_python_and_has_predecode_limits():
    compile(ocr._BOOTSTRAP, "ocr-runtime", "exec")
    assert ocr._BOOTSTRAP.index("textpage.get_text_range") < ocr._BOOTSTRAP.index("page.render")
    assert "rendered > 80000000" in ocr._BOOTSTRAP
    assert "image.width * image.height <= 40000000" in ocr._BOOTSTRAP
    assert '"infer_onnx": False' in ocr._BOOTSTRAP


def _bootstrap_pdf(tmp_path, monkeypatch, texts, sizes=None, image_pages=(), raster_pages=()):
    """Execute only the optional runtime's preflight using fully synthetic modules."""
    import importlib.metadata

    class TextPage:
        def __init__(self, text):
            self.text = text

        def close(self):
            pass

        def count_chars(self):
            return len(self.text)

        def get_text_range(self):
            return self.text

    class Page(TextPage):
        def __init__(self, text, index):
            super().__init__(text)
            self.index = index

        def get_size(self):
            return (sizes or [(72, 72)] * len(texts))[self.index]

        def get_textpage(self):
            return TextPage(self.text)

        def render(self, **kwargs):
            assert self.index in (*image_pages, *raster_pages), "preflight rasterized an unexpected synthetic page"
            return Bitmap("")

        def get_objects(self, **kwargs):
            return [SimpleNamespace(type=3, level=0)] if self.index in image_pages else []

    class Bitmap(TextPage):
        def to_pil(self):
            return SimpleNamespace(convert=lambda mode: "synthetic raster")

    class Analyzer:
        def __init__(self, **kwargs):
            configs = kwargs["configs"]
            for section in configs.values():
                for model in section.values():
                    config = json.loads(Path(model["path_cfg"]).read_text())
                    assert config["hf_hub_repo"].startswith(str(tmp_path / "synthetic-model-hub"))

        def __call__(self, image):
            assert image == "synthetic raster"
            return SimpleNamespace(model_dump=lambda: {"paragraphs": [
                {"order": 0, "contents": "合成画像内文字"}], "tables": []}), None, None

    class Array:
        def __getitem__(self, key):
            return "synthetic raster"

    class Document(TextPage):
        def __len__(self):
            return len(texts)

        def __getitem__(self, index):
            return Page(texts[index], index)

    monkeypatch.setattr(importlib.metadata, "version", lambda name: ocr.ENGINE_VERSION)
    monkeypatch.setitem(sys.modules, "pypdfium2", SimpleNamespace(PdfDocument=Document))
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace(setNumThreads=lambda count: None))
    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace(array=lambda image: Array()))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(set_num_threads=lambda count: None,
                                                             set_num_interop_threads=lambda count: None))
    monkeypatch.setitem(sys.modules, "yomitoku", SimpleNamespace(DocumentAnalyzer=Analyzer))
    def snapshot_download(*, repo_id, local_files_only):
        assert local_files_only is True
        assert repo_id.startswith("KotaroKinoshita/")
        assert "cell-detector" not in repo_id
        return str(tmp_path / "synthetic-model-hub" / repo_id.rsplit("/", 1)[1])

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=snapshot_download))
    monkeypatch.setitem(sys.modules, "resource", SimpleNamespace(RLIMIT_FSIZE=1, RLIMIT_CORE=2, setrlimit=lambda *args: None))
    output = tmp_path / "result.json"
    monkeypatch.setattr(sys, "argv", ["bootstrap", str(tmp_path / "source.pdf"), str(output)])
    exec(compile(ocr._BOOTSTRAP, "synthetic-ocr-bootstrap", "exec"), {})
    return json.loads(output.read_text())


def test_bootstrap_pdf_text_layer_skips_ocr_and_preserves_pages(tmp_path, monkeypatch):
    result = _bootstrap_pdf(tmp_path, monkeypatch, ["合成第一頁\r\n", "合成第二頁"])
    assert result["pages"] == [{"page": 1, "engine": "pdf_text", "text": "合成第一頁\r\n"},
                               {"page": 2, "engine": "pdf_text", "text": "合成第二頁"}]


def test_bootstrap_nonempty_pdf_text_with_embedded_image_still_uses_ocr(tmp_path, monkeypatch):
    result = _bootstrap_pdf(tmp_path, monkeypatch, ["合成本文のみ", "2"], image_pages=(1,))
    pages = ocr._parse_output(json.dumps(result))
    assert pages[0]["engine"] == "pdf_text" and pages[0]["text"] == "合成本文のみ"
    assert pages[1]["engine"] == "yomitoku" and pages[1]["text"] == "合成画像内文字"


def test_bootstrap_broken_native_pdf_control_characters_fall_back_to_ocr(tmp_path, monkeypatch):
    result = _bootstrap_pdf(tmp_path, monkeypatch, ["合成\x13\x11文字"], raster_pages=(0,))
    pages = ocr._parse_output(json.dumps(result))
    assert pages[0]["engine"] == "yomitoku" and pages[0]["text"] == "合成画像内文字"


@pytest.mark.skipif(not Path("/usr/bin/sandbox-exec").exists(), reason="macOS sandbox runtime only")
def test_actual_sandbox_boots_python_through_alias_and_confines_source_and_cache(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    hub = home / ".cache" / "huggingface" / "hub"
    hub.mkdir(parents=True)
    model = hub / "synthetic-model"
    model.write_text("synthetic model")
    token = hub.parent / "token"
    token.write_text("synthetic token")
    other_cache = home / ".cache" / "unrelated"
    other_cache.write_text("synthetic unrelated cache")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    real_python = Path(sys.executable).resolve()
    alias = tmp_path / "python-alias"
    alias.symlink_to(real_python.parent.parent, target_is_directory=True)
    python = venv / "bin" / "python"
    python.symlink_to(alias / "bin" / real_python.name)
    private = tmp_path / "private"
    private.mkdir()
    source = private / "source.pdf"
    source.write_text("synthetic source")
    denied = tmp_path / "outside"
    denied.write_text("synthetic outside")
    profile = ocr._sandbox_profile(str(venv / "bin" / "yomitoku"), str(python), source)
    code = """import json, sys
from pathlib import Path
result = []
for path in sys.argv[1:]:
    try:
        Path(path).read_bytes()
        result.append(True)
    except PermissionError:
        result.append(False)
print(json.dumps(result))
"""
    result = subprocess.run(["/usr/bin/sandbox-exec", "-p", profile, str(python), "-I", "-S", "-c", code,
                             str(source), str(model), str(token), str(other_cache), str(denied)],
                            env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
                            capture_output=True, text=True, timeout=15, check=True)
    assert json.loads(result.stdout) == [True, True, False, False, False]


@pytest.mark.parametrize("texts,sizes", [([""] * 31, None),
                                        ([""], [(10000, 10000)]),
                                        (["", "", ""], [(2200, 2200)] * 3)])
def test_bootstrap_pdf_rejects_page_or_pixel_limits_before_render(tmp_path, monkeypatch, texts, sizes):
    with pytest.raises(SystemExit) as caught:
        _bootstrap_pdf(tmp_path, monkeypatch, texts, sizes)
    assert caught.value.code == 22


def test_structured_table_columns_remain_ambiguous_in_medical_detail(setup, monkeypatch):
    ledger, cfg, _, _, _ = setup
    value = {"engine_version": ocr.ENGINE_VERSION, "pages": [{"page": 1, "engine": "yomitoku", "document": {
        "paragraphs": [], "tables": [{"cells": [
            {"row": 0, "col": 0, "contents": "既往歴"},
            {"row": 0, "col": 1, "contents": "処方薬"},
            {"row": 1, "col": 0, "contents": "合成病A"},
            {"row": 1, "col": 1, "contents": "合成薬B"}]}]}}]}
    pages = ocr._parse_output(json.dumps(value))
    monkeypatch.setattr(ocr, "_runtime", lambda *args: pages)
    assert ocr.run_pending(ledger, cfg)["complete"] == 1
    detail = ocr.view(ledger.db, 1, include_text=True)
    assert detail["pages"][0]["text"] == "既往歴\t処方薬\n合成病A\t合成薬B"
    assert all(section["category"] == "other" and section["status"] == "needs_review"
               for section in detail["medical"]["sections"])


def test_expired_deadline_never_scans(setup, monkeypatch):
    ledger, cfg, _, calls, _ = setup
    statements = []
    ledger.db.set_trace_callback(statements.append)
    assert ocr.run_pending(ledger, cfg, deadline=0)["complete"] == 0
    assert not calls and not any("SELECT attachment_id" in sql for sql in statements)


def test_busy_settle_waits_without_recomputing_and_marks_snapshot(setup, monkeypatch):
    ledger, cfg, _, calls, path = setup
    real = ocr.writer_lock.acquire_run_lock
    attempts = []

    def acquire(lock):
        if str(lock).endswith("run.lock") and calls:
            return None
        return real(lock)

    def wait(lock, **kwargs):
        attempts.append(kwargs)
        return real(lock)

    monkeypatch.setattr(ocr.writer_lock, "acquire_run_lock", acquire)
    monkeypatch.setattr(ocr.writer_lock, "wait_run_lock", wait)
    assert ocr.run_pending(ledger, cfg)["complete"] == 1
    assert len(calls) == len(attempts) == 1
    assert ledger.db.execute("SELECT notify_dirty FROM notification_meta").fetchone()[0] == 1


@pytest.mark.parametrize("number", [ocr.signal.SIGTERM, ocr.signal.SIGHUP])
def test_parent_signal_kills_runtime_group_and_restores_handler(tmp_path, monkeypatch, number):
    old = ocr.signal.getsignal(number)
    killed = []
    process = SimpleNamespace(pid=1234, returncode=None)

    def wait(timeout):
        ocr.signal.getsignal(number)(number, None)

    process.wait = wait
    monkeypatch.setattr(ocr.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(ocr.os, "killpg", lambda *args: killed.append(args))
    monkeypatch.setattr(ocr, "reap_worker", lambda process: None)
    with pytest.raises(SystemExit) as caught:
        ocr._runtime("/synthetic/bin/yomitoku", tmp_path / "source.pdf", tmp_path / "out.json", 1)
    assert caught.value.code == 128 + number
    assert killed and ocr.signal.getsignal(number) == old


def test_bounded_scan_reaches_backfill_beyond_completed_prefix(setup):
    ledger, cfg, _, calls, _ = setup
    for aid in range(2, 601):
        ledger.db.execute("INSERT INTO attachments SELECT ?,message_id,state,sha256,bytes,local_path "
                          "FROM attachments WHERE attachment_id=1", (aid,))
    ledger.db.commit()
    # The first candidate stays priority; backfill cursor must advance independently.
    for _ in range(6):
        assert ocr.run_pending(ledger, cfg)["complete"] == 1
    assert len(calls) == 6
    cursor = json.loads((Path(ledger.path).parent / "attachment_ocr_cursor.json").read_text())
    assert cursor["after"] >= 3 and cursor["turn"] == 6


def test_priority_uses_active_patient_activity_before_archived(setup, monkeypatch):
    ledger, cfg, pages, _, _ = setup
    ledger.db.execute("INSERT INTO patients VALUES(20,1)")
    ledger.db.execute("INSERT INTO patients VALUES(21,0)")
    ledger.db.execute("INSERT INTO messages VALUES(11,21,'full',NULL,'synthetic active',2)")
    ledger.db.execute("INSERT INTO attachments SELECT 2,11,state,sha256,bytes,local_path FROM attachments WHERE attachment_id=1")
    ledger.db.commit()
    monkeypatch.setattr(ocr, "_runtime", lambda *args: pages)
    assert ocr.run_pending(ledger, cfg)["complete"] == 1
    assert ocr.current(ledger.db, 2) and ocr.current(ledger.db, 1) is None


def test_backfill_cursor_advances_past_bounded_cached_prefix(setup, monkeypatch):
    ledger, cfg, pages, _, _ = setup
    monkeypatch.setattr(ocr, "_runtime", lambda *args: pages)
    ocr.run_pending(ledger, cfg)
    template = json.loads(ledger.db.execute("SELECT content FROM artifacts").fetchone()[0])
    for aid in range(2, 602):
        ledger.db.execute("INSERT INTO attachments SELECT ?,message_id,state,sha256,bytes,local_path "
                          "FROM attachments WHERE attachment_id=1", (aid,))
        if aid <= 600:
            doc = {**template, "attachment_id": aid}
            ledger.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                              "VALUES(?,20,10,?,'yomitoku',?,1)", (ocr.KIND, json.dumps(doc),
                                                                    json.dumps({"attachment_id": aid})))
    ledger.db.commit()
    for _ in range(6):
        ocr.run_pending(ledger, cfg)
    assert ocr.current(ledger.db, 601) is not None


def test_status_keeps_backoff_failure_until_success(setup, monkeypatch):
    ledger, cfg, pages, _, _ = setup

    def fail(*args):
        raise ValueError("synthetic failure")

    monkeypatch.setattr(ocr, "_runtime", fail)
    first = ocr.run_pending(ledger, cfg)
    assert first["pending_failed"] == 1 and first["queue_status"] == "known"
    backed_off = ocr.run_pending(ledger, cfg)
    assert backed_off["failed"] == 0 and backed_off["pending_failed"] == 1
    ledger.db.execute("UPDATE artifacts SET content=json_set(content,'$.next_try',0)")
    ledger.db.commit()
    monkeypatch.setattr(ocr, "_runtime", lambda *args: pages)
    for _ in range(3):
        recovered = ocr.run_pending(ledger, cfg)
        if recovered["complete"]:
            break
    assert recovered["complete"] == 1 and recovered["pending_failed"] == 0
    assert recovered["queue_status"] == "known"


def test_queue_status_does_not_claim_unobserved_rows_have_no_failures(setup, monkeypatch):
    ledger, cfg, pages, _, _ = setup
    for aid in range(2, 258):
        ledger.db.execute("INSERT INTO attachments SELECT ?,message_id,state,sha256,bytes,local_path "
                          "FROM attachments WHERE attachment_id=1", (aid,))
    ledger.db.commit()
    monkeypatch.setattr(ocr, "_runtime", lambda *args: pages)
    result = ocr.run_pending(ledger, cfg)
    assert result["queue_status"] == "partial"
