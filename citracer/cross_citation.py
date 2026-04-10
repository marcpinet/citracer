"""Cross-graph bibliographic link discovery.

After the BFS tracer has finished building the keyword-associated graph,
this module walks every parsed paper's bibliography against every other
node in the graph and emits dashed "bibliographic link" edges for pairs
that cite each other outside the keyword's neighbourhood.

The pass is purely in-memory — no API calls — so the runtime cost is
O(n² × |bib|) with tight constants, which is dwarfed by the trace itself.
"""
from __future__ import annotations
import logging
from datetime import date

from rapidfuzz import fuzz

from .constants import CROSS_CITATION_FUZZY_THRESHOLD, CROSS_CITATION_MIN_TITLE_LEN, YEAR_GAP_THRESHOLD
from .models import BibEntry, CitationEdge, PaperNode, TracerGraph
from .utils import normalize_arxiv_id, normalize_doi, normalize_title

logger = logging.getLogger(__name__)

_YEAR_GAP_THRESHOLD = YEAR_GAP_THRESHOLD


def _better_year(anchor: int | None, current: int | None, candidate: int | None) -> int | None:
    """Pick the oldest plausible year for the same paper.

    ``anchor`` is the node's frozen first-seen year: comparisons are made
    against it so repeated updates can't cascade arbitrarily far back.
    """
    if candidate is None:
        return current
    if not (1970 <= candidate <= date.today().year + 1):
        return current  # garbage
    if anchor is None:
        # No anchor: just be permissive for the very first assignment.
        if current is None or candidate < current:
            return candidate
        return current
    if candidate >= anchor:
        return current
    if anchor - candidate > _YEAR_GAP_THRESHOLD:
        return current  # too far from anchor, probably a parser mistake
    # Candidate is older than anchor AND within the allowed gap.
    if current is None or candidate < current:
        return candidate
    return current


def add_secondary_edges(graph: TracerGraph) -> int:
    """For every pair of nodes (A, B) where A != B, add a dashed edge A→B
    iff A's bibliography contains an entry matching B (by DOI, arXiv id, or
    fuzzy-matched title). Returns the number of edges added.

    Matches are scoped to the graph we already built — no external API calls.
    """
    added = 0
    for source in graph.nodes.values():
        if not source.bibliography:
            continue  # only nodes we actually parsed have this
        for target in graph.nodes.values():
            if source.paper_id == target.paper_id:
                continue
            # Don't add a secondary edge if a primary one already exists.
            if graph.has_edge(source.paper_id, target.paper_id, "primary"):
                continue
            match = _find_matching_bib(source.bibliography, target)
            if match is None:
                continue
            # Backfill the target's year with the oldest plausible date,
            # anchored on the target's first-seen year.
            target.year = _better_year(target.original_year, target.year, match.year)
            graph.add_edge(CitationEdge(
                source_id=source.paper_id,
                target_id=target.paper_id,
                context=(
                    f"bibliographic link (ref {match.key}: "
                    f"{(match.title or match.raw or '')[:120]})"
                ),
                depth=max(source.depth, target.depth),
                edge_type="secondary",
            ))
            added += 1
    return added


def _find_matching_bib(
    bib_dict: dict[str, BibEntry],
    node: PaperNode,
) -> BibEntry | None:
    """Return the BibEntry from `bib_dict` that best matches `node`, or None."""
    # First pass: exact id matches (fast & reliable)
    node_doi = normalize_doi(node.doi)
    node_arxiv = normalize_arxiv_id(node.arxiv_id)
    for bib in bib_dict.values():
        if node_doi and normalize_doi(bib.doi) == node_doi:
            return bib
        if node_arxiv and normalize_arxiv_id(bib.arxiv_id) == node_arxiv:
            return bib

    # Second pass: fuzzy title match
    if not node.title:
        return None
    target = normalize_title(node.title)
    if not target or len(target) < CROSS_CITATION_MIN_TITLE_LEN:
        return None
    best: BibEntry | None = None
    best_score = 0.0
    for bib in bib_dict.values():
        raw = bib.title or bib.raw
        if not raw:
            continue
        candidate = normalize_title(raw)
        score = min(
            fuzz.token_set_ratio(target, candidate),
            fuzz.token_sort_ratio(target, candidate),
        )
        if score > best_score:
            best_score = score
            best = bib
    return best if best_score >= CROSS_CITATION_FUZZY_THRESHOLD else None
