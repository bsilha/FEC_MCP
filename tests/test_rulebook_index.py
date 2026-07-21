import sqlite3
import time
from pathlib import Path

from fec_mcp import rulebook_index as ri
from fec_mcp.rulebook_index import RulebookIndex


class FakePage:
    def __init__(self, text: str):
        self._text = text

    def extract_text(self) -> str:
        return self._text


class FakeReader:
    """Stand-in for pypdf.PdfReader, keyed by the registry below."""

    registry: dict[str, list[str]] = {}

    def __init__(self, path: str):
        self.path = path
        self.pages = [FakePage(t) for t in self.registry[path]]
        self.metadata = None


def _write_dummy_pdf(path: Path, page_texts: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-fake-for-tests")
    FakeReader.registry[str(path)] = page_texts


def test_empty_directory_has_no_sources(tmp_path):
    idx = RulebookIndex(rulebooks_dir=tmp_path)
    assert idx.list_sources() == []
    assert idx.search("contribution limit") == []


def test_index_builds_and_finds_text(tmp_path, monkeypatch):
    monkeypatch.setattr(ri, "PdfReader", FakeReader)

    pdf_path = tmp_path / "sample_guide.pdf"
    _write_dummy_pdf(
        pdf_path,
        [
            "The individual contribution limit to a candidate committee is $3,500 per election.",
            "Political committees must file periodic reports with the Commission.",
        ],
    )

    idx = RulebookIndex(rulebooks_dir=tmp_path)
    sources = idx.list_sources()
    assert len(sources) == 1
    assert sources[0].filename == "sample_guide.pdf"
    assert sources[0].pages == 2
    assert sources[0].jurisdiction == "federal"

    hits = idx.search("individual contribution limit")
    assert len(hits) == 1
    assert hits[0].source == "sample_guide.pdf"
    assert hits[0].page == 1
    assert hits[0].jurisdiction == "federal"

    hits2 = idx.search("periodic reports")
    assert hits2[0].page == 2

    text = idx.get_page_text("sample_guide.pdf", 1)
    assert "3,500" in text

    assert idx.get_page_text("sample_guide.pdf", 99) is None


def test_index_rebuilds_when_pdf_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(ri, "PdfReader", FakeReader)

    pdf_path = tmp_path / "guide.pdf"
    _write_dummy_pdf(pdf_path, ["foreign national contributions are prohibited"])

    idx = RulebookIndex(rulebooks_dir=tmp_path)
    assert len(idx.search("foreign national")) == 1
    assert len(idx.search("disclaimer requirements")) == 0

    time.sleep(0.01)  # ensure mtime changes
    _write_dummy_pdf(
        pdf_path,
        ["foreign national contributions are prohibited", "disclaimer requirements apply"],
    )

    idx2 = RulebookIndex(rulebooks_dir=tmp_path)
    assert len(idx2.search("disclaimer requirements")) == 1


def test_query_sanitization_does_not_raise(tmp_path):
    idx = RulebookIndex(rulebooks_dir=tmp_path)
    assert idx.search('"unterminated OR NOT AND *') == []
    assert idx.search("") == []


def test_partial_term_match_ranks_but_does_not_exclude(tmp_path, monkeypatch):
    """Regression test: multi-word queries used to AND all terms together,
    so a page missing even one query word was excluded entirely. Terms are
    now OR-ed (with bm25 ranking), so a page matching most-but-not-all terms
    should still come back."""
    monkeypatch.setattr(ri, "PdfReader", FakeReader)

    pdf_path = tmp_path / "guide.pdf"
    _write_dummy_pdf(
        pdf_path,
        ["A leadership PAC contribution to a candidate authorized committee is limited."],
    )

    idx = RulebookIndex(rulebooks_dir=tmp_path)
    # Query includes "versus", which never appears in the page text.
    hits = idx.search("leadership PAC contribution limit versus candidate authorized committee")
    assert len(hits) == 1
    assert hits[0].page == 1


def test_hyphenated_query_terms_still_match(tmp_path, monkeypatch):
    """Regression test: FTS5 parses a bare hyphen as a NOT/column-filter
    operator, so querying "in-kind" used to raise a syntax error (swallowed
    into zero results) even when the page contains "in-kind" text (which the
    index itself tokenizes as separate "in"/"kind" tokens)."""
    monkeypatch.setattr(ri, "PdfReader", FakeReader)

    pdf_path = tmp_path / "guide.pdf"
    _write_dummy_pdf(pdf_path, ["Use of a corporate facility counts as an in-kind contribution."])

    idx = RulebookIndex(rulebooks_dir=tmp_path)
    hits = idx.search("in-kind contribution")
    assert len(hits) == 1
    assert hits[0].page == 1


def test_reserved_fts_keywords_as_literal_query_terms(tmp_path, monkeypatch):
    """A query containing the literal word "OR" (uppercase) must not raise,
    since bare uppercase OR/AND/NOT/NEAR are FTS5 operators."""
    monkeypatch.setattr(ri, "PdfReader", FakeReader)

    pdf_path = tmp_path / "guide.pdf"
    _write_dummy_pdf(pdf_path, ["Individual OR PAC contributions are both allowed."])

    idx = RulebookIndex(rulebooks_dir=tmp_path)
    hits = idx.search("Individual OR PAC")
    assert len(hits) == 1


def test_source_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(ri, "PdfReader", FakeReader)

    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    _write_dummy_pdf(a, ["disclaimer rules for committee A"])
    _write_dummy_pdf(b, ["disclaimer rules for committee B"])

    idx = RulebookIndex(rulebooks_dir=tmp_path)
    hits = idx.search("disclaimer rules", source="a.pdf")
    assert len(hits) == 1
    assert hits[0].source == "a.pdf"


def test_state_pdf_gets_state_jurisdiction(tmp_path, monkeypatch):
    monkeypatch.setattr(ri, "PdfReader", FakeReader)

    fed = tmp_path / "candgui.pdf"
    ca = tmp_path / "states" / "ca" / "limits.pdf"
    _write_dummy_pdf(fed, ["federal contribution limit rules"])
    _write_dummy_pdf(ca, ["california contribution limit rules"])

    idx = RulebookIndex(rulebooks_dir=tmp_path)
    sources = {s.filename: s for s in idx.list_sources()}

    assert sources["candgui.pdf"].jurisdiction == "federal"
    assert sources["states/ca/limits.pdf"].jurisdiction == "ca"


def test_list_jurisdictions(tmp_path, monkeypatch):
    monkeypatch.setattr(ri, "PdfReader", FakeReader)

    _write_dummy_pdf(tmp_path / "candgui.pdf", ["federal text"])
    _write_dummy_pdf(tmp_path / "states" / "ca" / "a.pdf", ["ca text one"])
    _write_dummy_pdf(tmp_path / "states" / "ca" / "b.pdf", ["ca text two"])
    _write_dummy_pdf(tmp_path / "states" / "ny" / "a.pdf", ["ny text"])

    idx = RulebookIndex(rulebooks_dir=tmp_path)
    jurisdictions = {j["jurisdiction"]: j["source_count"] for j in idx.list_jurisdictions()}

    assert jurisdictions == {"federal": 1, "ca": 2, "ny": 1}


def test_search_filtered_by_jurisdiction(tmp_path, monkeypatch):
    monkeypatch.setattr(ri, "PdfReader", FakeReader)

    _write_dummy_pdf(tmp_path / "candgui.pdf", ["contribution limit is $3500 federally"])
    _write_dummy_pdf(tmp_path / "states" / "ca" / "limits.pdf", ["contribution limit is different in california"])

    idx = RulebookIndex(rulebooks_dir=tmp_path)

    fed_hits = idx.search("contribution limit", jurisdiction="federal")
    assert len(fed_hits) == 1
    assert fed_hits[0].source == "candgui.pdf"

    ca_hits = idx.search("contribution limit", jurisdiction="ca")
    assert len(ca_hits) == 1
    assert ca_hits[0].source == "states/ca/limits.pdf"

    # Case-insensitive jurisdiction filter.
    ca_hits_upper = idx.search("contribution limit", jurisdiction="CA")
    assert len(ca_hits_upper) == 1

    all_hits = idx.search("contribution limit")
    assert len(all_hits) == 2


def test_list_sources_filtered_by_jurisdiction(tmp_path, monkeypatch):
    monkeypatch.setattr(ri, "PdfReader", FakeReader)

    _write_dummy_pdf(tmp_path / "candgui.pdf", ["federal text"])
    _write_dummy_pdf(tmp_path / "states" / "ca" / "a.pdf", ["ca text"])

    idx = RulebookIndex(rulebooks_dir=tmp_path)

    ca_only = idx.list_sources(jurisdiction="ca")
    assert [s.filename for s in ca_only] == ["states/ca/a.pdf"]

    fed_only = idx.list_sources(jurisdiction="federal")
    assert [s.filename for s in fed_only] == ["candgui.pdf"]


def test_migrates_cleanly_from_pre_jurisdiction_schema(tmp_path, monkeypatch):
    """Regression test: an index.sqlite3 built by the pre-jurisdiction schema
    (sources table with filename/pages columns, no jurisdiction) used to
    crash every query with "no such column: source", because CREATE TABLE
    IF NOT EXISTS is a no-op against an existing table with different
    columns. A schema-version bump must detect this and drop+rebuild rather
    than silently reusing the incompatible old tables."""
    monkeypatch.setattr(ri, "PdfReader", FakeReader)

    pdf_path = tmp_path / "candgui.pdf"
    _write_dummy_pdf(pdf_path, ["individual contribution limit is $3,500"])

    # Build an old-schema (pre-migration) cache by hand, exactly as the
    # prior version of this module would have.
    index_dir = tmp_path / ".index"
    index_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(index_dir / "index.sqlite3")
    conn.execute("CREATE TABLE manifest (data TEXT NOT NULL)")
    conn.execute(
        "CREATE VIRTUAL TABLE pages USING fts5(source, title, page UNINDEXED, text, tokenize='porter unicode61')"
    )
    conn.execute("CREATE TABLE sources (filename TEXT PRIMARY KEY, title TEXT, pages INTEGER)")
    conn.execute("INSERT INTO sources VALUES ('candgui.pdf', 'stale', 1)")
    conn.execute("INSERT INTO pages VALUES ('candgui.pdf', 'stale', 1, 'stale cached text')")
    conn.execute("INSERT INTO manifest (data) VALUES ('[]')")
    conn.commit()
    conn.close()

    idx = RulebookIndex(rulebooks_dir=tmp_path)
    sources = idx.list_sources()
    assert len(sources) == 1
    assert sources[0].jurisdiction == "federal"

    hits = idx.search("individual contribution limit")
    assert len(hits) == 1
    assert hits[0].page == 1
