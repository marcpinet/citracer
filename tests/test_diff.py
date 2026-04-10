"""Tests for citracer.diff — baseline comparison and date filtering."""
import json
from pathlib import Path

import pytest

from citracer.diff import DiffResult, apply_diff, load_baseline, parse_since
from citracer.models import CitationEdge, PaperNode, TracerGraph


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _graph(*nodes, edges=None) -> TracerGraph:
    """Build a TracerGraph from PaperNode specs."""
    g = TracerGraph()
    for n in nodes:
        g.add_node(n)
    for e in edges or []:
        g.add_edge(e)
    return g


def _node(pid, year=None, pub_date=None, status="analyzed"):
    return PaperNode(
        paper_id=pid, title=pid, year=year,
        publication_date=pub_date, status=status,
    )


def _baseline_json(node_ids, edge_triples=None):
    """Build a citracer JSON export dict."""
    return {
        "nodes": [{"id": nid, "title": nid, "status": "analyzed"} for nid in node_ids],
        "edges": [
            {"source": s, "target": t, "type": tp}
            for s, t, tp in (edge_triples or [])
        ],
    }


# ---------------------------------------------------------------------------
# load_baseline
# ---------------------------------------------------------------------------

class TestLoadBaseline:
    def test_valid(self, tmp_path):
        p = tmp_path / "baseline.json"
        p.write_text(json.dumps(_baseline_json(["a", "b", "c"])))
        node_ids, edge_keys = load_baseline(p)
        assert node_ids == {"a", "b", "c"}
        assert edge_keys == set()

    def test_with_edges(self, tmp_path):
        p = tmp_path / "baseline.json"
        data = _baseline_json(["a", "b"], [("a", "b", "primary")])
        p.write_text(json.dumps(data))
        node_ids, edge_keys = load_baseline(p)
        assert ("a", "b", "primary") in edge_keys

    def test_empty_nodes(self, tmp_path):
        p = tmp_path / "baseline.json"
        p.write_text(json.dumps({"nodes": [], "edges": []}))
        node_ids, _ = load_baseline(p)
        assert node_ids == set()

    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_baseline(tmp_path / "nope.json")

    def test_malformed_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not json {{{")
        with pytest.raises(ValueError, match="Invalid JSON"):
            load_baseline(p)

    def test_missing_nodes_key(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text(json.dumps({"edges": []}))
        with pytest.raises(ValueError, match="missing 'nodes'"):
            load_baseline(p)

    def test_node_without_id(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text(json.dumps({"nodes": [{"title": "no id"}]}))
        with pytest.raises(ValueError, match="missing 'id'"):
            load_baseline(p)


# ---------------------------------------------------------------------------
# parse_since
# ---------------------------------------------------------------------------

class TestParseSince:
    def test_year_only(self):
        assert parse_since("2025") == (2025, None)

    def test_year_month(self):
        assert parse_since("2025-06") == (2025, 6)

    def test_year_month_single_digit(self):
        assert parse_since("2025-1") == (2025, 1)

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="Invalid --since"):
            parse_since("2025/06")

    def test_invalid_letters(self):
        with pytest.raises(ValueError, match="Invalid --since"):
            parse_since("jan-2025")

    def test_bad_month_zero(self):
        with pytest.raises(ValueError, match="Invalid month"):
            parse_since("2025-0")

    def test_bad_month_thirteen(self):
        with pytest.raises(ValueError, match="Invalid month"):
            parse_since("2025-13")


# ---------------------------------------------------------------------------
# apply_diff
# ---------------------------------------------------------------------------

class TestApplyDiff:
    def test_diff_only(self):
        g = _graph(
            _node("a", year=2020),
            _node("b", year=2022),
            _node("c", year=2024),
        )
        result = apply_diff(g, baseline_node_ids={"a", "b"})
        assert g.nodes["a"].is_new is False
        assert g.nodes["b"].is_new is False
        assert g.nodes["c"].is_new is True
        assert result.n_new_nodes == 1

    def test_since_year_only(self):
        g = _graph(
            _node("old", year=2020),
            _node("mid", year=2024),
            _node("new", year=2025),
        )
        result = apply_diff(g, since="2025")
        assert g.nodes["old"].is_new is False
        assert g.nodes["mid"].is_new is False
        assert g.nodes["new"].is_new is True
        assert result.n_new_nodes == 1

    def test_since_with_publication_date(self):
        g = _graph(
            _node("early", year=2024, pub_date="2024-03-15"),
            _node("late", year=2024, pub_date="2024-09-01"),
        )
        result = apply_diff(g, since="2024-06")
        assert g.nodes["early"].is_new is False  # 2024-03 < 2024-06
        assert g.nodes["late"].is_new is True     # 2024-09 >= 2024-06

    def test_since_falls_back_to_year_when_no_pub_date(self):
        g = _graph(
            _node("no_date", year=2025, pub_date=None),
        )
        result = apply_diff(g, since="2024-06")
        # No pub_date, but year=2025 >= 2024 → new
        assert g.nodes["no_date"].is_new is True

    def test_since_skips_unknown_year(self):
        g = _graph(
            _node("unknown", year=None),
        )
        result = apply_diff(g, since="2024")
        assert g.nodes["unknown"].is_new is False
        assert result.n_skipped_unknown_date == 1

    def test_diff_and_since_intersection(self):
        g = _graph(
            _node("old_and_old", year=2020),   # in baseline, old
            _node("new_but_old", year=2020),   # not in baseline, but old
            _node("old_but_new", year=2025),   # in baseline, but recent
            _node("new_and_new", year=2025),   # not in baseline AND recent
        )
        result = apply_diff(
            g,
            baseline_node_ids={"old_and_old", "old_but_new"},
            since="2024",
        )
        assert g.nodes["old_and_old"].is_new is False  # in baseline
        assert g.nodes["new_but_old"].is_new is False  # new but too old
        assert g.nodes["old_but_new"].is_new is False  # in baseline
        assert g.nodes["new_and_new"].is_new is True   # both conditions met
        assert result.n_new_nodes == 1

    def test_no_flags(self):
        g = _graph(_node("a", year=2024))
        result = apply_diff(g)
        assert g.nodes["a"].is_new is False
        assert result.n_new_nodes == 0

    def test_root_can_be_new(self):
        g = _graph(_node("root", year=2024, status="root"))
        result = apply_diff(g, baseline_node_ids=set())
        assert g.nodes["root"].is_new is True

    def test_original_status_preserved(self):
        g = _graph(
            _node("a", year=2024, status="analyzed"),
            _node("b", year=2024, status="no_match"),
            _node("c", year=2024, status="unavailable"),
        )
        apply_diff(g, baseline_node_ids=set())
        # All marked new, but status unchanged
        for n in g.nodes.values():
            assert n.is_new is True
            assert n.status in ("analyzed", "no_match", "unavailable")

    def test_edge_is_new(self):
        g = _graph(
            _node("a"), _node("b"),
            edges=[CitationEdge(source_id="a", target_id="b")],
        )
        result = apply_diff(
            g,
            baseline_node_ids={"a", "b"},
            baseline_edge_keys=set(),  # no edges in baseline
        )
        assert g.edges[0].is_new is True
        assert result.n_new_edges == 1

    def test_edge_not_new_when_in_baseline(self):
        g = _graph(
            _node("a"), _node("b"),
            edges=[CitationEdge(source_id="a", target_id="b")],
        )
        result = apply_diff(
            g,
            baseline_node_ids={"a", "b"},
            baseline_edge_keys={("a", "b", "primary")},
        )
        assert g.edges[0].is_new is False
        assert result.n_new_edges == 0

    def test_diff_result_counts(self):
        g = _graph(
            _node("old"), _node("new1"), _node("new2"),
            edges=[
                CitationEdge(source_id="old", target_id="new1"),
                CitationEdge(source_id="new1", target_id="new2"),
            ],
        )
        result = apply_diff(
            g,
            baseline_node_ids={"old"},
            baseline_edge_keys=set(),
        )
        assert result.n_new_nodes == 2
        assert result.n_new_edges == 2
        assert result.n_skipped_unknown_date == 0
