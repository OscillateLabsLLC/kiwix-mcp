"""Tests for the OVOS retrieval engine.

These encode behaviour measured against a live Kiwix server, where naive
"take rank-1 and speak it" produced unusable spoken answers.
"""
from __future__ import annotations

import json
import time

import httpx
import pytest
import respx

from kiwix_client.client import KiwixClient
from kiwix_ovos.engine import (
    AnswerTuning,
    KiwixBookNotFound,
    KiwixRetrievalEngine,
    _title_overlap,
    extract_keyword,
)

BASE = "http://localhost:9090"
BOOK = "appropedia_en_all_maxi_2025-03"
# article_url() builds bare /{book}/{path} — the one scheme both server generations
# serve (older kiwix-tools directly, newer libkiwix via a 302 to /content/).
BOOK_PREFIX = f"/{BOOK}"


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "utterance,expected",
    [
        ("who is Isaac Newton", "Isaac Newton"),
        ("Who was Ada Lovelace?", "Ada Lovelace"),
        ("what is the water cycle", "water cycle"),
        ("tell me about rainwater harvesting", "rainwater harvesting"),
        ("how do I purify water", "purify water"),
        ("how to purify water", "purify water"),
        ("look up solar stills", "solar stills"),
        # No conversational framing: passed through untouched.
        ("Isaac Newton", "Isaac Newton"),
        ("", ""),
    ],
)
def test_extract_keyword_strips_framing(utterance, expected):
    """Searching the raw utterance matches on stopwords: measured against a live
    server, "who is Isaac Newton" surfaced unrelated parish records because "who"
    matched. Extraction is load-bearing, not cosmetic."""
    assert extract_keyword(utterance) == expected


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "utterance,expected",
    [
        # The subject is the article title, so a trailing verb guarantees a
        # title-index miss. Measured on a live server: "when did john candy die"
        # extracted "john candy die" and returned zero suggestions.
        ("when did john candy die", "john candy"),
        ("when did the renaissance begin", "the renaissance"),
        ("when was mel brooks born", "mel brooks"),
        ("when did world war two end", "world war two"),
        ("when did apollo 11 launch", "apollo 11"),
        # A bare verb must survive rather than trimming to nothing: the framing
        # pattern matches here, leaving only the verb, and an empty keyword would
        # make the engine decline instead of searching.
        ("who is born", "born"),
        # No framing pattern matches, so the utterance passes through untouched.
        ("who died", "who died"),
        # Nouns that merely look verb-adjacent are untouched.
        ("what is the closed timelike curve", "closed timelike curve"),
    ],
)
def test_extract_keyword_drops_trailing_verbs(utterance, expected):
    assert extract_keyword(utterance) == expected


def test_engine_requires_a_book():
    """Search is scoped per-book; an empty scope is a configuration error."""
    client = KiwixClient(BASE)
    with pytest.raises(ValueError, match="book slug is required"):
        KiwixRetrievalEngine(client, book="")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _search_html(*results: tuple) -> str:
    """Build a kiwix-serve search results page.

    Each result is (href, title, snippet, word_count).
    """
    items = []
    for href, title, snippet, words in results:
        items.append(
            f'<li><a href="{href}">{title}</a>'
            f"<cite>{snippet}</cite>"
            f'<div class="informations">{words:,} words</div></li>'
        )
    return (
        "<html><body>"
        "<div>Results <b>1</b> of <b>2</b></div>"
        '<div class="results"><ul>' + "".join(items) + "</ul></div>"
        "</body></html>"
    )


def _engine(tuning: AnswerTuning = None, **overrides) -> KiwixRetrievalEngine:
    """Engine with the title index disabled — exercises the full-text path.

    Most gate behaviour is full-text specific (word counts, snippets), so these tests
    pin that path explicitly. The /suggest path has its own section below.
    """
    if tuning is None:
        tuning = AnswerTuning(use_suggest=False, **overrides)
    return KiwixRetrievalEngine(KiwixClient(BASE), book=BOOK, tuning=tuning)


def _suggest_json(*entries: tuple) -> str:
    """Build a /suggest payload. Each entry is (title, path).

    A trailing kind="pattern" entry is always appended: kiwix-serve emits one as a
    full-text fallback, and it carries no article path.
    """
    items = [
        {"value": title, "label": title, "kind": "path", "path": path}
        for title, path in entries
    ]
    items.append({"value": "x", "label": "containing...", "kind": "pattern"})
    return json.dumps(items)


# ---------------------------------------------------------------------------
# Answer selection
# ---------------------------------------------------------------------------

@respx.mock
def test_book_length_result_is_rejected_outright():
    """The measured failure case, isolated: a 101,851-word Gutenberg book is the ONLY
    hit, and its title matches the keyword perfectly. It must still be refused — its
    opening prose is 1820s publisher boilerplate, not an answer.

    Deliberately single-result so the word-count ceiling is the only thing that can
    reject it; a two-result version passes even with the ceiling disabled, because
    scoring alone would prefer the shorter article."""
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200,
            text=_search_html(
                ("/b/A/The_Life_of_Sir_Isaac_Newton", "Isaac Newton",
                 "publisher boilerplate", 101_851),
            ),
        )
    )
    assert _engine().search("who is Isaac Newton") is None


@respx.mock
def test_prefers_article_over_book_when_both_match():
    """Given a book-length text and a real article that both match the title, the
    article wins."""
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200,
            text=_search_html(
                ("/b/A/The_Life_of_Sir_Isaac_Newton", "The Life of Sir Isaac Newton",
                 "publisher boilerplate", 101_851),
                ("/b/A/Isaac_Newton", "Isaac Newton", "snippet", 1_200),
            ),
        )
    )
    respx.get(f"{BASE}/b/A/Isaac_Newton").mock(
        return_value=httpx.Response(
            200,
            text="<p>Isaac Newton was an English mathematician and physicist who "
                 "formulated the laws of motion and universal gravitation.</p>",
        )
    )

    answer = _engine().search("who is Isaac Newton")

    assert answer is not None
    assert answer.title == "Isaac Newton"
    assert "mathematician" in answer.summary


@respx.mock
def test_stub_article_is_rejected():
    """Below the floor there is not enough prose to speak."""
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200,
            text=_search_html(("/b/A/Water_filter", "Water filter", "stub", 5)),
        )
    )
    assert _engine().search("water filter") is None


@respx.mock
def test_returns_none_when_no_title_matches():
    """Staying silent beats speaking a passing mention. In a common_query contest a
    wrong answer is worse than no bid."""
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200,
            text=_search_html(
                ("/b/A/Name_Dominoes", "1970: Name Dominoes", "mentions Newton", 9_800),
                ("/b/A/Vital_Records", "Vital Records of Auburn", "who...", 31_294),
            ),
        )
    )
    assert _engine().search("who is Isaac Newton") is None


@respx.mock
def test_missing_word_count_does_not_disqualify():
    """word_count of 0 means the server omitted the metadata, not that the article is
    empty; discarding on missing metadata would lose good answers."""
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200,
            text='<html><body><div class="results"><ul>'
                 '<li><a href="/b/A/Water_filter">Water filter</a>'
                 "<cite>a snippet</cite></li></ul></div></body></html>",
        )
    )
    respx.get(f"{BASE}/b/A/Water_filter").mock(
        return_value=httpx.Response(
            200, text="<p>A water filter removes impurities from water using a "
                      "fine physical barrier or chemical process.</p>"
        )
    )

    answer = _engine().search("water filter")
    assert answer is not None
    assert "impurities" in answer.summary


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

@respx.mock
def test_stale_book_slug_raises_loudly():
    """Slugs embed dates (wikipedia_en_all_maxi_2024-01) so they break on ZIM update.
    kiwix-serve answers with a clean 'No such book:' 400. That is a config error and
    must not degrade into a silent no-answer."""
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(400, text="No such book: stale_slug_2024-01")
    )
    with pytest.raises(KiwixBookNotFound, match="list_books"):
        _engine().search("water filter")


@respx.mock
def test_search_timeout_yields_no_answer():
    """Measured: search on a 6.86M-article ZIM exceeds the client timeout. OVOS
    common_query has a bounded budget, so a late answer is worse than none."""
    respx.get(f"{BASE}/search").mock(side_effect=httpx.ReadTimeout("too slow"))
    assert _engine().search("water filter") is None


@respx.mock
def test_article_fetch_failure_falls_back_to_snippet():
    """Snippets read poorly but beat silence once the title is a strong match."""
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200,
            text=_search_html(
                ("/b/A/Water_filter", "Water filter", "A water filter removes "
                 "impurities from water.", 1_228),
            ),
        )
    )
    respx.get(f"{BASE}/b/A/Water_filter").mock(side_effect=httpx.ReadTimeout("slow"))

    answer = _engine().search("water filter")
    assert answer is not None
    assert "impurities" in answer.summary


# ---------------------------------------------------------------------------
# Summarisation
# ---------------------------------------------------------------------------

@respx.mock
def test_summary_is_trimmed_on_a_sentence_boundary():
    """A spoken answer must be short and must not end mid-sentence."""
    long_body = "<p>" + " ".join(
        f"Sentence number {i} about water filtration systems." for i in range(40)
    ) + "</p>"
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200,
            text=_search_html(("/b/A/Water_filter", "Water filter", "snip", 1_228)),
        )
    )
    respx.get(f"{BASE}/b/A/Water_filter").mock(
        return_value=httpx.Response(200, text=long_body)
    )

    answer = _engine().search("water filter")
    assert answer is not None
    assert len(answer.summary) <= 420
    assert answer.summary.endswith(".")


# ---------------------------------------------------------------------------
# Title index (/suggest) — the fast path
# ---------------------------------------------------------------------------

def _suggest_engine(**overrides) -> KiwixRetrievalEngine:
    return KiwixRetrievalEngine(
        KiwixClient(BASE), book=BOOK, tuning=AnswerTuning(**overrides)
    )


@respx.mock
def test_suggest_is_preferred_over_full_text():
    """The title index answers in ~35ms where full-text search took 6-26s cold on the
    same ZIM. If /suggest resolves, /search must never be called — respx would raise
    on the unmocked request if it were."""
    respx.get(f"{BASE}/suggest").mock(
        return_value=httpx.Response(
            200, text=_suggest_json(("Isaac Newton", "A/Isaac_Newton"))
        )
    )
    respx.get(f"{BASE}{BOOK_PREFIX}/A/Isaac_Newton").mock(
        return_value=httpx.Response(
            200,
            text="<p>Isaac Newton was an English mathematician and "
                 "physicist who formulated the laws of motion.</p>",
        )
    )

    answer = _suggest_engine().search("who is Isaac Newton")

    assert answer is not None
    assert answer.title == "Isaac Newton"
    assert "mathematician" in answer.summary


@respx.mock
def test_suggest_outranks_book_length_namesake():
    """Measured on a live Gutenberg ZIM: /suggest returns the exact-title article
    first and the 101k-word "The Life of Sir Isaac Newton" third — the inverse of
    full-text ranking. The exact-match bonus must preserve that ordering."""
    respx.get(f"{BASE}/suggest").mock(
        return_value=httpx.Response(
            200,
            text=_suggest_json(
                ("Isaac Newton Stevens", "A/Isaac_Newton_Stevens"),
                ("The Life of Sir Isaac Newton", "A/Life_of_Newton"),
                ("Isaac Newton", "A/Isaac_Newton"),
            ),
        )
    )
    respx.get(f"{BASE}{BOOK_PREFIX}/A/Isaac_Newton").mock(
        return_value=httpx.Response(
            200,
            text="<p>Isaac Newton was an English mathematician and "
                 "physicist who formulated the laws of motion.</p>",
        )
    )

    answer = _suggest_engine().search("Isaac Newton")
    assert answer is not None
    assert answer.title == "Isaac Newton"


@respx.mock
def test_falls_back_to_full_text_when_suggest_finds_nothing():
    """A title index miss must not end the search — descriptive-title corpora
    (Appropedia's "Water filter" for "how to purify water") still need full text."""
    respx.get(f"{BASE}/suggest").mock(
        return_value=httpx.Response(200, text=_suggest_json())
    )
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200,
            text=_search_html(("/b/A/Water_filter", "Water filter", "snip", 1_228)),
        )
    )
    respx.get(f"{BASE}/b/A/Water_filter").mock(
        return_value=httpx.Response(
            200,
            text="<p>A water filter removes impurities from water using a "
                 "fine physical barrier or a chemical process.</p>",
        )
    )

    answer = _suggest_engine().search("water filter")
    assert answer is not None
    assert "impurities" in answer.summary


@respx.mock
def test_falls_back_to_full_text_when_suggest_errors():
    """Older servers may lack a title index; that is a fallback, not a failure."""
    respx.get(f"{BASE}/suggest").mock(side_effect=httpx.ReadTimeout("slow"))
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200,
            text=_search_html(("/b/A/Water_filter", "Water filter", "snip", 1_228)),
        )
    )
    respx.get(f"{BASE}/b/A/Water_filter").mock(
        return_value=httpx.Response(
            200,
            text="<p>A water filter removes impurities from water using a "
                 "fine physical barrier or a chemical process.</p>",
        )
    )

    answer = _suggest_engine().search("water filter")
    assert answer is not None


@respx.mock
def test_suggest_pattern_entries_are_ignored():
    """kiwix-serve appends a kind="pattern" full-text fallback entry with no path;
    treating it as an article would fetch a nonexistent URL."""
    respx.get(f"{BASE}/suggest").mock(
        return_value=httpx.Response(200, text=_suggest_json())
    )
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, text=_search_html())
    )
    assert _suggest_engine().search("water filter") is None


@respx.mock
def test_suggest_title_gate_still_applies():
    """/suggest does prefix matching, so an unrelated hit is still possible."""
    respx.get(f"{BASE}/suggest").mock(
        return_value=httpx.Response(
            200, text=_suggest_json(("Something Else Entirely", "A/Unrelated"))
        )
    )
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, text=_search_html())
    )
    assert _suggest_engine().search("Isaac Newton") is None


# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title,words",
    [
        ("Isaac Newton", 1_200),
        ("Isaac Newton", 0),
        ("Isaac Newton Stevens", 900),
        ("The Life of Sir Isaac Newton", 101_851),
        ("Completely Unrelated", 500),
        ("", 0),
    ],
)
def test_confidence_stays_within_zero_and_one(title, words):
    """OVOS common_query documents confidence as a float in 0.0-1.0. An earlier
    additive scheme (recall + exact bonus + precision + concision bonus) could reach
    1.9 and violated that contract.

    Checked with non-default weights on purpose: the defaults happen to sum to 1.0,
    so an un-normalised implementation stays in range by luck and this assertion
    would pass while the contract was still broken.
    """
    engine = _engine(tuning=AnswerTuning(
        use_suggest=False,
        recall_weight=3.0,
        precision_weight=2.0,
        concision_weight=1.0,
    ))
    conf = engine._confidence(
        "Isaac Newton", title, _title_overlap("Isaac Newton", title), words
    )
    assert 0.0 <= conf <= 1.0


def test_exact_title_outranks_namesake_and_book():
    """Recall alone ties these (all contain both keyword tokens); precision is what
    orders them correctly."""
    engine = _engine()
    k = "Isaac Newton"

    def conf(title, words=900):
        return engine._confidence(k, title, _title_overlap(k, title), words)

    assert conf("Isaac Newton") > conf("Isaac Newton Stevens")
    assert conf("Isaac Newton Stevens") > conf("The Life of Sir Isaac Newton")


def test_concision_decays_rather_than_cliffs():
    """Length preference is graded: a 101k-word text scores lower than the same title
    at article length, without a hard cutoff inside the admitted range."""
    engine = _engine()
    k = "Isaac Newton"
    title = "The Life of Sir Isaac Newton"
    overlap = _title_overlap(k, title)
    assert engine._confidence(k, title, overlap, 900) > engine._confidence(
        k, title, overlap, 101_851
    )


def test_ranking_weights_are_validated():
    """Weights are normalised, so all-zero would mean 'rank by nothing'."""
    with pytest.raises(ValueError, match="non-negative"):
        AnswerTuning(recall_weight=-1.0)
    with pytest.raises(ValueError, match="at least one"):
        AnswerTuning(recall_weight=0, precision_weight=0, concision_weight=0)


@respx.mock
def test_deadline_skips_the_slow_full_text_path():
    """Once the budget is gone, entering full-text guarantees a wasted answer:
    measured 6-26s cold on a large ZIM, against a common_query window that collapses
    to ~2s. /search is left unmocked so respx raises if it is called."""
    respx.get(f"{BASE}/suggest").mock(
        side_effect=lambda request: (time.sleep(0.2), httpx.Response(200, text="[]"))[1]
    )
    engine = KiwixRetrievalEngine(
        KiwixClient(BASE), book=BOOK, tuning=AnswerTuning(deadline=0.05)
    )
    assert engine.search("water filter") is None


@respx.mock
def test_deadline_of_zero_disables_the_ceiling():
    """A deployment with a generous common_query window can opt out."""
    respx.get(f"{BASE}/suggest").mock(return_value=httpx.Response(200, text="[]"))
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200,
            text=_search_html(("/b/A/Water_filter", "Water filter", "snip", 1_228)),
        )
    )
    respx.get(f"{BASE}/b/A/Water_filter").mock(
        return_value=httpx.Response(
            200,
            text='<html><body><div id="mw-content-text"><p>A water filter removes '
                 "impurities from water using a fine physical barrier or a chemical "
                 "process.</p></div></body></html>",
        )
    )
    engine = KiwixRetrievalEngine(
        KiwixClient(BASE), book=BOOK, tuning=AnswerTuning(deadline=0)
    )
    answer = engine.search("water filter")
    assert answer is not None
    assert "impurities" in answer.summary


def test_deadline_is_validated():
    with pytest.raises(ValueError, match="deadline"):
        AnswerTuning(deadline=-1)


def test_tuning_rejects_impossible_thresholds():
    """Make illegal states unrepresentable rather than silently misbehaving."""
    with pytest.raises(ValueError, match="min_title_overlap"):
        AnswerTuning(min_title_overlap=1.5)
    with pytest.raises(ValueError, match="max_words"):
        AnswerTuning(min_words=500, max_words=100)
    with pytest.raises(ValueError, match="summary_chars"):
        AnswerTuning(summary_chars=0)
    with pytest.raises(ValueError, match="timeout"):
        AnswerTuning(timeout=0)


def test_from_config_ignores_unrelated_keys():
    """Callers pass a whole config block; unrelated keys must not explode."""
    tuning = AnswerTuning.from_config(
        {"min_words": 10, "base_url": "http://x", "book": "y"}
    )
    assert tuning.min_words == 10
    assert tuning.summary_chars == AnswerTuning().summary_chars


@respx.mock
def test_long_form_tuning_allows_book_length_results():
    """Project Gutenberg is entirely book-length, so the default ceiling would reject
    every article in it. for_long_form() disables the ceiling and compensates with a
    stricter title match."""
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200,
            text=_search_html(
                ("/b/A/The_Life_of_Sir_Isaac_Newton", "Isaac Newton",
                 "a snippet", 101_851),
            ),
        )
    )
    respx.get(f"{BASE}/b/A/The_Life_of_Sir_Isaac_Newton").mock(
        return_value=httpx.Response(
            200,
            text="<p>Isaac Newton was an English mathematician who formulated the "
                 "laws of motion and universal gravitation.</p>",
        )
    )

    engine = KiwixRetrievalEngine(
        KiwixClient(BASE), book=BOOK,
        tuning=AnswerTuning.for_long_form(use_suggest=False),
    )
    answer = engine.search("who is Isaac Newton")

    assert answer is not None
    assert "mathematician" in answer.summary
    # ...and the same corpus is refused under the default tuning.
    assert _engine().search("who is Isaac Newton") is None


@respx.mock
def test_custom_summary_budget_is_honoured():
    """summary_chars is a per-deployment preference, not a fixed constant."""
    body = "<p>" + " ".join(
        f"Sentence {i} about filtration systems and water." for i in range(40)
    ) + "</p>"
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200,
            text=_search_html(("/b/A/Water_filter", "Water filter", "snip", 1_228)),
        )
    )
    respx.get(f"{BASE}/b/A/Water_filter").mock(
        return_value=httpx.Response(200, text=body)
    )

    engine = KiwixRetrievalEngine(
        KiwixClient(BASE), book=BOOK,
        tuning=AnswerTuning(summary_chars=120, use_suggest=False),
    )
    answer = engine.search("water filter")
    assert answer is not None
    assert len(answer.summary) <= 140


@respx.mock
def test_relaxed_title_gate_admits_partial_matches():
    """A stricter or looser gate is a corpus-specific judgement call."""
    html = _search_html(
        ("/b/A/Solar_water_disinfection", "Solar water disinfection", "snip", 1_600),
    )
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, text=html))
    respx.get(f"{BASE}/b/A/Solar_water_disinfection").mock(
        return_value=httpx.Response(
            200, text="<p>Solar water disinfection uses sunlight to make water safe "
                      "to drink over several hours of exposure.</p>"
        )
    )

    # "purify water": only "water" overlaps -> 0.5, which the default admits but a
    # stricter gate must reject.
    strict = KiwixRetrievalEngine(
        KiwixClient(BASE), book=BOOK,
        tuning=AnswerTuning(min_title_overlap=0.9, use_suggest=False),
    )
    assert strict.search("how do I purify water") is None


@respx.mock
def test_sentences_split_for_tell_me_more():
    """get_expanded_answer/"tell me more" needs the summary as discrete steps."""
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200,
            text=_search_html(("/b/A/Water_filter", "Water filter", "snip", 1_228)),
        )
    )
    respx.get(f"{BASE}/b/A/Water_filter").mock(
        return_value=httpx.Response(
            200,
            text="<p>A water filter removes impurities. It uses a physical barrier. "
                 "Some designs add chemical treatment.</p>",
        )
    )

    answer = _engine().search("water filter")
    assert answer is not None
    assert len(answer.sentences()) == 3
