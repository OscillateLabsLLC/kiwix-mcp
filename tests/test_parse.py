"""Offline unit tests for parsing logic, using synthetic fixtures in testdata/."""
from __future__ import annotations

from pathlib import Path

import pytest

from kiwix_client.parse import (
    _fix_opds_ampersands,
    parse_opds_feed,
    parse_search_html,
    strip_html,
)

TESTDATA = Path(__file__).parent / "testdata"


# ---------------------------------------------------------------------------
# OPDS feed parsing
# ---------------------------------------------------------------------------

class TestParseOPDSFeed:
    def test_jq_book_fields(self):
        data = (TESTDATA / "opds_feed.xml").read_bytes()
        books = parse_opds_feed(data)
        jq = next((b for b in books if b.name == "devdocs_en_jq"), None)
        assert jq is not None, "jq book not found in catalog"
        assert jq.title == "jq Docs"
        assert jq.slug == "devdocs_en_jq_2025-10"
        assert jq.summary == "jq documentation, by DevDocs"
        assert jq.language == "eng"
        assert jq.article_count == 1
        assert jq.updated_at is not None

    def test_filtered_feed(self):
        data = (TESTDATA / "opds_feed_filtered.xml").read_bytes()
        books = parse_opds_feed(data)
        assert len(books) == 1
        assert books[0].name == "devdocs_en_jq"

    def test_entity_decoding_apos(self):
        data = (TESTDATA / "opds_feed.xml").read_bytes()
        books = parse_opds_feed(data)
        xkcd = next((b for b in books if b.name == "explainxkcd_en_all"), None)
        assert xkcd is not None
        assert xkcd.summary == "It's 'cause you're dumb"

    def test_entity_decoding_amp(self):
        data = (TESTDATA / "opds_feed.xml").read_bytes()
        books = parse_opds_feed(data)
        printing = next(
            (b for b in books if b.name == "3dprinting.stackexchange.com_en_all"), None
        )
        assert printing is not None
        assert printing.title == "3D Printing Q&A"


# ---------------------------------------------------------------------------
# fixOPDSAmpersands / fixBareAmps
# ---------------------------------------------------------------------------

class TestFixAmpersands:
    @pytest.mark.parametrize("inp, want", [
        # bare & in href query string — must be escaped
        ('href="/catalog?count=500&start=0"', 'href="/catalog?count=500&amp;start=0"'),
        # multiple bare & in href
        ('href="/catalog?count=500&q=jq&start=0"', 'href="/catalog?count=500&amp;q=jq&amp;start=0"'),
        # already-escaped & in href — must be preserved
        ('href="/catalog?a=1&amp;b=2"', 'href="/catalog?a=1&amp;b=2"'),
        # numeric entity in href — must be preserved
        ('href="/page?x=&#123;"', 'href="/page?x=&#123;"'),
        # text content with valid entities — must be untouched
        ("<title>It&apos;s Q&amp;A</title>", "<title>It&apos;s Q&amp;A</title>"),
        # mix: bare & in href, valid entities in surrounding text
        (
            '<summary>Q&amp;A</summary><link href="/x?a=1&b=2"/>',
            '<summary>Q&amp;A</summary><link href="/x?a=1&amp;b=2"/>',
        ),
    ])
    def test_fix_opds_ampersands(self, inp: str, want: str):
        result = _fix_opds_ampersands(inp.encode()).decode()
        assert result == want, f"input={inp!r}\ngot ={result!r}\nwant={want!r}"


# ---------------------------------------------------------------------------
# Search HTML parsing
# ---------------------------------------------------------------------------

class TestParseSearchHTML:
    def test_results_page1(self):
        html = (TESTDATA / "search_filter.html").read_text()
        sr = parse_search_html(html, "filter", 0)

        assert sr.total > 0
        assert sr.page_length == 25
        assert sr.start_index == 0
        assert len(sr.results) == 25

        r = sr.results[0]
        assert r.book == "example_wiki_en_all_2024-01"
        assert r.url == "/example_wiki_en_all_2024-01/A/FR:BeCikloXmlFond"
        assert r.path == "A/FR:BeCikloXmlFond"

    def test_results_page2(self):
        html = (TESTDATA / "search_filter_page2.html").read_text()
        sr = parse_search_html(html, "filter", 25)

        assert sr.start_index == 25
        assert len(sr.results) > 0
        for i, r in enumerate(sr.results):
            assert r.book != "", f"results[{i}].book is empty (url={r.url})"

    def test_empty_results(self):
        html = (TESTDATA / "search_empty.html").read_text()
        sr = parse_search_html(html, "noresultsxyzzy", 0)
        assert len(sr.results) == 0

    def test_book_scoped(self):
        html = (TESTDATA / "search_book_scoped.html").read_text()
        sr = parse_search_html(html, "map", 0)

        assert sr.total > 0
        assert len(sr.results) > 0

        from_scoped = sum(1 for r in sr.results if r.book == "example_docs_en_all_2024-01")
        assert from_scoped > 0, "expected at least one result from scoped book"
        assert from_scoped >= len(sr.results) // 2, (
            f"expected majority from scoped book, got {from_scoped}/{len(sr.results)}"
        )


# ---------------------------------------------------------------------------
# strip_html
# ---------------------------------------------------------------------------

class TestStripHTML:
    @pytest.mark.parametrize("inp, want", [
        ("<p>Hello <b>world</b></p>", "Hello world"),
        ('<a href="/foo">link text</a>', "link text"),
        ("&amp; &lt; &gt; &quot;", "& < > \""),
        ("&nbsp;text&nbsp;", "text"),
        ("  <div>  lots   of   space  </div>  ", "lots of space"),
        ("no tags here", "no tags here"),
        ("...<b>filter</b> output", "... filter output"),
    ])
    def test_strip_html(self, inp: str, want: str):
        assert strip_html(inp) == want, f"input={inp!r}"
