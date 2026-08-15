"""Coverage for the committee lifecycle -> report-family logic.

These are the rules that decide which published FEC deadlines actually
bind a committee, so the cases below are written as compliance
assertions ("a committee that lost its primary still files regular
reports") rather than as mechanical enum round-trips.
"""

import pytest

from fec_mcp.deadlines import (
    CommitteeStatus,
    ReportFamily,
    allowed_transitions,
    can_transition,
    is_election_driven,
    report_families_for,
)


def test_every_status_has_report_families_defined():
    """Guard against a status being added to the enum without a
    corresponding rule -- a missing entry would raise KeyError at request
    time rather than failing here."""
    for status in CommitteeStatus:
        assert report_families_for(status), f"{status} has no report families"


def test_every_status_has_transitions_defined():
    for status in CommitteeStatus:
        allowed_transitions(status)  # must not raise


@pytest.mark.parametrize("status", list(CommitteeStatus))
def test_regular_reports_always_apply(status):
    """The obligation people most often assume ends with a loss. It
    doesn't -- regular reporting continues until a termination report is
    actually filed, in every lifecycle state."""
    assert ReportFamily.REGULAR in report_families_for(status)


def test_losing_the_primary_drops_general_election_reports():
    families = report_families_for(CommitteeStatus.LOST_PRIMARY)
    assert ReportFamily.PRE_GENERAL not in families
    assert ReportFamily.POST_GENERAL not in families
    assert families == {ReportFamily.REGULAR}


def test_winning_the_primary_adds_general_election_reports():
    families = report_families_for(CommitteeStatus.WON_PRIMARY)
    assert ReportFamily.PRE_GENERAL in families
    assert ReportFamily.POST_GENERAL in families


def test_in_primary_does_not_surface_general_election_reports_yet():
    """Before the primary, the general is hypothetical -- surfacing
    pre/post-general deadlines the committee may never owe would be
    noise at best and a wrong obligation at worst."""
    families = report_families_for(CommitteeStatus.IN_PRIMARY)
    assert ReportFamily.PRE_PRIMARY in families
    assert ReportFamily.PRE_GENERAL not in families
    assert ReportFamily.POST_GENERAL not in families


def test_post_general_still_owed_after_either_general_election_outcome():
    """Winning or losing the general doesn't excuse the post-general
    report -- both outcomes still owe it."""
    for status in (CommitteeStatus.WON_GENERAL, CommitteeStatus.LOST_GENERAL):
        assert ReportFamily.POST_GENERAL in report_families_for(status)


def test_pre_primary_not_repeated_after_the_primary_is_over():
    for status in (
        CommitteeStatus.WON_PRIMARY,
        CommitteeStatus.LOST_PRIMARY,
        CommitteeStatus.WON_GENERAL,
        CommitteeStatus.LOST_GENERAL,
    ):
        assert ReportFamily.PRE_PRIMARY not in report_families_for(status)


def test_non_candidate_committee_has_no_election_driven_reports_from_status():
    """A PAC's pre/post-election obligations depend on whether it actually
    spent in connection with an election, which status alone can't tell
    us -- so status must not imply them."""
    assert report_families_for(CommitteeStatus.ONGOING) == {ReportFamily.REGULAR}
    assert not is_election_driven(CommitteeStatus.ONGOING)


def test_is_election_driven_flags_only_statuses_with_election_reports():
    assert is_election_driven(CommitteeStatus.IN_PRIMARY)
    assert is_election_driven(CommitteeStatus.WON_PRIMARY)
    assert not is_election_driven(CommitteeStatus.LOST_PRIMARY)
    assert not is_election_driven(CommitteeStatus.TERMINATING)


# -- lifecycle transitions --------------------------------------------------


def test_primary_can_be_won_or_lost():
    assert can_transition(CommitteeStatus.IN_PRIMARY, CommitteeStatus.WON_PRIMARY)
    assert can_transition(CommitteeStatus.IN_PRIMARY, CommitteeStatus.LOST_PRIMARY)


def test_cannot_win_the_general_without_winning_the_primary():
    assert not can_transition(CommitteeStatus.IN_PRIMARY, CommitteeStatus.WON_GENERAL)
    assert not can_transition(CommitteeStatus.LOST_PRIMARY, CommitteeStatus.WON_GENERAL)


def test_a_lost_primary_can_only_wind_down():
    assert allowed_transitions(CommitteeStatus.LOST_PRIMARY) == {CommitteeStatus.TERMINATING}


def test_an_officeholder_can_re_enter_the_next_cycle():
    """A sitting officeholder running again reuses the same committee and
    re-enters at the primary."""
    assert can_transition(CommitteeStatus.WON_GENERAL, CommitteeStatus.IN_PRIMARY)


def test_terminating_is_terminal():
    assert allowed_transitions(CommitteeStatus.TERMINATING) == frozenset()
    assert not can_transition(CommitteeStatus.TERMINATING, CommitteeStatus.IN_PRIMARY)


def test_restating_the_current_status_is_allowed():
    """A no-op save from the UI must not be rejected as an illegal move."""
    for status in CommitteeStatus:
        assert can_transition(status, status)


def test_every_status_is_reachable_or_is_a_documented_entry_point():
    """Guard against a status that nothing can ever transition into and
    that isn't a valid starting state -- that would be dead code."""
    entry_points = {CommitteeStatus.IN_PRIMARY, CommitteeStatus.ONGOING}
    reachable = set(entry_points)
    for targets in (allowed_transitions(s) for s in CommitteeStatus):
        reachable |= set(targets)
    assert reachable == set(CommitteeStatus)
