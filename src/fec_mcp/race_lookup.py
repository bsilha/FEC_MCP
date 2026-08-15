"""Resolve which federal race a committee's candidate is running in.

Needed because the FEC's own filing calendar is keyed by state: federal
primaries fall on different dates in different states, so rows read
"NY Pre-Primary Report Due", "OK Pre-Primary Report Due", and so on.
Deciding whether one of those binds a given committee means knowing which
state's federal race its candidate is in. (This is the location of a
FEDERAL race -- nothing here touches state campaign-finance law.)

Getting from a committee to its candidate turns out not to be reliable
through any single endpoint: a live probe found `candidate_ids` empty on
a genuine principal campaign committee, so the obvious path silently
yields nothing for at least some real committees. Rather than pick one
endpoint and quietly fail whenever it comes back empty, this tries
several in order and reports which one actually worked, so the dead links
can be removed once real usage shows which are load-bearing.

Every route here is PATH-scoped, never filtered by query parameter, and
that is a correctness requirement rather than a style preference. An
earlier version led with /candidates/?committee_id=, which looked ideal:
one request, and the candidate record already carries state/district/
office. A live check against a real Michigan committee returned twenty
candidates spanning a dozen states, alphabetically ordered -- OpenFEC
ignores an unsupported query filter and serves the unfiltered first page
instead of erroring. The chain read that as "20 candidates are linked to
this committee", picked the most recent by election year, and confidently
reported a race that had nothing to do with the committee. No exception
was raised at any point.

So results are never trusted to satisfy a filter that was merely
requested: a route must be scoped by URL path, and an implausibly large
result set is treated as evidence the scoping did not happen.

Nothing here guesses. When no route resolves, the result says so and the
caller asks the user for the state and district instead of proceeding on
an assumption -- a wrong race silently produces a wrong deadline set,
which is the failure this whole feature exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .openfec_client import OpenFECError


@dataclass(frozen=True)
class RaceResolution:
    """Which race a committee belongs to, and how confident we are.

    `needs_confirmation` is True even on a clean resolve. The caller is
    expected to show the resolved race back to the user before relying on
    the deadlines derived from it, because every failure mode here is
    silent: an out-of-date or ambiguous candidate link produces a
    plausible-looking race and therefore a plausible-looking, wrong set
    of deadlines.
    """

    state: str | None = None
    district: str | None = None
    office: str | None = None
    candidate_id: str | None = None
    candidate_name: str | None = None
    resolved_via: str = "unresolved"
    needs_confirmation: bool = True
    note: str = ""
    alternatives: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def resolved(self) -> bool:
        return self.state is not None


def _cycle_key(candidate: dict[str, Any]) -> int:
    """Most recent election year on a candidate record, for picking between
    several candidates linked to the same committee."""
    years = candidate.get("election_years") or candidate.get("cycles") or []
    numeric = [int(y) for y in years if str(y).isdigit()]
    return max(numeric) if numeric else 0


def _from_candidate(candidate: dict[str, Any], via: str, note: str = "") -> RaceResolution:
    district = candidate.get("district")
    # Real records carry " " (a single space) for statewide/at-large rows;
    # normalize that to absent rather than letting it compare as a district.
    if isinstance(district, str) and not district.strip():
        district = None

    return RaceResolution(
        state=(candidate.get("state") or None),
        district=district,
        office=(candidate.get("office") or None),
        candidate_id=(candidate.get("candidate_id") or None),
        candidate_name=(candidate.get("name") or None),
        resolved_via=via,
        needs_confirmation=True,
        note=note,
    )


# A committee is linked to a handful of candidates at most -- one in the
# ordinary case, a few for a committee reused across cycles or seats.
# Anything beyond this is not a linked set: it is an unscoped result page,
# which is exactly how a Michigan committee once resolved to a random race.
# The guard is defence-in-depth behind path-scoped routes, so that a route
# which stops being scoped fails loudly instead of silently.
_MAX_PLAUSIBLE_LINKED_CANDIDATES = 5


def _pick(candidates: list[dict[str, Any]], via: str) -> tuple[RaceResolution | None, str]:
    """Choose one candidate from a linked set, preferring the most recent.

    A committee can be linked to more than one candidate over its life. The
    newest is the right default, but the others are carried on the result
    so the caller can offer them rather than hiding that a choice was made.

    Returns (resolution, reason); resolution is None when this route
    produced nothing usable, and reason explains why for the caller's
    diagnostic note.
    """
    usable = [c for c in candidates if c.get("state")]
    if not usable:
        return None, "returned no candidate with a state"

    if len(usable) > _MAX_PLAUSIBLE_LINKED_CANDIDATES:
        return None, (
            f"returned {len(usable)} candidates, far more than a committee can "
            "plausibly be linked to -- treating the result as unscoped rather "
            "than resolving to one of them"
        )

    ordered = sorted(usable, key=_cycle_key, reverse=True)
    best = ordered[0]

    if len(ordered) == 1:
        return _from_candidate(best, via), "resolved"

    others = tuple(
        {
            "candidate_id": c.get("candidate_id"),
            "name": c.get("name"),
            "state": c.get("state"),
            "district": c.get("district"),
            "office": c.get("office"),
        }
        for c in ordered[1:]
    )
    resolution = _from_candidate(
        best,
        via,
        note=(
            f"{len(ordered)} candidates are linked to this committee; "
            "picked the most recent by election year."
        ),
    )
    return RaceResolution(**{**resolution.__dict__, "alternatives": others}), "resolved"


async def resolve_committee_race(
    client: Any,
    committee_id: str,
    committee_record: dict[str, Any] | None = None,
) -> RaceResolution:
    """Find the federal race a committee's candidate is running in.

    Tries each known route in turn and stops at the first that yields a
    candidate with a state. Every route is wrapped individually: one
    endpoint erroring must not prevent the others from being tried, since
    the whole point is that no single one is reliable.

    Args:
        client: An OpenFECClient (or anything with the same methods).
        committee_id: FEC committee ID, e.g. "C00832790".
        committee_record: An already-fetched committee record, if the
            caller has one, to avoid re-requesting it for route 3.
    """
    attempts: list[str] = []

    # Route 1: the committee -> candidates sub-resource. Scoped by URL path,
    # so it cannot return candidates belonging to other committees the way
    # a query-parameter filter silently can.
    try:
        data = await client.get_committee_candidates(committee_id)
        picked, reason = _pick(data.get("results") or [], "committee_candidates_endpoint")
        if picked:
            return picked
        attempts.append(f"/committee/{{id}}/candidates/ {reason}")
    except (OpenFECError, AttributeError, TypeError) as exc:
        attempts.append(f"/committee/{{id}}/candidates/ failed: {exc}")

    # Route 2: candidate_ids on the committee record, resolved one at a time
    # through /candidate/{id}/ -- also path-scoped. Observed empty on a real
    # principal campaign committee, which is why it isn't first, but it
    # costs nothing when the record is already in hand.
    try:
        record = committee_record
        if record is None:
            data = await client.get_committee(committee_id)
            results = data.get("results") or []
            record = results[0] if results else None

        candidate_ids = (record or {}).get("candidate_ids") or []
        for candidate_id in candidate_ids:
            detail = await client.get_candidate(candidate_id)
            picked, _ = _pick(detail.get("results") or [], "committee_record_candidate_ids")
            if picked:
                return picked
        if not candidate_ids:
            attempts.append("committee record has no candidate_ids")
        else:
            attempts.append("candidate_ids on the committee record resolved to no usable race")
    except (OpenFECError, AttributeError, TypeError, IndexError) as exc:
        attempts.append(f"committee record lookup failed: {exc}")

    return RaceResolution(
        resolved_via="unresolved",
        needs_confirmation=True,
        note=(
            "Could not determine which race this committee belongs to. "
            "Provide the state (and district, for a House seat) to get "
            "state-specific deadlines. Tried: " + "; ".join(attempts)
        ),
    )
