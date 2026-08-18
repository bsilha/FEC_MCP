"""The set of committees a user is responsible for, and where each one
sits in its cycle.

Compliance staff routinely carry several committees at once, and the
question they actually ask is "what is due next, and for whom" -- which
needs all of them in one place. What cannot be shared between them is
STATUS: each committee sits somewhere different in its own cycle, and
that alone decides its deadline set. So a roster entry is a committee
plus its own status and race, never just an ID.

Status is never defaulted. A committee added without one contributes no
deadlines at all until somebody says where it is, because a guessed
status produces a complete, confident, wrong schedule -- and unlike a
missing schedule, nothing about a wrong one looks wrong.

Persisted to a small JSON file for the same reason the sent-invite
registry is: the value here is not re-entering four committees and their
statuses every session, and a status set months ago needs to still be
there when a race finally resolves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

DEFAULT_ROSTER_PATH = Path(__file__).resolve().parents[2] / "data" / "committee_roster.json"

# The one value a status may hold that means "nobody has said yet".
UNSET_STATUS = ""


@dataclass(frozen=True)
class RosterEntry:
    """One committee a user is tracking."""

    committee_id: str
    name: str
    designation: str = ""
    filing_frequency: str = ""
    status: str = UNSET_STATUS
    state: str | None = None
    district: str | None = None
    status_set_on: str | None = None

    @property
    def has_status(self) -> bool:
        return bool(self.status)

    @property
    def is_candidate_committee(self) -> bool:
        return (self.designation or "").upper() in {"P", "A"}


class CommitteeRoster:
    """A user's committees, ordered as they were added."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else DEFAULT_ROSTER_PATH
        self._entries: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text() or "{}")
        except (json.JSONDecodeError, OSError):
            # A corrupt roster starts empty rather than blocking the view.
            # Losing the list is annoying and visible; refusing to render
            # because a cache file is malformed is neither.
            return {}
        return raw if isinstance(raw, dict) else {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._entries, indent=2, sort_keys=True))

    def entries(self) -> list[RosterEntry]:
        return [
            RosterEntry(
                committee_id=committee_id,
                name=data.get("name") or committee_id,
                designation=data.get("designation") or "",
                filing_frequency=data.get("filing_frequency") or "",
                status=data.get("status") or UNSET_STATUS,
                state=data.get("state") or None,
                district=data.get("district") or None,
                status_set_on=data.get("status_set_on"),
            )
            for committee_id, data in sorted(
                self._entries.items(), key=lambda kv: kv[1].get("added_at") or ""
            )
        ]

    def get(self, committee_id: str) -> RosterEntry | None:
        for entry in self.entries():
            if entry.committee_id == committee_id.upper():
                return entry
        return None

    def add(self, committee: dict[str, Any]) -> None:
        """Add a committee from an OpenFEC record. Status starts unset --
        deliberately, so nothing appears in the agenda until a person has
        said where this committee is."""
        committee_id = (committee.get("committee_id") or "").upper()
        if not committee_id or committee_id in self._entries:
            return

        self._entries[committee_id] = {
            "name": committee.get("name") or committee_id,
            "designation": committee.get("designation") or "",
            "filing_frequency": committee.get("filing_frequency") or "",
            # The committee's own state is a reasonable first guess at the
            # race's state and saves retyping, but it is only a guess: a
            # committee's mailing address need not be where the race is.
            "state": committee.get("state") or None,
            "district": None,
            "status": UNSET_STATUS,
            "status_set_on": None,
            # A full timestamp, not a date. Everything added in one sitting
            # shares a date, and the sort then falls through to dict order
            # -- which save() rewrites to alphabetical by committee ID, so
            # after a reload the roster silently reordered itself and rows
            # no longer matched the order they were entered in.
            "added_at": datetime.now().isoformat(timespec="microseconds"),
        }
        self.save()

    def update(
        self,
        committee_id: str,
        *,
        status: str | None = None,
        state: str | None = None,
        district: str | None = None,
    ) -> None:
        entry = self._entries.get(committee_id.upper())
        if entry is None:
            return

        if status is not None and status != entry.get("status"):
            entry["status"] = status
            # Stamped so a status set long ago is visibly old. A race
            # resolves and nobody updates the app; "still in the primary"
            # from eight months back should look questionable rather than
            # authoritative.
            entry["status_set_on"] = date.today().isoformat() if status else None
        if state is not None:
            entry["state"] = state or None
        if district is not None:
            entry["district"] = district or None
        self.save()

    def remove(self, committee_id: str) -> None:
        self._entries.pop(committee_id.upper(), None)
        self.save()

    def __len__(self) -> int:
        return len(self._entries)


def cycle_horizon_months(today: date | None = None) -> int:
    """Months to look ahead to cover the rest of the current election cycle.

    Federal cycles run two years and close with a year-end report due the
    following January 31, so the horizon runs to that report rather than a
    rolling twelve months -- a roster is for planning a cycle, and a
    window that quietly ends mid-cycle hides real filings.

    Never returns less than twelve. Late in a cycle the remaining months
    are few, and shortening the window then would drop deadlines the FEC
    has already published for the next one.
    """
    today = today or date.today()
    cycle_year = today.year if today.year % 2 == 0 else today.year + 1
    close = date(cycle_year + 1, 1, 31)
    months = (close.year - today.year) * 12 + (close.month - today.month) + 1
    return max(12, months)
