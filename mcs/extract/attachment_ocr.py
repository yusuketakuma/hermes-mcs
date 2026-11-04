"""Store attachment text separately and run an optional isolated local OCR runtime."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import time

import writer_lock
from mcs_util import valid_hash
from worker_process import reap_worker

KIND = "attachment_ocr_v1"
SCHEMA = "attachment-ocr/v1"
ENGINE_VERSION = "0.16.0"
RUNTIME_CONTRACT = "yomitoku-0.16.0-cpu-lite-text-first/v1"
MAX_INPUT = 64 * 1024 * 1024
MAX_OUTPUT = 2 * 1024 * 1024
MAX_PAGES = 30
_STATES = {"complete", "empty", "failed", "unavailable", "unsupported"}

# Optional packages are imported only in the separate, network-denied runtime.
_BOOTSTRAP = r'''
import importlib.metadata, json, math, os, resource, sys
from contextlib import closing
from pathlib import Path
resource.setrlimit(resource.RLIMIT_FSIZE, (2097152, 2097152))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
source, output = Path(sys.argv[1]), Path(sys.argv[2])
version = importlib.metadata.version("yomitoku")
if version != "0.16.0":
    sys.exit(21)
analyzer = None
def ocr(image):
    global analyzer
    import cv2
    import numpy as np
    import torch
    cv2.setNumThreads(1)
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1) if analyzer is None else None
    if analyzer is None:
        from huggingface_hub import snapshot_download
        paths = {}
        for role, repo in {
            "detector": "yomitoku-text-detector-dbnet-v2_1",
            "recognizer": "yomitoku-text-recognizer-parseq-tiny-dynw-v5",
            "layout": "yomitoku-layout-parser-rtdtrv2-v2",
            "table": "yomitoku-table-structure-recognizer-rtdtrv2-open-beta",
        }.items():
            snapshot = snapshot_download(repo_id="KotaroKinoshita/" + repo, local_files_only=True)
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
                    with closing(page.render(scale=200 / 72)) as bitmap:
                        entry["document"] = ocr(bitmap.to_pil())
                del entry["text"]
else:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 40000000
    with Image.open(source) as image:
        if not 0 < image.width * image.height <= 40000000 or getattr(image, "n_frames", 1) != 1:
            sys.exit(22)
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
    if not isinstance(value, dict) or set(value) - {"enabled", "executable", "timeout_seconds"}:
        return "attachment_ocr_config"
    if type(value.get("enabled", False)) is not bool:
        return "attachment_ocr_enabled"
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
    if project_id is not None and row["project_id"] != project_id:
        return None
    return row if row["body_state"] != "deleted" else None


def _latest(db, source):
    row = db.execute("SELECT artifact_id,project_id,message_id,CASE WHEN length(content)<=4194304 "
                     "THEN content END AS content FROM artifacts WHERE kind=? "
                     "AND project_id=? AND message_id=? AND CASE WHEN json_valid(meta) "
                     "THEN json_extract(meta,'$.attachment_id') END=? ORDER BY artifact_id DESC LIMIT 1",
                     (KIND, source["project_id"], source["message_id"], source["attachment_id"])).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row["content"])
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    return {**value, "artifact_id": row["artifact_id"], "project_id": row["project_id"],
            "message_id": row["message_id"]}


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


def view(db, attachment_id, project_id=None, include_text=False):
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
    prior = _latest(db, source)
    return {"state": prior["state"] if _matching(prior, source) and prior["state"] not in ("complete", "empty")
            else "unprocessed", "page_count": 0}


def _document_text(document):
    if not isinstance(document, dict):
        raise ValueError("output_schema")
    blocks = []
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
                if (not isinstance(cells, list) or any(not isinstance(cell, dict)
                        or type(cell.get("row")) is not int or type(cell.get("col")) is not int
                        or cell.get("contents") is not None and not isinstance(cell["contents"], str)
                        for cell in cells)):
                    raise ValueError("output_schema")
                text = "\n".join(cell.get("contents") or "" for cell in sorted(cells, key=lambda c: (c["row"], c["col"])))
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
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, env=env, cwd=source.parent, start_new_session=True)
    try:
        process.wait(timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        reap_worker(process)
        raise
    finally:
        # A successful leader must not leave a worker using private source bytes.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.returncode:
        raise ValueError({21: "engine_version", 22: "input_limits", 23: "output_limits"}.get(process.returncode, "engine_failed"))
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


def _due(doc, source):
    if not _matching(doc, source):
        return True
    if doc["state"] in ("complete", "empty"):
        return not _completed(doc)
    if doc["state"] == "unsupported":
        return False
    return (type(doc.get("attempts")) is int and doc["attempts"] < 3
            and type(doc.get("next_try")) in (int, float) and doc["next_try"] <= time.time())


def _settle(ledger, source, doc, root, generation):
    fd = writer_lock.acquire_run_lock(str(root / "run.lock"))
    if fd is None:
        return False
    try:
        if writer_lock.updater_state() != generation or generation[0]:
            return False
        with ledger.db:
            latest = _source(ledger.db, source["attachment_id"], source["project_id"])
            if (latest is None or latest["state"] != "downloaded"
                    or any(latest[key] != source[key] for key in ("message_id", "sha256", "bytes"))
                    or not _due(_latest(ledger.db, latest), latest)):
                return False
            ledger.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                              "VALUES(?,?,?,?,?,?,?)", (KIND, source["project_id"], source["message_id"],
                              json.dumps(doc, ensure_ascii=False), "yomitoku", json.dumps({
                                  "attachment_id": source["attachment_id"], "input_sha256": source["sha256"],
                                  "runtime_contract": RUNTIME_CONTRACT}), time.time()))
        return True
    finally:
        os.close(fd)


def run_pending(ledger, cfg, *, deadline=None, limit=1):
    """Run bounded local attachment text extraction without holding the shared writer lock."""
    counts = {"complete": 0, "empty": 0, "failed": 0, "unsupported": 0, "deferred": 0}
    if config_error(cfg):
        return {**counts, "reason": "config_invalid"}
    settings = cfg.get("attachment_ocr", {})
    if not settings.get("enabled", False):
        return {**counts, "reason": "disabled"}
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
        rows = ledger.db.execute("SELECT attachment_id FROM attachments WHERE state='downloaded' ORDER BY attachment_id").fetchall()
        used = 0
        for row in rows:
            source = _source(ledger.db, row[0])
            if source is None:
                continue
            prior = _latest(ledger.db, source)
            if not _due(prior, source):
                continue
            timeout = settings.get("timeout_seconds", 120)
            if deadline is not None:
                timeout = min(timeout, deadline - time.monotonic())
            if timeout < 1 or used >= limit:
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
            except (OSError, ValueError, ImportError):
                reason = "local_processing_failed"
            attempts = prior.get("attempts", 0) if _matching(prior, source) else 0
            attempts = attempts + 1 if type(attempts) is int and 0 <= attempts < 3 else 1
            methods = {page["engine"] for page in pages}
            doc = {"schema": SCHEMA, "attachment_id": source["attachment_id"], "input_sha256": source["sha256"],
                   "input_bytes": source["bytes"], "engine": "yomitoku", "engine_version": ENGINE_VERSION if pages else None,
                   "method": "mixed" if len(methods) > 1 else next(iter(methods), None),
                   "runtime_contract": RUNTIME_CONTRACT, "state": state, "pages": pages,
                   "text_hash": _hash(json.dumps(pages, ensure_ascii=False, sort_keys=True)),
                   "attempts": attempts, "next_try": time.time() + 3600, "reason": reason}
            if _settle(ledger, source, doc, root, generation):
                counts[state] += 1
            else:
                counts["deferred"] += 1
        return counts
    finally:
        os.close(fd)
