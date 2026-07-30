"""Answer questions across several explicitly configured Kiwix books.

A :class:`KiwixRetrievalEngine` covers exactly one book on one server. This module
adds the layer above it: a fixed set of books, each with its own client and tuning,
queried concurrently, with the highest-confidence answer winning.

Books are always explicit — never auto-discovered from the catalog. Which corpora a
voice assistant is allowed to answer from is a deployment decision, and an offline
library may hold books that should not feed spoken answers.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from typing import Dict, List, Optional

from kiwix_client import KiwixClient, extract_article_text

from kiwix_ovos.engine import (
    AnswerTuning,
    KiwixAnswer,
    KiwixBookNotFound,
    KiwixRetrievalEngine,
)

__all__ = ["BookConfig", "KiwixLibrary", "books_from_config", "preset"]

#: Ceiling on concurrent book queries. Each is one or two blocking HTTP calls, so a
#: small pool is plenty; this exists to avoid stampeding a Raspberry-Pi-class server.
_MAX_WORKERS = 8

#: Confidence at which the remaining books are not worth waiting for. An exact title
#: match scores 1.0, so this admits near-exact hits while still preferring a better
#: answer if one arrives first. Slow books otherwise set the latency for every query.
_CONFIDENT_ENOUGH = 0.9

#: Articles held for "tell me more" continuations. Wikipedia articles run to hundreds
#: of KB, so this is deliberately small — but see the TTL below: eviction is by age
#: with a refresh on every access, so an in-progress conversation is never evicted
#: just because other questions were asked in between.
_ARTICLE_CACHE_SIZE = 32

#: How long an unused article stays cached. Refreshed on every read, so a continuing
#: "tell me more" conversation keeps its article alive indefinitely; only genuinely
#: idle entries expire.
_ARTICLE_TTL_SECONDS = 30 * 60


@dataclass
class BookConfig:
    """One book the assistant may answer from.

    Parameters
    ----------
    book:
        Slug or stable OPDS name. Required.
    base_url:
        kiwix-serve URL. Defaults to the library's shared URL, so a single-server
        deployment need not repeat it.
    tuning:
        Per-book answer thresholds. Necessary in practice: an encyclopedia and a book
        library cannot share one length ceiling.
    lang:
        Two-letter language code this book answers in. Inferred from the slug when
        omitted (``wikipedia_en_all_maxi_2024-01`` -> ``en``), since ZIMs are
        single-language. Set explicitly when the slug does not follow that pattern.
    """

    book: str
    base_url: Optional[str] = None
    tuning: Optional[AnswerTuning] = None
    lang: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.book:
            raise ValueError("BookConfig requires a book slug or OPDS name")
        if self.lang:
            self.lang = _normalize_lang(self.lang)
        else:
            self.lang = _lang_from_slug(self.book)


#: ZIM slugs conventionally carry the language as the second underscore-separated
#: field: wikipedia_en_all_maxi_2024-01, gutenberg_fr_all_2020-10.
_RE_SLUG_LANG = re.compile(r"^[^_]+_([a-z]{2,3})(?:_|$)")


def _normalize_lang(lang: str) -> str:
    """Reduce a BCP-47 tag to its primary subtag: ``en-US`` -> ``en``."""
    return lang.strip().lower().replace("_", "-").split("-")[0]


def _lang_from_slug(book: str) -> Optional[str]:
    """Infer a book's language from its slug, or None when the slug does not say.

    Returning None means "answers in any language" rather than guessing wrong: a
    mis-inferred language would silently exclude a book from every query.
    """
    match = _RE_SLUG_LANG.match(book)
    if not match:
        return None
    # "mul" marks multilingual ZIMs (ted_mul_farming_2024-10); not a real language.
    return None if match.group(1) == "mul" else match.group(1)


#: Tuning presets by corpus shape, chosen from measured behaviour rather than taste.
#: Referenced from config as ``preset: long_form``.
_PRESETS: Dict[str, AnswerTuning] = {
    # Encyclopedias: short, well-titled articles. The defaults were tuned here.
    "encyclopedia": AnswerTuning(),
    # Book libraries (Project Gutenberg): every article is book-length, so the
    # ceiling must go or the corpus is rejected wholesale.
    "long_form": AnswerTuning.for_long_form(),
    # How-to corpora (wikiHow): titles are descriptive sentences rather than the
    # subject alone ("4 Ways to Purify Water"), so title recall runs lower.
    "how_to": AnswerTuning(min_title_overlap=0.34, concise_words=10_000),
}


def preset(name: str) -> AnswerTuning:
    """Look up a named tuning preset, raising on an unknown name."""
    try:
        return _PRESETS[name]
    except KeyError:
        raise ValueError(
            f"unknown tuning preset {name!r}; choose from {sorted(_PRESETS)}"
        ) from None


def books_from_config(config: Dict) -> List[BookConfig]:
    """Build book configs from a plain settings dict.

    Accepts either a list of book entries::

        books:
          - book: wikipedia_en_all_maxi_2024-01
          - book: gutenberg_en_all_2023-08
            preset: long_form
          - book: wikihow_en_maxi_2023-03
            preset: how_to
            min_title_overlap: 0.4      # any AnswerTuning field overrides the preset

    or a single ``book`` key for the one-book case. Raises when neither is present:
    a solver with no books would silently never answer.
    """
    entries = config.get("books")
    if not entries:
        single = config.get("book")
        if not single:
            # Name the keys we actually received: the usual cause is a nested or
            # misspelled settings file, and "no books configured" alone sends the
            # reader looking in the wrong place.
            seen = sorted(k for k in config if not k.startswith("__"))
            raise ValueError(
                "configure at least one book: set 'books' (a list) or 'book' (a "
                "single slug) at the top level of the settings file. kiwix-serve "
                f"scopes search per-book. Keys found instead: {seen or 'none'}"
            )
        entries = [{"book": single, **_tuning_keys(config)}]
    if isinstance(entries, str):
        entries = [{"book": entries}]

    # Top-level tuning keys are defaults for every book; a book entry overrides them.
    # Without this, `{"timeout": 2.5, "books": [...]}` silently ignored the timeout.
    defaults = _tuning_keys(config)

    books: List[BookConfig] = []
    for entry in entries:
        if isinstance(entry, str):
            entry = {"book": entry}
        books.append(
            BookConfig(
                book=entry.get("book", ""),
                base_url=entry.get("base_url", config.get("base_url")),
                tuning=_tuning_from_entry({**defaults, **entry}),
                lang=entry.get("lang"),
            )
        )
    return books


def _tuning_keys(entry: Dict) -> Dict:
    """Subset of a config dict that maps onto AnswerTuning fields."""
    names = {f.name for f in dataclass_fields(AnswerTuning)}
    return {k: v for k, v in entry.items() if k in names}


def _tuning_from_entry(entry: Dict) -> AnswerTuning:
    """Resolve a book entry's tuning: preset first, then explicit overrides."""
    base = preset(entry["preset"]) if entry.get("preset") else AnswerTuning()
    overrides = _tuning_keys(entry)
    if not overrides:
        return base
    merged = {f.name: getattr(base, f.name) for f in dataclass_fields(AnswerTuning)}
    merged.update(overrides)
    return AnswerTuning(**merged)


class KiwixLibrary:
    """Query several configured books and return the best answer.

    Confidence is a normalised 0.0-1.0 score, so it is directly comparable across
    books; the highest wins. Books are queried concurrently, making the cost of an
    extra book roughly zero in wall-clock terms.
    """

    def __init__(
        self,
        books: List[BookConfig],
        base_url: str = "",
        max_workers: int = _MAX_WORKERS,
        confident_enough: float = _CONFIDENT_ENOUGH,
    ) -> None:
        if not books:
            raise ValueError(
                "at least one book must be configured; books are explicit so that a "
                "deployment controls which corpora may answer"
            )
        self._engines: List[KiwixRetrievalEngine] = []
        self._books: List[str] = []
        self._langs: Dict[str, Optional[str]] = {}
        for config in books:
            url = config.base_url or base_url
            if not url:
                raise ValueError(
                    f"book {config.book!r} has no base_url and no library default"
                )
            tuning = config.tuning or AnswerTuning()
            self._engines.append(
                KiwixRetrievalEngine(
                    KiwixClient(url, timeout=tuning.timeout),
                    book=config.book,
                    tuning=tuning,
                )
            )
            self._books.append(config.book)
            self._langs[config.book] = config.lang
        self._max_workers = max(1, min(max_workers, len(self._engines)))
        self._confident_enough = confident_enough
        self._failures: Dict[str, str] = {}
        # url -> (prose, last_used_monotonic)
        self._articles: Dict[str, tuple] = {}

    @property
    def books(self) -> List[str]:
        return list(self._books)

    @staticmethod
    def _clock() -> float:
        """Monotonic seconds. Overridable so tests can control cache expiry."""
        return time.monotonic()

    def article_text(self, url: str, book: str = "") -> str:
        """Fetch an article by relative URL and return its prose.

        Used for "tell me more": the spoken answer is a short summary, so the
        continuation needs the rest of the article. ``book`` selects the right client
        when books span several servers; without it the first client is used.

        Results are cached because this is normally a *re-fetch* of the article the
        answer already came from — measured at 0.188s on top of a 0.311s answer, pure
        duplicate work. The cache is small and FIFO-evicted: follow-ups arrive
        seconds after the answer, so recency is all that matters.
        """
        if not url or not self._engines:
            return ""
        cached = self._read_article(url)
        if cached is not None:
            return cached

        engine = next(
            (e for e in self._engines if e.book == book), self._engines[0]
        )
        text = extract_article_text(engine.fetch_article(url))
        self._cache_article(url, text)
        return text

    def _read_article(self, url: str) -> Optional[str]:
        """Return a cached article, refreshing its recency, or None if absent.

        Refresh-on-read is what keeps a conversation alive: a listener working through
        "tell me more" would otherwise lose the article to unrelated questions and the
        assistant would appear to develop amnesia mid-topic.
        """
        entry = self._articles.get(url)
        if entry is None:
            return None
        text, cached_at = entry
        if self._clock() - cached_at > _ARTICLE_TTL_SECONDS:
            self._articles.pop(url, None)
            return None
        # Re-insert so this becomes the most recently used entry.
        self._articles.pop(url)
        self._articles[url] = (text, self._clock())
        return text

    def _cache_article(self, url: str, text: str) -> None:
        """Store an article, dropping expired then least-recently-used entries."""
        now = self._clock()
        expired = [
            key for key, (_, at) in self._articles.items()
            if now - at > _ARTICLE_TTL_SECONDS
        ]
        for key in expired:
            self._articles.pop(key, None)

        self._articles.pop(url, None)
        while len(self._articles) >= _ARTICLE_CACHE_SIZE:
            # dicts preserve insertion order, so the first key is least-recently-used.
            self._articles.pop(next(iter(self._articles)))
        self._articles[url] = (text, now)

    def tuning_for(self, book: str) -> AnswerTuning:
        """Return the tuning in effect for ``book``."""
        for engine in self._engines:
            if engine.book == book:
                return engine.tuning
        raise KeyError(f"{book!r} is not configured; have {self._books}")

    def engines_for_lang(self, lang: Optional[str]) -> List[KiwixRetrievalEngine]:
        """Books that can answer in ``lang``.

        ZIMs are single-language, so answering an Italian question from an English
        Wikipedia is worse than declining. Books whose language could not be
        determined are always eligible — excluding them on a guess would silently
        drop working corpora.

        With no ``lang`` given, every book is eligible.
        """
        if not lang:
            return list(self._engines)
        wanted = _normalize_lang(lang)
        eligible = [
            engine for engine in self._engines
            if self._langs.get(engine.book) in (None, wanted)
        ]
        # Rather than answer nothing at all, fall back to the full set: a deployment
        # whose books are all tagged differently from the assistant's locale would
        # otherwise go permanently silent.
        return eligible or list(self._engines)

    def search(
        self, query: str, lang: Optional[str] = None
    ) -> Optional[KiwixAnswer]:
        """Return the highest-confidence answer across all books, or None.

        Stops waiting once a book returns an answer at or above
        :attr:`confident_enough`. Without that, one slow book sets the latency for
        every query: measured on a live server, an encyclopedia answered in 0.21s
        while a medical corpus spent its full 5s timeout failing to answer at all,
        and fan-out waited for the loser.

        ``lang`` restricts the search to books that answer in that language.
        """
        if not query:
            return None
        self._failures = {}
        engines = self.engines_for_lang(lang)

        if len(engines) == 1:
            return self._search_one(engines[0], query)

        best: Optional[KiwixAnswer] = None
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = [
                pool.submit(self._search_one, engine, query)
                for engine in engines
            ]
            try:
                for future in as_completed(futures):
                    answer = future.result()
                    if answer is None:
                        continue
                    if best is None or answer.confidence > best.confidence:
                        best = answer
                    if best.confidence >= self._confident_enough:
                        break
            finally:
                # Losing books are still in flight; abandon rather than block. Their
                # results are no longer needed and a voice reply is time-critical.
                for future in futures:
                    future.cancel()
        return best

    def search_all(
        self, query: str, lang: Optional[str] = None
    ) -> List[KiwixAnswer]:
        """Return every eligible book's answer, best first.

        Useful for "tell me more" style follow-ups and for diagnosing why a
        particular book won.
        """
        if not query:
            return []
        self._failures = {}
        engines = self.engines_for_lang(lang)

        # A single book does not justify a thread; keep the common case simple and
        # keep exceptions on the calling stack.
        if len(engines) == 1:
            answer = self._search_one(engines[0], query)
            return [answer] if answer else []

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            results = list(
                pool.map(lambda e: self._search_one(e, query), engines)
            )
        answers = [answer for answer in results if answer is not None]
        answers.sort(key=lambda answer: answer.confidence, reverse=True)
        return answers

    def _search_one(
        self, engine: KiwixRetrievalEngine, query: str
    ) -> Optional[KiwixAnswer]:
        """Query one book, converting a per-book failure into a decline.

        One book with a stale slug must not suppress answers from the others, so the
        failure is contained here. It is recorded rather than discarded: a book that
        never answers is invisible otherwise, and a dated slug breaks on every ZIM
        update.
        """
        try:
            answer = engine.search(query)
            if answer is not None and answer.article_text:
                # Populate the cache from the fetch the answer already did, so a
                # follow-up is a hit instead of a duplicate round-trip.
                self._cache_article(answer.url, answer.article_text)
            return answer
        except KiwixBookNotFound as exc:
            self.record_failure(engine.book, str(exc))
            return None
        except Exception as exc:  # defensive: transport/parse failure in one book
            self.record_failure(engine.book, f"{type(exc).__name__}: {exc}")
            return None

    def record_failure(self, book: str, reason: str) -> None:
        """Record a per-book failure. Overridden by the solver to log via OVOS."""
        self._failures[book] = reason

    @property
    def failures(self) -> Dict[str, str]:
        """Books that failed on the most recent query, by reason."""
        return dict(self._failures)
