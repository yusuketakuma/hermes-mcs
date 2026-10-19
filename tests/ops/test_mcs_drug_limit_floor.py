"""`related` and `master-impact` refuse `--limit 0` up front instead of blaming the inputs."""
import hashlib

import pytest

import mcs_drug
import mcs_requests
from test_drug_map import DOCUMENT


def test_related_limit_zero_does_not_blame_a_valid_dictionary(tmp_path, capsys):
    path = tmp_path / "synthetic-dictionary.json"
    raw = mcs_requests.canonical(DOCUMENT)
    path.write_bytes(raw)
    path.chmod(0o600)
    sha = hashlib.sha256(raw).hexdigest()
    # Same dictionary, default limit: works.
    assert mcs_drug.main(["related", "キラナ", "--dictionary", str(path),
                          "--sha256", sha]) == 0
    capsys.readouterr()
    try:
        code = mcs_drug.main(["related", "キラナ", "--dictionary", str(path),
                              "--sha256", sha, "--limit", "0"])
    except SystemExit as exit_:   # an argparse range refusal is an acceptable fix
        code = exit_.code
    err = capsys.readouterr().err
    assert "辞書の設定・所有者権限・SHA256を確認してください" not in err, (code, err)


def test_master_impact_limit_zero_is_refused_by_argparse(tmp_path, capsys):
    missing = str(tmp_path / "missing")
    with pytest.raises(SystemExit) as exit_:
        mcs_drug.main(["master-impact", "--before", missing, "--before-pin", missing,
                       "--after", missing, "--after-pin", missing, "--limit", "0"])
    assert exit_.value.code == 2
    err = capsys.readouterr().err
    assert "limit は1〜200の整数です" in err
    assert "権限とSHA256を確認してください" not in err
