"""Injected timeout compatibility is based on keyword support, not parameter name."""
import time

import pytest

import semantic_drain as drain
import semantic_runtime as runtime


def positional_only(prompt, timeout=None, /):
    return prompt


def variadic_positional(prompt, *timeout):
    return prompt


@pytest.mark.parametrize("model", [positional_only, variadic_positional])
def test_non_keyword_timeout_callable_retains_prompt_only_protocol(model):
    assert runtime.accepts_timeout(model) is False
    assert runtime.llm_call(model, "SYNTH", time.monotonic() + 10) == "SYNTH"
    timed, _ = drain._timed_llm(model)
    assert runtime.accepts_timeout(timed) is False
    assert runtime.llm_call(timed, "SYNTH", time.monotonic() + 10) == "SYNTH"
