"""OVOS/Neon QuestionSolver backed by a Kiwix server.

Thin adapter over :class:`kiwix_ovos.engine.KiwixRetrievalEngine`; all retrieval and
answer-quality logic lives there, framework-free.

Verified against the Neon Hub pins (ovos-plugin-manager 0.9.0, ovos-workshop 0.1.7):
that release already uses the modern ``(query, lang, units)`` solver signature, so no
compatibility shim is needed. Note the plugin *entry point group* is still the legacy
``neon.plugin.solver`` at 0.9.0 (upstream carries a ``TODO rename
"opm.solver.question"``), so pyproject declares both.

Unlike the Wikipedia solver this is derived from, answers come from a LAN-local Kiwix
server, so this plugin works with no internet connection.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ovos_plugin_manager.templates.solvers import QuestionSolver
from ovos_utils.log import LOG

from kiwix_ovos.engine import KiwixAnswer
from kiwix_ovos.library import KiwixLibrary, books_from_config

__all__ = ["KiwixSolver"]


class KiwixSolver(QuestionSolver):
    """Answer questions from offline ZIM archives served by kiwix-serve.

    Books are always explicit — which corpora may feed spoken answers is a deployment
    decision, not something to infer from the catalog::

        base_url: http://localhost:8080
        books:
          - book: wikipedia_en_all_maxi_2024-01
          - book: gutenberg_en_all_2023-08
            preset: long_form
          - book: wikihow_en_maxi_2023-03
            preset: how_to
            min_title_overlap: 0.4      # any AnswerTuning field overrides the preset

    Each book gets its own client and tuning; all are queried concurrently and the
    highest-confidence answer wins. A single ``book: <slug>`` key also works.
    """

    enable_tx = False
    priority = 60

    def __init__(self, config: Optional[Dict] = None, **kwargs) -> None:
        super().__init__(config=config or {}, **kwargs)
        # Raises when no book is configured. Failing at load beats a solver that
        # silently never answers, because that misconfiguration is invisible.
        self._library = _LoggingLibrary(
            books_from_config(self.config),
            base_url=self.config.get("base_url", "http://localhost:8080"),
        )

    # ------------------------------------------------------------------
    # Solver API
    # ------------------------------------------------------------------

    def get_spoken_answer(
        self,
        query: str,
        lang: Optional[str] = None,
        units: Optional[str] = None,
    ) -> Optional[str]:
        """Return a short spoken answer, or None to decline the question.

        Declining is a first-class outcome: in a common_query contest, silence beats
        speaking an article that merely mentions the subject.
        """
        answer = self._answer(query, lang)
        return answer.summary if answer else None

    def get_data(
        self,
        query: str,
        lang: Optional[str] = None,
        units: Optional[str] = None,
    ) -> Optional[Dict[str, str]]:
        answer = self._answer(query, lang)
        if not answer:
            return None
        return {
            "title": answer.title,
            "summary": answer.summary,
            "url": answer.url,
            "book": answer.book,
        }

    def get_expanded_answer(
        self,
        query: str,
        lang: Optional[str] = None,
        units: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Sentence-by-sentence steps backing "tell me more"."""
        answer = self._answer(query, lang)
        if not answer:
            return []
        return [
            {"title": answer.title, "summary": sentence, "img": ""}
            for sentence in answer.sentences()
        ]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _answer(
        self, query: str, lang: Optional[str] = None
    ) -> Optional[KiwixAnswer]:
        """Return the best answer across configured books, or None.

        The solver must never raise into the common_query path — one misconfigured
        plugin should not take down the whole question pipeline. Per-book failures
        are contained by the library and logged there.
        """
        try:
            return self._library.search(query, lang=lang)
        except Exception as exc:  # defensive: unexpected transport/parse failure
            LOG.warning(f"KiwixSolver failed to answer {query!r}: {exc}")
            return None


class _LoggingLibrary(KiwixLibrary):
    """KiwixLibrary that reports per-book failures through the OVOS logger.

    A book that never answers is otherwise invisible, and dated slugs
    (``wikipedia_en_all_maxi_2024-01``) break on every ZIM update.
    """

    def record_failure(self, book: str, reason: str) -> None:
        super().record_failure(book, reason)
        LOG.error(f"KiwixSolver: book {book!r} failed: {reason}")
