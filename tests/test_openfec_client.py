from unittest.mock import AsyncMock

import pytest

from fec_mcp.openfec_client import OpenFECClient, OpenFECError


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or str(payload)

    def json(self):
        return self._payload


@pytest.fixture
def client():
    c = OpenFECClient(api_key="TEST_KEY")
    yield c


async def test_search_candidates_success(client):
    client._client.get = AsyncMock(
        return_value=FakeResponse(200, {"results": [{"candidate_id": "P123", "name": "DOE, JANE"}]})
    )
    data = await client.search_candidates(name="Jane Doe", state="CA")
    assert data["results"][0]["candidate_id"] == "P123"

    _, kwargs = client._client.get.call_args
    assert kwargs["params"]["api_key"] == "TEST_KEY"
    assert kwargs["params"]["q"] == "Jane Doe"
    assert kwargs["params"]["state"] == "CA"
    assert "office" not in kwargs["params"]  # None values dropped


async def test_rate_limit_raises_openfec_error(client):
    client._client.get = AsyncMock(return_value=FakeResponse(429))
    with pytest.raises(OpenFECError, match="rate limit"):
        await client.search_committees(name="Acme PAC")


async def test_forbidden_raises_openfec_error(client):
    client._client.get = AsyncMock(return_value=FakeResponse(403))
    with pytest.raises(OpenFECError, match="403"):
        await client.get_candidate("P123")


async def test_generic_error_raises_openfec_error(client):
    client._client.get = AsyncMock(return_value=FakeResponse(500, text="boom"))
    with pytest.raises(OpenFECError, match="500"):
        await client.get_committee("C123")


async def test_network_error_raises_openfec_error(client):
    import httpx

    async def raise_network_error(*args, **kwargs):
        raise httpx.ConnectError("no route")

    client._client.get = raise_network_error
    with pytest.raises(OpenFECError, match="Network error"):
        await client.get_committee("C123")


def test_default_api_key_from_env(monkeypatch):
    monkeypatch.delenv("FEC_API_KEY", raising=False)
    c = OpenFECClient()
    assert c._api_key == "DEMO_KEY"

    monkeypatch.setenv("FEC_API_KEY", "MYKEY")
    c2 = OpenFECClient()
    assert c2._api_key == "MYKEY"
