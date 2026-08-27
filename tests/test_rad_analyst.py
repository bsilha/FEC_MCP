"""Coverage for get_rad_analyst.

Weighted toward the failure this endpoint invites. It filters by query
parameter, and OpenFEC has been observed to ignore an unsupported one and
serve an unfiltered page instead of erroring -- that is how a Michigan
committee once resolved to a race in another state. Naming the wrong
analyst is the same class of mistake: a confident, plausible, wrong
answer that nobody would think to check.
"""

import pytest

from fec_mcp import server


class StubClient:
    def __init__(self, results):
        self._results = results
        self.asked_for = None

    async def get_rad_analyst(self, committee_id):
        self.asked_for = committee_id
        return {"results": self._results}


@pytest.fixture
def wired(monkeypatch):
    def _install(results):
        client = StubClient(results)

        async def fake_client():
            return client

        monkeypatch.setattr(server, "_client", fake_client)
        return client

    return _install


# Shaped from a real /rad-analyst/ response, captured live. The types
# matter as much as the keys: telephone_ext comes back as a NUMBER.
ANALYST = {
    "analyst_id": 790053.0,
    "analyst_short_id": 403.0,
    "assignment_update_date": "2026-02-01",
    "committee_id": "C00401224",
    "committee_name": "A COMMITTEE",
    "email": "danalyst@fec.gov",
    "first_name": "Dana",
    "last_name": "Reyes",
    "rad_branch": "Authorized",
    "telephone_ext": 1234.0,
    "title": "Senior Campaign Finance Analyst",
}


@pytest.mark.asyncio
async def test_reports_the_assigned_analyst(wired):
    wired([ANALYST])
    result = await server.get_rad_analyst("C00401224")

    assert result["analyst"]["name"] == "Dana Reyes"
    assert result["analyst"]["extension"] == "1234"
    assert result["analyst"]["branch"] == "Authorized"
    assert result["analyst"]["email"] == "danalyst@fec.gov"
    assert result["committee_name"] == "A COMMITTEE"


@pytest.mark.asyncio
async def test_an_extension_travels_with_the_number_to_dial_it_from(wired):
    """An extension on its own cannot be called. RAD analysts have no
    direct outside line, so the extension is useless without it."""
    wired([ANALYST])
    result = await server.get_rad_analyst("C00401224")

    assert server.FEC_MAIN_LINE in result["how_to_reach"]
    assert "1234" in result["how_to_reach"]


@pytest.mark.asyncio
async def test_records_for_other_committees_are_never_reported(wired):
    """The guard that matters. If the query filter is ignored and a page
    of other committees' analysts comes back, saying nothing is correct
    and naming one of them is not."""
    wired([
        {**ANALYST, "committee_id": "C00999999", "first_name": "Someone", "last_name": "Else"},
        {**ANALYST, "committee_id": "C00888888", "first_name": "Also", "last_name": "Wrong"},
    ])
    result = await server.get_rad_analyst("C00401224")

    assert result["analyst"] is None
    assert "unfiltered" in result["error"]
    assert "Else" not in str(result)


@pytest.mark.asyncio
async def test_a_mixed_page_keeps_only_this_committees_analyst(wired):
    wired([
        {**ANALYST, "committee_id": "C00999999", "first_name": "Someone", "last_name": "Else"},
        ANALYST,
    ])
    result = await server.get_rad_analyst("C00401224")

    assert result["analyst"]["name"] == "Dana Reyes"


@pytest.mark.asyncio
async def test_no_analyst_on_file_is_a_note_not_an_error(wired):
    """Normal for a terminated or brand-new committee, so it must not
    look like a failure."""
    wired([])
    result = await server.get_rad_analyst("C00401224")

    assert result["analyst"] is None
    assert "error" not in result
    assert server.FEC_MAIN_LINE in result["note"]


@pytest.mark.asyncio
async def test_the_most_recent_assignment_wins(wired):
    wired([
        {**ANALYST, "first_name": "Older", "last_name": "Assignment",
         "assignment_update_date": "2024-01-01"},
        {**ANALYST, "first_name": "Newer", "last_name": "Assignment",
         "assignment_update_date": "2026-06-01"},
    ])
    result = await server.get_rad_analyst("C00401224")

    assert result["analyst"]["name"] == "Newer Assignment"
    assert len(result["other_records"]) == 1


@pytest.mark.asyncio
async def test_a_name_under_different_keys_still_reads(wired):
    """This endpoint has never been called live from here, so the name is
    assembled from whatever key carries it rather than one assumed pair."""
    wired([{"committee_id": "C00401224", "analyst_name": "Pat Okafor"}])
    result = await server.get_rad_analyst("C00401224")

    assert result["analyst"]["name"] == "Pat Okafor"


@pytest.mark.asyncio
async def test_committee_id_is_normalised(wired):
    client = wired([ANALYST])
    result = await server.get_rad_analyst("  c00401224 ")

    assert client.asked_for == "C00401224"
    assert result["analyst"]["name"] == "Dana Reyes"


@pytest.mark.asyncio
async def test_a_missing_committee_id_is_refused_without_calling_out(wired):
    client = wired([ANALYST])
    result = await server.get_rad_analyst("")

    assert "error" in result
    assert client.asked_for is None


@pytest.mark.asyncio
async def test_a_numeric_extension_is_rendered_dialable(wired):
    """Regression guard from a live check. OpenFEC sends telephone_ext as
    a number, so str() on it produced "ask for extension 1170.0" -- which
    cannot be dialled, and reads as bad data rather than a bug here."""
    wired([{**ANALYST, "telephone_ext": 1170.0}])
    result = await server.get_rad_analyst("C00401224")

    assert result["analyst"]["extension"] == "1170"
    assert "1170.0" not in result["how_to_reach"]
    assert "extension 1170" in result["how_to_reach"]


@pytest.mark.asyncio
async def test_an_extension_given_as_a_string_is_left_alone(wired):
    wired([{**ANALYST, "telephone_ext": "1170"}])
    result = await server.get_rad_analyst("C00401224")

    assert result["analyst"]["extension"] == "1170"


@pytest.mark.asyncio
async def test_the_analysts_email_is_offered_alongside_the_phone(wired):
    """The FEC gives analysts a direct address, which beats an extension
    on a switchboard for anything that is not urgent."""
    wired([ANALYST])
    result = await server.get_rad_analyst("C00401224")

    assert "danalyst@fec.gov" in result["how_to_reach"]


@pytest.mark.asyncio
async def test_a_record_without_an_email_still_gives_a_way_through(wired):
    wired([{**ANALYST, "email": None}])
    result = await server.get_rad_analyst("C00401224")

    assert result["analyst"]["email"] is None
    assert server.FEC_MAIN_LINE in result["how_to_reach"]
    assert result["how_to_reach"].endswith(".")
