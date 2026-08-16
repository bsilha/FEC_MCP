"""FEC MCP server.

Exposes two complementary tool families:

1. Rulebook tools -- full-text search over official campaign-finance PDF
   guides. Federal (FEC) guides live directly in ``data/rulebooks/``
   (campaign guides for candidates, party committees, PACs, and the
   contribution-limits chart). State guides live in
   ``data/rulebooks/states/{state_code}/`` (e.g. ``ca/``, ``ny/``) and are
   entirely optional -- add them only for states you actually need. This is
   the authoritative source for compliance rules and dollar limits: answers
   are grounded in quoted PDF pages with citations, not model recall.

2. OpenFEC tools -- live lookups against the public OpenFEC API
   (api.open.fec.gov) for real candidates, committees, filings, financial
   totals, elections, and the reporting calendar. OpenFEC only covers
   federal elections; it has no state-level data.

Neither family gives legal advice; tool outputs should be treated as
research aids and cited back to their source (PDF page or OpenFEC record).
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, timedelta
from typing import Any

from mcp.server.fastmcp import FastMCP

from .deadlines import CommitteeProfile, CommitteeStatus, match_deadline
from .openfec_client import OpenFECClient, OpenFECError
from .race_lookup import resolve_committee_race
from .rulebook_index import RulebookIndex

INSTRUCTIONS = """\
This server provides two kinds of tools:

- search_rulebooks / list_rulebook_sources / list_rulebook_jurisdictions /
  get_rulebook_page: search official campaign-finance guide PDFs. Federal
  (FEC) guides are always jurisdiction "federal"; state guides (if any are
  loaded) use the state's two-letter code as jurisdiction, e.g. "ca". Call
  list_rulebook_jurisdictions first if a question is about a specific
  state, to see whether that state's rulebooks are actually loaded -- do
  NOT assume a state is covered just because federal guides are. Use these
  tools for ANY question about contribution limits, disclaimer
  requirements, coordination rules, recordkeeping, registration
  thresholds, or other compliance rules. Always cite the source and page
  number from the results, and always state which jurisdiction (federal or
  which state) an answer applies to.

- search_candidates / get_candidate / get_candidate_totals /
  search_committees / get_committee / get_committee_filings /
  get_committee_totals / search_disbursements / search_filings /
  search_elections / get_reporting_calendar: live data from the OpenFEC API
  about real candidates, committees, filings, elections, and itemized
  Schedule B disbursements (who a committee gave money to). This data is
  FEDERAL ONLY -- OpenFEC has no state-level candidates/committees/filings.

- search_advisory_opinions / get_advisory_opinion: live search over FEC
  Advisory Opinions -- the FEC's rulings on specific factual scenarios
  (e.g. "can a campaign accept cryptocurrency donations"). Use these for
  questions about a specific edge-case scenario, and search_rulebooks
  instead for general compliance rules (contribution limits, disclaimer
  requirements, etc.). Federal only, like the rest of the OpenFEC tools.

If data/rulebooks/ has no PDFs loaded yet, rulebook tools will say so --
tell the user to add FEC campaign guide PDFs there rather than answering
compliance questions from general knowledge. Likewise, if a question is
about a state with no rulebooks loaded, say so explicitly rather than
answering from general knowledge or applying federal rules to a state
question.

Never state what a loaded document says, covers, or requires unless you
actually retrieved the page saying it and can cite that source and page
number. Knowing that a jurisdiction's rulebooks are loaded is not knowing
what is in them: list_rulebook_jurisdictions and list_rulebook_sources
return titles and page counts, never page contents, so they can support a
claim that a jurisdiction IS covered but never a claim about what its
documents actually say. This applies to offers and asides as much as to
the main answer -- "California's manuals also address this" is a claim
about document contents and needs a citation exactly like any other. If
you haven't searched or read a supporting page, either do so first or say
you haven't checked, rather than describing contents you haven't seen.
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
def list_rulebook_jurisdictions() -> dict[str, Any]:
    """List every jurisdiction with rulebook PDFs loaded, e.g. "federal" and
    any state codes like "ca", "ny".

    ALWAYS call this before answering a state-specific compliance question,
    to check whether that state's rulebooks are actually loaded rather than
    assuming coverage. Federal (FEC) coverage does not imply any state is
    covered, and vice versa -- each is a fully separate set of documents.
    """
    jurisdictions = _rulebook_index.list_jurisdictions()
    if not jurisdictions:
        return {
            "jurisdictions": [],
            "message": "No rulebook PDFs are loaded at all yet. See list_rulebook_sources.",
        }
    return {"jurisdictions": jurisdictions}


@mcp.tool()
def list_rulebook_sources(jurisdiction: str | None = None) -> dict[str, Any]:
    """List the rulebook PDFs currently loaded and searchable.

    Returns each source's path, title, page count, and jurisdiction
    ("federal" or a lowercase state code). If empty, no PDFs have been
    added to data/rulebooks/ yet -- the user should add the FEC's campaign
    guides (candidates, party committees, PACs) and the contribution
    limits chart PDF there, and optionally state guides under
    data/rulebooks/states/{state_code}/.

    Args:
        jurisdiction: Optional filter, "federal" or a lowercase two-letter
            state code (e.g. "ca"). Omit to list everything.
    """
    sources = _rulebook_index.list_sources(jurisdiction=jurisdiction)
    if not sources:
        return {
            "sources": [],
            "message": (
                "No rulebook PDFs are loaded"
                + (f" for jurisdiction '{jurisdiction}'" if jurisdiction else "")
                + ". Add FEC campaign guide PDFs (e.g. Campaign Guide for "
                "Congressional Candidates and Committees, Campaign Guide "
                "for Political Party Committees, Campaign Guide for "
                "Nonconnected Committees, and the Contribution Limits "
                "chart) to data/rulebooks/, and optionally state guides "
                "under data/rulebooks/states/{state_code}/, in this repo."
            ),
        }
    return {
        "sources": [
            {
                "source": s.filename,
                "title": s.title,
                "pages": s.pages,
                "jurisdiction": s.jurisdiction,
            }
            for s in sources
        ]
    }


@mcp.tool()
def search_rulebooks(
    query: str,
    top_k: int = 8,
    source: str | None = None,
    jurisdiction: str | None = None,
) -> dict[str, Any]:
    """Full-text search the loaded rulebook PDFs (federal and/or state).

    Use this for any compliance question: contribution limits, who may
    contribute, disclaimer requirements, coordination rules, joint
    fundraising, recordkeeping, registration thresholds, reporting
    requirements, personal use of funds, foreign national/corporate
    contribution bans, etc.

    IMPORTANT: if the question is about a specific state, pass that state's
    lowercase two-letter code as `jurisdiction` (call
    list_rulebook_jurisdictions first if unsure whether it's loaded) --
    otherwise a federal-only search may return irrelevant federal rules for
    what should be a state-law question, or vice versa. If the question
    doesn't specify federal vs. state, search without a jurisdiction filter
    and check each result's jurisdiction in the response before answering.

    Args:
        query: Search terms, e.g. "individual contribution limit candidate"
            or "disclaimer requirements".
        top_k: Max number of matching pages to return (default 8).
        source: Optional exact source path (from list_rulebook_sources) to
            restrict the search to a single PDF.
        jurisdiction: Optional filter, "federal" or a lowercase two-letter
            state code (e.g. "ca"). Omit to search all loaded jurisdictions.

    Returns matching pages with a snippet (search terms marked with >>> <<<),
    which jurisdiction each match belongs to, and the exact source + page
    number to cite. Always cite these and state the jurisdiction when
    answering; if no results, say so rather than guessing.
    """
    hits = _rulebook_index.search(query, top_k=top_k, source=source, jurisdiction=jurisdiction)
    if not hits:
        sources = _rulebook_index.list_sources(jurisdiction=jurisdiction)
        if not sources:
            scope = f" for jurisdiction '{jurisdiction}'" if jurisdiction else ""
            return {
                "results": [],
                "message": f"No rulebook PDFs are loaded{scope}. See list_rulebook_jurisdictions.",
            }
        return {"results": [], "message": "No matches found for this query."}

    return {
        "results": [
            {
                "source": h.source,
                "title": h.title,
                "page": h.page,
                "jurisdiction": h.jurisdiction,
                "snippet": h.snippet,
                "citation": f"{h.title} ({h.source}), p.{h.page} [{h.jurisdiction}]",
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
        source: Exact source path as returned by list_rulebook_sources /
            search_rulebooks (e.g. "candgui.pdf" or "states/ca/limits.pdf").
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


_DISBURSEMENT_KEYS = [
    "committee_id",
    "recipient_name",
    "recipient_committee_id",
    "recipient_state",
    "entity_type",
    "entity_type_desc",
    "disbursement_amount",
    "disbursement_date",
    "disbursement_description",
    "disbursement_purpose_category",
    "disbursement_type_description",
    "line_number_label",
    "two_year_transaction_period",
]


def _trim_disbursement(item: dict[str, Any]) -> dict[str, Any]:
    trimmed = _trim(item, _DISBURSEMENT_KEYS)
    recipient_committee = item.get("recipient_committee") or {}
    trimmed["recipient_committee_type_full"] = recipient_committee.get("committee_type_full")
    return trimmed


@mcp.tool()
async def search_disbursements(
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
) -> dict[str, Any]:
    """Search a committee's itemized Schedule B disbursements via the live OpenFEC API.

    Use this to see who a committee gave money to and how much -- e.g.
    contributions/transfers to other committees, operating expenditures,
    refunds.

    IMPORTANT -- to find how much a committee gave to *party* committees
    specifically: OpenFEC has no working server-side filter for the
    recipient's committee type, so (1) set disbursement_purpose_category to
    "CONTRIBUTIONS" and separately to "TRANSFERS" (the two categories FEC
    uses for gifts/transfers to other committees) to narrow the result set
    down from all disbursements, then (2) inspect each returned record's
    `entity_type_desc` (look for "POLITICAL PARTY COMMITTEE") and
    `recipient_committee_type_full` (look for "Party - Nonqualified" or
    "Party - Qualified") to identify which recipients are actually party
    committees, and sum `disbursement_amount` across those. Results are
    itemized transactions, not a pre-summed total -- page through ALL
    results (check `pagination.count`) before summing, since a single page
    may not have every match.

    Pagination on this endpoint does NOT use a page number (OpenFEC's `page`
    param silently returns page 1's results again for schedule_b) -- it uses
    a cursor instead. To get the next page, call again with `last_index` and
    `last_disbursement_date` set to the values from the previous response's
    `pagination.last_indexes` (both fields, together). Stop once a response
    returns fewer than `per_page` results.

    Args:
        committee_id: FEC committee ID whose disbursements to search, e.g. "C00401224".
        recipient_name: Optional recipient name search text (fuzzy).
        disbursement_purpose_category: Optional filter, one of: ADMINISTRATIVE,
            ADVERTISING, CONTRIBUTIONS, EVENTS, FUNDRAISING, LOAN-REPAYMENTS,
            MATERIALS, OTHER, POLLING, REFUNDS, TRANSFERS, TRAVEL.
        disbursement_description: Optional free-text filter on the reported
            purpose of the disbursement.
        min_date: Optional lower bound, "YYYY-MM-DD".
        max_date: Optional upper bound, "YYYY-MM-DD".
        min_amount: Optional minimum disbursement amount.
        max_amount: Optional maximum disbursement amount.
        cycle: Optional two-year cycle filter, e.g. 2026.
        per_page: Results per page (max 100).
        last_index: Cursor from a previous response's
            `pagination.last_indexes.last_index`, to fetch the next page.
        last_disbursement_date: Cursor from a previous response's
            `pagination.last_indexes.last_disbursement_date`. Required
            alongside last_index once paginating.
    """
    last_indexes = (
        {"last_index": last_index, "last_disbursement_date": last_disbursement_date}
        if last_index and last_disbursement_date
        else None
    )
    try:
        data = await (await _client()).search_disbursements(
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
            last_indexes=last_indexes,
        )
    except OpenFECError as exc:
        return {"error": str(exc)}
    results = [_trim_disbursement(r) for r in data.get("results", [])]
    return {
        "results": results,
        "page_total": round(sum(r.get("disbursement_amount") or 0 for r in results), 2),
        "pagination": data.get("pagination"),
    }


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
    min_start_date: str | None = None,
    max_start_date: str | None = None,
    per_page: int = 50,
    page: int = 1,
) -> dict[str, Any]:
    """Get FEC reporting/filing/election deadline dates via the live OpenFEC API.

    Args:
        category: Optional category filter. One of: "reporting-dates" (all
            Quarterly/Monthly/Pre-Post-Election filing deadlines), "quarterly",
            "monthly", "pre-post-election", "election-dates", "ec-periods"
            (electioneering communications periods), "ie-periods"
            (independent expenditure periods, incl. 24/48-hour notices).
        min_start_date: Optional lower bound, "YYYY-MM-DD". There is no
            year-only filter -- use this plus max_start_date instead.
        max_start_date: Optional upper bound, "YYYY-MM-DD".
        per_page: Results per page (max 100).
        page: Page number.
    """
    try:
        data = await (await _client()).get_calendar_dates(
            category=category,
            min_start_date=min_start_date,
            max_start_date=max_start_date,
            per_page=per_page,
            page=page,
        )
    except OpenFECError as exc:
        return {"error": str(exc)}
    return {"results": data.get("results", []), "pagination": data.get("pagination")}


@mcp.tool()
async def search_advisory_opinions(
    q: str | None = None,
    ao_no: str | None = None,
    ao_year: str | None = None,
    ao_name: str | None = None,
    ao_status: str | None = None,
    ao_requestor: str | None = None,
    ao_commenter: str | None = None,
    ao_representative: str | None = None,
    hits_returned: int = 20,
) -> dict[str, Any]:
    """Search FEC Advisory Opinions via the live OpenFEC legal-search API (federal only).

    Advisory Opinions are the FEC's rulings on specific factual scenarios a
    requestor asked about (e.g. "can a campaign accept cryptocurrency
    donations") -- use this for questions about a specific edge case or
    scenario. Use search_rulebooks instead for general compliance rules
    (contribution limits, disclaimer requirements, recordkeeping, etc.),
    since those come from the campaign guide PDFs, not advisory opinions.

    This endpoint is Elasticsearch-backed, not a fixed database schema, so
    the exact fields on each returned advisory-opinion object aren't a
    stable contract -- read whatever keys are actually present (typically
    includes an AO number, name/subject, status, and document links) rather
    than assuming specific field names. Any document link/URL field is a
    path relative to https://www.fec.gov (e.g. "/files/legal/aos/2014-02/
    2014-02.pdf"), not a complete URL -- always prepend that origin when
    presenting a link, rather than showing the bare path or guessing at a
    full URL.

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
    try:
        data = await (await _client()).search_advisory_opinions(
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
    except OpenFECError as exc:
        return {"error": str(exc)}
    return {
        "advisory_opinions": data.get("advisory_opinions", []),
        "total_advisory_opinions": data.get("total_advisory_opinions"),
    }


@mcp.tool()
async def get_advisory_opinion(ao_no: str) -> dict[str, Any]:
    """Get one FEC Advisory Opinion's full document record by its AO number (federal only).

    Returns every document filed under this AO number -- the request,
    draft opinions, the final opinion, the vote record, and any outside
    comments -- not just the final opinion, so check each document's own
    type/category field before treating its text as the Commission's
    actual holding (a draft or a comment is not the final ruling). Any
    document link/URL field is a path relative to https://www.fec.gov, not
    a complete URL -- always prepend that origin when presenting a link.

    Args:
        ao_no: AO number as returned by search_advisory_opinions, e.g. "2014-12".
    """
    try:
        data = await (await _client()).get_advisory_opinion(ao_no)
    except OpenFECError as exc:
        return {"error": str(exc)}
    return {"docs": data.get("docs", [])}


_COMMITTEE_ID_PATTERN = re.compile(r"^C\d{8}$", re.IGNORECASE)


async def _find_committee(committee: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Look up one committee by FEC ID or by name.

    Returns (record, error). Exactly one is non-None.

    An ambiguous name is an error rather than a best guess. Picking the
    top fuzzy match would produce a complete, well-formed, confidently
    wrong deadline schedule for a committee the caller never asked about,
    and nothing downstream could detect it -- the same failure mode that
    silently resolved six unrelated committees to one race earlier in this
    feature's development. Ambiguity gets handed back to be resolved by
    someone who knows which committee is meant.
    """
    text = (committee or "").strip()
    if not text:
        return None, {"error": "Provide an FEC committee ID (e.g. C00614701) or a committee name."}

    client = await _client()

    if _COMMITTEE_ID_PATTERN.match(text):
        try:
            data = await client.get_committee(text.upper())
        except OpenFECError as exc:
            return None, {"error": str(exc)}
        results = data.get("results") or []
        if not results:
            return None, {"error": f"No committee found with ID {text.upper()!r}."}
        return results[0], None

    try:
        data = await client.search_committees(name=text, per_page=10)
    except OpenFECError as exc:
        return None, {"error": str(exc)}

    matches = data.get("results") or []
    if not matches:
        return None, {
            "error": f"No committee found matching {text!r}.",
            "hint": "Try the FEC committee ID (e.g. C00614701), or search_committees for a broader look.",
        }

    if len(matches) > 1:
        # An exact, unambiguous name match is not really ambiguity.
        exact = [m for m in matches if (m.get("name") or "").strip().lower() == text.lower()]
        if len(exact) == 1:
            return exact[0], None

        return None, {
            "error": f"{len(matches)} committees match {text!r}. Pick one and pass its committee ID.",
            "matches": [
                {
                    "committee_id": m.get("committee_id"),
                    "name": m.get("name"),
                    "state": m.get("state"),
                    "committee_type": m.get("committee_type_full"),
                    "designation": m.get("designation_full"),
                }
                for m in matches
            ],
        }

    return matches[0], None


async def _fetch_reporting_calendar(
    start: date, end: date, max_pages: int = 4
) -> tuple[list[dict[str, Any]], str | None]:
    """Every published filing deadline in a date window.

    Paginates rather than taking the first page: the window a user asks
    about routinely holds more deadlines than one page returns, and a
    silently truncated calendar would look identical to a committee simply
    having fewer obligations. Returns (records, truncation_warning).
    """
    client = await _client()
    records: list[dict[str, Any]] = []
    page = 1
    while page <= max_pages:
        data = await client.get_calendar_dates(
            category="reporting-dates",
            min_start_date=start.isoformat(),
            max_start_date=end.isoformat(),
            per_page=100,
            page=page,
        )
        records.extend(data.get("results") or [])
        pagination = data.get("pagination") or {}
        if page >= (pagination.get("pages") or 1):
            return records, None
        page += 1

    return records, (
        f"Only the first {max_pages} pages of the FEC calendar were read; "
        "some deadlines in this window may be missing. Narrow months_ahead "
        "for a complete list."
    )


@mcp.tool()
async def get_committee_deadlines(
    committee: str,
    status: str,
    state: str | None = None,
    district: str | None = None,
    months_ahead: int = 12,
) -> dict[str, Any]:
    """Which FEC filing deadlines actually bind one committee, and when.

    Filters the FEC's own published calendar down to the deadlines a
    specific committee owes. Dates are never computed here -- they come
    from the FEC -- so this cannot drift from the official calendar.

    Which deadlines apply depends on the committee's lifecycle, which is
    why `status` is required rather than defaulted: a committee that lost
    its primary owes no general-election reports, and guessing that wrong
    silently produces a wrong deadline set.

    Args:
        committee: FEC committee ID (e.g. "C00614701") or committee name
            (e.g. "Crane for Congress"). A name matching several
            committees returns the list of matches to choose from rather
            than picking one -- the wrong committee would yield a
            complete, plausible, entirely wrong schedule.
        status: Where the committee is in its election lifecycle. One of:
            "in_primary", "won_primary", "lost_primary", "won_general",
            "lost_general", "terminating", or "ongoing" for a PAC or party
            committee that has no election of its own to win or lose.
        state: Two-letter state of the FEDERAL race this committee's
            candidate is in, e.g. "MI" (this is where the federal race is,
            not a state campaign-finance jurisdiction). Needed for
            state-timed deadlines like pre-primary reports, since federal
            primaries fall on different dates in different states. OpenFEC
            often cannot report a committee's candidate, so pass this when
            you know it -- an explicit value is always used in preference
            to a looked-up one.
        district: District of that race for a House seat, e.g. "04".
        months_ahead: How far forward to look, default 12 months.

    Returns the deadlines that apply (each with its date, what it is, and
    whether it is certain), those ruled out and why, and the race used --
    which should be confirmed before relying on the state-specific ones.
    """
    try:
        committee_status = CommitteeStatus(status)
    except ValueError:
        return {
            "error": f"Unknown status {status!r}.",
            "valid_statuses": [s.value for s in CommitteeStatus],
        }

    record, lookup_error = await _find_committee(committee)
    if lookup_error is not None:
        return lookup_error
    committee_id = record.get("committee_id") or committee

    # An explicitly-passed race always wins over a looked-up one: the
    # caller knows their own client's race, and OpenFEC frequently cannot
    # report it at all.
    race_note = "state supplied by the caller"
    race_source = "caller"
    if state is None:
        resolution = await resolve_committee_race(
            await _client(), committee_id, committee_record=record
        )
        state, district = resolution.state, resolution.district
        race_note, race_source = resolution.note, resolution.resolved_via

    profile = CommitteeProfile(
        committee_id=committee_id,
        name=record.get("name") or committee_id,
        designation=record.get("designation") or "",
        filing_frequency=record.get("filing_frequency") or "",
        status=committee_status,
        state=state,
        district=district,
        office=record.get("committee_type"),
    )

    today = date.today()
    try:
        calendar, truncation = await _fetch_reporting_calendar(
            today, today + timedelta(days=31 * max(1, months_ahead))
        )
    except OpenFECError as exc:
        return {"error": str(exc)}

    applies: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for entry in calendar:
        decision = match_deadline(entry, profile)
        row = {
            "date": entry.get("start_date"),
            "deadline": entry.get("summary"),
            "description": entry.get("description"),
            "report_type": decision.family.value if decision.family else None,
            "reason": decision.reason,
            "url": entry.get("url"),
        }
        if decision.applies:
            applies.append({**row, "certain": decision.certain})
        else:
            excluded.append(row)

    applies.sort(key=lambda r: r["date"] or "")

    unverified = [r for r in applies if not r["certain"]]
    warnings = []
    if truncation:
        warnings.append(truncation)
    if profile.is_authorized and state is None:
        warnings.append(
            "This committee's race is unknown, so state-timed deadlines "
            "(pre-primary, runoff) could not be checked. Pass `state` "
            "(and `district` for a House seat) to include them."
        )
    if unverified:
        warnings.append(
            f"{len(unverified)} deadline(s) are shown but unverified -- they could "
            "not be confidently ruled in or out, and are listed rather than hidden "
            "so a real obligation is never silently dropped. Check each one."
        )

    return {
        "committee": {
            "committee_id": committee_id,
            "name": profile.name,
            "designation": record.get("designation"),
            "filing_frequency": record.get("filing_frequency"),
            "is_candidate_committee": profile.is_authorized,
        },
        "status": committee_status.value,
        "race": {
            "state": state,
            "district": district,
            "source": race_source,
            "note": race_note,
            "confirm": (
                "Confirm this is the correct race before relying on the "
                "state-specific deadlines below."
            ),
        },
        "window": {"from": today.isoformat(), "to": (today + timedelta(days=31 * months_ahead)).isoformat()},
        "deadlines": applies,
        "excluded": excluded,
        "warnings": warnings,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
