# kiwix-mcp

[![Status: Active](https://img.shields.io/badge/status-active-brightgreen)](https://github.com/OscillateLabsLLC/.github/blob/main/SUPPORT_STATUS.md)
[![Run Unit Tests](https://github.com/OscillateLabsLLC/kiwix-mcp/actions/workflows/unit-tests.yml/badge.svg)](https://github.com/OscillateLabsLLC/kiwix-mcp/actions/workflows/unit-tests.yml)

MCP server and Python client library for [Kiwix](https://kiwix.org) HTTP servers.

## Compatibility

Tested against **kiwix-tools** (Debian `bookworm` package) and newer **libkiwix**-based deployments. Any standard `kiwix-serve` deployment should work.

Known quirks handled transparently:

- kiwix-serve emits unescaped `&` in OPDS catalog `href` attributes
- Newer servers require a book scope when the library spans multiple languages — the client returns a clear error with instructions in that case
- Newer servers use a path-prefixed URL scheme (e.g. `/kiwix/content/book_slug`) — slug and article URL handling adapts automatically

## Tools

| Tool                  | Description                                          |
| --------------------- | ---------------------------------------------------- |
| `kiwix_list_books`    | List available ZIM books; optional title filter      |
| `kiwix_search`        | Full-text search across all books or a specific book |
| `kiwix_fetch_article` | Fetch an article as plain text by URL                |

## Installation

```bash
pip install kiwix-mcp
```

Or with [uv](https://github.com/astral-sh/uv):

```bash
uv pip install kiwix-mcp
```

## Usage

### stdio (Claude Desktop / Claude Code)

```bash
kiwix-mcp --base-url http://localhost:8080
# or
KIWIX_BASE_URL=http://localhost:8080 kiwix-mcp
```

Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "kiwix": {
      "command": "kiwix-mcp",
      "args": ["--base-url", "http://localhost:8080"]
    }
  }
}
```

Claude Code — add at user scope so it's available in all projects:

```bash
claude mcp add --scope user --transport http kiwix https://your-kiwix-mcp-host/mcp/
```

### SSE

```bash
kiwix-mcp --transport sse --base-url http://localhost:8080
```

Then point your MCP client at `http://localhost:8000/sse`.

### HTTP (streamable)

```bash
kiwix-mcp --transport streamable-http --base-url http://localhost:8080
```

Then point your MCP client at `http://localhost:8000/mcp`.

### CORS (browser-based clients)

When using HTTP transports with browser-based MCP clients (e.g. llama-server WebUI), CORS headers are added automatically. By default all origins are allowed.

To restrict allowed origins:

```bash
kiwix-mcp --transport streamable-http --base-url http://localhost:8080 \
  --cors-allow-origins "http://localhost:3000,http://myapp.example.com"
# or
CORS_ALLOW_ORIGINS="http://localhost:3000" kiwix-mcp --transport streamable-http --base-url http://localhost:8080
```

CORS has no effect on stdio transport.

### Docker

```bash
docker run -e KIWIX_BASE_URL=http://your-kiwix-server:8080 \
  -p 8000:8000 \
  oscillatelabs/kiwix-mcp
```

Defaults to `streamable-http` transport bound on `0.0.0.0:8000`. Override with env vars:

```bash
docker run \
  -e KIWIX_BASE_URL=http://your-kiwix-server:8080 \
  -e TRANSPORT=sse \
  -e HOST=0.0.0.0 \
  -e PORT=8000 \
  -e CORS_ALLOW_ORIGINS="http://localhost:3000" \
  -p 8000:8000 \
  oscillatelabs/kiwix-mcp
```

## Options

| Flag                   | Default     | Env                  |
| ---------------------- | ----------- | -------------------- |
| `--base-url`           | —           | `KIWIX_BASE_URL`     |
| `--transport`          | `stdio`     | `TRANSPORT`          |
| `--host`               | `127.0.0.1` | `HOST`               |
| `--port`               | `8000`      | `PORT`               |
| `--cors-allow-origins` | `*`         | `CORS_ALLOW_ORIGINS` |

## Client library

```python
from kiwix_client import KiwixClient, strip_html

c = KiwixClient("http://localhost:8080")

# List all books
books = c.list_books()

# Filter by title keyword
books = c.list_books(q="wikipedia")

# Search
sr = c.search("query")

# Search within a specific book
sr = c.search("query", books="devdocs_en_rust_2025-10")

# Fetch article as plain text
html = c.fetch_article(sr.results[0].url)
plain = strip_html(html)
```

## How the Kiwix API works

`kiwix-serve` exposes three HTTP surfaces used by this client:

- **OPDS catalog** (`/catalog/v2/entries`) — Atom XML with book metadata and slugs; supports `?q=`, `?count=`, `?start=` params
- **Full-text search** (`/search?pattern=…&books.name=…&start=…`) — HTML; 25 results/page; `books.name=` scopes to a specific ZIM slug
- **Articles** (`/{book_slug}/A/{path}`) — HTML; use `strip_html` for plain text

There is no JSON API. Full-text search requires ZIMs built with `_ftindex:yes` — not all ZIMs include it. Servers with books spanning multiple languages require a book scope for any search request.

## Testing

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run the test suite (no network required)
pytest
```
