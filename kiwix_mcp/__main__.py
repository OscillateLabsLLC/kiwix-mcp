"""Entry point: kiwix-mcp."""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.middleware.cors import CORSMiddleware


def build_cors_app(mcp: FastMCP, transport: str, cors_origins: str) -> Any:
    """Wrap an MCP HTTP app with CORS middleware.

    Returns the wrapped ASGI app ready for uvicorn.
    """
    app = mcp.streamable_http_app() if transport == "streamable-http" else mcp.sse_app()
    origins = [o.strip() for o in cors_origins.split(",")]
    return CORSMiddleware(
        app,
        allow_origins=origins,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Kiwix MCP server")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("KIWIX_BASE_URL", ""),
        help="Kiwix server base URL (or set KIWIX_BASE_URL)",
    )
    parser.add_argument(
        "--transport",
        default=os.environ.get("TRANSPORT", "stdio"),
        choices=["stdio", "sse", "streamable-http"],
        help="Transport protocol (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "127.0.0.1"),
        help="Bind host for HTTP transports (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8000")),
        help="Bind port for HTTP transports (default: 8000)",
    )
    parser.add_argument(
        "--cors-allow-origins",
        default=os.environ.get("CORS_ALLOW_ORIGINS", "*"),
        help="Comma-separated CORS allowed origins (or set CORS_ALLOW_ORIGINS, default: '*')",
    )
    args = parser.parse_args()

    if not args.base_url:
        print("error: --base-url or KIWIX_BASE_URL is required", file=sys.stderr)
        sys.exit(1)

    from kiwix_client import KiwixClient
    from kiwix_mcp.server import create_server

    client = KiwixClient(args.base_url)
    mcp = create_server(client, host=args.host, port=args.port)

    transport = args.transport
    print(f"kiwix-mcp starting ({transport}) → {args.base_url}", file=sys.stderr)

    if transport in ("streamable-http", "sse"):
        import uvicorn

        app = build_cors_app(mcp, transport, args.cors_allow_origins)
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        mcp.run(transport=transport)


if __name__ == "__main__":
    main()
