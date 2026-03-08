"""Kiwix HTTP server client.

Wraps three API surfaces:
  - OPDS catalog (Atom XML) at /catalog/v2/entries
  - Full-text search at /search (HTML, scraped)
  - Article fetch at /{book_slug}/A/{path} (HTML)
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urljoin, urlparse

import httpx

from .parse import (
    Book,
    SearchResponse,
    SearchResult,
    parse_opds_feed,
    parse_search_html,
    strip_html,
)

__all__ = ["KiwixClient"]


class KiwixClient:
    """Client for a Kiwix HTTP server.

    Parameters
    ----------
    base_url:
        Full URL to the kiwix-serve instance, optionally including a path
        prefix (e.g. ``http://localhost:8080`` or
        ``http://host:3000/kiwix``).
    timeout:
        Request timeout in seconds.
    """

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        parsed = urlparse(self._base_url)
        # origin is used for article URLs, which are absolute paths from root
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "KiwixClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Catalog
    # ------------------------------------------------------------------

    def list_books(self, q: str = "") -> List[Book]:
        """Return all books from the OPDS catalog.

        Parameters
        ----------
        q:
            Optional title keyword to filter server-side.
        """
        params = {"count": "500", "start": "0"}
        if q:
            params["q"] = q
        resp = self._client.get(
            f"{self._base_url}/catalog/v2/entries", params=params
        )
        resp.raise_for_status()
        return parse_opds_feed(resp.content)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        pattern: str,
        books: str = "",
        start: int = 0,
    ) -> SearchResponse:
        """Full-text search across all books or a specific book slug.

        Parameters
        ----------
        pattern:
            Search query string.
        books:
            Optional book slug to restrict search. Leave empty to search all.
        start:
            Zero-based result offset (page size is 25).

        Raises
        ------
        ValueError
            When the server rejects the search because books span multiple
            languages and no book scope was provided.
        httpx.HTTPStatusError
            On other HTTP errors.
        """
        params = {"pattern": pattern, "start": str(start)}
        if books:
            params["books.name"] = books

        resp = self._client.get(f"{self._base_url}/search", params=params)

        if resp.status_code == 400 and "confusion-of-tongues" in resp.text:
            raise ValueError(
                "search requires a book scope: this server has books in multiple "
                "languages and cannot search across them. Use list_books() to find "
                "a book slug, then pass it as the 'books' argument."
            )
        resp.raise_for_status()
        return parse_search_html(resp.text, pattern, start)

    # ------------------------------------------------------------------
    # Article fetch
    # ------------------------------------------------------------------

    def fetch_article(self, relative_url: str) -> str:
        """Fetch an article by its relative URL and return raw HTML.

        Parameters
        ----------
        relative_url:
            Relative URL as returned by :meth:`search`, e.g.
            ``/book_slug/A/Article_Title``.
        """
        resp = self._client.get(self._origin + relative_url)
        resp.raise_for_status()
        return resp.text
