"""OVOS/Neon integration for Kiwix servers.

:mod:`kiwix_ovos.engine` is framework-free and importable without OVOS installed;
:mod:`kiwix_ovos.solver` requires the ``ovos`` extra.
"""
from kiwix_ovos.engine import (
    DEFAULT_TUNING,
    AnswerTuning,
    KiwixAnswer,
    KiwixBookNotFound,
    KiwixRetrievalEngine,
    extract_keyword,
)
from kiwix_ovos.library import (
    BookConfig,
    KiwixLibrary,
    books_from_config,
    preset,
)

__all__ = [
    "DEFAULT_TUNING",
    "AnswerTuning",
    "BookConfig",
    "KiwixAnswer",
    "KiwixBookNotFound",
    "KiwixLibrary",
    "KiwixRetrievalEngine",
    "books_from_config",
    "extract_keyword",
    "preset",
]
