"""Tests for the multi-book layer.

Books are explicit by design: which corpora may feed spoken answers is a deployment
decision, so nothing here auto-discovers from the catalog.
"""
from __future__ import annotations

import json
import time

import httpx
import pytest
import respx

from kiwix_ovos.engine import AnswerTuning
from kiwix_ovos.library import (
    _ARTICLE_CACHE_SIZE,
    _ARTICLE_TTL_SECONDS,
    BookConfig,
    KiwixLibrary,
    books_from_config,
    preset,
)

BASE = "http://localhost:9090"
ALT = "http://other:9090"

#: How long the stubbed "slow" book blocks. Long enough that waiting for it is
#: unambiguous, short enough not to drag the suite.
_SLOW_BOOK_DELAY = 1.0


def _suggest(title: str, path: str) -> str:
    return json.dumps([
        {"value": title, "kind": "path", "path": path},
        {"value": "x", "label": "containing...", "kind": "pattern"},
    ])


def _article(text: str) -> str:
    """Wrap prose as a Kiwix article page.

    Padded to clear extract_article_text()'s 60-character paragraph floor — short
    fixtures extract to "" and make cache assertions pass vacuously.
    """
    padded = f"{text} This sentence pads the paragraph past the prose floor."
    return f'<html><body><div id="mw-content-text"><p>{padded}</p></div></body></html>'


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_at_least_one_book_is_required():
    """A library with no books would silently never answer."""
    with pytest.raises(ValueError, match="at least one book"):
        KiwixLibrary([], base_url=BASE)


def test_books_from_config_requires_a_book():
    with pytest.raises(ValueError, match="at least one book"):
        books_from_config({})


def test_books_from_config_accepts_a_single_book_key():
    books = books_from_config({"book": "wikipedia_en_all_maxi_2024-01"})
    assert [b.book for b in books] == ["wikipedia_en_all_maxi_2024-01"]


def test_books_from_config_accepts_a_list():
    books = books_from_config({"books": [
        {"book": "wikipedia_en_all_maxi_2024-01"},
        {"book": "gutenberg_en_all_2023-08", "preset": "long_form"},
    ]})
    assert [b.book for b in books] == [
        "wikipedia_en_all_maxi_2024-01", "gutenberg_en_all_2023-08",
    ]
    assert books[1].tuning.max_words == 0


def test_books_from_config_accepts_bare_slugs():
    books = books_from_config({"books": ["a_2024-01", "b_2024-01"]})
    assert [b.book for b in books] == ["a_2024-01", "b_2024-01"]


def test_explicit_keys_override_a_preset():
    """The preset is a starting point, not a straitjacket."""
    books = books_from_config({"books": [
        {"book": "g", "preset": "long_form", "min_title_overlap": 0.4},
    ]})
    assert books[0].tuning.max_words == 0        # from the preset
    assert books[0].tuning.min_title_overlap == 0.4   # overridden


def test_unknown_preset_is_rejected():
    """A typo must not silently fall back to defaults."""
    with pytest.raises(ValueError, match="unknown tuning preset"):
        preset("encylopedia")


def test_book_config_requires_a_slug():
    with pytest.raises(ValueError, match="requires a book"):
        BookConfig(book="")


def test_book_without_any_base_url_is_rejected():
    with pytest.raises(ValueError, match="no base_url"):
        KiwixLibrary([BookConfig(book="b")])


def test_per_book_base_url_overrides_the_library_default():
    library = KiwixLibrary(
        [BookConfig(book="a"), BookConfig(book="b", base_url=ALT)],
        base_url=BASE,
    )
    assert library.books == ["a", "b"]


# ---------------------------------------------------------------------------
# Article cache
# ---------------------------------------------------------------------------

class _Clocked(KiwixLibrary):
    """Library with a controllable clock, for exercising cache expiry."""

    now = 0.0

    def _clock(self) -> float:
        return self.now


@respx.mock
def test_article_is_fetched_once_and_then_cached():
    """The continuation normally re-fetches the article the answer came from —
    measured at 0.188s on top of a 0.311s answer, pure duplicate work."""
    route = respx.get(f"{BASE}/b/A/Newton").mock(
        return_value=httpx.Response(200, text=_article("Newton was a physicist."))
    )
    library = KiwixLibrary([BookConfig(book="b")], base_url=BASE)

    first = library.article_text("/b/A/Newton", "b")
    second = library.article_text("/b/A/Newton", "b")

    assert first == second
    assert route.call_count == 1, "article was fetched twice despite the cache"


@respx.mock
def test_reading_a_cached_article_refreshes_it():
    """Refresh-on-read is what stops the assistant developing amnesia mid-topic: a
    listener working through "tell me more" must not lose their article because
    other questions were asked in between."""
    respx.get(f"{BASE}/b/A/Newton").mock(
        return_value=httpx.Response(200, text=_article("Newton was a physicist."))
    )
    respx.get(f"{BASE}/b/A/Other").mock(
        return_value=httpx.Response(200, text=_article("Something else entirely."))
    )
    route = respx.get(f"{BASE}/b/A/Newton").mock(
        return_value=httpx.Response(200, text=_article("Newton was a physicist."))
    )
    library = _Clocked([BookConfig(book="b")], base_url=BASE)

    library.article_text("/b/A/Newton", "b")
    # Read it repeatedly, each time most of a TTL later. Total elapsed time far
    # exceeds the TTL, so this only survives if *every* read pushes expiry back —
    # not merely the first one.
    for _ in range(5):
        library.now += _ARTICLE_TTL_SECONDS * 0.9
        assert library.article_text("/b/A/Newton", "b"), "article expired while in use"

    assert route.call_count == 1, (
        "article was re-fetched, so a read did not refresh its expiry"
    )


@respx.mock
def test_idle_article_expires():
    """Only genuinely abandoned entries are reclaimed."""
    route = respx.get(f"{BASE}/b/A/Newton").mock(
        return_value=httpx.Response(200, text=_article("Newton was a physicist."))
    )
    library = _Clocked([BookConfig(book="b")], base_url=BASE)

    library.article_text("/b/A/Newton", "b")
    library.now += _ARTICLE_TTL_SECONDS + 1
    library.article_text("/b/A/Newton", "b")

    assert route.call_count == 2, "expired article should have been re-fetched"


@respx.mock
def test_cache_is_bounded():
    """Wikipedia articles run to hundreds of KB; the cache must not grow forever."""
    for i in range(_ARTICLE_CACHE_SIZE + 5):
        respx.get(f"{BASE}/b/A/{i}").mock(
            return_value=httpx.Response(
                200, text=_article(f"Article number {i} is about something.")
            )
        )
    library = KiwixLibrary([BookConfig(book="b")], base_url=BASE)
    for i in range(_ARTICLE_CACHE_SIZE + 5):
        library.article_text(f"/b/A/{i}", "b")

    assert len(library._articles) <= _ARTICLE_CACHE_SIZE


# ---------------------------------------------------------------------------
# Language scoping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slug,expected", [
    ("wikipedia_en_all_maxi_2024-01", "en"),
    ("gutenberg_fr_all_2020-10", "fr"),
    ("edutechwiki_fr_all_maxi_2021-03", "fr"),
    ("nhs.uk_en_medicines_2025-09", "en"),
    ("3dprinting.stackexchange.com_en_all_2025-08", "en"),
    # "mul" marks multilingual ZIMs, not a language.
    ("ted_mul_farming_2024-10", None),
    # No language field at all; must not guess.
    ("pdzim_2025-04", None),
])
def test_language_is_inferred_from_the_slug(slug, expected):
    assert BookConfig(book=slug).lang == expected


@pytest.mark.parametrize("given", [
    "en-US", "en-us", "en_US", "en-UK", "en-uk", "EN", "en", "  en-GB  ",
])
def test_locale_variants_normalize_to_the_primary_subtag(given):
    """OVOS passes locale tags in several shapes; all must match an 'en' book."""
    assert BookConfig(book="pdzim_2025-04", lang=given).lang == "en"


def test_only_books_in_the_requested_language_are_queried():
    """ZIMs are single-language. Answering an Italian question from an English
    Wikipedia is worse than declining."""
    library = KiwixLibrary(
        [
            BookConfig(book="wikipedia_en_all_maxi_2024-01"),
            BookConfig(book="gutenberg_fr_all_2020-10"),
        ],
        base_url=BASE,
    )
    assert [e.book for e in library.engines_for_lang("en-US")] == [
        "wikipedia_en_all_maxi_2024-01"
    ]
    assert [e.book for e in library.engines_for_lang("fr-FR")] == [
        "gutenberg_fr_all_2020-10"
    ]


def test_books_of_unknown_language_stay_eligible():
    """Excluding a book on a failed inference would silently drop a working corpus."""
    library = KiwixLibrary(
        [
            BookConfig(book="wikipedia_en_all_maxi_2024-01"),
            BookConfig(book="ted_mul_farming_2024-10"),
        ],
        base_url=BASE,
    )
    assert "ted_mul_farming_2024-10" in [
        e.book for e in library.engines_for_lang("fr")
    ]


def test_no_matching_language_falls_back_to_every_book():
    """A deployment whose books are all tagged differently from the assistant's
    locale must not go permanently silent."""
    library = KiwixLibrary(
        [BookConfig(book="gutenberg_fr_all_2020-10")], base_url=BASE
    )
    assert len(library.engines_for_lang("de")) == 1


def test_no_language_given_queries_everything():
    library = KiwixLibrary(
        [
            BookConfig(book="wikipedia_en_all_maxi_2024-01"),
            BookConfig(book="gutenberg_fr_all_2020-10"),
        ],
        base_url=BASE,
    )
    assert len(library.engines_for_lang(None)) == 2


# ---------------------------------------------------------------------------
# Answer selection
# ---------------------------------------------------------------------------

@respx.mock
def test_highest_confidence_answer_wins():
    """Confidence is normalised 0.0-1.0, so it compares across books. The exact
    title match must beat the padded namesake in another book."""
    respx.get(f"{BASE}/suggest", params={"content": "weak"}).mock(
        return_value=httpx.Response(
            200, text=_suggest("Isaac Newton Stevens", "A/Stevens")
        )
    )
    respx.get(f"{BASE}/suggest", params={"content": "strong"}).mock(
        return_value=httpx.Response(200, text=_suggest("Isaac Newton", "A/Newton"))
    )
    respx.get(f"{BASE}/weak/A/Stevens").mock(
        return_value=httpx.Response(200, text=_article(
            "Isaac Newton Stevens was an American politician and soldier who "
            "served as governor of Washington Territory."
        ))
    )
    respx.get(f"{BASE}/strong/A/Newton").mock(
        return_value=httpx.Response(200, text=_article(
            "Sir Isaac Newton was an English mathematician and physicist who "
            "formulated the laws of motion and universal gravitation."
        ))
    )

    library = KiwixLibrary(
        [BookConfig(book="weak"), BookConfig(book="strong")], base_url=BASE
    )
    answer = library.search("Isaac Newton")

    assert answer is not None
    assert answer.book == "strong"
    assert "mathematician" in answer.summary


def test_search_picks_the_best_not_the_first_to_finish():
    """Completion order is not answer quality: a fast book with a weak match must
    not beat a slower book with a strong one."""
    from kiwix_ovos.engine import KiwixAnswer

    scores = {"fast": 0.3, "slow": 0.8}

    class Stubbed(KiwixLibrary):
        def _search_one(self, engine, query):
            conf = scores[engine.book]
            return KiwixAnswer(
                title=engine.book, book=engine.book, summary="s", confidence=conf
            )

    library = Stubbed(
        [BookConfig(book="fast"), BookConfig(book="slow")],
        base_url=BASE,
        # Above both scores, so neither triggers early exit and both are compared.
        confident_enough=0.95,
    )
    assert library.search("q").book == "slow"


def test_search_stops_early_on_a_confident_answer():
    """One slow book must not set the latency for every query: measured live, an
    encyclopedia answered in 0.21s while a medical corpus burned its full 5s timeout
    answering nothing."""
    from kiwix_ovos.engine import KiwixAnswer

    class Stubbed(KiwixLibrary):
        def _search_one(self, engine, query):
            if engine.book == "quick":
                return KiwixAnswer(
                    title="hit", book="quick", summary="s", confidence=1.0
                )
            time.sleep(_SLOW_BOOK_DELAY)
            return None

    library = Stubbed(
        [BookConfig(book="quick"), BookConfig(book="slow")],
        base_url=BASE,
        confident_enough=0.9,
    )
    started = time.monotonic()
    answer = library.search("q")
    elapsed = time.monotonic() - started

    assert answer is not None and answer.book == "quick"
    # "slow" sleeps for _SLOW_BOOK_DELAY; returning before then is the whole point.
    assert elapsed < _SLOW_BOOK_DELAY, (
        f"waited {elapsed:.2f}s for a book whose answer was not needed"
    )


@respx.mock
def test_search_all_returns_every_answer_best_first():
    respx.get(f"{BASE}/suggest", params={"content": "weak"}).mock(
        return_value=httpx.Response(
            200, text=_suggest("Isaac Newton Stevens", "A/Stevens")
        )
    )
    respx.get(f"{BASE}/suggest", params={"content": "strong"}).mock(
        return_value=httpx.Response(200, text=_suggest("Isaac Newton", "A/Newton"))
    )
    respx.get(f"{BASE}/weak/A/Stevens").mock(
        return_value=httpx.Response(200, text=_article(
            "Isaac Newton Stevens was an American politician who served as "
            "governor of Washington Territory for several years."
        ))
    )
    respx.get(f"{BASE}/strong/A/Newton").mock(
        return_value=httpx.Response(200, text=_article(
            "Sir Isaac Newton was an English mathematician and physicist who "
            "formulated the laws of motion and universal gravitation."
        ))
    )

    library = KiwixLibrary(
        [BookConfig(book="weak"), BookConfig(book="strong")], base_url=BASE
    )
    answers = library.search_all("Isaac Newton")

    assert [a.book for a in answers] == ["strong", "weak"]
    assert answers[0].confidence >= answers[1].confidence


@respx.mock
def test_one_failing_book_does_not_suppress_the_others():
    """A stale slug on one book is the expected failure — dated slugs break on every
    ZIM update — and must not silence a healthy book."""
    respx.get(f"{BASE}/suggest", params={"content": "broken"}).mock(
        return_value=httpx.Response(200, text="[]")
    )
    respx.get(f"{BASE}/search", params={"books.name": "broken"}).mock(
        return_value=httpx.Response(400, text="No such book: broken")
    )
    respx.get(f"{BASE}/suggest", params={"content": "healthy"}).mock(
        return_value=httpx.Response(200, text=_suggest("Isaac Newton", "A/Newton"))
    )
    respx.get(f"{BASE}/healthy/A/Newton").mock(
        return_value=httpx.Response(200, text=_article(
            "Sir Isaac Newton was an English mathematician and physicist who "
            "formulated the laws of motion and universal gravitation."
        ))
    )

    library = KiwixLibrary(
        [BookConfig(book="broken"), BookConfig(book="healthy")], base_url=BASE
    )
    answer = library.search("Isaac Newton")

    assert answer is not None
    assert answer.book == "healthy"
    assert "broken" in library.failures


@respx.mock
def test_returns_none_when_no_book_answers():
    respx.get(f"{BASE}/suggest").mock(return_value=httpx.Response(200, text="[]"))
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200, text='<html><body><div class="results"><ul></ul></div></body></html>'
        )
    )
    library = KiwixLibrary(
        [BookConfig(book="a"), BookConfig(book="b")], base_url=BASE
    )
    assert library.search("nothing here") is None


def test_empty_query_returns_no_answers():
    library = KiwixLibrary([BookConfig(book="a")], base_url=BASE)
    assert library.search_all("") == []


@respx.mock
def test_failures_are_cleared_between_queries():
    """Stale failures would misreport a healthy book as broken."""
    respx.get(f"{BASE}/suggest").mock(return_value=httpx.Response(200, text="[]"))
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(400, text="No such book: a")
    )
    library = KiwixLibrary(
        [BookConfig(book="a"), BookConfig(book="b")], base_url=BASE
    )
    library.search("first")
    assert library.failures

    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200, text='<html><body><div class="results"><ul></ul></div></body></html>'
        )
    )
    library.search("second")
    assert library.failures == {}
