"""An unset admission path never opens a broker in the current directory (temp dirs only)."""
import os

import pytest

import local_llm


def test_broker_without_a_path_refuses_instead_of_writing_beside_the_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("MCS_LLM_ADMISSION", raising=False)
    monkeypatch.setattr(local_llm, "_configured_admission", lambda: None)
    monkeypatch.setattr(local_llm, "_BROKERS", {})
    monkeypatch.chdir(tmp_path)
    assert local_llm._admission_db_path() == ""
    with pytest.raises(ValueError, match="^llm_admission_path_unset$"):
        local_llm._broker()
    assert os.listdir(tmp_path) == [] and local_llm._BROKERS == {}
