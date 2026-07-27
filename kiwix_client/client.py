"""Kiwix HTTP server client.

Wraps four API surfaces:
  - OPDS catalog (Atom XML) at /catalog/v2/entries
  - Full-text search at /search (HTML, scraped)
  - Title suggestions at /suggest (JSON) — far faster than /search on large ZIMs
  - Article fetch at /{book_slug}/A/{path} (HTML)
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import quote, urljoin, urlparse

import httpx

from .parse import (
    Book,
    SearchResponse,
    SearchResult,
    Suggestion,
    parse_opds_feed,
    parse_search_html,
    parse_suggestions,
    strip_html,
)

__all__ = ["KiwixClient"]

#: OPDS pagination size. kiwix-serve caps a single catalog request, so this is the
#: page we ask for rather than an arbitrary preference.
CATALOG_PAGE_SIZE = 500

#: Default number of title suggestions requested from /suggest.
DEFAULT_SUGGEST_COUNT = 10

#: Default per-request timeout, in seconds.
DEFAULT_TIMEOUT = 30.0


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

    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._base_url = base_url.rstrip("/")
        parsed = urlparse(self._base_url)
        # origin is used for article URLs, which are absolute paths from root
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        # Newer libkiwix servers 302 bare article paths to a /content/ prefixed URL.
        # httpx does not follow redirects by default, which would surface as an
        # HTTPStatusError on an otherwise valid article.
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

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
        params = {"count": str(CATALOG_PAGE_SIZE), "start": "0"}
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

        if resp.status_code == 400:
            raise ValueError(
                "search requires a book scope: kiwix-serve cannot search across "
                "all books on this server (typically because they span multiple "
                "languages or none was specified). Use list_books() to find a "
                "book slug, then pass it as the 'books' argument."
            )
        resp.raise_for_status()
        return parse_search_html(resp.text, pattern, start)

    # ------------------------------------------------------------------
    # Title suggestions
    # ------------------------------------------------------------------

    def suggest(
        self, term: str, book: str, count: int = DEFAULT_SUGGEST_COUNT
    ) -> List[Suggestion]:
        """Look up article titles matching ``term`` in ``book``.

        This queries the lightweight title index rather than the Xapian full-text
        index, and is dramatically faster on large ZIMs — measured against a
        6.86M-article Wikipedia ZIM, ``/suggest`` answered in 0.02-0.03s warm where
        ``/search`` took 6-26s cold. It is also a better semantic fit for voice: it
        ranks the article *named* by the query first, whereas full-text ranking
        surfaced a 101k-word book above the article of the same name.

        Returns an empty list when the server has no title index for the book.
        """
        params = {"term": term, "content": book, "count": str(count)}
        resp = self._client.get(f"{self._base_url}/suggest", params=params)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return parse_suggestions(resp.text)

    def article_url(self, book: str, path: str) -> str:
        """Build a fetchable relative URL for an article ``path`` within ``book``.

        ``/suggest`` returns only a path fragment, so the URL must be assembled here.
        Two live-server quirks are handled:

        - Paths may contain spaces and corpus-specific suffixes (Gutenberg returns
          ``A/Isaac Newton.6288.html``); unencoded, the request fails outright.
        - Servers disagree on the scheme: older kiwix-tools serves bare
          ``/{book}/{path}`` and 404s on ``/content/``; newer libkiwix 302s the bare
          form to ``/content/``. The bare form therefore works on both, given that
          redirects are followed (see :meth:`fetch_article`).
        """
        prefix = urlparse(self._base_url).path.rstrip("/")
        quoted = quote(path.lstrip("/"), safe="/")
        return f"{prefix}/{book}/{quoted}"

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
