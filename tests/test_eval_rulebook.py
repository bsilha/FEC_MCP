"""Regression coverage for evals/run_rulebook_eval.py's grading logic.

Only the grading functions are tested here -- they're pure functions over
a trace/answer-text the harness itself produces, and can be checked
offline against the real local rulebook index (no ANTHROPIC_API_KEY or
network access needed). Actually running the eval's live Claude calls is
out of scope for CI; see evals/README.md.
"""

import importlib.util
import sys
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parents[1] / "evals"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(EVALS_DIR))
rulebook_cases = _load("rulebook_cases", EVALS_DIR / "rulebook_cases.py")
run_eval = _load("run_rulebook_eval", EVALS_DIR / "run_rulebook_eval.py")

EvalCase = rulebook_cases.EvalCase


def test_grade_citations_accepts_a_real_source_citation():
    text = "Answer.\n\nSources:\nSOURCE | candgui.pdf | 1 | federal"
    assert run_eval._grade_citations(text) == []


def test_grade_citations_flags_a_page_that_does_not_exist():
    text = "Answer.\n\nSources:\nSOURCE | candgui.pdf | 99999 | federal"
    failures = run_eval._grade_citations(text)
    assert len(failures) == 1
    assert "fabricated citation" in failures[0]
    assert "candgui.pdf" in failures[0]


def test_grade_citations_flags_a_page_that_does_not_exist_for_a_state_pdf():
    text = "Answer.\n\nSources:\nSOURCE | states/ca/Manual_1_Final.pdf | 999999 | ca"
    failures = run_eval._grade_citations(text)
    assert len(failures) == 1
    assert "fabricated citation" in failures[0]


def test_grade_citations_flags_non_numeric_page():
    text = "Answer.\n\nSources:\nSOURCE | candgui.pdf | n/a | federal"
    failures = run_eval._grade_citations(text)
    assert len(failures) == 1
    assert "isn't numeric" in failures[0]


def test_grade_citations_ignores_answers_with_no_sources_block():
    assert run_eval._grade_citations("Just a plain answer, no citations.") == []


def test_grade_tool_selection_passes_when_an_expected_tool_was_called():
    case = EvalCase(id="t", question="q", expect_any_of=("search_rulebooks",))
    trace = [{"name": "search_rulebooks", "input": {}}]
    assert run_eval._grade_tool_selection(case, trace) == []


def test_grade_tool_selection_fails_when_no_expected_tool_was_called():
    case = EvalCase(id="t", question="q", expect_any_of=("search_rulebooks",))
    trace = [{"name": "get_candidate", "input": {}}]
    failures = run_eval._grade_tool_selection(case, trace)
    assert len(failures) == 1
    assert "expected one of" in failures[0]


def test_grade_tool_selection_fails_when_a_forbidden_tool_was_called():
    case = EvalCase(id="t", question="q", expect_any_of=("search_rulebooks",), forbid=("get_committee",))
    trace = [{"name": "search_rulebooks", "input": {}}, {"name": "get_committee", "input": {}}]
    failures = run_eval._grade_tool_selection(case, trace)
    assert any("forbidden tool" in f for f in failures)


def test_grade_tool_selection_runs_arg_check():
    case = EvalCase(
        id="t",
        question="q",
        expect_any_of=("search_rulebooks",),
        arg_check=lambda trace: "always fails",
    )
    trace = [{"name": "search_rulebooks", "input": {}}]
    assert run_eval._grade_tool_selection(case, trace) == ["always fails"]


def test_expect_jurisdiction_passes_when_jurisdiction_matches():
    check = rulebook_cases._expect_jurisdiction("search_rulebooks", jurisdiction="ca")
    trace = [{"name": "search_rulebooks", "input": {"jurisdiction": "ca"}}]
    assert check(trace) is None


def test_expect_jurisdiction_fails_when_jurisdiction_differs():
    check = rulebook_cases._expect_jurisdiction("search_rulebooks", jurisdiction="ca")
    trace = [{"name": "search_rulebooks", "input": {"jurisdiction": "federal"}}]
    assert check(trace) is not None


def test_forbid_jurisdiction_flags_a_call_to_an_unloaded_state():
    check = rulebook_cases._forbid_jurisdiction("tx")
    trace = [{"name": "search_rulebooks", "input": {"jurisdiction": "tx"}}]
    assert check(trace) is not None


def test_forbid_jurisdiction_passes_when_that_state_was_never_used():
    check = rulebook_cases._forbid_jurisdiction("tx")
    trace = [{"name": "list_rulebook_jurisdictions", "input": {}}]
    assert check(trace) is None


def test_all_cases_have_unique_ids():
    ids = [c.id for c in rulebook_cases.CASES]
    assert len(ids) == len(set(ids))


def test_all_cases_expect_or_forbid_something():
    """A case with neither expect_any_of nor forbid set would silently
    pass no matter what the model did -- guard against an empty case
    slipping in unnoticed."""
    for case in rulebook_cases.CASES:
        assert case.expect_any_of or case.forbid, f"{case.id} asserts nothing"
