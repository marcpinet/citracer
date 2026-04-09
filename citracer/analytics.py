"""Bibliometric analytics for citracer graphs.

Computes quantitative metrics on a TracerGraph: centrality measures,
temporal evolution of keyword usage, and pivot paper detection. Uses
networkx for graph algorithms.

The output is a plain dict suitable for JSON serialization, consumed by
the manifest, exporter, and visualizer.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict

import networkx as nx

from .models import TracerGraph

logger = logging.getLogger(__name__)


def analyze(graph: TracerGraph) -> dict:
    """Compute all bibliometric analytics on a traced graph.

    Returns a dict with four keys:

    - ``node_metrics``: per-node dict of betweenness, pagerank, degree, is_pivot
    - ``global``: graph-wide density, avg_degree, connected_components
    - ``timeline``: per-year keyword usage evolution
    - ``pivot_papers``: list of paper_ids identified as pivots
    """
    if not graph.nodes:
        return {
            "node_metrics": {},
            "global": {"density": 0.0, "avg_degree": 0.0, "connected_components": 0},
            "timeline": [],
            "pivot_papers": [],
        }

    G = _to_networkx(graph)
    node_metrics = _node_metrics(G, graph)
    pivots = _detect_pivots(G, graph, node_metrics)

    # Mark pivots in node_metrics
    for pid in pivots:
        if pid in node_metrics:
            node_metrics[pid]["is_pivot"] = True

    return {
        "node_metrics": node_metrics,
        "global": _global_metrics(G, graph),
        "timeline": _timeline(graph),
        "pivot_papers": pivots,
    }


def _to_networkx(graph: TracerGraph) -> nx.DiGraph:
    """Convert a TracerGraph to a networkx DiGraph."""
    G = nx.DiGraph()
    for pid, node in graph.nodes.items():
        G.add_node(pid, year=node.year, status=node.status)
    for edge in graph.edges:
        G.add_edge(edge.source_id, edge.target_id, edge_type=edge.edge_type)
    return G


def _node_metrics(G: nx.DiGraph, graph: TracerGraph) -> dict[str, dict]:
    """Compute per-node centrality metrics."""
    # Betweenness centrality on the directed graph
    betweenness = nx.betweenness_centrality(G)

    # PageRank (handles directed graphs naturally)
    try:
        pagerank = nx.pagerank(G)
    except nx.PowerIterationFailedConvergence:
        # Fallback: uniform distribution
        pagerank = {n: 1.0 / len(G) for n in G}

    metrics: dict[str, dict] = {}
    for pid in graph.nodes:
        metrics[pid] = {
            "betweenness": round(betweenness.get(pid, 0.0), 6),
            "pagerank": round(pagerank.get(pid, 0.0), 6),
            "in_degree": G.in_degree(pid),
            "out_degree": G.out_degree(pid),
            "is_pivot": False,
        }
    return metrics


def _global_metrics(G: nx.DiGraph, graph: TracerGraph) -> dict:
    """Compute graph-wide metrics."""
    n = len(G)
    m = G.number_of_edges()

    # Density: for a directed graph, max edges = n*(n-1)
    max_edges = n * (n - 1) if n > 1 else 1
    density = m / max_edges

    avg_degree = (2 * m) / n if n > 0 else 0.0

    # Connected components on undirected view
    n_components = nx.number_weakly_connected_components(G)

    return {
        "density": round(density, 6),
        "avg_degree": round(avg_degree, 2),
        "connected_components": n_components,
    }


def _timeline(graph: TracerGraph) -> list[dict]:
    """Per-year breakdown of keyword usage density.

    Returns a sorted list of dicts:
    ``{"year": int, "total": int, "with_keyword": int, "keyword_density": float}``
    """
    by_year: dict[int, dict] = defaultdict(lambda: {"total": 0, "with_keyword": 0})

    for node in graph.nodes.values():
        if node.year is None:
            continue
        by_year[node.year]["total"] += 1
        # A paper "has the keyword" if it is root/analyzed (keyword was found)
        if node.status in ("root", "analyzed"):
            by_year[node.year]["with_keyword"] += 1

    timeline = []
    for year in sorted(by_year):
        entry = by_year[year]
        total = entry["total"]
        with_kw = entry["with_keyword"]
        timeline.append({
            "year": year,
            "total": total,
            "with_keyword": with_kw,
            "keyword_density": round(with_kw / total, 4) if total > 0 else 0.0,
        })

    return timeline


def _detect_pivots(
    G: nx.DiGraph,
    graph: TracerGraph,
    node_metrics: dict[str, dict],
) -> list[str]:
    """Identify pivot papers — papers that likely introduced the concept
    to a sub-community.

    A paper is a pivot if:
    1. It is the earliest keyword-matched paper (status=analyzed or root)
       in its weakly connected component, OR
    2. It has high betweenness (> 2x mean) AND has the keyword.
    """
    pivots: set[str] = set()

    # Strategy 1: earliest keyword-matched per connected component
    undirected = G.to_undirected()
    for component in nx.connected_components(undirected):
        # Find earliest analyzed/root node in this component
        best_year = None
        best_pid = None
        for pid in component:
            node = graph.nodes.get(pid)
            if node is None:
                continue
            if node.status not in ("analyzed", "root"):
                continue
            if node.year is None:
                continue
            if best_year is None or node.year < best_year:
                best_year = node.year
                best_pid = pid
        if best_pid is not None:
            pivots.add(best_pid)

    # Strategy 2: high betweenness + keyword
    betweenness_values = [m["betweenness"] for m in node_metrics.values()]
    if betweenness_values:
        mean_btw = sum(betweenness_values) / len(betweenness_values)
        threshold = mean_btw * 2
        for pid, m in node_metrics.items():
            node = graph.nodes.get(pid)
            if node is None:
                continue
            if m["betweenness"] > threshold and node.status in ("analyzed", "root"):
                pivots.add(pid)

    return sorted(pivots)
