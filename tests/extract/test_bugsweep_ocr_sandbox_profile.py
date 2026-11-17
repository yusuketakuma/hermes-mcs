"""OCR sandbox profiles keep non-ASCII paths literal because SBPL does not decode \\uXXXX escapes."""
from pathlib import Path
import subprocess
import sys

import pytest

import attachment_ocr as ocr


def _profile(tmp_path):
    root = tmp_path / '合成"書類'
    hub = root / "data" / "huggingface" / "hub"
    hub.mkdir(parents=True)
    (hub / "synthetic-model").write_text("synthetic model")
    venv = root / "venv"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python"
    python.symlink_to(Path(sys.executable).resolve())
    private = root / "private"
    private.mkdir()
    source = private / "source.pdf"
    source.write_text("synthetic source")
    return ocr._sandbox_profile(str(venv / "bin" / "yomitoku"), str(python), source, hub), hub, source


def test_profile_keeps_non_ascii_paths_literal_and_escapes_quotes(tmp_path):
    profile, hub, source = _profile(tmp_path)
    assert "\\u" not in profile
    assert '合成\\"書類' in profile
    assert '(allow file-read* (subpath "' + str(hub).replace('"', '\\"') + '"))' in profile
    assert '(allow file-write* (subpath "' + str(source.parent.resolve()).replace('"', '\\"') + '"))' in profile


@pytest.mark.skipif(not Path("/usr/bin/sandbox-exec").exists(), reason="macOS sandbox runtime only")
def test_sandbox_reads_synthetic_file_under_non_ascii_hub(tmp_path):
    profile, hub, _source = _profile(tmp_path)
    result = subprocess.run(["/usr/bin/sandbox-exec", "-p", profile, "/bin/cat", str(hub / "synthetic-model")],
                            env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "synthetic model"
