"""Thin async client for the live OpenFEC API (https://api.open.fec.gov/v1).

Covers candidate, committee, filing, financial-totals, election, and
reporting-calendar lookups. Does NOT cover contribution limits or regulation
text -- OpenFEC has no endpoint for those; see rulebook_index.py for that,
which searches the official FEC PDF guides instead.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

BASE_URL = "https://api.open.fec.gov/v1"
DEFAULT_TIMEOUT = 20.0


class OpenFECError(RuntimeError):
    """Raised when the OpenFEC API returns an error response."""


def _api_key() -> str:
    # api.data.gov issues free keys at https://api.data.gov/signup/.
    # DEMO_KEY works but is heavily rate-limited (per api.data.gov policy).
    return os.environ.get("FEC_API_KEY", "DEMO_KEY")


class OpenFECClient:
    def __init__(self, api_key: str | None = None, timeout: float = DEFAULT_TIMEOUT):
        self._api_key = api_key or _api_key()
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = {k: v for k, v in (params or {}).items() if v is not None}
        query["api_key"] = self._api_key
        try:
            resp = await self._client.get(path, params=query)
        except httpx.RequestError as exc:
            raise OpenFECError(f"Network error calling OpenFEC API ({path}): {exc}") from exc

        if resp.status_code == 429:
            raise OpenFECError(
                "OpenFEC API rate limit exceeded. If using DEMO_KEY, get a free "
                "personal key at https://api.data.gov/signup/ and set FEC_API_KEY."
            )
        if resp.status_code == 403:
            raise OpenFECError(
                "OpenFEC API rejected the request (403) -- check that FEC_API_KEY is valid."
            )
        if resp.status_code >= 400:
            raise OpenFECError(f"OpenFEC API error {resp.status_code} for {path}: {resp.text[:500]}")

        return resp.json()

    # -- Candidates ----------------------------------------------------

    async def search_candidates(
        self,
        name: str | None = None,
        state: str | None = None,
        office: str | None = None,  # H, S, P
        party: str | None = None,
        cycle: int | None = None,
        candidate_status: str | None = None,  # C, F, N, P
        per_page: int = 20,
        page: int = 1,
    ) -> dict[str, Any]:
        return await self._get(
            "/candidates/search/",
            {
                "q": name,
                "state": state,
                "office": office,
                "party": party,
                "cycle": cycle,
                "candidate_status": candidate_status,
                "per_page": per_page,
                "page": page,
                "sort": "name",
            },
        )

    async def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        return await self._get(f"/candidate/{candidate_id}/")

    async def get_candidate_totals(self, candidate_id: str, cycle: int | None = None) -> dict[str, Any]:
        return await self._get(f"/candidate/{candidate_id}/totals/", {"cycle": cycle})

    # -- Committees ------------------------------------------------------

    async def search_committees(
        self,
        name: str | None = None,
        state: str | None = None,
        committee_type: str | None = None,  # e.g. P, H, S, N, Q, O, X, Y, Z
        designation: str | None = None,  # A, J, P, U, B, D
        cycle: int | None = None,
        per_page: int = 20,
        page: int = 1,
    ) -> dict[str, Any]:
        return await self._get(
            "/committees/",
            {
                "q": name,
                "state": state,
                "committee_type": committee_type,
                "designation": designation,
                "cycle": cycle,
                "per_page": per_page,
                "page": page,
                "sort": "name",
            },
        )

    async def get_committee(self, committee_id: str) -> dict[str, Any]:
        return await self._get(f"/committee/{committee_id}/")

    async def get_committee_filings(
        self,
        committee_id: str,
        form_type: str | None = None,
        cycle: int | None = None,
        per_page: int = 20,
        page: int = 1,
    ) -> dict[str, Any]:
        return await self._get(
            f"/committee/{committee_id}/filings/",
            {"form_type": form_type, "cycle": cycle, "per_page": per_page, "page": page, "sort": "-receipt_date"},
        )

    async def get_committee_totals(
        self, committee_id: str, cycle: int | None = None, per_page: int = 10
    ) -> dict[str, Any]:
        return await self._get(
            f"/committee/{committee_id}/totals/", {"cycle": cycle, "per_page": per_page}
        )

    # -- Filings (cross-committee) ---------------------------------------

    async def search_filings(
        self,
        committee_id: str | None = None,
        candidate_id: str | None = None,
        form_type: str | None = None,
        cycle: int | None = None,
        per_page: int = 20,
        page: int = 1,
    ) -> dict[str, Any]:
        return await self._get(
            "/filings/",
            {
                "committee_id": committee_id,
                "candidate_id": candidate_id,
                "form_type": form_type,
                "cycle": cycle,
                "per_page": per_page,
                "page": page,
                "sort": "-receipt_date",
            },
        )

    # -- Elections & reporting calendar -----------------------------------

    async def search_elections(
        self,
        state: str | None = None,
        office: str | None = None,  # house, senate, president
        cycle: int | None = None,
        district: str | None = None,
        per_page: int = 20,
        page: int = 1,
    ) -> dict[str, Any]:
        return await self._get(
            "/elections/",
            {
                "state": state,
                "office": office,
                "cycle": cycle,
                "district": district,
                "per_page": per_page,
                "page": page,
            },
        )

    async def get_calendar_dates(
        self,
        category: str | None = None,  # e.g. "reporting-dates", "election-dates"
        calendar_year: int | None = None,
        per_page: int = 50,
        page: int = 1,
    ) -> dict[str, Any]:
        return await self._get(
            "/calendar-dates/",
            {
                "category": category,
                "calendar_year": calendar_year,
                "per_page": per_page,
                "page": page,
                "sort": "date",
            },
        )
