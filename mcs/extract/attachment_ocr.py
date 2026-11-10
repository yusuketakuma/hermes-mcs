"""Store attachment text separately and run an optional isolated local OCR runtime."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import time

from ocr_model_contract import ENGINE_VERSION, MODELS, RUNTIME_CONTRACT

import writer_lock
import patient_priority
from safe_io import atomic_write, read_json_nofollow
from mcs_util import ProviderSettingsUnreadable, load_config, valid_hash, strict_json
from patient_read_scope import patient_scope_valid
from worker_process import reap_worker

KIND = "attachment_ocr_v1"
SCHEMA = "attachment-ocr/v1"
MAX_INPUT = 64 * 1024 * 1024
MAX_OUTPUT = 2 * 1024 * 1024
MAX_PAGES = 30
MAX_TABLE_ENTRIES = 10000
FAILURE_RETRY_S = 86400
_STATES = {"complete", "empty", "failed", "unavailable", "unsupported"}
EXCLUDED_STATES = frozenset({"unsupported", "rejected_by_policy"})

# Optional packages are imported only in the separate, network-denied runtime.
_BOOTSTRAP = f'MODELS = {MODELS!r}\nENGINE_VERSION = {ENGINE_VERSION!r}\n' + r'''
import importlib.metadata, json, math, os, resource, sys
from contextlib import closing, contextmanager
from pathlib import Path
resource.setrlimit(resource.RLIMIT_FSIZE, (2097152, 2097152))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
source, output = Path(sys.argv[1]), Path(sys.argv[2])
version = importlib.metadata.version("yomitoku")
if version != ENGINE_VERSION:
    sys.exit(21)
analyzer = None
@contextmanager
def runtime_phase(code):
    try:
        yield
    except Exception:
        sys.exit(code)
def ocr(image):
    global analyzer
    with runtime_phase(24):
        import cv2
        import numpy as np
        import torch
        cv2.setNumThreads(1)
        torch.set_num_threads(1)
        torch.set_num_interop_threads(1) if analyzer is None else None
        if analyzer is None:
            from huggingface_hub import snapshot_download
            paths = {}
            for role, (repo, revision) in MODELS.items():
                snapshot = snapshot_download(repo_id=repo, revision=revision, local_files_only=True)
                path = source.parent / (role + ".json")
                with open(path, "x", encoding="utf-8") as stream:
                    os.chmod(path, 0o600)
                    json.dump({"hf_hub_repo": str(snapshot)}, stream)
                paths[role] = str(path)
            from yomitoku import DocumentAnalyzer
            analyzer = DocumentAnalyzer(device="cpu", visualize=False, configs={
                "ocr": {"text_detector": {"infer_onnx": False, "path_cfg": paths["detector"]}, "text_recognizer": {
                    "model_name": "parseq-tiny-dynw-v5", "source_downscale": True,
                    "num_parallel_batches": 1, "path_cfg": paths["recognizer"]}},
                "layout_analyzer": {"layout_parser": {"path_cfg": paths["layout"]},
                    "table_structure_recognizer": {"path_cfg": paths["table"]}}})
    with runtime_phase(25):
        result, _, _ = analyzer(np.array(image.convert("RGB"))[:, :, ::-1])
        return result.model_dump()
pages, rendered = [], 0
if source.suffix == ".pdf":
    import pypdfium2
    with closing(pypdfium2.PdfDocument(source)) as document:
        if not 0 < len(document) <= 30:
            sys.exit(22)
        # Validate every raster bound before any page is rendered.
        for number in range(len(document)):
            with closing(document[number]) as page:
                width, height = page.get_size()
                pixels = math.ceil(width * 200 / 72) * math.ceil(height * 200 / 72)
                with closing(page.get_textpage()) as textpage:
                    if textpage.count_chars() > 1048576:
                        sys.exit(23)
                    text = textpage.get_text_range()
                has_image = False
                for count, obj in enumerate(page.get_objects(max_depth=30), 1):
                    if count > 10000 or obj.type == 5 and obj.level >= 29:
                        sys.exit(22)
                    has_image = has_image or obj.type == 3
                readable_text = "\ufffd" not in text and not any(
                    (ord(char) < 32 and char not in "\t\n\r") or ord(char) == 127 for char in text)
                if text.strip() and readable_text and not has_image:
                    pages.append({"page": number + 1, "engine": "pdf_text", "text": text})
                else:
                    if not 0 < pixels <= 40000000:
                        sys.exit(22)
                    rendered += pixels
                    if rendered > 80000000:
                        sys.exit(22)
                    pages.append({"page": number + 1, "engine": "yomitoku", "text": None})
        for entry in pages:
            if entry["text"] is None:
                with closing(document[entry["page"] - 1]) as page:
                    with runtime_phase(26), closing(page.render(scale=200 / 72)) as bitmap:
                        entry["document"] = ocr(bitmap.to_pil())
                del entry["text"]
else:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 40000000
    with runtime_phase(26), Image.open(source) as image:
        if not 0 < image.width * image.height <= 40000000 or getattr(image, "n_frames", 1) != 1:
            sys.exit(22)
        image.load()
        pages.append({"page": 1, "engine": "yomitoku", "document": ocr(image)})
raw = json.dumps({"engine_version": version, "pages": pages}, ensure_ascii=False).encode()
if len(raw) > 2097152:
    sys.exit(23)
with open(output, "xb") as stream:
    stream.write(raw)
'''


def config_error(cfg):
    """Return a fixed reason for an invalid optional OCR configuration."""
    value = cfg.get("attachment_ocr", {}) if isinstance(cfg, dict) else None
    if not isinstance(value, dict) or set(value) - {"enabled", "executable", "timeout_seconds", "reject_uncertain"}:
        return "attachment_ocr_config"
    if type(value.get("enabled", False)) is not bool:
        return "attachment_ocr_enabled"
    if type(value.get("reject_uncertain", False)) is not bool:
        return "attachment_ocr_reject_uncertain"
    executable = value.get("executable", "")
    if (not isinstance(executable, str) or "\x00" in executable
            or executable and not os.path.isabs(executable)
            or value.get("enabled") is True and not executable):
        return "attachment_ocr_executable"
    timeout = value.get("timeout_seconds", 120)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 600:
        return "attachment_ocr_timeout"
    return None


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source(db, attachment_id, project_id=None):
    if (type(attachment_id) is not int or attachment_id <= 0
            or project_id is not None and (type(project_id) is not int or project_id <= 0)):
        return None
    row = db.execute("SELECT a.*,m.project_id,m.body_state,m.parent_id FROM attachments a "
                     "JOIN messages m ON m.message_id=a.message_id WHERE a.attachment_id=?",
                     (attachment_id,)).fetchone()
    if row is None:
        return None
    row = dict(row)
    if (project_id is not None and row["project_id"] != project_id
            or not patient_scope_valid(db, row['project_id'])):
        return None
    return row if row["body_state"] != "deleted" else None



def _source_occurrence(db, source):
    """Bind one acquisition and parent revision in a borrowed read snapshot."""
    import inference_dependency_loss
    downloaded = source.get("downloaded_at")
    if (type(downloaded) not in (int, float) or downloaded < 0
            or type(downloaded) is float and not math.isfinite(downloaded)):
        return None
    db.execute("SAVEPOINT ocr_source_occurrence")
    try:
        latest = _source(db, source["attachment_id"], source["project_id"])
        if latest != source:
            return None
        parent = inference_dependency_loss.source_binding(db, source["project_id"], source["message_id"])
        return _hash(json.dumps([source, parent], sort_keys=True, ensure_ascii=False)) if valid_hash(parent) else None
    finally:
        db.execute("RELEASE ocr_source_occurrence")


def _latest(db, source):
    row = db.execute("SELECT artifact_id,project_id,message_id,created_at,CASE WHEN length(content)<=4194304 "
                     "THEN content END AS content FROM artifacts WHERE kind=? "
                     "AND project_id=? AND message_id=? AND CASE WHEN json_valid(meta) "
                     "THEN json_extract(meta,'$.attachment_id') END=? ORDER BY artifact_id DESC LIMIT 1",
                     (KIND, source["project_id"], source["message_id"], source["attachment_id"])).fetchone()
    if row is None:
        return None
    try:
        value = strict_json(row["content"])
    except (ValueError, TypeError, RecursionError, OverflowError):
        value = None
    if not isinstance(value, dict):
        return {"artifact_id": row["artifact_id"], "_invalid": True}
    return {**value, "artifact_id": row["artifact_id"], "project_id": row["project_id"],
            "message_id": row["message_id"], "_recorded_at": row["created_at"]}


def _matching(doc, source):
    return (isinstance(doc, dict) and doc.get("schema") == SCHEMA
            and doc.get("runtime_contract") == RUNTIME_CONTRACT
            and doc.get("attachment_id") == source["attachment_id"]
            and doc.get("input_sha256") == source["sha256"] and valid_hash(source["sha256"])
            and type(doc.get("input_bytes")) is int and doc["input_bytes"] == source["bytes"]
            and 0 <= doc["input_bytes"] <= MAX_INPUT
            and doc.get("state") in _STATES)


def _valid_pages(pages):
    if not isinstance(pages, list) or not 0 < len(pages) <= MAX_PAGES:
        return False
    size = 0
    try:
        for number, page in enumerate(pages, 1):
            if (not isinstance(page, dict) or type(page.get("page")) is not int or page["page"] != number
                    or not isinstance(page.get("text"), str) or page.get("text_hash") != _hash(page["text"])
                    or page.get("engine") not in ("pdf_text", "yomitoku")):
                return False
            size += len(page["text"].encode("utf-8"))
    except UnicodeError:
        return False
    return size <= MAX_OUTPUT


def _completed(doc):
    return (doc.get("state") in ("complete", "empty")
            and doc.get("engine") == "yomitoku" and doc.get("engine_version") == ENGINE_VERSION
            and _valid_pages(doc.get("pages"))
            and doc.get("text_hash") == _hash(json.dumps(doc["pages"], ensure_ascii=False, sort_keys=True))
            and (doc["state"] == "empty") == (not any(p["text"].strip() for p in doc["pages"])))


def current(db, attachment_id, project_id=None):
    """Read only a current, validated completed attachment source, including retained text."""
    source = _source(db, attachment_id, project_id)
    if source is None or source["state"] not in ("downloaded", "pruned"):
        return None
    doc = _latest(db, source)
    if not _matching(doc, source) or not _completed(doc):
        return None
    return {**doc, "original_available": source["state"] == "downloaded"}



def rejection_policy(cfg):
    """Identify an explicit reversible exclusion policy, independently of OCR retries."""
    if (not isinstance(cfg, dict) or config_error(cfg)
            or cfg.get("attachment_ocr", {}).get("enabled") is not True
            or cfg["attachment_ocr"].get("reject_uncertain") is not True):
        return None
    return _hash("ocr-reject-table-overlap/v2/" + RUNTIME_CONTRACT)


def rejected_failure(db, attachment_id, project_id=None, *, cfg=None):
    """Observe a source-proven structural rejection without editing its failed artifact."""
    policy = rejection_policy(cfg)
    if policy is None:
        return None
    import attachment_ocr_ack
    from mcs_contract import project_url
    db.execute("SAVEPOINT ocr_rejection_read")
    try:
        source = _source(db, attachment_id, project_id)
        prior = _latest(db, source) if source is not None else None
        if (source is None or source["state"] != "downloaded" or not _matching(prior, source)
                or prior["state"] != "failed" or prior.get("reason") != "output_schema_table_overlap"
                or type(prior.get("attempts")) is not int or not 1 <= prior["attempts"] <= 3
                or not valid_hash(prior.get("input_occurrence"))
                or prior["input_occurrence"] != _source_occurrence(db, source)):
            return None
        row = db.execute("SELECT CASE WHEN length(meta)<=32768 THEN meta END FROM artifacts "
                         "WHERE artifact_id=?", (prior["artifact_id"],)).fetchone()
        meta = strict_json(row[0]) if row else None
        if not isinstance(meta, dict) or meta.get("input_occurrence") != prior["input_occurrence"]:
            return None
        proof = attachment_ocr_ack.failure_target(db, prior["artifact_id"])
        return {"state": "rejected_by_policy", "page_count": 0,
                "artifact_id": prior["artifact_id"], "attachment_id": attachment_id,
                "parent_message_id": source["message_id"], "project_id": source["project_id"],
                "reason": "output_schema_table_overlap", "policy_binding": policy,
                "proof": proof["proof"], "original_project_url": project_url(source["project_id"]),
                "source_coverage": "unrecovered", "clinical_review": "not_proven",
                "original_file_availability": "not_rechecked"}
    except (ValueError, TypeError, KeyError, sqlite3.Error, OSError, RecursionError, OverflowError):
        return None
    finally:
        db.execute("RELEASE ocr_rejection_read")


def view(db, attachment_id, project_id=None, include_text=False, *, cfg=None):
    """Expose status without attachment text outside an explicit detail read."""
    source = _source(db, attachment_id, project_id)
    if source is None or source["state"] not in ("downloaded", "pruned"):
        return {"state": "unavailable", "page_count": 0}
    doc = current(db, attachment_id, project_id)
    if doc:
        result = {"state": doc["state"], "page_count": len(doc["pages"]),
                  "artifact_id": doc["artifact_id"], "original_available": doc["original_available"]}
        if include_text:
            import medical_document
            result["pages"] = doc["pages"]
            result["medical"] = medical_document.extract(doc["pages"])
        return result
    rejection = rejected_failure(db, attachment_id, project_id, cfg=cfg)
    if rejection is not None:
        return rejection
    prior = _latest(db, source)
    if source['state'] == 'pruned':
        return {'state': 'unavailable', 'page_count': 0, 'original_available': False}
    return {"state": prior["state"] if _matching(prior, source) and prior["state"] not in ("complete", "empty")
            else "unprocessed", "page_count": 0}


def capture_dependencies_tx(db, *, attachment_id=None, message_id=None):
    """Capture current OCR page dependencies before their source binding changes."""
    import inference_dependency_loss as loss
    import original_source_keys
    if not db.in_transaction:
        raise ValueError("dependency_loss_transaction_required")
    column, value = ("attachment_id", attachment_id) if attachment_id is not None else ("message_id", message_id)
    snapshots = []
    for row in db.execute(f"SELECT attachment_id FROM attachments WHERE {column}=?", (value,)):
        document = current(db, row[0])
        if document is None:
            continue
        for page in document["pages"]:
            key = f"attachment_ocr:{document['artifact_id']}:{page['page']}"
            source = original_source_keys.source(db, document["project_id"], key)
            if source is not None:
                snapshots.append((source["_source_binding"], loss.capture_tx(
                    db, document["project_id"], 0, source_key=key)))
    return snapshots


def record_dependencies_tx(db, snapshots):
    """Record only OCR dependencies whose retained source occurrence changed."""
    import inference_dependency_loss as loss
    import original_source_keys
    for binding, snapshot in snapshots:
        if original_source_keys.binding(db, snapshot["project_id"], snapshot["source_key"]) != binding:
            loss.record_tx(db, snapshot)


def _document_text(document):
    if not isinstance(document, dict):
        raise ValueError("output_schema")
    blocks, output_size = [], 0
    for key in ("paragraphs", "tables", "figures"):
        items = document.get(key, [] if key == "figures" else None)
        if not isinstance(items, list):
            raise ValueError("output_schema")
        for index, item in enumerate(items):
            if not isinstance(item, dict) or item.get("order") is not None and type(item["order"]) is not int:
                raise ValueError("output_schema")
            if key == "paragraphs":
                text = item.get("contents")
                if text is None:
                    text = ""
                if not isinstance(text, str):
                    raise ValueError("output_schema")
            elif key == "figures":
                text = _document_text({"paragraphs": item.get("paragraphs"), "tables": []})
            else:
                cells = item.get("cells")
                if (not isinstance(cells, list) or len(cells) > MAX_TABLE_ENTRIES
                        or any(not isinstance(cell, dict)
                        or type(cell.get("row")) is not int or type(cell.get("col")) is not int
                        or cell.get("contents") is not None and not isinstance(cell["contents"], str)
                        for cell in cells)):
                    raise ValueError("output_schema")
                rows, height, entries, content_size = {}, 0, 0, 0
                for cell in cells:
                    row, col = cell["row"], cell["col"]
                    row_span, col_span = cell.get("row_span", 1), cell.get("col_span", 1)
                    if (row < 0 or col < 0 or type(row_span) is not int or row_span < 1
                            or type(col_span) is not int or col_span < 1
                            or row + row_span > MAX_OUTPUT + 1 or col + col_span > MAX_OUTPUT + 1):
                        raise ValueError("output_schema")
                    entries += row_span
                    content = cell.get("contents") or ""
                    content_size += len(content.encode("utf-8"))
                    if entries > MAX_TABLE_ENTRIES or content_size > MAX_OUTPUT:
                        raise ValueError("output_schema")
                    height = max(height, row + row_span)
                    for offset in range(row_span):
                        rows.setdefault(row + offset, []).append((col, col + col_span, content if offset == 0 else ""))
                # Count every missing separator before allocating padding from runtime indices.
                size = content_size + max(0, height - 1)
                for row in rows.values():
                    row.sort()
                    end = 0
                    for col, stop, _ in row:
                        if col < end:
                            raise ValueError("output_schema_table_overlap")
                        end = stop
                    size += end - 1
                    if size > MAX_OUTPUT:
                        raise ValueError("output_schema")
                lines = []
                previous_row = 0
                for number, row in sorted(rows.items()):
                    parts, previous_col = [], 0
                    for col, stop, content in row:
                        parts.extend(("\t" * (col - previous_col), content))
                        previous_col = col
                    parts.append("\t" * (stop - 1 - previous_col))
                    lines.extend(("\n" * (number - previous_row), "".join(parts)))
                    previous_row = number
                text = "".join(lines)
            output_size += len(text.encode("utf-8")) + bool(blocks)
            if output_size > MAX_OUTPUT:
                raise ValueError("output_schema")
            blocks.append((index if item.get("order") is None else item["order"], text))
    return "\n".join(text for _, text in sorted(blocks, key=lambda item: item[0]))


def _parse_output(raw):
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("engine_version") != ENGINE_VERSION:
        raise ValueError("engine_version")
    items = value.get("pages")
    if not isinstance(items, list):
        raise ValueError("output_schema")
    pages = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("output_schema")
        text = item.get("text") if item.get("engine") == "pdf_text" else _document_text(item.get("document"))
        if not isinstance(text, str):
            raise ValueError("output_schema")
        pages.append({"page": item.get("page"), "engine": item.get("engine"), "text": text, "text_hash": _hash(text)})
    if not _valid_pages(pages):
        raise ValueError("output_schema")
    return pages


def _suffix(blob):
    if blob.startswith(b"%PDF-"):
        return ".pdf"
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if blob.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    return None


def _symlink_chain(path):
    """Retain lexical alias paths needed by macOS exec and dynamic-library resolution."""
    pending = Path(os.path.abspath(path))
    chain = {pending}
    for _ in range(40):
        for prefix in [*reversed(pending.parents), pending]:
            if prefix.is_symlink():
                target = Path(os.readlink(prefix))
                if not target.is_absolute():
                    target = prefix.parent / target
                chain.add(prefix)
                pending = Path(os.path.normpath(target / pending.relative_to(prefix)))
                chain.add(pending)
                break
        else:
            return chain
    raise ValueError("runtime_symlink_loop")


def _sandbox_profile(executable, python, source):
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    readable = [Path(path) for path in ("/System", "/usr", "/Library", "/private/var/db", "/dev")]
    readable += [Path(executable).parent.parent.resolve(), Path(python).resolve().parent.parent,
                 hub, source.parent.resolve()]
    metadata, aliases = set(), _symlink_chain(python)
    readable += [alias.parent.parent for alias in aliases if alias.is_file()]
    for path in [*readable, *aliases, source.parent]:
        for alias in _symlink_chain(path):
            metadata.update([alias, *alias.parents])
    # macOS libignition opens the root directory for openat(); this grants no descendant data.
    return ('(version 1)(allow default)(deny network*)(deny file-read*)(deny file-write*)'
            '(allow file-read* (literal "/"))'
            + ''.join('(allow file-read* (subpath ' + json.dumps(str(path)) + '))' for path in readable)
            + ''.join('(allow file-read-metadata (literal ' + json.dumps(str(path)) + '))' for path in sorted(metadata))
            + ''.join('(allow file-read-data (literal ' + json.dumps(str(path)) + '))' for path in sorted(aliases))
            + '(allow file-write* (subpath ' + json.dumps(str(source.parent.resolve())) + '))')


def _runtime(executable, source, output, timeout):
    python = str(Path(executable).parent / "python")
    cache = str(Path.home() / ".cache")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "en_US.UTF-8",
           "TMPDIR": str(source.parent), "HF_HOME": str(Path(cache) / "huggingface"),
           "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
           "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
           "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
           "VECLIB_MAXIMUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1", "TOKENIZERS_PARALLELISM": "false"}
    # Models remain readable; the runtime cannot write anywhere outside its private snapshot.
    profile = _sandbox_profile(executable, python, source)
    command = ["/usr/bin/sandbox-exec", "-p", profile, python, "-I", "-c", _BOOTSTRAP, str(source), str(output)]
    process = None
    previous = {}

    def interrupted(number, frame):
        raise SystemExit(128 + number)

    try:
        for number in (signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.signal(number, interrupted)
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, env=env, cwd=source.parent, start_new_session=True)
        process.wait(timeout=timeout)
    except BaseException:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            reap_worker(process)
        raise
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        # A successful leader must not leave a worker using private source bytes.
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    if process.returncode:
        raise ValueError({21: "engine_version", 22: "input_limits", 23: "output_limits",
                          24: "engine_load_failed", 25: "engine_inference_failed",
                          26: "input_decode_failed"}.get(process.returncode, "engine_failed"))
    fd = os.open(output, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 <= info.st_size <= MAX_OUTPUT:
            raise ValueError("output_limits")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_OUTPUT + 1)
        if len(raw) > MAX_OUTPUT:
            raise ValueError("output_limits")
        return _parse_output(raw)
    finally:
        os.close(fd)


def _data_root(ledger):
    path = next((row[2] for row in ledger.db.execute("PRAGMA database_list") if row[1] == "main"), "")
    if not path:
        raise ValueError("database_path")
    return Path(path).resolve().parent


def _due(doc, source, *, retry_failed_before=None):
    if not _matching(doc, source):
        return True
    if doc["state"] in ("complete", "empty"):
        return not _completed(doc)
    if doc["state"] == "unsupported":
        return False
    attempts, next_try = doc.get("attempts"), doc.get("next_try")
    if (type(attempts) is not int or not 1 <= attempts <= 3
            or type(next_try) not in (int, float) or next_try < 0
            or type(next_try) is float and not math.isfinite(next_try)):
        return False
    if retry_failed_before is not None:
        recorded_at = doc.get("_recorded_at")
        if (type(retry_failed_before) not in (int, float) or retry_failed_before < 0
                or type(retry_failed_before) is float and not math.isfinite(retry_failed_before)
                or type(recorded_at) not in (int, float) or recorded_at < 0
                or type(recorded_at) is float and not math.isfinite(recorded_at)):
            return False
        if recorded_at < retry_failed_before:
            return True
    if attempts == 3:
        recorded_at = doc.get("_recorded_at")
        if (type(recorded_at) not in (int, float) or recorded_at < 0
                or type(recorded_at) is float and not math.isfinite(recorded_at)):
            return False
        next_try = max(next_try, recorded_at + FAILURE_RETRY_S)
    return next_try <= time.time()


def _settings_current(settings):
    try:
        live = load_config()
    except (OSError, ValueError, TypeError, ProviderSettingsUnreadable):
        return False
    current = live.get('attachment_ocr', {}) if isinstance(live, dict) else {}
    return (not config_error(live) and current.get('enabled') is True
            and settings.get('enabled') is True
            and current.get('executable') == settings.get('executable')
            and current.get('timeout_seconds', 120) == settings.get('timeout_seconds', 120)
            and current.get('reject_uncertain', False) is settings.get('reject_uncertain', False))


_RUNTIME_REASONS = frozenset({
    "engine_failed", "engine_version", "input_limits",
    "output_limits", "output_schema", "runtime_symlink_loop",
    "engine_load_failed", "engine_inference_failed", "input_decode_failed",
    "output_schema_table_overlap",
})
FAILURE_REASONS = _RUNTIME_REASONS | {"timeout", "local_processing_failed"}


def _settle(ledger, source, doc, root, generation, settings, deadline=None, *, retry_failed_before=None):
    while True:
        if (deadline is not None and deadline - time.monotonic() <= 1
                or writer_lock.updater_state() != generation or generation[0]
                or not _settings_current(settings)):
            return False
        fd = writer_lock.acquire_run_lock(str(root / "run.lock"))
        if fd is None:
            wait_s = 5 if deadline is None else min(5, max(deadline - time.monotonic() - 1, 0.0))
            fd = writer_lock.wait_run_lock(str(root / "run.lock"), wait_s=wait_s,
                                          expected_code_stamp=generation[1],
                                          marker_path=str(root / writer_lock.UPDATE_MARKER_NAME))
            if fd is None:
                if deadline is None:
                    return False
                continue
        if deadline is not None and deadline - time.monotonic() <= 1:
            os.close(fd)
            return False
        try:
            if (writer_lock.updater_state() != generation or generation[0]
                    or not _settings_current(settings)):
                return False
            with ledger.db:
                ledger.db.execute("UPDATE attachments SET attachment_id=attachment_id WHERE 0")
                latest = _source(ledger.db, source["attachment_id"], source["project_id"])
                if (latest is None or latest["state"] != "downloaded"
                        or any(latest[key] != source[key] for key in ("message_id", "sha256", "bytes"))
                        or doc.get("input_occurrence") is not None
                        and doc["input_occurrence"] != _source_occurrence(ledger.db, latest)
                        or not _due(_latest(ledger.db, latest), latest, **({"retry_failed_before": retry_failed_before}
                            if retry_failed_before is not None else {}))):
                    return False
                dependencies = capture_dependencies_tx(ledger.db, attachment_id=source["attachment_id"])
                ledger.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                                  "VALUES(?,?,?,?,?,?,?)", (KIND, source["project_id"], source["message_id"],
                                  json.dumps(doc, ensure_ascii=False), "yomitoku", json.dumps({
                                      "attachment_id": source["attachment_id"], "input_sha256": source["sha256"],
                                      "runtime_contract": RUNTIME_CONTRACT,
                                      "input_occurrence": doc.get("input_occurrence")}), time.time()))
                record_dependencies_tx(ledger.db, dependencies)
                import notify_cards
                notify_cards.mark_snapshot_dirty(ledger.db)
            return True
        finally:
            os.close(fd)


def run_pending(ledger, cfg, *, deadline=None, limit=1, retry_failed_before=None):
    """Run bounded local attachment text extraction without holding the shared writer lock."""
    counts = {"complete": 0, "empty": 0, "failed": 0, "unsupported": 0, "deferred": 0}
    if config_error(cfg):
        return {**counts, "reason": "config_invalid"}
    settings = dict(cfg.get("attachment_ocr", {}))
    if not settings.get("enabled", False):
        return {**counts, "reason": "disabled"}
    if not _settings_current(settings):
        return {**counts, "deferred": 1}
    executable = settings["executable"]
    if (not os.path.isfile(executable) or not os.access(executable, os.X_OK)
            or not os.access(str(Path(executable).parent / "python"), os.X_OK)
            or not os.access("/usr/bin/sandbox-exec", os.X_OK)):
        return {**counts, "reason": "runtime_unavailable"}
    if type(limit) is not int or not 1 <= limit <= 30 or ledger.db.in_transaction:
        raise ValueError("ocr_run_args")
    root = _data_root(ledger)
    fd = writer_lock.acquire_run_lock(str(root / "attachment_ocr.lock"))
    if fd is None:
        return {**counts, "reason": "busy"}
    try:
        generation = writer_lock.updater_state()
        if generation[0]:
            return {**counts, "reason": "writer_blocked"}
        if deadline is not None and deadline - time.monotonic() < 1:
            return counts
        cursor_path = root / "attachment_ocr_cursor.json"
        try:
            cursor = read_json_nofollow(cursor_path, 1024)
            if (not isinstance(cursor, dict) or any(type(cursor.get(key)) is not int or cursor[key] < 0
                    for key in ("after", "offset", "turn"))):
                raise ValueError("cursor_invalid")
        except (OSError, ValueError):
            cursor = {"after": 0, "offset": 0, "turn": 0}
        priority = cursor["turn"] % 2 == 0
        if priority:
            rows = ledger.db.execute(
                "SELECT a.attachment_id FROM attachments a JOIN messages m USING(message_id) "
                "LEFT JOIN patients p ON p.project_id=m.project_id WHERE a.state='downloaded' "
                "ORDER BY COALESCE(p.is_archived,0), " + patient_priority.activity_order_sql('m.project_id')
                + ", a.attachment_id LIMIT 256 OFFSET ?", (cursor["offset"],)).fetchall()
            if not rows:
                cursor["offset"] = 0
        else:
            rows = ledger.db.execute("SELECT attachment_id FROM attachments WHERE state='downloaded' "
                                     "AND attachment_id>? ORDER BY attachment_id LIMIT 256",
                                     (cursor["after"],)).fetchall()
            if not rows:
                cursor["after"] = 0
        used = 0
        for row in rows:
            if used >= limit or deadline is not None and deadline - time.monotonic() < 1:
                break
            cursor_key = "offset" if priority else "after"
            previous_position = cursor[cursor_key]
            if priority:
                cursor["offset"] += 1
            else:
                cursor["after"] = row[0]
            source = _source(ledger.db, row[0])
            if source is None:
                continue
            prior = _latest(ledger.db, source)
            if not _due(prior, source, **({"retry_failed_before": retry_failed_before}
                    if retry_failed_before is not None else {})):
                continue
            occurrence = _source_occurrence(ledger.db, source)
            if "downloaded_at" in source and source["downloaded_at"] is not None and occurrence is None:
                counts["deferred"] += 1
                continue
            timeout = settings.get("timeout_seconds", 120)
            if deadline is not None:
                timeout = min(timeout, deadline - time.monotonic())
            if timeout < 1 or used >= limit:
                break
            import extract_llm
            if not extract_llm._context_memory_available():
                cursor[cursor_key] = previous_position
                counts["deferred"] += 1
                counts["reason"] = "memory_deferred"
                break
            used += 1
            state, pages, reason = "failed", [], "input_unavailable"
            try:
                from adapters.common.paths import read_verified_attachment
                blob = read_verified_attachment(source["local_path"], source, root=str(root))
                if blob is None:
                    raise ValueError("input_unavailable")
                suffix = _suffix(blob)
                if suffix is None:
                    state, reason = "unsupported", "unsupported_type"
                else:
                    with tempfile.TemporaryDirectory(prefix="mcs-ocr-") as directory:
                        private = Path(directory)
                        snapshot = private / ("source" + suffix)
                        with open(snapshot, "xb") as stream:
                            os.chmod(snapshot, 0o600)
                            stream.write(blob)
                        pages = _runtime(executable, snapshot, private / "result.json", timeout)
                    state, reason = ("complete" if any(page["text"].strip() for page in pages) else "empty"), ""
            except subprocess.TimeoutExpired:
                reason = "timeout"
            except (OSError, ValueError, ImportError) as error:
                reason = (str(error) if isinstance(error, ValueError) and str(error) in _RUNTIME_REASONS
                          else "local_processing_failed")
            attempts = (prior.get("attempts", 0) if _matching(prior, source)
                        and (occurrence is None or prior.get("input_occurrence") == occurrence) else 0)
            attempts = min(attempts + 1, 3) if type(attempts) is int and 0 <= attempts <= 3 else 1
            methods = {page["engine"] for page in pages}
            doc = {"schema": SCHEMA, "attachment_id": source["attachment_id"], "input_sha256": source["sha256"],
                   "input_bytes": source["bytes"], "input_occurrence": occurrence, "engine": "yomitoku", "engine_version": ENGINE_VERSION if pages else None,
                   "method": "mixed" if len(methods) > 1 else next(iter(methods), None),
                   "runtime_contract": RUNTIME_CONTRACT, "state": state, "pages": pages,
                   "text_hash": _hash(json.dumps(pages, ensure_ascii=False, sort_keys=True)),
                   **({"retry_requested_before": retry_failed_before} if retry_failed_before is not None else {}),
                   "attempts": attempts, "next_try": time.time() + (FAILURE_RETRY_S if attempts == 3 else 3600),
                   "reason": reason}
            if _settle(ledger, source, doc, root, generation, settings, deadline=deadline,
                       **({"retry_failed_before": retry_failed_before} if retry_failed_before is not None else {})):
                counts[state] += 1
            else:
                counts["deferred"] += 1
        cursor["turn"] += 1
        atomic_write(str(cursor_path), lambda stream: json.dump(cursor, stream), mode=0o600)
        import attachment_ocr_ack
        ledger.db.execute('BEGIN')
        try:
            counts.update(attachment_ocr_ack.failure_counts(ledger.db, cfg=cfg, deadline=deadline))
        finally:
            ledger.db.rollback()
        return counts
    finally:
        os.close(fd)
