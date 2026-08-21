"""Regression coverage for demo/app.py's tool wrappers.

demo/app.py hand-writes a thin @beta_tool wrapper around each real tool
function in fec_mcp.server, so the two can drift out of sync silently: a
wrapper's parameter list is only checked against the real function at call
time (a TypeError on an unexpected/missing keyword), not at import time or
by any type checker, since @beta_tool's decorator hides the underlying
function from static analysis. This happened for real with
get_reporting_calendar (demo/app.py had a stale "calendar_year" parameter
that fec_mcp.server.get_reporting_calendar had never had -- every call
failed with "unexpected keyword argument 'calendar_year'"). This test
inspects every wrapper's real (undecorated) signature against its
corresponding server function's signature so a mismatch fails fast in CI
instead of only surfacing when a user asks the demo the wrong question.
"""

import importlib.util
import inspect
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

from fec_mcp import server

DEMO_APP_PATH = Path(__file__).resolve().parents[1] / "demo" / "app.py"


def _load_demo_app():
    spec = importlib.util.spec_from_file_location("fec_mcp_demo_app", DEMO_APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


demo_app = _load_demo_app()


def test_tool_wrapper_signatures_match_server_functions():
    mismatches = []
    for tool in demo_app.TOOLS:
        server_func = getattr(server, tool.name, None)
        assert server_func is not None, f"server.py has no matching function for tool {tool.name!r}"

        wrapper_params = inspect.signature(tool.func).parameters
        server_params = inspect.signature(server_func).parameters

        if wrapper_params.keys() != server_params.keys():
            mismatches.append(
                f"{tool.name}: wrapper params {list(wrapper_params)} != "
                f"server params {list(server_params)}"
            )

    assert mismatches == [], "\n".join(mismatches)


def test_pdf_url_builds_static_path_with_page_fragment():
    assert demo_app._pdf_url("candgui.pdf", 28) == "/app/static/rulebooks/candgui.pdf#page=28"


def test_pdf_url_without_page_has_no_fragment():
    assert demo_app._pdf_url("candgui.pdf") == "/app/static/rulebooks/candgui.pdf"


def test_pdf_url_quotes_special_characters_but_not_slashes():
    url = demo_app._pdf_url("states/ca/AO (final).pdf")
    assert url == "/app/static/rulebooks/states/ca/AO%20%28final%29.pdf"


def test_sync_static_pdfs_mirrors_nested_structure_and_removes_stale_copies(tmp_path, monkeypatch):
    src_dir = tmp_path / "data_rulebooks"
    static_dir = tmp_path / "static_rulebooks"
    monkeypatch.setattr(demo_app, "DEFAULT_RULEBOOKS_DIR", src_dir)
    monkeypatch.setattr(demo_app, "STATIC_RULEBOOKS_DIR", static_dir)

    (src_dir / "states" / "ca").mkdir(parents=True)
    (src_dir / "candgui.pdf").write_bytes(b"fed")
    (src_dir / "states" / "ca" / "limits.pdf").write_bytes(b"ca")

    demo_app._sync_static_pdfs()

    assert (static_dir / "candgui.pdf").read_bytes() == b"fed"
    assert (static_dir / "states" / "ca" / "limits.pdf").read_bytes() == b"ca"

    # A PDF removed from data/rulebooks/ must not linger in static/rulebooks/.
    (src_dir / "candgui.pdf").unlink()
    demo_app._sync_static_pdfs()
    assert not (static_dir / "candgui.pdf").exists()
    assert (static_dir / "states" / "ca" / "limits.pdf").exists()


def test_sync_static_pdfs_skips_unchanged_files(tmp_path, monkeypatch):
    """Regression guard: this runs on every Streamlit rerun, so re-copying
    every PDF every time would make each rerun slower as more PDFs are
    added. A file whose size+mtime already match must not be re-copied."""
    src_dir = tmp_path / "data_rulebooks"
    static_dir = tmp_path / "static_rulebooks"
    monkeypatch.setattr(demo_app, "DEFAULT_RULEBOOKS_DIR", src_dir)
    monkeypatch.setattr(demo_app, "STATIC_RULEBOOKS_DIR", static_dir)

    src_dir.mkdir(parents=True)
    (src_dir / "candgui.pdf").write_bytes(b"fed")
    demo_app._sync_static_pdfs()

    mock_copy = MagicMock()
    monkeypatch.setattr(demo_app.shutil, "copy2", mock_copy)
    demo_app._sync_static_pdfs()
    mock_copy.assert_not_called()


def test_sync_static_pdfs_handles_missing_source_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(demo_app, "DEFAULT_RULEBOOKS_DIR", tmp_path / "nope")
    monkeypatch.setattr(demo_app, "STATIC_RULEBOOKS_DIR", tmp_path / "static")
    demo_app._sync_static_pdfs()  # must not raise


def test_split_citations_no_marker_returns_text_unchanged():
    text = "The individual contribution limit is $3,500 per election."
    prose, citations = demo_app._split_citations(text)
    assert prose == text
    assert citations == []


def test_split_citations_parses_source_line():
    text = (
        "The individual contribution limit is $3,500 per election.\n\n"
        "Sources:\n"
        "SOURCE | candgui.pdf | 28 | federal"
    )
    prose, citations = demo_app._split_citations(text)
    assert prose == "The individual contribution limit is $3,500 per election."
    assert citations == [
        {"kind": "source", "filename": "candgui.pdf", "page": "28", "jurisdiction": "federal"}
    ]


def test_split_citations_parses_ao_line_without_url():
    text = "AO 2014-02 is the controlling opinion.\n\nSources:\nAO | 2014-02 | Final"
    prose, citations = demo_app._split_citations(text)
    assert prose == "AO 2014-02 is the controlling opinion."
    assert citations == [{"kind": "ao", "ao_no": "2014-02", "status": "Final", "url": ""}]


def test_split_citations_parses_ao_line_with_url():
    text = (
        "AO 2014-02 is the controlling opinion.\n\n"
        "Sources:\n"
        "AO | 2014-02 | Final | https://www.fec.gov/files/legal/aos/2014-02/2014-02.pdf"
    )
    prose, citations = demo_app._split_citations(text)
    assert citations == [
        {
            "kind": "ao",
            "ao_no": "2014-02",
            "status": "Final",
            "url": "https://www.fec.gov/files/legal/aos/2014-02/2014-02.pdf",
        }
    ]


def test_split_citations_handles_multiple_citations_and_bullet_prefixes():
    text = (
        "Answer combining both a rulebook page and an AO.\n\n"
        "Sources:\n"
        "- SOURCE | candgui.pdf | 121 | federal\n"
        "* AO | 2014-02 | Final"
    )
    prose, citations = demo_app._split_citations(text)
    assert prose == "Answer combining both a rulebook page and an AO."
    assert len(citations) == 2
    assert citations[0]["kind"] == "source"
    assert citations[1]["kind"] == "ao"


def test_split_citations_falls_back_to_full_text_when_nothing_parses():
    """Regression guard: a "Sources:" block that's present but entirely
    malformed (e.g. the model didn't follow the format) must not silently
    swallow the citation-looking text -- fall back to showing everything
    rather than a garbled partial parse."""
    text = "Some answer.\n\nSources:\nI looked at a few pages but won't list them."
    prose, citations = demo_app._split_citations(text)
    assert prose == text
    assert citations == []


def test_citation_chip_html_source_links_to_pdf_page():
    chip = demo_app._citation_chip_html(
        {"kind": "source", "filename": "candgui.pdf", "page": "28", "jurisdiction": "federal"}
    )
    assert '/app/static/rulebooks/candgui.pdf#page=28' in chip
    assert "FEDERAL" in chip
    assert "candgui.pdf, p. 28" in chip
    assert "<a " in chip


def test_citation_chip_html_source_with_non_numeric_page_omits_fragment():
    chip = demo_app._citation_chip_html(
        {"kind": "source", "filename": "candgui.pdf", "page": "n/a", "jurisdiction": "federal"}
    )
    assert "#page=" not in chip
    assert "/app/static/rulebooks/candgui.pdf" in chip


def test_citation_chip_html_ao_without_url_is_not_a_link():
    chip = demo_app._citation_chip_html({"kind": "ao", "ao_no": "2014-02", "status": "Final", "url": ""})
    assert "<a " not in chip
    assert "AO 2014-02" in chip
    assert "FINAL" in chip


def test_citation_chip_html_ao_with_url_is_a_link():
    chip = demo_app._citation_chip_html(
        {
            "kind": "ao",
            "ao_no": "2014-02",
            "status": "Final",
            "url": "https://www.fec.gov/files/legal/aos/2014-02/2014-02.pdf",
        }
    )
    assert '<a class="fec-cite" href="https://www.fec.gov/files/legal/aos/2014-02/2014-02.pdf"' in chip


def test_citation_chip_html_escapes_html_in_fields():
    chip = demo_app._citation_chip_html(
        {"kind": "source", "filename": "<script>.pdf", "page": "1", "jurisdiction": "federal"}
    )
    assert "<script>" not in chip
    assert "&lt;script&gt;" in chip


def test_today_addendum_reflects_the_real_current_date(monkeypatch):
    """Regression guard: the raw Anthropic API has no built-in notion of
    "today" (unlike Claude Desktop/Code's own client scaffolding), so
    without this the model can't tell which upcoming deadline is actually
    next. Must be computed fresh per call, not fixed at import time -- a
    long-running demo session that started on one date must still report
    the real date on a later one."""
    import datetime as datetime_module

    class FakeDate(datetime_module.date):
        @classmethod
        def today(cls):
            return cls(2030, 5, 17)

    monkeypatch.setattr(demo_app, "date", FakeDate)
    assert "2030-05-17" in demo_app._today_addendum()


def test_jurisdiction_label_spells_out_known_state_codes():
    assert demo_app._jurisdiction_label("ca") == "California"
    assert demo_app._jurisdiction_label("ny") == "New York"
    assert demo_app._jurisdiction_label("federal") == "Federal"


def test_jurisdiction_label_falls_back_to_the_code_for_unknown_jurisdictions():
    """Regression guard: an unrecognized two-letter code (e.g. a US
    territory not in the lookup table) must still render as something
    readable, not raise or silently disappear from the sidebar."""
    assert demo_app._jurisdiction_label("zz") == "ZZ"


# -- running async server tools from Streamlit ------------------------------


class _FakeOpenFECClient:
    """Stands in for OpenFECClient so no test goes near the network.

    Construction is slowed on purpose: _client() holds the lock while it
    builds the client, and that window is where two threads sharing one
    lock collide. The real one builds an httpx.AsyncClient there, which
    is not free either.
    """

    def __init__(self, *args, **kwargs):
        self.closed = False
        time.sleep(0.002)

    async def aclose(self):
        self.closed = True


def test_each_loop_gets_its_own_openfec_client(monkeypatch):
    """The client wraps an httpx.AsyncClient, which refuses to be used
    from a loop other than the one that created it. _run_async gives each
    call its own loop, so one cached for the process would be poison from
    the second call onward."""
    import asyncio

    from fec_mcp import server

    monkeypatch.setattr(server, "OpenFECClient", _FakeOpenFECClient)
    seen = [demo_app._run_async(lambda **_kw: server._client()) for _ in range(3)]

    assert len({id(client) for client in seen}) == 3
    assert all(client.closed for client in seen)
    # and nothing is left behind for a loop that has gone
    assert server._clients == {} and server._client_locks == {}
    del asyncio


def test_concurrent_script_runs_do_not_collide_over_the_client(monkeypatch):
    """Regression guard for a traceback the user hit twice.

    Streamlit runs each session's script in its own thread, so two runs
    can be in flight at once -- two tabs, or a rerun starting before the
    previous one finished its OpenFEC calls. When the client and its lock
    were process-wide, one thread would end up awaiting a lock the other
    held in a loop it could not see:

        RuntimeError: <asyncio.locks.Lock ... [unlocked, waiters:1]> is
        bound to a different event loop

    and sometimes wedge instead of raising. Against the pre-fix code this
    harness produced three of those and left a thread hung; the assert on
    `wedged` is what catches the hang, since a deadlock fails no
    assertion on its own.
    """
    import threading

    from fec_mcp import server

    monkeypatch.setattr(server, "OpenFECClient", _FakeOpenFECClient)

    failures: list[BaseException] = []

    def script_run():
        try:
            for _ in range(8):
                demo_app._run_async(lambda **_kw: server._client())
        except Exception as exc:  # noqa: BLE001
            failures.append(exc)

    threads = [threading.Thread(target=script_run, daemon=True) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    wedged = [thread for thread in threads if thread.is_alive()]
    assert not failures, f"cross-loop failure: {failures[0]!r}"
    assert not wedged, f"{len(wedged)} script run(s) deadlocked on the client lock"


# -- committee search filtering ---------------------------------------------

# Verbatim from a live "abdul" search. OpenFEC's committee endpoint is
# full-text and also matches the linked candidate's name, which it does
# not return on the record -- both JAMAL rows are that case.
ABDUL_SEARCH_RESULTS = [
    {"committee_id": "C00958066", "name": "ABDUL FOR MICHIGAN VICTORY FUND"},
    {"committee_id": "C00902668", "name": "ABDUL FOR U.S. SENATE"},
    {"committee_id": "C00936682", "name": "ABDULLE FOR CONGRESS COMMITTEE"},
    {"committee_id": "C00238931", "name": "ABDUL-RAHMAN, SOLOMON"},
    {"committee_id": "C00248104", "name": "COMMITTEE TO ELECT ABDUL ALIM MUHAMMAD"},
    {"committee_id": "C00631234", "name": "JAMAL FOR CONGRESS"},
    {"committee_id": "C00682351", "name": "JAMAL FOR CONGRESS"},
]


def test_committee_search_keeps_only_committee_name_matches():
    """The field asks for a committee name or ID, so a committee matching
    only somewhere else in the FEC's records is not an answer to it."""
    kept, dropped = demo_app._name_matches_only(ABDUL_SEARCH_RESULTS, "abdul")

    assert [c["committee_id"] for c in kept] == [
        "C00958066", "C00902668", "C00936682", "C00238931", "C00248104",
    ]
    assert dropped == 2


def test_committee_search_filtering_is_case_insensitive():
    kept, _ = demo_app._name_matches_only(ABDUL_SEARCH_RESULTS, "ABDUL")
    assert len(kept) == 5


def test_committee_search_matches_a_name_substring():
    """ABDULLE contains "abdul" -- a prefix search would drop it, and it
    is a legitimate committee-name match."""
    kept, _ = demo_app._name_matches_only(ABDUL_SEARCH_RESULTS, "abdul")
    assert any(c["name"] == "ABDULLE FOR CONGRESS COMMITTEE" for c in kept)


def test_committee_search_reports_how_many_it_set_aside():
    """A search that quietly shows three of seven results looks broken;
    the count is what lets the caller say so."""
    _, dropped = demo_app._name_matches_only(ABDUL_SEARCH_RESULTS, "jamal")
    assert dropped == 5


def test_committee_search_with_no_name_matches_returns_nothing_kept():
    kept, dropped = demo_app._name_matches_only(ABDUL_SEARCH_RESULTS, "zzz")
    assert kept == []
    assert dropped == len(ABDUL_SEARCH_RESULTS)


def test_committee_search_handles_a_missing_name_field():
    kept, dropped = demo_app._name_matches_only([{"committee_id": "C1"}], "abdul")
    assert kept == []
    assert dropped == 1


def test_jurisdiction_sort_key_puts_federal_first_then_states_alphabetically():
    jurisdictions = ["ny", "federal", "ca", "ga"]
    ordered = sorted(jurisdictions, key=demo_app._jurisdiction_sort_key)
    assert ordered == ["federal", "ca", "ga", "ny"]


# -- how dates are shown ----------------------------------------------------


def test_deadline_dates_render_month_first():
    """US compliance calendars read MM-DD-YYYY; the FEC's own API speaks
    ISO, so the conversion happens at the last possible moment."""
    assert demo_app._us_date("2026-10-15") == "10-15-2026"
    assert demo_app._us_date("2027-01-31") == "01-31-2027"


def test_an_unreadable_date_is_shown_as_it_came_rather_than_blanked():
    """A date this cannot parse is still more use on screen than nothing."""
    assert demo_app._us_date("sometime in October") == "sometime in October"


def test_a_missing_date_renders_as_empty():
    assert demo_app._us_date(None) == ""


def test_the_agenda_row_shows_the_us_ordered_date():
    row = {"date": "2026-11-20", "_committee": "ELI CRANE FOR CONGRESS",
           "deadline": "Post-General Report Due", "report_type": "post_general"}
    assert "11-20-2026" in demo_app._agenda_row_html(row)
    assert "2026-11-20" not in demo_app._agenda_row_html(row)
