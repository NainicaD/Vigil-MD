"""Text normalization for device search. Pure functions, no I/O.

Search text and the user's words are normalized the same way: lower case, every run of
characters other than a-z and 0-9 becomes one space ("DreamStation® Auto-CPAP" →
"dreamstation auto cpap"). Matching is on these words; typos are scored with difflib.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def norm(text: str | None) -> str:
    return _NON_ALNUM.sub(" ", (text or "").lower()).strip()


def words(text: str | None) -> list[str]:
    return norm(text).split()


def trigrams(word: str) -> list[str]:
    """The 3-letter pieces of a word ("dream" → dre, rea, eam), for the index's OR query."""
    return [word[i:i + 3] for i in range(len(word) - 2)] if len(word) >= 3 else []


def word_similarity(a: str, b: str) -> float:
    """1.0 for the same word or its plural ("implants" / "implant"); e.g. 0.96 for
    dreamstaton / dreamstation. Prefixes of at least 4 letters count as a match
    ("dreamst" → dreamstation)."""
    if a == b or (len(a) > 3 and len(b) > 3 and a.rstrip("s") == b.rstrip("s")):
        return 1.0
    if len(a) >= 4 and b.startswith(a):
        return 0.95
    return SequenceMatcher(None, a, b).ratio()


def phrase_score(query_words: list[str], target_words: list[str]) -> float:
    """How well the query words are covered by the target: the mean, over query words, of
    the best similarity to any target word. 1.0 when every query word is in the target."""
    if not query_words or not target_words:
        return 0.0
    return sum(max(word_similarity(q, t) for t in target_words) for q in query_words) / len(query_words)
