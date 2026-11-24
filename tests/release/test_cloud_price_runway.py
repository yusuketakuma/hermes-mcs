"""Release gate: every current cloud price table needs >21 days of runway.

The reference date is the date of the newest version in CHANGELOG.md — the
release this tree describes — not the day the check happens to run, so the
gate is stable between releases and only fails when a release is cut too close
to a recheck deadline. Historical snapshots are never gated: they stay so
stored reservations keep resolving by the ``price_version`` name they were
written with (``record_result`` looks the version up through
``_price_definition``), and by-name lookups never check expiry.

release-notes.yml runs this directory through ``python3 -m unittest
discover``, so the file must be unittest-style and bootstrap the flat-import
path itself — pytest conftest does not run there.
"""

from datetime import date, datetime, timedelta
import re
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))
import _mcs_path  # noqa: E402,F401 - registers the flat-import module dirs

import anthropic_client  # noqa: E402
import cloud_usage_accounting as accounting  # noqa: E402
import openai_client  # noqa: E402
import provider_registry  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
RUNWAY_DAYS = 21
VERSION_DATE = re.compile(r"^## \[\d+\.\d+\.\d+\] — (\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)
CURRENT_MODELS = {
    anthropic_client.MODEL, anthropic_client.SONNET_MODEL,
    openai_client.MODEL, openai_client.SOL_MODEL,
}
USAGE = {
    "anthropic": {"input_tokens": 1000, "output_tokens": 100},
    "openai": {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100,
               "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}},
}
# Fixed inventory of every issued price version — named here, never read back
# from the provider modules, so a renamed or dropped version fails the check.
EXPECTED_SNAPSHOT_VERSIONS = frozenset({
    ("claude-haiku-5-5", "anthropic-haiku-5-5-2026-10-26"),
    ("claude-haiku-5-5", "anthropic-haiku-5-5-2026-11-15"),
    ("claude-sonnet-5-5", "anthropic-sonnet-5-5-2026-11-15"),
    ("gpt-6-luna", "openai-gpt-6-luna-2026-10-29"),
    ("gpt-6-luna", "openai-gpt-6-luna-2026-11-15"),
    ("gpt-6.1-sol", "openai-gpt-6.1-sol-2026-10-31"),
    ("gpt-6.1-sol", "openai-gpt-6.1-sol-2026-11-15"),
})


def _at(day):
    return datetime(day.year, day.month, day.day, 12, tzinfo=accounting.JST).timestamp()


def _current_models():
    return {model for module in provider_registry.PROVIDER_MODULES.values()
            for model in module.CURRENT_PRICE_VERSIONS}


def _reference_date():
    match = VERSION_DATE.search((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    assert match, "CHANGELOG.md needs at least one dated version heading"
    return date.fromisoformat(match.group(1))


def _runway_ok(now):
    return all(accounting.price_info(model, now=now)["price_days_remaining"] > RUNWAY_DAYS
               for model in _current_models())


class CloudPriceRunwayTest(unittest.TestCase):
    def test_gate_covers_exactly_the_four_current_models(self):
        self.assertEqual(_current_models(), CURRENT_MODELS)

    def test_current_price_runway_at_the_release_reference_date(self):
        now = _at(_reference_date())
        short = {model: accounting.price_info(model, now=now)
                 for model in sorted(_current_models())
                 if accounting.price_info(model, now=now)["price_days_remaining"] <= RUNWAY_DAYS}
        self.assertFalse(
            short,
            "official prices must be rechecked and the price tables updated first: "
            + ", ".join(f"{model} expires {info['price_valid_until']} "
                        f"({info['price_days_remaining']} days left)"
                        for model, info in short.items()))

    def test_runway_boundary_is_21_days_fail_and_22_pass(self):
        deadline = min(date.fromisoformat(accounting.price_info(model)["price_valid_until"])
                       for model in _current_models())
        self.assertFalse(_runway_ok(_at(deadline - timedelta(days=RUNWAY_DAYS))))
        self.assertTrue(_runway_ok(_at(deadline - timedelta(days=RUNWAY_DAYS + 1))))

    def test_issued_price_versions_resolve_by_name_for_reservation_reconciliation(self):
        """Every issued price version — the four current 2026-11-15 tables plus
        the historical ones stored reservations may still name — resolves
        through the same by-name path ``record_result`` uses. The inventory is
        a fixed list, not derived from the implementation constants, so a
        version renamed in a provider module or dropped from PRICE_SNAPSHOTS
        fails here."""
        for module in provider_registry.PROVIDER_MODULES.values():
            self.assertLessEqual(set(module.CURRENT_PRICE_VERSIONS.items()),
                                 set(module.PRICE_SNAPSHOTS))
            self.assertGreater(
                len(module.PRICE_SNAPSHOTS), len(module.CURRENT_PRICE_VERSIONS),
                "a recheck with unchanged prices keeps the older table so stored "
                "reservations reconcile")
        for model, version in sorted(EXPECTED_SNAPSHOT_VERSIONS):
            definition = accounting._price_definition(model, version)
            self.assertIs(definition, accounting.PRICE_DEFINITIONS[(model, version)])
            self.assertGreater(
                accounting.usage_cost(USAGE[definition["provider"]], model=model,
                                      price_version=version), 0)


if __name__ == "__main__":
    unittest.main()
