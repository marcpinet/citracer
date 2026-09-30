"""Reproducibility manifest for citracer traces.

Every trace generates a manifest.json alongside the graph output, encoding
all parameters needed to reproduce the exact same graph. This is a
prerequisite for academic credibility: anyone receiving a citracer graph
can re-run the trace with identical settings.
"""
from __future__ import annotations

import json
import logging
import platform
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .models import TracerGraph

logger = logging.getLogger(__name__)


#: CLI flags whose value must never be written to disk.
_SECRET_FLAGS = ("--s2-api-key", "--email", "--zotero-api-key")


def redact_argv(argv: list[str]) -> list[str]:
    """Copy of ``argv`` with the values of secret flags replaced by ***.

    Handles both ``--flag value`` and ``--flag=value`` forms, plus the
    unambiguous prefixes argparse accepts (``--s2-api``)."""
    out: list[str] = []
    redact_next = False
    for arg in argv:
        if redact_next:
            out.append("***")
            redact_next = False
            continue
        name, eq, _value = arg.partition("=")
        is_secret = len(name) > 3 and any(f.startswith(name) for f in _SECRET_FLAGS)
        if is_secret and eq:
            out.append(f"{name}=***")
        elif is_secret:
            out.append(arg)
            redact_next = True
        else:
            out.append(arg)
    return out


def _citracer_version() -> str:
    """Return the installed citracer version, with fallback."""
    try:
        from importlib.metadata import version
        return version("citracer")
    except Exception:
        return "unknown"


def build_manifest(
    *,
    args,
    graph: TracerGraph,
    root_source: dict,
    grobid_available: bool | None,
    s2_key_set: bool,
    email_set: bool,
    depth: int,
    analytics: dict | None = None,
) -> dict:
    """Build a reproducibility manifest from the trace parameters and results.

    Parameters
    ----------
    args : argparse.Namespace
        The parsed CLI arguments.
    graph : TracerGraph
        The completed graph.
    root_source : dict
        ``{"type": "arxiv"|"doi"|"pdf"|"url", "value": "<raw input>"}``.
    grobid_available : bool or None
        Whether GROBID was reachable at trace time (None: not needed,
        e.g. a reverse trace resolved entirely through Semantic Scholar).
    s2_key_set : bool
        Whether a Semantic Scholar API key was configured.
    email_set : bool
        Whether an OpenAlex email was configured.
    depth : int
        The effective depth used (after mode-dependent default).
    """
    # Root paper metadata from graph
    root_nodes = [n for n in graph.nodes.values() if n.status == "root"]
    root_meta = {}
    if root_nodes:
        r = root_nodes[0]
        root_meta = {
            "title": r.title,
            "doi": r.doi,
            "arxiv_id": r.arxiv_id,
        }

    # Status breakdown
    status_counts = Counter(n.status for n in graph.nodes.values())

    # Reconstruct command (secrets such as the S2 API key are masked: the
    # manifest is written next to the graph and embedded in JSON exports).
    command = " ".join(redact_argv(sys.argv))

    return {
        "citracer_version": _citracer_version(),
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
        "source": {
            **root_source,
            **{k: v for k, v in root_meta.items() if v},
        },
        "parameters": {
            "keywords": list(args.keyword),
            "match_mode": args.match_mode,
            "depth": depth,
            "context_window": args.context_window,
            "consolidate": getattr(args, "consolidate", False),
            "reverse": getattr(args, "reverse", False),
            "reverse_limit": getattr(args, "reverse_limit", 500),
            "enrich": getattr(args, "enrich", False),
            "no_refetch": getattr(args, "no_refetch", False),
            "grobid_url": args.grobid_url,
        },
        "environment": {
            "python_version": platform.python_version(),
            "platform": sys.platform,
            "grobid_available": grobid_available,
            "s2_api_key_set": s2_key_set,
            "email_set": email_set,
        },
        "results": {
            "nodes": len(graph.nodes),
            "edges": len(graph.edges),
            "by_status": dict(status_counts),
            "analytics": {
                "global": analytics.get("global", {}) if analytics else {},
                "timeline": analytics.get("timeline", []) if analytics else [],
                "pivot_papers": analytics.get("pivot_papers", []) if analytics else [],
            },
        },
        "command": command,
    }


def save_manifest(manifest: dict, output_dir: str | Path) -> Path:
    """Write the manifest to ``output_dir/manifest.json``."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "manifest.json"
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Wrote reproducibility manifest to %s", path)
    return path
