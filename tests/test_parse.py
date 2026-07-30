"""Offline unit tests for parsing logic, using synthetic fixtures in testdata/."""
from __future__ import annotations

from pathlib import Path

import pytest

from kiwix_client.parse import (
    _fix_opds_ampersands,
    parse_opds_feed,
    parse_search_html,
    extract_article_text,
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

    @pytest.mark.parametrize("inp, absent, present", [
        # Wikipedia ZIM articles open with an inline stylesheet; a tag-strip keeps
        # the CSS body, which then gets spoken aloud.
        ("<style>.mw-parser-output{font-style:italic}</style><p>Newton was a "
         "mathematician.</p>", "font-style", "mathematician"),
        # Gutenberg ZIM articles embed a JSON locale blob in a <script>.
        ('<script>{"default_locale": "en"}</script><p>Real prose here.</p>',
         "default_locale", "Real prose"),
        ("<noscript><p>Enable JavaScript</p></noscript><p>Actual text.</p>",
         "Enable JavaScript", "Actual text"),
    ])
    def test_non_prose_element_contents_are_dropped(self, inp, absent, present):
        """Script/style *contents* must be removed, not merely untagged.

        Found by fetching live Wikipedia and Gutenberg articles: summaries came back
        full of CSS rules and JSON, because the regex tag-strip only removed the
        surrounding tags.
        """
        out = strip_html(inp)
        assert absent not in out
        assert present in out

    def test_strip_html_handles_empty_input(self):
        assert strip_html("") == ""


# ---------------------------------------------------------------------------
# extract_article_text
# ---------------------------------------------------------------------------

class TestExtractArticleText:
    LEAD = (
        "Sir Isaac Newton was an English polymath active as a mathematician, "
        "physicist and astronomer who formulated the laws of motion."
    )

    def _wikipedia_page(self, body: str) -> str:
        return (
            '<html><body><div id="mw-content-text">'
            '<div class="hatnote">For other uses, see Isaac Newton '
            "(disambiguation).</div>"
            '<table class="infobox"><tr><th>Born</th>'
            "<td>(1643-01-04) 4 January 1643</td></tr>"
            "<tr><th>Died</th><td>31 March 1727</td></tr></table>"
            f"{body}</div></body></html>"
        )

    def test_infobox_and_hatnote_are_excluded(self):
        """Live Wikipedia summaries opened with "Born ( 1643-01-04 ) 4 January 1643"
        because the infobox sits inside the content container."""
        out = extract_article_text(self._wikipedia_page(f"<p>{self.LEAD}</p>"))
        assert out.startswith("Sir Isaac Newton")
        assert "1643-01-04" not in out
        assert "disambiguation" not in out

    def test_infobox_containing_paragraphs_is_still_excluded(self):
        """Paragraph-only extraction already skips <td> text, so this is what makes
        the table removal load-bearing: an infobox whose cells contain real <p>
        elements, long enough to clear the length filter."""
        html = (
            '<html><body><div id="mw-content-text">'
            '<table class="infobox"><tr><td><p>Born 4 January 1643 in '
            "Woolsthorpe, a hamlet in the county of Lincolnshire, England.</p>"
            "</td></tr></table>"
            f"<p>{self.LEAD}</p></div></body></html>"
        )
        out = extract_article_text(html)
        assert "Woolsthorpe" not in out
        assert out.startswith("Sir Isaac Newton")

    def test_unclassed_table_paragraphs_are_excluded(self):
        """Not every data table carries an .infobox class; tabular text reads as
        nonsense aloud regardless of how it is labelled."""
        html = (
            '<html><body><div id="mw-content-text">'
            "<table><tr><td><p>Population figures by census year, listed for each "
            "administrative region of the country.</p></td></tr></table>"
            f"<p>{self.LEAD}</p></div></body></html>"
        )
        out = extract_article_text(html)
        assert "Population figures" not in out
        assert out.startswith("Sir Isaac Newton")

    def test_pronunciation_guides_are_dropped(self):
        """IPA is phonetic symbols; a TTS engine reads them as noise."""
        body = (
            '<p>Photosynthesis (<span class="IPA">/ˌfoʊtəˈsɪnθəsɪs/</span>) is a '
            "biological process used by many cellular organisms.</p>"
        )
        out = extract_article_text(self._wikipedia_page(body))
        assert "ˈsɪnθəsɪs" not in out
        assert "biological process" in out

    def test_returns_empty_for_a_page_with_no_prose(self):
        """Older kiwix-tools answers a missing article with HTTP 200 and the library
        landing page. It has no paragraphs, so "" is the caller's signal that the
        title-index entry was stale."""
        landing = (
            '<html><body><div class="kiwix_searchform"><input></div>'
            '<div class="kiwix_button_cont"><a>Home</a></div>'
            '<div class="main_title">Project Gutenberg Library</div>'
            "</body></html>"
        )
        assert extract_article_text(landing) == ""

    def test_short_fragments_are_skipped(self):
        """Captions and bylines are paragraphs too, but not worth speaking."""
        body = f"<p>Fig 1.</p><p>By A. Author</p><p>{self.LEAD}</p>"
        out = extract_article_text(self._wikipedia_page(body))
        assert out.startswith("Sir Isaac Newton")
        assert "Fig 1." not in out

    def test_falls_back_when_no_known_container(self):
        """Non-MediaWiki corpora still yield their paragraphs."""
        html = f"<html><body><p>{self.LEAD}</p></body></html>"
        assert "Sir Isaac Newton" in extract_article_text(html)

    def test_non_paragraph_containers_are_not_treated_as_prose(self):
        """Navigation and metadata live in <div>s long enough to pass the length
        filter; only <p> is treated as prose."""
        html = (
            '<html><body><div id="mw-content-text">'
            '<div class="navigation">Browse the library index by author, by title, '
            "by language, or by recently added works.</div>"
            f"<p>{self.LEAD}</p></div></body></html>"
        )
        out = extract_article_text(html)
        assert "Browse the library" not in out
        assert out.startswith("Sir Isaac Newton")

    def test_lead_section_is_preferred_over_preceding_credits(self):
        """wikiHow puts "This article was co-authored by wikiHow Staff..." in an
        unclassed paragraph *before* #mf-section-0. It carries no distinguishing
        class, so scoping to the lead section is what separates it from the article.
        """
        html = (
            '<html><body><div id="mw-content-text">'
            "<p>This article was co-authored by wikiHow Staff. Our trained team of "
            "editors and researchers validate articles for accuracy.</p>"
            '<div id="mf-section-0"><p>Purifying water can be done through a variety '
            "of methods, like using a filter, treating with chemicals, or boiling."
            "</p></div></div></body></html>"
        )
        out = extract_article_text(html)
        assert out.startswith("Purifying water")
        assert "co-authored" not in out

    def test_falls_back_when_lead_section_has_no_prose(self):
        """An empty lead section must not shadow the rest of the article."""
        html = (
            '<html><body><div id="mw-content-text">'
            '<div id="mf-section-0"><p>Too short.</p></div>'
            f"<p>{self.LEAD}</p></div></body></html>"
        )
        assert "Sir Isaac Newton" in extract_article_text(html)

    def test_handles_empty_input(self):
        assert extract_article_text("") == ""
