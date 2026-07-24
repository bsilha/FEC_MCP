# fec-mcp chat demo

A one-page Streamlit chat UI over the exact same tools the `fec-mcp` MCP
server exposes -- rulebook PDF search plus live OpenFEC data -- for showing
the project to coworkers without anyone needing to configure an MCP client.

This is a demo, not a product: it runs locally on your machine, has no
authentication, and isn't meant for coworkers to use unattended.

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
