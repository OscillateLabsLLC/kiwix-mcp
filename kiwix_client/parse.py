"""Parsing logic for Kiwix server responses.

Covers:
  - OPDS Atom XML catalog (with kiwix-serve's bare-& quirk in href attributes)
  - Full-text search HTML (scraped)
  - HTML stripping for plain-text article output
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from typing import IO, List, Optional
from urllib.parse import urlparse

import defusedxml.ElementTree as dET
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Data types (mirrors Go structs)
# ---------------------------------------------------------------------------

@dataclass
class Book:
    id: str = ""
    title: str = ""
    name: str = ""        # e.g. "devdocs_en_jq"
    slug: str = ""        # e.g. "devdocs_en_jq_2025-10"
    summary: str = ""
    language: str = ""
    category: str = ""
    article_count: int = 0
    updated_at: Optional[datetime] = None


@dataclass
class SearchResult:
    book: str = ""        # book slug
    path: str = ""        # article path within book
    title: str = ""
    snippet: str = ""
    word_count: int = 0
    url: str = ""         # full relative URL e.g. /book_slug/A/path


@dataclass
class SearchResponse:
    query: str = ""
    start_index: int = 0
    page_length: int = 25
    total: int = 0
    results: List[SearchResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# OPDS feed parsing
# ---------------------------------------------------------------------------

# Matches href="..." attribute values — only these get amp-fixed.
_RE_HREF = re.compile(r'href="([^"]*)"')

# Named XML entities that are valid and should not be escaped.
_VALID_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}


def _fix_bare_amps(s: str) -> str:
    """Escape bare & in s, preserving valid XML entity references."""
    if "&" not in s:
        return s
    parts = s.split("&")
    out = [parts[0]]
    for part in parts[1:]:
        semi = part.find(";")
        if semi > 0:
            name = part[:semi]
            if name in _VALID_ENTITIES or (name.startswith("#") and len(name) > 1):
                out.append("&")
                out.append(part)
                continue
        out.append("&amp;")
        out.append(part)
    return "".join(out)


def _fix_opds_ampersands(raw: bytes) -> bytes:
    """Fix bare & in href attributes emitted by kiwix-serve."""
    def fix_href(m: re.Match) -> str:
        return f'href="{_fix_bare_amps(m.group(1))}"'
    return _RE_HREF.sub(fix_href, raw.decode("utf-8")).encode("utf-8")


_ATOM_NS = "http://www.w3.org/2005/Atom"
_DC_NS   = "http://purl.org/dc/terms/"

def _at(el: ET.Element, tag: str, ns: str = _ATOM_NS) -> str:
    child = el.find(f"{{{ns}}}{tag}")
    return (child.text or "").strip() if child is not None else ""


def parse_opds_feed(data: bytes) -> List[Book]:
    fixed = _fix_opds_ampersands(data)
    root = dET.fromstring(fixed)

    books: List[Book] = []
    for entry in root.findall(f"{{{_ATOM_NS}}}entry"):
        b = Book(
            id=_at(entry, "id"),
            title=_at(entry, "title"),
            name=_at(entry, "name"),
            summary=_at(entry, "summary"),
            language=_at(entry, "language"),
            category=_at(entry, "category"),
        )

        updated_str = _at(entry, "updated")
        if updated_str:
            try:
                b.updated_at = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
            except ValueError:
                pass

        article_str = _at(entry, "articleCount")
        if article_str.isdigit():
            b.article_count = int(article_str)

        for link in entry.findall(f"{{{_ATOM_NS}}}link"):
            if link.get("type") == "text/html":
                href = link.get("href", "")
                # href may be "/slug" or "/prefix/content/slug" — take last segment
                parts = [p for p in href.split("/") if p]
                b.slug = parts[-1] if parts else ""

        books.append(b)

    return books


# ---------------------------------------------------------------------------
# Search HTML parsing
# ---------------------------------------------------------------------------

_RE_TOTAL   = re.compile(r"of\s*<b>\s*([\d,]+)\s*</b>")
_RE_PAGELEN = re.compile(r"pageLength=(\d+)")
_RE_RESULT  = re.compile(
    r'(?s)<li>\s*<a href="([^"]+)">\s*(.*?)\s*</a>(.*?)</li>'
)
_RE_CITE    = re.compile(r"(?s)<cite>(.*?)</cite>")
_RE_WORDS   = re.compile(r"([\d,]+)\s+words")
_RE_TAGS    = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    return unescape(_RE_TAGS.sub("", s).strip())


def parse_search_html(html: str, query: str, start: int) -> SearchResponse:
    sr = SearchResponse(query=query, start_index=start, page_length=25)

    m = _RE_TOTAL.search(html)
    if m:
        sr.total = int(m.group(1).replace(",", ""))

    m = _RE_PAGELEN.search(html)
    if m:
        sr.page_length = int(m.group(1))

    results_start = html.find('<div class="results">')
    if results_start == -1:
        return sr

    results_html = html[results_start:]
    for m in _RE_RESULT.finditer(results_html):
        href, title_raw, rest = m.group(1), m.group(2), m.group(3)

        result = SearchResult(url=href)

        # Decompose href: /{book_slug}/A/{path}
        parts = href.lstrip("/").split("/", 2)
        result.book = parts[0] if parts else ""
        if len(parts) >= 3:
            result.path = parts[1] + "/" + parts[2]

        result.title = _clean(title_raw)

        cite = _RE_CITE.search(rest)
        if cite:
            result.snippet = _clean(cite.group(1))

        wm = _RE_WORDS.search(rest)
        if wm:
            result.word_count = int(wm.group(1).replace(",", ""))

        sr.results.append(result)

    return sr


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

_RE_WHITESPACE = re.compile(r"\s+")

_HTML_ENTITIES = {
    "&amp;":  "&",
    "&lt;":   "<",
    "&gt;":   ">",
    "&quot;": '"',
    "&#39;":  "'",
    "&nbsp;": " ",
    "&#x27;": "'",
    "&#x2F;": "/",
}


def _html_decode(s: str) -> str:
    for entity, char in _HTML_ENTITIES.items():
        s = s.replace(entity, char)
    return s


def strip_html(html: str) -> str:
    """Remove HTML tags and decode basic entities, returning plain text."""
    stripped = _RE_TAGS.sub(" ", html)
    decoded = _html_decode(stripped)
    return _RE_WHITESPACE.sub(" ", decoded).strip()
