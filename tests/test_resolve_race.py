"""Coverage for server.resolve_race, the race prefill behind the roster.

The point of this seam is a district: no committee record carries one, so
filling the district box means going committee -> candidate -> race. What
matters most here is what happens when that fails, which live probing says
is common -- OpenFEC has returned no candidate at all for real principal
campaign committees. A failed lookup must produce an empty field, never a
plausible one.
"""

import pytest

from fec_mcp import server

CRANE_COMMITTEE = {
    "committee_id": "C00784934",
    "name": "ELI CRANE FOR CONGRESS",
    "designation": "P",
    "committee_type": "H",
    "candidate_ids": ["H2AZ01234"],
}

CRANE_CANDIDATE = {
    "candidate_id": "H2AZ01234",
    "name": "CRANE, ELI",
    "office": "H",
    "state": "AZ",
    "district": "02",
    "election_years": [2022, 2024, 2026],
}


class StubClient:
    """Only the routes race_lookup actually uses."""

    def __init__(self, committee=None, linked=None, candidates=None):
        self._committee = committee or CRANE_COMMITTEE
        self._linked = linked if linked is not None else []
        self._candidates = candidates or {}

    async def get_committee(self, committee_id):
        return {"results": [self._committee]}

    async def get_committee_candidates(self, committee_id):
        return {"results": self._linked}

    async def get_candidate(self, candidate_id):
        record = self._candidates.get(candidate_id)
        return {"results": [record] if record else []}


@pytest.fixture
def stub(monkeypatch):
    def _install(**kwargs):
        client = StubClient(**kwargs)

        async def fake_client():
            return client

        monkeypatch.setattr(server, "_client", fake_client)
        return client

    return _install


@pytest.mark.asyncio
async def test_a_house_committee_resolves_to_a_state_and_district(stub):
    stub(linked=[CRANE_CANDIDATE])
    race = await server.resolve_race("C00784934")

    assert race["state"] == "AZ"
    assert race["district"] == "02"
    assert race["office"] == "H"


@pytest.mark.asyncio
async def test_the_district_comes_from_the_candidate_not_the_committee(stub):
    """The committee record has a state and no district at all; only the
    candidate record can answer the district question."""
    stub(linked=[], candidates={"H2AZ01234": CRANE_CANDIDATE})
    race = await server.resolve_race("C00784934")

    assert race["district"] == "02"
    assert race["resolved_via"] == "committee_record_candidate_ids"


@pytest.mark.asyncio
async def test_an_unresolvable_committee_yields_no_state_rather_than_a_guess(stub):
    """Observed live for real committees: no linked candidate anywhere.
    The caller renders this as an empty box, not as a value."""
    stub(committee={**CRANE_COMMITTEE, "candidate_ids": []}, linked=[])
    race = await server.resolve_race("C00784934")

    assert race["state"] is None
    assert race["district"] is None
    assert race["resolved_via"] == "unresolved"
    assert race["note"]


@pytest.mark.asyncio
async def test_a_senate_candidate_resolves_with_no_district(stub):
    """A Senate seat is statewide. The blank must survive as a blank."""
    stub(linked=[{
        "candidate_id": "S6MI00123", "name": "EL-SAYED, ABDUL",
        "office": "S", "state": "MI", "district": " ", "election_years": [2026],
    }])
    race = await server.resolve_race("C00694455")

    assert race["state"] == "MI"
    assert race["district"] is None
    assert race["office"] == "S"


@pytest.mark.asyncio
async def test_several_linked_candidates_are_reported_not_hidden(stub):
    """A committee reused across seats resolves to the most recent, and
    says the others exist so the choice is visible."""
    stub(linked=[
        CRANE_CANDIDATE,
        {"candidate_id": "H0AZ09999", "name": "CRANE, ELI", "office": "H",
         "state": "AZ", "district": "01", "election_years": [2020]},
    ])
    race = await server.resolve_race("C00784934")

    assert race["district"] == "02"          # the 2026 record wins
    assert len(race["alternatives"]) == 1
    assert race["alternatives"][0]["district"] == "01"


@pytest.mark.asyncio
async def test_an_implausibly_large_result_set_resolves_to_nothing(stub):
    """The guard that exists because /candidates/?committee_id= once
    returned an unfiltered page and the chain believed it."""
    stub(linked=[
        {"candidate_id": f"H0XX{i:05d}", "name": f"SOMEONE {i}", "office": "H",
         "state": "XX", "district": "01", "election_years": [2026]}
        for i in range(20)
    ], committee={**CRANE_COMMITTEE, "candidate_ids": []})
    race = await server.resolve_race("C00784934")

    assert race["state"] is None
    assert "unscoped" in race["note"]


@pytest.mark.asyncio
async def test_resolve_race_is_not_exposed_as_an_mcp_tool():
    """get_committee_deadlines already resolves a race when the caller
    does not supply one, so a model has no reason to reach for this."""
    tools = await server.mcp.list_tools()
    assert "resolve_race" not in {t.name for t in tools}
