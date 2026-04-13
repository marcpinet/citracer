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
    # Pre-index nodes by DOI and arXiv ID for O(1) exact matching.
    doi_to_id: dict[str, str] = {}
    arxiv_to_id: dict[str, str] = {}
    for node in graph.nodes.values():
        d = normalize_doi(node.doi)
        if d:
            doi_to_id[d] = node.paper_id
        a = normalize_arxiv_id(node.arxiv_id)
        if a:
            arxiv_to_id[a] = node.paper_id

    # Pre-normalize target titles for fuzzy matching (once, not per source).
    node_norm_titles: dict[str, str] = {}
    for node in graph.nodes.values():
        if node.title:
            nt = normalize_title(node.title)
            if nt and len(nt) >= CROSS_CITATION_MIN_TITLE_LEN:
                node_norm_titles[node.paper_id] = nt

    added = 0
    for source in graph.nodes.values():
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

        # Phase 1: exact ID matches via pre-built index — O(B) per source.
        exact_targets: set[str] = set()
        for bib in source.bibliography.values():
            target_id = None
            bd = normalize_doi(bib.doi)
            if bd and bd in doi_to_id:
                target_id = doi_to_id[bd]
            if target_id is None:
                ba = normalize_arxiv_id(bib.arxiv_id)
                if ba and ba in arxiv_to_id:
                    target_id = arxiv_to_id[ba]
            if target_id is None or target_id == source.paper_id:
                continue
            exact_targets.add(target_id)
            if graph.has_edge(source.paper_id, target_id, "primary"):
                continue
            _add(graph.nodes[target_id], bib)

        # Phase 2: fuzzy title matching for targets not found via exact IDs.
        # Pre-normalize bib titles once for this source.
        bib_titles: list[tuple[BibEntry, str]] = []
        for bib in source.bibliography.values():
            raw = bib.title or bib.raw
            if raw:
                bib_titles.append((bib, normalize_title(raw)))

        if not bib_titles:
            continue

        for target_id, target_title in node_norm_titles.items():
            if target_id == source.paper_id:
                continue
            if target_id in exact_targets:
                continue
            if graph.has_edge(source.paper_id, target_id, "primary"):
                continue
            best_bib: BibEntry | None = None
            best_score = 0.0
            for bib, bib_title in bib_titles:
                score = min(
                    fuzz.token_set_ratio(target_title, bib_title),
                    fuzz.token_sort_ratio(target_title, bib_title),
                )
                if score > best_score:
                    best_score = score
                    best_bib = bib
            if best_score >= CROSS_CITATION_FUZZY_THRESHOLD and best_bib:
                _add(graph.nodes[target_id], best_bib)

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
