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

    hits = idx.search("individual contribution limit")
    assert len(hits) == 1
    assert hits[0].source == "sample_guide.pdf"
    assert hits[0].page == 1

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
