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
import json
from typing import Any

import streamlit as st
from anthropic import Anthropic, beta_tool

from fec_mcp import server

MODEL = "claude-opus-5"
MAX_TOKENS = 4096


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
    calendar_year: int | None = None,
    per_page: int = 50,
    page: int = 1,
) -> str:
    """Get FEC reporting/filing/election deadline dates (federal only).

    Args:
        category: Optional category, e.g. "reporting-dates", "quarterly",
            "monthly", "election-dates".
        calendar_year: Optional calendar year filter, e.g. 2026.
        per_page: Results per page (max 100).
        page: Page number.
    """
    return _json(
        _run_async(
            server.get_reporting_calendar,
            category=category,
            calendar_year=calendar_year,
            per_page=per_page,
            page=page,
        )
    )


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
        system=server.INSTRUCTIONS,
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
    st.set_page_config(page_title="fec-mcp demo", page_icon="\U0001f5f3️")
    st.title("FEC compliance assistant (demo)")
    st.caption(
        "Same tools as the fec-mcp MCP server -- rulebook PDF search + live OpenFEC data -- "
        "wired into a plain chat page for demo purposes. Not for production use."
    )

    with st.sidebar:
        st.subheader("Setup")
        api_key = st.text_input(
            "ANTHROPIC_API_KEY",
            type="password",
            help="Falls back to the ANTHROPIC_API_KEY environment variable if left blank.",
        )
        jurisdictions = server.list_rulebook_jurisdictions()
        st.write("**Rulebook jurisdictions loaded:**")
        if jurisdictions.get("jurisdictions"):
            for j in jurisdictions["jurisdictions"]:
                st.write(f"- {j['jurisdiction']} ({j['source_count']} source(s))")
        else:
            st.write(jurisdictions.get("message", "None loaded."))
        if st.button("Clear conversation"):
            st.session_state.messages = []
            st.rerun()

    if "messages" not in st.session_state:
        st.session_state.messages = []

    for turn in st.session_state.messages:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])
            for call in turn.get("trace", []):
                st.caption(f"\U0001f527 {call['name']}({', '.join(f'{k}={v!r}' for k, v in call['input'].items())})")

    prompt = st.chat_input("Ask a federal (or loaded-state) campaign finance question...")
    if not prompt:
        return

    with st.chat_message("user"):
        st.markdown(prompt)
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
            st.caption(f"\U0001f527 {call['name']}({', '.join(f'{k}={v!r}' for k, v in call['input'].items())})")
        st.markdown(result["text"])
        if result["stop_reason"] == "pause_turn":
            st.warning("Response paused mid-turn (hit the server-tool iteration limit) -- answer may be incomplete.")

    st.session_state.messages.append(
        {"role": "assistant", "content": result["text"], "trace": result["trace"]}
    )


if __name__ == "__main__":
    main()
