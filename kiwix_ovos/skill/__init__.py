"""OVOS/Neon skill answering questions from offline Kiwix ZIM archives.

Targets ovos-workshop 0.1.x (the Neon Hub pin). The 0.x CommonQuerySkill contract
differs from modern OVOS: it implements ``CQS_match_query_phrase`` rather than using
the ``@common_query`` decorator, which does not exist in this release.

All retrieval lives in :mod:`kiwix_ovos.library`, which has no OVOS imports; this
module is the thin voice layer over it.
"""
import re
import time
from typing import Dict, List, Optional, Tuple

from ovos_utils import classproperty
from ovos_utils.log import LOG
from ovos_utils.process_utils import RuntimeRequirements
from ovos_workshop.decorators import intent_handler
from ovos_workshop.skills.common_query_skill import CommonQuerySkill, CQSMatchLevel

from kiwix_ovos.engine import KiwixAnswer
from kiwix_ovos.library import KiwixLibrary, books_from_config

__all__ = ["KiwixSkill"]

#: Engine confidence at or above which we claim an exact match. An exact title hit
#: scores 1.0, so this admits near-exact matches without promoting vague ones.
_EXACT_MATCH_CONFIDENCE = 0.9

#: Below this the answer is a weak topical match rather than a real answer.
_CATEGORY_MATCH_CONFIDENCE = 0.6

_RE_SENTENCE = re.compile(r"(?<=[.!?])\s+")

#: Cap on queued follow-up sentences. Articles run to thousands of sentences and
#: nobody says "tell me more" a hundred times; this bounds per-session memory.
_MAX_CONTINUATION_SENTENCES = 40

#: How long a session's follow-up queue survives without use. Refreshed on every
#: "tell me more", so an active conversation never expires mid-topic; this only
#: reclaims state from sessions that walked away.
_SESSION_TTL_SECONDS = 30 * 60


class KiwixSkill(CommonQuerySkill):
    """Answer general-knowledge questions from a local Kiwix server."""

    @classproperty
    def runtime_requirements(self):
        """Kiwix is LAN-local, so this skill works with no internet connection.

        Inverted from the Wikipedia skills this derives from, which require
        internet. Getting this wrong would stop the skill loading offline, which is
        the entire reason to run Kiwix.
        """
        return RuntimeRequirements(
            internet_before_load=False,
            network_before_load=False,
            gui_before_load=False,
            requires_internet=False,
            requires_network=True,
            requires_gui=False,
            no_internet_fallback=True,
            no_network_fallback=False,
            no_gui_fallback=True,
        )

    def initialize(self):
        self._library: Optional[KiwixLibrary] = None
        # session_id -> (remaining_sentences, last_used_monotonic)
        self._sessions: Dict[str, tuple] = {}
        self._reload_library()
        self.settings_change_callback = self._reload_library

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _reload_library(self) -> None:
        """Build the library from settings, tolerating a bad config.

        A configuration error must not stop the skill loading — an unloadable skill
        is harder to diagnose than one that logs and declines.
        """
        try:
            self._library = _LoggingLibrary(
                books_from_config(self.settings),
                base_url=self.settings.get("base_url", "http://localhost:8080"),
            )
            LOG.info(f"Kiwix skill ready; books: {self._library.books}")
        except ValueError as exc:
            self._library = None
            LOG.error(
                f"Kiwix skill has no usable book configuration, so it will not "
                f"answer: {exc}"
            )

    # ------------------------------------------------------------------
    # Common query
    # ------------------------------------------------------------------

    def CQS_match_query_phrase(
        self, phrase: str
    ) -> Optional[Tuple[str, CQSMatchLevel, str, Dict]]:
        """Bid on a question in the common-query contest.

        Returns the 4-tuple ``(match, level, answer, callback)`` that
        ovos-workshop 0.1.x expects. Note the abstract method's docstring describes
        a 3-tuple in a different order; the framework's own unpacking
        (``answer = result[2]``) is authoritative.

        Returning None is a first-class outcome — declining beats speaking an
        article that merely mentions the subject.
        """
        answer = self._search(phrase)
        if answer is None:
            return None
        return (
            phrase,
            self._match_level(answer.confidence),
            answer.summary,
            {
                "book": answer.book,
                "title": answer.title,
                # Carried so CQS_action can arm follow-ups without re-querying.
                "url": answer.url,
            },
        )

    def CQS_action(self, phrase: str, data: Dict) -> None:
        """Arm "tell me more" once this skill wins the contest.

        Only the winner should arm follow-ups — doing this in
        CQS_match_query_phrase would let a losing skill's answer be continued.

        The framework speaks the whole summary before calling this
        (``self.speak(data["answer"])``), so the continuation is the article text
        *after* the summary, not the summary's own remaining sentences.
        """
        LOG.debug(f"Kiwix answered from {data.get('book')}: {data.get('title')}")
        spoken = data.get("answer") or ""
        self._queue_continuation(
            self._continuation(data.get("url", ""), spoken, data.get("book", ""))
        )

    @staticmethod
    def _match_level(confidence: float) -> CQSMatchLevel:
        """Map engine confidence onto the framework's coarse match levels."""
        if confidence >= _EXACT_MATCH_CONFIDENCE:
            return CQSMatchLevel.EXACT
        if confidence >= _CATEGORY_MATCH_CONFIDENCE:
            return CQSMatchLevel.CATEGORY
        return CQSMatchLevel.GENERAL

    # ------------------------------------------------------------------
    # Explicit intents
    # ------------------------------------------------------------------

    @intent_handler("search_kiwix.intent")
    def handle_search(self, message):
        """Answer a direct "look it up in Kiwix" request."""
        query = message.data.get("query", "").strip()
        if not query:
            self.speak_dialog("no_query")
            return

        answer = self._search(query)
        if answer is None:
            self.speak_dialog("no_answer", {"query": query})
            return

        self._remember(answer)
        self.speak(answer.summary)

    @intent_handler("tell_more.intent")
    def handle_tell_more(self, _message):
        """Continue the last answer one sentence at a time."""
        remaining = self._take_continuation()
        if not remaining:
            self.speak_dialog("nothing_more")
            return
        self.speak(remaining)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _queue_continuation(self, sentences: List[str]) -> None:
        """Arm follow-ups for this session, expiring other sessions' stale state.

        Sessions come and go without telling us, so state is evicted by age rather
        than left to grow for the process lifetime.
        """
        now = time.monotonic()
        self._sessions = {
            key: value for key, value in self._sessions.items()
            if now - value[1] <= _SESSION_TTL_SECONDS
        }
        self._sessions[self._session_id] = (list(sentences), now)

    def _take_continuation(self) -> str:
        """Pop the next follow-up sentence, refreshing this session's freshness.

        Refresh-on-read matters: a listener working steadily through "tell me more"
        must not have their place expire out from under them mid-topic.
        """
        entry = self._sessions.get(self._session_id)
        if not entry:
            return ""
        sentences, _ = entry
        if not sentences:
            return ""
        spoken = sentences.pop(0)
        self._sessions[self._session_id] = (sentences, time.monotonic())
        return spoken

    @property
    def _session_id(self) -> str:
        """Per-session key so concurrent users do not share follow-up state."""
        try:
            from ovos_bus_client.session import SessionManager

            return SessionManager.get().session_id
        except Exception:
            return "default"

    def _search(self, query: str) -> Optional[KiwixAnswer]:
        """Query the library, never raising into the skill framework.

        ``self.lang`` is the session's language; the library uses it to skip books
        that answer in another language, since ZIMs are single-language and
        answering an Italian question from an English Wikipedia is worse than
        declining.
        """
        if self._library is None:
            return None
        try:
            return self._library.search(query, lang=self._lang)
        except Exception as exc:  # defensive: transport/parse failure
            LOG.warning(f"Kiwix skill failed to answer {query!r}: {exc}")
            return None

    @property
    def _lang(self) -> Optional[str]:
        """Session language, or None when it cannot be determined."""
        try:
            return self.lang
        except Exception:
            return None

    def _remember(self, answer: KiwixAnswer) -> None:
        """Stash the answer's remaining sentences for "tell me more".

        Used by the explicit intent, which speaks one sentence at a time. The
        CommonQuery path differs: the framework speaks the whole summary, so
        :meth:`CQS_action` continues past it instead.
        """
        sentences = answer.sentences()
        self._queue_continuation(sentences[1:] if sentences else [])

    def _continuation(self, url: str, already_spoken: str, book: str = "") -> List[str]:
        """Sentences of the article that follow what was already said.

        Re-fetches rather than caching the full article: the answer summary is
        deliberately short, and holding whole articles per session would grow
        without bound.
        """
        if not url or self._library is None:
            return []
        try:
            text = self._library.article_text(url, book)
        except Exception as exc:  # defensive: transport/parse failure
            LOG.debug(f"Kiwix skill could not load a continuation: {exc}")
            return []
        if not text:
            return []

        remainder = text[len(already_spoken):] if text.startswith(
            already_spoken
        ) else text
        sentences = [s.strip() for s in _RE_SENTENCE.split(remainder) if s.strip()]
        return sentences[:_MAX_CONTINUATION_SENTENCES]


class _LoggingLibrary(KiwixLibrary):
    """KiwixLibrary that reports per-book failures through the OVOS logger.

    A book that never answers is invisible otherwise, and dated slugs
    (``wikipedia_en_all_maxi_2024-01``) break on every ZIM update.
    """

    def record_failure(self, book: str, reason: str) -> None:
        super().record_failure(book, reason)
        LOG.error(f"Kiwix skill: book {book!r} failed: {reason}")
