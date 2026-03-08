---
name: kiwix
description: Search and read articles from a local Kiwix server — offline Wikipedia, documentation, Stack Exchange, and other ZIM content. Use when the user asks to look something up, find documentation, or read an article from local knowledge bases.
allowed-tools: Bash(kiwix-client *)
---

Access offline knowledge from a local Kiwix server using the `kiwix-client` CLI. All commands output JSON except `fetch`, which outputs plain text.

## Commands

```
kiwix-client books [query]                        # List available ZIM books; optional title filter
kiwix-client search <query> [--book <slug>] [--start <n>]  # Full-text search; --start paginates by 25
kiwix-client fetch <url>                          # Fetch an article as plain text
```

Set `KIWIX_BASE_URL` or pass `--base-url <url>` to every command.

## Workflow

1. **If the user specifies a source** (e.g. "look this up in Wikipedia"): run `kiwix-client books <keyword>` to find the slug, then search within it using `--book`.
2. **If no source specified**: run `kiwix-client search <query>` to search all indexed books.
3. If search returns no results or a "book scope required" error, run `kiwix-client books` and retry with `--book`.
4. For detailed content: run `kiwix-client fetch <url>` with the URL from the search result.
5. Paginate with `--start 25`, `--start 50`, etc. if needed.

## Notes

- Not all books support full-text search (`_ftindex:yes` required). If a book returns no results, try another.
- `kiwix-client fetch` returns plain text — no further stripping needed.

## Response Style

Answer naturally using the article content. Cite the book title and article title. Keep it concise unless the user asks for depth.
