"""Build iCalendar (RFC 5545) invitations for FEC filing deadlines.

Pure text generation -- no network, no files -- so the parts that are
easy to get subtly wrong (escaping, folding, UID stability, cancellation
semantics) are testable in isolation.

Two things here carry the whole feature:

UID stability. A deadline's UID must be identical every time invitations
are sent for it, because that is the only thing telling a calendar client
"this is the event you already have" rather than "this is a new event".
Get it wrong and re-sending after a status change silently duplicates
every deadline instead of updating it.

Cancellation. Sending a smaller set of events does NOT remove the ones
left out -- they simply stay in the recipient's calendar. So when a
committee loses its primary, the pre-general and post-general it no
longer owes must be explicitly withdrawn with METHOD:CANCEL, or the
calendar keeps showing filings that are not due. That is the same
wrong-answer failure this whole feature exists to prevent, just relocated
into someone's Outlook.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable

# RFC 5545 caps a content line at 75 octets, continuing with a leading
# space. Clients do reject over-long lines, and a deadline description
# citing an FEC URL exceeds this routinely.
_MAX_LINE_OCTETS = 75

PRODID = "-//fec-mcp//FEC filing deadlines//EN"

# Namespace suffix for UIDs. Not a real host; RFC 5545 only requires the
# UID be globally unique, and an @-suffixed string is the convention.
_UID_DOMAIN = "fec-mcp"


@dataclass(frozen=True)
class InviteEvent:
    """One deadline, ready to be written as a VEVENT."""

    uid: str
    summary: str
    description: str
    on: date
    sequence: int = 0
    url: str | None = None
    certain: bool = True


def deadline_uid(committee_id: str, deadline: dict[str, Any]) -> str:
    """A stable, unique UID for one committee's one deadline.

    Keyed on the FEC's own event_id where present, since that is stable
    across calendar refreshes in a way the summary text is not -- the FEC
    rewords descriptions between cycles. Falls back to a hash of date and
    summary, which is stable enough to update correctly as long as neither
    changes, and a reworded deadline producing one duplicate is far better
    than a shared UID silently collapsing two distinct deadlines into one.
    """
    event_id = deadline.get("event_id")
    if event_id:
        key = str(event_id)
    else:
        raw = f"{deadline.get('date')}|{deadline.get('deadline')}"
        key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"fec-{committee_id.lower()}-{key}@{_UID_DOMAIN}"


def _escape(text: str) -> str:
    """Escape per RFC 5545 section 3.3.11.

    Backslash first -- escaping it after the others would double-escape
    the backslashes they introduce.
    """
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold(line: str) -> list[str]:
    """Split one content line into RFC-5545-length chunks.

    Counts octets rather than characters: the limit is defined in octets,
    and a multi-byte character split across the boundary would corrupt it.
    """
    raw = line.encode("utf-8")
    if len(raw) <= _MAX_LINE_OCTETS:
        return [line]

    chunks: list[str] = []
    current = bytearray()
    limit = _MAX_LINE_OCTETS
    for char in line:
        encoded = char.encode("utf-8")
        if len(current) + len(encoded) > limit:
            chunks.append(current.decode("utf-8"))
            current = bytearray()
            # Continuation lines carry a leading space, which counts.
            limit = _MAX_LINE_OCTETS - 1
        current.extend(encoded)
    if current:
        chunks.append(current.decode("utf-8"))

    return [chunks[0]] + [f" {c}" for c in chunks[1:]]


def _stamp(moment: datetime | None = None) -> str:
    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _date(value: date) -> str:
    return value.strftime("%Y%m%d")


def build_calendar(
    events: Iterable[InviteEvent],
    *,
    organizer_email: str,
    attendee_emails: Iterable[str],
    method: str = "REQUEST",
    organizer_name: str = "FEC Compliance Calendar",
    now: datetime | None = None,
) -> str:
    """Serialize events as a complete iCalendar document.

    Args:
        method: "REQUEST" to create or update invitations, "CANCEL" to
            withdraw them. A CANCEL must repeat the original UID with a
            higher SEQUENCE; anything else leaves the event in place.

    Deadlines are all-day events: they name a day something is due, not a
    meeting. DTEND is the following day because RFC 5545 treats the end of
    an all-day event as exclusive -- setting it to the same day produces a
    zero-length event that some clients drop entirely.
    """
    cancelling = method.upper() == "CANCEL"
    stamp = _stamp(now)

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        f"METHOD:{method.upper()}",
    ]

    for event in events:
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{event.uid}",
                f"DTSTAMP:{stamp}",
                f"DTSTART;VALUE=DATE:{_date(event.on)}",
                f"DTEND;VALUE=DATE:{_date(_next_day(event.on))}",
                f"SUMMARY:{_escape(event.summary)}",
                f"DESCRIPTION:{_escape(event.description)}",
                f"SEQUENCE:{event.sequence}",
                f"STATUS:{'CANCELLED' if cancelling else 'CONFIRMED'}",
                "TRANSP:TRANSPARENT",
                f"ORGANIZER;CN={_escape(organizer_name)}:mailto:{organizer_email}",
            ]
        )
        if event.url:
            lines.append(f"URL:{event.url}")

        for email in attendee_emails:
            lines.append(
                "ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;"
                f"PARTSTAT=NEEDS-ACTION;RSVP=FALSE:mailto:{email}"
            )

        # A filing deadline is worth more than a same-morning ping. Only on
        # live invitations -- a reminder on a cancellation is noise.
        if not cancelling:
            lines.extend(
                [
                    "BEGIN:VALARM",
                    "TRIGGER:-P3D",
                    "ACTION:DISPLAY",
                    f"DESCRIPTION:{_escape('Due in 3 days: ' + event.summary)}",
                    "END:VALARM",
                ]
            )

        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")

    folded: list[str] = []
    for line in lines:
        folded.extend(_fold(line))
    # CRLF is required by RFC 5545, and Outlook in particular rejects
    # bare-LF calendars.
    return "\r\n".join(folded) + "\r\n"


def _next_day(value: date) -> date:
    from datetime import timedelta

    return value + timedelta(days=1)


def events_from_deadlines(
    committee_id: str,
    committee_name: str,
    deadlines: Iterable[dict[str, Any]],
    sequences: dict[str, int] | None = None,
) -> list[InviteEvent]:
    """Turn get_committee_deadlines() rows into invitation events.

    An unverified deadline is marked in the summary rather than dropped or
    silently presented as settled -- the recipient is the person who can
    actually resolve it, and hiding it would recreate the missed-filing
    risk the tool works to avoid.
    """
    sequences = sequences or {}
    events: list[InviteEvent] = []

    for row in deadlines:
        raw_date = row.get("date")
        if not raw_date:
            continue
        try:
            on = date.fromisoformat(str(raw_date))
        except ValueError:
            continue

        uid = deadline_uid(committee_id, row)
        certain = bool(row.get("certain", True))
        prefix = "" if certain else "[CONFIRM] "
        summary = f"{prefix}{committee_name}: {row.get('deadline') or 'FEC filing deadline'}"

        description_parts = [row.get("description") or ""]
        if not certain:
            description_parts.append(
                "This deadline could not be confirmed automatically -- it is "
                "included so it is not missed, but verify whether it applies."
            )
        if row.get("reason"):
            description_parts.append(f"Why it applies: {row['reason']}")
        if row.get("url"):
            description_parts.append(row["url"])

        events.append(
            InviteEvent(
                uid=uid,
                summary=summary,
                description="\n\n".join(p for p in description_parts if p),
                on=on,
                sequence=sequences.get(uid, 0),
                url=row.get("url"),
                certain=certain,
            )
        )

    return events
