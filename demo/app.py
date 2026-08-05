"""Streamlit chat demo for fec-mcp.

Not a production app -- a quick, shareable demo of the same tools the MCP
server exposes (rulebook search + live OpenFEC data), so it can be shown to
coworkers without anyone needing to configure an MCP client. It reuses the
actual tool implementations in fec_mcp.server (no logic is duplicated here)
and wires them into Claude via the Anthropic API's tool runner.

Run with:
    streamlit run demo/app.py

Requires ANTHROPIC_API_KEY (a real API key, separate from the MCP server's
FEC_API_KEY) to be set in the environment, or entered in the sidebar.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import quote

import streamlit as st
from anthropic import Anthropic, beta_tool

from fec_mcp import server
from fec_mcp.rulebook_index import DEFAULT_RULEBOOKS_DIR

MODEL = "claude-opus-5"
MAX_TOKENS = 4096

# Demo-only addition to server.INSTRUCTIONS (never edit server.INSTRUCTIONS
# itself for this): asks the model to end a cited answer with a fixed,
# machine-parseable citation block so the UI can render real clickable
# source chips instead of parsing free-form prose (fragile -- see
# _split_citations). This is deliberately NOT part of the shared
# server.INSTRUCTIONS constant, since that's also used by every other MCP
# client (VS Code, Claude Desktop): those clients have no UI to turn a
# "SOURCE | file | page | jurisdiction" line into a chip, so forcing this
# format on them would just dump robotic-looking lines into a normal chat.
CITATION_FORMAT_ADDENDUM = """

Demo UI addendum: when your answer cites a page from a rulebook PDF (from
search_rulebooks/get_rulebook_page) or an FEC Advisory Opinion (from
search_advisory_opinions/get_advisory_opinion), end the entire answer with
a line reading exactly "Sources:" followed by one citation per line, in
exactly this format and no other text on those lines:

SOURCE | <filename.pdf exactly as the tool returned it, e.g. candgui.pdf or states/ca/limits.pdf> | <page number> | <jurisdiction>
AO | <ao_no, e.g. 2014-02> | <status, e.g. Final> | <a full https://www.fec.gov URL if you have one from the tool results, otherwise leave this field blank>

Use the exact filename, page, jurisdiction, AO number, and status the
tools returned -- never invent, reformat, or abbreviate them. Only add
this block when you actually cited a specific rulebook page or advisory
opinion; omit it entirely for answers with no such citation (e.g. live
OpenFEC candidate/committee/disbursement data, or an answer saying nothing
relevant is loaded).

Do not paste a raw URL (e.g. a fec.gov PDF link) into the body of the
answer itself -- the Sources: block already renders as a clickable link
for each citation, so a URL in the prose is both redundant and, unlike
the Sources: block, not a real link there. You can still name what you're
citing in a sentence (e.g. "the controlling opinion is AO 2014-02"), just
don't repeat its URL.
"""

CITATION_CSS = """
<style>
.fec-cite-row { margin-top: 6px; }
.fec-cite {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 0.78rem; padding: 3px 9px; margin: 2px 6px 2px 0;
    border-radius: 6px; border: 1px solid rgba(49, 51, 63, 0.2);
    text-decoration: none; color: inherit;
}
a.fec-cite:hover { border-color: #ff4b4b; }
.fec-cite-badge {
    font-weight: 700; font-size: 0.66rem; letter-spacing: .03em;
    padding: 1px 5px; border-radius: 4px;
    background: rgba(49, 51, 63, 0.08);
}
.fec-cite-static { opacity: 0.85; }
</style>
"""

# Shown as clickable "Try asking" chips in the sidebar. Picked to cover
# each tool family (rulebook search, live OpenFEC data, advisory opinions,
# reporting calendar) plus one state-jurisdiction question, since that's a
# real edge case this tool handles correctly (only answers for jurisdictions
# that actually have PDFs loaded) that a naive tool wouldn't.
EXAMPLE_QUESTIONS = [
    "What's the individual contribution limit to a candidate committee this cycle?",
    "Search advisory opinions about cryptocurrency donations",
    "What are the next FEC quarterly reporting deadlines?",
    "What's the contribution limit for a California state assembly race?",
]

# Streamlit's built-in static file server (enabled in .streamlit/config.toml)
# resolves its "static" folder relative to *this script's own directory*
# (demo/static/), not the repo root or the directory `streamlit run` was
# invoked from -- confirmed by running it: pointing this at the repo root
# instead produced "no static folder found at .../demo/static". Served at
# /app/static/<path>. _sync_static_pdfs mirrors data/rulebooks/ here so
# rulebook PDFs get a real, clickable URL -- needed to link a citation
# straight to its page (browsers' built-in PDF viewers honor a #page=N URL
# fragment).
STATIC_RULEBOOKS_DIR = Path(__file__).resolve().parent / "static" / "rulebooks"


def _sync_static_pdfs() -> None:
    """Mirror data/rulebooks/**/*.pdf into static/rulebooks/.

    Only copies new/changed files (by size+mtime) and deletes stale copies
    of PDFs no longer in data/rulebooks/, so this is cheap to call on every
    Streamlit rerun and self-heals whenever PDFs are added, replaced, or
    removed -- no manual sync step needed.
    """
    if not DEFAULT_RULEBOOKS_DIR.exists():
        return

    sources = {
        p.relative_to(DEFAULT_RULEBOOKS_DIR): p for p in DEFAULT_RULEBOOKS_DIR.rglob("*.pdf")
    }

    if STATIC_RULEBOOKS_DIR.exists():
        for existing in STATIC_RULEBOOKS_DIR.rglob("*.pdf"):
            if existing.relative_to(STATIC_RULEBOOKS_DIR) not in sources:
                existing.unlink()

    for rel, src_path in sources.items():
        dest_path = STATIC_RULEBOOKS_DIR / rel
        src_stat = src_path.stat()
        if dest_path.exists():
            dest_stat = dest_path.stat()
            if dest_stat.st_size == src_stat.st_size and dest_stat.st_mtime == src_stat.st_mtime:
                continue
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest_path)


def _pdf_url(source: str, page: int | None = None) -> str:
    """URL for a rulebook PDF served via Streamlit's static file server.

    Appending "#page=N" is a standard convention browsers' built-in PDF
    viewers (Chrome, Firefox, Edge, Safari) honor to open straight to that
    page -- no special viewer needed. `page` is the PDF's own internal page
    order (the same number search_rulebooks/get_rulebook_page report), so
    the link always lands on the exact page being cited; it may not match
    a page number physically printed on the page itself if the document has
    an unnumbered cover or table of contents.
    """
    url = f"/app/static/rulebooks/{quote(source)}"
    if page:
        url += f"#page={page}"
    return url


def _run_async(coro_fn, /, **kwargs) -> Any:
    """Run one of fec_mcp.server's async (OpenFEC-backed) tool functions.

    Each call gets a fresh event loop (asyncio.run), so the cached
    OpenFECClient/httpx.AsyncClient from a previous call -- bound to a now-closed
    loop -- must be dropped first, or httpx raises a cross-event-loop error.
    """
    server._openfec_client = None
    return asyncio.run(coro_fn(**kwargs))


def _json(result: dict[str, Any]) -> str:
    return json.dumps(result, default=str)


def _md(text: str) -> str:
    """Escape literal '$' before handing text to st.markdown.

    st.markdown renders anything between a pair of '$' as LaTeX math, and
    campaign-finance answers are full of dollar amounts (e.g. "$3,500 ...
    $5,000") -- unescaped, everything between the first and second '$' in a
    message silently renders as a math expression in serif italic type
    instead of plain text.
    """
    return text.replace("$", "\\$")


def _split_citations(text: str) -> tuple[str, list[dict[str, str]]]:
    """Split a model answer into (prose, citations) via the trailing
    "Sources:" block CITATION_FORMAT_ADDENDUM asks the model to emit.

    Degrades gracefully rather than risking a garbled partial parse: if
    there's no "Sources:" marker, or nothing under it parses into a
    well-formed citation line, returns the full original text unchanged
    with an empty citation list.
    """
    marker_idx = text.rfind("Sources:")
    if marker_idx == -1:
        return text, []

    prose = text[:marker_idx].rstrip()
    block = text[marker_idx + len("Sources:") :]

    citations: list[dict[str, str]] = []
    for line in block.splitlines():
        line = line.strip().lstrip("-*").strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if parts[0] == "SOURCE" and len(parts) == 4:
            citations.append(
                {"kind": "source", "filename": parts[1], "page": parts[2], "jurisdiction": parts[3]}
            )
        elif parts[0] == "AO" and len(parts) in (3, 4):
            citations.append(
                {
                    "kind": "ao",
                    "ao_no": parts[1],
                    "status": parts[2],
                    "url": parts[3] if len(parts) == 4 else "",
                }
            )
        # Any other line under "Sources:" (stray commentary, a malformed
        # row) is silently skipped rather than crashing the render.

    if not citations:
        return text, []
    return prose, citations


def _citation_chip_html(c: dict[str, str]) -> str:
    if c["kind"] == "source":
        try:
            page_int: int | None = int(c["page"])
        except ValueError:
            page_int = None
        href = _pdf_url(c["filename"], page_int)
        label = html.escape(f"{c['filename']}, p. {c['page']}")
        badge = html.escape(c["jurisdiction"].upper())
        return (
            f'<a class="fec-cite" href="{html.escape(href)}" target="_blank" rel="noopener">'
            f'<span class="fec-cite-badge">{badge}</span>{label}</a>'
        )

    # kind == "ao"
    label = html.escape(f"AO {c['ao_no']}")
    badge = html.escape(c["status"].upper())
    if c["url"]:
        return (
            f'<a class="fec-cite" href="{html.escape(c["url"])}" target="_blank" rel="noopener">'
            f'<span class="fec-cite-badge">{badge}</span>{label}</a>'
        )
    return f'<span class="fec-cite fec-cite-static"><span class="fec-cite-badge">{badge}</span>{label}</span>'


def _render_citations(citations: list[dict[str, str]]) -> None:  # pragma: no cover -- Streamlit call
    if not citations:
        return
    chips = "".join(_citation_chip_html(c) for c in citations)
    st.markdown(f'<div class="fec-cite-row">{chips}</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Tool wrappers -- thin @beta_tool shims around fec_mcp.server's real tool
# functions, so this demo and the MCP server share one implementation.
# ---------------------------------------------------------------------------


@beta_tool
def list_rulebook_jurisdictions() -> str:
    """List every jurisdiction with rulebook PDFs loaded, e.g. "federal" and
    any state codes like "ca", "ny". Always call this before answering a
    state-specific compliance question, to check whether that state's
    rulebooks are actually loaded rather than assuming coverage.
    """
    return _json(server.list_rulebook_jurisdictions())


@beta_tool
def list_rulebook_sources(jurisdiction: str | None = None) -> str:
    """List the rulebook PDFs currently loaded and searchable.

    Args:
        jurisdiction: Optional filter, "federal" or a lowercase two-letter
            state code (e.g. "ca"). Omit to list everything.
    """
    return _json(server.list_rulebook_sources(jurisdiction=jurisdiction))


@beta_tool
def search_rulebooks(
    query: str,
    top_k: int = 8,
    source: str | None = None,
    jurisdiction: str | None = None,
) -> str:
    """Full-text search the loaded rulebook PDFs (federal and/or state).

    Use this for any compliance question: contribution limits, who may
    contribute, disclaimer requirements, coordination rules, joint
    fundraising, recordkeeping, registration thresholds, reporting
    requirements, personal use of funds, foreign national/corporate
    contribution bans, etc. If the question is about a specific state, pass
    that state's lowercase two-letter code as jurisdiction (call
    list_rulebook_jurisdictions first if unsure whether it's loaded).

    Args:
        query: Search terms, e.g. "individual contribution limit candidate".
        top_k: Max number of matching pages to return (default 8).
        source: Optional exact source path (from list_rulebook_sources) to
            restrict the search to a single PDF.
        jurisdiction: Optional filter, "federal" or a lowercase two-letter
            state code. Omit to search all loaded jurisdictions.
    """
    return _json(
        server.search_rulebooks(query, top_k=top_k, source=source, jurisdiction=jurisdiction)
    )


@beta_tool
def get_rulebook_page(source: str, page: int) -> str:
    """Get the full extracted text of one page from a loaded rulebook PDF.

    Args:
        source: Exact source path as returned by list_rulebook_sources /
            search_rulebooks.
        page: 1-indexed page number.
    """
    return _json(server.get_rulebook_page(source, page))


@beta_tool
def search_candidates(
    name: str | None = None,
    state: str | None = None,
    office: str | None = None,
    party: str | None = None,
    cycle: int | None = None,
    candidate_status: str | None = None,
    per_page: int = 20,
    page: int = 1,
) -> str:
    """Search real candidates via the live OpenFEC API (federal only).

    Args:
        name: Candidate name search text (fuzzy).
        state: Two-letter state code, e.g. "CA".
        office: "H" (House), "S" (Senate), or "P" (President).
        party: Party code, e.g. "DEM", "REP", "IND".
        cycle: Two-year election cycle, e.g. 2026.
        candidate_status: "C" (candidate), "F" (future), "N" (not yet
            candidate), "P" (prior candidate).
        per_page: Results per page (max 100).
        page: Page number.
    """
    return _json(
        _run_async(
            server.search_candidates,
            name=name,
            state=state,
            office=office,
            party=party,
            cycle=cycle,
            candidate_status=candidate_status,
            per_page=per_page,
            page=page,
        )
    )


@beta_tool
def get_candidate(candidate_id: str) -> str:
    """Get full details for one candidate by their FEC candidate ID (e.g. "P80001571")."""
    return _json(_run_async(server.get_candidate, candidate_id=candidate_id))


@beta_tool
def get_candidate_totals(candidate_id: str, cycle: int | None = None) -> str:
    """Get aggregated financial totals for a candidate's linked committees.

    Args:
        candidate_id: FEC candidate ID, e.g. "P80001571".
        cycle: Optional two-year cycle to filter to, e.g. 2026.
    """
    return _json(_run_async(server.get_candidate_totals, candidate_id=candidate_id, cycle=cycle))


@beta_tool
def search_committees(
    name: str | None = None,
    state: str | None = None,
    committee_type: str | None = None,
    designation: str | None = None,
    cycle: int | None = None,
    per_page: int = 20,
    page: int = 1,
) -> str:
    """Search real PACs, party committees, and campaign committees (federal only).

    Args:
        name: Committee name search text (fuzzy).
        state: Two-letter state code.
        committee_type: OpenFEC committee type code, e.g. "P" (presidential),
            "H"/"S" (House/Senate campaign), "N"/"Q" (PAC), "O" (super PAC),
            "X"/"Y" (party).
        designation: "A" (authorized), "J" (joint fundraising), "P"
            (principal campaign committee), "U" (unauthorized), "B"
            (lobbyist/registrant PAC), "D" (leadership PAC).
        cycle: Two-year cycle, e.g. 2026.
        per_page: Results per page (max 100).
        page: Page number.
    """
    return _json(
        _run_async(
            server.search_committees,
            name=name,
            state=state,
            committee_type=committee_type,
            designation=designation,
            cycle=cycle,
            per_page=per_page,
            page=page,
        )
    )


@beta_tool
def get_committee(committee_id: str) -> str:
    """Get full details for one committee by its FEC committee ID (e.g. "C00401224")."""
    return _json(_run_async(server.get_committee, committee_id=committee_id))


@beta_tool
def get_committee_filings(
    committee_id: str,
    form_type: str | None = None,
    cycle: int | None = None,
    per_page: int = 20,
    page: int = 1,
) -> str:
    """List a committee's FEC filings (e.g. Form 3, 3X, 3P finance reports).

    Args:
        committee_id: FEC committee ID, e.g. "C00401224".
        form_type: Optional FEC form type filter, e.g. "F3X".
        cycle: Optional two-year cycle filter, e.g. 2026.
        per_page: Results per page (max 100).
        page: Page number.
    """
    return _json(
        _run_async(
            server.get_committee_filings,
            committee_id=committee_id,
            form_type=form_type,
            cycle=cycle,
            per_page=per_page,
            page=page,
        )
    )


@beta_tool
def get_committee_totals(committee_id: str, cycle: int | None = None, per_page: int = 10) -> str:
    """Get a committee's financial totals (receipts, disbursements, cash on hand) by cycle.

    Args:
        committee_id: FEC committee ID, e.g. "C00401224".
        cycle: Optional two-year cycle filter, e.g. 2026.
        per_page: Number of cycle records to return.
    """
    return _json(
        _run_async(
            server.get_committee_totals, committee_id=committee_id, cycle=cycle, per_page=per_page
        )
    )


@beta_tool
def search_disbursements(
    committee_id: str,
    recipient_name: str | None = None,
    disbursement_purpose_category: str | None = None,
    disbursement_description: str | None = None,
    min_date: str | None = None,
    max_date: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    cycle: int | None = None,
    per_page: int = 50,
    last_index: str | None = None,
    last_disbursement_date: str | None = None,
) -> str:
    """Search a committee's itemized Schedule B disbursements (who a committee paid, how much).

    IMPORTANT: always pass min_date (and usually max_date) unless full
    history is explicitly wanted -- high-volume committees can have hundreds
    of thousands of disbursements, and an unfiltered query is slow enough to
    time out. For "recent" disbursements with no date given, default to
    something like the last 90 days.

    Args:
        committee_id: FEC committee ID whose disbursements to search, e.g. "C00401224".
        recipient_name: Optional recipient name search text (fuzzy).
        disbursement_purpose_category: Optional filter, one of: ADMINISTRATIVE,
            ADVERTISING, CONTRIBUTIONS, EVENTS, FUNDRAISING, LOAN-REPAYMENTS,
            MATERIALS, OTHER, POLLING, REFUNDS, TRANSFERS, TRAVEL.
        disbursement_description: Optional free-text filter on the reported purpose.
        min_date: Optional lower bound, "YYYY-MM-DD".
        max_date: Optional upper bound, "YYYY-MM-DD".
        min_amount: Optional minimum disbursement amount.
        max_amount: Optional maximum disbursement amount.
        cycle: Optional two-year cycle filter, e.g. 2026.
        per_page: Results per page (max 100).
        last_index: Cursor from a previous response's pagination.last_indexes.last_index.
        last_disbursement_date: Cursor from pagination.last_indexes.last_disbursement_date.
    """
    return _json(
        _run_async(
            server.search_disbursements,
            committee_id=committee_id,
            recipient_name=recipient_name,
            disbursement_purpose_category=disbursement_purpose_category,
            disbursement_description=disbursement_description,
            min_date=min_date,
            max_date=max_date,
            min_amount=min_amount,
            max_amount=max_amount,
            cycle=cycle,
            per_page=per_page,
            last_index=last_index,
            last_disbursement_date=last_disbursement_date,
        )
    )


@beta_tool
def search_filings(
    committee_id: str | None = None,
    candidate_id: str | None = None,
    form_type: str | None = None,
    cycle: int | None = None,
    per_page: int = 20,
    page: int = 1,
) -> str:
    """Search FEC filings across committees/candidates (federal only).

    Args:
        committee_id: Optional FEC committee ID filter.
        candidate_id: Optional FEC candidate ID filter.
        form_type: Optional FEC form type, e.g. "F3X", "F3P", "F3".
        cycle: Optional two-year cycle, e.g. 2026.
        per_page: Results per page (max 100).
        page: Page number.
    """
    return _json(
        _run_async(
            server.search_filings,
            committee_id=committee_id,
            candidate_id=candidate_id,
            form_type=form_type,
            cycle=cycle,
            per_page=per_page,
            page=page,
        )
    )


@beta_tool
def search_elections(
    state: str | None = None,
    office: str | None = None,
    cycle: int | None = None,
    district: str | None = None,
    per_page: int = 20,
    page: int = 1,
) -> str:
    """Search federal elections by state/office/cycle.

    Args:
        state: Two-letter state code.
        office: "house", "senate", or "president".
        cycle: Two-year cycle, e.g. 2026.
        district: District number (for House races), e.g. "01".
        per_page: Results per page (max 100).
        page: Page number.
    """
    return _json(
        _run_async(
            server.search_elections,
            state=state,
            office=office,
            cycle=cycle,
            district=district,
            per_page=per_page,
            page=page,
        )
    )


@beta_tool
def get_reporting_calendar(
    category: str | None = None,
    min_start_date: str | None = None,
    max_start_date: str | None = None,
    per_page: int = 50,
    page: int = 1,
) -> str:
    """Get FEC reporting/filing/election deadline dates (federal only).

    Args:
        category: Optional category, e.g. "reporting-dates", "quarterly",
            "monthly", "election-dates".
        min_start_date: Optional lower bound, "YYYY-MM-DD". There is no
            year-only filter -- use this plus max_start_date instead.
        max_start_date: Optional upper bound, "YYYY-MM-DD".
        per_page: Results per page (max 100).
        page: Page number.
    """
    return _json(
        _run_async(
            server.get_reporting_calendar,
            category=category,
            min_start_date=min_start_date,
            max_start_date=max_start_date,
            per_page=per_page,
            page=page,
        )
    )


@beta_tool
def search_advisory_opinions(
    q: str | None = None,
    ao_no: str | None = None,
    ao_year: str | None = None,
    ao_name: str | None = None,
    ao_status: str | None = None,
    ao_requestor: str | None = None,
    ao_commenter: str | None = None,
    ao_representative: str | None = None,
    hits_returned: int = 20,
) -> str:
    """Search FEC Advisory Opinions -- rulings on specific factual scenarios
    (e.g. "can a campaign accept cryptocurrency donations"), federal only.
    Use this for a specific edge-case scenario; use search_rulebooks instead
    for general compliance rules. Any document link/URL field returned is a
    path relative to https://www.fec.gov, not a complete URL -- always
    prepend that origin when presenting a link.

    Args:
        q: Free-text search, e.g. "cryptocurrency donations".
        ao_no: Exact AO number, e.g. "2014-12".
        ao_year: Filter by year requested, e.g. "2014".
        ao_name: Filter by AO name/subject text.
        ao_status: Filter by status, e.g. "Final".
        ao_requestor: Filter by requestor name.
        ao_commenter: Filter by commenter name.
        ao_representative: Filter by requestor's legal representative name.
        hits_returned: Max results (max 200).
    """
    return _json(
        _run_async(
            server.search_advisory_opinions,
            q=q,
            ao_no=ao_no,
            ao_year=ao_year,
            ao_name=ao_name,
            ao_status=ao_status,
            ao_requestor=ao_requestor,
            ao_commenter=ao_commenter,
            ao_representative=ao_representative,
            hits_returned=hits_returned,
        )
    )


@beta_tool
def get_advisory_opinion(ao_no: str) -> str:
    """Get one FEC Advisory Opinion's full document record by its AO number
    (federal only). Returns every document filed under this AO number --
    request, drafts, final opinion, vote record, comments -- not just the
    final opinion, so check each document's type/category before treating
    its text as the Commission's actual holding.

    Args:
        ao_no: AO number as returned by search_advisory_opinions, e.g. "2014-12".
    """
    return _json(_run_async(server.get_advisory_opinion, ao_no=ao_no))


TOOLS = [
    list_rulebook_jurisdictions,
    list_rulebook_sources,
    search_rulebooks,
    get_rulebook_page,
    search_candidates,
    get_candidate,
    get_candidate_totals,
    search_committees,
    get_committee,
    get_committee_filings,
    get_committee_totals,
    search_disbursements,
    search_filings,
    search_elections,
    get_reporting_calendar,
    search_advisory_opinions,
    get_advisory_opinion,
]


# ---------------------------------------------------------------------------
# Chat turn logic (no Streamlit calls here -- kept testable without a live
# ScriptRunContext; see main() for the actual page).
# ---------------------------------------------------------------------------


def run_turn(client: Anthropic, history: list[dict[str, Any]], user_text: str) -> dict[str, Any]:
    """Run one chat turn: send history + a new user message through the tool
    runner, return the assistant's final text plus a trace of tool calls made.

    Conversation history is kept as plain text turns (not the raw tool_use/
    tool_result blocks the runner produces) -- simpler to persist across
    Streamlit reruns, and Claude doesn't need the tool-call plumbing replayed
    to hold a coherent conversation, only what was asked and answered.
    """
    messages = history + [{"role": "user", "content": user_text}]

    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=server.INSTRUCTIONS + CITATION_FORMAT_ADDENDUM,
        tools=TOOLS,
        messages=messages,
    )

    trace: list[dict[str, Any]] = []
    last_message = None
    for message in runner:
        last_message = message
        for block in message.content:
            if block.type == "tool_use":
                trace.append({"name": block.name, "input": block.input})

    if last_message is None:
        return {"text": "(no response)", "trace": trace, "stop_reason": None}

    text = "".join(block.text for block in last_message.content if block.type == "text")
    return {"text": text, "trace": trace, "stop_reason": last_message.stop_reason}


def main() -> None:  # pragma: no cover -- Streamlit UI, not unit tested
    _sync_static_pdfs()
    st.set_page_config(page_title="fec-mcp demo", page_icon="\U0001f5f3️")
    st.markdown(CITATION_CSS, unsafe_allow_html=True)
    st.title("FEC compliance assistant (demo)")
    st.caption(
        "Same tools as the fec-mcp MCP server -- rulebook PDF search + live OpenFEC data -- "
        "wired into a plain chat page for demo purposes. Not for production use."
    )

    with st.sidebar:
        st.subheader("Setup")
        api_key = st.text_input(
            "Your Anthropic API key",
            type="password",
            help=(
                "Get one at console.anthropic.com. Used only for your own "
                "session -- not stored or shared with other visitors. Falls "
                "back to the server's ANTHROPIC_API_KEY environment "
                "variable if left blank (unset on this deployment)."
            ),
        )
        has_key = bool(api_key) or bool(os.environ.get("ANTHROPIC_API_KEY"))
        if not has_key:
            st.info("Paste your Anthropic API key above to start chatting.")
        sources_result = server.list_rulebook_sources()
        st.write("**Rulebook jurisdictions loaded:**")
        if sources_result.get("sources"):
            by_jurisdiction: dict[str, list[dict]] = {}
            for s in sources_result["sources"]:
                by_jurisdiction.setdefault(s["jurisdiction"], []).append(s)
            for jurisdiction in sorted(by_jurisdiction):
                srcs = by_jurisdiction[jurisdiction]
                st.markdown(f"**{jurisdiction}** ({len(srcs)} source(s))")
                for s in srcs:
                    href = _pdf_url(s["source"])
                    title = html.escape(s["title"])
                    st.markdown(
                        f'<a href="{href}" target="_blank" rel="noopener">{title}</a>',
                        unsafe_allow_html=True,
                    )
        else:
            st.write(sources_result.get("message", "None loaded."))

        st.write("**Try asking:**")
        for i, question in enumerate(EXAMPLE_QUESTIONS):
            if st.button(question, key=f"example_question_{i}", use_container_width=True):
                st.session_state["chat_input"] = question
                st.rerun()

        if st.button("Clear conversation"):
            st.session_state.messages = []
            st.rerun()

    if "messages" not in st.session_state:
        st.session_state.messages = []

    for turn in st.session_state.messages:
        with st.chat_message(turn["role"]):
            prose, citations = _split_citations(turn["content"])
            st.markdown(_md(prose))
            _render_citations(citations)
            for call in turn.get("trace", []):
                st.caption(_md(f"\U0001f527 {call['name']}({', '.join(f'{k}={v!r}' for k, v in call['input'].items())})"))

    prompt = st.chat_input(
        "Ask a federal (or loaded-state) campaign finance question...", key="chat_input"
    )
    if not prompt:
        return

    if not has_key:
        with st.chat_message("user"):
            st.markdown(_md(prompt))
        with st.chat_message("assistant"):
            st.warning("Add your Anthropic API key in the sidebar first, then ask again.")
        return

    with st.chat_message("user"):
        st.markdown(_md(prompt))
    st.session_state.messages.append({"role": "user", "content": prompt})

    client = Anthropic(api_key=api_key or None)
    history = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages[:-1]]

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                result = run_turn(client, history, prompt)
            except Exception as exc:  # noqa: BLE001 -- surface any API/tool error to the demo UI
                st.error(f"Error: {exc}")
                return
        for call in result["trace"]:
            st.caption(_md(f"\U0001f527 {call['name']}({', '.join(f'{k}={v!r}' for k, v in call['input'].items())})"))
        prose, citations = _split_citations(result["text"])
        st.markdown(_md(prose))
        _render_citations(citations)
        if result["stop_reason"] == "pause_turn":
            st.warning("Response paused mid-turn (hit the server-tool iteration limit) -- answer may be incomplete.")

    st.session_state.messages.append(
        {"role": "assistant", "content": result["text"], "trace": result["trace"]}
    )


if __name__ == "__main__":
    main()
