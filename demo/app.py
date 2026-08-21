"""Streamlit chat demo for fec-mcp.

Not a production app -- a quick, shareable demo of the same tools the MCP
server exposes (rulebook search + live OpenFEC data), so it can be shown to
coworkers without anyone needing to configure an MCP client. It reuses the
actual tool implementations in fec_mcp.server (no logic is duplicated here)
and wires them into Claude via the Anthropic API's tool runner.

Run with:
    streamlit run demo/app.py

Requires ANTHROPIC_API_KEY (a real API key, separate from the MCP server's
FEC_API_KEY) to be set in the environment, or entered in the sidebar.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import shutil
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

import streamlit as st
from anthropic import Anthropic, beta_tool

from fec_mcp import server
from fec_mcp.committee_roster import UNSET_STATUS, CommitteeRoster, cycle_horizon_months
from fec_mcp.rulebook_index import DEFAULT_RULEBOOKS_DIR
from fec_mcp.states import US_STATE_NAMES

MODEL = "claude-opus-5"
MAX_TOKENS = 4096

# Demo-only addition to server.INSTRUCTIONS (never edit server.INSTRUCTIONS
# itself for this): asks the model to end a cited answer with a fixed,
# machine-parseable citation block so the UI can render real clickable
# source chips instead of parsing free-form prose (fragile -- see
# _split_citations). This is deliberately NOT part of the shared
# server.INSTRUCTIONS constant, since that's also used by every other MCP
# client (VS Code, Claude Desktop): those clients have no UI to turn a
# "SOURCE | file | page | jurisdiction" line into a chip, so forcing this
# format on them would just dump robotic-looking lines into a normal chat.
CITATION_FORMAT_ADDENDUM = """

Demo UI addendum: when your answer cites a page from a rulebook PDF (from
search_rulebooks/get_rulebook_page) or an FEC Advisory Opinion (from
search_advisory_opinions/get_advisory_opinion), end the entire answer with
a line reading exactly "Sources:" followed by one citation per line, in
exactly this format and no other text on those lines:

SOURCE | <filename.pdf exactly as the tool returned it, e.g. candgui.pdf or states/ca/limits.pdf> | <page number> | <jurisdiction>
AO | <ao_no, e.g. 2014-02> | <status, e.g. Final> | <a full https://www.fec.gov URL if you have one from the tool results, otherwise leave this field blank>

Use the exact filename, page, jurisdiction, AO number, and status the
tools returned -- never invent, reformat, or abbreviate them. Only add
this block when you actually cited a specific rulebook page or advisory
opinion; omit it entirely for answers with no such citation (e.g. live
OpenFEC candidate/committee/disbursement data, or an answer saying nothing
relevant is loaded).

Do not paste a raw URL (e.g. a fec.gov PDF link) into the body of the
answer itself -- the Sources: block already renders as a clickable link
for each citation, so a URL in the prose is both redundant and, unlike
the Sources: block, not a real link there. You can still name what you're
citing in a sentence (e.g. "the controlling opinion is AO 2014-02"), just
don't repeat its URL.

Every claim in your answer about what a loaded document says, covers, or
requires must have a corresponding line in the Sources: block. This
applies to asides and offers just as much as to the main answer --
including claims about a jurisdiction other than the one the answer is
mainly about (e.g. ending a federal answer with "California's manuals
also cover this"). An uncited claim of that kind renders as plain prose
with no chip to click, so the reader has no way to check it. If you
haven't actually retrieved a page supporting such a claim, either search
for one first and cite it, or leave the claim out.
"""

NO_HEADINGS_ADDENDUM = """

Demo UI addendum: this chat renders each answer inside a compact, few-
hundred-pixel-wide message bubble, not a document -- do not use Markdown
headings (#, ##, ###, etc.) anywhere in your answer, since a heading
renders far larger than the bubble is designed for and dominates it. Use
a bolded lead-in sentence or plain paragraph breaks for structure instead.
"""


def _today_addendum() -> str:
    """Computed fresh per call (unlike the other *_ADDENDUM constants above,
    which are fixed strings) since it has to reflect the real current date,
    not whatever date happened to be current when this module was imported.

    The raw Anthropic API has no built-in notion of "today" -- that's
    something client-side scaffolding (Claude Desktop, Claude Code) adds on
    top of it, which this demo's direct client.beta.messages.tool_runner()
    call never gets. Without this, the model has no way to know which
    upcoming deadline is actually next and has to hedge instead of
    answering -- confirmed live: asked for "the next FEC quarterly
    reporting deadlines" without this, it returned the full deadline list
    with "I don't have a reliable read on today's date, so check this list
    against your current date" instead of just naming the next one.
    """
    return f"\n\nDemo UI addendum: today's date is {date.today().isoformat()}.\n"

# Brand colors matched to the internal Aristotle Campaign Manager app --
# eyeballed from a dashboard + logo screenshot the user provided, confirmed
# before this was applied. Not the exact brand hex values (those weren't
# available), so double-check against a real style guide if one shows up.
BRAND_NAVY_DARK = "#1B2836"
BRAND_NAVY_MID = "#263A4D"
BRAND_STEEL = "#4B6B85"
BRAND_ACCENT = "#1E88C7"
BRAND_PURPLE = "#6E71C9"  # source citation badges -- matches the chart's first data series
BRAND_RED = "#C0392B"  # the one thing on the page that needs fixing before it works
BRAND_TEAL = "#3FC7C9"  # AO status badges -- matches the chart's second data series


# Lifecycle statuses, in the order someone actually moves through them,
# with the consequence of each spelled out.
#
# The consequence text is the most valuable thing in this whole view. The
# status silently decides the entire deadline set -- pick "lost the
# primary" and the pre-general and post-general vanish -- and the raw enum
# values ("won_primary") give no hint of that. Someone picking from a list
# should be able to see what their answer changes before they commit to it.
#
# Each row carries the consequence twice, because a dropdown shows its
# selected option in a fixed-width box and clips whatever overruns it. The
# short form rides along inside the option text, where it survives that
# clipping; the long form lives in the column header's tooltip, which has
# no width limit. Verified live: at this column width the long form was
# cut at "Adds the 12G pre-general and 3", which is worse than no hint --
# it looks like a value that ran out rather than a sentence.
STATUS_CHOICES: list[tuple[str, str, str, str]] = [
    ("in_primary", "Still in the primary", "pre-primary only",
     "Pre-primary report only; no general-election reports yet"),
    ("won_primary", "Won the primary", "adds 12G + 30G",
     "Adds the 12G pre-general and 30G post-general"),
    ("lost_primary", "Lost the primary", "no general reports",
     "Drops both general-election reports; regular filing continues"),
    ("won_general", "Won the general", "30G still due",
     "Post-general still due; continues into the next cycle"),
    ("lost_general", "Lost the general", "30G still due",
     "Post-general still due; filing continues until termination"),
    ("terminating", "Winding down", "regular reports only",
     "Regular reports only, until the termination report"),
    ("ongoing", "PAC or party committee", "no primary or general",
     "No primary or general of its own to win or lose"),
]

_STATUS_LABELS = {value: label for value, label, _, _ in STATUS_CHOICES}
_STATUS_HINTS = {value: hint for value, _, hint, _ in STATUS_CHOICES}
_STATUS_DETAIL = {value: detail for value, _, _, detail in STATUS_CHOICES}

VIEW_SWITCH_CSS = """
<style>
/* The Chat/Deadlines switch is navigation, not another paragraph of the
   page intro it sits directly beneath -- without space above it, it reads
   as part of that sentence. The rule below it gives the same "content
   starts here" edge a tab bar would.

   Targeted by the container around the control, not the control itself:
   st.segmented_control renders with data-testid="stButtonGroup" in this
   Streamlit version, so the "stSegmentedControl" selector this rule was
   originally written against matched nothing and the spacing it describes
   was never actually on screen -- the switch sat flush against the last
   line of the intro paragraph. Verified live in the DOM. Both selectors
   are kept so the rule survives Streamlit renaming it either way. */
[data-testid="stElementContainer"]:has(> [data-testid="stButtonGroup"]),
[data-testid="stSegmentedControl"] {
    margin: 18px 0 4px;
    padding-bottom: 14px;
    border-bottom: 1px solid #d8dee3;
    /* The container shrink-wraps the two buttons, so without this the
       rule underlines the switch instead of dividing the page. */
    width: 100%;
}
</style>
"""

# How old a status has to be before the UI questions it. Roughly a
# quarter: long enough that a primary or general has plausibly happened
# since, short enough to catch it before the next filing.
_STATUS_STALE_AFTER_DAYS = 90

DEADLINE_CSS = f"""
<style>
/* Reclaim the top of the page. Rendered only on this view, so the chat
   page keeps its own spacing.

   Two separate costs, both measured live. Streamlit's default 96px of
   block padding was sized for a page that opens with a title; this one
   opens with a working list, and the fixed header bar above it already
   ends well clear of the content. And every st.markdown() that injects
   nothing but a <style> block still renders an element container --
   zero-height, but each one consumes a 16px flex gap, which came to 64px
   of nothing before the first real widget. Hiding those containers does
   not disable their rules: a <style> element applies whether or not its
   ancestors are displayed. */
[data-testid="stMainBlockContainer"] {{ padding-top: 1.6rem; }}
[data-testid="stElementContainer"]:has(style) {{ display: none; }}
/* Roster rows are a table, not a form -- one line each, so eight
   committees stay readable and the agenda below stays on screen. */
.fec-roster-head {{
    font-size: 0.66rem; font-weight: 700; letter-spacing: .06em;
    text-transform: uppercase; color: {BRAND_STEEL};
    border-bottom: 1px solid #e3e8ec; padding-bottom: 4px;
}}
/* A hint hanging off a heading: deliberately not shouting like the
   heading it follows. */
.fec-roster-head span {{
    border-bottom: 1px dotted {BRAND_STEEL}; cursor: help;
    text-transform: none; letter-spacing: 0; font-weight: 600; opacity: .8;
}}
/* ...but when the heading word ITSELF carries the tooltip, it still has
   to read as one of the headings. */
.fec-roster-head span.hdr {{
    text-transform: uppercase; letter-spacing: .06em; font-weight: 700; opacity: 1;
}}
.fec-roster-name {{
    font-weight: 700; font-size: 0.86rem; color: {BRAND_NAVY_DARK};
    line-height: 1.25; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
.fec-roster-sub {{ font-size: 0.72rem; color: {BRAND_STEEL}; line-height: 1.3; }}
/* Amber, not red: a status nobody has revisited is a question, not a
   failure -- it may well still be right. */
.fec-roster-aged {{ color: #8a5a12; font-weight: 600; }}
/* One deadline per row. Date first and fixed-width so the column reads as
   a timeline when scanned vertically, which is how someone checks "what's
   next" -- the thing this view exists to answer. */
.fec-dl-row {{
    display: flex; align-items: center; gap: 10px;
    padding: 8px 11px; border-bottom: 1px solid #eef1f4;
    font-size: 0.86rem; background: #fff;
}}
.fec-dl-row:first-child {{ border-radius: 7px 7px 0 0; }}
.fec-dl-row:last-child {{ border-bottom: none; border-radius: 0 0 7px 7px; }}
.fec-dl-wrap {{ border: 1px solid #d8dee3; border-radius: 8px; overflow: hidden; }}
.fec-dl-date {{ font-weight: 700; width: 84px; flex-shrink: 0; color: {BRAND_NAVY_MID}; }}
/* Fixed width so committee names line up as a column down the page --
   with several committees, "whose is this" is what gets scanned for. */
.fec-dl-who {{
    width: 200px; flex-shrink: 0; font-weight: 600; font-size: 0.79rem;
    color: {BRAND_STEEL}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
.fec-dl-name {{ flex: 1; color: {BRAND_NAVY_DARK}; }}
.fec-dl-month {{
    background: #eef2f6; padding: 5px 11px; font-size: 0.66rem; font-weight: 700;
    letter-spacing: .06em; color: {BRAND_STEEL}; text-transform: uppercase;
}}
.fec-dl-kind {{
    font-size: 0.62rem; font-weight: 700; letter-spacing: .03em;
    padding: 2px 7px; border-radius: 4px; background: #e8eef4; color: {BRAND_STEEL};
    white-space: nowrap;
}}
.fec-dl-kind.gen {{ background: #e7e8f7; color: {BRAND_PURPLE}; }}
/* One line, directly under the agenda heading: this list is short a
   committee, and which. Deliberately quiet -- the red box on the empty
   control is what asks to be fixed; this only keeps the list honest. */
.fec-dl-gap {{
    font-size: 0.76rem; color: {BRAND_RED}; margin: -2px 0 8px;
}}
/* Deliberately amber rather than red: an unconfirmed deadline is a
   decision someone needs to make, not an error the app has hit. */
.fec-dl-flag {{
    font-size: 0.62rem; font-weight: 700; letter-spacing: .03em;
    padding: 2px 7px; border-radius: 4px; background: #fff4e5; color: #7a5320;
    white-space: nowrap;
}}
</style>
"""


def _us_date(value: Any) -> str:
    """An ISO date as MM-DD-YYYY, the way US compliance calendars read.

    Anything unparseable is passed through untouched rather than blanked:
    a date this cannot read is still more use on screen than nothing.
    """
    try:
        return date.fromisoformat(str(value)).strftime("%m-%d-%Y")
    except (TypeError, ValueError):
        return str(value or "")


def _agenda_row_html(row: dict[str, Any]) -> str:
    """One deadline in the combined agenda.

    The committee name is its own fixed-width column rather than part of
    the description: with several committees, "whose is this" is the first
    thing being scanned for, and it has to line up down the page.
    """
    family = (row.get("report_type") or "").replace("_", "-").upper() or "DEADLINE"
    is_general = "GENERAL" in family
    flag = "" if row.get("certain", True) else '<span class="fec-dl-flag">CONFIRM</span>'
    # Committee names are clipped to keep the column aligned, and long ones
    # do get clipped -- so the full name hangs off the element as a title.
    who = str(row.get("_committee") or "")
    return (
        '<div class="fec-dl-row">'
        f'<span class="fec-dl-date">{html.escape(_us_date(row.get("date")))}</span>'
        f'<span class="fec-dl-who" title="{html.escape(who, quote=True)}">{html.escape(who)}</span>'
        f'<span class="fec-dl-name">{html.escape(str(row.get("deadline") or ""))}</span>'
        f"{flag}"
        f'<span class="fec-dl-kind{" gen" if is_general else ""}">{html.escape(family)}</span>'
        "</div>"
    )


# Rulebook jurisdictions come back from list_rulebook_sources() as lowercase
# two-letter USPS codes (e.g. "ca") -- fine for internal filtering, but per
# explicit user feedback a sidebar heading reading "CA" is less scannable
# than "California" once there's more than one or two states loaded.
def _jurisdiction_label(jurisdiction: str) -> str:
    """Sidebar heading text for a jurisdiction code, e.g. "ca" -> "California".

    Deliberately Title Case for every jurisdiction including "Federal" --
    not "FEDERAL" -- so every heading in the sidebar list shares the exact
    same capitalization as well as the same font/size (both driven by
    st.expander's own label styling, same widget for every row), per
    explicit user feedback that headings should look uniform rather than
    federal reading as visually distinct from the states.
    """
    if jurisdiction == "federal":
        return "Federal"
    return US_STATE_NAMES.get(jurisdiction.upper(), jurisdiction.upper())


def _jurisdiction_sort_key(jurisdiction: str) -> tuple:
    """Federal always first (per explicit user request), states after it in
    alphabetical order by their *displayed* name -- not their two-letter
    code -- so the visual order always matches what's on screen."""
    return (0, "") if jurisdiction == "federal" else (1, _jurisdiction_label(jurisdiction))


HEADER_CSS = f"""
<style>
/* In the mockup, the header sits above BOTH the sidebar and the chat
   column, as one bar spanning the whole app -- but Streamlit renders the
   sidebar and "main" content as two separate regions (confirmed via live
   DOM inspection: stAppViewContainer contains both as siblings), and
   st.markdown() from inside main() can only render into the main side.
   There's no API to inject a real sibling of the sidebar, so this uses
   position: fixed instead: taken out of normal document flow entirely and
   positioned relative to the browser viewport, so it visually overlays
   both regions regardless of where it's actually nested in the DOM.
   top: 0 pins it to the very top of the viewport -- deliberately
   painting over Streamlit's own native toolbar (Deploy button, the
   three-dot menu) rather than sitting below it, per explicit user
   feedback that a white strip above the navy bar read as leftover
   whitespace rather than a full-bleed header. z-index needed a second
   look, caught by actually screenshotting this rather than trusting
   bounding-box math alone: an initial z-index of 999000 (just below the
   native toolbar's 999990) left the navy bar invisible over the sidebar,
   because stSidebar itself carries z-index 999991 -- higher than the
   toolbar -- and painted over it there. 999999 outranks both, so the
   overlay now wins everywhere it covers (y=0-137, see the margin-top
   rules below for where sidebar/main content starts beneath it). */
.fec-header-overlay {{
    position: fixed; top: 0; left: 0; right: 0; z-index: 999999;
}}
/* Covering the native toolbar (stHeader) entirely also hides
   stExpandSidebarButton -- the real control Streamlit renders there to
   re-open a collapsed sidebar -- leaving no way back in once collapsed.
   An earlier attempt promoted stHeader itself above the overlay so the
   button could show through its native spot, in the navy bar -- z-index
   alone on the button can't do it, since position: fixed changes where
   an element is *drawn*, not which stacking context it belongs to, and
   the button is nested inside stToolbar/stHeader, which Streamlit pins
   at z-index 999990, below our overlay's 999999 (confirmed live via
   document.elementsFromPoint()). That approach worked, but per explicit
   user feedback it put an icon inside the branded navy bar, which
   wasn't wanted, and it left a second, stray icon visible beneath the
   header too (most likely this file's own [data-testid="stSidebarHeader"]
   rule below failing to slide fully off-screen in every browser --
   its off-screen position depends on inheriting stSidebar's own
   collapse transform as its containing block, which isn't guaranteed
   pixel-perfect everywhere).
   Simpler fix: don't fight stHeader's stacking context at all. Give
   stExpandSidebarButton its own position: fixed with an explicit
   top/left, the same trick already used for the sidebar's own collapse
   button below -- since it now renders at y=93+ instead of inside
   stHeader's y=0-60 native slot, it no longer spatially overlaps the
   overlay (which only covers y=0-77), so there's no z-index fight left
   to have; it only needs to clear stMainBlockContainer's own content
   at that spot, a much lower bar. Native background is transparent
   (confirmed live) and the button reads as a bare icon directly on the
   white page without it, so a light card-style background/shadow was
   added to match the rest of this file's chip/citation styling rather
   than leaving it looking unstyled. */
[data-testid="stExpandSidebarButton"] {{
    position: fixed;
    top: 93px; left: 14px;
    z-index: 3;
    background: #ffffff;
    border: 1px solid #D8DEE3;
    border-radius: 6px;
    box-shadow: 0 1px 3px rgba(0,0,0,.15);
}}
.fec-topbar {{
    background: linear-gradient(90deg, {BRAND_NAVY_DARK}, {BRAND_NAVY_MID});
    color: #fff; padding: 10px 18px;
    display: flex; align-items: center; gap: 10px; margin-bottom: 0;
}}
.fec-topbar .badge {{
    width: 24px; height: 24px; border-radius: 5px; background: {BRAND_NAVY_MID};
    border: 1px solid rgba(255,255,255,.25);
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: 13px; flex-shrink: 0;
}}
.fec-topbar .name {{ font-weight: 700; font-size: 15px; letter-spacing: .01em; }}
.fec-topbar .name span {{ font-weight: 400; opacity: .7; margin-left: 6px; font-size: 12px; }}
.fec-subbar {{
    background: {BRAND_STEEL}; color: #fff; font-size: 12px; font-weight: 600;
    padding: 7px 18px; letter-spacing: .02em;
}}
/* The fixed overlay is ~77px tall (44px topbar + 33px subbar, confirmed
   live) -- push the sidebar's and main content's own content down by that
   much more, on top of whatever padding they already have for
   Streamlit's native toolbar, so nothing renders underneath the overlay. */
/* Pushing stSidebarUserContent's *content* down with margin-top wasn't
   enough on its own: the actual scrollable element is its parent,
   stSidebarContent, which still spanned the full sidebar height (y=0 to
   the bottom) even after the content inside it was pushed down --
   confirmed by a screenshot showing the scrollbar track/thumb still
   running the full height, visibly crossing right through the navy
   header (this is a Linux/Chromium overlay-scrollbar quirk: the track is
   tied to the scroll box's own bounding rect, not to how far down its
   content is pushed, and it paints above the navy overlay's z-index
   regardless). Shifting stSidebarContent itself (not just its content)
   down by the header's height, with a matching height reduction so it
   still ends at the same bottom edge instead of overflowing past the
   viewport, moves the scrollbar's actual box down along with the content
   instead of leaving its track behind at y=0.
   That alone regressed the collapse-arrow button (stSidebarHeader, a
   DOM child of stSidebarContent): dragging the whole scroll box down
   dragged the button down with it, off of its native y=0-60 spot and
   into the middle of the now-blank space above the real content --
   caught by screenshotting the sidebar and seeing an empty white box
   where "YOUR ANTHROPIC API KEY" used to start immediately.
   position: absolute didn't fix it either: stSidebarContent itself is
   already position: relative (confirmed live), so it -- not stSidebar --
   became the containing block, and its own overflow-y: auto then clipped
   the button anyway since a negative top pushing it above the box's own
   padding edge falls outside that box's visible scroll region. Only
   position: fixed actually escapes both the wrong containing block and
   the overflow clip, pinning the button to the viewport itself the same
   way the navy overlay above is pinned; confirmed via
   document.elementFromPoint() actually hitting stSidebarHeader at (150,
   20), not just trusting getBoundingClientRect() math (which had looked
   right under the position: absolute attempt too, despite the button
   being invisible there). width: 300px hardcodes the sidebar's default
   width since position: fixed can't inherit an auto width from its old
   flex parent -- matches the fixed pixel offsets already used throughout
   this file for a resizable-but-not-actually-resized-in-practice sidebar. */
[data-testid="stSidebarContent"] {{
    margin-top: 77px;
    height: calc(100% - 77px);
}}
[data-testid="stMainBlockContainer"] {{ margin-top: 77px; }}
/* Reserving a dedicated 60px row for the collapse-arrow button
   (stSidebarHeader) below the header left a visible white bar sitting
   above "YOUR ANTHROPIC API KEY" -- per explicit user feedback, that
   gap should go away entirely rather than just changing color, so
   sidebar/main content now both start right at 77px (the header's own
   height), same as before this button existed. The button no longer
   gets its own row: no background (so there's nothing to show when
   it's not being hovered -- Streamlit already keeps the icon itself
   invisible until :hover, confirmed live), and it overlaps the first
   bit of real content instead of pushing it down.
   This was originally position: fixed (viewport-relative, hardcoded
   width: 300px to match the sidebar's default), but that doesn't track
   the sidebar's actual width -- confirmed live by dragging the sidebar
   wider and watching the button stay planted at the old 300px mark
   instead of following the new edge, per an explicit user screenshot
   showing exactly that. position: absolute fixes it: stSidebarContent
   (its actual DOM parent) is already position: relative, so left: 0;
   right: 0 (no hardcoded width at all) resolves against *that* box's
   real, live width instead of the viewport's -- confirmed live that
   the button's x-position follows a drag-resize from 300px to 500px to
   400px correctly, landing at the new right edge every time. This
   still doesn't hit the overflow-clipping problem an earlier absolute
   attempt ran into (see the collapsed-state comment below): that one
   needed a *negative* top to climb back above stSidebarContent's own
   top edge, which its overflow-y: auto clipped: top: 0 here stays
   entirely inside the box's own visible area, nothing to clip. */
[data-testid="stSidebarHeader"] {{
    position: absolute;
    top: 0; left: 0; right: 0;
    z-index: 2;
    pointer-events: none;
}}
/* The rule above overlaps the real sidebar widgets that render right
   below it (they share the same y=77+ starting point -- that's the
   whole point of floating it instead of reserving a row), and that
   silently broke every widget underneath: confirmed live that clicking
   the API key input, its help tooltip, and the eye/show-password
   toggle all hit stSidebarHeader instead via elementFromPoint, even
   though nothing about it is visible there -- an element with no drawn
   content still intercepts pointer events unless told not to. This
   went unnoticed through every earlier round of this file's testing
   because that testing checked screenshots and elementFromPoint on
   THIS button, never whether the real widgets underneath remained
   clickable.
   pointer-events: none on the container makes it fully transparent to
   the mouse -- clicks now fall through to the real input/tooltip/eye
   button beneath. The collapse button itself needs pointer-events:
   auto to opt back in, otherwise it would stop working too (verified
   live: hovering directly over the button still reveals and clicks it
   correctly with this override in place; hovering elsewhere in the
   now-transparent container does nothing, which is fine since there's
   no visible target there to interact with anyway). */
[data-testid="stSidebarCollapseButton"] {{ pointer-events: auto; }}
/* Explicitly hides this button once the sidebar reports
   aria-expanded="false", rather than relying on its position rule
   above to push it off-screen or out of view on its own. This dates
   back to when the button was position: fixed and its off-screen
   position depended on inheriting stSidebar's collapse transform as
   its containing block -- fragile in a way that once produced a stray
   duplicate icon a user could see but this file couldn't reproduce
   locally. Now that it's position: absolute inside stSidebarContent,
   there's a simpler reason to keep this: stSidebarContent's own
   height: calc(100% - 77px) rule below only applies while expanded
   (nothing un-sets it on collapse), so without this the button would
   still be sitting at the top of that box, just with nothing left to
   click through to. Unconditional and independent of whatever the
   sidebar's actual collapsed dimensions turn out to be. */
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarHeader"] {{
    display: none;
}}
/* stSidebarHeader's floating footprint (y=77-137, see above) overlaps
   the very top of the real content that starts right where it does --
   confirmed live via bounding boxes that the collapse button (x=258-290,
   y=93-121) genuinely intersects both the API key help icon (x=254-270,
   y=81-97) and the eye/show-password toggle (x=237-269, y=117-133),
   not just sitting close to them. Per explicit user feedback ("more
   spaced out"), pushing just stSidebarUserContent (the real widgets,
   not stSidebarHeader or stSidebarContent) down by a further 40px --
   roughly the button's own height plus breathing room -- clears both
   without reserving anywhere near Streamlit's native 76px
   (60px + 16px margin) for this row, which is what caused the original
   "white gap" complaint two rounds ago. Confirmed live the button and
   the help icon no longer overlap after this (button ends at y=121,
   help icon starts at y=121). */
[data-testid="stSidebarUserContent"] {{
    margin-top: 40px;
}}
/* Sized well above the mockup's 19px/13.5px: the mockup was a small,
   contained preview card, but at real full-width scale the same sizes
   read as tiny text floating in a mostly-empty page -- per explicit
   user feedback ("should scale up... too much whitespace"). */
.fec-page-heading h2 {{ font-size: 30px; font-weight: 700; margin: 0 0 8px; }}
.fec-page-heading p {{ font-size: 16px; color: #5B6B7A; margin: 0; max-width: 62ch; }}

.fec-side-label {{
    font-size: 10.5px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .07em; color: #5B6B7A; margin: 16px 0 8px;
}}
/* st.text_input's own label ("Your Anthropic API key") isn't something we
   render ourselves -- Streamlit owns that markup -- but stWidgetLabel is
   a stable, documented test id (confirmed via live DOM inspection, same
   as the chat-bubble testids above), so it's safe to restyle to match
   .fec-side-label rather than leaving it in Streamlit's default style. */
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p {{
    font-size: 10.5px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .07em; color: #5B6B7A;
}}
/* Streamlit overlays its own "Press Enter to apply" hint (shown while
   a typed value hasn't been submitted yet) directly on top of the
   input via position: absolute -- confirmed live this isn't reserving
   its own row, it's genuinely drawn over the input's own bottom-right
   corner, which collided with the eye/show-password button for a
   fully-typed API key (fixed in an earlier round by giving it
   clearance and a background pill). Per further user feedback, rather
   than fine-tune that overlay further, move it below the input
   entirely: position: static drops it out of the overlay and back
   into normal document flow, where it's already the last child of
   stTextInput (confirmed via live DOM inspection) -- so no manual
   top/left math is needed, it simply renders as the next line after
   the input, the same way any other block-level sibling would. The
   parent's height grows to fit it automatically, which pushes
   "Jurisdictions loaded" down slightly whenever this hint is showing;
   confirmed live that's the full extent of the effect, and that it
   still looks right with both a long and a short typed value. */
[data-testid="stSidebar"] [data-testid="InputInstructions"] {{
    position: static;
    background: transparent;
    padding: 2px 0 0;
    display: block;
    text-align: right;
}}

/* main() sets layout="wide" so the header/content aren't capped at
   Streamlit's default "centered" width (736px -- confirmed via live
   inspection, well under the mockup's own arbitrary 1180px). Deliberately
   no max-width cap here at all: went with a 1180px cap first (matching
   the mockup's own choice) but that still looked boxed-in on a wide
   monitor, so this is genuinely edge-to-edge -- fills whatever the
   browser window's actual width is, chosen over capping it, on the
   understanding that very long answers may run wider than ideal on an
   ultra-wide monitor as a tradeoff. */
</style>
"""

# Streamlit's chat bubble background/avatar aren't reachable through the
# [theme] config, and its own CSS classes are hashed per build (e.g.
# "st-emotion-cache-1fee4w7") so they're not safe to target -- but its
# data-testid attributes are Streamlit's own stable, documented markers for
# testing, confirmed present in this version via a live inspection
# (stChatMessage, stChatMessageAvatarUser/Assistant). Distinguishing user
# vs. assistant this way, rather than nth-child/order, works regardless of
# how many messages exist or their order.
CHAT_BUBBLE_CSS = f"""
<style>
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {{
    background: {BRAND_ACCENT}; border-radius: 10px;
}}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) * {{
    color: #fff !important;
}}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"]) {{
    background: #F2F4F6; border: 1px solid #D8DEE3; border-left: 3px solid {BRAND_STEEL};
    border-radius: 10px;
}}
[data-testid="stChatMessageAvatarUser"], [data-testid="stChatMessageAvatarAssistant"] {{
    display: none;
}}
/* Markdown headings (#, ##, ...) render at document-scale font sizes by
   default -- fine in a document, but a chat bubble is a few hundred px
   wide, so an h1 towers over everything else in it. NO_HEADINGS_ADDENDUM
   asks the model not to use headings here at all; this is the CSS-level
   backstop for whenever it does anyway (or a citation/tool result happens
   to contain one), so a heading is still visually distinct (bold) without
   dominating the bubble. */
[data-testid="stChatMessageContent"] h1,
[data-testid="stChatMessageContent"] h2,
[data-testid="stChatMessageContent"] h3,
[data-testid="stChatMessageContent"] h4,
[data-testid="stChatMessageContent"] h5,
[data-testid="stChatMessageContent"] h6 {{
    font-size: 1rem; font-weight: 700; margin: 0.5em 0 0.25em;
}}
</style>
"""

CITATION_CSS = f"""
<style>
.fec-cite-row {{ margin-top: 6px; }}
.fec-cite {{
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 0.78rem; padding: 3px 9px; margin: 2px 6px 2px 0;
    border-radius: 6px; border: 1px solid rgba(49, 51, 63, 0.2);
    text-decoration: none; color: inherit;
}}
a.fec-cite:hover {{ border-color: {BRAND_ACCENT}; }}
.fec-cite-badge {{
    font-weight: 700; font-size: 0.66rem; letter-spacing: .03em;
    padding: 1px 5px; border-radius: 4px; color: #fff;
}}
.fec-cite-badge.fec-cite-badge-source {{ background: {BRAND_PURPLE}; }}
.fec-cite-badge.fec-cite-badge-ao {{ background: {BRAND_TEAL}; }}
.fec-cite-static {{ opacity: 0.85; }}
</style>
"""

# Shown as clickable "Try asking" chips in the sidebar. Picked to cover
# each tool family (rulebook search, live OpenFEC data, advisory opinions,
# reporting calendar) plus one state-jurisdiction question, since that's a
# real edge case this tool handles correctly (only answers for jurisdictions
# that actually have PDFs loaded) that a naive tool wouldn't.
EXAMPLE_QUESTIONS = [
    "What's the individual contribution limit to a candidate committee this cycle?",
    "Search advisory opinions about cryptocurrency donations",
    "What are the next FEC quarterly reporting deadlines?",
    "What's the contribution limit for a California state assembly race?",
]

# Streamlit's built-in static file server (enabled in .streamlit/config.toml)
# resolves its "static" folder relative to *this script's own directory*
# (demo/static/), not the repo root or the directory `streamlit run` was
# invoked from -- confirmed by running it: pointing this at the repo root
# instead produced "no static folder found at .../demo/static". Served at
# /app/static/<path>. _sync_static_pdfs mirrors data/rulebooks/ here so
# rulebook PDFs get a real, clickable URL -- needed to link a citation
# straight to its page (browsers' built-in PDF viewers honor a #page=N URL
# fragment).
STATIC_RULEBOOKS_DIR = Path(__file__).resolve().parent / "static" / "rulebooks"


def _sync_static_pdfs() -> None:
    """Mirror data/rulebooks/**/*.pdf into static/rulebooks/.

    Only copies new/changed files (by size+mtime) and deletes stale copies
    of PDFs no longer in data/rulebooks/, so this is cheap to call on every
    Streamlit rerun and self-heals whenever PDFs are added, replaced, or
    removed -- no manual sync step needed.
    """
    if not DEFAULT_RULEBOOKS_DIR.exists():
        return

    sources = {
        p.relative_to(DEFAULT_RULEBOOKS_DIR): p for p in DEFAULT_RULEBOOKS_DIR.rglob("*.pdf")
    }

    if STATIC_RULEBOOKS_DIR.exists():
        for existing in STATIC_RULEBOOKS_DIR.rglob("*.pdf"):
            if existing.relative_to(STATIC_RULEBOOKS_DIR) not in sources:
                existing.unlink()

    for rel, src_path in sources.items():
        dest_path = STATIC_RULEBOOKS_DIR / rel
        src_stat = src_path.stat()
        if dest_path.exists():
            dest_stat = dest_path.stat()
            if dest_stat.st_size == src_stat.st_size and dest_stat.st_mtime == src_stat.st_mtime:
                continue
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest_path)


def _pdf_url(source: str, page: int | None = None) -> str:
    """URL for a rulebook PDF served via Streamlit's static file server.

    Appending "#page=N" is a standard convention browsers' built-in PDF
    viewers (Chrome, Firefox, Edge, Safari) honor to open straight to that
    page -- no special viewer needed. `page` is the PDF's own internal page
    order (the same number search_rulebooks/get_rulebook_page report), so
    the link always lands on the exact page being cited; it may not match
    a page number physically printed on the page itself if the document has
    an unnumbered cover or table of contents.
    """
    url = f"/app/static/rulebooks/{quote(source)}"
    if page:
        url += f"#page={page}"
    return url


def _run_async(coro_fn, /, **kwargs) -> Any:
    """Run one of fec_mcp.server's async (OpenFEC-backed) tool functions.

    Each call gets a fresh event loop (asyncio.run), so anything the
    server module cached against a previous, now-closed loop has to be
    dropped first.

    That is two things, not one. The OpenFECClient wraps an
    httpx.AsyncClient, which raises a cross-event-loop error if reused.
    And _client_lock is an asyncio.Lock, which binds itself to the first
    loop that contends for it and then refuses every other one:

        RuntimeError: <asyncio.locks.Lock ...> is bound to a different
        event loop

    The lock hid for a long time because asyncio.Lock.acquire() has an
    uncontended fast path that never looks at the loop at all -- so a
    lock with no waiters works across loops by accident. It only fails
    once a waiter is left queued by a loop that closed underneath it,
    which is why the traceback reports "waiters:1" and why this surfaced
    only after the roster began issuing several OpenFEC calls per rerun.

    The lock is correct for the real MCP server, which runs one loop for
    its lifetime; it is this demo's loop-per-call pattern that it cannot
    survive. So it is replaced here rather than removed there.
    """
    server._openfec_client = None
    server._client_lock = asyncio.Lock()
    return asyncio.run(coro_fn(**kwargs))


def _json(result: dict[str, Any]) -> str:
    return json.dumps(result, default=str)


def _md(text: str) -> str:
    """Escape literal '$' before handing text to st.markdown.

    st.markdown renders anything between a pair of '$' as LaTeX math, and
    campaign-finance answers are full of dollar amounts (e.g. "$3,500 ...
    $5,000") -- unescaped, everything between the first and second '$' in a
    message silently renders as a math expression in serif italic type
    instead of plain text.
    """
    return text.replace("$", "\\$")


def _split_citations(text: str) -> tuple[str, list[dict[str, str]]]:
    """Split a model answer into (prose, citations) via the trailing
    "Sources:" block CITATION_FORMAT_ADDENDUM asks the model to emit.

    Degrades gracefully rather than risking a garbled partial parse: if
    there's no "Sources:" marker, or nothing under it parses into a
    well-formed citation line, returns the full original text unchanged
    with an empty citation list.
    """
    marker_idx = text.rfind("Sources:")
    if marker_idx == -1:
        return text, []

    prose = text[:marker_idx].rstrip()
    block = text[marker_idx + len("Sources:") :]

    citations: list[dict[str, str]] = []
    for line in block.splitlines():
        line = line.strip().lstrip("-*").strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if parts[0] == "SOURCE" and len(parts) == 4:
            citations.append(
                {"kind": "source", "filename": parts[1], "page": parts[2], "jurisdiction": parts[3]}
            )
        elif parts[0] == "AO" and len(parts) in (3, 4):
            citations.append(
                {
                    "kind": "ao",
                    "ao_no": parts[1],
                    "status": parts[2],
                    "url": parts[3] if len(parts) == 4 else "",
                }
            )
        # Any other line under "Sources:" (stray commentary, a malformed
        # row) is silently skipped rather than crashing the render.

    if not citations:
        return text, []
    return prose, citations


def _citation_chip_html(c: dict[str, str]) -> str:
    if c["kind"] == "source":
        try:
            page_int: int | None = int(c["page"])
        except ValueError:
            page_int = None
        href = _pdf_url(c["filename"], page_int)
        label = html.escape(f"{c['filename']}, p. {c['page']}")
        badge = html.escape(c["jurisdiction"].upper())
        return (
            f'<a class="fec-cite" href="{html.escape(href)}" target="_blank" rel="noopener">'
            f'<span class="fec-cite-badge fec-cite-badge-source">{badge}</span>{label}</a>'
        )

    # kind == "ao"
    label = html.escape(f"AO {c['ao_no']}")
    badge = html.escape(c["status"].upper())
    if c["url"]:
        return (
            f'<a class="fec-cite" href="{html.escape(c["url"])}" target="_blank" rel="noopener">'
            f'<span class="fec-cite-badge fec-cite-badge-ao">{badge}</span>{label}</a>'
        )
    return (
        f'<span class="fec-cite fec-cite-static">'
        f'<span class="fec-cite-badge fec-cite-badge-ao">{badge}</span>{label}</span>'
    )


def _render_citations(citations: list[dict[str, str]]) -> None:  # pragma: no cover -- Streamlit call
    if not citations:
        return
    chips = "".join(_citation_chip_html(c) for c in citations)
    st.markdown(f'<div class="fec-cite-row">{chips}</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Tool wrappers -- thin @beta_tool shims around fec_mcp.server's real tool
# functions, so this demo and the MCP server share one implementation.
# ---------------------------------------------------------------------------


@beta_tool
def list_rulebook_jurisdictions() -> str:
    """List every jurisdiction with rulebook PDFs loaded, e.g. "federal" and
    any state codes like "ca", "ny". Always call this before answering a
    state-specific compliance question, to check whether that state's
    rulebooks are actually loaded rather than assuming coverage.
    """
    return _json(server.list_rulebook_jurisdictions())


@beta_tool
def list_rulebook_sources(jurisdiction: str | None = None) -> str:
    """List the rulebook PDFs currently loaded and searchable.

    Args:
        jurisdiction: Optional filter, "federal" or a lowercase two-letter
            state code (e.g. "ca"). Omit to list everything.
    """
    return _json(server.list_rulebook_sources(jurisdiction=jurisdiction))


@beta_tool
def search_rulebooks(
    query: str,
    top_k: int = 8,
    source: str | None = None,
    jurisdiction: str | None = None,
) -> str:
    """Full-text search the loaded rulebook PDFs (federal and/or state).

    Use this for any compliance question: contribution limits, who may
    contribute, disclaimer requirements, coordination rules, joint
    fundraising, recordkeeping, registration thresholds, reporting
    requirements, personal use of funds, foreign national/corporate
    contribution bans, etc. If the question is about a specific state, pass
    that state's lowercase two-letter code as jurisdiction (call
    list_rulebook_jurisdictions first if unsure whether it's loaded).

    Args:
        query: Search terms, e.g. "individual contribution limit candidate".
        top_k: Max number of matching pages to return (default 8).
        source: Optional exact source path (from list_rulebook_sources) to
            restrict the search to a single PDF.
        jurisdiction: Optional filter, "federal" or a lowercase two-letter
            state code. Omit to search all loaded jurisdictions.
    """
    return _json(
        server.search_rulebooks(query, top_k=top_k, source=source, jurisdiction=jurisdiction)
    )


@beta_tool
def get_rulebook_page(source: str, page: int) -> str:
    """Get the full extracted text of one page from a loaded rulebook PDF.

    Args:
        source: Exact source path as returned by list_rulebook_sources /
            search_rulebooks.
        page: 1-indexed page number.
    """
    return _json(server.get_rulebook_page(source, page))


@beta_tool
def search_candidates(
    name: str | None = None,
    state: str | None = None,
    office: str | None = None,
    party: str | None = None,
    cycle: int | None = None,
    candidate_status: str | None = None,
    per_page: int = 20,
    page: int = 1,
) -> str:
    """Search real candidates via the live OpenFEC API (federal only).

    Args:
        name: Candidate name search text (fuzzy).
        state: Two-letter state code, e.g. "CA".
        office: "H" (House), "S" (Senate), or "P" (President).
        party: Party code, e.g. "DEM", "REP", "IND".
        cycle: Two-year election cycle, e.g. 2026.
        candidate_status: "C" (candidate), "F" (future), "N" (not yet
            candidate), "P" (prior candidate).
        per_page: Results per page (max 100).
        page: Page number.
    """
    return _json(
        _run_async(
            server.search_candidates,
            name=name,
            state=state,
            office=office,
            party=party,
            cycle=cycle,
            candidate_status=candidate_status,
            per_page=per_page,
            page=page,
        )
    )


@beta_tool
def get_candidate(candidate_id: str) -> str:
    """Get full details for one candidate by their FEC candidate ID (e.g. "P80001571")."""
    return _json(_run_async(server.get_candidate, candidate_id=candidate_id))


@beta_tool
def get_candidate_totals(candidate_id: str, cycle: int | None = None) -> str:
    """Get aggregated financial totals for a candidate's linked committees.

    Args:
        candidate_id: FEC candidate ID, e.g. "P80001571".
        cycle: Optional two-year cycle to filter to, e.g. 2026.
    """
    return _json(_run_async(server.get_candidate_totals, candidate_id=candidate_id, cycle=cycle))


@beta_tool
def search_committees(
    name: str | None = None,
    state: str | None = None,
    committee_type: str | None = None,
    designation: str | None = None,
    cycle: int | None = None,
    per_page: int = 20,
    page: int = 1,
) -> str:
    """Search real PACs, party committees, and campaign committees (federal only).

    Args:
        name: Committee name search text (fuzzy).
        state: Two-letter state code.
        committee_type: OpenFEC committee type code, e.g. "P" (presidential),
            "H"/"S" (House/Senate campaign), "N"/"Q" (PAC), "O" (super PAC),
            "X"/"Y" (party).
        designation: "A" (authorized), "J" (joint fundraising), "P"
            (principal campaign committee), "U" (unauthorized), "B"
            (lobbyist/registrant PAC), "D" (leadership PAC).
        cycle: Two-year cycle, e.g. 2026.
        per_page: Results per page (max 100).
        page: Page number.
    """
    return _json(
        _run_async(
            server.search_committees,
            name=name,
            state=state,
            committee_type=committee_type,
            designation=designation,
            cycle=cycle,
            per_page=per_page,
            page=page,
        )
    )


@beta_tool
def get_committee(committee_id: str) -> str:
    """Get full details for one committee by its FEC committee ID (e.g. "C00401224")."""
    return _json(_run_async(server.get_committee, committee_id=committee_id))


@beta_tool
def get_committee_filings(
    committee_id: str,
    form_type: str | None = None,
    cycle: int | None = None,
    per_page: int = 20,
    page: int = 1,
) -> str:
    """List a committee's FEC filings (e.g. Form 3, 3X, 3P finance reports).

    Args:
        committee_id: FEC committee ID, e.g. "C00401224".
        form_type: Optional FEC form type filter, e.g. "F3X".
        cycle: Optional two-year cycle filter, e.g. 2026.
        per_page: Results per page (max 100).
        page: Page number.
    """
    return _json(
        _run_async(
            server.get_committee_filings,
            committee_id=committee_id,
            form_type=form_type,
            cycle=cycle,
            per_page=per_page,
            page=page,
        )
    )


@beta_tool
def get_committee_totals(committee_id: str, cycle: int | None = None, per_page: int = 10) -> str:
    """Get a committee's financial totals (receipts, disbursements, cash on hand) by cycle.

    Args:
        committee_id: FEC committee ID, e.g. "C00401224".
        cycle: Optional two-year cycle filter, e.g. 2026.
        per_page: Number of cycle records to return.
    """
    return _json(
        _run_async(
            server.get_committee_totals, committee_id=committee_id, cycle=cycle, per_page=per_page
        )
    )


@beta_tool
def search_disbursements(
    committee_id: str,
    recipient_name: str | None = None,
    disbursement_purpose_category: str | None = None,
    disbursement_description: str | None = None,
    min_date: str | None = None,
    max_date: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    cycle: int | None = None,
    per_page: int = 50,
    last_index: str | None = None,
    last_disbursement_date: str | None = None,
) -> str:
    """Search a committee's itemized Schedule B disbursements (who a committee paid, how much).

    IMPORTANT: always pass min_date (and usually max_date) unless full
    history is explicitly wanted -- high-volume committees can have hundreds
    of thousands of disbursements, and an unfiltered query is slow enough to
    time out. For "recent" disbursements with no date given, default to
    something like the last 90 days.

    Args:
        committee_id: FEC committee ID whose disbursements to search, e.g. "C00401224".
        recipient_name: Optional recipient name search text (fuzzy).
        disbursement_purpose_category: Optional filter, one of: ADMINISTRATIVE,
            ADVERTISING, CONTRIBUTIONS, EVENTS, FUNDRAISING, LOAN-REPAYMENTS,
            MATERIALS, OTHER, POLLING, REFUNDS, TRANSFERS, TRAVEL.
        disbursement_description: Optional free-text filter on the reported purpose.
        min_date: Optional lower bound, "YYYY-MM-DD".
        max_date: Optional upper bound, "YYYY-MM-DD".
        min_amount: Optional minimum disbursement amount.
        max_amount: Optional maximum disbursement amount.
        cycle: Optional two-year cycle filter, e.g. 2026.
        per_page: Results per page (max 100).
        last_index: Cursor from a previous response's pagination.last_indexes.last_index.
        last_disbursement_date: Cursor from pagination.last_indexes.last_disbursement_date.
    """
    return _json(
        _run_async(
            server.search_disbursements,
            committee_id=committee_id,
            recipient_name=recipient_name,
            disbursement_purpose_category=disbursement_purpose_category,
            disbursement_description=disbursement_description,
            min_date=min_date,
            max_date=max_date,
            min_amount=min_amount,
            max_amount=max_amount,
            cycle=cycle,
            per_page=per_page,
            last_index=last_index,
            last_disbursement_date=last_disbursement_date,
        )
    )


@beta_tool
def search_filings(
    committee_id: str | None = None,
    candidate_id: str | None = None,
    form_type: str | None = None,
    cycle: int | None = None,
    per_page: int = 20,
    page: int = 1,
) -> str:
    """Search FEC filings across committees/candidates (federal only).

    Args:
        committee_id: Optional FEC committee ID filter.
        candidate_id: Optional FEC candidate ID filter.
        form_type: Optional FEC form type, e.g. "F3X", "F3P", "F3".
        cycle: Optional two-year cycle, e.g. 2026.
        per_page: Results per page (max 100).
        page: Page number.
    """
    return _json(
        _run_async(
            server.search_filings,
            committee_id=committee_id,
            candidate_id=candidate_id,
            form_type=form_type,
            cycle=cycle,
            per_page=per_page,
            page=page,
        )
    )


@beta_tool
def search_elections(
    state: str | None = None,
    office: str | None = None,
    cycle: int | None = None,
    district: str | None = None,
    per_page: int = 20,
    page: int = 1,
) -> str:
    """Search federal elections by state/office/cycle.

    Args:
        state: Two-letter state code.
        office: "house", "senate", or "president".
        cycle: Two-year cycle, e.g. 2026.
        district: District number (for House races), e.g. "01".
        per_page: Results per page (max 100).
        page: Page number.
    """
    return _json(
        _run_async(
            server.search_elections,
            state=state,
            office=office,
            cycle=cycle,
            district=district,
            per_page=per_page,
            page=page,
        )
    )


@beta_tool
def get_reporting_calendar(
    category: str | None = None,
    min_start_date: str | None = None,
    max_start_date: str | None = None,
    per_page: int = 50,
    page: int = 1,
) -> str:
    """Get FEC reporting/filing/election deadline dates (federal only).

    Args:
        category: Optional category, e.g. "reporting-dates", "quarterly",
            "monthly", "election-dates".
        min_start_date: Optional lower bound, "YYYY-MM-DD". There is no
            year-only filter -- use this plus max_start_date instead.
        max_start_date: Optional upper bound, "YYYY-MM-DD".
        per_page: Results per page (max 100).
        page: Page number.
    """
    return _json(
        _run_async(
            server.get_reporting_calendar,
            category=category,
            min_start_date=min_start_date,
            max_start_date=max_start_date,
            per_page=per_page,
            page=page,
        )
    )


@beta_tool
def get_committee_deadlines(
    committee: str,
    status: str,
    state: str | None = None,
    district: str | None = None,
    months_ahead: int = 12,
) -> str:
    """Which FEC filing deadlines actually bind one committee, and when.

    Filters the FEC's published calendar down to what this committee owes,
    based on where it is in its election lifecycle. A committee that lost
    its primary owes no general-election reports; one that won owes both
    the pre-general and post-general.

    Args:
        committee: FEC committee ID (e.g. "C00614701") or committee name
            (e.g. "Crane for Congress"). If a name matches several
            committees, the matches are returned to choose from rather
            than one being picked.
        status: Required -- one of "in_primary", "won_primary",
            "lost_primary", "won_general", "lost_general", "terminating",
            or "ongoing" for a PAC or party committee. Ask the user which
            applies rather than assuming; the wrong status silently
            produces the wrong deadlines.
        state: Two-letter state of the federal race, e.g. "MI". Needed for
            state-timed deadlines like pre-primary reports, since federal
            primaries fall on different dates in different states. Pass it
            whenever the user knows it -- OpenFEC often cannot report
            which candidate a committee belongs to.
        district: District for a House seat, e.g. "04".
        months_ahead: How far ahead to look, default 12 months.
    """
    return _json(
        _run_async(
            server.get_committee_deadlines,
            committee=committee,
            status=status,
            state=state,
            district=district,
            months_ahead=months_ahead,
        )
    )


@beta_tool
def send_deadline_invites(
    committee: str,
    status: str,
    recipients: list[str],
    state: str | None = None,
    district: str | None = None,
    months_ahead: int = 12,
    send: bool = False,
) -> str:
    """Put a committee's FEC filing deadlines on people's calendars.

    Emails calendar invitations for every deadline the committee owes.
    Re-running after a status change updates the existing entries rather
    than duplicating them.

    It never removes an event from anyone's calendar. A deadline that
    stops applying stays there until someone deletes it by hand, so the
    result lists those under "no_longer_applies_remove_manually" -- always
    pass that list on to the user, since nothing else will tell them.

    IMPORTANT: `send` defaults to False and nothing is emailed until it is
    True. Email cannot be recalled and goes to other people. Always run
    the preview first, show the user which deadlines would be invited,
    which would be withdrawn, and who would receive them, and get their
    explicit go-ahead before calling again with send=True. Never set
    send=True on your own initiative.

    Args:
        committee: FEC committee ID (e.g. "C00614701") or committee name.
        status: Lifecycle status -- "in_primary", "won_primary",
            "lost_primary", "won_general", "lost_general", "terminating",
            or "ongoing" for a PAC. Ask the user which applies.
        recipients: Email addresses to invite.
        state: Two-letter state of the federal race, for state-timed
            deadlines like pre-primary reports.
        district: District for a House seat, e.g. "04".
        months_ahead: How far ahead to schedule, default 12 months.
        send: Leave False to preview; True only after user confirmation.
    """
    return _json(
        _run_async(
            server.send_deadline_invites,
            committee=committee,
            status=status,
            recipients=recipients,
            state=state,
            district=district,
            months_ahead=months_ahead,
            send=send,
        )
    )


@beta_tool
def search_advisory_opinions(
    q: str | None = None,
    ao_no: str | None = None,
    ao_year: str | None = None,
    ao_name: str | None = None,
    ao_status: str | None = None,
    ao_requestor: str | None = None,
    ao_commenter: str | None = None,
    ao_representative: str | None = None,
    hits_returned: int = 20,
) -> str:
    """Search FEC Advisory Opinions -- rulings on specific factual scenarios
    (e.g. "can a campaign accept cryptocurrency donations"), federal only.
    Use this for a specific edge-case scenario; use search_rulebooks instead
    for general compliance rules. Any document link/URL field returned is a
    path relative to https://www.fec.gov, not a complete URL -- always
    prepend that origin when presenting a link.

    Args:
        q: Free-text search, e.g. "cryptocurrency donations".
        ao_no: Exact AO number, e.g. "2014-12".
        ao_year: Filter by year requested, e.g. "2014".
        ao_name: Filter by AO name/subject text.
        ao_status: Filter by status, e.g. "Final".
        ao_requestor: Filter by requestor name.
        ao_commenter: Filter by commenter name.
        ao_representative: Filter by requestor's legal representative name.
        hits_returned: Max results (max 200).
    """
    return _json(
        _run_async(
            server.search_advisory_opinions,
            q=q,
            ao_no=ao_no,
            ao_year=ao_year,
            ao_name=ao_name,
            ao_status=ao_status,
            ao_requestor=ao_requestor,
            ao_commenter=ao_commenter,
            ao_representative=ao_representative,
            hits_returned=hits_returned,
        )
    )


@beta_tool
def get_advisory_opinion(ao_no: str) -> str:
    """Get one FEC Advisory Opinion's full document record by its AO number
    (federal only). Returns every document filed under this AO number --
    request, drafts, final opinion, vote record, comments -- not just the
    final opinion, so check each document's type/category before treating
    its text as the Commission's actual holding.

    Args:
        ao_no: AO number as returned by search_advisory_opinions, e.g. "2014-12".
    """
    return _json(_run_async(server.get_advisory_opinion, ao_no=ao_no))


TOOLS = [
    list_rulebook_jurisdictions,
    list_rulebook_sources,
    search_rulebooks,
    get_rulebook_page,
    search_candidates,
    get_candidate,
    get_candidate_totals,
    search_committees,
    get_committee,
    get_committee_filings,
    get_committee_totals,
    search_disbursements,
    search_filings,
    search_elections,
    get_reporting_calendar,
    get_committee_deadlines,
    send_deadline_invites,
    search_advisory_opinions,
    get_advisory_opinion,
]


# ---------------------------------------------------------------------------
# Chat turn logic (no Streamlit calls here -- kept testable without a live
# ScriptRunContext; see main() for the actual page).
# ---------------------------------------------------------------------------


def run_turn(client: Anthropic, history: list[dict[str, Any]], user_text: str) -> dict[str, Any]:
    """Run one chat turn: send history + a new user message through the tool
    runner, return the assistant's final text plus a trace of tool calls made.

    Conversation history is kept as plain text turns (not the raw tool_use/
    tool_result blocks the runner produces) -- simpler to persist across
    Streamlit reruns, and Claude doesn't need the tool-call plumbing replayed
    to hold a coherent conversation, only what was asked and answered.
    """
    messages = history + [{"role": "user", "content": user_text}]

    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=server.INSTRUCTIONS
        + CITATION_FORMAT_ADDENDUM
        + NO_HEADINGS_ADDENDUM
        + _today_addendum(),
        tools=TOOLS,
        messages=messages,
    )

    trace: list[dict[str, Any]] = []
    last_message = None
    for message in runner:
        last_message = message
        for block in message.content:
            if block.type == "tool_use":
                trace.append({"name": block.name, "input": block.input})

    if last_message is None:
        return {"text": "(no response)", "trace": trace, "stop_reason": None}

    text = "".join(block.text for block in last_message.content if block.type == "text")
    return {"text": text, "trace": trace, "stop_reason": last_message.stop_reason}


def _name_matches_only(matches: list[dict], query: str) -> tuple[list[dict], int]:
    """Keep only committees whose own NAME contains the query.

    OpenFEC's committee endpoint is full-text and also matches the linked
    candidate's name, which it doesn't return on the record -- so
    searching "abdul" comes back with JAMAL FOR CONGRESS, whose candidate
    is Abdul-something. Correct by OpenFEC's definition, but this field
    asks for a committee name or ID, and results matching neither are
    noise here.

    Returns (kept, dropped_count). The count matters: a search that
    silently shows nothing after OpenFEC returned seven results looks
    broken, so the caller says how many were set aside and why.
    """
    query = (query or "").strip().lower()
    if not query:
        return list(matches), 0
    kept = [m for m in matches if query in (m.get("name") or "").lower()]
    return kept, len(matches) - len(kept)


def _committee_step() -> dict[str, Any] | None:  # pragma: no cover -- Streamlit UI
    """Find and choose one committee to add to the roster.

    Searching returns a list to pick from rather than resolving silently.
    The underlying tool errors on an ambiguous name on purpose -- picking
    the top fuzzy match would produce a complete, plausible, wrong
    schedule for a committee nobody asked about -- and a UI can do better
    than an error: show the matches and let a person choose.
    """
    chosen = st.session_state.get("dl_committee")

    if chosen:
        left, right = st.columns([5, 1])
        with left:
            st.success(
                f"**{chosen.get('name')}** — {chosen.get('committee_id')} · "
                f"{chosen.get('state') or '—'} · "
                f"{'quarterly' if (chosen.get('filing_frequency') or '').upper() == 'Q' else 'monthly' if (chosen.get('filing_frequency') or '').upper() == 'M' else 'unknown'} filer"
            )
        with right:
            if st.button("Change", use_container_width=True):
                for key in ("dl_committee", "dl_matches", "dl_result", "dl_preview"):
                    st.session_state.pop(key, None)
                st.rerun()
        return chosen

    # The key carries a counter, bumped every time a committee is added.
    # Deleting a text input's key does NOT clear the box: the widget is
    # still mounted on the frontend, which hands its value straight back
    # on the next run -- so the previous search term sat in the field
    # after the committee had been added, as if the search were still
    # pending. A key that changes is a different widget, which is the one
    # reliable way to get an empty box back.
    field_col, button_col = st.columns([5, 1], vertical_alignment="bottom")
    with field_col:
        query = st.text_input(
            "Add a committee — name or FEC ID",
            placeholder="Eli Crane for Congress    —or—    C00784934",
            help=(
                "Matches the committee's own name or FEC ID. The FEC's search also "
                "returns committees matching elsewhere in their records, such as the "
                "candidate's name; those are filtered out here."
            ),
            key=f"dl_query_{st.session_state.get('dl_query_round', 0)}",
        )
    with button_col:
        find = st.button("Find committee", type="primary", use_container_width=True)
    if find and query.strip():
        text = query.strip()
        with st.spinner("Searching OpenFEC..."):
            if re.fullmatch(r"C\d{8}", text, re.IGNORECASE):
                found = _run_async(server.get_committee, committee_id=text.upper())
            else:
                found = _run_async(server.search_committees, name=text, per_page=10)
        if "error" in found:
            st.error(found["error"])
        else:
            # An ID lookup is already exact; only a name search needs
            # narrowing to the committee's own name.
            results = found.get("results") or []
            if re.fullmatch(r"C\d{8}", text, re.IGNORECASE):
                kept, dropped = results, 0
            else:
                kept, dropped = _name_matches_only(results, text)
            st.session_state["dl_matches"] = kept
            st.session_state["dl_dropped"] = dropped
            st.rerun()

    matches = st.session_state.get("dl_matches")
    dropped = st.session_state.get("dl_dropped", 0)
    if matches is not None:
        if not matches:
            st.warning("No committee name matched that. Try the FEC ID, or fewer words.")
        elif len(matches) == 1:
            st.session_state["dl_committee"] = matches[0]
            st.session_state.pop("dl_matches", None)
            st.rerun()
        else:
            st.caption(f"{len(matches)} matches — pick one:")
            for i, match in enumerate(matches):
                _match_button(match, f"dl_match_{i}")

        if dropped:
            # Said out loud rather than left invisible. OpenFEC returned
            # these and this field chose not to show them; a search that
            # quietly returns three of seven results looks like a bug in
            # exactly the way a stated filter does not.
            st.caption(
                f"{dropped} other committee(s) matched elsewhere in the FEC's records "
                "(usually the candidate's name) and are not shown — this field "
                "searches committee names and IDs only."
            )
    return None


def _match_button(match: dict[str, Any], key: str) -> None:  # pragma: no cover -- Streamlit UI
    """One selectable search result; selecting it advances the workflow."""
    label = (
        f"{match.get('name')} · {match.get('committee_id')} · "
        f"{match.get('state') or '—'} · {match.get('designation_full') or ''}"
    )
    if not st.button(label, key=key, use_container_width=True):
        return

    # Re-read the chosen committee from the detail endpoint rather than
    # keeping the search row. A search result is a listing record and has
    # been thinner than the detail one before; everything downstream --
    # which statuses apply, quarterly versus monthly -- depends on fields
    # a listing may not carry, and the failure is silent: the committee
    # simply looks like a different kind of committee than it is.
    with st.spinner("Loading committee..."):
        detail = _run_async(server.get_committee, committee_id=match["committee_id"])
    full = (detail.get("results") or [None])[0] if "error" not in detail else None
    st.session_state["dl_committee"] = full or match
    st.session_state.pop("dl_matches", None)
    st.rerun()


def _status_age_note(entry) -> str | None:
    """A nudge when a status is old enough to be doubted, and nothing when
    it isn't.

    Showing "set 2026-08-20" on every row the day it was set is noise on
    every row forever. What actually matters is the opposite case: a race
    resolved months ago and nobody updated the app, so "still in the
    primary" is quietly wrong. Only that case gets a line.
    """
    if not entry.status_set_on:
        return None
    try:
        age = (date.today() - date.fromisoformat(entry.status_set_on)).days
    except (TypeError, ValueError):
        return None
    if age < _STATUS_STALE_AFTER_DAYS:
        return None
    months = age // 30
    return f"⚠ set {months} month{'s' if months != 1 else ''} ago — still current?"


_ROSTER_COLUMNS = [4, 3.9, 0.85, 1.15, 0.6]

_DISTRICT_NOTE = (
    "House committees only. A district is what makes a House race a race -- "
    "AZ-02 and AZ-06 are different contests -- so it decides which pre-primary "
    "deadlines bind. Senate and presidential committees run statewide or "
    "nationally and have no district; the box is disabled for them."
)


def _needs_status_css(entries) -> str:
    """Outline the status box of every committee that still has none.

    The state belongs on the control, not in a banner somewhere below it:
    the thing to fix is the empty box, and marking the box says both what
    is wrong and where to fix it in one gesture.

    Streamlit tags any keyed widget's container with `st-key-<key>`, which
    is what makes a specific row addressable from a stylesheet -- and the
    keys here already carry the committee ID. The bordered control itself
    is an unlabelled div, so it is reached as the one wrapping the input
    rather than by a class of its own, which would be a generated name
    that changes between Streamlit builds.
    """
    keys = [
        f".st-key-roster_status_{e.committee_id}"
        for e in entries
        if not e.has_status and re.fullmatch(r"[A-Za-z0-9]+", e.committee_id or "")
    ]
    if not keys:
        return ""
    selector = ", ".join(f"{k} div:has(> input)" for k in keys)
    return (
        "<style>"
        f"{selector} {{ border-color: {BRAND_RED} !important; "
        f"box-shadow: 0 0 0 1px {BRAND_RED}; background: #fdf3f2 !important; }}"
        "</style>"
    )


def _roster_header() -> None:  # pragma: no cover -- Streamlit UI
    """Column headings for the roster.

    The short boxes at the end of each row are unlabeled otherwise, and
    two things that cannot fit on screen hang off these headings as
    tooltips: what each status changes (seven sentences, one per option),
    and why the district box is only for House committees.
    """
    detail = " · ".join(f"{label}: {_STATUS_DETAIL[value]}" for value, label, _, _ in STATUS_CHOICES)
    cols = st.columns(_ROSTER_COLUMNS, vertical_alignment="bottom")
    for col, text in zip(
        cols,
        [
            "Committee",
            "Where it is in the cycle "
            f'<span title="{html.escape(detail, quote=True)}">what each means</span>',
            "State",
            f'<span class="hdr" title="{html.escape(_DISTRICT_NOTE, quote=True)}">District</span>',
            "",
        ],
    ):
        with col:
            st.markdown(f'<div class="fec-roster-head">{text}</div>', unsafe_allow_html=True)


def _roster_row(entry, index: int) -> None:  # pragma: no cover -- Streamlit UI
    """One committee on the roster, kept to a single line.

    Status is per committee and cannot be shared: each sits somewhere
    different in its cycle, and that alone decides its deadline set. The
    consequence of each status lives in the dropdown's own option text,
    where it is read at the moment of choosing, rather than as a caption
    that repeats under every row forever once the choice is made.
    """
    name_col, status_col, state_col, dist_col, drop_col = st.columns(
        _ROSTER_COLUMNS, vertical_alignment="center"
    )

    with name_col:
        frequency = (entry.filing_frequency or "").upper()
        readable = {"Q": "quarterly", "M": "monthly"}.get(frequency, "unknown")
        # The staleness nudge rides on this line rather than under the
        # dropdown. Anything below the dropdown makes that one cell taller
        # than the rest, and centered columns then push its control out of
        # line with the name beside it -- verified live: the row with a
        # nudge sat 20px off every other row.
        aged = _status_age_note(entry)
        note = f' · <span class="fec-roster-aged">{html.escape(aged)}</span>' if aged else ""
        st.markdown(
            f'<div class="fec-roster-name" title="{html.escape(entry.name, quote=True)}">'
            f"{html.escape(entry.name)}</div>"
            f'<div class="fec-roster-sub">{entry.committee_id} · {readable}{note}</div>',
            unsafe_allow_html=True,
        )

    with status_col:
        choices = (
            [c for c in STATUS_CHOICES if c[0] != "ongoing"]
            if entry.is_candidate_committee
            else [c for c in STATUS_CHOICES if c[0] in {"ongoing", "terminating"}]
        )
        options = [UNSET_STATUS] + [value for value, _, _, _ in choices]
        current = entry.status if entry.status in options else UNSET_STATUS
        picked = st.selectbox(
            "Status",
            options=options,
            index=options.index(current),
            # Never defaulted: a guessed status yields a complete,
            # confident, wrong schedule, and nothing about a wrong
            # schedule looks wrong.
            format_func=lambda v: (
                "— pick where it is —" if not v
                else f"{_STATUS_LABELS[v]} — {_STATUS_HINTS[v]}"
            ),
            key=f"roster_status_{entry.committee_id}",
            label_visibility="collapsed",
        )
        if picked != entry.status:
            _roster().update(entry.committee_id, status=picked)
            st.rerun()

    # A race is meaningless for a PAC, which has no single one, so these
    # boxes are emptied rather than greyed out around a value. The state
    # seeded from the committee's own record is a mailing address, and a
    # greyed "MA" beside a national PAC reads as a claim about a race.
    races = entry.is_candidate_committee
    # Narrower still: a district exists only in a House race. On a Senate
    # or presidential committee it is not unknown, it does not exist, and
    # an open box invites a value that would silently narrow the race.
    districted = entry.runs_in_a_district
    with state_col:
        state = st.text_input(
            "State", value=(entry.state or "") if races else "", max_chars=2,
            key=f"roster_state_{entry.committee_id}",
            label_visibility="collapsed",
            placeholder="ST" if races else "",
            disabled=not races,
        ).strip().upper()
    with dist_col:
        district = st.text_input(
            "District", value=(entry.district or "") if districted else "", max_chars=2,
            key=f"roster_district_{entry.committee_id}",
            label_visibility="collapsed",
            # "n/a" rather than an empty box, and rather than the sentence
            # that used to sit under the whole table. A blank disabled box
            # reads as a value nobody has filled in yet; "n/a" says the
            # field does not apply, at the box it applies to. The reason
            # it does not apply is a hover away, on the column heading.
            #
            # Not `help=`, which is where this note started: on an input
            # with its label collapsed, Streamlit's help icon renders on
            # the label and so never appears at all.
            placeholder="00" if districted else ("n/a" if races else ""),
            disabled=not districted,
        ).strip()

    if races and (
        state != (entry.state or "")
        or (districted and district != (entry.district or ""))
    ):
        _roster().update(
            entry.committee_id, state=state, district=district if districted else ""
        )
        st.rerun()

    with drop_col:
        if st.button("✕", key=f"roster_drop_{entry.committee_id}", help="Remove"):
            _roster().remove(entry.committee_id)
            st.rerun()


def _with_resolved_race(committee: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover -- UI
    """Fill in the race a candidate committee is running in, if OpenFEC can say.

    A committee record carries a state, which is why one appears by itself
    when a committee is added -- but that state is the committee's own
    mailing address, and no committee record carries a district at all. A
    district lives on the CANDIDATE record, so getting one means going
    committee -> candidate -> race, which is what this does.

    Two things it deliberately does not do. It does not guess: when the
    lookup resolves nothing the fields stay empty, which is honest and
    also what happened before. And it does not overwrite a resolved state
    with a mailing address -- if the lookup found the race, that state is
    better evidence than the address on the paperwork.

    Both values land in ordinary editable boxes, so a wrong one can be
    corrected in place. That matters: OpenFEC's committee-to-candidate
    links have been observed empty on real principal campaign committees,
    and stale on others.
    """
    office = (committee.get("committee_type") or "").upper()
    if office not in {"H", "S", "P"}:
        return committee

    with st.spinner("Looking up the race..."):
        race = _run_async(server.resolve_race, committee_id=committee["committee_id"])

    if not race.get("state"):
        return committee

    resolved = {**committee, "state": race["state"]}
    # Only a House seat has a district. A Senate or presidential race has
    # none, and writing one would narrow the race to a contest that does
    # not exist.
    if office == "H" and race.get("district"):
        resolved["district"] = race["district"]
    return resolved


def _roster() -> CommitteeRoster:  # pragma: no cover -- Streamlit UI
    """One roster per session, reloaded from disk on first use."""
    if "dl_roster" not in st.session_state:
        st.session_state["dl_roster"] = CommitteeRoster()
    return st.session_state["dl_roster"]


def _combined_agenda(entries) -> None:  # pragma: no cover -- Streamlit UI
    """Every deadline across every committee, in date order.

    The question a person with several committees actually asks is "what
    is due next, and for whom" -- which no per-committee view answers.
    """
    months = cycle_horizon_months()
    ready = [e for e in entries if e.has_status]

    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    for entry in ready:
        result = _run_async(
            server.get_committee_deadlines,
            committee=entry.committee_id, status=entry.status,
            state=entry.state, district=entry.district, months_ahead=months,
        )
        if "error" in result:
            problems.append(f"{entry.name}: {result['error']}")
            continue
        for deadline in result.get("deadlines") or []:
            rows.append({**deadline, "_committee": entry.name})
        for warning in result.get("warnings") or []:
            problems.append(f"{entry.name}: {warning}")

    for problem in problems:
        st.warning(problem)

    if not rows:
        if ready:
            st.info("No filing deadlines fall in this window for these committees.")
        return

    rows.sort(key=lambda r: (r.get("date") or "", r["_committee"]))

    # Grouped by month so a long list stays scannable -- a full cycle can
    # run to two dozen rows across several committees.
    html_parts: list[str] = []
    current_month = None
    for row in rows:
        month = (row.get("date") or "")[:7]
        if month != current_month:
            current_month = month
            try:
                label = date.fromisoformat(row["date"]).strftime("%B %Y")
            except (TypeError, ValueError):
                label = month or "Undated"
            html_parts.append(f'<div class="fec-dl-month">{html.escape(label)}</div>')
        html_parts.append(_agenda_row_html(row))

    st.markdown(
        f'<div class="fec-dl-wrap">{"".join(html_parts)}</div>', unsafe_allow_html=True
    )

    unverified = [r for r in rows if not r.get("certain", True)]
    if unverified:
        st.caption(
            f"{len(unverified)} marked CONFIRM could not be settled automatically — "
            "listed rather than hidden so nothing is missed, but check whether each applies."
        )


def _invite_step(entries) -> None:  # pragma: no cover -- Streamlit UI
    """Send invitations for ONE committee at a time.

    Recipients differ per committee -- each campaign has its own treasurer
    and counsel -- so a single "send everything to everyone" action would
    email one client's filing schedule to another client's staff.
    """
    ready = [e for e in entries if e.has_status]
    if not ready:
        return

    st.markdown("**Send calendar invites**", unsafe_allow_html=True)
    by_label = {f"{e.name} · {_STATUS_LABELS[e.status]}": e for e in ready}
    chosen_label = st.selectbox("Committee", options=list(by_label), key="dl_invite_committee")
    entry = by_label[chosen_label]

    raw = st.text_area(
        "Recipients",
        placeholder="treasurer@campaign.com, counsel@firm.com",
        help="Separate addresses with commas or new lines. Sent for this committee only.",
        key=f"dl_recipients_{entry.committee_id}",
        height=68,
    )
    recipients = [a.strip() for a in re.split(r"[,\s]+", raw or "") if a.strip()]

    # Not disabled on `recipients`: Streamlit commits a text area only on
    # blur, so a button disabled from that value is still disabled at the
    # moment someone finishes typing and clicks.
    left, right = st.columns(2)
    with left:
        preview_clicked = st.button("Preview invitations", use_container_width=True)
    preview = st.session_state.get("dl_preview")
    fresh = preview if (preview or {}).get("_for") == entry.committee_id else None
    with right:
        send_clicked = st.button(
            f"Send to {len(recipients)} recipient(s)" if recipients else "Send invitations",
            type="primary", use_container_width=True,
            disabled=not (fresh and not fresh.get("error") and fresh.get("would_invite")),
        )

    if preview_clicked or send_clicked:
        if not recipients:
            st.warning("Add at least one email address first.")
        else:
            sending = bool(send_clicked)
            with st.spinner("Sending..." if sending else "Building invitations..."):
                result = _run_async(
                    server.send_deadline_invites,
                    committee=entry.committee_id, status=entry.status,
                    recipients=recipients, state=entry.state, district=entry.district,
                    months_ahead=cycle_horizon_months(), send=sending,
                )
            st.session_state["dl_preview"] = {**result, "_for": entry.committee_id}
            st.rerun()

    if not fresh:
        st.caption("Preview first — nothing is emailed until you send.")
        return

    if fresh.get("error"):
        st.error(fresh["error"])

    stale = fresh.get("no_longer_applies_remove_manually") or []
    if stale:
        # Nothing withdraws these, so this notice is the only thing
        # between a losing campaign and a filing nobody owes.
        st.error(
            f"**{len(stale)} deadline(s) no longer apply but are still in recipients' "
            "calendars.** Nothing is removed automatically — ask them to delete:\n\n"
            + "\n".join(
                f"- {_us_date(row.get('date')) or 'date unknown'} — {row.get('summary')}"
                for row in stale
            )
        )

    if fresh.get("sent"):
        st.success(fresh.get("note") or "Invitations sent.")
    elif fresh.get("would_invite"):
        st.info(
            f"Ready to send {len(fresh['would_invite'])} invitation(s) for "
            f"{entry.name} to {', '.join(fresh.get('recipients') or [])}."
        )


def _deadlines_view() -> None:  # pragma: no cover -- Streamlit UI, not unit tested
    """Several committees, each with its own status, in one agenda."""
    st.markdown(DEADLINE_CSS, unsafe_allow_html=True)
    roster = _roster()
    entries = roster.entries()

    missing = [e for e in entries if not e.has_status]

    # Nothing here collapses, and that is the point.
    #
    # Both panels used to be expanders, and both kept closing on the
    # actions taken from inside them. An expander's `expanded` argument is
    # a starting value that gets re-read whenever the widget's identity
    # changes -- and its identity includes its label, its position, and
    # that very argument. So the panel shut when the label's committee
    # count changed (any add or delete), when a row was inserted above it
    # (any add), and when `expanded` itself flipped as the last missing
    # status was filled in. Three separate triggers, each fixable, none of
    # them the real problem: setting up a roster is a sequence of edits,
    # and a container that decides for itself when that sequence is over
    # will always be wrong at some point in it.
    #
    # So the roster is just a table, always on screen. It costs about
    # 56px a committee, which is affordable because each row is one line.
    st.markdown("#### Your committees")

    # Above the table, not below it. The roster grows and the search box
    # was riding down with it -- at eight committees the way to add a
    # ninth had scrolled off, which is the one thing on this screen whose
    # position should not depend on how much is already on it.
    picked = _committee_step()
    if picked:
        # Adding one already on the roster is a no-op by design -- it must
        # not reset a status set months ago -- but a no-op that looks
        # exactly like a successful add is its own problem, so it says so.
        if roster.get(picked.get("committee_id") or ""):
            st.session_state["dl_already_tracked"] = picked.get("name")
        else:
            roster.add(_with_resolved_race(picked))
        # A new key for the search box, so it comes back empty. See
        # _committee_step: deleting the old key does not clear it.
        st.session_state["dl_query_round"] = st.session_state.get("dl_query_round", 0) + 1
        for key in ("dl_committee", "dl_matches", "dl_dropped"):
            st.session_state.pop(key, None)
        st.rerun()

    already = st.session_state.pop("dl_already_tracked", None)
    if already:
        st.info(f"{already} is already on your roster — its status was left as it is.")

    if entries:
        # Every committee still missing a status gets its status box
        # outlined in red, in place of a warning further down the page.
        st.markdown(_needs_status_css(entries), unsafe_allow_html=True)
        # Every row, always, and the page scrolls if there are many.
        #
        # A height cap was tried here and removed. It put the roster in
        # its own scroll box inside an already-scrolling page, clipped a
        # row mid-height at the boundary, and took the column headings
        # away with it -- sticky cannot hold them, since each heading
        # lives in its own one-row column and a sticky element cannot
        # outlive its parent's box. Rows nobody can see and boxes nobody
        # can name is a worse trade than a longer page.
        _roster_header()
        for i, entry in enumerate(entries):
            _roster_row(entry, i)
    else:
        st.caption("Add a committee to see which FEC deadlines bind it.")

    if not entries:
        return

    st.divider()

    st.markdown("#### Everything due · rest of the cycle")
    if missing:
        # What the red boxes above cannot say: that this list is short a
        # committee. Naming them here is not a duplicate of the outline --
        # the outline marks what to fix, this marks what is missing
        # because it has not been. A silently incomplete agenda is the one
        # outcome this view must never produce.
        st.markdown(
            f'<div class="fec-dl-gap">Not listed: '
            f'{html.escape(", ".join(e.name for e in missing))} — '
            "no status set (outlined in red above).</div>",
            unsafe_allow_html=True,
        )
    with st.spinner("Reading the FEC calendar..."):
        _combined_agenda(entries)

    st.divider()
    _invite_step(entries)


def main() -> None:  # pragma: no cover -- Streamlit UI, not unit tested
    _sync_static_pdfs()
    st.set_page_config(page_title="fec-mcp demo", page_icon="\U0001f5f3️", layout="wide")
    st.markdown(CITATION_CSS, unsafe_allow_html=True)
    st.markdown(HEADER_CSS, unsafe_allow_html=True)
    st.markdown(CHAT_BUBBLE_CSS, unsafe_allow_html=True)
    st.markdown(VIEW_SWITCH_CSS, unsafe_allow_html=True)
    # The title and blurb introduce the demo, which is worth doing once on
    # the way in and not worth ~150px above a working list. Read from the
    # previous run's selection, since the view switch is rendered further
    # down; on the very first run there is none, and Chat is the default.
    on_deadlines = st.session_state.get("active_view") == "Deadlines"
    page_heading = (
        ""
        if on_deadlines
        else (
            '<div class="fec-page-heading"><h2>FEC Compliance Assistant</h2>'
            "<p>Same tools as the fec-mcp MCP server &mdash; rulebook PDF search + live "
            "OpenFEC data &mdash; wired into a plain chat page for demo purposes. Not for "
            "production use.</p></div>"
        )
    )
    st.markdown(
        '<div class="fec-header-overlay">'
        '<div class="fec-topbar"><div class="badge">A</div>'
        '<div class="name">FEC Compliance Assistant<span>fec-mcp demo</span></div></div>'
        '<div class="fec-subbar">HOME&nbsp;&nbsp;&middot;&nbsp;&nbsp;RULEBOOKS&nbsp;&nbsp;'
        "&middot;&nbsp;&nbsp;CANDIDATES &amp; COMMITTEES&nbsp;&nbsp;&middot;&nbsp;&nbsp;"
        "ADVISORY OPINIONS</div>"
        "</div>" + page_heading,
        unsafe_allow_html=True,
    )

    with st.sidebar:
        api_key = st.text_input(
            "Your Anthropic API key",
            type="password",
            help=(
                "Get one at console.anthropic.com. Used only for your own "
                "session -- not stored or shared with other visitors. Falls "
                "back to the server's ANTHROPIC_API_KEY environment "
                "variable if left blank (unset on this deployment)."
            ),
        )
        has_key = bool(api_key) or bool(os.environ.get("ANTHROPIC_API_KEY"))
        if not has_key:
            st.info("Paste your Anthropic API key above to start chatting.")
        # list_rulebook_sources() is near-instant once the on-disk index is
        # built (reads a cached SQLite file), but the *first* call after
        # anything changes the index's manifest -- adding a PDF, or editing
        # TITLE_OVERRIDES the way this file's own sidebar reorganization
        # just did -- triggers a full rebuild that re-extracts text from
        # every loaded PDF. Confirmed live this takes ~35-50s for the 32
        # PDFs currently loaded, during which Streamlit had already
        # rendered everything above this point (the API key input, its
        # info box) and was simply blocked here with nothing on screen to
        # show it -- indistinguishable from the sidebar being broken. A
        # spinner turns that into a legible "still loading" state; it's
        # only actually visible on that first post-change run, since every
        # later rerun hits the cache.
        with st.spinner("Loading rulebook index..."):
            sources_result = server.list_rulebook_sources()
        st.markdown('<p class="fec-side-label">Rulebooks loaded</p>', unsafe_allow_html=True)
        if sources_result.get("sources"):
            by_jurisdiction: dict[str, list[dict]] = {}
            for s in sources_result["sources"]:
                by_jurisdiction.setdefault(s["jurisdiction"], []).append(s)
            # Every jurisdiction row is the same st.expander widget -- same
            # label font/size for "Federal" as for "California" or "Georgia"
            # by construction, rather than the old hand-styled chips (one
            # color for federal, another for states) that made federal read
            # as visually distinct. Federal starts expanded since it's what
            # most people are looking for; states start collapsed, since a
            # flat list of all 32 loaded PDFs at once was the original
            # "cluttered" complaint this replaces.
            for jurisdiction in sorted(by_jurisdiction, key=_jurisdiction_sort_key):
                srcs = by_jurisdiction[jurisdiction]
                label = f"{_jurisdiction_label(jurisdiction)} ({len(srcs)})"
                with st.expander(label, expanded=(jurisdiction == "federal")):
                    for s in srcs:
                        href = _pdf_url(s["source"])
                        title = html.escape(s["title"])
                        st.markdown(
                            f'<a href="{href}" target="_blank" rel="noopener">{title}</a>',
                            unsafe_allow_html=True,
                        )
        else:
            st.write(sources_result.get("message", "None loaded."))

        st.markdown('<p class="fec-side-label">Try asking</p>', unsafe_allow_html=True)
        for i, question in enumerate(EXAMPLE_QUESTIONS):
            if st.button(question, key=f"example_question_{i}", use_container_width=True):
                st.session_state["chat_input"] = question
                st.rerun()

        if st.button("Clear conversation"):
            st.session_state.messages = []
            st.rerun()

    # A segmented control rather than st.tabs, for two reasons that matter
    # here. st.tabs renders every tab's body on every rerun, so the
    # deadline form would re-hit OpenFEC on each chat message; and
    # st.chat_input stays pinned to the bottom of the page regardless of
    # which tab is showing, which would leave a chat box under the
    # deadline form. Conditional rendering avoids both.
    view = st.segmented_control(
        "View", options=["Chat", "Deadlines"], default="Chat",
        key="active_view", label_visibility="collapsed",
    )
    if view == "Deadlines":
        _deadlines_view()
        return

    if "messages" not in st.session_state:
        st.session_state.messages = []

    for turn in st.session_state.messages:
        with st.chat_message(turn["role"]):
            prose, citations = _split_citations(turn["content"])
            st.markdown(_md(prose))
            _render_citations(citations)
            for call in turn.get("trace", []):
                st.caption(_md(f"\U0001f527 {call['name']}({', '.join(f'{k}={v!r}' for k, v in call['input'].items())})"))

    prompt = st.chat_input(
        "Ask a federal (or loaded-state) campaign finance question...", key="chat_input"
    )
    if not prompt:
        return

    if not has_key:
        with st.chat_message("user"):
            st.markdown(_md(prompt))
        with st.chat_message("assistant"):
            st.warning("Add your Anthropic API key in the sidebar first, then ask again.")
        return

    with st.chat_message("user"):
        st.markdown(_md(prompt))
    st.session_state.messages.append({"role": "user", "content": prompt})

    client = Anthropic(api_key=api_key or None)
    history = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages[:-1]]

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                result = run_turn(client, history, prompt)
            except Exception as exc:  # noqa: BLE001 -- surface any API/tool error to the demo UI
                st.error(f"Error: {exc}")
                return
        for call in result["trace"]:
            st.caption(_md(f"\U0001f527 {call['name']}({', '.join(f'{k}={v!r}' for k, v in call['input'].items())})"))
        prose, citations = _split_citations(result["text"])
        st.markdown(_md(prose))
        _render_citations(citations)
        if result["stop_reason"] == "pause_turn":
            st.warning("Response paused mid-turn (hit the server-tool iteration limit) -- answer may be incomplete.")

    st.session_state.messages.append(
        {"role": "assistant", "content": result["text"], "trace": result["trace"]}
    )


if __name__ == "__main__":
    main()
