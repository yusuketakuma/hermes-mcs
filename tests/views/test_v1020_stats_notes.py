"""Stats say when a scope does not apply and that a count is not a rate."""
import ledger
from mcs_stats import run_stats

NOW = 1_800_000_000


def _stat(tmp_path, name, args=None):
    store = ledger.Ledger(str(tmp_path / "ledger.db"))
    try:
        return run_stats(store.db, NOW, {"stat": name, **(args or {})})["stats"][name]
    finally:
        store.close()


def test_card_parts_says_the_time_and_project_scope_do_not_apply(tmp_path):
    st = _stat(tmp_path, "card_parts", {"as_of": "2026-09-01"})
    assert st["status"] == "ok"
    assert any("--as-of/--since/--until/--project do not apply" in n for n in st["notes"])


def test_change_mentions_is_documented_as_a_count_not_a_rate(tmp_path):
    st = _stat(tmp_path, "med_change_followup")
    assert set(st["change_mentions_7d_plus"]) == {
        "numerator", "denominator", "unit", "value", "reason"}
    assert any("not a follow-up or missed-work rate" in n for n in st["notes"])
