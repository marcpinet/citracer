"""Recursive citation tracer.

The tracer runs a breadth-first walk over the citation graph. Each BFS
iteration drains the current queue into a batch, parses every unique PDF in
parallel via a ``ThreadPoolExecutor`` (GROBID calls are I/O-bound and the
upstream Docker image handles ~10 concurrent requests out of the box), then
processes the parsed results sequentially — the sequential phase mutates
the graph, resolves references and enqueues children for the next batch.
"""
from __future__ import annotations
import logging
import signal
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tqdm import tqdm

from . import keyword_matcher, pdf_parser
from .constants import GROBID_DEFAULT_WORKERS, YEAR_GAP_THRESHOLD
from .cross_citation import add_secondary_edges
from .models import CitationEdge, PaperNode, ParsedPaper, TracerGraph
from .reference_resolver import ReferenceResolver, ResolvedRef
from .utils import make_paper_id, normalize_arxiv_id, normalize_doi

logger = logging.getLogger(__name__)

#: Max threads used to resolve references in parallel. Each resolve hits
#: arxiv + possibly S2, but the rate limits are respected by locks inside
#: ReferenceResolver, so more threads don't break the rate limits — they
#: just overlap waiting periods.
RESOLVE_DEFAULT_WORKERS = 4

# Global cancellation flag set by SIGINT. The BFS loops poll it between
# iterations so Ctrl+C exits within seconds instead of waiting for every
# in-flight HTTP request to complete.
_CANCEL_REQUESTED = False


def _install_sigint_handler():
    """Register a SIGINT handler that flips the cancel flag.

    Returns the previous handler so the caller can restore it once tracing
    is done. We do NOT want to leave a global signal handler in place after
    trace() returns to a host application.
    """
    global _CANCEL_REQUESTED
    _CANCEL_REQUESTED = False

    def _handler(_signum, _frame):
        global _CANCEL_REQUESTED
        if _CANCEL_REQUESTED:
            # Second Ctrl+C: forceful exit, propagate the interrupt
            raise KeyboardInterrupt
        _CANCEL_REQUESTED = True
        logger.warning(
            "Cancellation requested, finishing in-flight work and stopping..."
        )

    try:
        return signal.signal(signal.SIGINT, _handler)
    except (ValueError, OSError):
        # signal.signal can fail if we're not in the main thread (tests etc.)
        return None


# Re-exported for backwards compatibility with `from .tracer import add_secondary_edges`.
__all__ = ["trace", "trace_reverse", "add_secondary_edges"]

# Queue item shape: (pdf_path, depth, parent_id, parent_context, parent_resolved)
QueueItem = tuple[Path, int, "str | None", str, "ResolvedRef | None"]


def trace(
    root_pdf: str | Path,
    keyword: str | list[str],
    max_depth: int = 3,
    cache_dir: str | Path = "./cache",
    grobid_url: str = "http://localhost:8070",
    context_window: int | None = None,
    s2_api_key: str | None = None,
    grobid_workers: int = GROBID_DEFAULT_WORKERS,
    consolidate_citations: bool = False,
    match_mode: str = "any",
    supplied_pdfs: dict[str, Path] | None = None,
    enrich: bool = False,
    email: str | None = None,
    use_semantic: bool = False,
    semantic_model: str | None = None,
    semantic_threshold: float | None = None,
) -> TracerGraph:
    # Normalize: always work with a list of keywords internally.
    keywords: list[str] = [keyword] if isinstance(keyword, str) else list(keyword)
    if not keywords:
        raise ValueError("At least one keyword is required.")
    if match_mode not in ("any", "all"):
        raise ValueError(f"match_mode must be 'any' or 'all', got {match_mode!r}")

    graph = TracerGraph()
    resolver = ReferenceResolver(
        cache_dir=cache_dir,
        s2_api_key=s2_api_key,
        supplied_pdfs=supplied_pdfs,
        enrich=enrich,
        email=email,
    )

    # parent_resolved is None for the root.
    queue: deque[QueueItem] = deque()
    queue.append((Path(root_pdf), 0, None, "", None))

    # Map a PDF we've already parsed to the node_id we created for it,
    # so a second incoming edge can be wired up without re-parsing.
    pdf_to_node_id: dict[Path, str] = {}

    # --- sequential post-parse handler (closure) -------------------------
    def _handle(item: QueueItem, parsed: ParsedPaper | None) -> None:
        pdf_path, depth, parent_id, parent_context, parent_resolved = item

        # Fast path: this PDF was parsed during this batch or a previous one.
        if pdf_path in pdf_to_node_id:
            existing_id = pdf_to_node_id[pdf_path]
            # Backfill the existing node's year, anchored on its first-seen
            # year so that repeated updates don't cascade away from truth.
            if parent_resolved is not None and parent_resolved.year is not None:
                existing = graph.nodes.get(existing_id)
                if existing is not None:
                    existing.year = _older_within_gap(
                        existing.original_year, parent_resolved.year
                    )
            if parent_id is not None:
                graph.add_edge(CitationEdge(
                    source_id=parent_id,
                    target_id=existing_id,
                    context=parent_context,
                    depth=depth,
                ))
            return

        if parsed is None:
            return  # parse failed for this path, already logged

        # Build node identity
        if parent_resolved is not None:
            node_id = parent_resolved.paper_id
            node = PaperNode(
                paper_id=node_id,
                title=parent_resolved.title,
                authors=parent_resolved.authors,
                year=parent_resolved.year,
                publication_date=parent_resolved.publication_date,
                original_year=parent_resolved.year,
                arxiv_id=parent_resolved.arxiv_id,
                doi=parent_resolved.doi,
                abstract=parent_resolved.abstract,
                citation_count=parent_resolved.citation_count,
                depth=depth,
                url=parent_resolved.url,
            )
        else:
            node_id = make_paper_id(
                doi=parsed.doi,
                arxiv_id=parsed.arxiv_id,
                title=parsed.title or pdf_path.stem,
            )
            node = PaperNode(
                paper_id=node_id,
                title=parsed.title or pdf_path.stem,
                authors=parsed.authors,
                year=parsed.year,
                original_year=parsed.year,
                arxiv_id=parsed.arxiv_id,
                doi=parsed.doi,
                depth=depth,
                status="root",
            )

        if node_id in graph.nodes:
            # Canonical id already exists — reached via a different PDF path.
            pdf_to_node_id[pdf_path] = node_id
            existing = graph.nodes[node_id]
            if parent_resolved is not None and parent_resolved.year is not None:
                existing.year = _older_within_gap(
                    existing.original_year, parent_resolved.year
                )
            if parent_id is not None:
                graph.add_edge(CitationEdge(
                    source_id=parent_id, target_id=node_id,
                    context=parent_context, depth=depth,
                ))
            return

        # Keep the parsed bibliography on the node for the cross-citation pass.
        node.bibliography = parsed.bibliography

        graph.add_node(node)
        pdf_to_node_id[pdf_path] = node_id
        if parent_id is not None:
            graph.add_edge(CitationEdge(
                source_id=parent_id, target_id=node_id,
                context=parent_context, depth=depth,
            ))

        # Search each keyword; tag hits with their source keyword.
        hits_by_kw: dict[str, list] = {}
        all_hits = []
        for kw in keywords:
            kw_hits = keyword_matcher.search(
                parsed, kw, context_window=context_window,
                use_semantic=use_semantic,
                semantic_model=semantic_model,
                semantic_threshold=semantic_threshold,
            )
            for h in kw_hits:
                h.keyword = kw
            hits_by_kw[kw] = kw_hits
            all_hits.extend(kw_hits)

        # Keep `hits` bound for the rest of the function (used below for
        # collect_ref_keys and context_for_ref).
        hits = all_hits

        # Apply match mode: `any` = at least one keyword matched; `all` =
        # every keyword must have at least one hit.
        if match_mode == "all":
            matched = all(len(hits_by_kw[kw]) > 0 for kw in keywords)
        else:
            matched = len(all_hits) > 0

        node.keyword_hits = [h.passage for h in all_hits]
        node.keyword_hit_types = [h.match_type for h in all_hits]
        node.keyword_hit_scores = [h.semantic_score for h in all_hits]

        if not matched:
            if node.status != "root":
                node.status = "no_match"
            logger.info("[depth %d] %s: no keyword match", depth, _short(node.title))
            pbar.update(1)
            return

        if node.status != "root":
            node.status = "analyzed"
        logger.info("[depth %d] %s: %d hit(s)", depth, _short(node.title), len(all_hits))
        pbar.update(1)

        if depth >= max_depth:
            return

        # Resolve refs in parallel. Each resolve hits arxiv/S2/OpenReview
        # which are rate-limited via per-service locks in ReferenceResolver,
        # so concurrent resolves are safe and massively reduce total wait
        # time (when one resolve is sleeping on a throttle, the others can
        # still be making HTTP round-trips).
        ref_keys = keyword_matcher.collect_ref_keys(hits)
        pending = []
        for ref_key in ref_keys:
            bib = parsed.bibliography.get(ref_key)
            if bib is None:
                logger.debug("ref key %s not in bibliography", ref_key)
                continue
            pending.append((ref_key, bib, keyword_matcher.context_for_ref(hits, ref_key)))

        if not pending:
            return

        resolved_list = list(resolve_executor.map(
            lambda tup: resolver.resolve(tup[1]),
            pending,
        ))

        for (ref_key, _bib, ctx), resolved in zip(pending, resolved_list):
            # Prefer the OLDEST known year for a paper — but only if the
            # candidate is within a small window of the arxiv/S2 year.
            # GROBID's bib entry sometimes uses the v1 preprint year (the
            # case we want: Nie 2022 instead of 2023) but it can also
            # produce garbage years from raw-string parsing, which we
            # don't want to propagate.
            resolved.year = _older_within_gap(resolved.year, _bib.year)
            if resolved.pdf_path is None:
                if resolved.paper_id not in graph.nodes:
                    leaf = PaperNode(
                        paper_id=resolved.paper_id,
                        title=resolved.title,
                        authors=resolved.authors,
                        year=resolved.year,
                        publication_date=resolved.publication_date,
                        original_year=resolved.year,
                        arxiv_id=resolved.arxiv_id,
                        doi=resolved.doi,
                        abstract=resolved.abstract,
                        citation_count=resolved.citation_count,
                        status="unavailable",
                        depth=depth + 1,
                        url=resolved.url,
                    )
                    graph.add_node(leaf)
                graph.add_edge(CitationEdge(
                    source_id=node_id, target_id=resolved.paper_id,
                    context=ctx, depth=depth + 1,
                ))
            else:
                queue.append((resolved.pdf_path, depth + 1, node_id, ctx, resolved))

    # --- main loop with batched parallel parses --------------------------
    pbar = tqdm(desc="tracing", unit="paper")
    executor = ThreadPoolExecutor(
        max_workers=max(1, grobid_workers),
        thread_name_prefix="citracer-parse",
    )
    resolve_executor = ThreadPoolExecutor(
        max_workers=RESOLVE_DEFAULT_WORKERS,
        thread_name_prefix="citracer-resolve",
    )
    prev_handler = _install_sigint_handler()
    try:
        while queue:
            if _CANCEL_REQUESTED:
                break

            batch: list[QueueItem] = list(queue)
            queue.clear()

            # Deduplicate: only submit PDFs we haven't parsed yet, and dedupe
            # paths inside the same batch too.
            to_parse: list[Path] = []
            seen_in_batch: set[Path] = set()
            for item in batch:
                p = item[0]
                if p in pdf_to_node_id or p in seen_in_batch:
                    continue
                seen_in_batch.add(p)
                to_parse.append(p)

            parsed_by_path: dict[Path, ParsedPaper | None] = {}
            if to_parse and not _CANCEL_REQUESTED:
                futures = {
                    executor.submit(
                        pdf_parser.parse,
                        p,
                        grobid_url=grobid_url,
                        consolidate_citations=consolidate_citations,
                    ): p
                    for p in to_parse
                }
                for fut in as_completed(futures):
                    if _CANCEL_REQUESTED:
                        # Cancel anything that hasn't started yet; running
                        # futures finish on their own (we can't kill HTTP
                        # mid-request) but at least we don't pile on more.
                        for f in futures:
                            f.cancel()
                        break
                    p = futures[fut]
                    try:
                        parsed_by_path[p] = fut.result()
                    except Exception as e:
                        logger.error("Parse failed for %s: %s", p, e)
                        parsed_by_path[p] = None

            if _CANCEL_REQUESTED:
                break

            # Process every batch item sequentially, in BFS order, using the
            # parsed results (or None on parse failure / already-known paths).
            for item in batch:
                if _CANCEL_REQUESTED:
                    break
                _handle(item, parsed_by_path.get(item[0]))
    finally:
        # cancel_futures=True drops queued-but-not-started work immediately.
        # wait=False means we don't block waiting for in-flight workers.
        executor.shutdown(wait=False, cancel_futures=True)
        resolve_executor.shutdown(wait=False, cancel_futures=True)
        resolver.close()
        pbar.close()
        if prev_handler is not None:
            try:
                signal.signal(signal.SIGINT, prev_handler)
            except (ValueError, OSError):
                pass

    if _CANCEL_REQUESTED:
        logger.warning(
            "Trace interrupted: returning partial graph (%d nodes, %d edges)",
            len(graph.nodes), len(graph.edges),
        )

    # Always compute bibliographic-only cross-edges. This is cheap (no API
    # calls, just string + fuzzy comparisons over the in-memory graph) and
    # lets the HTML toggle them on/off without re-running the trace.
    n_added = add_secondary_edges(graph)
    if n_added:
        logger.info("Added %d secondary citation edge(s)", n_added)

    return graph


def trace_reverse(
    root_paper_id: str,
    root_metadata: dict,
    keyword: str | list[str],
    max_depth: int = 1,
    cache_dir: str | Path = "./cache",
    s2_api_key: str | None = None,
    match_mode: str = "any",
    per_level_limit: int = 500,
) -> TracerGraph:
    """Reverse citation trace.

    Instead of walking down from a root paper's bibliography, we walk UP
    from the root to the papers that cite it, keeping only those whose
    citation context mentions the keyword. This uses Semantic Scholar's
    ``/paper/{id}/citations`` endpoint, which returns 1-2 sentence
    snippets around the citation — so we filter locally without ever
    downloading a PDF.

    Args:
        root_paper_id: An S2-compatible id for the root paper. Accepted
            forms include ``ARXIV:2211.14730``, ``DOI:...``, or the S2
            ``paperId`` directly.
        root_metadata: Dict with keys ``title``, ``authors``, ``year``,
            ``arxiv_id``, ``doi`` for the root node.
        keyword: Keyword(s) to filter citation contexts by. Same format
            as ``trace()``.
        max_depth: Recursion depth. 1 means "direct citers only". Higher
            values are risky for popular papers; each level can multiply
            the size of the graph.
        per_level_limit: Hard cap on the number of citations fetched from
            S2 per paper per level. Prevents runaway expansion on papers
            with thousands of citations.
        match_mode: ``"any"`` (at least one keyword matched, default) or
            ``"all"`` (every keyword must appear in some context).
    """
    keywords: list[str] = [keyword] if isinstance(keyword, str) else list(keyword)
    if not keywords:
        raise ValueError("At least one keyword is required.")
    if match_mode not in ("any", "all"):
        raise ValueError(f"match_mode must be 'any' or 'all', got {match_mode!r}")

    patterns = [keyword_matcher.build_pattern(kw) for kw in keywords]

    graph = TracerGraph()
    resolver = ReferenceResolver(cache_dir=cache_dir, s2_api_key=s2_api_key)

    # Add the root node first.
    root_node = PaperNode(
        paper_id=root_metadata.get("paper_id") or root_paper_id,
        title=root_metadata.get("title") or "(unknown)",
        authors=root_metadata.get("authors") or [],
        year=root_metadata.get("year"),
        original_year=root_metadata.get("year"),
        arxiv_id=root_metadata.get("arxiv_id"),
        doi=root_metadata.get("doi"),
        abstract=root_metadata.get("abstract"),
        depth=0,
        status="root",
        url=root_metadata.get("url"),
    )
    graph.add_node(root_node)

    # BFS queue: (s2_lookup_id, graph_node_id, depth)
    queue: deque[tuple[str, str, int]] = deque()
    queue.append((root_paper_id, root_node.paper_id, 0))

    # Track added nodes to skip duplicate work. Keys are graph paper_id.
    visited_node_ids: set[str] = {root_node.paper_id}

    pbar = tqdm(desc="reverse-tracing", unit="paper")
    prev_handler = _install_sigint_handler()
    try:
        while queue:
            if _CANCEL_REQUESTED:
                break
            s2_id, current_node_id, depth = queue.popleft()
            if depth >= max_depth:
                continue

            citations = resolver.get_citations(s2_id, limit=per_level_limit)
            if not citations:
                continue

            for c in citations:
                if _CANCEL_REQUESTED:
                    break
                contexts = c.get("contexts") or []
                if not contexts:
                    continue  # no snippet, can't filter — skip

                # Which keywords match in the contexts?
                matched_contexts: list[str] = []
                matched_kws: set[str] = set()
                for ctx in contexts:
                    for pat, kw in zip(patterns, keywords):
                        if pat.search(ctx):
                            matched_contexts.append(ctx)
                            matched_kws.add(kw)
                            break  # one match per context is enough

                if match_mode == "all":
                    if len(matched_kws) < len(keywords):
                        continue
                else:
                    if not matched_contexts:
                        continue

                cp = c.get("citingPaper") or {}
                node_id, node = _node_from_s2_paper(cp, depth + 1, matched_contexts)
                if not node_id:
                    continue

                if node_id in visited_node_ids:
                    # Already in graph — add_edge deduplicates automatically.
                    graph.add_edge(CitationEdge(
                        source_id=node_id,
                        target_id=current_node_id,
                        context=matched_contexts[0] if matched_contexts else "",
                        depth=depth + 1,
                    ))
                    continue

                visited_node_ids.add(node_id)
                graph.add_node(node)
                graph.add_edge(CitationEdge(
                    source_id=node_id,
                    target_id=current_node_id,
                    context=matched_contexts[0] if matched_contexts else "",
                    depth=depth + 1,
                ))
                pbar.update(1)

                # Queue this node for the next level, if we're recursing.
                # S2 accepts its own paperId for the citations endpoint,
                # so we pass that through.
                s2_next = cp.get("paperId") or _s2_id_from_externals(cp)
                if s2_next and depth + 1 < max_depth:
                    queue.append((s2_next, node_id, depth + 1))
    finally:
        resolver.close()
        pbar.close()
        if prev_handler is not None:
            try:
                signal.signal(signal.SIGINT, prev_handler)
            except (ValueError, OSError):
                pass

    if _CANCEL_REQUESTED:
        logger.warning(
            "Reverse trace interrupted: returning partial graph (%d nodes, %d edges)",
            len(graph.nodes), len(graph.edges),
        )
    else:
        logger.info(
            "Reverse trace complete: %d nodes, %d edges",
            len(graph.nodes), len(graph.edges),
        )
    return graph


def _node_from_s2_paper(
    cp: dict,
    depth: int,
    keyword_hits: list[str],
) -> tuple[str | None, "PaperNode | None"]:
    """Build a PaperNode from a Semantic Scholar citingPaper dict.

    Returns ``(paper_id, node)``. Returns ``(None, None)`` if the S2
    record is too sparse to form a meaningful node.
    """
    ext = cp.get("externalIds") or {}
    arxiv_id = normalize_arxiv_id(ext.get("ArXiv"))
    doi = normalize_doi(ext.get("DOI"))
    title = cp.get("title")
    if not (title or arxiv_id or doi):
        return None, None

    paper_id = make_paper_id(doi=doi, arxiv_id=arxiv_id, title=title or "")
    year = cp.get("year")
    url = None
    if arxiv_id:
        url = f"https://arxiv.org/abs/{arxiv_id}"
    elif doi:
        url = f"https://doi.org/{doi}"

    node = PaperNode(
        paper_id=paper_id,
        title=title or "(unknown)",
        authors=[a.get("name") for a in (cp.get("authors") or []) if a.get("name")],
        year=year,
        publication_date=cp.get("publicationDate"),
        original_year=year,
        arxiv_id=arxiv_id,
        doi=doi,
        abstract=cp.get("abstract"),
        depth=depth,
        status="analyzed",  # keyword was matched in at least one citation context
        keyword_hits=keyword_hits,
        keyword_hit_types=["regex"] * len(keyword_hits),
        keyword_hit_scores=[0.0] * len(keyword_hits),
        url=url,
    )
    return paper_id, node


def _s2_id_from_externals(cp: dict) -> str | None:
    """Build an S2-compatible lookup id from a citingPaper dict when
    the paperId field is absent."""
    ext = cp.get("externalIds") or {}
    if ext.get("ArXiv"):
        return f"ARXIV:{ext['ArXiv']}"
    if ext.get("DOI"):
        return f"DOI:{ext['DOI']}"
    return None


def _short(s: str | None, n: int = 80) -> str:
    if not s:
        return "(untitled)"
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


_YEAR_GAP_THRESHOLD = YEAR_GAP_THRESHOLD


def _plausible(y: int | None) -> bool:
    if y is None:
        return False
    from datetime import date
    return 1970 <= y <= date.today().year + 1


def _older_within_gap(anchor: int | None, candidate: int | None) -> int | None:
    """Return the better of two candidate years for the same paper.

    Honours the oldest plausible year within ``_YEAR_GAP_THRESHOLD`` of
    the ``anchor`` (usually the first year we ever saw for this paper).
    Filters out obvious garbage (years outside [1970, current+1]) and
    rejects candidates too far below the anchor (likely parser errors).
    """
    if not _plausible(candidate):
        return anchor
    if not _plausible(anchor):
        return candidate
    if candidate >= anchor:
        return anchor
    if anchor - candidate > _YEAR_GAP_THRESHOLD:
        return anchor
    return candidate
