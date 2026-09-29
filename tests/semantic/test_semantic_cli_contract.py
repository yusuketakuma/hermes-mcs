"""Offline/status entry points cannot implicitly start a model drain."""
import pytest
import semantic
from semantic_testkit import _seeded


def test_offline_status_is_read_only_and_drain_requires_explicit_action(tmp_path, monkeypatch, capsys):
    db = _seeded(tmp_path)
    path = tmp_path / "ledger.db"
    db.close()
    monkeypatch.setattr(semantic, "DB", str(path))
    before = path.read_bytes()
    monkeypatch.setattr(semantic.sys, "argv", ["semantic", "--status", "--offline"])
    assert semantic.main() == 0
    assert path.read_bytes() == before
    for flags in ([], ["--drain", "--offline"], ["--drain", "--max-jobs", "-1"],
                  ["--replay", "0", "--offline"], ["--replay", "-1"],
                  ["--replay", str(2**63)]):
        monkeypatch.setattr(semantic.sys, "argv", ["semantic", *flags])
        with pytest.raises(SystemExit) as error:
            semantic.main()
        assert error.value.code == 2
    assert path.read_bytes() == before
    capsys.readouterr()
