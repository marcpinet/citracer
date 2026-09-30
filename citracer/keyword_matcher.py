"""Keyword matching with morphological flexibility + ref association.

Two modes for associating refs to a keyword hit:
  - sentence mode (default): refs in the SAME sentence as the keyword OR
    in the NEXT sentence. Uses pysbd for boundary detection. Precise.
  - char-window mode (legacy / fallback): refs within ±N characters of the
    keyword. More permissive, used as a fallback when sentence detection
    misbehaves.

Sentence segmentation (pysbd, pure Python and slow) is done lazily, one
paragraph at a time, and only for paragraphs that actually contain a hit —
papers without a match never get segmented at all.

An optional semantic mode (``--semantic``) adds a second pass using a
sentence-transformer model. The regex pass runs first (fast, precise);
the semantic pass then scans sentences the regex missed and keeps those
whose embedding is close enough to the keyword. Results are unioned.
"""
from __future__ import annotations
import hashlib
import logging
import re
import threading
from bisect import bisect_right
from pathlib import Path

import pysbd

from .constants import (
    KEYWORD_MORPHO_MIN_LEN,
    SEMANTIC_DEFAULT_MODEL,
    SEMANTIC_SIMILARITY_THRESHOLD,
)
from .models import KeywordHit, ParsedPaper

logger = logging.getLogger(__name__)

_segmenter: pysbd.Segmenter | None = None
_segmenter_lock = threading.Lock()


def _get_segmenter() -> pysbd.Segmenter:
    global _segmenter
    with _segmenter_lock:
        if _segmenter is None:
            _segmenter = pysbd.Segmenter(language="en", clean=False, char_span=True)
        return _segmenter


def _segment(text: str) -> list[tuple[int, int]]:
    """Sentence spans of ``text`` (offsets relative to ``text``), sorted and
    de-duplicated (pysbd's char_span mode can emit the same span twice)."""
    try:
        out = _get_segmenter().segment(text)
    except Exception as e:
        logger.warning("pysbd failed (%s); falling back to single span", e)
        return [(0, len(text))]
    spans = sorted({(s.start, s.end) for s in out if s.end > s.start})
    deduped: list[tuple[int, int]] = []
    for a, b in spans:
        if deduped and a == deduped[-1][0]:
            deduped[-1] = (a, max(b, deduped[-1][1]))
            continue
        deduped.append((a, b))
    return deduped or [(0, len(text))]


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end) char offsets for every sentence in `text`."""
    return SentenceIndex(text).all_spans()


class SentenceIndex:
    """Lazily segmented view of a text, one paragraph (line) at a time.

    Paragraphs are the ``\\n``-separated blocks the PDF parser emits; they
    are always sentence boundaries, so segmenting them independently gives
    the same sentences as segmenting the whole text, at a fraction of the
    cost when only a few paragraphs contain hits.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self._paras = [(m.start(), m.end()) for m in re.finditer(r"[^\n]+", text)]
        self._para_starts = [a for a, _ in self._paras]
        self._spans: dict[int, list[tuple[int, int]]] = {}

    def _para_spans(self, pi: int) -> list[tuple[int, int]]:
        spans = self._spans.get(pi)
        if spans is None:
            a, b = self._paras[pi]
            chunk = self.text[a:b]
            if not chunk.strip():
                spans = []
            else:
                spans = [(a + s, a + e) for s, e in _segment(chunk)]
            self._spans[pi] = spans
        return spans

    def locate(self, pos: int) -> tuple[int, int] | None:
        """Return (paragraph index, sentence index) of the sentence holding
        ``pos``. A position in inter-sentence whitespace maps to the
        preceding sentence."""
        if not self._paras:
            return None
        pi = max(0, bisect_right(self._para_starts, pos) - 1)
        # Walk back over blank paragraphs.
        while pi > 0 and not self._para_spans(pi):
            pi -= 1
        spans = self._para_spans(pi)
        if not spans:
            return None
        si = max(0, bisect_right([s for s, _ in spans], pos) - 1)
        return pi, si

    def span(self, loc: tuple[int, int]) -> tuple[int, int]:
        pi, si = loc
        return self._para_spans(pi)[si]

    def next_span(self, loc: tuple[int, int]) -> tuple[int, int] | None:
        """The sentence after ``loc``, possibly in a following paragraph."""
        pi, si = loc
        spans = self._para_spans(pi)
        if si + 1 < len(spans):
            return spans[si + 1]
        for pj in range(pi + 1, len(self._paras)):
            nxt = self._para_spans(pj)
            if nxt:
                return nxt[0]
        return None

    def all_spans(self) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        for pi in range(len(self._paras)):
            out.extend(self._para_spans(pi))
        return out


class MatchContext:
    """Per-paper precomputed state shared by every keyword search."""

    def __init__(self, parsed: ParsedPaper) -> None:
        self.parsed = parsed
        self.text = parsed.text
        self.sentences = SentenceIndex(parsed.text)
        self.refs = sorted(parsed.inline_refs, key=lambda r: r.start)
        self._ref_starts = [r.start for r in self.refs]

    def refs_in_window(self, win_start: int, win_end: int) -> list[str]:
        """Deduplicated bib_keys for refs whose span falls inside the window."""
        seen: set[str] = set()
        out: list[str] = []
        i = bisect_right(self._ref_starts, win_start - 1)
        while i < len(self.refs) and self.refs[i].start < win_end:
            ref = self.refs[i]
            if ref.end <= win_end and ref.bib_key not in seen:
                out.append(ref.bib_key)
                seen.add(ref.bib_key)
            i += 1
        return out


# ---------------------------------------------------------------------------
# Pattern compilation
# ---------------------------------------------------------------------------

_VOWELS = set("aeiou")


def _is_acronym(tok: str) -> bool:
    """GAN, LSTM, BERT, PatchTST: two or more capitals -> case-sensitive."""
    return sum(1 for c in tok if c.isupper()) >= 2


def _bases(t: str) -> dict[str, bool]:
    """Plausible base forms of a lowercase token (undo inflections), mapped
    to whether verb inflections may be built on them. A base obtained by
    removing a plural is a noun: "means" -> "mean" must not give "meaning"."""
    bases = {t: True}
    if t.endswith("ing") and len(t) > 5:
        r = t[:-3]
        bases.update({r: True, r + "e": True})
        if len(r) > 2 and r[-1] == r[-2]:
            bases[r[:-1]] = True  # modelling -> model
    if t.endswith("ed") and len(t) > 4:
        r = t[:-2]
        bases.update({r: True, r + "e": True})
        if len(r) > 2 and r[-1] == r[-2]:
            bases[r[:-1]] = True
    if t.endswith("ies") and len(t) > 4:
        bases.setdefault(t[:-3] + "y", False)
    elif t.endswith("ses") and len(t) > 5:
        bases.setdefault(t[:-2] + "is", False)  # analyses -> analysis
        bases.setdefault(t[:-1], False)          # processes -> processe(s)
        bases.setdefault(t[:-2], False)          # processes -> process
    elif t.endswith("es") and len(t) > 4 and t[-3] in "sxzh":
        bases.setdefault(t[:-2], False)
    elif t.endswith("s") and not t.endswith("ss") and len(t) > 4:
        bases.setdefault(t[:-1], False)
    return bases


#: Endings of nouns / already-inflected words that don't take verb
#: inflections ("attentioned", "forecastinging" would be noise).
_NO_VERB_FORMS = ("ion", "ity", "ness", "ment", "ism", "ics", "ing", "ed",
                  "er", "or", "ure", "ance", "ence", "ency", "ent", "ant",
                  "ly", "al", "is")


def _forms(b: str, verbs: bool = True) -> set[str]:
    """Inflectional (and a few meaning-preserving derivational) forms.
    ``verbs=False`` restricts ``b`` to noun forms."""
    forms = {b, b + "s"}
    if b.endswith("is") and len(b) > 4:
        forms.add(b[:-2] + "es")  # analysis -> analyses, hypothesis -> hypotheses
    elif b.endswith(("s", "x", "z", "ch", "sh")):
        forms.add(b + "es")
    if b.endswith("y") and len(b) > 1 and b[-2] not in _VOWELS:
        forms |= {b[:-1] + "ies", b[:-1] + "ied"}
    if verbs and not b.endswith(_NO_VERB_FORMS):
        if b.endswith("e"):
            forms |= {b + "d", b[:-1] + "ing", b[:-1] + "ings"}
        else:
            forms |= {b + "ed", b + "ing", b + "ings"}
            # Consonant doubling only after a single vowel: model -> modelled
            if (len(b) > 2 and b[-1] not in _VOWELS and b[-1] not in "wxy"
                    and b[-2] in _VOWELS and b[-3] not in _VOWELS):
                forms |= {b + b[-1] + "ed", b + b[-1] + "ing"}
    # adjective <-> noun pairs that name the same concept
    for adj, nouns in (("ent", ("ence", "ences", "ency", "encies", "ently", "ents")),
                       ("ant", ("ance", "ances", "antly", "ants"))):
        noun = nouns[0]
        if b.endswith(adj):
            forms |= {b[:-len(adj)] + n for n in nouns}
        elif b.endswith(noun):
            stem = b[:-len(noun)]
            forms |= {stem + adj, stem + adj + "ly"}
    if b.endswith("ion"):
        forms |= {b + "al", b + "ally"}  # attention -> attentional
    if b.endswith("ic"):
        forms |= {b + "al", b + "ally"}
    if b.endswith("ent") or b.endswith("al"):
        forms.add(b + "ly")  # independent -> independently
    if b.endswith("ize") or b.endswith("ise"):
        forms |= {b[:-1] + "ation", b[:-1] + "ations"}
    if b.endswith("ization") or b.endswith("isation"):
        stem = b[:-5]
        forms |= {stem + "e", stem + "es", stem + "ed", stem + "ing"}
    # British / American spelling
    suffix = r"(?=(?:e|ed|es|ing|ation|ations|ational)$)"
    forms |= {re.sub(r"iz" + suffix, "is", f) for f in forms}
    forms |= {re.sub(r"is" + suffix, "iz", f) for f in forms}
    return forms


def _word_forms(tok: str) -> list[str]:
    """All surface forms matched for the LAST token of a keyword."""
    t = tok.lower()
    if len(t) <= KEYWORD_MORPHO_MIN_LEN:
        forms = {t, t + "s"}
    else:
        forms = set()
        for b, verbs in _bases(t).items():
            forms |= _forms(b, verbs)
    return sorted(forms, key=lambda f: (-len(f), f))


def _ci(s: str) -> str:
    """Case-insensitive regex for ``s`` without relying on the re.I flag
    (needed when the pattern mixes case-sensitive acronyms)."""
    out = []
    for ch in s:
        lo, up = ch.lower(), ch.upper()
        out.append(f"[{re.escape(lo)}{re.escape(up)}]" if lo != up else re.escape(ch))
    return "".join(out)


def _alternation(forms: list[str], lit) -> str:
    """Compact regex matching any of ``forms``: common prefix + suffixes."""
    prefix = forms[0]
    for f in forms[1:]:
        while not f.startswith(prefix):
            prefix = prefix[:-1]
    suffixes = sorted({f[len(prefix):] for f in forms}, key=lambda s: (-len(s), s))
    alts = "|".join(lit(s) for s in suffixes if s)
    if not alts:
        return lit(prefix)
    opt = "?" if "" in suffixes else ""
    return f"{lit(prefix)}(?:{alts}){opt}"


#: Token separator: whitespace, "-" or a typographic hyphen/dash (U+2010 to
#: U+2015, minus sign). Valid in both Python and JavaScript regexes.
_SEP = r"[\s\-‐-―−]"


def build_pattern(keyword: str) -> re.Pattern[str]:
    """Build a flexible regex from a keyword.

    - Tokens separated by whitespace or hyphens (ASCII or typographic) may
      be joined by a space, any hyphen, or nothing.
    - The last token matches its inflected forms ("independent" also
      matches "independence", "independently"; "model" matches "models",
      "modelling" but not "modern"; "transformer" doesn't match
      "transformation").
    - Every match must end on a word boundary.
    - Acronym tokens (two or more capitals: GAN, LSTM) are matched
      case-sensitively so "GAN" doesn't match "gan" inside prose, with an
      optional plural "s". Everything else is case-insensitive.

    The compiled pattern never uses inline flags, so ``pattern.pattern`` is
    also a valid JavaScript regex (use ``js_flags`` for the flags).
    """
    tokens = [t for t in re.split(_SEP + "+", keyword.strip()) if t]
    if not tokens:
        raise ValueError("Empty keyword")

    has_acronym = any(_is_acronym(t) for t in tokens)
    lit = _ci if has_acronym else re.escape

    parts: list[str] = []
    for i, tok in enumerate(tokens):
        is_last = i == len(tokens) - 1
        if _is_acronym(tok):
            parts.append(re.escape(tok) + ("s?" if is_last else ""))
        elif is_last:
            parts.append(_alternation(_word_forms(tok), lit))
        else:
            parts.append(lit(tok))

    pattern_str = r"(?<!\w)" + (_SEP + "?").join(parts) + r"(?!\w)"
    return re.compile(pattern_str, 0 if has_acronym else re.IGNORECASE)


def js_flags(pattern: re.Pattern[str]) -> str:
    """JavaScript RegExp flags equivalent to ``pattern``'s Python flags."""
    return "gi" if pattern.flags & re.IGNORECASE else "g"


# ---------------------------------------------------------------------------
# Hit construction
# ---------------------------------------------------------------------------

def _sentence_window(ctx: MatchContext, loc: tuple[int, int]) -> tuple[int, int]:
    """Current sentence + next sentence (refs there are associated too)."""
    start, end = ctx.sentences.span(loc)
    nxt = ctx.sentences.next_span(loc)
    return start, (nxt[1] if nxt else end)


def _hit_from_window(
    ctx: MatchContext,
    win_start: int,
    win_end: int,
    match_start: int,
    match_end: int,
    match_type: str = "regex",
) -> KeywordHit:
    snippet = re.sub(r"\s+", " ", ctx.text[win_start:win_end]).strip()
    return KeywordHit(
        passage=snippet,
        match_start=match_start,
        match_end=match_end,
        ref_keys=ctx.refs_in_window(win_start, win_end),
        match_type=match_type,
    )


# ---------------------------------------------------------------------------
# Semantic search (optional, requires sentence-transformers)
# ---------------------------------------------------------------------------

# Lazy-loaded model singleton, same pattern as _segmenter above.
_semantic_model = None
_semantic_model_name: str | None = None
# Loading and encoding are serialized: torch already parallelizes a single
# encode call, and concurrent calls from parse workers would oversubscribe.
_semantic_lock = threading.RLock()
# Keyword embeddings are computed once per run, not once per paper.
_keyword_embeddings: dict[tuple[str, str], object] = {}


def _get_semantic_model(model_name: str | None = None):
    """Load (or reuse) the sentence-transformer model.

    Raises ImportError with a helpful message if sentence-transformers
    is not installed.
    """
    global _semantic_model, _semantic_model_name
    name = model_name or SEMANTIC_DEFAULT_MODEL
    with _semantic_lock:
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
        _keyword_embeddings.clear()
        return _semantic_model


def _encode(model, texts: list[str]):
    with _semantic_lock:
        return model.encode(texts, normalize_embeddings=True)


def _keyword_embedding(model, model_name: str, keyword: str):
    key = (model_name, keyword)
    emb = _keyword_embeddings.get(key)
    if emb is None:
        emb = _encode(model, [keyword])[0]
        _keyword_embeddings[key] = emb
    return emb


def _sentence_embeddings(model, model_name: str, sentences: list[str], cache_dir: Path | None):
    """Embeddings of ``sentences``, cached on disk by content + model."""
    cache_path = None
    if cache_dir is not None:
        h = hashlib.sha256(model_name.encode("utf-8"))
        for s in sentences:
            h.update(b"\x00")
            h.update(s.encode("utf-8"))
        cache_path = Path(cache_dir) / "embeddings" / f"{h.hexdigest()[:32]}.npy"
        if cache_path.exists():
            try:
                import numpy as np
                embs = np.load(cache_path).astype("float32")
                if len(embs) == len(sentences):
                    return embs
            except Exception as e:
                logger.debug("unreadable embedding cache %s: %s", cache_path.name, e)
    embs = _encode(model, sentences)
    if cache_path is not None:
        try:
            import numpy as np
            from .http_client import write_atomic
            import io
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            buf = io.BytesIO()
            np.save(buf, np.asarray(embs, dtype="float16"))
            write_atomic(cache_path, buf.getvalue())
        except Exception as e:
            logger.debug("could not cache embeddings: %s", e)
    return embs


def _semantic_hits(
    ctx: MatchContext,
    keywords: list[str],
    exclude: dict[str, set[int]],
    model_name: str | None,
    threshold: float,
    cache_dir: Path | None,
) -> dict[str, list[KeywordHit]]:
    """Semantic pass for every keyword at once: sentences are segmented and
    encoded a single time per paper. Sentences whose start offset is in
    ``exclude[kw]`` (already matched by the regex) are skipped for that
    keyword."""
    spans = ctx.sentences.all_spans()
    cand: list[tuple[int, int]] = []
    texts: list[str] = []
    for s, e in spans:
        sent = ctx.text[s:e].strip()
        if len(sent) < 10:  # skip tiny fragments
            continue
        cand.append((s, e))
        texts.append(sent)
    out: dict[str, list[KeywordHit]] = {kw: [] for kw in keywords}
    if not texts:
        return out

    name = model_name or SEMANTIC_DEFAULT_MODEL
    model = _get_semantic_model(name)
    sent_embs = _sentence_embeddings(model, name, texts, cache_dir)
    next_end = {spans[i][0]: spans[i + 1][1] for i in range(len(spans) - 1)}

    for kw in keywords:
        sims = sent_embs @ _keyword_embedding(model, name, kw)
        skip = exclude.get(kw, set())
        for (s, e), sim in zip(cand, sims):
            if sim < threshold or s in skip:
                continue
            hit = _hit_from_window(ctx, s, next_end.get(s, e), s, e, match_type="semantic")
            hit.semantic_score = float(sim)
            out[kw].append(hit)
            logger.debug("Semantic hit (sim=%.3f): %s", sim, hit.passage[:80])
    return out


# ---------------------------------------------------------------------------
# Main search functions
# ---------------------------------------------------------------------------

def _regex_hits(
    ctx: MatchContext,
    keyword: str,
    context_window: int | None,
) -> tuple[list[KeywordHit], set[int]]:
    """Regex pass for one keyword. Returns the hits and the start offsets
    of the sentences they fall in (sentence mode only)."""
    pattern = build_pattern(keyword)
    hits: list[KeywordHit] = []
    matched_sentences: set[int] = set()
    seen_passages: set[str] = set()
    text = ctx.text

    for m in pattern.finditer(text):
        start, end = m.start(), m.end()
        if context_window is None:
            loc = ctx.sentences.locate(start)
            if loc is None:
                win_start, win_end = start, end
            else:
                sent_start = ctx.sentences.span(loc)[0]
                # One hit per sentence: a second occurrence in the same
                # sentence would only duplicate the passage.
                if sent_start in matched_sentences:
                    continue
                matched_sentences.add(sent_start)
                win_start, win_end = _sentence_window(ctx, loc)
        else:
            win_start = max(0, start - context_window)
            win_end = min(len(text), end + context_window)

        hit = _hit_from_window(ctx, win_start, win_end, start, end)
        if hit.passage in seen_passages:
            continue
        seen_passages.add(hit.passage)
        hits.append(hit)

    logger.debug("Found %d regex hit(s) for '%s'", len(hits), keyword)
    return hits, matched_sentences


def search_all(
    parsed: ParsedPaper,
    keywords: list[str],
    context_window: int | None = None,
    use_semantic: bool = False,
    semantic_model: str | None = None,
    semantic_threshold: float | None = None,
    cache_dir: str | Path | None = None,
    ctx: MatchContext | None = None,
) -> dict[str, list[KeywordHit]]:
    """Search every keyword in ``parsed``; returns hits per keyword, each
    tagged with its keyword.

    If `context_window` is None (default), use sentence-based association:
    refs in the SAME sentence as the keyword OR the NEXT sentence count.
    If an int is provided, use the legacy ±N char window instead.

    If `use_semantic` is True, a second pass runs after the regex: sentences
    that the regex didn't match are checked with a sentence-transformer
    embedding model, and those above the similarity threshold are added.
    ``cache_dir`` enables the on-disk sentence-embedding cache.
    """
    ctx = ctx or MatchContext(parsed)
    by_kw: dict[str, list[KeywordHit]] = {}
    matched: dict[str, set[int]] = {}
    for kw in keywords:
        by_kw[kw], matched[kw] = _regex_hits(ctx, kw, context_window)

    # Phase 2: semantic boost (only in sentence mode)
    if use_semantic and context_window is None and ctx.text.strip():
        threshold = (semantic_threshold if semantic_threshold is not None
                     else SEMANTIC_SIMILARITY_THRESHOLD)
        sem = _semantic_hits(
            ctx, keywords, matched, semantic_model, threshold,
            Path(cache_dir) if cache_dir is not None else None,
        )
        for kw, hits in sem.items():
            by_kw[kw].extend(hits)
            if hits:
                logger.debug("Semantic boost: %d additional hit(s) for '%s'", len(hits), kw)

    for kw, hits in by_kw.items():
        for h in hits:
            h.keyword = kw
    return by_kw


def search(
    parsed: ParsedPaper,
    keyword: str,
    context_window: int | None = None,
    use_semantic: bool = False,
    semantic_model: str | None = None,
    semantic_threshold: float | None = None,
    ctx: MatchContext | None = None,
) -> list[KeywordHit]:
    """Find all matches of `keyword` and associate inline refs.
    Single-keyword convenience wrapper around :func:`search_all`."""
    return search_all(
        parsed, [keyword], context_window=context_window,
        use_semantic=use_semantic, semantic_model=semantic_model,
        semantic_threshold=semantic_threshold, ctx=ctx,
    )[keyword]


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
