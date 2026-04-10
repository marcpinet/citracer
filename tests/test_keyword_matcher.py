"""Tests for citracer.keyword_matcher — pattern compilation +
sentence-based ref association + ref_keys utilities."""
import pytest

from citracer.keyword_matcher import (
    build_pattern,
    collect_ref_keys,
    context_for_ref,
    search,
)
from citracer.models import InlineRef, KeywordHit, ParsedPaper


def _parsed(text: str, inline_refs=None) -> ParsedPaper:
    """Tiny helper to build a ParsedPaper stub for matcher tests."""
    return ParsedPaper(
        text=text,
        bibliography={},
        inline_refs=list(inline_refs or []),
    )


# ---------------------------------------------------------------------------
# build_pattern
# ---------------------------------------------------------------------------

class TestBuildPattern:
    @pytest.mark.parametrize("variant", [
        "channel-independent",
        "channel independent",
        "channel-independence",
        "channel independence",
        "channel independently",
        "channelindependent",
        "channelindependence",
        "Channel-Independent",
        "CHANNEL-INDEPENDENCE",
    ])
    def test_morphological_variants(self, variant):
        p = build_pattern("channel-independent")
        assert p.search(variant) is not None

    def test_doesnt_match_substring(self):
        p = build_pattern("channel-independent")
        # preceded by a word char -> no match (negative lookbehind)
        assert p.search("multichannel-independent") is None

    def test_doesnt_match_unrelated(self):
        p = build_pattern("channel-independent")
        assert p.search("channel dependence") is None
        assert p.search("independent") is None

    def test_empty_keyword_raises(self):
        with pytest.raises(ValueError):
            build_pattern("")
        with pytest.raises(ValueError):
            build_pattern("   ")

    def test_single_token(self):
        p = build_pattern("transformer")
        assert p.search("transformer") is not None
        assert p.search("transformers") is not None
        assert p.search("transformer-based") is not None

    def test_short_token_not_stemmed(self):
        # Tokens of length <= KEYWORD_MORPHO_MIN_LEN (4) are taken as-is
        # (no `\w*` suffix). The negative lookbehind still excludes matches
        # where the token is preceded by another word char.
        p = build_pattern("foo")
        assert p.search("foo") is not None
        assert p.search("barfoo") is None     # blocked by lookbehind
        # NB: there is no lookahead guard, so "food" still matches — the
        # documented trade-off is accepted for short keywords.

    def test_multi_space_tokens(self):
        # "forecasting" -> stem "forecasti" -> matches any suffix after that
        p = build_pattern("long-term forecasting")
        assert p.search("long-term forecasting") is not None
        assert p.search("long term forecasting") is not None
        assert p.search("long-term forecastings") is not None  # morphological
        # "forecasted" drops below the "forecasti" stem and does not match.
        assert p.search("long-term forecasted") is None

    def test_flexible_hyphen_or_space(self):
        p = build_pattern("self-attention")
        assert p.search("self-attention") is not None
        assert p.search("self attention") is not None
        assert p.search("selfattention") is not None


# ---------------------------------------------------------------------------
# search — sentence-based mode
# ---------------------------------------------------------------------------

class TestSearchSentenceMode:
    def test_no_hits(self):
        p = _parsed("A paragraph about something unrelated.")
        hits = search(p, "channel-independent")
        assert hits == []

    def test_single_hit_no_refs(self):
        p = _parsed("We use channel-independent models here.")
        hits = search(p, "channel-independent")
        assert len(hits) == 1
        assert hits[0].ref_keys == []

    def test_ref_in_same_sentence(self):
        text = "We use channel-independent methods here. End."
        ref_pos = text.index("here")
        refs = [InlineRef(bib_key="b1", start=ref_pos, end=ref_pos + 4)]
        p = _parsed(text, refs)
        hits = search(p, "channel-independent")
        assert len(hits) == 1
        assert hits[0].ref_keys == ["b1"]

    def test_ref_in_next_sentence_is_captured(self):
        text = (
            "We propose a channel-independent architecture. "
            "It matches ref b2 performance."
        )
        ref_pos = text.index("b2")
        refs = [InlineRef(bib_key="b2", start=ref_pos, end=ref_pos + 2)]
        p = _parsed(text, refs)
        hits = search(p, "channel-independent")
        assert len(hits) == 1
        assert hits[0].ref_keys == ["b2"]

    def test_ref_in_previous_sentence_NOT_captured(self):
        text = (
            "Earlier work b_prev showed results. "
            "We use channel-independent models here."
        )
        ref_pos = text.index("b_prev")
        refs = [InlineRef(bib_key="b_prev", start=ref_pos, end=ref_pos + 6)]
        p = _parsed(text, refs)
        hits = search(p, "channel-independent")
        assert len(hits) == 1
        assert hits[0].ref_keys == []

    def test_multiple_refs_same_sentence(self):
        text = (
            "Channel-independent methods b1 and b2 both work well. "
            "Another sentence."
        )
        refs = [
            InlineRef(bib_key="b1", start=text.index("b1"), end=text.index("b1") + 2),
            InlineRef(bib_key="b2", start=text.index("b2"), end=text.index("b2") + 2),
        ]
        p = _parsed(text, refs)
        hits = search(p, "channel-independent")
        assert hits[0].ref_keys == ["b1", "b2"]

    def test_ref_keys_deduplicated_within_hit(self):
        text = "channel-independence cites b1 twice b1 and once b2."
        p1 = text.index("b1")
        p2 = text.index("b1", p1 + 1)
        pb2 = text.index("b2")
        refs = [
            InlineRef(bib_key="b1", start=p1, end=p1 + 2),
            InlineRef(bib_key="b1", start=p2, end=p2 + 2),
            InlineRef(bib_key="b2", start=pb2, end=pb2 + 2),
        ]
        p = _parsed(text, refs)
        hits = search(p, "channel-independent")
        assert hits[0].ref_keys == ["b1", "b2"]


# ---------------------------------------------------------------------------
# search — char-window legacy mode
# ---------------------------------------------------------------------------

class TestSearchCharWindow:
    def test_window_captures_previous_sentence(self):
        text = (
            "Earlier work b_prev showed results. "
            "We use channel-independent models here."
        )
        ref_pos = text.index("b_prev")
        refs = [InlineRef(bib_key="b_prev", start=ref_pos, end=ref_pos + 6)]
        p = _parsed(text, refs)
        hits = search(p, "channel-independent", context_window=300)
        assert hits[0].ref_keys == ["b_prev"]

    def test_tight_window_excludes_far_refs(self):
        text = "channel-independent" + (" word" * 200) + " b1."
        ref_pos = text.index("b1")
        refs = [InlineRef(bib_key="b1", start=ref_pos, end=ref_pos + 2)]
        p = _parsed(text, refs)
        hits = search(p, "channel-independent", context_window=50)
        assert hits[0].ref_keys == []


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class TestCollectRefKeys:
    def test_union_across_hits(self):
        hits = [
            KeywordHit(passage="", match_start=0, match_end=0, ref_keys=["a", "b"]),
            KeywordHit(passage="", match_start=0, match_end=0, ref_keys=["b", "c"]),
        ]
        assert collect_ref_keys(hits) == ["a", "b", "c"]

    def test_preserves_first_seen_order(self):
        hits = [
            KeywordHit(passage="", match_start=0, match_end=0, ref_keys=["z", "a"]),
            KeywordHit(passage="", match_start=0, match_end=0, ref_keys=["m"]),
        ]
        assert collect_ref_keys(hits) == ["z", "a", "m"]

    def test_empty(self):
        assert collect_ref_keys([]) == []


class TestContextForRef:
    def test_returns_first_matching_passage(self):
        hits = [
            KeywordHit(passage="first", match_start=0, match_end=0, ref_keys=["a"]),
            KeywordHit(passage="second", match_start=0, match_end=0, ref_keys=["a", "b"]),
        ]
        assert context_for_ref(hits, "a") == "first"
        assert context_for_ref(hits, "b") == "second"

    def test_not_found(self):
        hits = [KeywordHit(passage="x", match_start=0, match_end=0, ref_keys=["a"])]
        assert context_for_ref(hits, "missing") == ""


# ---------------------------------------------------------------------------
# Semantic matching (mocked — no real model in CI)
# ---------------------------------------------------------------------------

import numpy as np
from unittest.mock import patch, MagicMock
from citracer import keyword_matcher as km_module


class _FakeModel:
    """Stand-in for SentenceTransformer that returns deterministic embeddings.

    Uses a 2D embedding trick: keyword is [1, 0]. A sentence with target
    cosine similarity `s` gets embedding [s, sqrt(1-s^2)], which after
    L2 normalization dot-products to exactly `s` with [1, 0].
    """

    def __init__(self, similarity_map: dict[str, float] | None = None):
        self._sim_map = similarity_map or {}

    def encode(self, texts: list[str], normalize_embeddings: bool = True):
        n = len(texts)
        embs = np.zeros((n, 2), dtype=np.float32)
        # First text is always the keyword → [1, 0]
        embs[0] = [1.0, 0.0]
        for i, t in enumerate(texts):
            if i == 0:
                continue
            sim = 0.0  # default: orthogonal (similarity = 0)
            for frag, s in self._sim_map.items():
                if frag.lower() in t.lower():
                    sim = s
                    break
            embs[i] = [sim, np.sqrt(max(0, 1 - sim * sim))]
        return embs


class TestSemanticSearch:
    def _install_fake_model(self, sim_map):
        """Install a fake model into the module-level cache."""
        from citracer.constants import SEMANTIC_DEFAULT_MODEL
        km_module._semantic_model = _FakeModel(sim_map)
        km_module._semantic_model_name = SEMANTIC_DEFAULT_MODEL

    def teardown_method(self):
        km_module._semantic_model = None
        km_module._semantic_model_name = None

    def test_semantic_disabled_by_default(self):
        """Without use_semantic=True, only regex runs."""
        text = "We process each variate independently. Next sentence."
        p = _parsed(text)
        hits = search(p, "channel-independent")
        assert len(hits) == 0  # "variate independently" doesn't match regex
        assert all(h.match_type == "regex" for h in hits)

    def test_semantic_finds_conceptual_match(self):
        """Semantic mode catches a sentence the regex would miss."""
        text = "We process each variate independently. Another sentence here."
        p = _parsed(text)
        self._install_fake_model({"variate independently": 0.8})
        hits = search(p, "channel-independent", use_semantic=True, semantic_threshold=0.5)
        assert len(hits) == 1
        assert hits[0].match_type == "semantic"
        assert "variate independently" in hits[0].passage

    def test_semantic_dedup_with_regex(self):
        """A sentence matched by regex is NOT re-matched by semantic."""
        text = "We use channel-independent models here. Another sentence."
        p = _parsed(text)
        self._install_fake_model({"channel-independent": 0.95})
        hits = search(p, "channel-independent", use_semantic=True, semantic_threshold=0.3)
        # Only 1 hit (regex), not 2
        assert len(hits) == 1
        assert hits[0].match_type == "regex"

    def test_semantic_ref_association(self):
        """Semantic hit includes refs from same + next sentence."""
        text = "Each variate is processed separately b1. The next sentence b2. Third."
        refs = [
            InlineRef(bib_key="b1", start=text.index("b1"), end=text.index("b1") + 2),
            InlineRef(bib_key="b2", start=text.index("b2"), end=text.index("b2") + 2),
        ]
        p = _parsed(text, refs)
        self._install_fake_model({"variate is processed separately": 0.7})
        hits = search(p, "channel-independent", use_semantic=True, semantic_threshold=0.5)
        assert len(hits) == 1
        # b1 is in same sentence, b2 is in next sentence → both captured
        assert "b1" in hits[0].ref_keys
        assert "b2" in hits[0].ref_keys

    def test_semantic_below_threshold_rejected(self):
        """Low-similarity sentences are not included."""
        text = "This paper studies weather forecasting. Another sentence."
        p = _parsed(text)
        self._install_fake_model({"weather forecasting": 0.2})
        hits = search(p, "channel-independent", use_semantic=True, semantic_threshold=0.5)
        assert len(hits) == 0

    def test_semantic_not_available_error(self):
        """Clear error when sentence-transformers is not installed."""
        km_module._semantic_model = None
        km_module._semantic_model_name = None
        with patch.dict("sys.modules", {"sentence_transformers": None}):
            text = "Some text here. Another sentence."
            p = _parsed(text)
            with pytest.raises(ImportError, match="pip install citracer"):
                search(p, "test", use_semantic=True)

    def test_semantic_skipped_in_char_window_mode(self):
        """Semantic mode only works in sentence mode, not char-window mode."""
        text = "We process each variate independently. Another sentence."
        p = _parsed(text)
        self._install_fake_model({"variate independently": 0.8})
        # context_window=300 forces char-window mode → semantic skipped
        hits = search(p, "channel-independent", context_window=300,
                      use_semantic=True, semantic_threshold=0.5)
        assert len(hits) == 0  # regex doesn't match, semantic skipped
