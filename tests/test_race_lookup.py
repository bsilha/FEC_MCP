"""Coverage for the committee -> race resolution chain.

Driven by fake clients rather than the live API: the point of the chain
is what happens when individual endpoints fail or come back empty, and
those states are the ones a live API won't reproduce on demand. The
candidate records used are copied from real OpenFEC output, including
the " " (single space) district a real primary-election row carried.
"""

import pytest

from fec_mcp.openfec_client import OpenFECError
from fec_mcp.race_lookup import RaceResolution, resolve_committee_race

# Verbatim shape from OpenFEC /candidates/.
CANDIDATE = {
    "candidate_id": "H2CA18171",
    "name": "ACEVEDO-ARREGUIN, LUIS ANTONIO",
    "office": "H",
    "state": "CA",
    "district": "18",
    "election_years": [2022, 2026],
    "cycles": [2022, 2024, 2026],
}

OLDER_CANDIDATE = {
    "candidate_id": "H0CA18999",
    "name": "PRIOR, CANDIDATE",
    "office": "H",
    "state": "CA",
    "district": "18",
    "election_years": [2018],
    "cycles": [2018],
}


class FakeClient:
    """Records which routes were called so tests can assert the chain
    stopped where it should, rather than only checking the final answer."""

    def __init__(self, *, list_candidates=None, committee_candidates=None,
                 committee=None, candidate=None):
        self._list_candidates = list_candidates
        self._committee_candidates = committee_candidates
        self._committee = committee
        self._candidate = candidate
        self.calls: list[str] = []

    async def _serve(self, name, value):
        self.calls.append(name)
        if value is None:
            raise OpenFECError(f"{name} unavailable")
        if isinstance(value, Exception):
            raise value
        return value

    async def list_candidates(self, **kwargs):
        return await self._serve("list_candidates", self._list_candidates)

    async def get_committee_candidates(self, committee_id):
        return await self._serve("committee_candidates", self._committee_candidates)

    async def get_committee(self, committee_id):
        return await self._serve("get_committee", self._committee)

    async def get_candidate(self, candidate_id):
        return await self._serve("get_candidate", self._candidate)


async def test_route_1_resolves_and_short_circuits_the_rest():
    client = FakeClient(list_candidates={"results": [CANDIDATE]})
    result = await resolve_committee_race(client, "C00832790")

    assert result.resolved
    assert (result.state, result.district, result.office) == ("CA", "18", "H")
    assert result.resolved_via == "candidates_committee_id_filter"
    assert client.calls == ["list_candidates"], "later routes must not run once one succeeds"


async def test_falls_through_to_route_2_when_route_1_is_empty():
    client = FakeClient(
        list_candidates={"results": []},
        committee_candidates={"results": [CANDIDATE]},
    )
    result = await resolve_committee_race(client, "C00832790")

    assert result.resolved
    assert result.resolved_via == "committee_candidates_endpoint"


async def test_falls_through_to_route_2_when_route_1_errors():
    """One endpoint being unavailable must not abort the whole chain --
    that's the entire reason there is a chain."""
    client = FakeClient(
        list_candidates=None,  # raises
        committee_candidates={"results": [CANDIDATE]},
    )
    result = await resolve_committee_race(client, "C00832790")

    assert result.resolved
    assert result.resolved_via == "committee_candidates_endpoint"


async def test_falls_through_to_route_3_candidate_ids_on_the_committee():
    client = FakeClient(
        list_candidates={"results": []},
        committee_candidates={"results": []},
        committee={"results": [{"candidate_ids": ["H2CA18171"]}]},
        candidate={"results": [CANDIDATE]},
    )
    result = await resolve_committee_race(client, "C00832790")

    assert result.resolved
    assert result.resolved_via == "committee_record_candidate_ids"


async def test_route_3_uses_a_committee_record_already_in_hand():
    """Avoids a redundant request when the caller already fetched it."""
    client = FakeClient(
        list_candidates={"results": []},
        committee_candidates={"results": []},
        candidate={"results": [CANDIDATE]},
    )
    result = await resolve_committee_race(
        client, "C00832790", committee_record={"candidate_ids": ["H2CA18171"]}
    )

    assert result.resolved
    assert "get_committee" not in client.calls


async def test_unresolved_when_every_route_comes_up_empty():
    """The observed real-world case: candidate_ids empty on a genuine
    principal campaign committee. Must report honestly, not guess."""
    client = FakeClient(
        list_candidates={"results": []},
        committee_candidates={"results": []},
        committee={"results": [{"candidate_ids": []}]},
    )
    result = await resolve_committee_race(client, "C00832790")

    assert not result.resolved
    assert result.state is None
    assert result.resolved_via == "unresolved"
    assert "Provide the state" in result.note


async def test_unresolved_note_records_every_route_it_tried():
    """So a failure is diagnosable from the tool output alone."""
    client = FakeClient(list_candidates=None, committee_candidates=None, committee=None)
    result = await resolve_committee_race(client, "C00832790")

    assert not result.resolved
    for fragment in ("candidates?committee_id=", "/committee/{id}/candidates/", "committee record"):
        assert fragment in result.note


async def test_a_candidate_without_a_state_is_not_treated_as_resolved():
    """A record that can't answer the question is not an answer."""
    client = FakeClient(
        list_candidates={"results": [{"candidate_id": "X", "name": "NO STATE"}]},
        committee_candidates={"results": [CANDIDATE]},
    )
    result = await resolve_committee_race(client, "C00832790")

    assert result.state == "CA"
    assert result.resolved_via == "committee_candidates_endpoint"


async def test_multiple_linked_candidates_picks_the_most_recent():
    client = FakeClient(list_candidates={"results": [OLDER_CANDIDATE, CANDIDATE]})
    result = await resolve_committee_race(client, "C00832790")

    assert result.candidate_id == "H2CA18171"
    assert "picked the most recent" in result.note


async def test_multiple_linked_candidates_surfaces_the_alternatives():
    """A choice was made on the user's behalf, so it must be visible."""
    client = FakeClient(list_candidates={"results": [OLDER_CANDIDATE, CANDIDATE]})
    result = await resolve_committee_race(client, "C00832790")

    assert len(result.alternatives) == 1
    assert result.alternatives[0]["candidate_id"] == "H0CA18999"


async def test_blank_district_is_normalized_to_absent():
    """Real statewide/primary rows carry " " rather than null; left as-is
    it would compare as a district and match nothing."""
    statewide = dict(CANDIDATE, district=" ", office="S")
    client = FakeClient(list_candidates={"results": [statewide]})
    result = await resolve_committee_race(client, "C00832790")

    assert result.district is None
    assert result.state == "CA"


async def test_every_resolution_asks_for_confirmation():
    """Even a clean resolve: every failure mode here is silent, producing
    a plausible race and therefore plausible wrong deadlines."""
    client = FakeClient(list_candidates={"results": [CANDIDATE]})
    result = await resolve_committee_race(client, "C00832790")
    assert result.needs_confirmation


@pytest.mark.parametrize("payload", [{}, {"results": None}])
async def test_malformed_payloads_do_not_crash_the_chain(payload):
    client = FakeClient(
        list_candidates=payload,
        committee_candidates={"results": [CANDIDATE]},
    )
    result = await resolve_committee_race(client, "C00832790")
    assert result.resolved


def test_unresolved_resolution_is_falsy_on_resolved_property():
    assert not RaceResolution().resolved
