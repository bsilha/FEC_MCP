# fec-mcp

An MCP (Model Context Protocol) server for Federal Election Commission rules,
regulations, and data covering PACs, party committees, and candidates.

It provides two complementary tool families:

1. **Rulebook search** -- full-text search over the FEC's own official PDF
   guides (campaign guides for candidates, party committees, and PACs, plus
   the contribution-limits chart). This is the authoritative source for
   compliance questions and dollar limits: results are quoted directly from
   the PDFs with filename + page citations, not hardcoded or model-recalled
   figures.
2. **Live OpenFEC data** -- real-time lookups against the public
   [OpenFEC API](https://api.open.fec.gov/) for candidates, committees,
   filings, financial totals, elections, and the reporting calendar.

## Setup

### 1. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Add the FEC rulebook PDFs

Place the official FEC PDF guides in `data/rulebooks/`, for example:

- The FEC's Campaign Guide for Congressional Candidates and Committees
- The FEC's Campaign Guide for Political Party Committees
- The FEC's Campaign Guide for Nonconnected Committees (PACs)
- The current Contribution Limits chart

These are published on [fec.gov](https://www.fec.gov) under its Campaign
Guides / Contribution Limits resources; grab the current versions from
there (this repo does not bundle download links since they change over
time -- verify you have the current cycle's documents).

Any PDF you drop in `data/rulebooks/` is picked up automatically -- the
server builds a search index (cached in `data/rulebooks/.index/`) the first
time it starts and rebuilds it whenever files are added, removed, or
changed.

### 3. Get an OpenFEC API key (optional but recommended)

Live OpenFEC lookups work out of the box with the shared `DEMO_KEY`, which
is heavily rate-limited. For real use, get a free key at
<https://api.data.gov/signup/> and set it:

```bash
export FEC_API_KEY=your_key_here
```

### 4. Configure your MCP client

For Claude Code / Claude Desktop, add to your MCP config:

```json
{
  "mcpServers": {
    "fec": {
      "command": "/absolute/path/to/FEC_MCP/.venv/bin/fec-mcp",
      "env": {
        "FEC_API_KEY": "your_key_here"
      }
    }
  }
}
```

Or run directly for local testing:

```bash
fec-mcp
```

## Tools

**Rulebook search (grounded in the PDFs you add):**

| Tool | Purpose |
| --- | --- |
| `list_rulebook_sources` | List loaded PDFs and page counts |
| `search_rulebooks` | Full-text search across all loaded PDFs |
| `get_rulebook_page` | Read the full text of one page |

**Live OpenFEC data:**

| Tool | Purpose |
| --- | --- |
| `search_candidates` / `get_candidate` / `get_candidate_totals` | Candidate lookup and financials |
| `search_committees` / `get_committee` / `get_committee_filings` / `get_committee_totals` | PAC/party/campaign committee lookup, filings, financials |
| `search_filings` | Cross-committee filing search |
| `search_elections` | Election search by state/office/cycle |
| `get_reporting_calendar` | FEC reporting and election deadline dates |

## Notes on scope

- **No hardcoded contribution limits or rule text.** OpenFEC's live API has
  no endpoint for contribution limits or regulation text, and those figures
  change (limits are inflation-adjusted every two-year cycle) -- baking them
  into code risks going stale silently. Instead, this server searches the
  FEC's own PDFs you provide, so answers are always sourced to a specific
  document and page.
- **Not legal advice.** Tool output should be treated as a research aid and
  cited back to its source (PDF page or OpenFEC record), not relied on as a
  substitute for consulting the FEC or legal counsel.

## Development

```bash
pip install -e ".[dev]"
pytest
```
