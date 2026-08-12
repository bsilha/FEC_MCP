"""One-off report-style eval for fec_mcp's rulebook tools.

Not wired into CI/pytest -- this is a manually-run report, not a merge
gate. Run it with:

    ANTHROPIC_API_KEY=sk-... .venv/bin/python evals/run_rulebook_eval.py

Optional flags:

    --case ID       Run only the given case id(s) (repeatable).
    --report PATH   Also write full per-case results as JSON to PATH.

Every case sends one question through demo/app.py's real run_turn() --
same system prompt, same full tool set (all 17 tools, not just the
rulebook ones) -- so tool selection is graded as a genuine discrimination
task rather than a rubber stamp. Two things are checked per case, both
deterministically (no LLM judge):

1. Tool selection -- did the model call (at least one of) the expected
   tool(s) for this question, and avoid any tool that would be flatly
   wrong for it (e.g. reaching for search_rulebooks on a pure live-data
   lookup, or fabricating coverage for a state with no rulebooks loaded)?

2. Citation correctness -- every citation in the model's "Sources:" block
   (parsed via demo/app.py's own _split_citations) is independently
   re-verified against the real rulebook index (server.get_rulebook_page)
   or, for AO citations, a live OpenFEC lookup (server.get_advisory_opinion)
   -- not against what the model *claims* the tool returned. A fabricated
   filename/page/AO number fails here even if the prose reads plausibly.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

from anthropic import Anthropic

from fec_mcp import server

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_APP_PATH = REPO_ROOT / "demo" / "app.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rulebook_cases import CASES, EvalCase  # noqa: E402


def _load_demo_app():
    spec = importlib.util.spec_from_file_location("fec_mcp_demo_app", DEMO_APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


demo_app = _load_demo_app()


def _grade_tool_selection(case: EvalCase, trace: list[dict]) -> list[str]:
    failures = []
    called = {t["name"] for t in trace}

    if case.expect_any_of and not called & set(case.expect_any_of):
        got = sorted(called) if called else "(no tool calls)"
        failures.append(f"expected one of {case.expect_any_of}, got {got}")

    forbidden_hit = called & set(case.forbid)
    if forbidden_hit:
        failures.append(f"forbidden tool(s) called: {sorted(forbidden_hit)}")

    if case.arg_check is not None:
        reason = case.arg_check(trace)
        if reason:
            failures.append(reason)

    return failures


def _grade_citations(text: str) -> list[str]:
    _, citations = demo_app._split_citations(text)
    failures = []

    for citation in citations:
        if citation["kind"] == "source":
            filename, page_raw = citation["filename"], citation["page"]
            try:
                page = int(page_raw)
            except ValueError:
                failures.append(f"citation page {page_raw!r} for {filename!r} isn't numeric")
                continue
            result = server.get_rulebook_page(filename, page)
            if "error" in result:
                failures.append(f"fabricated citation: {filename!r} p.{page} -- {result['error']}")

        elif citation["kind"] == "ao":
            ao_no = citation["ao_no"]
            server._openfec_client = None
            try:
                data = asyncio.run(server.get_advisory_opinion(ao_no))
            except Exception as exc:  # live network call -- report, don't crash the run
                failures.append(f"couldn't verify AO {ao_no!r}: {exc}")
                continue
            if "error" in data or not data.get("docs"):
                failures.append(f"fabricated citation: AO {ao_no!r} has no matching documents")

    return failures


def run_case(client: Anthropic, case: EvalCase) -> dict:
    result = demo_app.run_turn(client, [], case.question)
    tool_failures = _grade_tool_selection(case, result["trace"])
    citation_failures = _grade_citations(result["text"]) if case.check_citations else []
    failures = tool_failures + citation_failures
    return {
        "id": case.id,
        "question": case.question,
        "passed": not failures,
        "failures": failures,
        "trace": [t["name"] for t in result["trace"]],
        "answer": result["text"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", action="append", help="Run only case(s) with this id (repeatable).")
    parser.add_argument("--report", type=Path, help="Also write full per-case results as JSON to this path.")
    args = parser.parse_args()

    cases = CASES
    if args.case:
        wanted = set(args.case)
        cases = [c for c in cases if c.id in wanted]
        missing = wanted - {c.id for c in cases}
        if missing:
            sys.exit(f"Unknown case id(s): {sorted(missing)}")

    client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment

    results = []
    passed = 0
    for case in cases:
        outcome = run_case(client, case)
        results.append(outcome)
        status = "PASS" if outcome["passed"] else "FAIL"
        print(f"[{status}] {case.id}: {case.question}")
        print(f"       tools called: {outcome['trace']}")
        for reason in outcome["failures"]:
            print(f"       - {reason}")
        if outcome["passed"]:
            passed += 1

    print(f"\n{passed}/{len(cases)} cases passed")

    if args.report:
        args.report.write_text(json.dumps(results, indent=2))
        print(f"Wrote full report to {args.report}")

    if passed != len(cases):
        sys.exit(1)


if __name__ == "__main__":
    main()
