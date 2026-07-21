from unittest.mock import AsyncMock

import pytest

import fec_mcp.server as server


async def test_list_tools_registers_all_expected_tools():
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {
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
        "search_disbursements",
        "search_filings",
        "search_elections",
        "get_reporting_calendar",
    }


def test_list_rulebook_sources_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_rulebook_index", server.RulebookIndex(rulebooks_dir=tmp_path))
    result = server.list_rulebook_sources()
    assert result["sources"] == []
    assert "No rulebook PDFs" in result["message"]


def test_list_rulebook_jurisdictions_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_rulebook_index", server.RulebookIndex(rulebooks_dir=tmp_path))
    result = server.list_rulebook_jurisdictions()
    assert result["jurisdictions"] == []
    assert "No rulebook PDFs" in result["message"]


def test_list_rulebook_jurisdictions_and_state_filtering(tmp_path, monkeypatch):
    from fec_mcp.rulebook_index import RulebookIndex

    fed = tmp_path / "candgui.pdf"
    ca = tmp_path / "states" / "ca" / "limits.pdf"
    fed.parent.mkdir(parents=True, exist_ok=True)
    ca.parent.mkdir(parents=True, exist_ok=True)
    fed.write_bytes(b"%PDF-fake")
    ca.write_bytes(b"%PDF-fake")

    class FakePage:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    class FakeReader:
        registry = {
            str(fed): ["federal contribution limit text"],
            str(ca): ["california contribution limit text"],
        }

        def __init__(self, path):
            self.pages = [FakePage(t) for t in self.registry[path]]
            self.metadata = None

    import fec_mcp.rulebook_index as ri

    monkeypatch.setattr(ri, "PdfReader", FakeReader)
    monkeypatch.setattr(server, "_rulebook_index", RulebookIndex(rulebooks_dir=tmp_path))

    jurisdictions = server.list_rulebook_jurisdictions()
    assert {j["jurisdiction"] for j in jurisdictions["jurisdictions"]} == {"federal", "ca"}

    sources_ca = server.list_rulebook_sources(jurisdiction="ca")
    assert [s["source"] for s in sources_ca["sources"]] == ["states/ca/limits.pdf"]

    search_ca = server.search_rulebooks("contribution limit", jurisdiction="ca")
    assert len(search_ca["results"]) == 1
    assert search_ca["results"][0]["jurisdiction"] == "ca"
    assert search_ca["results"][0]["source"] == "states/ca/limits.pdf"

    search_all = server.search_rulebooks("contribution limit")
    assert len(search_all["results"]) == 2


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
