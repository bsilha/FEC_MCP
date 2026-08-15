"""Committee-specific reporting-deadline logic.

The FEC publishes the authoritative filing calendar itself (OpenFEC's
/calendar-dates/ endpoint), so this module deliberately does NOT compute
deadline dates from first principles -- re-deriving "Q3 is due October 15"
or "pre-primary is 12 days out" in Python would mean maintaining a second,
unofficial copy of the filing rules, and any drift between it and the
FEC's own calendar would show up as a wrong date in a compliance tool.
That is the worst failure mode this feature has.

Instead this module answers the question the published calendar cannot:
*which* of those published deadlines actually bind a particular committee.
That depends on three things the calendar doesn't know about the
committee --

1. whether it is a candidate's authorized committee or a non-candidate
   committee (PAC/party), which decides whether election-driven reports
   are even in play;
2. whether it files quarterly or monthly, which decides which family of
   regular reports applies; and
3. where the committee is in its election lifecycle -- a candidate who
   lost their primary has no pre-general or post-general report, while
   one who won has both.

(3) is the part that changes over time, so it is modeled here as an
explicit state machine rather than inferred: a committee's status is set
by the user and is authoritative. Inferring it from election results was
considered and rejected for now -- OpenFEC's results data lags real-world
outcomes, and a silently stale "still in the primary" would generate
confidently wrong deadlines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .states import state_code


class CommitteeStatus(str, Enum):
    """Where a committee sits in its election lifecycle.

    Values are the strings stored/round-tripped through tool arguments and
    the demo UI, so they are part of this module's public contract -- don't
    rename one without migrating any persisted status.
    """

    IN_PRIMARY = "in_primary"
    WON_PRIMARY = "won_primary"
    LOST_PRIMARY = "lost_primary"
    WON_GENERAL = "won_general"
    LOST_GENERAL = "lost_general"
    TERMINATING = "terminating"
    # Non-candidate committees (PACs, party committees) have no primary or
    # general of their own to win or lose; they simply keep filing.
    ONGOING = "ongoing"


class ReportFamily(str, Enum):
    """A family of filing deadlines, not an individual due date.

    The concrete dates come from the FEC's published calendar; these are
    the buckets used to decide which of those dates are relevant.
    """

    # Quarterly or monthly reports, whichever the committee files. These
    # continue for as long as the committee exists -- including after a
    # loss, until a termination report is actually filed.
    REGULAR = "regular"
    PRE_PRIMARY = "pre_primary"
    PRE_GENERAL = "pre_general"
    POST_GENERAL = "post_general"


# Which report families are in play for each status.
#
# REGULAR appears in every entry on purpose: losing an election ends a
# committee's election-driven reports but not its obligation to keep
# filing regular reports. A committee that lost its primary still files
# until its debts are settled and it files a termination report, which is
# exactly the obligation people assume goes away and it doesn't.
_FAMILIES_BY_STATUS: dict[CommitteeStatus, frozenset[ReportFamily]] = {
    # Primary hasn't happened yet, so the general is still hypothetical --
    # don't surface pre/post-general deadlines the committee may never owe.
    CommitteeStatus.IN_PRIMARY: frozenset({ReportFamily.REGULAR, ReportFamily.PRE_PRIMARY}),
    # Advanced to the general: pre-primary is behind them, pre/post-general
    # now apply.
    CommitteeStatus.WON_PRIMARY: frozenset(
        {ReportFamily.REGULAR, ReportFamily.PRE_GENERAL, ReportFamily.POST_GENERAL}
    ),
    # Eliminated -- no general-election reports at all, but regular
    # reporting continues.
    CommitteeStatus.LOST_PRIMARY: frozenset({ReportFamily.REGULAR}),
    # Won the seat. The post-general report is still owed, and regular
    # reporting continues into the next cycle as the officeholder's
    # committee.
    CommitteeStatus.WON_GENERAL: frozenset({ReportFamily.REGULAR, ReportFamily.POST_GENERAL}),
    # Lost the general: the post-general report is still owed, and regular
    # reporting continues until termination.
    CommitteeStatus.LOST_GENERAL: frozenset({ReportFamily.REGULAR, ReportFamily.POST_GENERAL}),
    # Winding down -- regular reports only, until the termination report.
    CommitteeStatus.TERMINATING: frozenset({ReportFamily.REGULAR}),
    # Non-candidate committees file regular reports continuously. They can
    # also owe pre/post-election reports, but that is driven by whether
    # they actually made contributions or expenditures in connection with a
    # given election -- committee activity, not lifecycle status -- so it
    # is not derivable from status alone and is handled separately.
    CommitteeStatus.ONGOING: frozenset({ReportFamily.REGULAR}),
}


# Which statuses a committee can legitimately move to from its current one.
#
# WON_GENERAL -> IN_PRIMARY is intentional and not a typo: a sitting
# officeholder who runs again re-enters the cycle at the primary, reusing
# the same committee.
_ALLOWED_TRANSITIONS: dict[CommitteeStatus, frozenset[CommitteeStatus]] = {
    CommitteeStatus.IN_PRIMARY: frozenset(
        {CommitteeStatus.WON_PRIMARY, CommitteeStatus.LOST_PRIMARY, CommitteeStatus.TERMINATING}
    ),
    CommitteeStatus.WON_PRIMARY: frozenset(
        {CommitteeStatus.WON_GENERAL, CommitteeStatus.LOST_GENERAL, CommitteeStatus.TERMINATING}
    ),
    CommitteeStatus.LOST_PRIMARY: frozenset({CommitteeStatus.TERMINATING}),
    CommitteeStatus.WON_GENERAL: frozenset(
        {CommitteeStatus.IN_PRIMARY, CommitteeStatus.TERMINATING}
    ),
    CommitteeStatus.LOST_GENERAL: frozenset({CommitteeStatus.TERMINATING}),
    # Terminal: a committee that has filed its termination report is done.
    CommitteeStatus.TERMINATING: frozenset(),
    # A non-candidate committee never enters the candidate lifecycle.
    CommitteeStatus.ONGOING: frozenset({CommitteeStatus.TERMINATING}),
}


def report_families_for(status: CommitteeStatus) -> frozenset[ReportFamily]:
    """Which families of deadline apply to a committee in this status."""
    return _FAMILIES_BY_STATUS[status]


def allowed_transitions(status: CommitteeStatus) -> frozenset[CommitteeStatus]:
    """Statuses this committee could legitimately move to next.

    Used to constrain the status picker in the UI, so a committee can't be
    marked "won the general" without having won a primary first.
    """
    return _ALLOWED_TRANSITIONS[status]


def can_transition(current: CommitteeStatus, new: CommitteeStatus) -> bool:
    """Whether `current -> new` is a legitimate lifecycle move.

    Re-selecting the current status is always allowed (a no-op save from
    the UI shouldn't be rejected).
    """
    return new == current or new in _ALLOWED_TRANSITIONS[current]


def is_election_driven(status: CommitteeStatus) -> bool:
    """Whether this status implies any election-tied reports at all.

    False for committees that only owe regular reports -- useful for
    deciding whether to bother resolving the committee's elections.
    """
    return bool(report_families_for(status) - {ReportFamily.REGULAR})


# ---------------------------------------------------------------------------
# Matching published FEC calendar records to a specific committee
# ---------------------------------------------------------------------------

# OpenFEC calendar_category_id values (see CALENDAR_CATEGORIES in
# openfec_client.py, which maps the friendly names callers pass).
CATEGORY_QUARTERLY = 25
CATEGORY_MONTHLY = 26
CATEGORY_PRE_POST_ELECTION = 27

# committee.filing_frequency values seen in real OpenFEC records. Others
# exist (terminated/waived/administratively-closed committees carry their
# own codes), which is why unknown values are handled explicitly rather
# than assumed to mean quarterly.
FREQUENCY_QUARTERLY = "Q"
FREQUENCY_MONTHLY = "M"

# committee.designation values that mean "this is a candidate's own
# committee", and therefore that the candidate's elections drive its
# pre/post-election reports.
_AUTHORIZED_DESIGNATIONS = frozenset({"P", "A"})

# A calendar summary leads with the race it belongs to, e.g.
# "TN/07 Special Post-General Report Due" or "TX/18 Special General
# Election Runoff". Senate/statewide rows have no district segment.
_RACE_PREFIX = re.compile(r"^([A-Z]{2})(?:/(\d{1,2}|S|SEN|AL))?\b")

# Which report family a pre/post-election row refers to. Ordered longest-
# first so "post-general" is tested before any looser "general" match.
_FAMILY_KEYWORDS: tuple[tuple[str, ReportFamily], ...] = (
    ("post-general", ReportFamily.POST_GENERAL),
    ("post general", ReportFamily.POST_GENERAL),
    ("pre-general", ReportFamily.PRE_GENERAL),
    ("pre general", ReportFamily.PRE_GENERAL),
    ("pre-primary", ReportFamily.PRE_PRIMARY),
    ("pre primary", ReportFamily.PRE_PRIMARY),
)


@dataclass(frozen=True)
class CommitteeProfile:
    """The committee facts that decide which deadlines apply.

    Built from an OpenFEC committee record plus the user-set lifecycle
    status; `state`/`district`/`office` describe the RACE the committee's
    candidate is in, which is not always the committee's own mailing-address
    state, so they're passed separately rather than read off the committee
    record's `state` field.
    """

    committee_id: str
    name: str
    designation: str
    filing_frequency: str
    status: CommitteeStatus
    state: str | None = None
    district: str | None = None
    office: str | None = None

    @property
    def is_authorized(self) -> bool:
        """Whether this is a candidate's own committee (vs. a PAC/party)."""
        return (self.designation or "").upper() in _AUTHORIZED_DESIGNATIONS


@dataclass(frozen=True)
class DeadlineMatch:
    """Whether one published deadline binds one committee, and why.

    `certain` is the important field: False means the record could not be
    confidently classified (an unrecognized report type, or a race prefix
    that wouldn't parse). Those are reported as applying anyway, flagged,
    rather than filtered out -- see `match_deadline`.
    """

    applies: bool
    family: ReportFamily | None
    reason: str
    certain: bool = True


def _normalize_district(value: str | None) -> str | None:
    """District numbers appear zero-padded in calendar summaries ("07") and
    unpadded elsewhere ("7"); at-large seats show up as "00", "AL", or
    blank. Normalize so those compare equal."""
    if value is None:
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    if text in {"AL", "00", "0"}:
        return "AL"
    if text in {"S", "SEN"}:
        return "S"
    return text.lstrip("0") or "AL"


def parse_race(record: dict[str, Any]) -> tuple[str | None, str | None]:
    """Best-effort (state_code, district) for a calendar record.

    OpenFEC leaves the record's own `state` field null on the rows seen so
    far, so this reads the `summary` prefix ("TN/07 ...") first and falls
    back to the full state name in `location` ("Tennessee"). Either element
    may be None when it can't be determined -- callers must treat that as
    "unknown", never as "no state".
    """
    summary = (record.get("summary") or "").strip()
    match = _RACE_PREFIX.match(summary)
    if match:
        return match.group(1), _normalize_district(match.group(2))

    # `state` is null in practice, but honor it if it's ever populated.
    raw_state = record.get("state")
    if isinstance(raw_state, str) and len(raw_state.strip()) == 2:
        return raw_state.strip().upper(), None
    if isinstance(raw_state, list) and raw_state:
        return str(raw_state[0]).strip().upper(), None

    return state_code(record.get("location") or ""), None


def classify_family(record: dict[str, Any]) -> ReportFamily | None:
    """Which report family a calendar record belongs to, or None if it
    can't be told from the record's own text."""
    category_id = record.get("calendar_category_id")
    try:
        category_id = int(category_id)
    except (TypeError, ValueError):
        category_id = None

    if category_id == CATEGORY_QUARTERLY or category_id == CATEGORY_MONTHLY:
        return ReportFamily.REGULAR

    # `summary` is the record's authoritative short label; `description` is
    # only consulted when the summary says nothing recognizable. Scanning
    # the two concatenated would let a stale or differently-worded
    # description override a clear summary, resolving by keyword order
    # rather than by which field actually names the report.
    for field in ("summary", "description"):
        text = (record.get(field) or "").lower()
        for keyword, family in _FAMILY_KEYWORDS:
            if keyword in text:
                return family
    return None


def match_deadline(record: dict[str, Any], profile: CommitteeProfile) -> DeadlineMatch:
    """Decide whether one published FEC deadline binds one committee.

    Errs toward inclusion on purpose. A deadline wrongly shown costs the
    user a moment deciding it doesn't apply; a deadline wrongly hidden can
    cost them a missed filing, so anything this function cannot confidently
    rule out is returned with applies=True and certain=False for the caller
    to surface as unverified rather than silently drop.
    """
    category_id = record.get("calendar_category_id")
    try:
        category_id = int(category_id)
    except (TypeError, ValueError):
        return DeadlineMatch(
            True, None, "record has no usable calendar_category_id", certain=False
        )

    # -- Regular reports: driven purely by the committee's filing frequency.
    if category_id in (CATEGORY_QUARTERLY, CATEGORY_MONTHLY):
        frequency = (profile.filing_frequency or "").upper()
        if frequency == FREQUENCY_QUARTERLY:
            applies = category_id == CATEGORY_QUARTERLY
            return DeadlineMatch(
                applies,
                ReportFamily.REGULAR if applies else None,
                "quarterly filer" if applies else "monthly report; committee files quarterly",
            )
        if frequency == FREQUENCY_MONTHLY:
            applies = category_id == CATEGORY_MONTHLY
            return DeadlineMatch(
                applies,
                ReportFamily.REGULAR if applies else None,
                "monthly filer" if applies else "quarterly report; committee files monthly",
            )
        # An unrecognized frequency code must not silently suppress every
        # regular report -- show both families and flag the uncertainty.
        return DeadlineMatch(
            True,
            ReportFamily.REGULAR,
            f"unrecognized filing_frequency {profile.filing_frequency!r}; "
            "showing all regular reports",
            certain=False,
        )

    # -- Pre/post-election reports.
    if category_id == CATEGORY_PRE_POST_ELECTION:
        family = classify_family(record)
        if family is None:
            return DeadlineMatch(
                True,
                None,
                "election-related report of an unrecognized type",
                certain=False,
            )

        # Checked BEFORE the lifecycle filter, not after: an unauthorized
        # committee's pre/post-election obligations depend on whether it
        # actually spent in connection with THAT race -- committee activity,
        # which its lifecycle status cannot answer. Applying the status
        # filter first would rule every election report out for PACs and
        # party committees (whose status is ONGOING, i.e. regular reports
        # only), and they do owe pre/post-general reports when they spend in
        # a race.
        if not profile.is_authorized:
            return DeadlineMatch(
                True,
                family,
                "non-candidate committee; depends on whether it had activity in this race",
                certain=False,
            )

        if family not in report_families_for(profile.status):
            return DeadlineMatch(
                False,
                family,
                f"{family.value} does not apply to a committee that is {profile.status.value}",
            )

        record_state, record_district = parse_race(record)
        if record_state is None:
            return DeadlineMatch(
                True, family, "could not determine which race this deadline belongs to",
                certain=False,
            )
        if profile.state is None:
            return DeadlineMatch(
                True, family, "committee's race state is unknown", certain=False
            )
        if record_state.upper() != profile.state.upper():
            return DeadlineMatch(False, family, f"different state ({record_state})")

        profile_district = _normalize_district(profile.district)
        if record_district and profile_district and record_district != profile_district:
            return DeadlineMatch(
                False, family, f"different district ({record_state}/{record_district})"
            )

        return DeadlineMatch(True, family, f"{family.value} for {record_state}")

    # Some other calendar category (election dates, IE/EC periods) -- not a
    # filing deadline this committee owes.
    return DeadlineMatch(False, None, f"calendar category {category_id} is not a filing deadline")
