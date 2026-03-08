"""MCP server wrapping the Kiwix client.

Exposed tools:
  - kiwix_list_books    — list available ZIM books from the catalog
  - kiwix_search        — full-text search across all books or a specific book
  - kiwix_fetch_article — retrieve article content as plain text
"""
from __future__ import annotations


from mcp.server.fastmcp import FastMCP

from kiwix_client import KiwixClient, strip_html
from kiwix_client.parse import Book, SearchResponse


def create_server(client: KiwixClient, host: str = "127.0.0.1", port: int = 8000) -> FastMCP:
    mcp = FastMCP("kiwix-mcp", host=host, port=port)

    # ------------------------------------------------------------------
    # kiwix_list_books
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "List ZIM books available on the Kiwix server. "
            "Optionally filter by title keyword."
        ),
    )
    def kiwix_list_books(query: str = "") -> str:
        """List available ZIM books.

        Args:
            query: Optional title keyword to filter books (e.g. 'wikipedia').
        """
        books = client.list_books(q=query)
        return _format_books(books)

    # ------------------------------------------------------------------
    # kiwix_search
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Full-text search across Kiwix ZIM books. "
            "Returns article titles, snippets, and URLs. "
            "Use kiwix_fetch_article to retrieve full content."
        ),
    )
    def kiwix_search(query: str, book: str = "", start: int = 0) -> str:
        """Search Kiwix books.

        Args:
            query: Search query (required).
            book: Optional book slug to restrict search (e.g.
                'devdocs_en_rust_2025-10'). Leave empty to search all books.
            start: Zero-based result offset for pagination (page size is 25).
        """
        if not query:
            raise ValueError("query is required")
        try:
            sr = client.search(pattern=query, books=book, start=start)
        except ValueError as exc:
            if "book scope" in str(exc):
                return (
                    "This server requires a book to be specified for search. "
                    "Use kiwix_list_books to find a book slug, then retry "
                    "with the 'book' parameter."
                )
            raise
        return _format_search_response(sr)

    # ------------------------------------------------------------------
    # kiwix_fetch_article
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Fetch an article from the Kiwix server and return its content "
            "as plain text. Use the URL field from kiwix_search results."
        ),
    )
    def kiwix_fetch_article(url: str) -> str:
        """Fetch a Kiwix article as plain text.

        Args:
            url: Relative article URL as returned by kiwix_search
                (e.g. '/devdocs_en_rust_2025-10/A/std/vec/struct.Vec.html').
        """
        if not url:
            raise ValueError("url is required")
        html = client.fetch_article(url)
        return strip_html(html)

    return mcp


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _format_books(books: list[Book]) -> str:
    if not books:
        return "No books found."
    lines = [f"{len(books)} book(s):\n"]
    for b in books:
        lines.append(f"Slug:     {b.slug}")
        lines.append(f"Title:    {b.title}")
        if b.summary:
            lines.append(f"Summary:  {b.summary}")
        if b.category:
            lines.append(f"Category: {b.category}")
        lines.append(f"Articles: {b.article_count}")
        lines.append("")
    return "\n".join(lines)


def _format_search_response(sr: SearchResponse) -> str:
    if not sr.results:
        return f'No results for "{sr.query}".'
    range_end = sr.start_index + len(sr.results)
    lines = [
        f'Results {sr.start_index + 1}-{range_end} of {sr.total} for "{sr.query}":\n'
    ]
    for i, r in enumerate(sr.results):
        lines.append(f"{sr.start_index + i + 1}. {r.title}")
        lines.append(f"   Book:    {r.book}")
        lines.append(f"   URL:     {r.url}")
        if r.snippet:
            snippet = r.snippet[:300] + "…" if len(r.snippet) > 300 else r.snippet
            lines.append(f"   Snippet: {snippet}")
        if r.word_count:
            lines.append(f"   Words:   {r.word_count}")
        lines.append("")

    if range_end < sr.total:
        next_start = sr.start_index + sr.page_length
        lines.append(f"More results available — use start={next_start} for the next page.")

    return "\n".join(lines)
