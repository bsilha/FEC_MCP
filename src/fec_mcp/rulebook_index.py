"""Full-text search over official campaign-finance rulebook PDFs.

Users drop official guides (PDFs) into ``data/rulebooks/``:

- Federal (FEC) guides go directly in ``data/rulebooks/*.pdf`` -- e.g. the
  campaign guides for candidates, party committees, PACs, and the
  contribution-limits chart.
- State guides go in ``data/rulebooks/states/{state_code}/*.pdf``, where
  ``state_code`` is the lowercase two-letter USPS code (e.g. ``ca``, ``ny``).
  Each state's regulating agency differs (e.g. California's FPPC), and not
  every state publishes a single consolidated guide the way the FEC does --
  add whatever official PDFs are available for a given state.

On first use this module extracts text page-by-page with pypdf and builds a
SQLite FTS5 index cached in ``data/rulebooks/.index/index.sqlite3``. The
index is rebuilt automatically whenever the set of PDFs (or their
size/mtime) changes.

This is the authoritative source for contribution limits and compliance
rules in this server -- it quotes official documents with page citations
rather than relying on any hardcoded or model-recalled figures.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULEBOOKS_DIR = REPO_ROOT / "data" / "rulebooks"

FEDERAL_JURISDICTION = "federal"


@dataclass
class SearchHit:
    source: str
    title: str
    page: int
    snippet: str
    score: float
    jurisdiction: str


@dataclass
class SourceInfo:
    filename: str
    title: str
    pages: int
    jurisdiction: str


def _build_fts_query(query: str) -> str:
    """Turn free-text user input into a safe, forgiving FTS5 MATCH expression.

    Strips FTS5 special syntax (keeping only letters and digits per token) so
    arbitrary input can't break the query, then OR-joins the terms. Hyphens
    are stripped too, not preserved: FTS5's query parser treats a hyphen as a
    NOT/column-filter operator (so "in-kind" errors out as an invalid query),
    while the index's own tokenizer already splits stored text on hyphens
    (so "in-kind" is indexed as separate "in"/"kind" tokens) -- preserving
    hyphens in the query just created a mismatch that silently dropped
    matches. FTS5's default is to AND adjacent bareword tokens, which is too
    strict for natural-language questions: a page matching 11 of 12 query
    words would be excluded entirely. ORing the terms and relying on bm25()
    ranking (already used by callers) surfaces the best-matching pages first
    while still returning partial matches instead of nothing.
    """
    cleaned = re.sub(r"[^\w\s]", " ", query, flags=re.UNICODE)
    reserved = {"OR", "AND", "NOT", "NEAR"}
    tokens = [f'"{t}"' if t in reserved else t for t in cleaned.split()]
    return " OR ".join(tokens)


def _pdf_title(reader: PdfReader, fallback: str) -> str:
    try:
        meta_title = reader.metadata.title if reader.metadata else None
    except Exception:
        meta_title = None
    if meta_title and meta_title.strip():
        return meta_title.strip()
    return fallback


def _jurisdiction_for(rel_path: Path) -> str:
    """Derive a source's jurisdiction from its path relative to rulebooks_dir.

    ``states/{state_code}/whatever.pdf`` -> the lowercased state_code.
    Anything else (i.e. directly under rulebooks_dir) -> "federal".
    """
    parts = rel_path.parts
    if len(parts) >= 3 and parts[0] == "states":
        return parts[1].lower()
    return FEDERAL_JURISDICTION


def _manifest(pdf_paths: list[tuple[Path, str]]) -> list[dict]:
    return sorted(
        (
            {"source": rel, "size": p.stat().st_size, "mtime": p.stat().st_mtime}
            for p, rel in pdf_paths
        ),
        key=lambda d: d["source"],
    )


class RulebookIndex:
    def __init__(self, rulebooks_dir: Path | None = None):
        self.rulebooks_dir = rulebooks_dir or DEFAULT_RULEBOOKS_DIR
        self.index_dir = self.rulebooks_dir / ".index"
        self.db_path = self.index_dir / "index.sqlite3"
        self._conn: sqlite3.Connection | None = None

    def _pdf_paths(self) -> list[tuple[Path, str]]:
        """Return (absolute_path, relative_posix_path) for every PDF under
        rulebooks_dir, searched recursively so states/{code}/*.pdf is picked
        up alongside the top-level federal PDFs."""
        if not self.rulebooks_dir.exists():
            return []
        pairs = [
            (p, p.relative_to(self.rulebooks_dir).as_posix())
            for p in self.rulebooks_dir.rglob("*.pdf")
        ]
        return sorted(pairs, key=lambda pair: pair[1])

    def _connect(self) -> sqlite3.Connection:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS manifest (data TEXT NOT NULL)")
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5("
            "source, title, page UNINDEXED, text, tokenize='porter unicode61'"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sources ("
            "source TEXT PRIMARY KEY, title TEXT, page_count INTEGER, jurisdiction TEXT"
            ")"
        )
        return conn

    def _current_manifest(self, conn: sqlite3.Connection) -> list[dict] | None:
        row = conn.execute("SELECT data FROM manifest LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def ensure_built(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn

        conn = self._connect()
        pdf_paths = self._pdf_paths()
        wanted_manifest = _manifest(pdf_paths)
        stored_manifest = self._current_manifest(conn)

        if stored_manifest != wanted_manifest:
            self._rebuild(conn, pdf_paths, wanted_manifest)

        self._conn = conn
        return conn

    def _rebuild(
        self, conn: sqlite3.Connection, pdf_paths: list[tuple[Path, str]], manifest: list[dict]
    ) -> None:
        conn.execute("DELETE FROM pages")
        conn.execute("DELETE FROM sources")
        conn.execute("DELETE FROM manifest")

        for abs_path, rel_source in pdf_paths:
            jurisdiction = _jurisdiction_for(Path(rel_source))
            try:
                reader = PdfReader(str(abs_path))
            except Exception as exc:
                # Skip unreadable/corrupt PDFs rather than failing the whole index.
                conn.execute(
                    "INSERT INTO sources (source, title, page_count, jurisdiction) VALUES (?, ?, ?, ?)",
                    (rel_source, f"[UNREADABLE: {exc}]", 0, jurisdiction),
                )
                continue

            title = _pdf_title(reader, fallback=abs_path.stem.replace("_", " ").replace("-", " ").title())
            num_pages = len(reader.pages)
            conn.execute(
                "INSERT INTO sources (source, title, page_count, jurisdiction) VALUES (?, ?, ?, ?)",
                (rel_source, title, num_pages, jurisdiction),
            )
            for i, page in enumerate(reader.pages, start=1):
                try:
                    text = page.extract_text() or ""
                except Exception:
                    text = ""
                if text.strip():
                    conn.execute(
                        "INSERT INTO pages (source, title, page, text) VALUES (?, ?, ?, ?)",
                        (rel_source, title, i, text),
                    )

        conn.execute("INSERT INTO manifest (data) VALUES (?)", (json.dumps(manifest),))
        conn.commit()

    def list_sources(self, jurisdiction: str | None = None) -> list[SourceInfo]:
        conn = self.ensure_built()
        sql = "SELECT source, title, page_count, jurisdiction FROM sources"
        params: list = []
        if jurisdiction:
            sql += " WHERE jurisdiction = ?"
            params.append(jurisdiction.lower())
        sql += " ORDER BY jurisdiction, source"
        rows = conn.execute(sql, params).fetchall()
        return [SourceInfo(filename=r[0], title=r[1], pages=r[2], jurisdiction=r[3]) for r in rows]

    def list_jurisdictions(self) -> list[dict]:
        """List every jurisdiction with loaded sources (e.g. "federal", "ca"),
        each with a source count -- lets callers discover which states (if
        any) have rulebooks loaded without guessing state codes."""
        conn = self.ensure_built()
        rows = conn.execute(
            "SELECT jurisdiction, COUNT(*) FROM sources GROUP BY jurisdiction ORDER BY jurisdiction"
        ).fetchall()
        return [{"jurisdiction": r[0], "source_count": r[1]} for r in rows]

    def search(
        self,
        query: str,
        top_k: int = 8,
        source: str | None = None,
        jurisdiction: str | None = None,
    ) -> list[SearchHit]:
        conn = self.ensure_built()
        fts_query = _build_fts_query(query)
        if not fts_query:
            return []

        sql = (
            "SELECT p.source, p.title, p.page, "
            "snippet(pages, 3, '>>>', '<<<', ' ... ', 40) AS snip, "
            "bm25(pages) AS rank, "
            "s.jurisdiction "
            "FROM pages AS p "
            "JOIN sources AS s ON s.source = p.source "
            "WHERE pages MATCH ?"
        )
        params: list = [fts_query]
        if source:
            sql += " AND p.source = ?"
            params.append(source)
        if jurisdiction:
            sql += " AND s.jurisdiction = ?"
            params.append(jurisdiction.lower())
        sql += " ORDER BY rank LIMIT ?"
        params.append(top_k)

        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []

        return [
            SearchHit(source=r[0], title=r[1], page=r[2], snippet=r[3], score=r[4], jurisdiction=r[5])
            for r in rows
        ]

    def get_page_text(self, source: str, page: int) -> str | None:
        conn = self.ensure_built()
        row = conn.execute(
            "SELECT text FROM pages WHERE source = ? AND page = ?", (source, page)
        ).fetchone()
        return row[0] if row else None
