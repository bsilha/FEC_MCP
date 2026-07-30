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
3. In the app's **Settings -> Secrets**, add:
   ```toml
   ANTHROPIC_API_KEY = "your_key_here"
   FEC_API_KEY = "your_key_here"
   ```
   Community Cloud exposes these as real environment variables to the app,
   so no code changes are needed -- same env vars the app already reads
   locally.
4. Deploy. First load will be slower than usual while the rulebook search
   index builds from the PDFs in `data/rulebooks/`; it's cached after that
   until the app restarts.

**Before sharing the URL:** this puts a real Anthropic API key behind a
page anyone with the link can use, with no rate limiting or per-user cost
tracking -- every message costs real API spend. Community Cloud supports
restricting an app to specific viewer emails (app settings -> **Sharing**);
turn that on before handing out the link, or budget for open access
accordingly.

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
