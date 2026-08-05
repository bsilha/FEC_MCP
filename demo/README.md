# fec-mcp chat demo

A one-page Streamlit chat UI over the exact same tools the `fec-mcp` MCP
server exposes -- rulebook PDF search plus live OpenFEC data -- for showing
the project to coworkers without anyone needing to configure an MCP client.

This is a demo, not a product: it has no authentication and isn't meant for
coworkers to use unattended -- see "Deploy" below for running it somewhere
persistent instead of on your own machine.

## Setup

From the repo root, in the same virtual environment where you already ran
`pip install -e .`:

```bash
pip install -e ".[demo]"
```

You need a real [Anthropic API key](https://console.anthropic.com/) (this
calls the Claude API directly -- separate from `FEC_API_KEY`, which is for
OpenFEC):

```bash
export ANTHROPIC_API_KEY=your_key_here
```

(Or leave it unset and paste a key into the sidebar text field when the app
is running.)

## Run

```bash
streamlit run demo/app.py
```

This opens a browser tab with a chat box. It reuses `data/rulebooks/` and
your `FEC_API_KEY` exactly like the MCP server does -- same search index,
same live OpenFEC access, same jurisdiction coverage.

Each loaded PDF in the sidebar is a clickable link that opens the actual
source document in a new tab (via Streamlit's built-in static file server,
enabled in `.streamlit/config.toml`). On first run, `demo/app.py` mirrors
`data/rulebooks/` into `demo/static/rulebooks/` automatically -- this is
generated, not something to edit or commit (it's gitignored and re-syncs
itself whenever a PDF is added, changed, or removed).

A cited answer's sources render as clickable chips (jurisdiction/status
badge + link to the exact PDF page or advisory opinion), not just plain
text -- this is a demo-only addition to the system prompt
(`CITATION_FORMAT_ADDENDUM` in `demo/app.py`), not something that changes
what other MCP clients (VS Code, Claude Desktop) see.

The sidebar's "Try asking" buttons fill the chat input with a real example
question (edit or send as-is) -- see `EXAMPLE_QUESTIONS` in `demo/app.py`
to change them.

## Deploy (Streamlit Community Cloud)

Running it on your own machine means it's only up while you have a terminal
open. [Streamlit Community Cloud](https://streamlit.io/cloud) (free) will
host it persistently at a stable URL instead:

1. Push this repo to GitHub (already done if you're reading this from the
   repo) and sign in to <https://share.streamlit.io> with that GitHub
   account.
2. **New app** -> pick this repo/branch -> set the entrypoint to
   `demo/app.py`. Community Cloud installs from `requirements.txt` at the
   repo root (already set up to install `fec-mcp` itself plus the `demo`
   extra) -- no separate build config needed.
3. In the app's **Settings -> Secrets**, add only:
   ```toml
   FEC_API_KEY = "your_key_here"
   ```
   Deliberately **don't** add `ANTHROPIC_API_KEY` here -- leaving it unset
   means each visitor has to paste their own key into the sidebar before
   they can chat (their key is used for their session only, never stored
   or shared with other visitors), instead of everyone silently spending
   against one shared key you're on the hook for. Community Cloud exposes
   secrets as real environment variables, so `FEC_API_KEY` needs no code
   changes -- same env var the app already reads locally.
4. Deploy. First load will be slower than usual while the rulebook search
   index builds from the PDFs in `data/rulebooks/`; it's cached after that
   until the app restarts.

Since no Anthropic key lives on the server, there's no per-message cost
exposure from sharing the URL widely -- each visitor pays for their own
usage with their own key. Community Cloud can still restrict an app to
specific viewer emails (app settings -> **Sharing**) if you'd rather limit
who can even load the page.

## What it's for

Showing someone the actual behavior -- cited answers from the loaded PDFs,
live candidate/committee/disbursement lookups -- in a normal-looking chat
page instead of walking them through VS Code and an MCP config file. Each
response shows which tools were called (🔧 lines) so it's visible that
answers are coming from real search/API calls, not just the model's own
knowledge.

If this turns into something coworkers use regularly rather than a one-off
demo, the natural next step is deploying `fec-mcp` itself as a remote MCP
server so people connect to it from their own Claude client, rather than
scaling up this demo page.
