"""Offline unit tests for the MCP server layer, using a mock client."""
from __future__ import annotations

from typing import List

import pytest

from kiwix_client.parse import Book, SearchResponse, SearchResult
from kiwix_mcp.server import create_server, _format_books, _format_search_response


# ---------------------------------------------------------------------------
# Mock client
# ---------------------------------------------------------------------------

class MockKiwixClient:
    def __init__(
        self,
        books: List[Book] | None = None,
        search_response: SearchResponse | None = None,
        article: str = "",
        error: Exception | None = None,
    ):
        self._books = books or []
        self._search_response = search_response or SearchResponse()
        self._article = article
        self._error = error

    def list_books(self, q: str = "") -> List[Book]:
        if self._error:
            raise self._error
        return self._books

    def search(self, pattern: str, books: str = "", start: int = 0) -> SearchResponse:
        if self._error:
            raise self._error
        return self._search_response

    def fetch_article(self, relative_url: str) -> str:
        if self._error:
            raise self._error
        return self._article


# ---------------------------------------------------------------------------
# Helpers to invoke MCP tools via FastMCP
# ---------------------------------------------------------------------------

def _run_tool_sync(tool, kwargs: dict) -> str:
    """Run a FastMCP tool synchronously and return text output."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(tool.run(kwargs))
    finally:
        loop.close()
    # result is the return value of the tool function
    return result


# ---------------------------------------------------------------------------
# Formatter tests (pure, no MCP plumbing needed)
# ---------------------------------------------------------------------------

class TestFormatBooks:
    def test_no_books(self):
        assert _format_books([]) == "No books found."

    def test_with_books(self):
        books = [
            Book(title="jq Docs", slug="devdocs_en_jq_2025-10", name="devdocs_en_jq",
                 article_count=1, summary="jq docs"),
            Book(title="Rust Docs", slug="devdocs_en_rust_2025-10", name="devdocs_en_rust",
                 article_count=5),
        ]
        out = _format_books(books)
        assert "2 book(s)" in out
        assert "devdocs_en_jq_2025-10" in out
        assert "jq Docs" in out
        assert "devdocs_en_rust_2025-10" in out
        assert "jq docs" in out


class TestFormatSearch:
    def test_no_results(self):
        sr = SearchResponse(query="xyzzy", total=0, page_length=25)
        assert 'No results for "xyzzy"' in _format_search_response(sr)

    def test_with_results(self):
        sr = SearchResponse(
            query="filter",
            total=12392,
            start_index=0,
            page_length=25,
            results=[
                SearchResult(
                    title="FR:BeCikloXmlFond",
                    book="example_wiki_en_all_2024-01",
                    url="/example_wiki_en_all_2024-01/A/FR:BeCikloXmlFond",
                    snippet="...Filter output...",
                    word_count=12052,
                )
            ],
        )
        out = _format_search_response(sr)
        assert "Results 1-1 of 12392" in out
        assert "FR:BeCikloXmlFond" in out
        assert "/example_wiki_en_all_2024-01/A/FR:BeCikloXmlFond" in out
        assert "Filter output" in out
        assert "12052" in out

    def test_pagination_hint(self):
        results = [
            SearchResult(title="R", book="b", url="/b/A/p")
            for _ in range(25)
        ]
        sr = SearchResponse(query="test", total=100, start_index=25, page_length=25, results=results)
        out = _format_search_response(sr)
        assert "Results 26-50 of 100" in out
        assert "start=50" in out

    def test_no_next_hint_on_last_page(self):
        results = [SearchResult(title="R", book="b", url="/b/A/p") for _ in range(10)]
        sr = SearchResponse(query="test", total=10, start_index=0, page_length=25, results=results)
        out = _format_search_response(sr)
        assert "More results" not in out

    def test_snippet_truncated(self):
        sr = SearchResponse(
            query="q", total=1, start_index=0, page_length=25,
            results=[SearchResult(title="T", book="b", url="/b/A/p", snippet="x" * 400)],
        )
        out = _format_search_response(sr)
        assert "…" in out


# ---------------------------------------------------------------------------
# Tool wiring tests — verify the MCP layer calls the client correctly and
# handles errors it's responsible for. Output format is covered by formatter
# tests above; these focus on what the tool layer adds.
# ---------------------------------------------------------------------------

class TestMCPTools:
    def _make_server(self, **kwargs):
        return create_server(MockKiwixClient(**kwargs))

    def _tool(self, mcp, name: str):
        return mcp._tool_manager.get_tool(name)

    def test_list_books_passes_query_to_client(self):
        """Tool must forward the query parameter to the client."""
        mock = MockKiwixClient()
        mcp = create_server(mock)
        # Capture what q value the client receives
        received = []
        original = mock.list_books
        def capturing(q=""):
            received.append(q)
            return original(q=q)
        mock.list_books = capturing
        _run_tool_sync(self._tool(mcp, "kiwix_list_books"), {"query": "rust"})
        assert received == ["rust"]

    def test_search_passes_book_and_start_to_client(self):
        """Tool must forward book and start parameters to the client."""
        mock = MockKiwixClient(search_response=SearchResponse(query="q", total=0, page_length=25))
        mcp = create_server(mock)
        received = []
        original = mock.search
        def capturing(pattern, books="", start=0):
            received.append((pattern, books, start))
            return original(pattern=pattern, books=books, start=start)
        mock.search = capturing
        _run_tool_sync(self._tool(mcp, "kiwix_search"), {"query": "q", "book": "mybook_2025", "start": 25})
        assert received == [("q", "mybook_2025", 25)]

    def test_search_book_scope_error_returns_actionable_message(self):
        """The confusion-of-tongues error must produce a user-actionable message, not a raw exception."""
        mock = MockKiwixClient(
            error=ValueError(
                "search requires a book scope: this server has books in multiple languages"
            )
        )
        mcp = create_server(mock)
        out = _run_tool_sync(self._tool(mcp, "kiwix_search"), {"query": "test"})
        assert "kiwix_list_books" in out
        assert "'book'" in out

    def test_fetch_article_passes_url_and_strips_html(self):
        """Tool must pass the URL to the client and strip HTML from the result."""
        html = "<html><body><h1>Hello</h1><p>World &amp; stuff</p></body></html>"
        mock = MockKiwixClient(article=html)
        mcp = create_server(mock)
        received_urls = []
        original = mock.fetch_article
        def capturing(relative_url):
            received_urls.append(relative_url)
            return original(relative_url)
        mock.fetch_article = capturing
        out = _run_tool_sync(self._tool(mcp, "kiwix_fetch_article"), {"url": "/b/A/Hello"})
        assert received_urls == ["/b/A/Hello"]
        assert "Hello" in out
        assert "World & stuff" in out
        assert "<" not in out


# ---------------------------------------------------------------------------
# Dynamic descriptions + override plumbing (from issue #8)
# ---------------------------------------------------------------------------

def _get_tool_description(mcp, tool_name: str) -> str:
    """Extract the registered description for a given MCP tool."""
    import asyncio
    tools = asyncio.run(mcp.list_tools())
    for t in tools:
        if t.name == tool_name:
            return t.description or ""
    raise KeyError(f"tool {tool_name!r} not registered")


class TestDynamicDescriptions:
    """create_server should adapt kiwix_search description to book count."""

    def test_single_book_server_gets_simplified_search_description(self):
        """On a single-book server the 'book' parameter is irrelevant, so the
        description should omit any call-list-first nagging."""
        mock = MockKiwixClient(books=[
            Book(slug="only", title="Only Book", summary="", category="", article_count=0)
        ])
        mcp = create_server(mock)
        desc = _get_tool_description(mcp, "kiwix_search")
        assert "REQUIRED" not in desc
        assert "kiwix_list_books" not in desc  # no nagging on single-book servers

    def test_multi_book_server_gets_nagging_search_description(self):
        """Multiple books → description must tell the model to list first and
        mark 'book' as required, because models otherwise skip that step."""
        mock = MockKiwixClient(books=[
            Book(slug="a", title="A", summary="", category="", article_count=0),
            Book(slug="b", title="B", summary="", category="", article_count=0),
            Book(slug="c", title="C", summary="", category="", article_count=0),
        ])
        mcp = create_server(mock)
        desc = _get_tool_description(mcp, "kiwix_search")
        assert "kiwix_list_books" in desc
        assert "3" in desc  # book count surfaced so the model knows the situation

    def test_probe_failure_falls_back_to_static_description(self):
        """If list_books() raises at startup, we must still create a working
        server with the static default description rather than crashing."""
        mock = MockKiwixClient(error=RuntimeError("server unreachable"))
        # Must not raise even though list_books fails:
        mcp = create_server(mock)
        desc = _get_tool_description(mcp, "kiwix_search")
        # Static default contains this phrasing:
        assert "Full-text search across Kiwix ZIM books" in desc

    def test_auto_describe_false_uses_static(self):
        """Callers that want the static description regardless of server
        state can pass auto_describe=False."""
        mock = MockKiwixClient(books=[
            Book(slug="a", title="A", summary="", category="", article_count=0),
            Book(slug="b", title="B", summary="", category="", article_count=0),
        ])
        mcp = create_server(mock, auto_describe=False)
        desc = _get_tool_description(mcp, "kiwix_search")
        # Static default — no book count, no REQUIRED nagging:
        assert "REQUIRED" not in desc
        assert "2" not in desc


class TestDescriptionOverrides:
    """Explicit overrides win over auto-compute and static defaults."""

    def test_search_description_override_wins_over_autocompute(self):
        mock = MockKiwixClient(books=[
            Book(slug="a", title="A", summary="", category="", article_count=0),
            Book(slug="b", title="B", summary="", category="", article_count=0),
        ])
        mcp = create_server(mock, search_description="custom search desc")
        assert _get_tool_description(mcp, "kiwix_search") == "custom search desc"

    def test_list_books_description_override(self):
        mock = MockKiwixClient()
        mcp = create_server(mock, list_books_description="custom list desc")
        assert _get_tool_description(mcp, "kiwix_list_books") == "custom list desc"

    def test_fetch_description_override(self):
        mock = MockKiwixClient()
        mcp = create_server(mock, fetch_description="custom fetch desc")
        assert _get_tool_description(mcp, "kiwix_fetch_article") == "custom fetch desc"
