"""Coverage for iCalendar generation and the sent-invite registry.

Weighted toward the two properties the feature depends on -- UID
stability across sends, and cancellations actually being emitted -- since
both fail silently. A duplicated event looks like a scheduling quirk and
a missing cancellation looks like nothing at all, so neither shows up
without a test asserting it directly.
"""

from datetime import date

import pytest

from fec_mcp.calendar_invites import (
    InviteEvent,
    build_calendar,
    deadline_uid,
    events_from_deadlines,
)
from fec_mcp.invite_registry import InviteRegistry

PRE_GENERAL = {
    "date": "2026-10-22",
    "deadline": "12G Pre-General Report Due",
    "description": "The 12-day Pre-General Report due for all general election candidates.",
    "event_id": 8270,
    "certain": True,
    "reason": "nationwide pre_general deadline",
    "url": "https://www.fec.gov/help-candidates-and-committees/dates-and-deadlines/",
}

POST_GENERAL = {
    "date": "2026-12-03",
    "deadline": "30G Post-General Report Due",
    "description": "30G Post-General Report Due",
    "event_id": 8272,
    "certain": True,
}

QUARTERLY = {
    "date": "2026-10-15",
    "deadline": "October Quarterly Report Due",
    "description": "October Quarterly Report due today",
    "event_id": 8275,
    "certain": True,
}


def _lines(ics):
    """Unfold continuation lines before asserting on content."""
    out = []
    for line in ics.split("\r\n"):
        if line.startswith(" ") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


# -- UID stability ----------------------------------------------------------


def test_uid_is_stable_across_calls():
    """The only thing telling a client "you already have this event"."""
    assert deadline_uid("C00614701", PRE_GENERAL) == deadline_uid("C00614701", PRE_GENERAL)


def test_uid_differs_between_committees():
    assert deadline_uid("C00614701", PRE_GENERAL) != deadline_uid("C00401224", PRE_GENERAL)


def test_uid_differs_between_deadlines():
    assert deadline_uid("C00614701", PRE_GENERAL) != deadline_uid("C00614701", POST_GENERAL)


def test_uid_survives_the_fec_rewording_a_deadline():
    """Keyed on the FEC's event_id, so a reworded summary still updates
    the existing event rather than creating a second one."""
    reworded = dict(PRE_GENERAL, deadline="12-Day Pre-General Report", description="changed")
    assert deadline_uid("C00614701", reworded) == deadline_uid("C00614701", PRE_GENERAL)


def test_uid_falls_back_to_content_hash_without_an_event_id():
    no_id = {k: v for k, v in PRE_GENERAL.items() if k != "event_id"}
    assert deadline_uid("C1", no_id) == deadline_uid("C1", dict(no_id))
    assert deadline_uid("C1", no_id) != deadline_uid("C1", POST_GENERAL)


# -- iCalendar correctness --------------------------------------------------


@pytest.fixture
def sample_events():
    return events_from_deadlines("C00614701", "CRANE FOR CONGRESS", [PRE_GENERAL, QUARTERLY])


def test_calendar_uses_crlf_line_endings(sample_events):
    """Required by RFC 5545; Outlook rejects bare-LF calendars."""
    ics = build_calendar(sample_events, organizer_email="a@b.com", attendee_emails=["c@d.com"])
    assert "\r\n" in ics
    assert "\n" not in ics.replace("\r\n", "")


def test_calendar_declares_request_method_for_invitations(sample_events):
    ics = build_calendar(sample_events, organizer_email="a@b.com", attendee_emails=["c@d.com"])
    assert "METHOD:REQUEST" in _lines(ics)
    assert "STATUS:CONFIRMED" in _lines(ics)


def test_cancellation_declares_cancel_and_cancelled_status(sample_events):
    ics = build_calendar(
        sample_events, organizer_email="a@b.com", attendee_emails=["c@d.com"], method="CANCEL"
    )
    lines = _lines(ics)
    assert "METHOD:CANCEL" in lines
    assert "STATUS:CANCELLED" in lines


def test_cancellations_carry_no_alarm(sample_events):
    """A reminder for an event being withdrawn is pure noise."""
    ics = build_calendar(
        sample_events, organizer_email="a@b.com", attendee_emails=["c@d.com"], method="CANCEL"
    )
    assert "BEGIN:VALARM" not in ics


def test_every_attendee_gets_an_attendee_line(sample_events):
    ics = build_calendar(
        sample_events,
        organizer_email="organizer@example.com",
        attendee_emails=["a@example.com", "b@example.com", "c@example.com"],
    )
    for email in ("a@example.com", "b@example.com", "c@example.com"):
        assert f"mailto:{email}" in ics


def test_all_day_event_ends_the_following_day(sample_events):
    """RFC 5545 treats an all-day DTEND as exclusive -- same-day start and
    end is a zero-length event some clients drop entirely."""
    ics = build_calendar([sample_events[0]], organizer_email="a@b.com", attendee_emails=[])
    lines = _lines(ics)
    assert "DTSTART;VALUE=DATE:20261022" in lines
    assert "DTEND;VALUE=DATE:20261023" in lines


def test_long_lines_are_folded_within_the_octet_limit(sample_events):
    ics = build_calendar(
        sample_events, organizer_email="a@b.com", attendee_emails=["c@d.com"]
    )
    for line in ics.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, f"unfolded line: {line[:90]}"


def test_folded_lines_reassemble_to_the_original_text():
    long_description = "FEC deadline. " + "x" * 400
    event = InviteEvent(uid="u@fec-mcp", summary="S", description=long_description, on=date(2026, 10, 22))
    ics = build_calendar([event], organizer_email="a@b.com", attendee_emails=[])
    assert f"DESCRIPTION:{long_description}" in _lines(ics)


def test_special_characters_are_escaped():
    """Unescaped commas and semicolons terminate a property value early,
    truncating the description at the first one."""
    event = InviteEvent(
        uid="u@fec-mcp",
        summary="Report; due, today",
        description="Line one\nLine two; with, punctuation",
        on=date(2026, 10, 22),
    )
    ics = build_calendar([event], organizer_email="a@b.com", attendee_emails=[])
    assert "SUMMARY:Report\\; due\\, today" in _lines(ics)
    assert "\\n" in ics


def test_multibyte_characters_are_not_split_by_folding():
    event = InviteEvent(
        uid="u@fec-mcp", summary="é" * 100, description="d", on=date(2026, 10, 22)
    )
    ics = build_calendar([event], organizer_email="a@b.com", attendee_emails=[])
    assert "é" * 100 in "".join(_lines(ics))  # decodes without error


def test_unverified_deadlines_are_flagged_rather_than_dropped():
    """The recipient is the person who can resolve it; hiding it would
    recreate the missed-filing risk."""
    uncertain = dict(PRE_GENERAL, certain=False)
    events = events_from_deadlines("C1", "SOME PAC", [uncertain])
    assert events[0].summary.startswith("[CONFIRM]")
    assert "verify whether it applies" in events[0].description


def test_rows_without_a_usable_date_are_skipped_not_crashed():
    events = events_from_deadlines(
        "C1", "X", [{"deadline": "no date"}, dict(PRE_GENERAL, date="not-a-date"), QUARTERLY]
    )
    assert len(events) == 1


# -- the registry, and cancellation -----------------------------------------


@pytest.fixture
def registry(tmp_path):
    return InviteRegistry(path=tmp_path / "sent.json")


def test_first_send_has_nothing_to_cancel(registry):
    plan = registry.plan("C00614701", ["uid-a", "uid-b"])
    assert plan.to_send == ["uid-a", "uid-b"]
    assert plan.to_cancel == []
    assert all(plan.sequences[u] == 0 for u in plan.to_send)


def test_a_deadline_that_no_longer_applies_is_cancelled(registry):
    """The core of "deadlines update when a committee loses" -- without
    this the dropped events stay in every recipient's calendar."""
    first = registry.plan("C00614701", ["quarterly", "pre-general", "post-general"])
    registry.record("C00614701", first)

    second = registry.plan("C00614701", ["quarterly"])
    assert set(second.to_cancel) == {"pre-general", "post-general"}
    assert second.to_send == ["quarterly"]


def test_sequence_increments_on_every_send(registry):
    """RFC 5545 ignores a revision whose SEQUENCE is not strictly higher --
    which looks exactly like the update having worked."""
    first = registry.plan("C1", ["uid-a"])
    registry.record("C1", first)
    second = registry.plan("C1", ["uid-a"])
    assert second.sequences["uid-a"] == first.sequences["uid-a"] + 1


def test_cancellation_also_gets_a_higher_sequence(registry):
    first = registry.plan("C1", ["uid-a"])
    registry.record("C1", first)
    second = registry.plan("C1", [])
    assert second.to_cancel == ["uid-a"]
    assert second.sequences["uid-a"] > first.sequences["uid-a"]


def test_cancelled_uids_are_forgotten_so_they_are_not_cancelled_twice(registry):
    registry.record("C1", registry.plan("C1", ["uid-a"]))
    registry.record("C1", registry.plan("C1", []))
    assert registry.plan("C1", []).to_cancel == []


def test_registry_persists_across_instances(tmp_path):
    path = tmp_path / "sent.json"
    first = InviteRegistry(path=path)
    first.record("C1", first.plan("C1", ["uid-a"]), recipients=["x@example.com"])

    reopened = InviteRegistry(path=path)
    assert reopened.sent_uids("C1") == {"uid-a": 0}
    assert reopened.recipients("C1") == ["x@example.com"]


def test_committee_ids_are_matched_case_insensitively(registry):
    registry.record("c00614701", registry.plan("c00614701", ["uid-a"]))
    assert registry.sent_uids("C00614701") == {"uid-a": 0}


def test_a_corrupt_registry_does_not_block_sending(tmp_path):
    """Re-issuing invitations is recoverable; refusing to send because a
    cache file is malformed is not."""
    path = tmp_path / "sent.json"
    path.write_text("{ this is not json")
    assert InviteRegistry(path=path).sent_uids("C1") == {}


def test_committees_do_not_interfere_with_each_other(registry):
    registry.record("C1", registry.plan("C1", ["uid-a"]))
    registry.record("C2", registry.plan("C2", ["uid-b"]))
    assert registry.plan("C1", ["uid-a"]).to_cancel == []
    assert registry.sent_uids("C2") == {"uid-b": 0}


def test_forget_clears_a_committees_history(registry):
    registry.record("C1", registry.plan("C1", ["uid-a"]))
    registry.forget("C1")
    assert registry.sent_uids("C1") == {}


def test_plan_deduplicates_repeated_uids(registry):
    plan = registry.plan("C1", ["uid-a", "uid-a", "uid-b"])
    assert plan.to_send == ["uid-a", "uid-b"]


def test_registry_remembers_each_events_date_and_summary(registry):
    """So a withdrawal can reproduce the original event rather than
    inventing a date for it."""
    plan = registry.plan("C1", ["uid-a"])
    registry.record(
        "C1", plan, details={"uid-a": {"date": "2026-10-22", "summary": "12G Pre-General"}}
    )
    stored = registry.sent_events("C1")["uid-a"]

    assert stored["date"] == "2026-10-22"
    assert stored["summary"] == "12G Pre-General"
    assert stored["sequence"] == 0


def test_a_registry_written_before_dates_were_stored_still_works(tmp_path):
    """Older entries stored a bare sequence integer. Those must keep
    working -- failing to read them would orphan every event already in
    someone's calendar, with no way left to withdraw it."""
    path = tmp_path / "sent.json"
    path.write_text('{"C1": {"uids": {"legacy-uid": 3}, "recipients": []}}')
    registry = InviteRegistry(path=path)

    assert registry.sent_uids("C1") == {"legacy-uid": 3}
    assert registry.sent_events("C1")["legacy-uid"] == {
        "sequence": 3,
        "date": None,
        "summary": None,
    }
    # ...and it can still be cancelled, with a properly bumped sequence.
    plan = registry.plan("C1", [])
    assert plan.to_cancel == ["legacy-uid"]
    assert plan.sequences["legacy-uid"] == 4
