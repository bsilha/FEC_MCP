# The 19 tools

Every tool is registered in two places from one definition: the MCP server
(`src/fec_mcp/server.py`, via `@mcp.tool()`) and the Streamlit demo
(`demo/app.py`, via thin `@beta_tool` wrappers that call the same
functions). Anything Claude Desktop or VS Code can do here, the demo can
do too, and neither has logic the other lacks.

They fall into four families, and the split matters more than the count:
**the first three fetch, the fourth decides.**

## Rulebook search — 4 tools, offline

Read the PDFs in `data/rulebooks/`. No network. These are the only tools
that can produce a citation.

| Tool | Purpose |
| --- | --- |
| `list_rulebook_jurisdictions()` | Which jurisdictions have PDFs loaded — `federal` plus state codes |
| `list_rulebook_sources(jurisdiction=None)` | The loaded PDFs, page counts and titles |
| `search_rulebooks(query, top_k=8, source=None, jurisdiction=None)` | Full-text search across them |
| `get_rulebook_page(source, page)` | Full extracted text of one page |

`search_rulebooks` finds a passage; `get_rulebook_page` reads the whole
page around it. That pairing is also what makes citations checkable —
the eval re-opens the exact page a citation names and fails the case if
it isn't there.

## Live OpenFEC data — 11 tools

All hit the public [OpenFEC API](https://api.open.fec.gov/). Federal
only; OpenFEC has no state data.

**Candidates**

| Tool | Purpose |
| --- | --- |
| `search_candidates(name, state, office, party, cycle, candidate_status, ...)` | Find candidates |
| `get_candidate(candidate_id)` | One candidate by FEC ID |
| `get_candidate_totals(candidate_id, cycle=None)` | Receipts, disbursements, cash on hand |

**Committees**

| Tool | Purpose |
| --- | --- |
| `search_committees(name, state, committee_type, designation, cycle, ...)` | Find PACs, party and campaign committees |
| `get_committee(committee_id)` | One committee by FEC ID |
| `get_committee_totals(committee_id, cycle=None, ...)` | Financial totals by cycle |
| `get_committee_filings(committee_id, form_type=None, cycle=None, ...)` | Its filings (Form 3, 3X, 3P) |

**Transactions, elections, calendar**

| Tool | Purpose |
| --- | --- |
| `search_disbursements(committee_id, recipient_name, purpose, dates, amounts, ...)` | Itemized Schedule B spending — the most filterable tool here |
| `search_filings(committee_id, candidate_id, form_type, cycle, ...)` | Filing search across committees and candidates |
| `search_elections(state, office, cycle, district, ...)` | Federal races |
| `get_reporting_calendar(category, min_start_date, max_start_date, ...)` | The FEC's raw deadline calendar |

## Advisory opinions — 2 tools

| Tool | Purpose |
| --- | --- |
| `search_advisory_opinions(q, ao_no, ao_year, ao_name, ao_status, ...)` | Search the FEC legal-search API |
| `get_advisory_opinion(ao_no)` | One AO's full document record |

Worth treating as its own family rather than folding into "live data".
Many questions that *sound* like rulebook questions are settled by an
advisory opinion instead — cryptocurrency contributions being the
standing example, which is what eval case `rb-18` exists to check.

## Deadlines and calendar invites — 2 tools

| Tool | Purpose |
| --- | --- |
| `get_committee_deadlines(committee, status, state=None, district=None, months_ahead=12)` | Which filing deadlines actually bind one committee |
| `send_deadline_invites(committee, status, recipients, ..., send=False)` | Email those deadlines as calendar invitations |

`get_committee_deadlines` is the only tool in the server that reaches a
conclusion. Everything else fetches or searches; this one applies the
lifecycle rules in `deadlines.py` to say a committee does *not* owe a
report — a claim it can be wrong about, and one whose wrongness is
invisible. That is why it returns the deadlines it ruled out along with
its reason for each, rather than only the ones that apply.

`send_deadline_invites` defaults to `send=False`. Calling it without
meaning to sends nothing; the demo's Preview button depends on that.

## Two properties worth keeping

**Only one tool has an outside side effect.** `send_deadline_invites` is
the single tool that leaves the machine with anything. The other 18 are
read-only. Any new tool that writes, sends, or files should be
scrutinised against that, because it is currently a very short list.

**Only one tool decides anything.** See above. When a fetch tool is
wrong, it is wrong the way a failed request is wrong — visibly. When a
deciding tool is wrong, it produces a complete, confident, plausible
answer. New tools in that category need the same treatment
`get_committee_deadlines` gets: show the reasoning, show what was ruled
out, and never present a judgement as a lookup.
