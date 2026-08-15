"""US state/territory code <-> name lookup, shared across the package.

Lives here rather than in demo/app.py (which previously owned the only
copy) because the deadline logic needs the *reverse* direction: OpenFEC's
calendar records leave the `state` field null and put the jurisdiction in
a `location` field as a full name ("Tennessee"), so matching a deadline to
a committee means turning that name back into a code.

Territories are included even though the demo's rulebook sidebar has no
documents for them -- FEC filing deadlines are real for territorial
delegate races, and a deadline silently failing to match because its
location isn't in this table is exactly the kind of miss this feature
can't afford.
"""

from __future__ import annotations

US_STATE_NAMES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia",
    # Territories and commonwealths that elect federal delegates/commissioners.
    "AS": "American Samoa", "GU": "Guam", "MP": "Northern Mariana Islands",
    "PR": "Puerto Rico", "VI": "Virgin Islands",
}

# Reverse lookup, lowercased for case-insensitive matching.
_NAME_TO_CODE: dict[str, str] = {name.lower(): code for code, name in US_STATE_NAMES.items()}

# OpenFEC's calendar `location` field doesn't always use the same wording
# as US_STATE_NAMES. Only add an alias here when it has actually been seen
# in real API output -- guessing at variants invents matches that never
# occur and hides the ones that do.
_NAME_ALIASES: dict[str, str] = {
    "u.s. virgin islands": "VI",
    "us virgin islands": "VI",
    "washington, d.c.": "DC",
    "washington dc": "DC",
}


def state_name(code: str) -> str:
    """Full name for a two-letter code, or the upper-cased code if unknown."""
    return US_STATE_NAMES.get(code.upper(), code.upper())


def state_code(name: str) -> str | None:
    """Two-letter code for a full state name, or None if unrecognized.

    Returns None rather than guessing: callers matching a deadline to a
    committee must be able to tell "this is a different state" apart from
    "I couldn't parse this location", since those warrant different
    handling -- the first is safe to filter out, the second isn't.
    """
    if not name:
        return None
    key = name.strip().lower()
    if key in _NAME_TO_CODE:
        return _NAME_TO_CODE[key]
    return _NAME_ALIASES.get(key)
