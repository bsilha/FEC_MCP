# fec_mcp rulebook eval

A one-off report suite, not a CI gate. Run it manually after a prompt,
tool-description, or index change you want to sanity-check:

```
ANTHROPIC_API_KEY=sk-ant-... .venv/bin/python evals/run_rulebook_eval.py
```

Requires a real `ANTHROPIC_API_KEY` (this repo's `.venv` already has the
`anthropic` package installed) -- each case makes a live call to
`claude-opus-5` via the same `demo/app.py` chat-turn code the demo app
itself uses, so nothing here is a mock.

## What it checks

Every case in `rulebook_cases.py` is one natural-language question, run
through the real `run_turn()` with every tool the app registers (not just
the rulebook ones), and graded on two axes, both deterministic -- no LLM
judge:

1. **Tool selection** -- did the model call at least one of the expected
   tools, and none of the forbidden ones? A few cases also assert on
   arguments (e.g. that a California-specific question actually passed
   `jurisdiction="ca"`, or that a question about an *unloaded* state like
   Texas never gets treated as covered).
2. **Citation correctness** -- every citation in the model's `Sources:`
   block is independently re-verified against the real rulebook index
   (`server.get_rulebook_page`) or, for AO citations, a live OpenFEC
   lookup (`server.get_advisory_opinion`) -- not against what the model
   *claims* it found. A fabricated filename/page/AO number fails here
   even if the surrounding prose reads plausibly.

## Options

```
--case ID       Run only the given case id(s), repeatable (e.g. --case rb-01-individual-contribution-limit)
--report PATH   Also write full per-case results (including the full answer text) as JSON to PATH
```

Exit code is non-zero if any case fails, so it's scriptable, but nothing
currently invokes it automatically.

## Current scope, and what's deliberately not covered yet

- Only the **rulebook tools** (`search_rulebooks`, `get_rulebook_page`,
  `list_rulebook_sources`, `list_rulebook_jurisdictions`) have dedicated
  cases, plus a few negative controls that check the model correctly
  reaches for an OpenFEC/AO tool (or no tool) instead. OpenFEC tool
  coverage (candidates/committees/disbursements/filings) isn't graded
  beyond those negative controls.
- No LLM-as-judge grading -- everything is a deterministic assertion
  against real data, by design, so results are reproducible run to run.
- Not wired into `pytest` or CI. If/when a small, fast, fully-offline
  subset proves stable, promoting a handful of cases into
  `tests/test_demo.py`-style CI coverage would be the natural next step.
- `demo/app.py`'s citation-chip HTML rendering isn't exercised here --
  that's already covered separately by `tests/test_demo.py`.
