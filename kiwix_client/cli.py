"""CLI for the Kiwix client library.

Usage:
    kiwix-client books [query]
    kiwix-client search <query> [--book <slug>] [--start <n>]
    kiwix-client fetch <url>
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .client import KiwixClient
from .parse import strip_html


def main() -> None:
    parser = argparse.ArgumentParser(description="Kiwix client CLI")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("KIWIX_BASE_URL", ""),
        help="Kiwix server base URL (or set KIWIX_BASE_URL)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_books = sub.add_parser("books", help="List available ZIM books")
    p_books.add_argument("query", nargs="?", default="", help="Optional title filter")

    p_search = sub.add_parser("search", help="Full-text search")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--book", default="", help="Book slug to scope search")
    p_search.add_argument("--start", type=int, default=0, help="Result offset (page size 25)")

    p_fetch = sub.add_parser("fetch", help="Fetch an article as plain text")
    p_fetch.add_argument("url", help="Relative article URL from search results")

    args = parser.parse_args()

    if not args.base_url:
        print("error: --base-url or KIWIX_BASE_URL is required", file=sys.stderr)
        sys.exit(1)

    with KiwixClient(args.base_url) as client:
        if args.command == "books":
            books = client.list_books(q=args.query)
            print(json.dumps([
                {
                    "slug": b.slug,
                    "title": b.title,
                    "name": b.name,
                    "summary": b.summary,
                    "language": b.language,
                    "category": b.category,
                    "article_count": b.article_count,
                }
                for b in books
            ], indent=2))

        elif args.command == "search":
            sr = client.search(pattern=args.query, books=args.book, start=args.start)
            print(json.dumps({
                "query": sr.query,
                "total": sr.total,
                "start_index": sr.start_index,
                "page_length": sr.page_length,
                "results": [
                    {
                        "title": r.title,
                        "book": r.book,
                        "url": r.url,
                        "snippet": r.snippet,
                        "word_count": r.word_count,
                    }
                    for r in sr.results
                ],
            }, indent=2))

        elif args.command == "fetch":
            html = client.fetch_article(args.url)
            print(strip_html(html))


if __name__ == "__main__":
    main()
