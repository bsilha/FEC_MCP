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
