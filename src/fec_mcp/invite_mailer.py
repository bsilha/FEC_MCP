"""Deliver deadline invitations by email.

Kept separate from calendar_invites.py so that generating a calendar --
the part with all the correctness risk -- stays pure and testable, and
this module holds only the parts that need credentials and a network.

A calendar invitation is carried as a text/calendar MIME part with an
explicit `method` parameter, not as an attached file. That parameter is
what makes Gmail and Outlook render the message as an invitation with
Accept/Decline rather than showing an unexplained .ics file.

Deliberately NOT also attached as a file. This module used to add a
second copy as an attachment, on the reasoning that a client ignoring
the inline part would at least hand the recipient something openable.
The cost of that generosity was the feature itself: attaching anything
wraps the message in multipart/mixed, and Gmail, handed a .ics file with
a filename beside the invitation, shows the file. Which is exactly what
recipients reported seeing -- an attachment to download and import by
hand, in a feature whose whole purpose is that nobody has to.

So the structure below is the narrow one every calendar client agrees
on: multipart/alternative, text/plain first, text/calendar second, and
nothing else in the message.
"""

from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage


class InviteMailerError(RuntimeError):
    """Raised when email cannot be configured or delivered."""


@dataclass(frozen=True)
class SMTPSettings:
    host: str
    port: int
    username: str | None
    password: str | None
    from_address: str
    use_tls: bool = True

    @classmethod
    def from_env(cls) -> "SMTPSettings":
        """Read SMTP configuration from the environment.

        Raises rather than falling back to a default server: quietly
        guessing where to send mail on someone's behalf is not a failure
        mode worth having.
        """
        host = os.environ.get("FEC_SMTP_HOST")
        from_address = os.environ.get("FEC_SMTP_FROM")
        missing = [
            name
            for name, value in (("FEC_SMTP_HOST", host), ("FEC_SMTP_FROM", from_address))
            if not value
        ]
        if missing:
            raise InviteMailerError(
                "Email is not configured. Set " + ", ".join(missing) + " (and normally "
                "FEC_SMTP_USER / FEC_SMTP_PASSWORD, plus FEC_SMTP_PORT if not 587)."
            )

        return cls(
            host=host,
            port=int(os.environ.get("FEC_SMTP_PORT", "587")),
            username=os.environ.get("FEC_SMTP_USER"),
            password=os.environ.get("FEC_SMTP_PASSWORD"),
            from_address=from_address,
            use_tls=os.environ.get("FEC_SMTP_TLS", "1") != "0",
        )


def build_message(
    *,
    settings: SMTPSettings,
    recipients: list[str],
    subject: str,
    body: str,
    ics: str,
) -> EmailMessage:
    """Assemble an invitation email carrying an iCalendar payload.

    Produces exactly:

        multipart/alternative
          text/plain
          text/calendar; method=REQUEST; charset=UTF-8

    Order matters -- text/calendar last, as the richest alternative --
    and so does what is absent. Adding any attachment, including a copy
    of this same calendar, wraps the whole thing in multipart/mixed and
    Gmail then renders the file instead of the invitation.

    Always METHOD=REQUEST, matching the calendar body. This tool only
    invites; it never withdraws an event from a recipient's calendar.
    """
    method = "REQUEST"
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.from_address
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    # This `method` must match the METHOD inside the calendar body -- a
    # mismatch is another way a client falls back to showing a bare .ics
    # file instead of an invitation.
    message.add_alternative(ics, subtype="calendar", params={"method": method, "charset": "UTF-8"})
    return message


def send_message(message: EmailMessage, settings: SMTPSettings) -> None:
    """Deliver one message over SMTP."""
    try:
        with smtplib.SMTP(settings.host, settings.port, timeout=30) as smtp:
            if settings.use_tls:
                smtp.starttls()
            if settings.username and settings.password:
                smtp.login(settings.username, settings.password)
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise InviteMailerError(
            f"Could not send invitations via {settings.host}:{settings.port} -- "
            f"{type(exc).__name__}: {exc}"
        ) from exc
