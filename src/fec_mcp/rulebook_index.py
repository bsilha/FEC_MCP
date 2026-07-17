"""Full-text search over official FEC rulebook PDFs.

Users drop the FEC's own campaign guides / contribution-limits charts (PDFs)
into ``data/rulebooks/``. On first use this module extracts text page-by-page
with pypdf and builds a SQLite FTS5 index cached in
``data/rulebooks/.index/index.sqlite3``. The index is rebuilt automatically
whenever the set of PDFs (or their size/mtime) changes.

This is the authoritative source for contribution limits and compliance
rules in this server -- it quotes the FEC's own documents with page
citations rather than relying on any hardcoded or model-recalled figures.
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


@dataclass
class SearchHit:
    source: str
    title: str
    page: int
    snippet: str
    score: float


@dataclass
class SourceInfo:
    filename: str
    title: str
    pages: int


def _sanitize_fts_query(query: str) -> str:
    """Strip FTS5 special syntax so arbitrary user input can't break the query.

    Keeps letters, digits, whitespace and hyphens; everything else becomes a
    space. Adjacent bareword tokens are ANDed together by FTS5 by default.
    """
    cleaned = re.sub(r"[^\w\s-]", " ", query, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _pdf_title(reader: PdfReader, fallback: str) -> str:
    try:
        meta_title = reader.metadata.title if reader.metadata else None
    except Exception:
        meta_title = None
    if meta_title and meta_title.strip():
        return meta_title.strip()
    return fallback


def _manifest(pdf_paths: list[Path]) -> list[dict]:
    return sorted(
        (
            {"filename": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime}
            for p in pdf_paths
        ),
        key=lambda d: d["filename"],
    )


class RulebookIndex:
    def __init__(self, rulebooks_dir: Path | None = None):
        self.rulebooks_dir = rulebooks_dir or DEFAULT_RULEBOOKS_DIR
        self.index_dir = self.rulebooks_dir / ".index"
        self.db_path = self.index_dir / "index.sqlite3"
        self._conn: sqlite3.Connection | None = None

    def _pdf_paths(self) -> list[Path]:
        if not self.rulebooks_dir.exists():
            return []
        return sorted(self.rulebooks_dir.glob("*.pdf"))

    def _connect(self) -> sqlite3.Connection:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS manifest (data TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5("
            "source, title, page UNINDEXED, text, tokenize='porter unicode61'"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sources (filename TEXT PRIMARY KEY, title TEXT, pages INTEGER)"
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

    def _rebuild(self, conn: sqlite3.Connection, pdf_paths: list[Path], manifest: list[dict]) -> None:
        conn.execute("DELETE FROM pages")
        conn.execute("DELETE FROM sources")
        conn.execute("DELETE FROM manifest")

        for pdf_path in pdf_paths:
            try:
                reader = PdfReader(str(pdf_path))
            except Exception as exc:
                # Skip unreadable/corrupt PDFs rather than failing the whole index.
                conn.execute(
                    "INSERT INTO sources (filename, title, pages) VALUES (?, ?, ?)",
                    (pdf_path.name, f"[UNREADABLE: {exc}]", 0),
                )
                continue

            title = _pdf_title(reader, fallback=pdf_path.stem.replace("_", " ").replace("-", " ").title())
            num_pages = len(reader.pages)
            conn.execute(
                "INSERT INTO sources (filename, title, pages) VALUES (?, ?, ?)",
                (pdf_path.name, title, num_pages),
            )
            for i, page in enumerate(reader.pages, start=1):
                try:
                    text = page.extract_text() or ""
                except Exception:
                    text = ""
                if text.strip():
                    conn.execute(
                        "INSERT INTO pages (source, title, page, text) VALUES (?, ?, ?, ?)",
                        (pdf_path.name, title, i, text),
                    )

        conn.execute("INSERT INTO manifest (data) VALUES (?)", (json.dumps(manifest),))
        conn.commit()

    def list_sources(self) -> list[SourceInfo]:
        conn = self.ensure_built()
        rows = conn.execute("SELECT filename, title, pages FROM sources ORDER BY filename").fetchall()
        return [SourceInfo(filename=r[0], title=r[1], pages=r[2]) for r in rows]

    def search(self, query: str, top_k: int = 8, source: str | None = None) -> list[SearchHit]:
        conn = self.ensure_built()
        fts_query = _sanitize_fts_query(query)
        if not fts_query:
            return []

        sql = (
            "SELECT source, title, page, "
            "snippet(pages, 3, '>>>', '<<<', ' ... ', 40) AS snip, "
            "bm25(pages) AS rank "
            "FROM pages WHERE pages MATCH ?"
        )
        params: list = [fts_query]
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY rank LIMIT ?"
        params.append(top_k)

        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []

        return [
            SearchHit(source=r[0], title=r[1], page=r[2], snippet=r[3], score=r[4])
            for r in rows
        ]

    def get_page_text(self, source: str, page: int) -> str | None:
        conn = self.ensure_built()
        row = conn.execute(
            "SELECT text FROM pages WHERE source = ? AND page = ?", (source, page)
        ).fetchone()
        return row[0] if row else None
