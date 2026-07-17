"""FEC MCP server.

Exposes two complementary tool families:

1. Rulebook tools -- full-text search over official FEC PDF guides that the
   user places in ``data/rulebooks/`` (campaign guides for candidates,
   party committees, PACs, and the contribution-limits chart). This is the
   authoritative source for compliance rules and dollar limits: answers are
   grounded in quoted PDF pages with citations, not model recall.

2. OpenFEC tools -- live lookups against the public OpenFEC API
   (api.open.fec.gov) for real candidates, committees, filings, financial
   totals, elections, and the reporting calendar.

Neither family gives legal advice; tool outputs should be treated as
research aids and cited back to their source (PDF page or OpenFEC record).
"""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.fastmcp import FastMCP

from .openfec_client import OpenFECClient, OpenFECError
from .rulebook_index import RulebookIndex

INSTRUCTIONS = """\
This server provides two kinds of tools:

- search_rulebooks / list_rulebook_sources / get_rulebook_page: search the
  official FEC campaign guide and contribution-limits PDFs the user has
  placed in data/rulebooks/. Use these for ANY question about contribution
  limits, disclaimer requirements, coordination rules, recordkeeping,
  registration thresholds, or other compliance rules. Always cite the
  source filename and page number from the results.

- search_candidates / get_candidate / get_candidate_totals /
  search_committees / get_committee / get_committee_filings /
  get_committee_totals / search_filings / search_elections /
  get_reporting_calendar: live data from the OpenFEC API about real
  candidates, committees, filings, and elections.

If data/rulebooks/ has no PDFs loaded yet, rulebook tools will say so --
tell the user to add FEC campaign guide PDFs there rather than answering
compliance questions from general knowledge.
"""

mcp = FastMCP("fec-mcp", instructions=INSTRUCTIONS)

_rulebook_index = RulebookIndex()
_openfec_client: OpenFECClient | None = None
_client_lock = asyncio.Lock()


async def _client() -> OpenFECClient:
    global _openfec_client
    async with _client_lock:
        if _openfec_client is None:
            _openfec_client = OpenFECClient()
    return _openfec_client


def _trim(item: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {k: item.get(k) for k in keys if k in item}


# ---------------------------------------------------------------------------
# Rulebook tools (PDF search)
# ---------------------------------------------------------------------------


@mcp.tool()
def list_rulebook_sources() -> dict[str, Any]:
    """List the FEC rulebook PDFs currently loaded and searchable.

    Returns each source's filename, title, and page count. If empty, no
    PDFs have been added to data/rulebooks/ yet -- the user should add the
    FEC's campaign guides (candidates, party committees, PACs) and the
    contribution limits chart PDF there.
    """
    sources = _rulebook_index.list_sources()
    if not sources:
        return {
            "sources": [],
            "message": (
                "No rulebook PDFs are loaded. Add FEC campaign guide PDFs "
                "(e.g. Campaign Guide for Congressional Candidates and "
                "Committees, Campaign Guide for Political Party Committees, "
                "Campaign Guide for Nonconnected Committees, and the "
                "Contribution Limits chart) to data/rulebooks/ in this repo."
            ),
        }
    return {
        "sources": [
            {"filename": s.filename, "title": s.title, "pages": s.pages} for s in sources
        ]
    }


@mcp.tool()
def search_rulebooks(query: str, top_k: int = 8, source: str | None = None) -> dict[str, Any]:
    """Full-text search the loaded FEC rulebook PDFs.

    Use this for any compliance question: contribution limits, who may
    contribute, disclaimer requirements, coordination rules, joint
    fundraising, recordkeeping, registration thresholds, reporting
    requirements, personal use of funds, foreign national/corporate
    contribution bans, etc.

    Args:
        query: Search terms, e.g. "individual contribution limit candidate"
            or "disclaimer requirements".
        top_k: Max number of matching pages to return (default 8).
        source: Optional filename (from list_rulebook_sources) to restrict
            the search to a single PDF.

    Returns matching pages with a snippet (search terms marked with >>> <<<)
    and the exact source filename + page number to cite. Always cite these
    when answering; if no results, say so rather than guessing.
    """
    hits = _rulebook_index.search(query, top_k=top_k, source=source)
    if not hits:
        sources = _rulebook_index.list_sources()
        if not sources:
            return {
                "results": [],
                "message": "No rulebook PDFs are loaded yet. See list_rulebook_sources.",
            }
        return {"results": [], "message": "No matches found for this query."}

    return {
        "results": [
            {
                "source": h.source,
                "title": h.title,
                "page": h.page,
                "snippet": h.snippet,
                "citation": f"{h.title} ({h.source}), p.{h.page}",
            }
            for h in hits
        ]
    }


@mcp.tool()
def get_rulebook_page(source: str, page: int) -> dict[str, Any]:
    """Get the full extracted text of one page from a loaded rulebook PDF.

    Use after search_rulebooks to read more context around a match, or to
    read a specific page (e.g. a contribution-limits table page) in full.

    Args:
        source: Exact filename as returned by list_rulebook_sources /
            search_rulebooks.
        page: 1-indexed page number.
    """
    text = _rulebook_index.get_page_text(source, page)
    if text is None:
        return {"error": f"No page {page} found for source '{source}'. Check list_rulebook_sources."}
    return {"source": source, "page": page, "text": text}


# ---------------------------------------------------------------------------
# OpenFEC tools (live data)
# ---------------------------------------------------------------------------

_CANDIDATE_KEYS = [
    "candidate_id",
    "name",
    "party_full",
    "office_full",
    "state",
    "district",
    "election_years",
    "candidate_status",
    "incumbent_challenge_full",
    "cycles",
    "principal_committees",
]

_COMMITTEE_KEYS = [
    "committee_id",
    "name",
    "committee_type_full",
    "designation_full",
    "organization_type_full",
    "party_full",
    "state",
    "treasurer_name",
    "first_file_date",
    "committee_id",
]


@mcp.tool()
async def search_candidates(
    name: str | None = None,
    state: str | None = None,
    office: str | None = None,
    party: str | None = None,
    cycle: int | None = None,
    candidate_status: str | None = None,
    per_page: int = 20,
    page: int = 1,
) -> dict[str, Any]:
    """Search real candidates via the live OpenFEC API.

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
    try:
        data = await (await _client()).search_candidates(
            name=name,
            state=state,
            office=office,
            party=party,
            cycle=cycle,
            candidate_status=candidate_status,
            per_page=per_page,
            page=page,
        )
    except OpenFECError as exc:
        return {"error": str(exc)}
    results = [_trim(r, _CANDIDATE_KEYS) for r in data.get("results", [])]
    return {"results": results, "pagination": data.get("pagination")}


@mcp.tool()
async def get_candidate(candidate_id: str) -> dict[str, Any]:
    """Get full details for one candidate by their FEC candidate ID (e.g. "P80001571")."""
    try:
        data = await (await _client()).get_candidate(candidate_id)
    except OpenFECError as exc:
        return {"error": str(exc)}
    return {"results": data.get("results", [])}


@mcp.tool()
async def get_candidate_totals(candidate_id: str, cycle: int | None = None) -> dict[str, Any]:
    """Get aggregated financial totals (receipts, disbursements, cash on hand)
    for a candidate's linked committees, by FEC candidate ID.

    Args:
        candidate_id: FEC candidate ID, e.g. "P80001571".
        cycle: Optional two-year cycle to filter to, e.g. 2026.
    """
    try:
        data = await (await _client()).get_candidate_totals(candidate_id, cycle=cycle)
    except OpenFECError as exc:
        return {"error": str(exc)}
    return {"results": data.get("results", [])}


@mcp.tool()
async def search_committees(
    name: str | None = None,
    state: str | None = None,
    committee_type: str | None = None,
    designation: str | None = None,
    cycle: int | None = None,
    per_page: int = 20,
    page: int = 1,
) -> dict[str, Any]:
    """Search real PACs, party committees, and campaign committees via the live OpenFEC API.

    Args:
        name: Committee name search text (fuzzy).
        state: Two-letter state code.
        committee_type: OpenFEC committee type code, e.g. "P" (presidential),
            "H"/"S" (House/Senate campaign), "N" (PAC - nonqualified),
            "Q" (PAC - qualified), "O" (super PAC / independent expenditure
            only), "X"/"Y" (party, nonqualified/qualified).
        designation: "A" (authorized by candidate), "J" (joint fundraising),
            "P" (principal campaign committee), "U" (unauthorized),
            "B" (lobbyist/registrant PAC), "D" (leadership PAC).
        cycle: Two-year cycle, e.g. 2026.
        per_page: Results per page (max 100).
        page: Page number.
    """
    try:
        data = await (await _client()).search_committees(
            name=name,
            state=state,
            committee_type=committee_type,
            designation=designation,
            cycle=cycle,
            per_page=per_page,
            page=page,
        )
    except OpenFECError as exc:
        return {"error": str(exc)}
    results = [_trim(r, _COMMITTEE_KEYS) for r in data.get("results", [])]
    return {"results": results, "pagination": data.get("pagination")}


@mcp.tool()
async def get_committee(committee_id: str) -> dict[str, Any]:
    """Get full details for one committee (PAC, party, or campaign committee) by its FEC committee ID (e.g. "C00401224")."""
    try:
        data = await (await _client()).get_committee(committee_id)
    except OpenFECError as exc:
        return {"error": str(exc)}
    return {"results": data.get("results", [])}


@mcp.tool()
async def get_committee_filings(
    committee_id: str,
    form_type: str | None = None,
    cycle: int | None = None,
    per_page: int = 20,
    page: int = 1,
) -> dict[str, Any]:
    """List a committee's FEC filings (e.g. Form 3, 3X, 3P finance reports).

    Args:
        committee_id: FEC committee ID, e.g. "C00401224".
        form_type: Optional FEC form type filter, e.g. "F3X".
        cycle: Optional two-year cycle filter, e.g. 2026.
        per_page: Results per page (max 100).
        page: Page number.
    """
    try:
        data = await (await _client()).get_committee_filings(
            committee_id, form_type=form_type, cycle=cycle, per_page=per_page, page=page
        )
    except OpenFECError as exc:
        return {"error": str(exc)}
    return {"results": data.get("results", []), "pagination": data.get("pagination")}


@mcp.tool()
async def get_committee_totals(
    committee_id: str, cycle: int | None = None, per_page: int = 10
) -> dict[str, Any]:
    """Get a committee's financial totals (receipts, disbursements, cash on hand) by cycle.

    Args:
        committee_id: FEC committee ID, e.g. "C00401224".
        cycle: Optional two-year cycle filter, e.g. 2026.
        per_page: Number of cycle records to return.
    """
    try:
        data = await (await _client()).get_committee_totals(committee_id, cycle=cycle, per_page=per_page)
    except OpenFECError as exc:
        return {"error": str(exc)}
    return {"results": data.get("results", [])}


@mcp.tool()
async def search_filings(
    committee_id: str | None = None,
    candidate_id: str | None = None,
    form_type: str | None = None,
    cycle: int | None = None,
    per_page: int = 20,
    page: int = 1,
) -> dict[str, Any]:
    """Search FEC filings across committees/candidates via the live OpenFEC API.

    Args:
        committee_id: Optional FEC committee ID filter.
        candidate_id: Optional FEC candidate ID filter.
        form_type: Optional FEC form type, e.g. "F3X", "F3P", "F3".
        cycle: Optional two-year cycle, e.g. 2026.
        per_page: Results per page (max 100).
        page: Page number.
    """
    try:
        data = await (await _client()).search_filings(
            committee_id=committee_id,
            candidate_id=candidate_id,
            form_type=form_type,
            cycle=cycle,
            per_page=per_page,
            page=page,
        )
    except OpenFECError as exc:
        return {"error": str(exc)}
    return {"results": data.get("results", []), "pagination": data.get("pagination")}


@mcp.tool()
async def search_elections(
    state: str | None = None,
    office: str | None = None,
    cycle: int | None = None,
    district: str | None = None,
    per_page: int = 20,
    page: int = 1,
) -> dict[str, Any]:
    """Search federal elections via the live OpenFEC API.

    Args:
        state: Two-letter state code.
        office: "house", "senate", or "president".
        cycle: Two-year cycle, e.g. 2026.
        district: District number (for House races), e.g. "01".
        per_page: Results per page (max 100).
        page: Page number.
    """
    try:
        data = await (await _client()).search_elections(
            state=state, office=office, cycle=cycle, district=district, per_page=per_page, page=page
        )
    except OpenFECError as exc:
        return {"error": str(exc)}
    return {"results": data.get("results", []), "pagination": data.get("pagination")}


@mcp.tool()
async def get_reporting_calendar(
    category: str | None = None,
    calendar_year: int | None = None,
    per_page: int = 50,
    page: int = 1,
) -> dict[str, Any]:
    """Get FEC reporting/filing/election deadline dates via the live OpenFEC API.

    Args:
        category: Optional category filter, e.g. "reporting-dates",
            "election-dates", "24-hour-notices", "48-hour-notices".
        calendar_year: Optional calendar year filter, e.g. 2026.
        per_page: Results per page (max 100).
        page: Page number.
    """
    try:
        data = await (await _client()).get_calendar_dates(
            category=category, calendar_year=calendar_year, per_page=per_page, page=page
        )
    except OpenFECError as exc:
        return {"error": str(exc)}
    return {"results": data.get("results", []), "pagination": data.get("pagination")}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
