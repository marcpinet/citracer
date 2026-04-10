"""Metadata enrichment via OpenAlex for papers missing abstract / citation count.

OpenAlex is free, requires no API key, and returns rich metadata including
title, authors, year, abstract (via abstract_inverted_index), citation count,
and open-access URLs.

Providing an email enables the "polite pool" (10 req/s vs 1 req/s anonymous).
"""
from __future__ import annotations

import logging
import threading
import time

import requests
from rapidfuzz import fuzz

from .api_types import NormalizedMeta
from .constants import (
    OPENALEX_MIN_INTERVAL_WITH_EMAIL,
    OPENALEX_MIN_INTERVAL_WITHOUT_EMAIL,
    OPENALEX_TIMEOUT_SECONDS,
)
from .metadata_cache import MetadataCache
from .utils import normalize_doi, normalize_title

logger = logging.getLogger(__name__)

OPENALEX_BASE = "https://api.openalex.org"


def _reconstruct_abstract(inverted_index: dict) -> str:
    """Reconstruct an abstract from OpenAlex's abstract_inverted_index format.

    The inverted index maps each word to a list of integer positions where
    it appears. We rebuild the original text by placing words at their
    positions.
    """
    if not inverted_index:
        return ""
    max_pos = max(pos for positions in inverted_index.values() for pos in positions)
    words = [""] * (max_pos + 1)
    for word, positions in inverted_index.items():
        for pos in positions:
            words[pos] = word
    return " ".join(w for w in words if w)


class MetadataEnricher:
    def __init__(
        self,
        cache: MetadataCache,
        email: str | None = None,
    ) -> None:
        self.cache = cache
        self.email = email
        self._min_interval = (
            OPENALEX_MIN_INTERVAL_WITH_EMAIL
            if email
            else OPENALEX_MIN_INTERVAL_WITHOUT_EMAIL
        )
        self._last_call = 0.0
        self._lock = threading.Lock()

    def _throttle(self) -> None:
        with self._lock:
            now = time.time()
            delta = now - self._last_call
            if delta < self._min_interval:
                time.sleep(self._min_interval - delta)
            self._last_call = time.time()

    def _params(self) -> dict:
        p: dict = {}
        if self.email:
            p["mailto"] = self.email
        return p

    def _get(self, url: str, label: str) -> dict | None:
        self._throttle()
        try:
            r = requests.get(
                url,
                params=self._params(),
                headers={"User-Agent": "citracer"},
                timeout=OPENALEX_TIMEOUT_SECONDS,
            )
        except Exception as e:
            logger.warning("OpenAlex %s failed: %s", label, e)
            return None
        if r.status_code != 200:
            logger.debug("OpenAlex %s -> HTTP %s", label, r.status_code)
            return None
        return r.json()

    def _normalize(self, work: dict) -> NormalizedMeta:
        """Extract NormalizedMeta fields from an OpenAlex Work object."""
        # Abstract: reconstruct from inverted index
        abstract = None
        aii = work.get("abstract_inverted_index")
        if aii:
            abstract = _reconstruct_abstract(aii)

        # Authors
        authors = []
        for authorship in work.get("authorships") or []:
            author = authorship.get("author", {})
            name = author.get("display_name")
            if name:
                authors.append(name)

        # Open access URL
        oa_url = None
        best_oa = work.get("best_oa_location") or {}
        oa_url = best_oa.get("pdf_url") or best_oa.get("landing_page_url")

        # DOI
        doi_raw = work.get("doi") or ""
        # OpenAlex returns DOI as full URL: https://doi.org/10.xxx
        doi = normalize_doi(doi_raw.replace("https://doi.org/", "").replace("http://doi.org/", ""))

        return {
            "title": work.get("title"),
            "authors": authors,
            "year": work.get("publication_year"),
            "abstract": abstract,
            "doi": doi,
            "citation_count": work.get("cited_by_count"),
            "open_access_url": oa_url,
        }

    def enrich_by_doi(self, doi: str) -> NormalizedMeta | None:
        """Look up a work by DOI on OpenAlex."""
        cache_key = f"doi:{doi}"
        hit, cached = self.cache.get("openalex", cache_key)
        if hit:
            return cached

        url = f"{OPENALEX_BASE}/works/doi:{doi}"
        data = self._get(url, f"doi {doi}")
        if not data or data.get("error"):
            return None

        meta = self._normalize(data)
        self.cache.set("openalex", cache_key, meta)
        logger.info("OpenAlex enriched DOI %s (citations=%s)", doi, meta.get("citation_count"))
        return meta

    def enrich_by_title(self, title: str) -> NormalizedMeta | None:
        """Search OpenAlex by title when no DOI is available."""
        cache_key = f"title:{normalize_title(title)[:120]}"
        hit, cached = self.cache.get("openalex", cache_key)
        if hit:
            return cached

        url = f"{OPENALEX_BASE}/works?search={requests.utils.quote(title[:300])}&per_page=1"
        data = self._get(url, f"search {title[:60]!r}")
        if not data:
            return None

        results = data.get("results") or []
        if not results:
            return None

        work = results[0]
        # Verify title match with fuzzy matching
        target = normalize_title(title)
        candidate = normalize_title(work.get("title") or "")
        score = min(
            fuzz.token_set_ratio(target, candidate),
            fuzz.token_sort_ratio(target, candidate),
        )
        if score < 85:
            logger.debug(
                "OpenAlex title search: no good match for %r (best=%s)",
                title[:60], score,
            )
            return None

        meta = self._normalize(work)
        self.cache.set("openalex", cache_key, meta)
        logger.info("OpenAlex enriched title %r (citations=%s)", title[:50], meta.get("citation_count"))
        return meta
