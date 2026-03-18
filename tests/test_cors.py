"""Tests for CORS support on HTTP transports.

These verify that build_cors_app() — the function we own — correctly wires
CORS middleware onto MCP's ASGI app.  The original bug was that browser-based
MCP clients got 405 on OPTIONS preflight; these tests confirm that path works.
"""
from __future__ import annotations

from starlette.testclient import TestClient

from kiwix_mcp.__main__ import build_cors_app
from kiwix_mcp.server import create_server

from tests.test_mcp import MockKiwixClient


def _build(transport: str = "streamable-http", cors_origins: str = "*") -> TestClient:
    """Run build_cors_app with a mock client and return a TestClient."""
    mcp = create_server(MockKiwixClient(), host="127.0.0.1", port=8000)
    app = build_cors_app(mcp, transport, cors_origins)
    return TestClient(app)


class TestPreflightNot405:
    """The original bug: OPTIONS preflight on /mcp returned 405, blocking browsers."""

    def test_streamable_http_options_succeeds(self):
        client = _build(transport="streamable-http")
        resp = client.options(
            "/mcp",
            headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    def test_sse_options_succeeds(self):
        client = _build(transport="sse")
        resp = client.options(
            "/sse",
            headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"


class TestOriginParsing:
    """build_cors_app splits comma-separated origins — test that our parsing works."""

    def test_wildcard_default(self):
        client = _build(cors_origins="*")
        resp = client.options(
            "/mcp",
            headers={"Origin": "http://anything.example.com", "Access-Control-Request-Method": "POST"},
        )
        assert resp.headers["access-control-allow-origin"] == "*"

    def test_single_origin_allowed(self):
        client = _build(cors_origins="http://myapp.local:3000")
        resp = client.options(
            "/mcp",
            headers={"Origin": "http://myapp.local:3000", "Access-Control-Request-Method": "POST"},
        )
        assert resp.headers["access-control-allow-origin"] == "http://myapp.local:3000"

    def test_single_origin_rejects_other(self):
        client = _build(cors_origins="http://myapp.local:3000")
        resp = client.options(
            "/mcp",
            headers={"Origin": "http://evil.example.com", "Access-Control-Request-Method": "POST"},
        )
        assert "access-control-allow-origin" not in resp.headers

    def test_comma_separated_origins(self):
        client = _build(cors_origins="http://app1.example.com, http://app2.example.com")
        # First origin works
        resp = client.options(
            "/mcp",
            headers={"Origin": "http://app1.example.com", "Access-Control-Request-Method": "POST"},
        )
        assert resp.headers["access-control-allow-origin"] == "http://app1.example.com"
        # Second origin works (with whitespace trimmed)
        resp = client.options(
            "/mcp",
            headers={"Origin": "http://app2.example.com", "Access-Control-Request-Method": "POST"},
        )
        assert resp.headers["access-control-allow-origin"] == "http://app2.example.com"

    def test_env_var_style_no_spaces(self):
        """CORS_ALLOW_ORIGINS="http://a,http://b" (no spaces) must also work."""
        client = _build(cors_origins="http://a.example.com,http://b.example.com")
        resp = client.options(
            "/mcp",
            headers={"Origin": "http://b.example.com", "Access-Control-Request-Method": "POST"},
        )
        assert resp.headers["access-control-allow-origin"] == "http://b.example.com"
