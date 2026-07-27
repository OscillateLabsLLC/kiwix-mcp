"""Tests for KiwixClient HTTP error handling."""
from __future__ import annotations

import httpx
import pytest
import respx

from kiwix_client.client import KiwixClient


@respx.mock
def test_search_400_raises_valueerror_regardless_of_body_text():
    """kiwix-serve returns 400 when search needs a book scope. The client must
    convert this into a ValueError with an actionable message so the caller
    (server.py) can respond helpfully instead of leaking a raw HTTPStatusError.

    Prior bug (issue #8): the client only raised ValueError when the body
    contained 'confusion-of-tongues', which never matches actual kiwix-serve
    output. Any other 400 body escaped as HTTPStatusError.
    """
    respx.get("http://localhost:9090/search").mock(
        return_value=httpx.Response(400, text="<html>bad request</html>")
    )
    client = KiwixClient("http://localhost:9090")
    with pytest.raises(ValueError, match="book scope"):
        client.search(pattern="test")


@respx.mock
def test_fetch_article_follows_redirects():
    """Newer libkiwix servers 302 a bare article path to a /content/ prefixed URL.

    httpx does NOT follow redirects by default, so without follow_redirects=True this
    raised HTTPStatusError on a perfectly valid article. Verified against a live
    server: bare paths 302 on libkiwix but are served directly by older kiwix-tools,
    so the bare form is the one scheme that works everywhere — provided the redirect
    is followed.
    """
    respx.get("http://localhost:9090/book/A/Krill").mock(
        return_value=httpx.Response(
            302, headers={"Location": "/content/book/A/Krill"}
        )
    )
    respx.get("http://localhost:9090/content/book/A/Krill").mock(
        return_value=httpx.Response(200, text="<p>Krill are crustaceans.</p>")
    )

    client = KiwixClient("http://localhost:9090")
    assert "crustaceans" in client.fetch_article("/book/A/Krill")


@respx.mock
def test_search_400_message_references_list_books():
    """The error message must guide the caller toward list_books() so they can
    discover valid book slugs."""
    respx.get("http://localhost:9090/search").mock(
        return_value=httpx.Response(400, text="")
    )
    client = KiwixClient("http://localhost:9090")
    with pytest.raises(ValueError) as exc_info:
        client.search(pattern="test")
    assert "list_books" in str(exc_info.value)
