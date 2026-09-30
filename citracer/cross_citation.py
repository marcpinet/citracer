"""Cross-graph bibliographic link discovery.

After the BFS tracer has finished building the keyword-associated graph,
this module walks every parsed paper's bibliography against every other
node in the graph and emits dashed "bibliographic link" edges for pairs
that cite each other outside the keyword's neighbourhood.

The pass is purely in-memory — no API calls. Exact DOI / arXiv matches
use the graph's alias index; fuzzy title matching is done by rapidfuzz's
C-level ``process.extract`` with a score cutoff, so only the handful of
candidates above the threshold ever reach Python code.
"""
from __future__ import annotations
import logging

from rapidfuzz import fuzz, process

from .constants import CROSS_CITATION_FUZZY_THRESHOLD, CROSS_CITATION_MIN_TITLE_LEN, YEAR_GAP_THRESHOLD
from .models import BibEntry, CitationEdge, PaperNode, TracerGraph
from .utils import arxiv_id_from_doi, normalize_title, plausible_year

logger = logging.getLogger(__name__)

_YEAR_GAP_THRESHOLD = YEAR_GAP_THRESHOLD


def _better_year(anchor: int | None, current: int | None, candidate: int | None) -> int | None:
    """Pick the oldest plausible year for the same paper.

    ``anchor`` is the node's frozen first-seen year: comparisons are made
    against it so repeated updates can't cascade arbitrarily far back.
    """
    if candidate is None:
        return current
    if not plausible_year(candidate):
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
    # Pre-normalize target titles for fuzzy matching (once, not per source).
    target_ids: list[str] = []
    target_titles: list[str] = []
    for node in graph.nodes.values():
        if node.title:
            nt = normalize_title(node.title)
            if nt and len(nt) >= CROSS_CITATION_MIN_TITLE_LEN:
                target_ids.append(node.paper_id)
                target_titles.append(nt)

    added = 0
    for source in list(graph.nodes.values()):
        if not source.bibliography:
            continue

        def _add(target: PaperNode, bib: BibEntry) -> None:
            nonlocal added
            target.year = _better_year(target.original_year, target.year, bib.year)
            graph.add_edge(CitationEdge(
                source_id=source.paper_id,
                target_id=target.paper_id,
                context=(
                    f"bibliographic link (ref {bib.key}: "
                    f"{(bib.title or bib.raw or '')[:120]})"
                ),
                depth=max(source.depth, target.depth),
                edge_type="secondary",
            ))
            added += 1

        # Phase 1: exact ID matches via the graph's alias index — O(B).
        exact_targets: set[str] = set()
        for bib in source.bibliography.values():
            if not (bib.doi or bib.arxiv_id):
                continue
            target_id = graph.find(
                doi=bib.doi, arxiv_id=bib.arxiv_id or arxiv_id_from_doi(bib.doi),
            )
            if target_id is None or target_id == source.paper_id:
                continue
            exact_targets.add(target_id)
            if graph.has_edge(source.paper_id, target_id, "primary"):
                continue
            _add(graph.nodes[target_id], bib)

        # Phase 2: fuzzy title matching for targets not found via exact IDs.
        bibs: list[BibEntry] = []
        bib_titles: list[str] = []
        for bib in source.bibliography.values():
            raw = bib.title or bib.raw
            nt = normalize_title(raw) if raw else ""
            if nt:
                bibs.append(bib)
                bib_titles.append(nt)
        if not bib_titles:
            continue

        for target_id, target_title in zip(target_ids, target_titles):
            if target_id == source.paper_id or target_id in exact_targets:
                continue
            if graph.has_edge(source.paper_id, target_id, "primary"):
                continue
            # token_sort_ratio >= cutoff is necessary for min(set, sort) >=
            # cutoff, so let rapidfuzz prune in C, then score the survivors.
            candidates = process.extract(
                target_title, bib_titles,
                scorer=fuzz.token_sort_ratio,
                score_cutoff=CROSS_CITATION_FUZZY_THRESHOLD,
                limit=None,
            )
            best_bib: BibEntry | None = None
            best_score = 0.0
            for _choice, sort_score, idx in candidates:
                score = min(sort_score, fuzz.token_set_ratio(target_title, bib_titles[idx]))
                if score > best_score:
                    best_score = score
                    best_bib = bibs[idx]
            if best_score >= CROSS_CITATION_FUZZY_THRESHOLD and best_bib:
                _add(graph.nodes[target_id], best_bib)

    return added
