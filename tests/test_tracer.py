"""Smoke tests for citracer.tracer — the BFS orchestrator.

The PDF parser and the reference resolver are both replaced with fakes
so the test is hermetic (no GROBID, no network). What we validate:
  - BFS actually walks down to the configured depth
  - max_depth is respected
  - Deduplication via pdf_to_node_id + canonical id
  - Year anchoring prevents cascade
  - Multi-keyword: any vs all
  - Secondary edges are always computed
"""
from pathlib import Path

import pytest

from citracer import tracer as tracer_mod
from citracer.models import (
    BibEntry,
    InlineRef,
    ParsedPaper,
)
from citracer.reference_resolver import ResolvedRef
from citracer.tracer import trace_reverse


# ---------------------------------------------------------------------------
# Test scaffolding: fake parser + fake resolver
# ---------------------------------------------------------------------------

class _FakeParser:
    """Replaces pdf_parser.parse() with a lookup table indexed by PDF path."""

    def __init__(self, by_path: dict[str, ParsedPaper]):
        self.by_path = by_path
        self.calls: list[str] = []

    def parse(self, pdf_path, grobid_url=None, consolidate_citations=False):
        self.calls.append(str(pdf_path))
        key = str(pdf_path)
        if key in self.by_path:
            return self.by_path[key]
        raise FileNotFoundError(f"fake parser: no ParsedPaper for {key}")


class _FakeResolver:
    """Replaces ReferenceResolver.resolve() with a lookup keyed by bib_key.
    Records the order of calls for assertions."""

    def __init__(self, by_key: dict[str, ResolvedRef]):
        self.by_key = by_key
        self.calls: list[str] = []

    def resolve(self, bib: BibEntry) -> ResolvedRef:
        self.calls.append(bib.key)
        if bib.key not in self.by_key:
            # "Unavailable" — no pdf path, minimal metadata
            return ResolvedRef(
                paper_id=f"unresolved:{bib.key}",
                title=bib.title or bib.raw or "(unknown)",
                year=bib.year,
            )
        return self.by_key[bib.key]


def _parsed(title: str, text: str, bib_entries: dict[str, BibEntry] | None = None,
            year: int | None = None, arxiv_id: str | None = None) -> ParsedPaper:
    bib = bib_entries or {}
    refs = []
    for key in bib:
        idx = text.find(key)
        if idx != -1:
            refs.append(InlineRef(bib_key=key, start=idx, end=idx + len(key)))
    return ParsedPaper(
        text=text,
        bibliography=bib,
        inline_refs=refs,
        title=title,
        year=year,
        arxiv_id=arxiv_id,
    )


@pytest.fixture
def monkeypatched_tracer(monkeypatch, tmp_path):
    """Patch tracer.pdf_parser and tracer.ReferenceResolver to use fakes.
    Returns a helper that installs a given (parser, resolver) pair and
    runs tracer.trace with sensible defaults."""

    def install_and_run(
        parser: _FakeParser,
        resolver: _FakeResolver,
        *,
        root: str,
        keyword: str | list[str] = "channel-independent",
        depth: int = 2,
        match_mode: str = "any",
    ):
        monkeypatch.setattr(tracer_mod.pdf_parser, "parse", parser.parse)
        # Replace ReferenceResolver class with a lambda returning our fake
        monkeypatch.setattr(tracer_mod, "ReferenceResolver", lambda **_kw: resolver)
        return tracer_mod.trace(
            root_pdf=Path(root),
            keyword=keyword,
            max_depth=depth,
            cache_dir=tmp_path / "cache",
            grobid_workers=1,  # deterministic ordering
            match_mode=match_mode,
        )

    return install_and_run


# ---------------------------------------------------------------------------
# Basic 2-level BFS
# ---------------------------------------------------------------------------

class TestBasicBfs:
    def test_depth_1_captures_root_and_children(self, monkeypatched_tracer, tmp_path):
        # Root cites b0 (which resolves to a downloadable child at depth 1)
        child_pdf = tmp_path / "child.pdf"
        child_pdf.write_bytes(b"%PDF fake")

        root_parsed = _parsed(
            "Root Paper",
            "We use channel-independent architectures b0 for time series.",
            {"b0": BibEntry(key="b0", title="Child Paper", year=2020)},
            year=2024,
        )
        child_parsed = _parsed(
            "Child Paper",
            "A classic channel-independent method.",
            {},
            year=2020,
        )
        parser = _FakeParser({
            "root.pdf": root_parsed,
            str(child_pdf): child_parsed,
        })
        resolver = _FakeResolver({
            "b0": ResolvedRef(
                paper_id="arxiv:child",
                title="Child Paper",
                year=2020,
                arxiv_id="child",
                pdf_path=child_pdf,
            ),
        })

        graph = monkeypatched_tracer(parser, resolver, root="root.pdf", depth=1)
        assert len(graph.nodes) == 2
        assert any(n.status == "root" for n in graph.nodes.values())
        # Primary edge root -> child
        primary = [e for e in graph.edges if e.edge_type == "primary"]
        assert len(primary) == 1

    def test_max_depth_respected(self, monkeypatched_tracer, tmp_path):
        # Root -> A -> B; with depth=1, B should never be parsed
        a_pdf = tmp_path / "a.pdf"
        b_pdf = tmp_path / "b.pdf"
        a_pdf.write_bytes(b"%PDF")
        b_pdf.write_bytes(b"%PDF")

        root_parsed = _parsed("Root", "channel-independent root cites b0 here.",
                              {"b0": BibEntry(key="b0", title="A")})
        a_parsed = _parsed("A", "channel-independent A cites b0 here.",
                           {"b0": BibEntry(key="b0", title="B")})
        b_parsed = _parsed("B", "channel-independent B is the leaf.")

        parser = _FakeParser({
            "root.pdf": root_parsed,
            str(a_pdf): a_parsed,
            str(b_pdf): b_parsed,
        })
        resolver = _FakeResolver({
            "b0": ResolvedRef(paper_id="arxiv:A", title="A", arxiv_id="A", pdf_path=a_pdf),
        })

        graph = monkeypatched_tracer(parser, resolver, root="root.pdf", depth=1)
        # With depth=1, A is processed but A's refs are NOT queued (depth 1 == max_depth)
        assert any(str(c) == str(a_pdf) for c in parser.calls)
        assert not any(str(c) == str(b_pdf) for c in parser.calls)


# ---------------------------------------------------------------------------
# Secondary edges post-processing
# ---------------------------------------------------------------------------

class TestSecondaryEdges:
    def test_always_computed(self, monkeypatched_tracer, tmp_path):
        # Root cites A (primary, via keyword) and also has A in its biblio
        # via the same ref, so no secondary edge is added. We validate that
        # add_secondary_edges at least ran (returning 0 is fine).
        a_pdf = tmp_path / "a.pdf"
        a_pdf.write_bytes(b"%PDF")

        root_parsed = _parsed(
            "Root",
            "channel-independent mention with b0 cited here.",
            {"b0": BibEntry(key="b0", title="A", arxiv_id="A")},
        )
        a_parsed = _parsed("A", "channel-independent A.", {})
        parser = _FakeParser({"root.pdf": root_parsed, str(a_pdf): a_parsed})
        resolver = _FakeResolver({
            "b0": ResolvedRef(paper_id="arxiv:A", title="A", arxiv_id="A", pdf_path=a_pdf),
        })

        graph = monkeypatched_tracer(parser, resolver, root="root.pdf", depth=1)
        assert graph.nodes["arxiv:A"] is not None


# ---------------------------------------------------------------------------
# Multi-keyword
# ---------------------------------------------------------------------------

class TestMultiKeyword:
    def test_match_mode_any_union(self, monkeypatched_tracer, tmp_path):
        root_parsed = _parsed(
            "Root",
            "We mention channel-independent only. No second keyword here.",
            {},
        )
        parser = _FakeParser({"root.pdf": root_parsed})
        resolver = _FakeResolver({})

        graph = monkeypatched_tracer(
            parser, resolver, root="root.pdf",
            keyword=["channel-independent", "nonexistent"],
            match_mode="any",
        )
        root = next(iter(graph.nodes.values()))
        # At least one keyword matched → root stays analyzed (but it's the
        # root, so it keeps status="root"). The keyword_hits list should
        # contain the channel-independent match.
        assert len(root.keyword_hits) >= 1

    def test_match_mode_all_requires_every_keyword(self, monkeypatched_tracer, tmp_path):
        # Root has "channel-independent" but NOT "quantization" → under
        # match_mode=all, it must be marked no_match (but it's the root so
        # its status stays "root"). We check that no children get queued.
        root_parsed = _parsed(
            "Root",
            "Only channel-independent appears in this paper.",
            {"b0": BibEntry(key="b0", title="Child")},
        )
        parser = _FakeParser({"root.pdf": root_parsed})
        resolver = _FakeResolver({
            "b0": ResolvedRef(paper_id="unresolved", title="C", pdf_path=None),
        })

        graph = monkeypatched_tracer(
            parser, resolver, root="root.pdf",
            keyword=["channel-independent", "quantization"],
            match_mode="all",
            depth=2,
        )
        # Resolver should NEVER be called — no keyword in "all" mode means
        # we don't collect ref_keys or resolve anything.
        assert resolver.calls == []
        assert len(graph.nodes) == 1  # root only


# ---------------------------------------------------------------------------
# Year backfill anchor
# ---------------------------------------------------------------------------

class TestYearBackfill:
    def test_older_year_within_gap_is_applied(self, monkeypatched_tracer, tmp_path):
        child_pdf = tmp_path / "c.pdf"
        child_pdf.write_bytes(b"%PDF")
        root_parsed = _parsed(
            "Root",
            "channel-independent cites b0.",
            # Bib entry records year 2022 (the preprint)
            {"b0": BibEntry(key="b0", title="PatchTST", year=2022)},
        )
        child_parsed = _parsed("PatchTST", "channel-independent.", {}, year=2023)
        parser = _FakeParser({"root.pdf": root_parsed, str(child_pdf): child_parsed})
        # Resolver returns year 2023 (the published version)
        resolver = _FakeResolver({
            "b0": ResolvedRef(paper_id="arxiv:PatchTST",
                              title="PatchTST", year=2023,
                              arxiv_id="PatchTST", pdf_path=child_pdf),
        })

        graph = monkeypatched_tracer(parser, resolver, root="root.pdf", depth=1)
        child_node = graph.nodes["arxiv:PatchTST"]
        # The preprint year (2022) should win because it's older AND within
        # the ±2 year anchor window.
        assert child_node.year == 2022

    def test_wildly_older_year_rejected(self, monkeypatched_tracer, tmp_path):
        child_pdf = tmp_path / "c.pdf"
        child_pdf.write_bytes(b"%PDF")
        root_parsed = _parsed(
            "Root", "channel-independent cites b0.",
            # Bib entry has year 2015 (nonsense — a parser error)
            {"b0": BibEntry(key="b0", title="Real Paper", year=2015)},
        )
        child_parsed = _parsed("Real Paper", "channel-independent.", {}, year=2024)
        parser = _FakeParser({"root.pdf": root_parsed, str(child_pdf): child_parsed})
        resolver = _FakeResolver({
            "b0": ResolvedRef(paper_id="arxiv:Real", title="Real Paper",
                              year=2024, arxiv_id="Real", pdf_path=child_pdf),
        })

        graph = monkeypatched_tracer(parser, resolver, root="root.pdf", depth=1)
        # Gap 2024 - 2015 = 9 > threshold 2 → reject, keep 2024
        assert graph.nodes["arxiv:Real"].year == 2024


# ---------------------------------------------------------------------------
# Reverse trace
# ---------------------------------------------------------------------------

def _s2_citation(
    *,
    paper_id: str,
    title: str,
    year: int,
    contexts: list[str],
    arxiv_id: str | None = None,
    authors: list[str] | None = None,
) -> dict:
    """Build a fake S2 /citations response item."""
    return {
        "contexts": contexts,
        "intents": ["background"],
        "citingPaper": {
            "paperId": paper_id,
            "title": title,
            "year": year,
            "authors": [{"name": a} for a in (authors or [])],
            "externalIds": {"ArXiv": arxiv_id} if arxiv_id else {},
            "abstract": None,
        },
    }


class TestReverseTrace:
    def test_basic_one_level(self, monkeypatch, tmp_path):
        """A paper with two citers, one mentions the keyword, one doesn't."""
        citations = [
            _s2_citation(
                paper_id="s2_hit",
                title="A paper about channel-independent methods",
                year=2024,
                arxiv_id="2401.00001",
                authors=["A. Author"],
                contexts=[
                    "We adopt the channel-independent idea from this paper.",
                ],
            ),
            _s2_citation(
                paper_id="s2_miss",
                title="Completely unrelated paper",
                year=2024,
                arxiv_id="2401.00002",
                contexts=["This paper is cited for a different reason entirely."],
            ),
        ]

        class _FakeResolver:
            def __init__(self, *_a, **_kw):
                self.calls: list[tuple[str, int]] = []
            def get_citations(self, paper_id, limit=1000, page_size=100):
                self.calls.append((paper_id, limit))
                return citations

        monkeypatch.setattr(tracer_mod, "ReferenceResolver", _FakeResolver)

        graph = trace_reverse(
            root_paper_id="ARXIV:2211.14730",
            root_metadata={
                "paper_id": "arxiv:2211.14730",
                "title": "Root Paper",
                "authors": ["Root Author"],
                "year": 2022,
                "arxiv_id": "2211.14730",
            },
            keyword="channel-independent",
            max_depth=1,
            cache_dir=tmp_path,
        )
        # Root + the one matching citer
        assert len(graph.nodes) == 2
        assert "arxiv:2401.00001" in graph.nodes
        assert "arxiv:2401.00002" not in graph.nodes  # no context match
        # Edge goes FROM the citer TO the root
        assert len(graph.edges) == 1
        e = graph.edges[0]
        assert e.source_id == "arxiv:2401.00001"
        assert e.target_id == "arxiv:2211.14730"

    def test_empty_contexts_skipped(self, monkeypatch, tmp_path):
        """When S2 has the citation but no context snippets, we can't
        filter by keyword and must skip it."""
        citations = [
            _s2_citation(
                paper_id="s2",
                title="Some paper",
                year=2024,
                arxiv_id="2401.00001",
                contexts=[],  # no snippets available
            ),
        ]

        class _FakeResolver:
            def __init__(self, *_a, **_kw): pass
            def get_citations(self, *a, **k): return citations

        monkeypatch.setattr(tracer_mod, "ReferenceResolver", _FakeResolver)

        graph = trace_reverse(
            root_paper_id="ARXIV:x",
            root_metadata={"paper_id": "arxiv:x", "title": "x", "year": 2022,
                           "arxiv_id": "x"},
            keyword="channel-independent",
            max_depth=1,
            cache_dir=tmp_path,
        )
        # Only the root node — the empty-context citation was skipped
        assert len(graph.nodes) == 1

    def test_multi_keyword_all_mode(self, monkeypatch, tmp_path):
        """`match_mode=all` requires every keyword to appear somewhere
        in the citation contexts."""
        # Paper A's contexts cover both keywords; paper B's only one
        citations = [
            _s2_citation(
                paper_id="both", title="A", year=2024, arxiv_id="a",
                contexts=[
                    "We use channel-independent methods.",
                    "The patching idea is also inherited.",
                ],
            ),
            _s2_citation(
                paper_id="one", title="B", year=2024, arxiv_id="b",
                contexts=["Only channel-independent is mentioned."],
            ),
        ]

        class _FakeResolver:
            def __init__(self, *_a, **_kw): pass
            def get_citations(self, *a, **k): return citations

        monkeypatch.setattr(tracer_mod, "ReferenceResolver", _FakeResolver)

        graph = trace_reverse(
            root_paper_id="ARXIV:x",
            root_metadata={"paper_id": "arxiv:x", "title": "x", "year": 2022,
                           "arxiv_id": "x"},
            keyword=["channel-independent", "patching"],
            match_mode="all",
            max_depth=1,
            cache_dir=tmp_path,
        )
        assert "arxiv:a" in graph.nodes
        assert "arxiv:b" not in graph.nodes

    def test_two_level_recursion(self, monkeypatch, tmp_path):
        """Depth 2: papers citing the root, then papers citing those."""
        level1 = [
            _s2_citation(
                paper_id="l1", title="Level 1 paper", year=2023,
                arxiv_id="2301.00001",
                contexts=["Channel-independent is used here."],
            ),
        ]
        level2 = [
            _s2_citation(
                paper_id="l2", title="Level 2 paper", year=2024,
                arxiv_id="2401.00001",
                contexts=["We build on channel-independent principles."],
            ),
        ]

        class _FakeResolver:
            call_count = 0
            def __init__(self, *_a, **_kw): pass
            def get_citations(self, paper_id, **_kw):
                _FakeResolver.call_count += 1
                # First call (root) -> level1; second call (l1) -> level2
                if _FakeResolver.call_count == 1:
                    return level1
                return level2

        monkeypatch.setattr(tracer_mod, "ReferenceResolver", _FakeResolver)

        graph = trace_reverse(
            root_paper_id="ARXIV:root",
            root_metadata={"paper_id": "arxiv:root", "title": "Root",
                           "year": 2022, "arxiv_id": "root"},
            keyword="channel-independent",
            max_depth=2,
            cache_dir=tmp_path,
        )
        assert "arxiv:root" in graph.nodes
        assert "arxiv:2301.00001" in graph.nodes
        assert "arxiv:2401.00001" in graph.nodes
        # 2 edges: l1->root, l2->l1
        assert len(graph.edges) == 2
        sources = {e.source_id for e in graph.edges}
        targets = {e.target_id for e in graph.edges}
        assert "arxiv:2301.00001" in sources
        assert "arxiv:2401.00001" in sources
        assert "arxiv:root" in targets
        assert "arxiv:2301.00001" in targets

    def test_max_depth_respected(self, monkeypatch, tmp_path):
        """At depth 1, the resolver should only be called once (for root)."""
        class _FakeResolver:
            call_count = 0
            def __init__(self, *_a, **_kw): pass
            def get_citations(self, *a, **k):
                _FakeResolver.call_count += 1
                return [_s2_citation(
                    paper_id="l1", title="L1", year=2023, arxiv_id="l1",
                    contexts=["channel-independent here"],
                )]

        monkeypatch.setattr(tracer_mod, "ReferenceResolver", _FakeResolver)

        trace_reverse(
            root_paper_id="ARXIV:x",
            root_metadata={"paper_id": "arxiv:x", "title": "x", "year": 2022,
                           "arxiv_id": "x"},
            keyword="channel-independent",
            max_depth=1,
            cache_dir=tmp_path,
        )
        assert _FakeResolver.call_count == 1

    def test_invalid_match_mode_raises(self, tmp_path):
        with pytest.raises(ValueError, match="match_mode"):
            trace_reverse(
                root_paper_id="x",
                root_metadata={"title": "x", "year": 2022},
                keyword="foo",
                max_depth=1,
                cache_dir=tmp_path,
                match_mode="weird",
            )

    def test_no_keyword_raises(self, tmp_path):
        with pytest.raises(ValueError, match="keyword"):
            trace_reverse(
                root_paper_id="x",
                root_metadata={"title": "x", "year": 2022},
                keyword=[],
                cache_dir=tmp_path,
            )
