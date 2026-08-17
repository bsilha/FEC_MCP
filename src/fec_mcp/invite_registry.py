"""Remember which deadline invitations were sent, so later sends update
them instead of duplicating them.

Two jobs. First, SEQUENCE tracking: a calendar client only accepts a
revision whose SEQUENCE is strictly higher than the one it already holds,
so the previous value has to be known or every update is silently
discarded. Second, noticing when a previously-sent deadline stops
applying -- this tool never withdraws an event from anyone's calendar, so
a stale entry has to be reported to a person who can remove it. Without
a record of what was sent, there is nothing to compare against and a
stale deadline just quietly persists.

The store is a small JSON file keyed by committee. It is deliberately not
a database: the whole state is a few dozen UIDs per committee, and a
plain file can be inspected, diffed, and hand-corrected when something
goes wrong -- which matters more here than write throughput.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[2] / "data" / "sent_invites.json"


def _sequence_of(value: Any) -> int:
    """Read a stored SEQUENCE from either the current dict form or the
    bare integer earlier versions wrote."""
    if isinstance(value, dict):
        return int(value.get("sequence", 0))
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@dataclass
class InviteDiff:
    """What to send this time, relative to what was sent before.

    `no_longer_applies` names deadlines previously invited that this
    committee no longer owes. They are reported, never withdrawn -- this
    tool does not remove events from other people's calendars -- so
    somebody has to delete them by hand.
    """

    to_send: list[str] = field(default_factory=list)
    no_longer_applies: list[str] = field(default_factory=list)
    sequences: dict[str, int] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.to_send and not self.no_longer_applies


class InviteRegistry:
    """Tracks the UIDs and SEQUENCE numbers sent per committee."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else DEFAULT_REGISTRY_PATH
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text() or "{}")
        except (json.JSONDecodeError, OSError):
            # A corrupt registry must not block sending. Starting empty
            # means the next send re-issues everything as new invitations,
            # which is noisy but recoverable; refusing to send at all
            # because a cache file is malformed would not be.
            return {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True))

    def _raw_uids(self, committee_id: str) -> dict[str, Any]:
        return self._data.get(committee_id.upper(), {}).get("uids", {})

    def sent_uids(self, committee_id: str) -> dict[str, int]:
        """UID -> last SEQUENCE sent, for one committee."""
        return {uid: _sequence_of(value) for uid, value in self._raw_uids(committee_id).items()}

    def sent_events(self, committee_id: str) -> dict[str, dict[str, Any]]:
        """UID -> what was sent for it: sequence, date, and summary.

        Withdrawing an event should reproduce its original date and
        summary rather than inventing new ones. RFC 5545 matches a
        cancellation on UID, so a wrong DTSTART is not supposed to matter,
        but clients vary in how strictly they follow that and a
        cancellation that silently fails to match leaves the event sitting
        in someone's calendar -- the exact outcome this is meant to
        prevent.

        Tolerates the older format, where the stored value was a bare
        sequence integer, so an existing registry keeps working rather
        than orphaning every event recorded before this change.
        """
        events: dict[str, dict[str, Any]] = {}
        for uid, value in self._raw_uids(committee_id).items():
            if isinstance(value, dict):
                events[uid] = dict(value)
            else:
                events[uid] = {"sequence": value, "date": None, "summary": None}
        return events

    def recipients(self, committee_id: str) -> list[str]:
        return list(self._data.get(committee_id.upper(), {}).get("recipients", []))

    def plan(self, committee_id: str, current_uids: list[str]) -> InviteDiff:
        """Work out what to send and what to withdraw.

        Every current UID is (re)sent rather than only the new ones: a
        deadline can stay applicable while its date or wording changes, and
        an unchanged re-send is harmless to a calendar client, whereas a
        skipped update leaves the wrong date in place.

        SEQUENCE increments on every send. RFC 5545 requires a strictly
        higher SEQUENCE for a client to accept a revision, and a stale or
        equal one is silently ignored -- which looks exactly like the
        update having worked.
        """
        previous = self.sent_uids(committee_id)
        current = list(dict.fromkeys(current_uids))  # de-duplicate, keep order

        sequences = {uid: previous.get(uid, -1) + 1 for uid in current}
        stale = [uid for uid in previous if uid not in set(current)]

        return InviteDiff(to_send=current, no_longer_applies=stale, sequences=sequences)

    def record(
        self,
        committee_id: str,
        diff: InviteDiff,
        recipients: list[str] | None = None,
        details: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Commit a completed send. Call only after delivery succeeded --
        recording first would make a failed send look delivered, and the
        next run would then skip re-sending it.

        `details` carries each UID's date and summary so a later
        withdrawal can reproduce the original event rather than inventing
        one.
        """
        key = committee_id.upper()
        entry = self._data.setdefault(key, {"uids": {}, "recipients": []})
        details = details or {}

        for uid in diff.to_send:
            stored = {"sequence": diff.sequences.get(uid, 0)}
            stored.update({k: v for k, v in (details.get(uid) or {}).items() if v is not None})
            entry["uids"][uid] = stored

        # Deadlines that stopped applying stay on the record rather than
        # being forgotten. The event is still in the recipient's calendar
        # -- nothing was withdrawn -- so if it becomes applicable again its
        # SEQUENCE must continue from where it left off. Dropping it would
        # restart at 0, which a client holding a higher sequence ignores,
        # making the re-invitation look sent while changing nothing.

        if recipients is not None:
            entry["recipients"] = list(dict.fromkeys(recipients))

        self.save()

    def forget(self, committee_id: str) -> None:
        """Drop a committee's history, so the next send starts clean."""
        self._data.pop(committee_id.upper(), None)
        self.save()
