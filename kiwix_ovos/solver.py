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

from dataclasses import fields
from typing import Dict, List, Optional

from ovos_plugin_manager.templates.solvers import QuestionSolver
from ovos_utils.log import LOG

from kiwix_client import KiwixClient
from kiwix_ovos.engine import (
    AnswerTuning,
    KiwixAnswer,
    KiwixBookNotFound,
    KiwixRetrievalEngine,
)

__all__ = ["KiwixSolver"]


class KiwixSolver(QuestionSolver):
    """Answer questions from offline ZIM archives served by kiwix-serve.

    Connection config:
        base_url:   kiwix-serve URL (default ``http://localhost:8080``)
        book:       book slug or stable OPDS name to search (required)
        long_form:  set true for book-length corpora such as Project Gutenberg, which
                    would otherwise be rejected wholesale by the length ceiling

    Every field of :class:`~kiwix_ovos.engine.AnswerTuning` may also be set here
    (``min_words``, ``max_words``, ``min_title_overlap``, ``summary_chars``,
    ``timeout``, …); unrecognised keys are ignored.
    """

    enable_tx = False
    priority = 60

    def __init__(self, config: Optional[Dict] = None, **kwargs) -> None:
        super().__init__(config=config or {}, **kwargs)
        base_url = self.config.get("base_url", "http://localhost:8080")
        book = self.config.get("book", "")
        if not book:
            # Fail loudly: a solver that silently never answers is worse than one that
            # refuses to load, because the misconfiguration is invisible in logs.
            raise ValueError(
                "KiwixSolver requires a 'book' config key (a ZIM slug or OPDS name). "
                "kiwix-serve scopes search per-book."
            )

        tuning = self._build_tuning(self.config)
        self._engine = KiwixRetrievalEngine(
            KiwixClient(base_url, timeout=tuning.timeout),
            book=book,
            tuning=tuning,
        )

    @staticmethod
    def _build_tuning(config: Dict) -> AnswerTuning:
        """Derive tuning from config, honouring the ``long_form`` preset."""
        if config.get("long_form"):
            overrides = {
                k: v for k, v in config.items()
                if k in {f.name for f in fields(AnswerTuning)}
            }
            return AnswerTuning.for_long_form(**overrides)
        return AnswerTuning.from_config(config)

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
        answer = self._answer(query)
        return answer.summary if answer else None

    def get_data(
        self,
        query: str,
        lang: Optional[str] = None,
        units: Optional[str] = None,
    ) -> Optional[Dict[str, str]]:
        answer = self._answer(query)
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
        answer = self._answer(query)
        if not answer:
            return []
        return [
            {"title": answer.title, "summary": sentence, "img": ""}
            for sentence in answer.sentences()
        ]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _answer(self, query: str) -> Optional[KiwixAnswer]:
        """Query the engine, converting a stale-slug config error into a logged decline.

        The solver must never raise into the common_query path — one misconfigured
        plugin should not take down the whole question pipeline.
        """
        try:
            return self._engine.search(query)
        except KiwixBookNotFound as exc:
            LOG.error(f"KiwixSolver misconfigured: {exc}")
            return None
        except Exception as exc:  # defensive: unexpected transport/parse failure
            LOG.warning(f"KiwixSolver failed to answer {query!r}: {exc}")
            return None
