"""Coverage for the send_deadline_invites tool.

The assertions that matter most are about what does NOT happen: no email
without explicit opt-in, and nothing recorded as delivered when delivery
failed. Both are silent when wrong -- an accidental send cannot be
recalled, and a false delivery record makes the next run skip re-sending,
turning one failure into a permanently missing calendar entry.
"""

import pytest

from fec_mcp import server
from fec_mcp.invite_mailer import InviteMailerError, SMTPSettings
from fec_mcp.invite_registry import InviteRegistry
from tests.test_committee_deadlines_tool import CALENDAR, CANDIDATE_COMMITTEE, StubClient

RECIPIENTS = ["treasurer@example.com", "counsel@example.com"]


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Stubs OpenFEC, points the registry at a temp file, and captures
    anything that would be emailed instead of sending it."""
    sent: list[dict] = []

    def _install(committee=CANDIDATE_COMMITTEE, calendar=CALENDAR, send_error=None):
        client = StubClient(committee, calendar=calendar)

        async def fake_client():
            return client

        monkeypatch.setattr(server, "_client", fake_client)
        monkeypatch.setattr(
            server, "InviteRegistry", lambda: InviteRegistry(path=tmp_path / "sent.json")
        )
        monkeypatch.setattr(
            server.SMTPSettings,
            "from_env",
            classmethod(
                lambda cls: SMTPSettings(
                    host="smtp.example.com", port=587, username="u",
                    password="p", from_address="calendar@example.com",
                )
            ),
        )

        def fake_send(message, settings):
            if send_error:
                raise InviteMailerError(send_error)
            sent.append(
                {
                    "subject": message["Subject"],
                    "to": message["To"],
                    "body": message.as_string(),
                }
            )

        monkeypatch.setattr(server, "send_message", fake_send)
        return sent

    return _install


async def test_preview_does_not_send_anything(wired):
    """`send` defaults to False -- email is outward-facing and cannot be
    recalled, so it must never happen without an explicit opt-in."""
    sent = wired()
    result = await server.send_deadline_invites(
        "C00614701", status="won_primary", recipients=RECIPIENTS, state="MI", district="04"
    )

    assert sent == []
    assert result["sent"] is False
    assert result["would_invite"]
    assert "confirmation" in result["note"]


async def test_preview_lists_what_would_be_sent_and_to_whom(wired):
    wired()
    result = await server.send_deadline_invites(
        "C00614701", status="won_primary", recipients=RECIPIENTS, state="MI", district="04"
    )

    assert result["recipients"] == RECIPIENTS
    summaries = [row["summary"] for row in result["would_invite"]]
    assert any("12G Pre-General" in s for s in summaries)


async def test_sending_requires_at_least_one_recipient(wired):
    wired()
    result = await server.send_deadline_invites("C00614701", status="ongoing", recipients=[])
    assert "error" in result


async def test_send_true_delivers_invitations(wired):
    sent = wired()
    result = await server.send_deadline_invites(
        "C00614701", status="won_primary", recipients=RECIPIENTS,
        state="MI", district="04", send=True,
    )

    assert result["sent"] is True
    assert len(sent) == 1
    assert "METHOD:REQUEST" in sent[0]["body"]
    assert "CRANE FOR CONGRESS" in sent[0]["subject"]


async def test_a_status_change_reports_the_deadlines_no_longer_owed(wired):
    """Losing the primary drops the general-election reports from the
    invitation set. Nothing is withdrawn -- this tool never removes an
    event from someone's calendar -- so they must be named clearly enough
    for a person to delete them."""
    sent = wired()
    await server.send_deadline_invites(
        "C00614701", status="won_primary", recipients=RECIPIENTS,
        state="MI", district="04", send=True,
    )
    sent.clear()

    result = await server.send_deadline_invites(
        "C00614701", status="lost_primary", recipients=RECIPIENTS,
        state="MI", district="04", send=True,
    )

    stale = result["no_longer_applies_remove_manually"]
    assert len(stale) == 2
    summaries = " ".join(row["summary"] for row in stale)
    assert "12G Pre-General" in summaries
    assert "30G Post-General" in summaries


async def test_stale_deadlines_are_reported_with_the_date_they_were_sent_under(wired):
    """A UID is useless for finding an event by hand; the date and title
    are what someone actually searches their calendar for."""
    sent = wired()
    await server.send_deadline_invites(
        "C00614701", status="won_primary", recipients=RECIPIENTS,
        state="MI", district="04", send=True,
    )
    result = await server.send_deadline_invites(
        "C00614701", status="lost_primary", recipients=RECIPIENTS,
        state="MI", district="04", send=True,
    )

    dates = {row["date"] for row in result["no_longer_applies_remove_manually"]}
    assert dates == {"2026-10-22", "2026-12-03"}


async def test_nothing_is_ever_cancelled(wired):
    """The hard requirement: no message this tool sends may withdraw an
    event from a recipient's calendar."""
    sent = wired()
    await server.send_deadline_invites(
        "C00614701", status="won_primary", recipients=RECIPIENTS,
        state="MI", district="04", send=True,
    )
    await server.send_deadline_invites(
        "C00614701", status="lost_primary", recipients=RECIPIENTS,
        state="MI", district="04", send=True,
    )

    for message in sent:
        assert "METHOD:CANCEL" not in message["body"]
        assert "STATUS:CANCELLED" not in message["body"]


async def test_the_email_body_spells_out_what_to_delete(wired):
    """The recipient is the one holding the stale calendar entry, so the
    instruction has to reach them, not just the tool's caller."""
    sent = wired()
    await server.send_deadline_invites(
        "C00614701", status="won_primary", recipients=RECIPIENTS,
        state="MI", district="04", send=True,
    )
    sent.clear()
    await server.send_deadline_invites(
        "C00614701", status="lost_primary", recipients=RECIPIENTS,
        state="MI", district="04", send=True,
    )

    body = sent[0]["body"]
    assert "no longer apply" in body
    assert "delete them" in body.lower()


async def test_resending_the_same_status_updates_rather_than_duplicates(wired):
    """Same UIDs with a higher SEQUENCE -- a client treats that as a
    revision of the event it already has."""
    sent = wired()
    first = await server.send_deadline_invites(
        "C00614701", status="won_primary", recipients=RECIPIENTS,
        state="MI", district="04", send=True,
    )
    second = await server.send_deadline_invites(
        "C00614701", status="won_primary", recipients=RECIPIENTS,
        state="MI", district="04", send=True,
    )

    assert second["no_longer_applies_remove_manually"] == []
    first_seqs = {r["summary"]: r["sequence"] for r in first["would_invite"]}
    for row in second["would_invite"]:
        assert row["sequence"] == first_seqs[row["summary"]] + 1


async def test_a_failed_send_is_not_recorded_as_delivered(wired, tmp_path):
    """Recording a failed send would make the next run skip it, turning
    one failure into a permanently missing calendar entry."""
    wired(send_error="smtp is down")
    result = await server.send_deadline_invites(
        "C00614701", status="won_primary", recipients=RECIPIENTS,
        state="MI", district="04", send=True,
    )

    assert result["sent"] is False
    assert "smtp is down" in result["error"]
    assert InviteRegistry(path=tmp_path / "sent.json").sent_uids("C00614701") == {}


async def test_unconfigured_email_reports_clearly_and_sends_nothing(wired, monkeypatch):
    sent = wired()

    def unconfigured(cls):
        raise InviteMailerError("Email is not configured. Set FEC_SMTP_HOST")

    monkeypatch.setattr(server.SMTPSettings, "from_env", classmethod(unconfigured))
    result = await server.send_deadline_invites(
        "C00614701", status="won_primary", recipients=RECIPIENTS, state="MI", send=True,
    )

    assert sent == []
    assert result["sent"] is False
    assert "not configured" in result["error"]


async def test_a_committee_lookup_failure_is_surfaced_not_emailed(wired):
    sent = wired()
    result = await server.send_deadline_invites(
        "C00614701", status="not_a_status", recipients=RECIPIENTS, send=True
    )

    assert sent == []
    assert "error" in result


async def test_every_recipient_appears_on_the_invitation(wired):
    sent = wired()
    await server.send_deadline_invites(
        "C00614701", status="won_primary", recipients=RECIPIENTS,
        state="MI", district="04", send=True,
    )

    for address in RECIPIENTS:
        assert address in sent[0]["body"]
