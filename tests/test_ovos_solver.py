"""Tests for the OVOS QuestionSolver adapter.

Skipped unless the ``ovos`` extra is installed, so the MCP-only install stays lean.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

pytest.importorskip(
    "ovos_plugin_manager", reason="requires the 'ovos' extra"
)

from kiwix_ovos.solver import KiwixSolver  # noqa: E402

BASE = "http://localhost:8080"
BOOK = "appropedia_en_all_maxi_2025-03"

_RESULTS_HTML = (
    '<html><body><div class="results"><ul>'
    '<li><a href="/b/A/Water_filter">Water filter</a><cite>a snippet</cite>'
    '<div class="informations">1,228 words</div></li>'
    "</ul></div></body></html>"
)
_ARTICLE_HTML = (
    "<p>A water filter removes impurities from water using a fine physical barrier. "
    "Some designs add a chemical process.</p>"
)


def _mock_server(suggest: str = "[]") -> None:
    """Mock both lookup paths.

    The engine tries the /suggest title index first and falls back to /search, so a
    test that mocks only one leaves the other unmocked. Default is an empty title
    index, exercising the full-text fallback.
    """
    respx.get(f"{BASE}/suggest").mock(
        return_value=httpx.Response(200, text=suggest)
    )
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, text=_RESULTS_HTML)
    )
    respx.get(f"{BASE}/b/A/Water_filter").mock(
        return_value=httpx.Response(200, text=_ARTICLE_HTML)
    )


def test_missing_book_config_raises():
    """A solver that silently never answers hides its own misconfiguration."""
    with pytest.raises(ValueError, match="'book' config key"):
        KiwixSolver({})


@respx.mock
def test_get_spoken_answer_extracts_keyword_and_answers():
    """Exercises the full path: framing stripped, gate passed, article summarised."""
    _mock_server()
    solver = KiwixSolver({"book": BOOK})
    answer = solver.get_spoken_answer("how do I purify water")
    assert answer is not None
    assert "impurities" in answer


@respx.mock
def test_get_expanded_answer_returns_sentence_steps():
    """"tell me more" walks the summary one sentence at a time."""
    _mock_server()
    solver = KiwixSolver({"book": BOOK})
    steps = solver.get_expanded_answer("water filter")
    assert len(steps) == 2
    assert all(step["title"] == "Water filter" for step in steps)


@respx.mock
def test_prefers_title_index_over_full_text():
    """The title index answers in ~35ms where full-text took 6-26s cold on the same
    ZIM. /search is deliberately left unmocked: respx raises if it is called."""
    respx.get(f"{BASE}/suggest").mock(
        return_value=httpx.Response(200, text=json.dumps([
            {"value": "Water filter", "kind": "path", "path": "A/Water_filter"},
        ]))
    )
    respx.get(f"{BASE}/{BOOK}/A/Water_filter").mock(
        return_value=httpx.Response(200, text=_ARTICLE_HTML)
    )
    solver = KiwixSolver({"book": BOOK})
    answer = solver.get_spoken_answer("water filter")
    assert answer is not None
    assert "impurities" in answer


@respx.mock
def test_declines_instead_of_raising_on_stale_slug():
    """A misconfigured plugin must not raise into the common_query pipeline and take
    down every other solver with it; it logs and declines."""
    respx.get(f"{BASE}/suggest").mock(return_value=httpx.Response(200, text="[]"))
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(400, text="No such book: stale_2024-01")
    )
    solver = KiwixSolver({"book": "stale_2024-01"})
    assert solver.get_spoken_answer("water filter") is None


@respx.mock
def test_tuning_keys_pass_through_from_config():
    """Skill settings must be able to reach the engine's thresholds."""
    solver = KiwixSolver({"book": BOOK, "summary_chars": 99, "min_words": 5})
    assert solver._engine.tuning.summary_chars == 99
    assert solver._engine.tuning.min_words == 5


@respx.mock
def test_long_form_config_disables_length_ceiling():
    """Pointing the solver at Project Gutenberg requires the long_form preset, or the
    default ceiling rejects every article in the corpus."""
    solver = KiwixSolver({"book": "gutenberg_en_all_2023-08", "long_form": True})
    assert solver._engine.tuning.max_words == 0
    assert solver._engine.tuning.min_title_overlap == 0.75


@respx.mock
def test_declines_when_nothing_clears_the_gate():
    """Silence beats speaking an article that merely mentions the subject."""
    respx.get(f"{BASE}/suggest").mock(return_value=httpx.Response(200, text="[]"))
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200,
            text='<html><body><div class="results"><ul>'
                 '<li><a href="/b/A/Unrelated">Something Else Entirely</a>'
                 "<cite>snip</cite>"
                 '<div class="informations">900 words</div></li>'
                 "</ul></div></body></html>",
        )
    )
    solver = KiwixSolver({"book": BOOK})
    assert solver.get_spoken_answer("water filter") is None
