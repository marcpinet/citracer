"""Recursive citation tracer.

The tracer runs a breadth-first walk over the citation graph as a
streaming pipeline:

* **parse workers** (``grobid_workers`` threads) send a PDF to GROBID, run
  the keyword matcher on it, and — when the paper matched and will be
  expanded — immediately hand its cited references to the resolve pool;
* **resolve workers** resolve each reference once (deduplicated) and, as
  soon as a PDF is available, submit it to the parse pool;
* the **main thread** consumes those results in strict BFS order and is the
  only one mutating the graph, so depths and deduplication are exactly the
  same as a level-by-level walk, while GROBID, the matcher and the
  metadata APIs all work concurrently instead of waiting on each other.
"""
from __future__ import annotations
import logging
import signal
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import replace
from pathlib import Path

from tqdm import tqdm

from . import keyword_matcher, pdf_parser
from .constants import GROBID_DEFAULT_WORKERS, YEAR_GAP_THRESHOLD
from .cross_citation import _better_year, add_secondary_edges
from .models import BibEntry, CitationEdge, KeywordHit, PaperNode, ParsedPaper, TracerGraph
from .reference_resolver import ReferenceResolver, ResolvedRef
from .utils import make_paper_id, normalize_arxiv_id, normalize_doi, plausible_year

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


def _restore_sigint_handler(prev_handler) -> None:
    if prev_handler is not None:
        try:
            signal.signal(signal.SIGINT, prev_handler)
        except (ValueError, OSError):
            pass


# Re-exported for backwards compatibility with `from .tracer import add_secondary_edges`.
__all__ = ["trace", "trace_reverse", "add_secondary_edges"]

# Queue item shape: (pdf_path, depth, parent_id, parent_context, parent_resolved)
QueueItem = tuple[Path, int, "str | None", str, "ResolvedRef | None"]

# Result of a parse job: the parsed paper and its hits per keyword.
ParseResult = tuple[ParsedPaper, dict[str, list[KeywordHit]]]


def _bib_key(bib: BibEntry) -> str:
    return make_paper_id(doi=bib.doi, arxiv_id=bib.arxiv_id, title=bib.title or bib.raw)


def _wait(fut: Future, label: str):
    """Block on ``fut`` while staying responsive to Ctrl+C. Returns None on
    cancellation or if the job raised."""
    while True:
        if _CANCEL_REQUESTED:
            return None
        try:
            return fut.result(timeout=0.5)
        except FutureTimeout:
            continue
        except Exception as e:
            logger.error("%s failed: %s", label, e)
            return None


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
    no_refetch: bool = False,
    use_semantic: bool = False,
    semantic_model: str | None = None,
    semantic_threshold: float | None = None,
    resolver: ReferenceResolver | None = None,
) -> TracerGraph:
    """Forward trace from ``root_pdf``.

    Pass an existing ``resolver`` to share its caches, in-run memo and
    circuit-breaker state with the caller (it is then left open).
    """
    # Normalize: always work with a list of keywords internally.
    keywords: list[str] = [keyword] if isinstance(keyword, str) else list(keyword)
    if not keywords:
        raise ValueError("At least one keyword is required.")
    if match_mode not in ("any", "all"):
        raise ValueError(f"match_mode must be 'any' or 'all', got {match_mode!r}")

    graph = TracerGraph()
    own_resolver = resolver is None
    if resolver is None:
        resolver = ReferenceResolver(
            cache_dir=cache_dir,
            s2_api_key=s2_api_key,
            supplied_pdfs=supplied_pdfs,
            enrich=enrich,
            email=email,
            no_refetch=no_refetch,
        )

    def _matched(hits_by_kw: dict[str, list[KeywordHit]]) -> bool:
        # `any` = at least one keyword matched; `all` = every keyword must
        # have at least one hit.
        if match_mode == "all":
            return all(hits_by_kw.get(kw) for kw in keywords)
        return any(hits_by_kw.get(kw) for kw in keywords)

    def _refs_to_follow(parsed: ParsedPaper, hits: list[KeywordHit]) -> list[tuple[BibEntry, str]]:
        out = []
        for ref_key in keyword_matcher.collect_ref_keys(hits):
            bib = parsed.bibliography.get(ref_key)
            if bib is None:
                logger.debug("ref key %s not in bibliography", ref_key)
                continue
            out.append((bib, keyword_matcher.context_for_ref(hits, ref_key)))
        return out

    def _existing_for_bib(bib: BibEntry) -> str | None:
        """Graph node already known for a bib entry by DOI / arXiv id."""
        if not (bib.doi or bib.arxiv_id):
            return None
        return graph.find(doi=bib.doi, arxiv_id=bib.arxiv_id)

    # --- worker-side jobs -----------------------------------------------
    parse_futures: dict[Path, Future] = {}
    # Paths already handled by the main thread. Their future is dropped so
    # the parsed text and hits can be garbage-collected (only the node's
    # bibliography is kept, for the cross-citation pass).
    done_paths: set[Path] = set()
    resolve_futures: dict[str, Future] = {}
    futures_lock = threading.Lock()

    def submit_parse(path: Path, depth: int) -> Future | None:
        with futures_lock:
            if path in done_paths:
                return None
            fut = parse_futures.get(path)
            if fut is None:
                try:
                    fut = parse_executor.submit(_parse_job, path, depth)
                except RuntimeError:  # executor shut down (cancellation)
                    return None
                parse_futures[path] = fut
            return fut

    def submit_resolve(bib: BibEntry, child_depth: int) -> Future | None:
        key = _bib_key(bib)
        with futures_lock:
            fut = resolve_futures.get(key)
            if fut is None:
                try:
                    fut = resolve_executor.submit(_resolve_job, bib, child_depth)
                except RuntimeError:
                    return None
                resolve_futures[key] = fut
            return fut

    def _parse_job(path: Path, depth: int) -> ParseResult:
        parsed = pdf_parser.parse(
            path,
            grobid_url=grobid_url,
            consolidate_citations=consolidate_citations,
            # Only the root's own metadata comes from its header; every
            # other node gets it from the resolver.
            consolidate_header=depth == 0,
            cache_dir=cache_dir,
        )
        hits_by_kw = keyword_matcher.search_all(
            parsed, keywords,
            context_window=context_window,
            use_semantic=use_semantic,
            semantic_model=semantic_model,
            semantic_threshold=semantic_threshold,
            cache_dir=cache_dir,
        )
        # Speculatively start resolving the refs this paper will need.
        # ``depth`` is an upper bound of the BFS depth (it comes from the
        # first parent that reached it), so if it's below max_depth the
        # paper will definitely be expanded.
        if depth < max_depth and _matched(hits_by_kw) and not _CANCEL_REQUESTED:
            all_hits = [h for kw in keywords for h in hits_by_kw.get(kw, [])]
            bibs = [b for b, _ctx in _refs_to_follow(parsed, all_hits)
                    if _existing_for_bib(b) is None]
            if bibs:
                try:
                    resolve_executor.submit(_warm_job, bibs, depth + 1)
                except RuntimeError:
                    pass
        return parsed, hits_by_kw

    def _warm_job(bibs: list[BibEntry], child_depth: int) -> None:
        prefetch = getattr(resolver, "prefetch", None)
        if prefetch is not None:
            prefetch(bibs)  # one S2 batch call for all of them
        for bib in bibs:
            submit_resolve(bib, child_depth)

    def _resolve_job(bib: BibEntry, child_depth: int) -> ResolvedRef:
        r = resolver.resolve(bib)
        if r.pdf_path is not None and not _CANCEL_REQUESTED and graph.find(
            paper_id=r.paper_id, doi=r.doi, arxiv_id=r.arxiv_id,
        ) is None:
            submit_parse(r.pdf_path, child_depth)
        return r

    # --- main-thread graph construction ------------------------------------
    # Map a PDF we've already handled to the node_id we created for it,
    # so a second incoming edge can be wired up without re-parsing.
    pdf_to_node_id: dict[Path, str] = {}

    def _link_existing(existing_id: str, item: QueueItem) -> None:
        _pdf, depth, parent_id, parent_context, parent_resolved = item
        existing = graph.nodes[existing_id]
        # Backfill the existing node's year, anchored on its first-seen
        # year so that repeated updates don't cascade away from truth.
        if parent_resolved is not None:
            existing.year = _better_year(
                existing.original_year, existing.year, parent_resolved.year,
            )
            graph.absorb(existing_id, **_ref_fields(parent_resolved))
        if parent_id is not None:
            graph.add_edge(CitationEdge(
                source_id=parent_id, target_id=existing_id,
                context=parent_context, depth=depth,
            ))

    def _handle(item: QueueItem, result: ParseResult | None):
        """Add the paper to the graph. Returns (node_id, depth, refs) when
        its references must be followed, else None."""
        pdf_path, depth, parent_id, parent_context, parent_resolved = item

        # Fast path: this PDF was handled already.
        if pdf_path in pdf_to_node_id:
            _link_existing(pdf_to_node_id[pdf_path], item)
            return None

        if result is None:
            return None  # parse failed for this path, already logged
        parsed, hits_by_kw = result

        # Build node identity
        if parent_resolved is not None:
            node = PaperNode(
                paper_id=parent_resolved.paper_id,
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
            # Enrich the root node with S2 metadata (publication_date,
            # abstract, citation_count, url) that GROBID doesn't provide.
            root_s2 = None
            s2_by_id = getattr(resolver, "s2_by_id", None)
            if s2_by_id is not None:
                if parsed.doi:
                    root_s2 = s2_by_id(f"DOI:{parsed.doi}")
                if root_s2 is None and parsed.arxiv_id:
                    root_s2 = s2_by_id(f"ARXIV:{parsed.arxiv_id}")
            node = PaperNode(
                paper_id=make_paper_id(
                    doi=parsed.doi,
                    arxiv_id=parsed.arxiv_id,
                    title=parsed.title or pdf_path.stem,
                ),
                title=parsed.title or pdf_path.stem,
                authors=parsed.authors,
                year=parsed.year,
                publication_date=root_s2.get("publication_date") if root_s2 else None,
                original_year=parsed.year,
                arxiv_id=parsed.arxiv_id,
                doi=parsed.doi,
                abstract=root_s2.get("abstract") if root_s2 else None,
                citation_count=root_s2.get("citation_count") if root_s2 else None,
                depth=depth,
                status="root",
                url=(f"https://arxiv.org/abs/{parsed.arxiv_id}" if parsed.arxiv_id
                     else f"https://doi.org/{parsed.doi}" if parsed.doi
                     else None),
            )
        node_id = node.paper_id

        existing_id = graph.find(
            paper_id=node_id, doi=node.doi, arxiv_id=node.arxiv_id,
            title=node.title, year=node.year,
        )
        if existing_id is not None:
            # Same paper already in the graph (reached via a different PDF
            # path or under another identifier).
            pdf_to_node_id[pdf_path] = existing_id
            _link_existing(existing_id, item)
            if graph.nodes[existing_id].status != "unavailable":
                return None
            # It was a dead end so far (no PDF found through another
            # citation): now that we have its text, analyze it in place.
            node = graph.nodes[existing_id]
            node_id = existing_id
        else:
            graph.add_node(node)
            pdf_to_node_id[pdf_path] = node_id
            if parent_id is not None:
                graph.add_edge(CitationEdge(
                    source_id=parent_id, target_id=node_id,
                    context=parent_context, depth=depth,
                ))

        # Keep the parsed bibliography on the node for the cross-citation pass.
        node.bibliography = parsed.bibliography

        # Hits of every keyword. A sentence matching several keywords is
        # shown once.
        all_hits: list[KeywordHit] = []
        seen_passages: set[str] = set()
        for kw in keywords:
            for h in hits_by_kw.get(kw, []):
                if h.passage in seen_passages:
                    continue
                seen_passages.add(h.passage)
                all_hits.append(h)
        node.keyword_hits = [h.passage for h in all_hits]
        node.keyword_hit_types = [h.match_type for h in all_hits]
        node.keyword_hit_scores = [h.semantic_score for h in all_hits]

        if not _matched(hits_by_kw):
            if node.status != "root":
                node.status = "no_match"
            logger.info("[depth %d] %s: no keyword match", depth, _short(node.title))
            pbar.update(1)
            return None

        if node.status != "root":
            node.status = "analyzed"
        logger.info("[depth %d] %s: %d hit(s)", depth, _short(node.title), len(all_hits))
        pbar.update(1)

        if depth >= max_depth:
            return None
        # ref association uses the hits of every keyword, duplicates included
        hits = [h for kw in keywords for h in hits_by_kw.get(kw, [])]
        refs = _refs_to_follow(parsed, hits)
        return (node_id, depth, refs) if refs else None

    # --- main loop ---------------------------------------------------------
    pbar = tqdm(desc="tracing", unit="paper")
    parse_executor = ThreadPoolExecutor(
        max_workers=max(1, grobid_workers),
        thread_name_prefix="citracer-parse",
    )
    resolve_executor = ThreadPoolExecutor(
        max_workers=RESOLVE_DEFAULT_WORKERS,
        thread_name_prefix="citracer-resolve",
    )
    prev_handler = _install_sigint_handler()
    try:
        level: list[QueueItem] = [(Path(root_pdf), 0, None, "", None)]
        submit_parse(Path(root_pdf), 0)
        while level and not _CANCEL_REQUESTED:
            # 1. Handle every paper of this level, in BFS order.
            expansions = []
            for item in level:
                if _CANCEL_REQUESTED:
                    break
                result = None
                if item[0] not in pdf_to_node_id:
                    fut = submit_parse(item[0], item[1])
                    if fut is not None:
                        result = _wait(fut, f"Parse of {item[0]}")
                    with futures_lock:
                        done_paths.add(item[0])
                        parse_futures.pop(item[0], None)
                exp = _handle(item, result)
                del result
                if exp is not None:
                    expansions.append(exp)
            if _CANCEL_REQUESTED:
                break

            # 2. Resolve their references (most were already started by
            #    the parse workers) — each unique reference once.
            links: list[tuple[str, int, BibEntry, str, str | None]] = []
            for node_id, depth, refs in expansions:
                for bib, ctx in refs:
                    existing = _existing_for_bib(bib)
                    if existing is None:
                        submit_resolve(bib, depth + 1)
                    links.append((node_id, depth, bib, ctx, existing))
            resolved_by_key: dict[str, ResolvedRef] = {}
            for _nid, _d, bib, _ctx, existing in links:
                key = _bib_key(bib)
                if existing is not None or key in resolved_by_key:
                    continue
                fut = resolve_futures.get(key)
                r = _wait(fut, f"Resolve of {bib.title or bib.key!r}") if fut else None
                if r is not None:
                    resolved_by_key[key] = r
            if _CANCEL_REQUESTED:
                break

            # Batch-enrich every resolved ref of the level at once (S2
            # batch + OpenAlex: 1 request per 500 / 50 papers).
            resolver.batch_enrich(list(resolved_by_key.values()))

            # 3. Wire up the results, still in BFS order.
            next_level: list[QueueItem] = []
            for node_id, depth, bib, ctx, existing in links:
                if existing is None:
                    shared = resolved_by_key.get(_bib_key(bib))
                    if shared is None:
                        continue  # resolve failed, already logged
                    # Copy: the same resolution may serve several parents.
                    resolved = replace(shared, authors=list(shared.authors))
                    # Prefer the OLDEST known year for a paper — but only if
                    # the candidate is within a small window of the arxiv/S2
                    # year. GROBID's bib entry sometimes uses the v1 preprint
                    # year (the case we want: Nie 2022 instead of 2023) but
                    # it can also produce garbage years from raw-string
                    # parsing, which we don't want to propagate.
                    resolved.year = _older_within_gap(resolved.year, bib.year)
                    existing = graph.find(
                        paper_id=resolved.paper_id, doi=resolved.doi,
                        arxiv_id=resolved.arxiv_id, title=resolved.title,
                        year=resolved.year,
                    )
                    if existing is None and resolved.pdf_path is not None:
                        next_level.append((resolved.pdf_path, depth + 1, node_id, ctx, resolved))
                        continue
                    if existing is None:
                        graph.add_node(PaperNode(
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
                        ))
                        existing = resolved.paper_id
                    else:
                        _link_existing(existing, (Path(), depth + 1, None, ctx, resolved))
                else:
                    target = graph.nodes[existing]
                    target.year = _better_year(target.original_year, target.year, bib.year)
                graph.add_edge(CitationEdge(
                    source_id=node_id, target_id=existing,
                    context=ctx, depth=depth + 1,
                ))
            level = next_level
    finally:
        # cancel_futures=True drops queued-but-not-started work immediately.
        # wait=False means we don't block waiting for in-flight workers.
        parse_executor.shutdown(wait=False, cancel_futures=True)
        resolve_executor.shutdown(wait=False, cancel_futures=True)
        if own_resolver:
            resolver.close()
        pbar.close()
        _restore_sigint_handler(prev_handler)

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


def _ref_fields(r: ResolvedRef) -> dict:
    return {
        "paper_id": r.paper_id, "doi": r.doi, "arxiv_id": r.arxiv_id,
        "title": r.title, "authors": r.authors, "abstract": r.abstract,
        "citation_count": r.citation_count,
        "publication_date": r.publication_date, "url": r.url,
    }


def trace_reverse(
    root_paper_id: str,
    root_metadata: dict,
    keyword: str | list[str],
    max_depth: int = 1,
    cache_dir: str | Path = "./cache",
    s2_api_key: str | None = None,
    match_mode: str = "any",
    per_level_limit: int = 500,
    resolver: ReferenceResolver | None = None,
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
        resolver: Optional shared resolver (left open if given).
    """
    keywords: list[str] = [keyword] if isinstance(keyword, str) else list(keyword)
    if not keywords:
        raise ValueError("At least one keyword is required.")
    if match_mode not in ("any", "all"):
        raise ValueError(f"match_mode must be 'any' or 'all', got {match_mode!r}")

    patterns = [keyword_matcher.build_pattern(kw) for kw in keywords]

    graph = TracerGraph()
    own_resolver = resolver is None
    if resolver is None:
        resolver = ReferenceResolver(cache_dir=cache_dir, s2_api_key=s2_api_key)

    # Add the root node first.
    root_node = PaperNode(
        paper_id=root_metadata.get("paper_id") or root_paper_id,
        title=root_metadata.get("title") or "(unknown)",
        authors=root_metadata.get("authors") or [],
        year=root_metadata.get("year"),
        publication_date=root_metadata.get("publication_date"),
        original_year=root_metadata.get("year"),
        arxiv_id=root_metadata.get("arxiv_id"),
        doi=root_metadata.get("doi"),
        abstract=root_metadata.get("abstract"),
        citation_count=root_metadata.get("citation_count"),
        depth=0,
        status="root",
        url=root_metadata.get("url"),
    )
    graph.add_node(root_node)

    # BFS queue: (s2_lookup_id, graph_node_id, depth)
    queue: list[tuple[str, str, int]] = [(root_paper_id, root_node.paper_id, 0)]
    n_no_context = 0

    pbar = tqdm(desc="reverse-tracing", unit="paper")
    prev_handler = _install_sigint_handler()
    try:
        while queue:
            if _CANCEL_REQUESTED:
                break
            s2_id, current_node_id, depth = queue.pop(0)
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
                    n_no_context += 1
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

                edge_ctx = matched_contexts[0] if matched_contexts else ""
                existing = graph.find(
                    paper_id=node_id, doi=node.doi, arxiv_id=node.arxiv_id,
                    title=node.title, year=node.year,
                )
                if existing is not None:
                    # Already in graph — add_edge deduplicates automatically.
                    graph.add_edge(CitationEdge(
                        source_id=existing,
                        target_id=current_node_id,
                        context=edge_ctx,
                        depth=depth + 1,
                    ))
                    continue

                graph.add_node(node)
                graph.add_edge(CitationEdge(
                    source_id=node_id,
                    target_id=current_node_id,
                    context=edge_ctx,
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
        if own_resolver:
            resolver.close()
        pbar.close()
        _restore_sigint_handler(prev_handler)

    if n_no_context:
        logger.info(
            "%d citing paper(s) skipped: S2 has no citation context for them",
            n_no_context,
        )
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


def _older_within_gap(anchor: int | None, candidate: int | None) -> int | None:
    """Return the better of two candidate years for the same paper.

    Honours the oldest plausible year within ``_YEAR_GAP_THRESHOLD`` of
    the ``anchor`` (usually the first year we ever saw for this paper).
    Filters out obvious garbage (implausible years) and rejects candidates
    too far below the anchor (likely parser errors).
    """
    if not plausible_year(candidate):
        return anchor
    if not plausible_year(anchor):
        return candidate
    if candidate >= anchor:
        return anchor
    if anchor - candidate > _YEAR_GAP_THRESHOLD:
        return anchor
    return candidate
