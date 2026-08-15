"""Coverage for matching published FEC calendar records to a committee.

The calendar records below are copied verbatim from real OpenFEC API
output (captured by a schema probe against /calendar-dates/), not
invented -- the field quirks these tests depend on, especially `state`
being null while the race lives in `summary`/`location`, are real API
behavior and would be easy to "fix" into something the API never returns.
"""

import pytest

from fec_mcp.deadlines import (
    CommitteeProfile,
    CommitteeStatus,
    ReportFamily,
    classify_family,
    match_deadline,
    parse_race,
)

# Verbatim from OpenFEC /calendar-dates/, calendar_category_id 27.
REAL_POST_GENERAL = {
    "all_day": True,
    "calendar_category_id": 27,
    "category": "Pre and Post-Elections",
    "description": "Tennessee's 7th Congressional District Special Post-General Report due",
    "end_date": None,
    "event_id": "8250",
    "location": "Tennessee",
    "start_date": "2026-01-01",
    "state": None,
    "summary": "TN/07 Special Post-General Report Due",
    "url": "https://www.fec.gov/help-candidates-and-committees/dates-and-deadlines/",
}

QUARTERLY_RECORD = {
    "calendar_category_id": 25,
    "category": "Quarterly",
    "summary": "October Quarterly Report Due",
    "start_date": "2026-10-15",
    "state": None,
    "location": None,
}

MONTHLY_RECORD = {
    "calendar_category_id": 26,
    "category": "Monthly",
    "summary": "Monthly Report Due",
    "start_date": "2026-10-20",
    "state": None,
    "location": None,
}


def profile(**overrides) -> CommitteeProfile:
    base = dict(
        committee_id="C00832790",
        name="TEST FOR CONGRESS",
        designation="P",
        filing_frequency="Q",
        status=CommitteeStatus.WON_PRIMARY,
        state="TN",
        district="07",
        office="H",
    )
    base.update(overrides)
    return CommitteeProfile(**base)


# -- race parsing -----------------------------------------------------------


def test_parse_race_reads_the_summary_prefix():
    """`state` is null on real records; the race is in the summary."""
    assert parse_race(REAL_POST_GENERAL) == ("TN", "7")


def test_parse_race_falls_back_to_location_when_no_summary_prefix():
    record = {"summary": "Post-General Report Due", "location": "Tennessee", "state": None}
    assert parse_race(record) == ("TN", None)


def test_parse_race_returns_none_when_location_is_unrecognized():
    """None means "couldn't tell", which callers must not treat as "no
    state" -- an unparseable record has to stay visible."""
    record = {"summary": "Report Due", "location": "Somewhere Else", "state": None}
    assert parse_race(record) == (None, None)


def test_parse_race_handles_senate_style_prefix():
    record = {"summary": "TX/S Pre-General Report Due", "location": "Texas", "state": None}
    assert parse_race(record) == ("TX", "S")


def test_parse_race_honors_a_populated_state_field_if_present():
    record = {"summary": "Post-General Report Due", "location": None, "state": "GA"}
    assert parse_race(record) == ("GA", None)


# -- family classification --------------------------------------------------


def test_classify_family_uses_category_for_regular_reports():
    assert classify_family(QUARTERLY_RECORD) is ReportFamily.REGULAR
    assert classify_family(MONTHLY_RECORD) is ReportFamily.REGULAR


def test_classify_family_reads_report_type_from_real_summary():
    assert classify_family(REAL_POST_GENERAL) is ReportFamily.POST_GENERAL


def test_classify_family_does_not_confuse_post_general_with_pre_general():
    pre = dict(REAL_POST_GENERAL, summary="TN/07 Pre-General Report Due")
    assert classify_family(pre) is ReportFamily.PRE_GENERAL


def test_classify_family_returns_none_for_an_unrecognized_report_type():
    odd = dict(REAL_POST_GENERAL, summary="TN/07 Something Unusual Due", description="")
    assert classify_family(odd) is None


# -- regular reports vs filing frequency ------------------------------------


def test_quarterly_filer_gets_quarterly_and_not_monthly():
    quarterly = profile(filing_frequency="Q")
    assert match_deadline(QUARTERLY_RECORD, quarterly).applies
    assert not match_deadline(MONTHLY_RECORD, quarterly).applies


def test_monthly_filer_gets_monthly_and_not_quarterly():
    monthly = profile(filing_frequency="M")
    assert match_deadline(MONTHLY_RECORD, monthly).applies
    assert not match_deadline(QUARTERLY_RECORD, monthly).applies


def test_unknown_filing_frequency_shows_all_regular_reports_but_flags_it():
    """Suppressing every regular report because of an unrecognized code
    would hide real deadlines -- show them, marked uncertain."""
    unknown = profile(filing_frequency="X")
    for record in (QUARTERLY_RECORD, MONTHLY_RECORD):
        result = match_deadline(record, unknown)
        assert result.applies
        assert not result.certain


# -- lifecycle status drives election reports -------------------------------


def test_committee_that_won_its_primary_owes_the_post_general():
    result = match_deadline(REAL_POST_GENERAL, profile(status=CommitteeStatus.WON_PRIMARY))
    assert result.applies
    assert result.family is ReportFamily.POST_GENERAL
    assert result.certain


def test_committee_that_lost_its_primary_does_not_owe_the_post_general():
    result = match_deadline(REAL_POST_GENERAL, profile(status=CommitteeStatus.LOST_PRIMARY))
    assert not result.applies
    assert "lost_primary" in result.reason


def test_committee_still_in_its_primary_does_not_owe_the_post_general():
    result = match_deadline(REAL_POST_GENERAL, profile(status=CommitteeStatus.IN_PRIMARY))
    assert not result.applies


# -- race matching ----------------------------------------------------------


def test_deadline_for_a_different_state_does_not_apply():
    result = match_deadline(REAL_POST_GENERAL, profile(state="CA", district="12"))
    assert not result.applies
    assert "different state" in result.reason


def test_deadline_for_a_different_district_in_the_same_state_does_not_apply():
    result = match_deadline(REAL_POST_GENERAL, profile(state="TN", district="05"))
    assert not result.applies
    assert "different district" in result.reason


def test_district_comparison_ignores_zero_padding():
    """Calendar summaries zero-pad ("07"); committee/candidate records
    often don't ("7"). Those are the same seat."""
    assert match_deadline(REAL_POST_GENERAL, profile(district="7")).applies
    assert match_deadline(REAL_POST_GENERAL, profile(district="07")).applies


def test_unknown_committee_race_keeps_the_deadline_visible_but_uncertain():
    result = match_deadline(REAL_POST_GENERAL, profile(state=None, district=None))
    assert result.applies
    assert not result.certain


def test_unparseable_race_on_the_record_keeps_it_visible_but_uncertain():
    record = dict(REAL_POST_GENERAL, summary="Post-General Report Due", location="Atlantis")
    result = match_deadline(record, profile())
    assert result.applies
    assert not result.certain


# -- non-candidate committees -----------------------------------------------


def test_pac_election_reports_are_shown_but_flagged_as_activity_dependent():
    """A PAC's pre/post-election obligation depends on whether it actually
    spent in that race, which lifecycle status can't answer -- so it can
    be neither confirmed nor ruled out here."""
    pac = profile(designation="U", status=CommitteeStatus.ONGOING, state=None, district=None)
    result = match_deadline(REAL_POST_GENERAL, pac)
    assert result.applies
    assert not result.certain
    assert "activity" in result.reason


def test_authorized_designations_are_recognized():
    assert profile(designation="P").is_authorized
    assert profile(designation="A").is_authorized
    assert not profile(designation="U").is_authorized


# -- non-deadline categories ------------------------------------------------


def test_election_date_records_are_not_treated_as_filing_deadlines():
    """Category 36 is when the election happens, not a report the
    committee owes."""
    election_day = {
        "calendar_category_id": 36,
        "category": "Election Dates",
        "summary": "TX/18 Special General Election Runoff",
        "start_date": "2026-01-31",
    }
    assert not match_deadline(election_day, profile()).applies


@pytest.mark.parametrize("bad", [None, "", "not-a-number"])
def test_record_without_a_usable_category_stays_visible_but_uncertain(bad):
    result = match_deadline(dict(REAL_POST_GENERAL, calendar_category_id=bad), profile())
    assert result.applies
    assert not result.certain
