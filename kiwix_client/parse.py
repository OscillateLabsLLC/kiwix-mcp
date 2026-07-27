"""Parsing logic for Kiwix server responses.

Covers:
  - OPDS Atom XML catalog (with kiwix-serve's bare-& quirk in href attributes)
  - Full-text search HTML (scraped)
  - HTML stripping for plain-text article output
"""
from __future__ import annotations

import json
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
class Suggestion:
    """A title-index hit from ``/suggest``.

    Distinct from :class:`SearchResult`: suggestions carry no snippet or word count,
    only a title and the article path within the book.
    """

    title: str = ""
    path: str = ""       # path within the book, e.g. "A/Isaac_Newton"


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
# Suggestion parsing
# ---------------------------------------------------------------------------

def parse_suggestions(payload: str) -> List[Suggestion]:
    """Parse the JSON array returned by ``/suggest``.

    Entries whose ``kind`` is not ``"path"`` are dropped: kiwix-serve appends a
    full-text-search fallback entry ("containing '<term>'...") that carries no article
    path and is not an answer.

    Malformed payloads yield an empty list rather than raising — a missing title index
    should degrade to "no suggestions", not break the caller.
    """
    try:
        entries = json.loads(payload)
    except (ValueError, TypeError):
        return []
    if not isinstance(entries, list):
        return []

    suggestions: List[Suggestion] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "path":
            continue
        path = (entry.get("path") or "").strip()
        title = unescape((entry.get("value") or "").strip())
        if path and title:
            suggestions.append(Suggestion(title=title, path=path))
    return suggestions


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


#: Elements whose *contents* are not prose and must be dropped, not just untagged.
#: A bare tag-strip leaves CSS rules and JSON blobs inline, which then surface in
#: spoken summaries as gibberish.
_NON_PROSE_TAGS = ("script", "style", "noscript", "template")


def strip_html(html: str) -> str:
    """Remove HTML markup and return readable plain text.

    Drops the contents of script/style elements entirely; a regex tag-strip keeps
    them, which is how CSS rules and JSON language blobs ended up in article
    summaries fetched from live Wikipedia and Gutenberg ZIMs.
    """
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        # Fall back to the regex path rather than failing the whole fetch.
        return _RE_WHITESPACE.sub(" ", _html_decode(_RE_TAGS.sub(" ", html))).strip()

    for element in soup(_NON_PROSE_TAGS):
        element.decompose()
    return _RE_WHITESPACE.sub(" ", soup.get_text(" ")).strip()


# ---------------------------------------------------------------------------
# Article prose extraction
# ---------------------------------------------------------------------------

#: Containers holding the article body, most specific first. Wikipedia/MediaWiki ZIMs
#: use #mw-content-text; others fall back to broader wrappers.
_CONTENT_SELECTORS = (
    "#mw-content-text",
    "#bodyContent",
    "main",
    "article",
    "#content",
)

#: Structural furniture that contributes no spoken prose. Infoboxes are the worst
#: offender: they yield fragments like "Born ( 1643-01-04 ) 4 January 1643".
_FURNITURE_SELECTORS = (
    "table",
    "figure",
    "sup.reference",
    ".infobox",
    ".navbox",
    ".hatnote",
    ".thumb",
    ".mw-editsection",
    ".reflist",
    ".metadata",
    # Pronunciation guides are phonetic symbols; a TTS engine reads them as noise.
    ".IPA",
    ".rt-commentedText",
    "span[title^='Representation in the International Phonetic Alphabet']",
    "#kiwix_searchform",
    ".kiwix_searchform",
    ".kiwix_button_cont",
    # wikiHow editorial credits and the "co-authored by wikiHow Staff" blurb, which
    # precede the instructions. Deliberately narrow: the ".hasad" section *contains*
    # the real lead paragraph, so removing that wrapper would discard the answer.
    ".article_byline",
    ".ur_author",
    ".sp_helpful_rating_wrapper",
    ".toc",
    "#toc",
    ".mw-references-wrap",
)

#: A paragraph shorter than this is almost always a caption, byline or stray
#: fragment rather than a sentence worth speaking.
_MIN_PARAGRAPH_CHARS = 60

#: MediaWiki-derived ZIMs (Wikipedia, wikiHow) mark the article's opening section.
#: Preferring it skips credits and navigation that sit above the real prose.
_LEAD_SECTION_SELECTOR = "#mf-section-0, .mf-section-0"


def extract_article_text(html: str) -> str:
    """Extract readable prose from a Kiwix article, or "" if it has none.

    Returns only paragraph text from the article body, which solves two problems seen
    against live servers:

    - Wikipedia summaries opened with infobox fragments ("Born ( 1643-01-04 ) 4
      January 1643") because those sit inside the content container. Infoboxes are
      tables and contribute no <p>, so restricting to paragraphs drops them.
    - Older kiwix-tools builds answer a missing article with HTTP 200 and the library
      landing page rather than a 404. That page has no paragraphs at all, so an empty
      return is the signal a caller needs to treat it as "no article".

    Falls back to :func:`strip_html` only when a content container is found but holds
    no usable paragraphs, so non-MediaWiki corpora still yield something.
    """
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return strip_html(html)

    for element in soup(_NON_PROSE_TAGS):
        element.decompose()

    root = None
    for selector in _CONTENT_SELECTORS:
        root = soup.select_one(selector)
        if root is not None:
            break
    if root is None:
        root = soup.body or soup

    for selector in _FURNITURE_SELECTORS:
        for element in root.select(selector):
            element.decompose()

    # Prefer an explicit lead section when the corpus marks one. wikiHow puts
    # editorial credits in unclassed paragraphs *before* #mf-section-0, and they
    # carry no distinguishing class of their own — scoping to the lead section is
    # what separates them from the article without fragile per-site rules.
    lead = root.select_one(_LEAD_SECTION_SELECTOR)
    scope = lead if lead is not None else root

    paragraphs = [
        _RE_WHITESPACE.sub(" ", p.get_text(" ", strip=True)).strip()
        for p in scope.find_all("p")
    ]
    usable = [p for p in paragraphs if len(p) >= _MIN_PARAGRAPH_CHARS]
    if not usable and scope is not root:
        # Lead section held no prose; fall back to the whole article body.
        usable = [
            text for text in (
                _RE_WHITESPACE.sub(" ", p.get_text(" ", strip=True)).strip()
                for p in root.find_all("p")
            ) if len(text) >= _MIN_PARAGRAPH_CHARS
        ]
    return " ".join(usable)
