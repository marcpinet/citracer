"""Serialize a TracerGraph to standard graph formats.

Supported formats:
  - **json**: a self-describing citracer-native format. Includes all node
    and edge metadata (status, depth, year, hits, edge type, context, ...).
    Useful for downstream scripting or human inspection.
  - **graphml**: the GraphML XML dialect understood by Gephi, networkx,
    yEd, Cytoscape, and most general-purpose graph tools. Node/edge
    attributes are declared with typed `<key>` elements.

PDFs and bibliographies are *not* included — this is a graph export, not
a full backup. If you need those, copy ``cache/pdfs/`` alongside the
exported file.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from .models import TracerGraph

logger = logging.getLogger(__name__)


def export_graph(
    graph: TracerGraph,
    path: str | Path,
    fmt: str | None = None,
    manifest: dict | None = None,
    analytics: dict | None = None,
) -> Path:
    """Write ``graph`` to ``path`` in the format derived from the file
    extension (``.json`` or ``.graphml``), or from the explicit ``fmt``
    argument if given.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fmt = (fmt or out.suffix.lstrip(".")).lower()

    if fmt == "json":
        _export_json(graph, out, manifest=manifest, analytics=analytics)
    elif fmt == "graphml":
        _export_graphml(graph, out, analytics=analytics)
    else:
        raise ValueError(
            f"Unknown export format {fmt!r}. Use 'json' or 'graphml'."
        )
    logger.info("Exported graph (%d nodes, %d edges) to %s",
                len(graph.nodes), len(graph.edges), out)
    return out


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def _export_json(
    graph: TracerGraph,
    out: Path,
    manifest: dict | None = None,
    analytics: dict | None = None,
) -> None:
    payload: dict = {}
    if manifest:
        payload["metadata"] = manifest
    if analytics:
        payload["analytics"] = analytics
    payload["nodes"] = [
        {
            "id": n.paper_id,
            "title": n.title,
            "authors": n.authors,
            "year": n.year,
            "publication_date": n.publication_date,
            "status": n.status,
            "depth": n.depth,
            "doi": n.doi,
            "arxiv_id": n.arxiv_id,
            "abstract": n.abstract,
            "citation_count": n.citation_count,
            "url": n.url,
            "keyword_hits": n.keyword_hits,
            "is_new": n.is_new,
        }
        for n in graph.nodes.values()
    ]
    payload["edges"] = [
        {
            "source": e.source_id,
            "target": e.target_id,
            "type": e.edge_type,
            "depth": e.depth,
            "context": e.context,
            "is_new": e.is_new,
        }
        for e in graph.edges
    ]
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# GraphML
# ---------------------------------------------------------------------------

_GRAPHML_KEYS = [
    # (id, for_, name, type)
    ("title",        "node", "title",        "string"),
    ("authors",      "node", "authors",      "string"),
    ("year",         "node", "year",         "int"),
    ("status",       "node", "status",       "string"),
    ("depth",        "node", "depth",        "int"),
    ("doi",          "node", "doi",          "string"),
    ("arxiv_id",     "node", "arxiv_id",     "string"),
    ("abstract",     "node", "abstract",     "string"),
    ("citation_count", "node", "citation_count", "int"),
    ("url",          "node", "url",          "string"),
    ("keyword_hits", "node", "keyword_hits", "int"),
    ("publication_date", "node", "publication_date", "string"),
    ("is_new",       "node", "is_new",       "boolean"),
    ("betweenness",  "node", "betweenness",  "double"),
    ("pagerank",     "node", "pagerank",     "double"),
    ("is_pivot",     "node", "is_pivot",     "boolean"),

    ("edge_type",    "edge", "edge_type",    "string"),
    ("edge_depth",   "edge", "depth",        "int"),
    ("context",      "edge", "context",      "string"),
    ("edge_is_new",  "edge", "is_new",       "boolean"),
]


def _export_graphml(graph: TracerGraph, out: Path, analytics: dict | None = None) -> None:
    lines: list[str] = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append(
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://graphml.graphdrawing.org/xmlns '
        'http://graphml.graphdrawing.org/xmlns/1.0/graphml.xsd">'
    )

    for key_id, for_, name, type_ in _GRAPHML_KEYS:
        lines.append(
            f'  <key id="{key_id}" for="{for_}" '
            f'attr.name="{name}" attr.type="{type_}"/>'
        )

    lines.append('  <graph id="citracer" edgedefault="directed">')

    for n in graph.nodes.values():
        lines.append(f'    <node id="{xml_escape(n.paper_id)}">')
        _data(lines, "title", n.title)
        _data(lines, "authors", ", ".join(n.authors) if n.authors else None)
        _data(lines, "year", n.year)
        _data(lines, "status", n.status)
        _data(lines, "depth", n.depth)
        _data(lines, "doi", n.doi)
        _data(lines, "arxiv_id", n.arxiv_id)
        _data(lines, "abstract", n.abstract)
        _data(lines, "citation_count", n.citation_count)
        _data(lines, "url", n.url)
        _data(lines, "keyword_hits", len(n.keyword_hits))
        _data(lines, "publication_date", n.publication_date)
        if n.is_new:
            _data(lines, "is_new", n.is_new)
        if analytics:
            nm = analytics.get("node_metrics", {}).get(n.paper_id, {})
            _data(lines, "betweenness", nm.get("betweenness"))
            _data(lines, "pagerank", nm.get("pagerank"))
            _data(lines, "is_pivot", nm.get("is_pivot"))
        lines.append("    </node>")

    for i, e in enumerate(graph.edges):
        lines.append(
            f'    <edge id="e{i}" '
            f'source="{xml_escape(e.source_id)}" '
            f'target="{xml_escape(e.target_id)}">'
        )
        _data(lines, "edge_type", e.edge_type)
        _data(lines, "edge_depth", e.depth)
        _data(lines, "context", e.context)
        if e.is_new:
            _data(lines, "edge_is_new", e.is_new)
        lines.append("    </edge>")

    lines.append("  </graph>")
    lines.append("</graphml>")

    out.write_text("\n".join(lines), encoding="utf-8")


def _data(lines: list[str], key: str, value) -> None:
    if value is None or value == "":
        return
    lines.append(f'      <data key="{key}">{xml_escape(str(value))}</data>')
