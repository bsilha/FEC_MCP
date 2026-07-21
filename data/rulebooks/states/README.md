# State rulebooks

This directory is for state-level campaign-finance compliance PDFs, kept
separate from the federal (FEC) guides in `data/rulebooks/`.

## Convention

Add a subdirectory named after the state's lowercase two-letter USPS code,
and drop that state's official PDFs into it:

```
data/rulebooks/states/
  ca/
    fppc_manual.pdf
    contribution_limits.pdf
  ny/
    ...
```

Any PDF placed under `states/{state_code}/` is picked up automatically by
the search index (same mechanism as the federal guides) and tagged with
that state's code as its `jurisdiction`. There's no fixed list of expected
filenames -- each state's regulating agency (e.g. California's FPPC, New
York's Public Campaign Finance Board) publishes different documents in
different formats, so add whatever official guides are actually available
for a given state.

## Scope

This is entirely optional and incremental -- the server works fine with
zero states loaded (federal-only), one state, or many. There's no
requirement to cover all 50; add states as you actually need them.

Use `list_rulebook_jurisdictions` to see which states (if any) currently
have PDFs loaded.
