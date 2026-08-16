"""End-to-end coverage for the get_committee_deadlines tool.

Drives the tool against a stubbed OpenFEC client built from real API
payloads, so the assembly -- committee lookup, race resolution, calendar
paging, per-record matching -- is exercised without network access.
"""

import pytest

from fec_mcp import server

# Real committee record fields the tool actually reads.
CANDIDATE_COMMITTEE = {
    "committee_id": "C00614701",
    "name": "CRANE FOR CONGRESS",
    "designation": "P",
    "committee_type": "H",
    "filing_frequency": "Q",
    "candidate_ids": [],
}

PAC_COMMITTEE = {
    "committee_id": "C00401224",
    "name": "ACTBLUE",
    "designation": "U",
    "committee_type": "V",
    "filing_frequency": "M",
    "candidate_ids": [],
}

# Verbatim calendar rows (see tests/test_deadline_matching.py for why the
# categories look wrong -- the FEC files 12G/30G under "Quarterly").
CALENDAR = [
    {"calendar_category_id": 25, "summary": "October Quarterly Report Due",
     "description": "October Quarterly Report due today", "location": "FEC",
     "state": None, "start_date": "2026-10-15"},
    {"calendar_category_id": 26, "summary": "October Monthly Report Due",
     "description": "October Monthly Report due today", "location": "FEC",
     "state": None, "start_date": "2026-10-20"},
    {"calendar_category_id": 25, "summary": "12G Pre-General Report Due",
     "description": "The 12-day Pre-General Report due for all general election candidates.",
     "location": "FEC", "state": None, "start_date": "2026-10-22"},
    {"calendar_category_id": 25, "summary": "30G Post-General Report Due",
     "description": "30G Post-General Report Due", "location": "FEC",
     "state": None, "start_date": "2026-12-03"},
    {"calendar_category_id": 27, "summary": "MI Pre-Primary Report Due",
     "description": "Michigan Pre-Primary Report due", "location": "Michigan",
     "state": None, "start_date": "2026-07-23"},
    {"calendar_category_id": 27, "summary": "NY Pre-Primary Report Due",
     "description": "New York Pre-Primary Report due", "location": "New York",
     "state": None, "start_date": "2026-06-11"},
]


class StubClient:
    def __init__(self, committee, calendar=CALENDAR, pages=1, search_results=None):
        self._committee = committee
        self._calendar = calendar
        self._pages = pages
        self._search_results = search_results
        self.calendar_pages_requested = []
        self.searched_names = []

    async def get_committee(self, committee_id):
        return {"results": [self._committee]}

    async def search_committees(self, name=None, per_page=20, **kwargs):
        self.searched_names.append(name)
        if self._search_results is None:
            return {"results": [self._committee]}
        return {"results": self._search_results}

    async def get_committee_candidates(self, committee_id):
        return {"results": []}  # matches observed live behavior

    async def get_candidate(self, candidate_id):
        return {"results": []}

    async def get_calendar_dates(self, category=None, min_start_date=None,
                                 max_start_date=None, per_page=50, page=1):
        self.calendar_pages_requested.append(page)
        return {
            "results": self._calendar if page == 1 else [],
            "pagination": {"pages": self._pages, "page": page},
        }


@pytest.fixture
def stub(monkeypatch):
    def _install(committee, **kwargs):
        client = StubClient(committee, **kwargs)

        async def fake_client():
            return client

        monkeypatch.setattr(server, "_client", fake_client)
        return client

    return _install


def _summaries(rows):
    return [r["deadline"] for r in rows]


async def test_rejects_an_unknown_status_and_lists_the_valid_ones():
    result = await server.get_committee_deadlines("C00614701", status="won_the_lottery")
    assert "error" in result
    assert "in_primary" in result["valid_statuses"]


async def test_won_primary_committee_gets_the_general_election_reports(stub):
    stub(CANDIDATE_COMMITTEE)
    result = await server.get_committee_deadlines(
        "C00614701", status="won_primary", state="MI", district="04"
    )
    summaries = _summaries(result["deadlines"])

    assert "12G Pre-General Report Due" in summaries
    assert "30G Post-General Report Due" in summaries


async def test_lost_primary_committee_gets_neither_general_election_report(stub):
    """The feature's headline promise, exercised through the real tool."""
    stub(CANDIDATE_COMMITTEE)
    result = await server.get_committee_deadlines(
        "C00614701", status="lost_primary", state="MI", district="04"
    )
    summaries = _summaries(result["deadlines"])

    assert "12G Pre-General Report Due" not in summaries
    assert "30G Post-General Report Due" not in summaries
    # ...but it still owes its regular quarterly report.
    assert "October Quarterly Report Due" in summaries


async def test_quarterly_filer_does_not_get_monthly_reports(stub):
    stub(CANDIDATE_COMMITTEE)
    result = await server.get_committee_deadlines(
        "C00614701", status="in_primary", state="MI", district="04"
    )
    summaries = _summaries(result["deadlines"])

    assert "October Quarterly Report Due" in summaries
    assert "October Monthly Report Due" not in summaries


async def test_state_timed_deadlines_are_filtered_to_the_committees_race(stub):
    stub(CANDIDATE_COMMITTEE)
    result = await server.get_committee_deadlines(
        "C00614701", status="in_primary", state="MI", district="04"
    )
    summaries = _summaries(result["deadlines"])

    assert "MI Pre-Primary Report Due" in summaries
    assert "NY Pre-Primary Report Due" not in summaries


async def test_an_explicit_state_is_used_without_calling_race_resolution(stub):
    """The caller knows their own client's race, and OpenFEC frequently
    can't report it -- so an explicit value must win outright."""
    stub(CANDIDATE_COMMITTEE)
    result = await server.get_committee_deadlines(
        "C00614701", status="in_primary", state="MI", district="04"
    )
    assert result["race"]["source"] == "caller"
    assert result["race"]["state"] == "MI"


async def test_unresolved_race_still_returns_nationwide_and_regular_deadlines(stub):
    """The observed live case: OpenFEC can't say which race the committee
    is in. Most of the value survives that."""
    stub(CANDIDATE_COMMITTEE)
    result = await server.get_committee_deadlines("C00614701", status="won_primary")
    summaries = _summaries(result["deadlines"])

    assert result["race"]["state"] is None
    assert "October Quarterly Report Due" in summaries
    assert "12G Pre-General Report Due" in summaries


async def test_unresolved_race_warns_that_state_timed_deadlines_were_skipped(stub):
    stub(CANDIDATE_COMMITTEE)
    result = await server.get_committee_deadlines("C00614701", status="won_primary")
    assert any("race is unknown" in w for w in result["warnings"])


async def test_pac_gets_monthly_reports_and_flagged_election_reports(stub):
    """A PAC's election reports depend on whether it spent in the race,
    which status can't answer -- shown, but marked unverified."""
    stub(PAC_COMMITTEE)
    result = await server.get_committee_deadlines("C00401224", status="ongoing")
    summaries = _summaries(result["deadlines"])

    assert "October Monthly Report Due" in summaries
    assert "October Quarterly Report Due" not in summaries
    assert any(not r["certain"] for r in result["deadlines"])


async def test_excluded_deadlines_explain_a_lifecycle_exclusion(stub):
    """So a missing deadline can be explained rather than just absent."""
    stub(CANDIDATE_COMMITTEE)
    result = await server.get_committee_deadlines(
        "C00614701", status="lost_primary", state="MI", district="04"
    )
    reasons = {r["deadline"]: r["reason"] for r in result["excluded"]}
    assert "lost_primary" in reasons["12G Pre-General Report Due"]


async def test_excluded_deadlines_explain_a_wrong_state_exclusion(stub):
    """Checked under in_primary, where pre-primary reports are actually in
    scope -- under lost_primary the lifecycle rules it out first, and that
    reason is the more useful one to report."""
    stub(CANDIDATE_COMMITTEE)
    result = await server.get_committee_deadlines(
        "C00614701", status="in_primary", state="MI", district="04"
    )
    reasons = {r["deadline"]: r["reason"] for r in result["excluded"]}
    assert "different state" in reasons["NY Pre-Primary Report Due"]


async def test_deadlines_come_back_in_date_order(stub):
    stub(CANDIDATE_COMMITTEE)
    result = await server.get_committee_deadlines(
        "C00614701", status="won_primary", state="MI", district="04"
    )
    dates = [r["date"] for r in result["deadlines"]]
    assert dates == sorted(dates)


async def test_calendar_is_paginated_rather_than_read_one_page_deep(stub):
    """A silently truncated calendar looks exactly like a committee having
    fewer obligations, so every page in the window must be read."""
    client = stub(CANDIDATE_COMMITTEE, pages=3)
    await server.get_committee_deadlines("C00614701", status="won_primary", state="MI")
    assert client.calendar_pages_requested == [1, 2, 3]


async def test_truncated_calendar_is_warned_about(stub):
    client = stub(CANDIDATE_COMMITTEE, pages=99)
    result = await server.get_committee_deadlines(
        "C00614701", status="won_primary", state="MI"
    )
    assert any("may be missing" in w for w in result["warnings"])
    assert len(client.calendar_pages_requested) < 99


async def test_unknown_committee_reports_an_error(stub, monkeypatch):
    client = stub(CANDIDATE_COMMITTEE)

    async def empty(committee_id):
        return {"results": []}

    monkeypatch.setattr(client, "get_committee", empty)
    result = await server.get_committee_deadlines("C00000000", status="ongoing")
    assert "error" in result


# -- looking a committee up by ID or by name --------------------------------


async def test_a_committee_id_is_used_directly_without_searching(stub):
    client = stub(CANDIDATE_COMMITTEE)
    result = await server.get_committee_deadlines("C00614701", status="ongoing")

    assert client.searched_names == [], "an ID should not trigger a name search"
    assert result["committee"]["committee_id"] == "C00614701"


async def test_a_lowercase_committee_id_is_still_recognized_as_an_id(stub):
    client = stub(CANDIDATE_COMMITTEE)
    await server.get_committee_deadlines("c00614701", status="ongoing")
    assert client.searched_names == []


async def test_a_name_is_searched_for(stub):
    client = stub(CANDIDATE_COMMITTEE)
    result = await server.get_committee_deadlines("Crane for Congress", status="ongoing")

    assert client.searched_names == ["Crane for Congress"]
    assert result["committee"]["name"] == "CRANE FOR CONGRESS"


async def test_a_single_name_match_resolves_to_that_committee(stub):
    stub(CANDIDATE_COMMITTEE, search_results=[CANDIDATE_COMMITTEE])
    result = await server.get_committee_deadlines("Crane for Congress", status="ongoing")
    assert result["committee"]["committee_id"] == "C00614701"


async def test_an_ambiguous_name_returns_the_matches_instead_of_guessing(stub):
    """Picking the top fuzzy match would produce a complete, plausible,
    entirely wrong schedule for a committee nobody asked about -- and
    nothing downstream could detect it."""
    others = [
        dict(CANDIDATE_COMMITTEE, committee_id="C00111111", name="CRANE FOR CONGRESS INC"),
        dict(CANDIDATE_COMMITTEE, committee_id="C00222222", name="CRANE VICTORY FUND"),
    ]
    stub(CANDIDATE_COMMITTEE, search_results=others)
    result = await server.get_committee_deadlines("Crane", status="ongoing")

    assert "error" in result
    assert "deadlines" not in result
    assert {m["committee_id"] for m in result["matches"]} == {"C00111111", "C00222222"}


async def test_an_exact_name_match_is_not_treated_as_ambiguous(stub):
    """A fuzzy search returning near-misses alongside an exact hit isn't
    real ambiguity -- requiring an ID there would be needless friction."""
    results = [
        dict(CANDIDATE_COMMITTEE, committee_id="C00111111", name="CRANE FOR CONGRESS INC"),
        CANDIDATE_COMMITTEE,  # exact
    ]
    stub(CANDIDATE_COMMITTEE, search_results=results)
    result = await server.get_committee_deadlines("Crane for Congress", status="ongoing")

    assert "error" not in result
    assert result["committee"]["committee_id"] == "C00614701"


async def test_a_name_matching_nothing_reports_that_clearly(stub):
    stub(CANDIDATE_COMMITTEE, search_results=[])
    result = await server.get_committee_deadlines("Nonexistent Committee", status="ongoing")

    assert "error" in result
    assert "No committee found" in result["error"]


async def test_an_empty_committee_argument_is_rejected(stub):
    stub(CANDIDATE_COMMITTEE)
    result = await server.get_committee_deadlines("   ", status="ongoing")
    assert "error" in result


async def test_result_asks_for_the_race_to_be_confirmed(stub):
    stub(CANDIDATE_COMMITTEE)
    result = await server.get_committee_deadlines(
        "C00614701", status="won_primary", state="MI", district="04"
    )
    assert "Confirm" in result["race"]["confirm"]
