"""Voice-oriented retrieval over a Kiwix server.

Framework-free by design: this module imports nothing from OVOS/Neon so it can be
developed and tested without a hub, and so it survives framework churn. The OVOS
solver in :mod:`kiwix_ovos.solver` is a thin adapter over this.

The problem this solves is that Kiwix does *full-text search*, not title lookup.
Rank-1 is frequently a poor spoken answer — measured against a live server, the top
hit for "who is Isaac Newton" was a 101,851-word Gutenberg book whose opening
paragraph is 1820s publisher boilerplate. Two mechanisms defend against that:

  1. Keyword extraction, so "who is X" searches for "X" (the stopword "who" alone
     pulled in unrelated parish records).
  2. A confidence gate on title overlap *and* article length, so encyclopedia-shaped
     articles win over book-length texts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from typing import List, Optional, Tuple

import httpx

from kiwix_client import KiwixClient, extract_article_text
from kiwix_client.parse import SearchResult

__all__ = [
    "DEFAULT_TUNING",
    "AnswerTuning",
    "KiwixAnswer",
    "KiwixBookNotFound",
    "KiwixRetrievalEngine",
    "extract_keyword",
]


_RE_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_RE_WORD = re.compile(r"[\w']+")

# Overlap is a ratio of token counts, so guard the float comparison rather than
# testing == 1.0. Not a tuning knob: this is "all tokens matched".
_EXACT_MATCH_THRESHOLD = 0.99

# When the length ceiling is disabled (long-form corpora), concision still needs an
# upper bound to decay toward. Ten times the concise threshold keeps the signal
# meaningful across book-length texts without reintroducing a hard cutoff.
_OPEN_CEILING_FACTOR = 10

# Kiwix article bodies often open with navigation/boilerplate lines rather than prose.
_DEFAULT_BOILERPLATE_PREFIXES = (
    "jump to navigation",
    "jump to search",
    "from wikipedia",
    "this article",
)


# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AnswerTuning:
    """Thresholds governing which search hits are worth speaking.

    Every value here is a judgement call about *this* corpus, so all of them are
    overridable. The defaults suit encyclopedia-shaped books (Appropedia, wikiHow,
    MDWiki, Wikipedia); book-length corpora such as Project Gutenberg need a much
    higher ``max_words`` or they will be rejected wholesale — see
    :meth:`for_long_form`.
    """

    #: Reject hits shorter than this; too little prose to say anything useful.
    min_words: int = 40

    #: Reject hits longer than this. Measured: Gutenberg hits run 20k-160k words and
    #: open with publisher boilerplate rather than a definition. Set to 0 to disable.
    max_words: int = 20_000

    #: Fraction of extracted-keyword tokens that must appear in the result title
    #: (0.0-1.0). Below this the hit is likely a passing mention.
    min_title_overlap: float = 0.5

    #: Relative weights of the three ranking signals. They are normalised, so only
    #: their ratios matter and the resulting confidence always lands in 0.0-1.0 —
    #: which is what OVOS common_query documents as its expected range.
    #:
    #: - recall:    fraction of keyword tokens found in the title (does the article
    #:              cover what was asked?)
    #: - precision: fraction of title tokens drawn from the keyword (is the article
    #:              *about* it, or is the keyword incidental?)
    #: - concision: article-length preference, only when the server reports a word
    #:              count.
    recall_weight: float = 0.5
    precision_weight: float = 0.3
    concision_weight: float = 0.2

    #: Article length at or below which the concision signal is a full 1.0; it decays
    #: linearly to 0.0 at :attr:`max_words` (or at 10x this value when the ceiling is
    #: disabled). Nudges toward encyclopedia-length prose over sprawling texts.
    concise_words: int = 5_000

    #: Sentences shorter than this are treated as headers/fragments when hunting for
    #: the first real paragraph.
    min_sentence_chars: int = 25

    #: Approximate character budget for the spoken summary.
    summary_chars: int = 400

    #: Seconds before abandoning a search. Measured: 4.7s on a 119k-article Gutenberg
    #: book, and a 6.86M-article Wikipedia ZIM exceeded the client timeout entirely.
    #: OVOS common_query has a bounded budget, so a late answer is worse than none.
    timeout: float = 5.0

    #: Try the ``/suggest`` title index before full-text search. Measured on a
    #: 6.86M-article Wikipedia ZIM: ~35ms vs 6-26s cold for ``/search``. Disable only
    #: for corpora whose titles are unhelpfully descriptive, where full-text ranking
    #: finds the right article and title lookup does not.
    use_suggest: bool = True

    #: How many title suggestions to consider.
    suggest_count: int = 10

    #: Lowercase line prefixes to skip when looking for the lead paragraph.
    boilerplate_prefixes: tuple = _DEFAULT_BOILERPLATE_PREFIXES

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_title_overlap <= 1.0:
            raise ValueError("min_title_overlap must be between 0.0 and 1.0")
        if self.max_words and self.max_words < self.min_words:
            raise ValueError("max_words must be >= min_words (or 0 to disable)")
        if self.summary_chars <= 0:
            raise ValueError("summary_chars must be positive")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        weights = (self.recall_weight, self.precision_weight, self.concision_weight)
        if any(w < 0 for w in weights):
            raise ValueError("ranking weights must be non-negative")
        if sum(weights) <= 0:
            raise ValueError("at least one ranking weight must be positive")

    @classmethod
    def for_long_form(cls, **overrides) -> "AnswerTuning":
        """Tuning for book-length corpora such as Project Gutenberg.

        Disables the length ceiling — every article in such a corpus would otherwise be
        rejected — and demands a stronger title match to compensate for the weaker
        length signal.
        """
        settings = {"max_words": 0, "min_title_overlap": 0.75}
        settings.update(overrides)
        return cls(**settings)

    @classmethod
    def from_config(cls, config: Optional[dict]) -> "AnswerTuning":
        """Build from a plain dict, ignoring unrelated keys.

        Lets a solver pass its whole config block through without having to know which
        keys are tuning knobs.
        """
        if not config:
            return cls()
        fields = {f.name for f in dataclass_fields(cls)}
        return cls(**{k: v for k, v in config.items() if k in fields})


#: Default tuning, suitable for encyclopedia-shaped books.
DEFAULT_TUNING = AnswerTuning()


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------

# Conversational framings, ordered narrow to broad. Ported from
# neon-solver-plugin-wikipedia, which used simplematch; plain regex avoids the extra
# dependency for what is a handful of patterns.
_QUESTION_PATTERNS = (
    r"^who\s+(?:is|are|was|were)\s+(?P<q>.+)$",
    r"^what\s+(?:is|are|was|were)\s+(?:the\s+)?(?P<q>.+)$",
    r"^where\s+(?:is|are|was|were)\s+(?P<q>.+)$",
    r"^when\s+(?:is|are|was|were|did)\s+(?P<q>.+)$",
    r"^how\s+(?:do|does|did|to)\s+(?:i\s+|you\s+)?(?P<q>.+)$",
    r"^tell\s+me\s+about\s+(?P<q>.+)$",
    r"^(?:search|look\s+up)\s+(?:for\s+)?(?P<q>.+)$",
)

_COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _QUESTION_PATTERNS]

_TRAILING_NOISE = re.compile(r"\s*\?+\s*$")


def extract_keyword(query: str) -> str:
    """Strip conversational framing from ``query``.

    "who is Isaac Newton" -> "Isaac Newton". Returns the cleaned query unchanged when
    no pattern matches, so a bare keyword search still works.

    This is load-bearing rather than cosmetic: searching the raw utterance matches on
    stopwords and buries the relevant article.
    """
    cleaned = _TRAILING_NOISE.sub("", (query or "").strip())
    for pattern in _COMPILED_PATTERNS:
        match = pattern.match(cleaned)
        if match:
            return match.group("q").strip()
    return cleaned


def _tokens(text: str) -> List[str]:
    return [t.lower() for t in _RE_WORD.findall(text or "")]


def _title_overlap(keyword: str, title: str) -> float:
    """Fraction of keyword tokens present in ``title`` (0.0-1.0).

    Deliberately one-directional: this is *recall*, used as the admission gate. It
    cannot distinguish an exact title from one that merely contains the keyword —
    "Isaac Newton", "Isaac Newton Stevens" and "The Life of Sir Isaac Newton" all
    score 1.0. Use :func:`_title_precision` to rank among admitted candidates.
    """
    keyword_tokens = set(_tokens(keyword))
    if not keyword_tokens:
        return 0.0
    title_tokens = set(_tokens(title))
    return len(keyword_tokens & title_tokens) / len(keyword_tokens)


def _title_precision(keyword: str, title: str) -> float:
    """Fraction of *title* tokens that come from the keyword (0.0-1.0).

    The complement of :func:`_title_overlap`. Penalises titles padded with extra
    words, so an exact match outranks a longer namesake: measured on a live Gutenberg
    ZIM, "Isaac Newton" scores 1.0 while "The Life of Sir Isaac Newton" scores 0.33.
    """
    title_tokens = set(_tokens(title))
    if not title_tokens:
        return 0.0
    keyword_tokens = set(_tokens(keyword))
    return len(keyword_tokens & title_tokens) / len(title_tokens)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

class KiwixBookNotFound(RuntimeError):
    """Raised when the configured book slug is not present on the server.

    Separate from a no-answer so callers can fail loudly at configuration time; the
    common cause is a ZIM update changing the dated slug.
    """


@dataclass
class KiwixAnswer:
    """A spoken-answer candidate drawn from a Kiwix article."""

    title: str = ""
    summary: str = ""
    url: str = ""
    book: str = ""
    confidence: float = 0.0

    #: Full extracted prose the summary was cut from. Carried so callers can offer a
    #: "tell me more" continuation without re-fetching the article.
    article_text: str = ""

    def sentences(self) -> List[str]:
        """Split the summary into sentences for incremental "tell me more" delivery."""
        return [s.strip() for s in _RE_SENTENCE_END.split(self.summary) if s.strip()]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class KiwixRetrievalEngine:
    """Turns a spoken question into a speakable answer from a Kiwix server.

    Parameters
    ----------
    client:
        A configured :class:`~kiwix_client.client.KiwixClient`.
    book:
        Book slug or stable OPDS name to scope searches to. Required — kiwix-serve
        rejects an unknown slug with a clear ``No such book:`` 400, and scoping also
        keeps latency predictable.
    tuning:
        Answer-quality thresholds. Defaults suit encyclopedia-shaped books; use
        :meth:`AnswerTuning.for_long_form` for corpora like Project Gutenberg.
    """

    def __init__(
        self,
        client: KiwixClient,
        book: str,
        tuning: Optional[AnswerTuning] = None,
    ) -> None:
        if not book:
            raise ValueError(
                "a book slug is required; kiwix-serve search is scoped per-book. "
                "Use KiwixClient.list_books() to discover valid slugs."
            )
        self._client = client
        self._book = book
        self._tuning = tuning or DEFAULT_TUNING

    @property
    def tuning(self) -> AnswerTuning:
        return self._tuning

    @property
    def book(self) -> str:
        return self._book

    def fetch_article(self, url: str) -> str:
        """Fetch raw article HTML through this book's client."""
        return self._client.fetch_article(url)

    # -- public API ----------------------------------------------------

    def search(self, query: str) -> Optional[KiwixAnswer]:
        """Return the best speakable answer for ``query``, or None.

        Tries the title index (``/suggest``) first and falls back to full-text search.
        Measured on a 6.86M-article Wikipedia ZIM, that ordering is the difference
        between a ~140ms answer and a 6-26s one; it also ranks better, because
        full-text search put a 101k-word book above the same-named article.

        Returning None is a valid and preferred outcome: in a common_query contest,
        staying silent beats speaking a wrong or unintelligible answer.
        """
        keyword = extract_keyword(query)
        if not keyword:
            return None

        answer = self._answer_from_suggestions(keyword)
        if answer is not None:
            return answer
        return self._answer_from_full_text(keyword)

    # -- internals -----------------------------------------------------

    def _answer_from_suggestions(self, keyword: str) -> Optional[KiwixAnswer]:
        """Resolve via the title index, or None to fall through to full-text.

        Suggestions carry no word count, so the length gate does not apply — a title
        hit is already the right shape by construction. The title gate still applies:
        ``/suggest`` does prefix matching, so a stray hit is still possible.
        """
        if not self._tuning.use_suggest:
            return None

        try:
            suggestions = self._client.suggest(
                keyword, self._book, count=self._tuning.suggest_count
            )
        except (httpx.TimeoutException, httpx.HTTPError):
            return None

        best: Optional[Tuple[float, object]] = None
        for suggestion in suggestions:
            overlap = _title_overlap(keyword, suggestion.title)
            if overlap < self._tuning.min_title_overlap:
                continue
            # Suggestions carry no word count, so concision is simply absent.
            score = self._confidence(keyword, suggestion.title, overlap)
            if best is None or score > best[0]:
                best = (score, suggestion)

        if best is None:
            return None

        score, suggestion = best
        url = self._client.article_url(self._book, suggestion.path)
        summary, full_text = self._summarize_url(url)
        if not summary:
            # Old ZIMs carry stale title-index entries pointing at articles that are
            # not in the archive (seen on a 2020 Gutenberg ZIM). Fall through to
            # full-text rather than declining outright.
            return None

        return KiwixAnswer(
            title=suggestion.title,
            summary=summary,
            url=url,
            book=self._book,
            confidence=score,
            article_text=full_text,
        )

    def _answer_from_full_text(self, keyword: str) -> Optional[KiwixAnswer]:
        """Fall back to the Xapian full-text index."""
        results = self._search_scoped(keyword)
        if not results:
            return None

        best = self._best_candidate(keyword, results)
        if best is None:
            return None

        result, confidence = best
        summary, full_text = self._summarize(result)
        if not summary:
            return None

        return KiwixAnswer(
            title=result.title,
            summary=summary,
            url=result.url,
            book=result.book or self._book,
            confidence=confidence,
            article_text=full_text,
        )

    def _search_scoped(self, keyword: str) -> List[SearchResult]:
        """Run the scoped search, converting transport failures into no-answer.

        A stale book slug is the expected failure here — slugs embed dates
        (``wikipedia_en_all_maxi_2024-01``), so they break on ZIM update. That case is
        re-raised so callers can surface it loudly at configuration time rather than
        degrading into a silent no-answer.
        """
        try:
            response = self._client.search(pattern=keyword, books=self._book)
        except ValueError as exc:
            # KiwixClient turns any 400 into ValueError; an unknown slug is a config
            # error worth surfacing, not a missing answer.
            raise KiwixBookNotFound(
                f"Kiwix rejected the configured book {self._book!r}. If the ZIM was "
                f"updated its slug likely changed; run list_books() to get the "
                f"current slug. Server said: {exc}"
            ) from exc
        except (httpx.TimeoutException, httpx.HTTPError):
            # Slow or unreachable server: no answer beats a late answer.
            return []
        return response.results

    def _best_candidate(
        self, keyword: str, results: List[SearchResult]
    ) -> Optional[Tuple[SearchResult, float]]:
        """Pick the highest-scoring result that clears the confidence gate.

        Rank-1 is deliberately not trusted: measured against a live server it was a
        100k-word book for one query and a niche gadget article for another, while the
        genuinely useful article sat further down the list.
        """
        scored = []
        for result in results:
            overlap = _title_overlap(keyword, result.title)
            if overlap < self._tuning.min_title_overlap:
                continue
            if not self._is_speakable_length(result):
                continue
            scored.append((
                self._confidence(keyword, result.title, overlap, result.word_count),
                result,
            ))

        if not scored:
            return None

        scored.sort(key=lambda pair: pair[0], reverse=True)
        best_score, best_result = scored[0]
        return best_result, best_score

    def _is_speakable_length(self, result: SearchResult) -> bool:
        """Reject book-length texts and stubs.

        word_count of 0 means the server did not report a length; allow it rather than
        discarding a possibly-good article on missing metadata.
        """
        if not result.word_count:
            return True
        if result.word_count < self._tuning.min_words:
            return False
        # max_words of 0 disables the ceiling, for book-length corpora.
        if self._tuning.max_words and result.word_count > self._tuning.max_words:
            return False
        return True

    def _confidence(
        self, keyword: str, title: str, overlap: float, word_count: int = 0
    ) -> float:
        """Blend the ranking signals into a 0.0-1.0 confidence.

        A weighted mean rather than a stack of additive bonuses: the result is bounded
        by construction, which is what OVOS common_query documents as its expected
        range, and the weights express relative importance instead of arbitrary
        magnitudes that must be mentally summed.

        Precision matters as much as recall here: "Isaac Newton" and "Isaac Newton
        Stevens" both contain every keyword token, so recall alone ties them and the
        winner falls out of iteration order.
        """
        tuning = self._tuning
        signals = [
            (overlap, tuning.recall_weight),
            (_title_precision(keyword, title), tuning.precision_weight),
        ]
        # Only score concision when the server reported a length; otherwise the signal
        # is absent rather than zero, and including it would penalise unfairly.
        if word_count:
            signals.append((self._concision(word_count), tuning.concision_weight))

        total_weight = sum(weight for _, weight in signals)
        if total_weight <= 0:
            return 0.0
        return sum(value * weight for value, weight in signals) / total_weight

    def _concision(self, word_count: int) -> float:
        """Score article length: 1.0 up to ``concise_words``, decaying to 0.0."""
        tuning = self._tuning
        if word_count <= tuning.concise_words:
            return 1.0
        ceiling = tuning.max_words or tuning.concise_words * _OPEN_CEILING_FACTOR
        if word_count >= ceiling:
            return 0.0
        span = ceiling - tuning.concise_words
        return 1.0 - (word_count - tuning.concise_words) / span

    def _summarize(self, result: SearchResult) -> Tuple[str, str]:
        """Fetch the article and reduce it to a short spoken lead.

        Falls back to the search snippet when the article cannot be fetched — snippets
        read poorly (they are keyword-in-context fragments) but beat silence when we
        already know the title is a strong match.
        """
        summary, full_text = self._summarize_url(result.url)
        return (summary or self._trim(result.snippet)), full_text

    def _summarize_url(self, url: str) -> Tuple[str, str]:
        """Fetch an article URL and return ``(spoken_summary, full_prose)``.

        Both are returned so callers can offer a "tell me more" continuation without
        re-fetching; measured, that duplicate cost 0.188s on a 0.311s answer.

        ``("", "")`` means the page holds no prose. Besides genuine errors that covers
        a real failure mode: older kiwix-tools builds answer a missing article with
        HTTP 200 and the library landing page, so a stale title-index entry looks
        like success until you notice there are no paragraphs.
        """
        try:
            html = self._client.fetch_article(url)
        except (httpx.TimeoutException, httpx.HTTPError):
            return "", ""
        text = extract_article_text(html)
        if not text:
            return "", ""
        return self._trim(self._lead_paragraph(text)), text

    def _lead_paragraph(self, text: str) -> str:
        """Drop leading navigation/boilerplate before the first real sentence."""
        sentences = [s.strip() for s in _RE_SENTENCE_END.split(text) if s.strip()]
        for index, sentence in enumerate(sentences):
            lowered = sentence.lower()
            if any(lowered.startswith(p) for p in self._tuning.boilerplate_prefixes):
                continue
            if len(sentence) < self._tuning.min_sentence_chars:
                continue  # section headers, stray fragments
            return " ".join(sentences[index:])
        return text

    def _trim(self, text: str) -> str:
        """Truncate to the summary budget on a sentence boundary."""
        budget = self._tuning.summary_chars
        text = (text or "").strip()
        if len(text) <= budget:
            return text

        kept: List[str] = []
        length = 0
        for sentence in _RE_SENTENCE_END.split(text):
            sentence = sentence.strip()
            if not sentence:
                continue
            if kept and length + len(sentence) > budget:
                break
            kept.append(sentence)
            length += len(sentence) + 1

        if kept:
            return " ".join(kept)
        return text[:budget].rstrip() + "…"
