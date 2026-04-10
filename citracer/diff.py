"""Diff a new trace against a previous JSON export.

Marks nodes and edges that didn't exist in the baseline as ``is_new``,
so the visualizer can highlight them in orange. Supports an optional
``--since YYYY`` or ``--since YYYY-MM`` date filter that restricts
which nodes count as "new" by publication date.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .models import TracerGraph

logger = logging.getLogger(__name__)


@dataclass
class DiffResult:
    n_new_nodes: int
    n_new_edges: int
    n_skipped_unknown_date: int


def load_baseline(path: str | Path) -> tuple[set[str], set[tuple[str, str, str]]]:
    """Read a previous citracer JSON export and return its node IDs and
    edge keys.

    Returns:
        (node_ids, edge_keys) where edge_keys are ``(source, target, type)``
        tuples.

    Raises:
        FileNotFoundError: If the file doesn't exist.
        ValueError: If the JSON is malformed.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Baseline file not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in baseline file: {e}") from e

    if "nodes" not in data:
        raise ValueError(
            f"Baseline file missing 'nodes' key — is this a citracer JSON export? ({p})"
        )

    node_ids: set[str] = set()
    for n in data["nodes"]:
        nid = n.get("id")
        if nid is None:
            raise ValueError("Baseline node missing 'id' field")
        node_ids.add(nid)

    edge_keys: set[tuple[str, str, str]] = set()
    for e in data.get("edges", []):
        src = e.get("source")
        tgt = e.get("target")
        etype = e.get("type", "primary")
        if src and tgt:
            edge_keys.add((src, tgt, etype))

    return node_ids, edge_keys


def parse_since(value: str) -> tuple[int, int | None]:
    """Parse a ``--since`` value into ``(year, month_or_none)``.

    Accepts ``YYYY`` or ``YYYY-MM``.

    Raises:
        ValueError: On invalid format.
    """
    m = re.fullmatch(r"(\d{4})(?:-(\d{1,2}))?", value.strip())
    if not m:
        raise ValueError(
            f"Invalid --since format: {value!r} (expected YYYY or YYYY-MM)"
        )
    year = int(m.group(1))
    month = int(m.group(2)) if m.group(2) else None
    if month is not None and not (1 <= month <= 12):
        raise ValueError(f"Invalid month in --since: {month}")
    return year, month


def _passes_since(
    node_year: int | None,
    node_pub_date: str | None,
    since_year: int,
    since_month: int | None,
) -> bool | None:
    """Check whether a node's date passes the ``--since`` filter.

    Returns True/False, or None if the node has no date information
    (caller should treat as "not new" and count it as skipped).
    """
    # Try publication_date first (YYYY-MM-DD, finer than year)
    if node_pub_date and since_month is not None:
        m = re.match(r"(\d{4})-(\d{2})", node_pub_date)
        if m:
            py, pm = int(m.group(1)), int(m.group(2))
            return (py, pm) >= (since_year, since_month)

    # Fall back to year-only comparison
    if node_year is not None:
        return node_year >= since_year

    # No date at all
    return None


def apply_diff(
    graph: TracerGraph,
    baseline_node_ids: set[str] | None = None,
    baseline_edge_keys: set[tuple[str, str, str]] | None = None,
    since: str | None = None,
) -> DiffResult:
    """Mark nodes and edges as ``is_new`` based on diff and/or date filter.

    Args:
        graph: The just-traced graph (mutated in place).
        baseline_node_ids: Node IDs from a previous export (``--diff``).
            ``None`` means no diff baseline.
        baseline_edge_keys: Edge keys from a previous export.
            ``None`` means no diff baseline.
        since: Date filter string (``YYYY`` or ``YYYY-MM``).
            ``None`` means no date filter.

    When both ``--diff`` and ``--since`` are provided, a node must satisfy
    **both** conditions (intersection) to be marked new.

    Note: paper_id is not fully stable across runs — if a paper was resolved
    by title hash in one run and by DOI in another, it may falsely appear as
    new. Re-running both traces from the same cache minimizes this.
    """
    since_year, since_month = parse_since(since) if since else (None, None)

    has_diff = baseline_node_ids is not None
    has_since = since_year is not None

    n_new_nodes = 0
    n_skipped = 0

    for node in graph.nodes.values():
        passes_diff = True
        passes_date = True

        if has_diff:
            passes_diff = node.paper_id not in baseline_node_ids

        if has_since:
            result = _passes_since(
                node.year, node.publication_date, since_year, since_month,
            )
            if result is None:
                passes_date = False
                n_skipped += 1
            else:
                passes_date = result

        if has_diff and has_since:
            node.is_new = passes_diff and passes_date
        elif has_diff:
            node.is_new = passes_diff
        elif has_since:
            node.is_new = passes_date
        # else: no flags, is_new stays False

        if node.is_new:
            n_new_nodes += 1

    # Mark new edges
    n_new_edges = 0
    bl_edges = baseline_edge_keys or set()
    for edge in graph.edges:
        key = (edge.source_id, edge.target_id, edge.edge_type)
        if has_diff and key not in bl_edges:
            edge.is_new = True
            n_new_edges += 1

    if n_skipped and has_since:
        logger.warning(
            "%d node(s) with unknown publication date skipped by --since filter",
            n_skipped,
        )

    return DiffResult(
        n_new_nodes=n_new_nodes,
        n_new_edges=n_new_edges,
        n_skipped_unknown_date=n_skipped,
    )
