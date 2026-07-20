from unittest.mock import AsyncMock

import pytest

import fec_mcp.server as server


async def test_list_tools_registers_all_expected_tools():
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {
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
        "search_filings",
        "search_elections",
        "get_reporting_calendar",
    }


def test_list_rulebook_sources_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_rulebook_index", server.RulebookIndex(rulebooks_dir=tmp_path))
    result = server.list_rulebook_sources()
    assert result["sources"] == []
    assert "No rulebook PDFs" in result["message"]


async def test_search_candidates_trims_fields(monkeypatch):
    fake_client = AsyncMock()
    fake_client.search_candidates = AsyncMock(
        return_value={
            "results": [
                {
                    "candidate_id": "P123",
                    "name": "DOE, JANE",
                    "party_full": "DEMOCRATIC PARTY",
                    "office_full": "President",
                    "state": "US",
                    "election_years": [2028],
                    "candidate_status": "C",
                    "some_internal_field_we_dont_want": "noise",
                }
            ],
            "pagination": {"page": 1, "pages": 1},
        }
    )

    async def fake_get_client():
        return fake_client

    monkeypatch.setattr(server, "_client", fake_get_client)

    result = await server.search_candidates(name="Jane Doe")
    assert result["results"] == [
        {
            "candidate_id": "P123",
            "name": "DOE, JANE",
            "party_full": "DEMOCRATIC PARTY",
            "office_full": "President",
            "state": "US",
            "election_years": [2028],
            "candidate_status": "C",
        }
    ]
    assert result["pagination"] == {"page": 1, "pages": 1}


async def test_search_candidates_surfaces_openfec_error(monkeypatch):
    from fec_mcp.openfec_client import OpenFECError

    fake_client = AsyncMock()
    fake_client.search_candidates = AsyncMock(side_effect=OpenFECError("boom"))

    async def fake_get_client():
        return fake_client

    monkeypatch.setattr(server, "_client", fake_get_client)

    result = await server.search_candidates(name="Jane Doe")
    assert result == {"error": "boom"}
