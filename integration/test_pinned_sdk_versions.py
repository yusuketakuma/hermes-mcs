"""Metadata and immutable-source checks for the explicit real-SDK lanes."""
import hashlib
from importlib import metadata, util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def lane(request):
    value = request.config.getoption("--sdk-lane", default=None)
    if value is None:
        pytest.skip("explicit SDK lane: sh scripts/check_pinned_sdks.sh")
    assert (3, 11) <= sys.version_info[:2] < (3, 14)
    return value


def _pin(requirement):
    name, version = requirement.split("==", 1)
    return name.split("[", 1)[0], version


def test_actual_distribution_versions_match_lane(lane, request):
    if lane == "standalone":
        requirements = []
        for line in (ROOT / "deployment/requirements-standalone.txt").read_text().splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            requirement, _, marker = line.partition(";")
            if marker:
                match = re.fullmatch(r'\s*python_version (>=|<) "(\d+)\.(\d+)"', marker)
                assert match is not None, marker
                target = tuple(map(int, match.groups()[1:]))
                if ((sys.version_info[:2] >= target) if match[1] == ">="
                        else (sys.version_info[:2] < target)) is False:
                    continue
            requirements.append(requirement.strip())
        requirements.append("pytest==9.1.1")
    else:
        import tomllib

        project = request.config.getoption("--hermes-project")
        assert project, "explicit immutable Hermes source archive is required"
        data = tomllib.loads(Path(project).read_text())
        requirements = data["project"]["optional-dependencies"]["messaging"]
        requirements += [r for r in data["project"]["optional-dependencies"]["dev"]
                         if r.startswith("pytest==")]
        assert metadata.version("hermes-agent") == data["project"]["version"]
    actual = {}
    for requirement in requirements:
        name, expected = _pin(requirement)
        actual[name] = metadata.version(name)
        assert actual[name] == expected, (name, expected, actual[name])
    print(json.dumps({"sdk_lane": lane, "python": sys.version.split()[0], "packages": actual},
                     sort_keys=True))


def test_hermes_import_source_matches_ci_pin(lane, request):
    if lane != "hermes":
        return
    project = Path(request.config.getoption("--hermes-project")).resolve().parent
    reference = request.config.getoption("--hermes-reference-root")
    assert reference, "local Git objects for the CI Hermes pin are required"
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    pin = re.search(r"repository: yusuketakuma/hermes-agent\s+ref: ([0-9a-f]{40})", workflow)
    assert pin is not None
    result = subprocess.run(
        ["git", "-C", reference, "archive", "--format=tar", pin[1]],
        capture_output=True, check=True, timeout=30)
    # Git archive applies committed export/EOL attributes (e.g. ps1 CRLF).
    # Compare every exported file, not raw blobs with different line endings.
    files = 0
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        for member in archive:
            source = project / member.name
            if member.isdir():
                continue
            if member.issym():
                assert str(source.readlink()) == member.linkname, member.name
            else:
                assert member.isfile(), member.name
                exported = archive.extractfile(member)
                assert exported is not None, member.name
                expected = hashlib.sha256(exported.read()).digest()
                assert hashlib.sha256(source.read_bytes()).digest() == expected, member.name
            files += 1
    gateway = util.find_spec("gateway")
    assert gateway is not None and gateway.origin is not None
    assert Path(gateway.origin).resolve().is_relative_to(project)
    assert not (project / ".env").exists()
    print(json.dumps({"hermes_ci_pin": pin[1], "verified_archive_files": files}, sort_keys=True))
