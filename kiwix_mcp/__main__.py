"""Entry point: kiwix-mcp."""
from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Kiwix MCP server")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("KIWIX_BASE_URL", ""),
        help="Kiwix server base URL (or set KIWIX_BASE_URL)",
    )
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse", "streamable-http"],
        help="Transport protocol (default: stdio)",
    )
    args = parser.parse_args()

    if not args.base_url:
        print("error: --base-url or KIWIX_BASE_URL is required", file=sys.stderr)
        sys.exit(1)

    from kiwix_client import KiwixClient
    from kiwix_mcp.server import create_server

    client = KiwixClient(args.base_url)
    mcp = create_server(client)

    transport = args.transport
    print(f"kiwix-mcp starting ({transport}) → {args.base_url}", file=sys.stderr)
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
