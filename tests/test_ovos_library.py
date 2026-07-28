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
    return f'<html><body><div id="mw-content-text"><p>{text}</p></div></body></html>'


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
