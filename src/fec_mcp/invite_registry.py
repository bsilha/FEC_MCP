"""Remember which deadline invitations were sent, so they can be updated
or withdrawn later.

Without this there is no way to withdraw anything. A calendar client only
removes an event when it receives a CANCEL naming that event's UID, so
sending a smaller set after a committee loses its primary leaves the
general-election deadlines sitting in every recipient's calendar
indefinitely. Knowing what was previously sent is what makes the
difference between "the deadlines updated" and "some new deadlines
arrived alongside the stale ones".

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


@dataclass
class InviteDiff:
    """What to send this time, relative to what was sent before."""

    to_send: list[str] = field(default_factory=list)
    to_cancel: list[str] = field(default_factory=list)
    sequences: dict[str, int] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.to_send and not self.to_cancel


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

    def sent_uids(self, committee_id: str) -> dict[str, int]:
        """UID -> last SEQUENCE sent, for one committee."""
        return dict(self._data.get(committee_id.upper(), {}).get("uids", {}))

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
        to_cancel = [uid for uid in previous if uid not in set(current)]
        # A cancellation is itself a revision and needs its own bump.
        for uid in to_cancel:
            sequences[uid] = previous[uid] + 1

        return InviteDiff(to_send=current, to_cancel=to_cancel, sequences=sequences)

    def record(
        self,
        committee_id: str,
        diff: InviteDiff,
        recipients: list[str] | None = None,
    ) -> None:
        """Commit a completed send. Call only after delivery succeeded --
        recording first would make a failed send look delivered, and the
        next run would then skip re-sending it."""
        key = committee_id.upper()
        entry = self._data.setdefault(key, {"uids": {}, "recipients": []})

        for uid in diff.to_send:
            entry["uids"][uid] = diff.sequences.get(uid, 0)
        for uid in diff.to_cancel:
            entry["uids"].pop(uid, None)

        if recipients is not None:
            entry["recipients"] = list(dict.fromkeys(recipients))

        self.save()

    def forget(self, committee_id: str) -> None:
        """Drop a committee's history, so the next send starts clean."""
        self._data.pop(committee_id.upper(), None)
        self.save()
