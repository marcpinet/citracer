"""Tests for citracer.cross_citation — secondary edge discovery
and anchored year backfill."""
from citracer.cross_citation import (
    _better_year,
    _find_matching_bib,
    add_secondary_edges,
)
from citracer.models import BibEntry, CitationEdge, PaperNode, TracerGraph


def _node(
    paper_id: str,
    title: str = "Untitled",
    year: int | None = None,
    arxiv_id: str | None = None,
    doi: str | None = None,
    bibliography: dict[str, BibEntry] | None = None,
) -> PaperNode:
    return PaperNode(
        paper_id=paper_id,
        title=title,
        year=year,
        original_year=year,
        arxiv_id=arxiv_id,
        doi=doi,
        bibliography=bibliography or {},
    )


# ---------------------------------------------------------------------------
# _find_matching_bib
# ---------------------------------------------------------------------------

class TestFindMatchingBib:
    def test_match_by_arxiv_id(self):
        bib = {
            "b0": BibEntry(key="b0", title="Wrong title", arxiv_id="2211.14730"),
        }
        target = _node("x", title="A time series is worth 64 words", arxiv_id="2211.14730")
        assert _find_matching_bib(bib, target) is not None

    def test_match_by_doi(self):
        bib = {"b0": BibEntry(key="b0", title="Wrong title", doi="10.1/abc")}
        target = _node("x", title="...", doi="10.1/abc")
        assert _find_matching_bib(bib, target) is not None

    def test_doi_normalized_before_comparison(self):
        bib = {"b0": BibEntry(key="b0", doi="https://doi.org/10.1/ABC")}
        target = _node("x", doi="10.1/abc")
        assert _find_matching_bib(bib, target) is not None

    def test_fuzzy_title_match(self):
        bib = {
            "b0": BibEntry(
                key="b0",
                title="A Time Series Is Worth 64 Words: Long-term Forecasting With Transformers",
            ),
        }
        target = _node(
            "x",
            title="A time series is worth 64 words: Long-term forecasting with transformers",
        )
        assert _find_matching_bib(bib, target) is not None

    def test_no_match(self):
        bib = {"b0": BibEntry(key="b0", title="Completely unrelated paper on cryptography")}
        target = _node("x", title="Channel independent time series forecasting")
        assert _find_matching_bib(bib, target) is None

    def test_short_title_rejected(self):
        # Too short to fuzzy-match reliably
        bib = {"b0": BibEntry(key="b0", title="ABC")}
        target = _node("x", title="ABC")
        assert _find_matching_bib(bib, target) is None


# ---------------------------------------------------------------------------
# _better_year — year backfill with anchor + gap
# ---------------------------------------------------------------------------

class TestBetterYear:
    def test_none_candidate_keeps_current(self):
        assert _better_year(anchor=2023, current=2023, candidate=None) == 2023

    def test_accepts_older_within_gap(self):
        # anchor=2023, candidate=2022 is OK (gap 1, within _YEAR_GAP_THRESHOLD=2)
        assert _better_year(anchor=2023, current=2023, candidate=2022) == 2022

    def test_rejects_older_outside_gap(self):
        # anchor=2023, candidate=2020 -> gap 3 > threshold, reject
        assert _better_year(anchor=2023, current=2023, candidate=2020) == 2023

    def test_rejects_newer_candidate(self):
        # We only accept older years
        assert _better_year(anchor=2022, current=2022, candidate=2025) == 2022

    def test_rejects_garbage_year(self):
        # pre-1970 is nonsense for modern papers
        assert _better_year(anchor=2023, current=2023, candidate=1950) == 2023

    def test_prevents_cascading(self):
        # The whole point of the anchor: second update compares against the
        # original year, NOT the already-updated current, so a sequence of
        # small moves can't drift arbitrarily far.
        y = _better_year(anchor=2023, current=2023, candidate=2022)
        assert y == 2022
        # Now try to move further: anchor still 2023, gap 2023-2021=2 OK
        y = _better_year(anchor=2023, current=y, candidate=2021)
        assert y == 2021
        # But 2020 is gap 3 from the anchor -> rejected, stays at 2021
        y = _better_year(anchor=2023, current=y, candidate=2020)
        assert y == 2021

    def test_no_anchor_permissive(self):
        # When anchor is None we just pick the older plausible value
        assert _better_year(anchor=None, current=None, candidate=2015) == 2015
        assert _better_year(anchor=None, current=2020, candidate=2015) == 2015


# ---------------------------------------------------------------------------
# add_secondary_edges — end-to-end on a small hand-built graph
# ---------------------------------------------------------------------------

class TestAddSecondaryEdges:
    def _build_graph(self) -> TracerGraph:
        g = TracerGraph()
        # Paper A cites B (via DOI match) and C (via arxiv match) in its bib,
        # but only has a primary edge to B. After secondary pass we expect a
        # new dashed edge A -> C.
        a = _node(
            "a",
            title="Source paper",
            year=2024,
            bibliography={
                "b0": BibEntry(key="b0", title="Target B stuff", doi="10.1/target-b"),
                "b1": BibEntry(key="b1", title="Target C stuff", arxiv_id="2211.14730"),
            },
        )
        b = _node("doi:10.1/target-b", title="Target B", year=2023, doi="10.1/target-b")
        c = _node("arxiv:2211.14730", title="Target C", year=2023, arxiv_id="2211.14730")
        g.add_node(a)
        g.add_node(b)
        g.add_node(c)
        # Primary edge A -> B already exists
        g.add_edge(CitationEdge(source_id="a", target_id="doi:10.1/target-b"))
        return g, a, b, c

    def test_adds_secondary_edge_for_new_match(self):
        g, a, b, c = self._build_graph()
        added = add_secondary_edges(g)
        assert added == 1
        # Should have added a -> c
        secondary = [e for e in g.edges if e.edge_type == "secondary"]
        assert any(e.source_id == "a" and e.target_id == "arxiv:2211.14730" for e in secondary)

    def test_does_not_duplicate_primary_edge(self):
        g, a, b, c = self._build_graph()
        add_secondary_edges(g)
        # A -> B was already a primary edge — should not get a secondary copy
        edges_a_to_b = [
            e for e in g.edges
            if e.source_id == "a" and e.target_id == "doi:10.1/target-b"
        ]
        assert len(edges_a_to_b) == 1
        assert edges_a_to_b[0].edge_type == "primary"

    def test_secondary_edges_are_marked(self):
        g, _a, _b, _c = self._build_graph()
        add_secondary_edges(g)
        secondary = [e for e in g.edges if e.edge_type == "secondary"]
        assert all(e.context.startswith("bibliographic link") for e in secondary)

    def test_year_backfill_during_secondary_pass(self):
        # Source's bibliography knows an older year for the target (gap 1).
        g = TracerGraph()
        a = _node(
            "a",
            year=2024,
            bibliography={"b0": BibEntry(key="b0", arxiv_id="2211.14730", year=2022)},
        )
        target = _node(
            "arxiv:2211.14730",
            title="A time series is worth 64 words long enough",
            year=2023,
            arxiv_id="2211.14730",
        )
        g.add_node(a)
        g.add_node(target)
        add_secondary_edges(g)
        assert g.nodes["arxiv:2211.14730"].year == 2022  # backfilled
        # original_year should remain the frozen anchor
        assert g.nodes["arxiv:2211.14730"].original_year == 2023

    def test_nodes_without_bibliography_are_skipped(self):
        # Leaf nodes (status=unavailable) have empty bibliography dicts; they
        # can be targets of secondary edges but never sources.
        g = TracerGraph()
        leaf = _node("leaf", title="Leaf", year=2020)
        other = _node("other", title="Other", year=2021)
        g.add_node(leaf)
        g.add_node(other)
        assert add_secondary_edges(g) == 0
