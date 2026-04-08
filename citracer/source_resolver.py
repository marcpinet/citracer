"""Resolve a high-level "source" argument (DOI, arXiv id, URL, or local
path) into a local PDF file.

This lets users say ``--doi 10.48550/arxiv.2211.14730`` or
``--arxiv 2211.14730`` instead of manually downloading the root paper before
running citracer. Local-path input via ``--pdf`` is still supported.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import requests

from .constants import PDF_DOWNLOAD_TIMEOUT_SECONDS
from .reference_resolver import ReferenceResolver
from .utils import normalize_arxiv_id, normalize_doi

logger = logging.getLogger(__name__)

_ARXIV_URL_RE = re.compile(
    r"^https?://arxiv\.org/(?:abs|pdf)/(?P<id>[\w./-]+?)(?:v\d+)?(?:\.pdf)?/?$",
    re.IGNORECASE,
)
_DOI_URL_RE = re.compile(
    r"^https?://(?:dx\.)?doi\.org/(?P<doi>.+)$",
    re.IGNORECASE,
)
_OPENREVIEW_URL_RE = re.compile(
    r"^https?://openreview\.net/(?:forum|pdf)\?id=(?P<id>[\w-]+)",
    re.IGNORECASE,
)


def resolve_source(
    pdf: str | None,
    doi: str | None,
    arxiv_id: str | None,
    url: str | None,
    resolver: ReferenceResolver,
) -> Path:
    """Return a local Path to the root PDF, downloading if necessary.

    Exactly one of the four inputs must be provided. Raises ValueError
    otherwise or if the source cannot be resolved to a downloadable PDF.
    """
    provided = [x for x in (pdf, doi, arxiv_id, url) if x]
    if len(provided) != 1:
        raise ValueError(
            "Exactly one of --pdf / --doi / --arxiv / --url must be provided."
        )

    if pdf:
        p = Path(pdf).expanduser()
        if not p.exists():
            raise ValueError(f"PDF not found: {p}")
        return p

    if url:
        # Re-route a URL into the appropriate typed branch.
        m = _ARXIV_URL_RE.match(url.strip())
        if m:
            arxiv_id = m.group("id")
        else:
            m = _DOI_URL_RE.match(url.strip())
            if m:
                doi = m.group("doi")
            else:
                m = _OPENREVIEW_URL_RE.match(url.strip())
                if m:
                    return _download_openreview(m.group("id"), resolver)
                else:
                    raise ValueError(
                        f"Unrecognised URL (expected arxiv.org / doi.org / "
                        f"openreview.net): {url}"
                    )

    if arxiv_id:
        aid = normalize_arxiv_id(arxiv_id)
        if not aid:
            raise ValueError(f"Invalid arXiv id: {arxiv_id!r}")
        logger.info("Resolving root from arXiv:%s", aid)
        path = resolver._download_arxiv(aid)  # type: ignore[attr-defined]
        if not path:
            raise ValueError(f"Could not download arXiv paper {aid}")
        return path

    if doi:
        d = normalize_doi(doi)
        if not d:
            raise ValueError(f"Invalid DOI: {doi!r}")
        # Special case: arxiv DOIs map directly to arxiv IDs.
        m = re.match(r"10\.48550/arxiv\.(.+)", d, re.IGNORECASE)
        if m:
            aid = normalize_arxiv_id(m.group(1))
            logger.info("DOI %s is an arXiv DOI, routing to arxiv:%s", d, aid)
            path = resolver._download_arxiv(aid)  # type: ignore[attr-defined]
            if not path:
                raise ValueError(f"Could not download arXiv paper {aid}")
            return path

        # General DOI: ask Semantic Scholar for the externalIds, then try
        # arxiv download if an ArXiv id is known. This reuses the existing
        # resolver pipeline without duplicating logic.
        logger.info("Looking up DOI %s via Semantic Scholar", d)
        meta = resolver._s2_by_id(f"DOI:{d}")  # type: ignore[attr-defined]
        if meta and meta.get("arxiv_id"):
            path = resolver._download_arxiv(meta["arxiv_id"])  # type: ignore[attr-defined]
            if path:
                return path
        raise ValueError(
            f"Could not find an open-access PDF for DOI {d}. "
            "citracer can only trace papers available on arXiv or OpenReview."
        )

    # Unreachable — the "exactly one" check above catches this.
    raise ValueError("No source provided.")


def _download_openreview(forum_id: str, resolver: ReferenceResolver) -> Path:
    """Fetch a root PDF from OpenReview using the shared resolver's helper."""
    path = resolver._download_openreview(forum_id)  # type: ignore[attr-defined]
    if path:
        return path
    raise ValueError(f"Could not download OpenReview paper {forum_id}")
