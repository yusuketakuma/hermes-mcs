#!/usr/bin/env python3
"""Synthetic shadow end-to-end bench driver (T16).

Runs the canonical semantic-facts/v2 pipeline — extraction, relation
reconciliation, bidirectional audit, one targeted repair, mandatory
rendering — over the synthetic bench corpus against the configured
local LLM (real Qwen when running) and, when TYPESAFE_API_KEY is set, a
real Jev client.

Usage:
  python3 scripts/development/semantic_shadow_e2e.py \
      --cases evaluation/semantic_completeness_cases.json \
      --out /tmp/shadow-e2e.json [--jev] [--deadline 300]

Without --jev (or without the API key) the audit stages honestly
report INCOMPLETE — the driver never fabricates a pass.
"""
import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))
import _mcs_path  # noqa: F401  registers every subdir as an import root


def main() -> int:
    parser = argparse.ArgumentParser(
        description="canonical shadow end-to-end bench")
    parser.add_argument("--cases", required=True,
                        help="bench cases JSON (synthetic corpus)")
    parser.add_argument("--out", required=True, help="report JSON path")
    parser.add_argument("--jev", action="store_true",
                        help="use a real Jev client (TYPESAFE_API_KEY)")
    parser.add_argument("--deadline", type=float, default=300.0)
    args = parser.parse_args()
    if not math.isfinite(args.deadline) or args.deadline <= 0:
        parser.error("--deadline must be finite and greater than zero")

    import semantic
    import semantic_evaluation

    try:
        payload = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as error:
        print(f"cases unreadable: {error}", file=sys.stderr)
        return 1
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list) or not cases or any(
            not isinstance(case, dict) or case.get("messages") and (
                not isinstance(case["messages"], list)
                or any(not isinstance(msg, dict) for msg in case["messages"])) for case in cases):
        print("cases file requires case objects and message objects", file=sys.stderr)
        return 1

    jev_client = None
    if args.jev:
        import semantic_jev as jev
        from mcs_util import env_value
        api_key = env_value("TYPESAFE_API_KEY")
        if api_key:
            jev_client = jev.JevClient(api_key=api_key,
                                       model=jev.JEV_MODEL)
        else:
            print("warning: TYPESAFE_API_KEY unset — audit stages will "
                  "be INCOMPLETE", file=sys.stderr)

    report = semantic_evaluation.run_shadow_e2e(
        cases, semantic.llm_chat, jev_client, deadline_s=args.deadline)
    Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(f"shadow-e2e: cases={report['cases']} "
          f"complete={report['complete']} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
