"""extract_llm.resolved_model keeps the patch point and the config override."""
import extract_llm
import local_llm


def test_config_model_overrides_the_default():
    assert extract_llm.resolved_model(None) == local_llm.MODEL
    cfg = {"local_llm": {"model": "synthetic-model.gguf"}}
    assert extract_llm.resolved_model(cfg) == "synthetic-model.gguf"


def test_patched_constant_wins_over_config(monkeypatch):
    monkeypatch.setattr(extract_llm, "MODEL", "patched-model")
    cfg = {"local_llm": {"model": "synthetic-model.gguf"}}
    assert extract_llm.resolved_model(cfg) == "patched-model"
