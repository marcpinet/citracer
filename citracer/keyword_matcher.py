"""Keyword matching with morphological flexibility + ref association.

Two modes for associating refs to a keyword hit:
  - sentence mode (default): refs in the SAME sentence as the keyword OR
    in the NEXT sentence. Uses pysbd for boundary detection. Precise.
  - char-window mode (legacy / fallback): refs within ±N characters of the
    keyword. More permissive, used as a fallback when sentence detection
    misbehaves.

An optional semantic mode (``--semantic``) adds a second pass using a
sentence-transformer model. The regex pass runs first (fast, precise);
the semantic pass then scans sentences the regex missed and keeps those
whose embedding is close enough to the keyword. Results are unioned.
"""
from __future__ import annotations
import logging
import re

import pysbd

from .constants import (
    KEYWORD_MORPHO_MIN_LEN,
    SEMANTIC_DEFAULT_MODEL,
    SEMANTIC_SIMILARITY_THRESHOLD,
)
from .models import InlineRef, KeywordHit, ParsedPaper

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


# ---------------------------------------------------------------------------
# Shared helpers for building KeywordHit from a sentence window
# ---------------------------------------------------------------------------

def _refs_in_window(
    inline_refs: list[InlineRef],
    win_start: int,
    win_end: int,
) -> list[str]:
    """Return deduplicated bib_keys for refs whose span falls inside the window."""
    seen: set[str] = set()
    out: list[str] = []
    for ref in inline_refs:
        if ref.start >= win_start and ref.end <= win_end:
            if ref.bib_key not in seen:
                out.append(ref.bib_key)
                seen.add(ref.bib_key)
    return out


def _hit_from_sentence(
    text: str,
    spans: list[tuple[int, int]],
    idx: int,
    inline_refs: list[InlineRef],
    match_type: str = "regex",
) -> KeywordHit:
    """Build a KeywordHit for a sentence (current + next for refs)."""
    win_start = spans[idx][0]
    next_idx = idx + 1
    win_end = spans[next_idx][1] if next_idx < len(spans) else spans[idx][1]
    snippet = re.sub(r"\s+", " ", text[win_start:win_end]).strip()
    return KeywordHit(
        passage=snippet,
        match_start=spans[idx][0],
        match_end=spans[idx][1],
        ref_keys=_refs_in_window(inline_refs, win_start, win_end),
        match_type=match_type,
    )


# ---------------------------------------------------------------------------
# Semantic search (optional, requires sentence-transformers)
# ---------------------------------------------------------------------------

# Lazy-loaded model singleton, same pattern as _segmenter above.
_semantic_model = None
_semantic_model_name: str | None = None


def _get_semantic_model(model_name: str | None = None):
    """Load (or reuse) the sentence-transformer model.

    Raises ImportError with a helpful message if sentence-transformers
    is not installed.
    """
    global _semantic_model, _semantic_model_name
    name = model_name or SEMANTIC_DEFAULT_MODEL
    if _semantic_model is not None and _semantic_model_name == name:
        return _semantic_model
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        raise ImportError(
            "--semantic requires the sentence-transformers package.\n"
            "Install it with: pip install citracer[semantic]"
        ) from None
    logger.info("Loading semantic model '%s' (first call, may take a few seconds)...", name)
    _semantic_model = SentenceTransformer(name)
    _semantic_model_name = name
    return _semantic_model


def _semantic_search(
    text: str,
    keyword: str,
    spans: list[tuple[int, int]],
    inline_refs: list[InlineRef],
    exclude_sentences: set[int],
    model_name: str | None = None,
    threshold: float = SEMANTIC_SIMILARITY_THRESHOLD,
) -> list[KeywordHit]:
    """Find sentences semantically similar to ``keyword`` that the regex missed.

    Only sentences whose index is NOT in ``exclude_sentences`` are checked.
    Returns a list of KeywordHit with ``match_type="semantic"``.
    """
    if not spans:
        return []

    # Collect candidate sentences (those NOT already matched by regex)
    candidate_indices: list[int] = []
    candidate_texts: list[str] = []
    for i, (s, e) in enumerate(spans):
        if i in exclude_sentences:
            continue
        sent = text[s:e].strip()
        if len(sent) < 10:  # skip tiny fragments
            continue
        candidate_indices.append(i)
        candidate_texts.append(sent)

    if not candidate_texts:
        return []

    model = _get_semantic_model(model_name)

    # Batch encode: keyword + all candidate sentences in one call
    all_texts = [keyword] + candidate_texts
    embeddings = model.encode(all_texts, normalize_embeddings=True)
    kw_emb = embeddings[0]
    sent_embs = embeddings[1:]

    # Cosine similarity (embeddings are already L2-normalized)
    similarities = sent_embs @ kw_emb

    hits: list[KeywordHit] = []
    for j, sim in enumerate(similarities):
        if sim >= threshold:
            idx = candidate_indices[j]
            hit = _hit_from_sentence(text, spans, idx, inline_refs, match_type="semantic")
            hits.append(hit)
            logger.debug(
                "Semantic hit (sim=%.3f) in sentence %d: %s",
                sim, idx, hit.passage[:80],
            )

    return hits


# ---------------------------------------------------------------------------
# Main search function
# ---------------------------------------------------------------------------

def search(
    parsed: ParsedPaper,
    keyword: str,
    context_window: int | None = None,
    use_semantic: bool = False,
    semantic_model: str | None = None,
    semantic_threshold: float | None = None,
) -> list[KeywordHit]:
    """Find all matches of `keyword` and associate inline refs.

    If `context_window` is None (default), use sentence-based association:
    refs in the SAME sentence as the keyword OR the NEXT sentence count.
    If an int is provided, use the legacy ±N char window instead.

    If `use_semantic` is True, a second pass runs after the regex: sentences
    that the regex didn't match are checked with a sentence-transformer
    embedding model, and those above the similarity threshold are added.
    """
    pattern = build_pattern(keyword)
    text = parsed.text
    hits: list[KeywordHit] = []

    use_sentences = context_window is None
    spans = _sentence_spans(text) if use_sentences else []

    # Track which sentences the regex matched, for semantic dedup
    regex_matched_sentences: set[int] = set()

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
                regex_matched_sentences.add(idx)
        else:
            win_start = max(0, start - context_window)
            win_end = min(len(text), end + context_window)

        ref_keys = _refs_in_window(parsed.inline_refs, win_start, win_end)
        snippet = re.sub(r"\s+", " ", text[win_start:win_end]).strip()
        hits.append(
            KeywordHit(
                passage=snippet,
                match_start=start,
                match_end=end,
                ref_keys=ref_keys,
            )
        )

    n_regex = len(hits)
    logger.debug("Found %d regex hit(s) for '%s'", n_regex, keyword)

    # Phase 2: semantic boost (only in sentence mode)
    if use_semantic and use_sentences and spans:
        threshold = semantic_threshold if semantic_threshold is not None else SEMANTIC_SIMILARITY_THRESHOLD
        sem_hits = _semantic_search(
            text, keyword, spans, parsed.inline_refs,
            exclude_sentences=regex_matched_sentences,
            model_name=semantic_model,
            threshold=threshold,
        )
        hits.extend(sem_hits)
        if sem_hits:
            logger.debug(
                "Semantic boost: %d additional hit(s) for '%s'",
                len(sem_hits), keyword,
            )

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
