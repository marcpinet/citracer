"""Tests for citracer.exporter — JSON and GraphML output."""
import json
from xml.etree import ElementTree as ET

import pytest

from citracer.exporter import export_graph
from citracer.models import CitationEdge, PaperNode, TracerGraph


@pytest.fixture
def sample_graph() -> TracerGraph:
    g = TracerGraph()
    g.add_node(PaperNode(
        paper_id="arxiv:2211.14730",
        title="A time series is worth 64 words",
        authors=["Yuqi Nie", "Nam Nguyen"],
        year=2022,
        arxiv_id="2211.14730",
        doi=None,
        status="analyzed",
        depth=1,
        keyword_hits=["passage one", "passage two"],
        url="https://arxiv.org/abs/2211.14730",
        abstract="A short abstract.",
    ))
    g.add_node(PaperNode(
        paper_id="doi:10.1/abc",
        title="Root paper with <special> & chars",
        authors=["Marc Pinet"],
        year=2024,
        doi="10.1/abc",
        status="root",
        depth=0,
    ))
    g.add_edge(CitationEdge(
        source_id="doi:10.1/abc",
        target_id="arxiv:2211.14730",
        context="Cited in the 'channel-independent' passage",
        depth=1,
        edge_type="primary",
    ))
    g.add_edge(CitationEdge(
        source_id="arxiv:2211.14730",
        target_id="doi:10.1/abc",
        context="bibliographic link",
        depth=1,
        edge_type="secondary",
    ))
    return g


# ---------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------

class TestJsonExport:
    def test_writes_valid_json(self, sample_graph, tmp_path):
        out = tmp_path / "graph.json"
        export_graph(sample_graph, out)
        data = json.loads(out.read_text(encoding="utf-8"))
        assert "nodes" in data and "edges" in data

    def test_node_count(self, sample_graph, tmp_path):
        out = tmp_path / "graph.json"
        export_graph(sample_graph, out)
        data = json.loads(out.read_text(encoding="utf-8"))
        assert len(data["nodes"]) == 2
        assert len(data["edges"]) == 2

    def test_node_fields_preserved(self, sample_graph, tmp_path):
        out = tmp_path / "graph.json"
        export_graph(sample_graph, out)
        data = json.loads(out.read_text(encoding="utf-8"))
        nie = next(n for n in data["nodes"] if n["id"] == "arxiv:2211.14730")
        assert nie["title"] == "A time series is worth 64 words"
        assert nie["year"] == 2022
        assert nie["arxiv_id"] == "2211.14730"
        assert nie["status"] == "analyzed"
        assert nie["keyword_hits"] == ["passage one", "passage two"]
        assert nie["abstract"] == "A short abstract."

    def test_edge_fields_preserved(self, sample_graph, tmp_path):
        out = tmp_path / "graph.json"
        export_graph(sample_graph, out)
        data = json.loads(out.read_text(encoding="utf-8"))
        primary = next(e for e in data["edges"] if e["type"] == "primary")
        assert primary["source"] == "doi:10.1/abc"
        assert primary["target"] == "arxiv:2211.14730"
        assert "channel-independent" in primary["context"]

    def test_special_chars_preserved(self, sample_graph, tmp_path):
        out = tmp_path / "graph.json"
        export_graph(sample_graph, out)
        data = json.loads(out.read_text(encoding="utf-8"))
        root = next(n for n in data["nodes"] if n["id"] == "doi:10.1/abc")
        # <, >, & make it through JSON without damage
        assert root["title"] == "Root paper with <special> & chars"


# ---------------------------------------------------------------------------
# GraphML export
# ---------------------------------------------------------------------------

class TestGraphmlExport:
    _NS = {"g": "http://graphml.graphdrawing.org/xmlns"}

    def test_writes_valid_xml(self, sample_graph, tmp_path):
        out = tmp_path / "graph.graphml"
        export_graph(sample_graph, out)
        # Must parse without errors
        root = ET.parse(out).getroot()
        assert root.tag.endswith("graphml")

    def test_node_count(self, sample_graph, tmp_path):
        out = tmp_path / "graph.graphml"
        export_graph(sample_graph, out)
        root = ET.parse(out).getroot()
        assert len(root.findall(".//g:node", self._NS)) == 2
        assert len(root.findall(".//g:edge", self._NS)) == 2

    def test_keys_declared(self, sample_graph, tmp_path):
        out = tmp_path / "graph.graphml"
        export_graph(sample_graph, out)
        root = ET.parse(out).getroot()
        keys = [k.get("id") for k in root.findall(".//g:key", self._NS)]
        # A representative sample from _GRAPHML_KEYS
        for expected in ("title", "year", "status", "edge_type"):
            assert expected in keys

    def test_xml_escaping(self, sample_graph, tmp_path):
        out = tmp_path / "graph.graphml"
        export_graph(sample_graph, out)
        xml_text = out.read_text(encoding="utf-8")
        # The ampersand in "<special> & chars" must be escaped; the raw text
        # should not contain a bare '&' followed by a letter.
        assert "&amp;" in xml_text
        # Both parser and well-formed XML — already asserted by ET.parse above.

    def test_explicit_format_argument(self, sample_graph, tmp_path):
        # Override extension-based detection
        out = tmp_path / "graph.dat"
        export_graph(sample_graph, out, fmt="graphml")
        assert ET.parse(out).getroot().tag.endswith("graphml")

    def test_unknown_format_raises(self, sample_graph, tmp_path):
        out = tmp_path / "graph.txt"
        with pytest.raises(ValueError):
            export_graph(sample_graph, out)
