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

from enum import Enum


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
