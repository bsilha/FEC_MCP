"""Test cases for evals/run_rulebook_eval.py.

Each case is one natural-language question run through the exact same
demo/app.py chat turn used by the real app (full tool set registered, same
system prompt), graded on two axes:

- Tool selection: did Claude call (at least one of) the expected tool(s),
  and avoid any forbidden ones? Checked against the trace of tool names/
  args the model actually produced.
- Citation correctness: verified separately, in run_rulebook_eval.py, by
  re-checking every citation in the final answer against the real
  rulebook index / OpenFEC AO lookup -- not defined per-case here.

Kept intentionally narrow (rulebook tools) per the current eval scope, but
registers the full tool set from demo/app.py so tool selection is a real
discrimination task -- correctly *not* calling a rulebook tool matters as
much as correctly calling one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# Every tool name in demo/app.py's TOOLS list, for the "expect zero tool
# calls at all" negative-control case below.
#
# This list going stale is a silent hole rather than an error: rb-20 only
# fails on a tool it knows to forbid, so any tool missing here is one the
# model may call on a plain arithmetic question without the case
# noticing. It had already drifted once -- the two deadline tools were
# added to the app months after the eval was written, and rb-20 stopped
# covering them without anything going red. test_eval_rulebook.py now
# asserts this matches the app's own list, so the next addition fails
# loudly instead.
ALL_TOOLS = (
    "list_rulebook_jurisdictions",
    "list_rulebook_sources",
    "search_rulebooks",
    "get_rulebook_page",
    "search_candidates",
    "get_candidate",
    "get_candidate_totals",
    "search_committees",
    "get_committee",
    "get_committee_filings",
    "get_committee_totals",
    "get_rad_analyst",
    "search_disbursements",
    "search_filings",
    "search_elections",
    "get_reporting_calendar",
    "get_committee_deadlines",
    "send_deadline_invites",
    "search_advisory_opinions",
    "get_advisory_opinion",
)


@dataclass
class EvalCase:
    id: str
    question: str
    expect_any_of: tuple[str, ...] = ()
    forbid: tuple[str, ...] = ()
    arg_check: Callable[[list[dict]], str | None] | None = None
    check_citations: bool = True
    notes: str = ""


def _expect_jurisdiction(*tools: str, jurisdiction: str) -> Callable[[list[dict]], str | None]:
    """Fails unless one of `tools` was called with jurisdiction=<jurisdiction>."""

    def check(trace: list[dict]) -> str | None:
        for call in trace:
            if call["name"] in tools and call["input"].get("jurisdiction") == jurisdiction:
                return None
        return f"no call to {tools} passed jurisdiction={jurisdiction!r}"

    return check


def _forbid_jurisdiction(jurisdiction: str) -> Callable[[list[dict]], str | None]:
    """Fails if any call passed the given (not-loaded) jurisdiction -- a
    fabricated-coverage smell, e.g. treating an unloaded state as covered."""

    def check(trace: list[dict]) -> str | None:
        for call in trace:
            if call["input"].get("jurisdiction") == jurisdiction:
                return f"{call['name']} was called with jurisdiction={jurisdiction!r}, which isn't loaded"
        return None

    return check


CASES: list[EvalCase] = [
    # -- Federal compliance questions: search_rulebooks, no jurisdiction filter required --
    EvalCase(
        id="rb-01-individual-contribution-limit",
        question="What's the individual contribution limit to a candidate's committee per election?",
        expect_any_of=("search_rulebooks",),
    ),
    EvalCase(
        id="rb-02-disclaimer-requirements",
        question="What disclaimer is required on a campaign's paid Facebook ads?",
        expect_any_of=("search_rulebooks",),
    ),
    EvalCase(
        id="rb-03-foreign-national-ban",
        question="Can a campaign accept a contribution from a foreign national?",
        expect_any_of=("search_rulebooks",),
    ),
    EvalCase(
        id="rb-04-personal-use-of-funds",
        question="Can a candidate use leftover campaign funds to pay their personal mortgage?",
        expect_any_of=("search_rulebooks",),
    ),
    EvalCase(
        id="rb-05-recordkeeping",
        question="What records does a treasurer need to keep for contributions over $200?",
        expect_any_of=("search_rulebooks",),
    ),
    EvalCase(
        id="rb-06-joint-fundraising",
        question="What are the rules for a joint fundraising committee splitting proceeds between two campaigns?",
        expect_any_of=("search_rulebooks",),
    ),
    EvalCase(
        id="rb-07-coordinated-communications",
        question="When does a communication paid for by an outside group count as 'coordinated' with a candidate?",
        expect_any_of=("search_rulebooks",),
    ),
    EvalCase(
        id="rb-08-pac-registration-threshold",
        question="At what dollar threshold does a federal PAC have to register with the FEC?",
        expect_any_of=("search_rulebooks",),
    ),
    # -- State-specific questions: must filter to the right jurisdiction --
    EvalCase(
        id="rb-09-ca-major-donor-committee",
        question="Under California law, when does a major donor committee have to file disclosure reports?",
        expect_any_of=("search_rulebooks",),
        arg_check=_expect_jurisdiction("search_rulebooks", "list_rulebook_sources", jurisdiction="ca"),
    ),
    EvalCase(
        id="rb-10-ca-slate-mailer",
        question="What disclosure rules apply to a slate mailer organization in California?",
        expect_any_of=("search_rulebooks",),
        arg_check=_expect_jurisdiction("search_rulebooks", "list_rulebook_sources", jurisdiction="ca"),
    ),
    EvalCase(
        id="rb-11-ga-registration",
        question="Under Georgia state law, when does a political committee have to register with the state ethics commission?",
        expect_any_of=("search_rulebooks",),
        arg_check=_expect_jurisdiction("search_rulebooks", "list_rulebook_sources", jurisdiction="ga"),
    ),
    EvalCase(
        id="rb-12-ny-filing-deadlines",
        question="What are New York State's campaign finance disclosure filing deadlines?",
        expect_any_of=("search_rulebooks",),
        arg_check=_expect_jurisdiction("search_rulebooks", "list_rulebook_sources", jurisdiction="ny"),
    ),
    # -- Jurisdiction-discipline: a state with NO rulebooks loaded --
    EvalCase(
        id="rb-13-unloaded-state-texas",
        question="What are Texas's contribution limits for state legislative candidates?",
        expect_any_of=("list_rulebook_jurisdictions",),
        arg_check=_forbid_jurisdiction("tx"),
        notes="Texas isn't loaded; model must check coverage rather than assume or fabricate an answer.",
    ),
    EvalCase(
        id="rb-14-unloaded-state-check",
        question="Do you have Nevada's campaign finance rules loaded?",
        expect_any_of=("list_rulebook_jurisdictions",),
        check_citations=False,
        notes="Pure coverage question -- no citation expected either way.",
    ),
    # -- list_rulebook_sources --
    EvalCase(
        id="rb-15-list-federal-guides",
        question="What FEC campaign guide PDFs do you have on file?",
        expect_any_of=("list_rulebook_sources", "list_rulebook_jurisdictions"),
        check_citations=False,
    ),
    EvalCase(
        id="rb-16-list-ca-documents",
        question="What documents are loaded for California specifically?",
        expect_any_of=("list_rulebook_sources", "list_rulebook_jurisdictions"),
        check_citations=False,
    ),
    # -- get_rulebook_page: explicit source+page already known --
    EvalCase(
        id="rb-17-direct-page-read",
        question=(
            "Show me the full text of page 1 of contribution-limits-chart-2025-2026.pdf, "
            "exactly as written."
        ),
        expect_any_of=("get_rulebook_page", "search_rulebooks"),
    ),
    # -- Negative controls: must reach for the right primary tool --
    EvalCase(
        id="rb-18-ao-not-rulebook",
        question="Has the FEC ever ruled on whether a campaign can accept contributions in cryptocurrency?",
        expect_any_of=("search_advisory_opinions",),
        notes=(
            "The controlling answer is an advisory opinion (AO 2014-02), so "
            "search_advisory_opinions must be called -- but a live run showed the "
            "model *also* calling search_rulebooks/get_rulebook_page to pull the "
            "campaign guides' reporting/valuation instructions for bitcoin "
            "contributions (candgui.pdf p.121/138, partygui.pdf p.33/128-129), all "
            "independently verified as real pages that do discuss bitcoin/crypto. "
            "That's a more complete answer, not a wrong one, so this case no longer "
            "forbids rulebook tools -- only asserts the AO tool was used."
        ),
    ),
    EvalCase(
        id="rb-19-committee-lookup-not-rulebook",
        question="Who is the treasurer of FEC committee C00401224?",
        expect_any_of=("get_committee",),
        forbid=("search_rulebooks", "get_rulebook_page"),
        check_citations=False,
        notes="Live-data lookup, not a compliance-rule question -- no PDF citation involved.",
    ),
    EvalCase(
        id="rb-20-no-tool-needed",
        question="What's 15% of $3,300?",
        forbid=ALL_TOOLS,
        check_citations=False,
        notes="Pure arithmetic -- answering it doesn't require any tool at all.",
    ),
]
