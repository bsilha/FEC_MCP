"""Coverage for the committee roster.

Weighted toward the rule that carries the most risk: a committee without
a status contributes nothing. A guessed status produces a complete,
confident, wrong schedule, and unlike a missing one, nothing about a
wrong schedule looks wrong.
"""

from datetime import date

import pytest

from fec_mcp.committee_roster import (
    UNSET_STATUS,
    CommitteeRoster,
    cycle_horizon_months,
)

CRANE = {
    "committee_id": "C00784934",
    "name": "ELI CRANE FOR CONGRESS",
    "designation": "P",
    "filing_frequency": "Q",
    "state": "AZ",
}
PAC = {
    "committee_id": "C00401224",
    "name": "ACTBLUE",
    "designation": "U",
    "filing_frequency": "M",
    "state": "MA",
}


@pytest.fixture
def roster(tmp_path):
    return CommitteeRoster(path=tmp_path / "roster.json")


def test_a_newly_added_committee_has_no_status(roster):
    """The rule the whole feature rests on."""
    roster.add(CRANE)
    entry = roster.get("C00784934")

    assert entry.status == UNSET_STATUS
    assert not entry.has_status
    assert entry.status_set_on is None


def test_adding_keeps_the_fields_deadline_logic_needs(roster):
    roster.add(CRANE)
    entry = roster.get("C00784934")

    assert entry.designation == "P"
    assert entry.filing_frequency == "Q"
    assert entry.is_candidate_committee


def test_a_pac_is_not_a_candidate_committee(roster):
    roster.add(PAC)
    assert not roster.get("C00401224").is_candidate_committee


def test_the_committees_own_state_seeds_the_race_state(roster):
    """A saved keystroke, not an assertion -- a committee's mailing
    address need not be where its race is, so it stays editable."""
    roster.add(CRANE)
    assert roster.get("C00784934").state == "AZ"


def test_setting_a_status_stamps_when_it_was_set(roster):
    """So a status set eight months ago looks its age rather than
    authoritative once a race has resolved."""
    roster.add(CRANE)
    roster.update("C00784934", status="won_primary")
    entry = roster.get("C00784934")

    assert entry.status == "won_primary"
    assert entry.status_set_on == date.today().isoformat()


def test_restating_the_same_status_does_not_refresh_its_date(roster):
    """Re-rendering the same value must not make a stale status look new."""
    roster.add(CRANE)
    roster.update("C00784934", status="won_primary")
    roster._entries["C00784934"]["status_set_on"] = "2026-01-01"
    roster.update("C00784934", status="won_primary")

    assert roster.get("C00784934").status_set_on == "2026-01-01"


def test_clearing_a_status_clears_its_date_too(roster):
    roster.add(CRANE)
    roster.update("C00784934", status="won_primary")
    roster.update("C00784934", status=UNSET_STATUS)
    entry = roster.get("C00784934")

    assert not entry.has_status
    assert entry.status_set_on is None


def test_race_can_be_edited_independently_of_status(roster):
    roster.add(CRANE)
    roster.update("C00784934", state="MI", district="04")
    entry = roster.get("C00784934")

    assert (entry.state, entry.district) == ("MI", "04")
    assert not entry.has_status


def test_adding_the_same_committee_twice_does_not_duplicate_or_reset_it(roster):
    roster.add(CRANE)
    roster.update("C00784934", status="lost_primary")
    roster.add(CRANE)

    assert len(roster) == 1
    assert roster.get("C00784934").status == "lost_primary"


def test_entries_keep_the_order_they_were_added(roster):
    roster.add(CRANE)
    roster.add(PAC)
    assert [e.committee_id for e in roster.entries()] == ["C00784934", "C00401224"]


def test_order_survives_a_reload_even_when_added_the_same_day(tmp_path):
    """Regression guard. Ordering keyed on a date alone ties for anything
    added in one sitting, and the sort then falls through to dict order --
    which save() writes alphabetically by committee ID. The roster
    silently reordered on reload, so the status dropdown on row one
    belonged to a different committee than the name beside it."""
    path = tmp_path / "roster.json"
    first = CommitteeRoster(path=path)
    first.add(CRANE)   # C00784934 -- later alphabetically
    first.add(PAC)     # C00401224 -- earlier alphabetically

    reopened = CommitteeRoster(path=path)
    assert [e.committee_id for e in reopened.entries()] == ["C00784934", "C00401224"]


def test_removing_a_committee_drops_it(roster):
    roster.add(CRANE)
    roster.remove("C00784934")
    assert len(roster) == 0
    assert roster.get("C00784934") is None


def test_committee_ids_are_matched_case_insensitively(roster):
    roster.add(CRANE)
    roster.update("c00784934", status="in_primary")
    assert roster.get("c00784934").status == "in_primary"


def test_the_roster_survives_a_restart(tmp_path):
    path = tmp_path / "roster.json"
    first = CommitteeRoster(path=path)
    first.add(CRANE)
    first.update("C00784934", status="won_primary", district="02")

    reopened = CommitteeRoster(path=path)
    entry = reopened.get("C00784934")
    assert entry.status == "won_primary"
    assert entry.district == "02"


def test_a_corrupt_roster_starts_empty_rather_than_failing(tmp_path):
    path = tmp_path / "roster.json"
    path.write_text("{ not json")
    assert len(CommitteeRoster(path=path)) == 0


def test_updating_a_committee_that_is_not_on_the_roster_is_a_no_op(roster):
    roster.update("C00000000", status="won_primary")
    assert len(roster) == 0


# -- how far ahead the agenda looks -----------------------------------------


def test_horizon_reaches_the_year_end_report_that_closes_the_cycle():
    """Cycles close with a year-end report due the following January 31;
    a window ending before it would hide a real filing."""
    assert cycle_horizon_months(date(2025, 1, 15)) == 25  # through 2027-01-31


def test_horizon_never_drops_below_a_year():
    """Late in a cycle only months remain, but the FEC has usually
    published into the next one by then -- shortening the window would
    drop those."""
    assert cycle_horizon_months(date(2026, 11, 1)) == 12


def test_horizon_from_an_odd_year_runs_to_the_next_even_cycle_close():
    assert cycle_horizon_months(date(2027, 6, 1)) >= 12


# -- which committees have a district at all ---------------------------------

SENATE = {
    "committee_id": "C00694455",
    "name": "ABDUL FOR U.S. SENATE",
    "designation": "P",
    "filing_frequency": "Q",
    "committee_type": "S",
    "state": "MI",
}


def test_a_house_committee_runs_in_a_district(roster):
    roster.add({**CRANE, "committee_type": "H"})
    assert roster.get("C00784934").runs_in_a_district


def test_a_senate_committee_has_no_district(roster):
    """Not "unknown" -- a Senate seat is statewide, so a district on one
    would narrow the race to a contest that does not exist."""
    roster.add(SENATE)
    entry = roster.get("C00694455")

    assert entry.is_candidate_committee
    assert not entry.runs_in_a_district


def test_a_presidential_committee_has_no_district(roster):
    roster.add({**SENATE, "committee_id": "C00111111", "committee_type": "P"})
    assert not roster.get("C00111111").runs_in_a_district


def test_a_pac_has_no_district(roster):
    roster.add(PAC)
    assert not roster.get("C00401224").runs_in_a_district


def test_a_candidate_committee_of_unknown_office_keeps_its_district_box(roster):
    """Rosters saved before committee_type was stored have none. An
    unnecessary box on a Senate committee is a smaller harm than a House
    committee whose district cannot be entered at all."""
    roster.add({k: v for k, v in CRANE.items()})  # no committee_type
    assert roster.get("C00784934").runs_in_a_district


def test_committee_type_survives_a_reload(tmp_path):
    path = tmp_path / "roster.json"
    CommitteeRoster(path=path).add(SENATE)
    assert CommitteeRoster(path=path).get("C00694455").committee_type == "S"


def test_a_resolved_district_is_kept_when_the_committee_is_added(roster):
    """The caller looks the race up before adding; no committee record
    carries a district of its own."""
    roster.add({**CRANE, "committee_type": "H", "district": "02"})
    assert roster.get("C00784934").district == "02"


def test_a_committee_added_without_a_district_has_none(roster):
    roster.add(CRANE)
    assert roster.get("C00784934").district is None
