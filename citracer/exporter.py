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


def export_graph(graph: TracerGraph, path: str | Path, fmt: str | None = None) -> Path:
    """Write ``graph`` to ``path`` in the format derived from the file
    extension (``.json`` or ``.graphml``), or from the explicit ``fmt``
    argument if given.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fmt = (fmt or out.suffix.lstrip(".")).lower()

    if fmt == "json":
        _export_json(graph, out)
    elif fmt == "graphml":
        _export_graphml(graph, out)
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

def _export_json(graph: TracerGraph, out: Path) -> None:
    payload = {
        "nodes": [
            {
                "id": n.paper_id,
                "title": n.title,
                "authors": n.authors,
                "year": n.year,
                "status": n.status,
                "depth": n.depth,
                "doi": n.doi,
                "arxiv_id": n.arxiv_id,
                "abstract": n.abstract,
                "url": n.url,
                "keyword_hits": n.keyword_hits,
            }
            for n in graph.nodes.values()
        ],
        "edges": [
            {
                "source": e.source_id,
                "target": e.target_id,
                "type": e.edge_type,
                "depth": e.depth,
                "context": e.context,
            }
            for e in graph.edges
        ],
    }
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
    ("url",          "node", "url",          "string"),
    ("keyword_hits", "node", "keyword_hits", "int"),

    ("edge_type",    "edge", "edge_type",    "string"),
    ("edge_depth",   "edge", "depth",        "int"),
    ("context",      "edge", "context",      "string"),
]


def _export_graphml(graph: TracerGraph, out: Path) -> None:
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
        _data(lines, "url", n.url)
        _data(lines, "keyword_hits", len(n.keyword_hits))
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
        lines.append("    </edge>")

    lines.append("  </graph>")
    lines.append("</graphml>")

    out.write_text("\n".join(lines), encoding="utf-8")


def _data(lines: list[str], key: str, value) -> None:
    if value is None or value == "":
        return
    lines.append(f'      <data key="{key}">{xml_escape(str(value))}</data>')
