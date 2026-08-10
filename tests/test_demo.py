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


def test_jurisdiction_sort_key_puts_federal_first_then_states_alphabetically():
    jurisdictions = ["ny", "federal", "ca", "ga"]
    ordered = sorted(jurisdictions, key=demo_app._jurisdiction_sort_key)
    assert ordered == ["federal", "ca", "ga", "ny"]
