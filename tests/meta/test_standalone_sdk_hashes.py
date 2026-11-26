"""The hashed standalone SDK list names exactly the pinned versions, and install.sh installs only from it."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
_PIN = re.compile(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9.!+_-]+)")


def _pins(text):
    return {re.sub("[-_.]+", "-", name).lower(): version for name, version in _PIN.findall(text)}


def test_every_pin_has_its_hashes_and_no_other_package_is_listed():
    pins = _pins("\n".join(line for line in (ROOT / "deployment/requirements-standalone.txt").read_text().splitlines()
                           if not line.startswith("#")))
    lock = (ROOT / "deployment/requirements-standalone.lock").read_text()
    entries = [block for block in re.split(r"\n(?=\S)", "\n".join(
        line for line in lock.splitlines() if not line.startswith("#"))) if block.strip()]
    assert _pins("\n".join(entry.splitlines()[0] for entry in entries)) == pins and len(entries) == len(pins)
    for entry in entries:
        first, *rest = entry.splitlines()
        assert _PIN.fullmatch(first.rstrip(" \\")), first
        assert rest and all(re.fullmatch(r"    --hash=sha256:[0-9a-f]{64}( \\)?", line) for line in rest), first


def test_install_refuses_a_download_that_is_not_in_the_hashed_list():
    script = (ROOT / "deployment/install/runtime.sh").read_text()
    installs = [line.strip() for line in script.splitlines() if "pip install" in line and "REQUIREMENTS" in line]
    assert installs == ['uv pip install --python "$VENV_PY" -r "$REQUIREMENTS_LOCK" --require-hashes',
                        '"$VENV_PY" -m pip install -r "$REQUIREMENTS_LOCK" --require-hashes']
    preflight = (ROOT / "mcs/ops/update/update_preflight.py").read_text()
    # a tag that changes either file is never applied without install.sh; a rollback looks at the pins alone
    assert ('_STANDALONE_SDK_FILES = ("deployment/requirements-standalone.txt", '
            '"deployment/requirements-standalone.lock")') in preflight
    assert preflight.count("*_STANDALONE_SDK_FILES") == 1 and preflight.count("_STANDALONE_SDK_FILES[0]") == 1


def test_the_witness_of_the_transfer_record_is_ignored_like_the_record():
    # ~/.mcs is both the checkout and the runtime home; an untracked file there holds managed updates.
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert "external_transfer.json" in ignored and "external_transfer.required" in ignored
