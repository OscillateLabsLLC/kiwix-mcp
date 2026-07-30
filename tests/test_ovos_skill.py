"""Tests for the OVOS/Neon CommonQuery skill.

Skipped unless the ``skill`` extra is installed, so the MCP-only install stays lean.

The skill is exercised without a messagebus: OVOSSkill's constructor wants a live
bus, but the CommonQuery contract is a pure function of settings, so the object is
built directly and its settings injected.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

pytest.importorskip("ovos_workshop", reason="requires the 'skill' extra")

from ovos_workshop.skills.common_query_skill import CQSMatchLevel  # noqa: E402

from kiwix_ovos.skill import KiwixSkill  # noqa: E402

BASE = "http://localhost:9090"
BOOK = "wikipedia_en_all_maxi_2024-01"

_ARTICLE = (
    '<html><body><div id="mw-content-text"><p>Sir Isaac Newton was an English '
    "mathematician and physicist who formulated the laws of motion and universal "
    "gravitation. He also built the first practical reflecting telescope.</p>"
    "</div></body></html>"
)


def _suggest(title: str, path: str) -> str:
    return json.dumps([
        {"value": title, "kind": "path", "path": path},
        {"value": "x", "label": "containing...", "kind": "pattern"},
    ])


def _skill(settings: dict) -> KiwixSkill:
    """Build a skill with injected settings and no messagebus."""
    skill = KiwixSkill.__new__(KiwixSkill)
    skill._settings = settings
    skill._library = None
    skill._sessions = {}
    skill._reload_library()
    return skill


def _configured() -> KiwixSkill:
    return _skill({"base_url": BASE, "books": [{"book": BOOK}]})


def _mock_answer() -> None:
    respx.get(f"{BASE}/suggest").mock(
        return_value=httpx.Response(200, text=_suggest("Isaac Newton", "A/Newton"))
    )
    respx.get(f"{BASE}/{BOOK}/A/Newton").mock(
        return_value=httpx.Response(200, text=_ARTICLE)
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_bad_config_does_not_prevent_loading():
    """An unloadable skill is harder to diagnose than one that logs and declines."""
    skill = _skill({})
    assert skill._library is None
    assert skill.CQS_match_query_phrase("who is Isaac Newton") is None


def test_runtime_requirements_allow_offline_operation():
    """Inverted from the Wikipedia skills this derives from. Getting this wrong
    stops the skill loading offline, which is the whole point of Kiwix."""
    reqs = KiwixSkill.runtime_requirements
    assert reqs.requires_internet is False
    assert reqs.internet_before_load is False
    assert reqs.requires_network is True


# ---------------------------------------------------------------------------
# CommonQuery contract
# ---------------------------------------------------------------------------

@respx.mock
def test_returns_the_four_tuple_the_framework_unpacks():
    """ovos-workshop 0.1.x reads `answer = result[2]`. The abstract method's own
    docstring describes a 3-tuple in a different order; the framework's unpacking
    is authoritative."""
    _mock_answer()
    result = _configured().CQS_match_query_phrase("who is Isaac Newton")

    assert result is not None
    assert len(result) == 4
    match, level, answer, callback = result
    assert match == "who is Isaac Newton"
    assert isinstance(level, CQSMatchLevel)
    assert "mathematician" in answer
    assert callback["book"] == BOOK
    assert callback["title"] == "Isaac Newton"


@respx.mock
def test_exact_title_match_bids_exact():
    _mock_answer()
    _, level, _, _ = _configured().CQS_match_query_phrase("who is Isaac Newton")
    assert level == CQSMatchLevel.EXACT


@respx.mock
def test_weak_match_bids_below_exact():
    """A padded namesake is a weaker answer and must not claim an exact match."""
    respx.get(f"{BASE}/suggest").mock(
        return_value=httpx.Response(
            200, text=_suggest("Isaac Newton Stevens and the Territory", "A/S")
        )
    )
    respx.get(f"{BASE}/{BOOK}/A/S").mock(
        return_value=httpx.Response(200, text=_ARTICLE)
    )
    result = _configured().CQS_match_query_phrase("Isaac Newton")
    assert result is not None
    assert result[1] != CQSMatchLevel.EXACT


@respx.mock
def test_declines_when_nothing_answers():
    """Declining is first-class: silence beats speaking a passing mention."""
    respx.get(f"{BASE}/suggest").mock(return_value=httpx.Response(200, text="[]"))
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200, text='<html><body><div class="results"><ul></ul></div></body></html>'
        )
    )
    assert _configured().CQS_match_query_phrase("xyzzy nonsense") is None


@respx.mock
def test_transport_failure_declines_rather_than_raising():
    """CQS_match_query_phrase must never raise into the query pipeline."""
    respx.get(f"{BASE}/suggest").mock(side_effect=httpx.ConnectError("down"))
    respx.get(f"{BASE}/search").mock(side_effect=httpx.ConnectError("down"))
    assert _configured().CQS_match_query_phrase("who is Isaac Newton") is None


# ---------------------------------------------------------------------------
# Follow-up state
# ---------------------------------------------------------------------------

@respx.mock
def test_cqs_action_arms_tell_me_more():
    """Previously CQS_action only logged, so "tell me more" after a common-query
    answer replied "that is all I have" while article text remained unread.

    Needs an article longer than the summary budget — otherwise the summary covers
    the whole article and there is legitimately nothing to continue with.
    """
    tail = " ".join(
        f"Newton also investigated topic number {i} in considerable depth "
        f"during his later years at Cambridge." for i in range(30)
    )
    respx.get(f"{BASE}/suggest").mock(
        return_value=httpx.Response(200, text=_suggest("Isaac Newton", "A/Newton"))
    )
    respx.get(f"{BASE}/{BOOK}/A/Newton").mock(
        return_value=httpx.Response(
            200,
            text='<html><body><div id="mw-content-text"><p>Sir Isaac Newton was '
                 "an English mathematician and physicist who formulated the laws of "
                 f"motion and universal gravitation. {tail}</p></div></body></html>",
        )
    )
    skill = _configured()
    result = skill.CQS_match_query_phrase("who is Isaac Newton")
    assert result is not None

    _, _, answer, callback = result
    callback["answer"] = answer  # the framework injects this before CQS_action
    skill.CQS_action("who is Isaac Newton", callback)

    queued, _ = skill._sessions[skill._session_id]
    assert queued, "no follow-up queued after winning the contest"
    # The framework speaks the whole summary, so the continuation must go past it.
    assert not any(sentence in answer for sentence in queued)


@respx.mock
def test_cqs_action_without_a_url_queues_nothing():
    """A missing URL is not a crash; it just means no continuation."""
    _mock_answer()
    skill = _configured()
    skill.CQS_action("who is Isaac Newton", {"answer": "spoken", "book": BOOK})
    assert skill._sessions[skill._session_id][0] == []


@respx.mock
def test_remember_stores_remaining_sentences_for_tell_me_more():
    """The first sentence is spoken immediately, so only the rest is queued."""
    _mock_answer()
    skill = _configured()
    answer = skill._search("who is Isaac Newton")
    assert answer is not None

    skill._remember(answer)
    queued, _ = skill._sessions[skill._session_id]
    assert queued
    assert "reflecting telescope" in " ".join(queued)
    assert not any(q.startswith("Sir Isaac Newton was") for q in queued)
