"""Keyword matching with morphological flexibility + ref association.

Two modes for associating refs to a keyword hit:
  - sentence mode (default): refs in the SAME sentence as the keyword OR
    in the NEXT sentence. Uses pysbd for boundary detection. Precise.
  - char-window mode (legacy / fallback): refs within ±N characters of the
    keyword. More permissive, used as a fallback when sentence detection
    misbehaves.
"""
from __future__ import annotations
import logging
import re

import pysbd

from .constants import KEYWORD_MORPHO_MIN_LEN
from .models import KeywordHit, ParsedPaper

logger = logging.getLogger(__name__)

_segmenter: pysbd.Segmenter | None = None


def _get_segmenter() -> pysbd.Segmenter:
    global _segmenter
    if _segmenter is None:
        _segmenter = pysbd.Segmenter(language="en", clean=False, char_span=True)
    return _segmenter


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end) char offsets for every sentence in `text`."""
    try:
        out = _get_segmenter().segment(text)
    except Exception as e:
        logger.warning("pysbd failed (%s); falling back to single span", e)
        return [(0, len(text))]
    return [(s.start, s.end) for s in out]


def _find_sentence_idx(spans: list[tuple[int, int]], pos: int) -> int:
    """Binary-ish search for the sentence containing `pos`. Returns -1 if none."""
    for i, (a, b) in enumerate(spans):
        if a <= pos < b:
            return i
    # fallback: keyword sits in inter-sentence whitespace
    for i, (a, _b) in enumerate(spans):
        if a > pos:
            return max(0, i - 1)
    return len(spans) - 1 if spans else -1


def build_pattern(keyword: str) -> re.Pattern[str]:
    """Build a flexible regex from a keyword.

    - Tokens separated by whitespace or hyphens are joined with [\\s\\-]?
    - The last token is truncated to its stem (len-2) to allow morphological
      variants (e.g. "independent" -> "independen\\w*").
    - Single short token: just match the stem with \\w* suffix.
    """
    tokens = [t for t in re.split(r"[\s\-]+", keyword.strip()) if t]
    if not tokens:
        raise ValueError("Empty keyword")

    parts: list[str] = []
    for i, tok in enumerate(tokens):
        is_last = i == len(tokens) - 1
        if is_last and len(tok) > KEYWORD_MORPHO_MIN_LEN:
            stem = re.escape(tok[:-2])
            parts.append(stem + r"\w*")
        else:
            parts.append(re.escape(tok))

    pattern_str = r"(?<!\w)" + r"[\s\-]?".join(parts)
    return re.compile(pattern_str, re.IGNORECASE)


def search(
    parsed: ParsedPaper,
    keyword: str,
    context_window: int | None = None,
) -> list[KeywordHit]:
    """Find all matches of `keyword` and associate inline refs.

    If `context_window` is None (default), use sentence-based association:
    refs in the SAME sentence as the keyword OR the NEXT sentence count.
    If an int is provided, use the legacy ±N char window instead.
    """
    pattern = build_pattern(keyword)
    text = parsed.text
    hits: list[KeywordHit] = []

    use_sentences = context_window is None
    spans = _sentence_spans(text) if use_sentences else []

    for m in pattern.finditer(text):
        start, end = m.start(), m.end()

        if use_sentences:
            idx = _find_sentence_idx(spans, start)
            if idx == -1:
                win_start, win_end = start, end
            else:
                win_start = spans[idx][0]
                next_idx = idx + 1
                win_end = spans[next_idx][1] if next_idx < len(spans) else spans[idx][1]
        else:
            win_start = max(0, start - context_window)
            win_end = min(len(text), end + context_window)

        ref_keys: list[str] = []
        seen: set[str] = set()
        for ref in parsed.inline_refs:
            if ref.start >= win_start and ref.end <= win_end:
                if ref.bib_key not in seen:
                    ref_keys.append(ref.bib_key)
                    seen.add(ref.bib_key)

        snippet = re.sub(r"\s+", " ", text[win_start:win_end]).strip()
        hits.append(
            KeywordHit(
                passage=snippet,
                match_start=start,
                match_end=end,
                ref_keys=ref_keys,
            )
        )

    logger.debug("Found %d keyword hit(s) for '%s'", len(hits), keyword)
    return hits


def collect_ref_keys(hits: list[KeywordHit]) -> list[str]:
    """Union of ref_keys across all hits, preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        for k in h.ref_keys:
            if k not in seen:
                seen.add(k)
                out.append(k)
    return out


def context_for_ref(hits: list[KeywordHit], ref_key: str) -> str:
    """Return the first passage that mentions `ref_key`."""
    for h in hits:
        if ref_key in h.ref_keys:
            return h.passage
    return ""
