"""Typed shapes for external API payloads consumed by the reference resolver.

These TypedDicts document the subset of fields citracer actually reads from
each service, and give the IDE / type checker something to chew on in the
resolver. They are *not* strict runtime schemas — the resolver still has to
cope with missing or malformed fields defensively.
"""
from __future__ import annotations

from typing import TypedDict


class S2Author(TypedDict, total=False):
    name: str | None
    authorId: str | None


class S2ExternalIds(TypedDict, total=False):
    DOI: str | None
    ArXiv: str | None
    PubMed: str | None
    MAG: str | None


class S2OpenAccessPdf(TypedDict, total=False):
    url: str | None
    status: str | None


class S2Paper(TypedDict, total=False):
    """Shape of a single paper object returned by the Semantic Scholar
    /paper/{id} and /paper/search endpoints, for the fields we request
    via the `fields=` query parameter."""
    paperId: str | None
    title: str | None
    authors: list[S2Author]
    year: int | None
    abstract: str | None
    externalIds: S2ExternalIds
    openAccessPdf: S2OpenAccessPdf | None
    citationCount: int | None


class S2SearchResponse(TypedDict, total=False):
    data: list[S2Paper]
    total: int
    offset: int
    next: int


class NormalizedMeta(TypedDict, total=False):
    """Uniform shape we build internally after normalizing results from
    arXiv / S2 / OpenReview / OpenAlex. All optional; any subset may be
    present."""
    title: str | None
    authors: list[str]
    year: int | None
    abstract: str | None
    doi: str | None
    arxiv_id: str | None
    openreview_id: str | None
    citation_count: int | None
    open_access_url: str | None


class OpenReviewCandidate(TypedDict, total=False):
    """Minimal shape we extract from OpenReview's /notes/search v1/v2
    responses. v1 returns raw strings; v2 wraps fields as {'value': X}
    — `_orev_value()` unwraps both forms before we build this dict."""
    id: str
    title: str | None
    abstract: str | None
    authors: list[str]
