"""CLI entry point for citracer."""
from __future__ import annotations
import argparse
import logging
import os
import sys
import webbrowser
from pathlib import Path

import requests
from dotenv import load_dotenv

from . import pdf_parser, tracer, visualizer
from .constants import GROBID_DEFAULT_WORKERS
from .exporter import export_graph
from .reference_resolver import ReferenceResolver
from .source_resolver import resolve_source
from .utils import make_paper_id, setup_logging

# Load .env from CWD (or any parent dir) into os.environ. Silent if absent.
load_dotenv()

logger = logging.getLogger("citracer")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="citracer",
        description="Trace citation chains for a keyword across research papers.",
    )
    # Source input: one of --pdf / --doi / --arxiv / --url is required.
    src = p.add_argument_group("source (exactly one required)")
    src.add_argument("--pdf", help="Path to a local source PDF.")
    src.add_argument("--doi", help="DOI of the source paper (e.g. 10.48550/arxiv.2211.14730).")
    src.add_argument("--arxiv", help="arXiv ID of the source paper (e.g. 2211.14730).")
    src.add_argument("--url", help="URL of the source paper (arxiv.org, doi.org, or openreview.net).")

    p.add_argument(
        "--keyword",
        action="append",
        required=True,
        help="Keyword to trace. Repeat to trace multiple keywords at once "
             "(e.g. --keyword foo --keyword bar).",
    )
    p.add_argument(
        "--match-mode",
        choices=("any", "all"),
        default="any",
        help="In multi-keyword mode, 'any' (default) marks a paper as matched "
             "if at least one keyword appears in its text. 'all' requires "
             "every keyword to appear at least once.",
    )
    p.add_argument(
        "--depth",
        type=int,
        default=None,
        help="Max recursion depth. Default: 3 for forward trace, 1 for "
             "--reverse (since reverse can explode combinatorially).",
    )
    p.add_argument("--details", action="store_true", help="Show passages directly in node tooltips.")
    p.add_argument("--output", default="./output/graph.html", help="Output HTML file.")
    p.add_argument("--cache-dir", default="./cache", help="Local cache directory.")
    p.add_argument("--grobid-url", default="http://localhost:8070", help="GROBID service URL.")
    p.add_argument(
        "--s2-api-key",
        default=None,
        help="Semantic Scholar API key. If omitted, falls back to the "
             "S2_API_KEY environment variable (which can be set in a .env "
             "file at the project root). If neither is set, the public "
             "unauthenticated endpoint is used.",
    )
    p.add_argument("--context-window", type=int, default=None,
                   help="If set, fall back to a ±N char window around each "
                        "keyword for ref association. Default: sentence-based "
                        "(same sentence + next sentence).")
    p.add_argument(
        "--grobid-workers",
        type=int,
        default=GROBID_DEFAULT_WORKERS,
        help=f"Number of concurrent GROBID parse requests per BFS level "
             f"(default: {GROBID_DEFAULT_WORKERS}). Set to 1 to disable "
             f"parallelism.",
    )
    p.add_argument(
        "--consolidate",
        action="store_true",
        help="Ask GROBID to consolidate each bibliographic reference against "
             "CrossRef (more accurate titles/DOIs but ~2-5s extra per PDF).",
    )
    p.add_argument(
        "--reverse",
        action="store_true",
        help="Reverse trace: find papers that CITE the source paper while "
             "mentioning the keyword in their citation context. Uses "
             "Semantic Scholar's citation contexts, no PDF downloads. "
             "Default --depth is 1 in this mode.",
    )
    p.add_argument(
        "--reverse-limit",
        type=int,
        default=500,
        metavar="N",
        help="In reverse trace, max number of citations to fetch per level "
             "(default: 500). Protects against papers with thousands of "
             "citations.",
    )
    p.add_argument(
        "--export",
        action="append",
        default=[],
        metavar="PATH",
        help="Export the graph to a file. Format is derived from the "
             "extension: .json for the citracer JSON format, .graphml for "
             "the standard GraphML (Gephi, networkx, yEd). Repeat to "
             "export multiple formats in one run.",
    )
    p.add_argument("--no-open", action="store_true", help="Do not open the result in a browser.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)

    # Validate source: exactly one of --pdf / --doi / --arxiv / --url
    sources_given = [v for v in (args.pdf, args.doi, args.arxiv, args.url) if v]
    if len(sources_given) != 1:
        logger.error(
            "Exactly one of --pdf / --doi / --arxiv / --url must be provided."
        )
        return 2

    # Verify GROBID is reachable before starting. The pymupdf fallback exists
    # but produces much lower-quality output (author strings get parsed as
    # titles, etc.) so we surface this loudly and let the user opt in.
    if not _check_grobid(args.grobid_url):
        bar = "=" * 70
        logger.warning(bar)
        logger.warning("GROBID is not reachable at %s", args.grobid_url)
        logger.warning(bar)
        logger.warning("citracer needs GROBID for accurate bibliography parsing.")
        logger.warning("Start it with:")
        logger.warning("    docker run --rm -p 8070:8070 lfoppiano/grobid:0.9.0")
        logger.warning("")
        logger.warning("Without GROBID, citracer will fall back to pymupdf + regex,")
        logger.warning("which degrades quality significantly (references may be")
        logger.warning("mis-parsed, titles may show up as author lists, etc.).")
        logger.warning(bar)
        try:
            # `input()` is the one legitimate place we need stdin here.
            ans = input("  Continue with the fallback parser anyway? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            logger.error("Aborted by user (no GROBID).")
            return 3

    # Resolve the S2 API key: explicit CLI flag > env var (.env or shell) > None
    s2_key = args.s2_api_key or os.environ.get("S2_API_KEY") or None
    if s2_key:
        logger.debug("Using Semantic Scholar API key (source: %s)",
                     "CLI" if args.s2_api_key else "environment")

    # Resolve the root source into a local PDF path (download if needed).
    resolver = ReferenceResolver(cache_dir=args.cache_dir, s2_api_key=s2_key)
    try:
        pdf = resolve_source(
            pdf=args.pdf,
            doi=args.doi,
            arxiv_id=args.arxiv,
            url=args.url,
            resolver=resolver,
        )
    except ValueError as e:
        logger.error("Could not resolve source: %s", e)
        return 2

    keywords: list[str] = args.keyword
    kw_display = ", ".join(f"'{k}'" for k in keywords)

    # Mode-dependent default for --depth. argparse gives us None when the
    # user didn't pass it explicitly.
    if args.depth is None:
        depth = 1 if args.reverse else 3
    else:
        depth = args.depth

    if args.reverse:
        # Reverse trace: we need the root paper's S2-compatible id and
        # enough metadata for the root node, but we don't need the
        # bibliography or body text. Parse the PDF header only.
        depth = max(1, depth)
        if depth > 2:
            logger.warning(
                "Reverse trace at depth %d can explode combinatorially; "
                "consider --depth 1 or 2.", depth,
            )

        parsed = pdf_parser.parse(pdf, grobid_url=args.grobid_url)
        root_metadata = {
            "paper_id": make_paper_id(
                doi=parsed.doi, arxiv_id=parsed.arxiv_id,
                title=parsed.title or pdf.stem,
            ),
            "title": parsed.title or pdf.stem,
            "authors": parsed.authors,
            "year": parsed.year,
            "arxiv_id": parsed.arxiv_id,
            "doi": parsed.doi,
        }
        # Pick an S2 lookup id. Priority: explicit CLI arxiv/doi > parsed.
        s2_lookup_id: str | None = None
        if args.arxiv:
            s2_lookup_id = f"ARXIV:{args.arxiv}"
        elif args.doi:
            s2_lookup_id = f"DOI:{args.doi}"
        elif parsed.arxiv_id:
            s2_lookup_id = f"ARXIV:{parsed.arxiv_id}"
        elif parsed.doi:
            s2_lookup_id = f"DOI:{parsed.doi}"
        if not s2_lookup_id:
            logger.error(
                "Reverse trace needs a DOI or arXiv id on the root paper, "
                "but none was found. Provide --doi or --arxiv explicitly.",
            )
            return 4

        logger.info(
            "Reverse tracing keyword(s) %s from %s via S2 citations "
            "(depth=%d, match=%s, per-level-limit=%d)",
            kw_display, s2_lookup_id, depth, args.match_mode, args.reverse_limit,
        )
        graph = tracer.trace_reverse(
            root_paper_id=s2_lookup_id,
            root_metadata=root_metadata,
            keyword=keywords,
            max_depth=depth,
            cache_dir=args.cache_dir,
            s2_api_key=s2_key,
            match_mode=args.match_mode,
            per_level_limit=args.reverse_limit,
        )
    else:
        logger.info(
            "Tracing keyword(s) %s from %s (depth=%d, match=%s)",
            kw_display, pdf.name, depth, args.match_mode,
        )
        graph = tracer.trace(
            root_pdf=pdf,
            keyword=keywords,
            max_depth=depth,
            cache_dir=args.cache_dir,
            grobid_url=args.grobid_url,
            context_window=args.context_window,
            s2_api_key=s2_key,
            grobid_workers=args.grobid_workers,
            consolidate_citations=args.consolidate,
            match_mode=args.match_mode,
        )

    logger.info("Graph: %d nodes, %d edges", len(graph.nodes), len(graph.edges))
    out_path = visualizer.render(
        graph,
        output=args.output,
        keyword=keywords,
        show_details=args.details,
    )
    logger.info("Wrote %s", out_path)

    # Optional graph exports (JSON / GraphML).
    for export_path in args.export or []:
        try:
            export_graph(graph, export_path)
        except Exception as e:
            logger.error("Export to %s failed: %s", export_path, e)

    if not args.no_open:
        webbrowser.open(out_path.resolve().as_uri())
    return 0


def _check_grobid(grobid_url: str, timeout: float = 3.0) -> bool:
    """Return True iff GROBID's /api/isalive returns 200 within the timeout."""
    try:
        r = requests.get(f"{grobid_url.rstrip('/')}/api/isalive", timeout=timeout)
    except Exception:
        return False
    return r.status_code == 200 and b"true" in r.content.lower()


if __name__ == "__main__":
    sys.exit(main())
