"""Characterize standard tmp_path lifetimes through the guarded official runner."""
import json
from pathlib import Path
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]


def test_standard_tmp_path_retention_preserves_live_fixture_lifetimes(tmp_path, record_testsuite_property):
    retained = {}
    for policy in ("all", "failed"):
        root = tmp_path / policy
        root.mkdir()
        for directory in ("home", "tmp"):
            (root / directory).mkdir()
        (root / "test_lifetime.py").write_text(textwrap.dedent('''
            import json
            from pathlib import Path
            import pytest
            import os
            import socket
            import conftest as root_guard

            assert os.environ.get("MCS_TEST_SANDBOX") == "1"
            assert socket.socket.connect.__name__ == "_blocked"
            assert root_guard._hang_stderr is not None and root_guard._test_timeout() == 300

            @pytest.fixture(scope="module")
            def shared(tmp_path_factory):
                path = tmp_path_factory.mktemp("shared")
                state = {"path": path, "windows": []}
                Path(__file__).with_name("shared.json").write_text(json.dumps(str(path)))
                return state

            @pytest.fixture
            def recorded(tmp_path, request):
                name = request.node.name
                Path(__file__).with_name(name + ".json").write_text(json.dumps(str(tmp_path)))
                with (tmp_path / "payload").open("w+") as stream:
                    stream.write("synthetic fixture")
                    stream.flush()
                    yield tmp_path
                    # Dependent finalizers still own their live files/FD until done.
                    assert tmp_path.exists()
                    stream.seek(0)
                    assert stream.read() == "synthetic fixture"
                Path(__file__).with_name(name + ".teardown").write_text("closed")

            @pytest.fixture
            def broken_setup(recorded):
                raise RuntimeError("synthetic setup failure")

            @pytest.fixture
            def broken_teardown(recorded):
                yield
                assert recorded.exists()
                raise RuntimeError("synthetic teardown failure")

            def test_01_pass(recorded, shared):
                identity = id(shared)
                for window in range(3):
                    for call in range(5):
                        assert id(shared) == identity and recorded.exists()
                        shared["windows"].append((window, call))
                assert len(shared["windows"]) == 15

            def test_02_shared_still_live(recorded, shared):
                assert len(shared["windows"]) == 15 and shared["path"].exists()

            def test_03_call_failure(recorded):
                assert False, "synthetic call failure"

            def test_04_setup_failure(broken_setup):
                raise AssertionError("setup failure must prevent call")

            def test_05_teardown_failure(broken_teardown):
                pass
        '''))
        base, xml = root / "base", root / "result.xml"
        override = ["-o", "tmp_path_retention_policy=all"] if policy == "all" else []
        result = subprocess.run(
            ["/bin/sh", str(ROOT / "scripts/run_tests.sh"), "-q", "-o", "addopts=",
             "-p", "conftest", "-p", "integration.conftest", "-p", "no:cacheprovider",
             "-c", str(ROOT / "pyproject.toml"), "--confcutdir", str(root),
             "--basetemp", str(base), "--junitxml", str(xml), *override, str(root / "test_lifetime.py")],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
            env={"HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
                 "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "UTC",
                 "MCS_TEST_PYTHON": sys.executable, "PYTHONDONTWRITEBYTECODE": "1"})
        assert result.returncode == 1, result.stdout + result.stderr
        report = ET.parse(xml)
        assert len(report.findall(".//failure")) == 1
        assert len(report.findall(".//error")) == 2
        assert not report.findall(".//skipped")
        names = ["test_01_pass", "test_02_shared_still_live", "test_03_call_failure",
                 "test_04_setup_failure", "test_05_teardown_failure"]
        assert {node.get("name") for node in report.findall(".//testcase")} == set(names)
        retained[policy] = {"files": 0, "bytes": 0}
        for name in names:
            path = Path(json.loads((root / (name + ".json")).read_text()))
            assert path.parent == base
            assert path.exists() is (policy == "all" or name == "test_03_call_failure")
            assert (root / (name + ".teardown")).read_text() == "closed"
            if path.exists():
                retained[policy]["files"] += 1
                retained[policy]["bytes"] += (path / "payload").stat().st_size
        assert Path(json.loads((root / "shared.json").read_text())).exists()
    assert (tmp_path / "all/test_lifetime.py").read_bytes() == (tmp_path / "failed/test_lifetime.py").read_bytes()
    assert retained["all"]["files"] == 5 and retained["failed"]["files"] == 1
    assert retained["all"]["bytes"] == 5 * retained["failed"]["bytes"] > 0
    record_testsuite_property("synthetic_tmp_path_retained", json.dumps(retained, sort_keys=True))
