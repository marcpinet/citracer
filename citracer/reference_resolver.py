"""Resolve a BibEntry to enriched metadata + a downloadable PDF when possible.

Strategy (in order):
  1. If GROBID already extracted an arXiv id, download directly from arXiv.
  2. If the entry has a DOI, look it up by id on Semantic Scholar (usually
     already prefetched by one ``POST /paper/batch`` call for the whole BFS
     level) — exact, and it often yields the arXiv id.
  3. Otherwise search by title. With an S2 API key, S2's title-match
     endpoint (~1 req/s) goes first; without one, arXiv (~1 req/3s, no
     429 pain) goes first and S2 is the fallback.
  4. OpenReview as a last resort for ICLR / TMLR papers without a DOI.
  5. Cache PDFs and metadata locally. Genuine misses are cached with a TTL;
     transient failures (timeouts, 429, 5xx, open circuit breakers) never
     are, so a flaky service can't mark a paper unavailable for good.
"""
from __future__ import annotations
import logging
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

import arxiv
import requests
from rapidfuzz import fuzz

from .api_types import NormalizedMeta, OpenReviewCandidate, S2Paper, S2SearchResponse
from .metadata_cache import MetadataCache
from .constants import (
    ARXIV_COOLDOWN_AFTER_FAILURE_SECONDS,
    TITLE_FUZZY_MATCH_THRESHOLD,
    ARXIV_KEYWORD_SEARCH_MAX_WORDS,
    ARXIV_KEYWORD_SEARCH_MIN_WORD_LEN,
    ARXIV_MIN_INTERVAL,
    ARXIV_NUM_RETRIES,
    ARXIV_PAGE_SIZE,
    METADATA_CACHE_TTL_SECONDS,
    NEGATIVE_CACHE_TTL_SECONDS,
    OPENREVIEW_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    OPENREVIEW_CIRCUIT_BREAKER_THRESHOLD,
    OPENREVIEW_FUZZY_MATCH_THRESHOLD,
    OPENREVIEW_TIMEOUT_SECONDS,
    PDF_DOWNLOAD_TIMEOUT_SECONDS,
    SEARCH_YEAR_TOLERANCE,
    S2_429_BACKOFF_DELAYS,
    S2_429_CIRCUIT_BREAKER_THRESHOLD,
    S2_BATCH_SIZE,
    S2_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    S2_CITATIONS_PAGE_SIZE,
    S2_MIN_INTERVAL_WITH_KEY,
    S2_MIN_INTERVAL_WITHOUT_KEY,
    SCIHUB_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    SCIHUB_CIRCUIT_BREAKER_THRESHOLD,
    SCIHUB_MIRRORS,
    SCIHUB_TIMEOUT_SECONDS,
)
from .http_client import TransientError, download_pdf, is_transient_status, session
from .models import BibEntry, identity_keys
from .utils import (
    arxiv_id_from_doi,
    make_paper_id,
    normalize_arxiv_id,
    normalize_doi,
    normalize_title,
)

logger = logging.getLogger(__name__)

S2_BASE = "https://api.semanticscholar.org/graph/v1"
S2_FIELDS = "paperId,title,authors,year,publicationDate,abstract,externalIds,openAccessPdf,citationCount"

#: Fields we ask S2 to return for each citing paper in a reverse trace.
#: `contexts` are the 1-2 sentence snippets around the citation — the
#: whole point of the exercise, since matching the keyword against these
#: lets us filter out irrelevant citations without downloading any PDFs.
S2_CITATION_FIELDS = (
    "contexts,intents,"
    "citingPaper.paperId,citingPaper.title,citingPaper.authors,"
    "citingPaper.year,citingPaper.publicationDate,"
    "citingPaper.externalIds,citingPaper.abstract"
)

OPENREVIEW_V2 = "https://api2.openreview.net"
OPENREVIEW_V1 = "https://api.openreview.net"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: Longest Retry-After we honour, in seconds.
_MAX_RETRY_AFTER = 60.0


def _orev_value(field):
    """OpenReview v2 wraps fields as {'value': X}; v1 returns the value directly."""
    if isinstance(field, dict) and "value" in field:
        return field["value"]
    return field


_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "over", "under",
    "this", "that", "these", "those", "their", "there", "where", "which",
    "what", "when", "while", "using", "based", "via", "novel",
    "towards", "toward", "against",
}


@dataclass
class ResolvedRef:
    paper_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    publication_date: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    openreview_id: str | None = None
    abstract: str | None = None
    citation_count: int | None = None
    pdf_path: Path | None = None
    url: str | None = None


def _copy_ref(r: ResolvedRef) -> ResolvedRef:
    return replace(r, authors=list(r.authors))


def _title_score(a: str, b: str) -> float:
    # min() of both ratios: token_set_ratio alone is too permissive (two
    # papers sharing domain vocabulary score >85), token_sort_ratio
    # penalizes structural differences.
    return min(fuzz.token_set_ratio(a, b), fuzz.token_sort_ratio(a, b))


class _CircuitBreaker:
    """Skip a service for a cooldown after repeated failures."""

    def __init__(self, name: str, threshold: int, cooldown: float, hint: str = "") -> None:
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown
        self.hint = hint
        self._failures = 0
        self._tripped_at: float | None = None
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        with self._lock:
            if self._tripped_at is None:
                return False
            if time.time() - self._tripped_at > self.cooldown:
                # Cooldown elapsed, give it another chance
                self._tripped_at = None
                self._failures = 0
                return False
            return True

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold and self._tripped_at is None:
                self._tripped_at = time.time()
                logger.warning(
                    "%s failed %d time(s) in a row; skipping it for %.0fs.%s",
                    self.name, self._failures, self.cooldown,
                    f" {self.hint}" if self.hint else "",
                )

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0


class ReferenceResolver:
    def __init__(
        self,
        cache_dir: str | Path = "./cache",
        s2_api_key: str | None = None,
        s2_min_interval: float | None = None,
        supplied_pdfs: dict[str, Path] | None = None,
        enrich: bool = False,
        email: str | None = None,
        no_refetch: bool = False,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.pdf_dir = self.cache_dir / "pdfs"
        self.pdf_dir.mkdir(parents=True, exist_ok=True)
        self.meta_cache = MetadataCache(self.cache_dir / "metadata.sqlite")
        self._no_refetch = no_refetch
        self.s2_api_key = s2_api_key
        # Semantic Scholar enforces ~1 req/sec for free API keys, and even
        # stricter throttling on the unauthenticated public endpoint.
        if s2_min_interval is None:
            s2_min_interval = (
                S2_MIN_INTERVAL_WITH_KEY if s2_api_key else S2_MIN_INTERVAL_WITHOUT_KEY
            )
        self.s2_min_interval = s2_min_interval
        self._s2_key_warned = False
        self._last_s2_call = 0.0
        self._last_arxiv_call = 0.0
        # Guards all rate-limit state so the resolver is safe to share
        # across threads when callers parallelize resolve() invocations.
        self._s2_lock = threading.Lock()
        self._arxiv_dl_lock = threading.Lock()
        # arxiv.Client's own rate limiting is not thread-safe: without this
        # lock, concurrent resolves fire bursts at the arXiv API (-> 503s).
        self._arxiv_api_lock = threading.Lock()
        # Circuit breakers: when an upstream service rate-limits us
        # repeatedly, stop hammering it for a cooldown period instead of
        # paying the full backoff cost on every subsequent call.
        self._s2_breaker = _CircuitBreaker(
            "Semantic Scholar", S2_429_CIRCUIT_BREAKER_THRESHOLD,
            S2_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
            hint="Get a free API key for much faster resolves: "
                 "https://www.semanticscholar.org/product/api#api-key",
        )
        self._arxiv_breaker = _CircuitBreaker(
            "arXiv API", 1, ARXIV_COOLDOWN_AFTER_FAILURE_SECONDS,
        )
        self._orev_breaker = _CircuitBreaker(
            "OpenReview", OPENREVIEW_CIRCUIT_BREAKER_THRESHOLD,
            OPENREVIEW_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
        )
        self._scihub_breaker = _CircuitBreaker(
            "Sci-Hub", SCIHUB_CIRCUIT_BREAKER_THRESHOLD,
            SCIHUB_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
        )
        self._arxiv_client = arxiv.Client(
            page_size=ARXIV_PAGE_SIZE,
            delay_seconds=ARXIV_MIN_INTERVAL,
            num_retries=ARXIV_NUM_RETRIES,
        )
        # In-run memo of resolve() results (hits AND misses): a reference
        # cited by 20 papers is resolved once, not 20 times.
        self._memo: dict[str, ResolvedRef] = {}
        self._key_locks: dict[str, threading.Lock] = {}
        self._memo_lock = threading.Lock()
        # Per-thread flag: did any service fail transiently during the
        # current lookup? Decides whether a miss may be cached.
        self._tls = threading.local()
        self.supplied_pdfs = supplied_pdfs or {}
        self._enricher = None
        if enrich or email:
            from .metadata_enrichment import MetadataEnricher
            self._enricher = MetadataEnricher(
                self.meta_cache, email=email,
            )

    # ---------- transient-failure tracking ----------

    def _mark_transient(self) -> None:
        self._tls.transient = True

    @contextmanager
    def _track_transient(self):
        """Yield a dict whose ``failed`` key tells whether a transient
        failure happened inside the block (the outer flag is preserved)."""
        outer = getattr(self._tls, "transient", False)
        self._tls.transient = False
        state = {"failed": False}
        try:
            yield state
        finally:
            state["failed"] = self._tls.transient
            self._tls.transient = outer or state["failed"]

    # ---------- public ----------

    def close(self) -> None:
        """Close the underlying metadata cache."""
        self.meta_cache.close()

    @staticmethod
    def cache_key(bib: BibEntry) -> str:
        return make_paper_id(doi=bib.doi, arxiv_id=bib.arxiv_id, title=bib.title or bib.raw)

    def batch_enrich(self, refs: list[ResolvedRef]) -> None:
        """Fill missing citation counts / dates / abstracts in bulk.

        One S2 ``POST /paper/batch`` call covers up to 500 papers (papers
        found through an arXiv title search otherwise never get a citation
        count), then OpenAlex (if enabled) covers up to 50 DOIs per call.
        Title-only OpenAlex enrichment is handled inline during ``resolve()``.
        """
        s2_ids: dict[str, list[ResolvedRef]] = {}
        for ref in refs:
            sid = (f"ARXIV:{ref.arxiv_id}" if ref.arxiv_id
                   else f"DOI:{ref.doi}" if ref.doi else None)
            if sid:
                s2_ids.setdefault(sid, []).append(ref)
        if s2_ids:
            found = self._s2_batch(list(s2_ids))
            for sid, meta in found.items():
                for ref in s2_ids.get(sid, []):
                    # Matched by identifier: S2's title and authors are
                    # authoritative, GROBID's are often damaged (merged
                    # words, truncated author lists).
                    if meta.get("title"):
                        ref.title = meta["title"]
                    if meta.get("authors"):
                        ref.authors = list(meta["authors"])
                    if ref.citation_count is None:
                        ref.citation_count = meta.get("citation_count")
                    if not ref.publication_date:
                        ref.publication_date = meta.get("publication_date")
                    if not ref.abstract:
                        ref.abstract = meta.get("abstract")

        if not self._enricher:
            return
        doi_refs: list[tuple[ResolvedRef, str]] = []
        for ref in refs:
            if not ref.doi:
                continue
            if ref.abstract and ref.citation_count is not None:
                continue
            doi_refs.append((ref, ref.doi))

        if not doi_refs:
            return

        enriched = self._enricher.enrich_batch_by_dois(
            [doi for _, doi in doi_refs],
        )
        for ref, doi in doi_refs:
            meta = enriched.get(doi)
            if not meta:
                continue
            if not ref.abstract and meta.get("abstract"):
                ref.abstract = meta["abstract"]
            if ref.citation_count is None and meta.get("citation_count") is not None:
                ref.citation_count = meta["citation_count"]
            if not ref.year and meta.get("year"):
                ref.year = meta["year"]
            if not ref.authors and meta.get("authors"):
                ref.authors = meta["authors"]

    def prefetch(self, bibs: list[BibEntry]) -> None:
        """Warm the S2 cache for every entry that carries a DOI or arXiv id,
        with one batch request per 500 ids instead of one request each."""
        ids = []
        for bib in bibs:
            if bib.arxiv_id:
                ids.append(f"ARXIV:{normalize_arxiv_id(bib.arxiv_id)}")
            elif bib.doi and not arxiv_id_from_doi(bib.doi):
                ids.append(f"DOI:{normalize_doi(bib.doi)}")
        if ids:
            self._s2_batch(ids)

    def resolve(self, bib: BibEntry) -> ResolvedRef:
        key = self.cache_key(bib)
        with self._memo_lock:
            lock = self._key_locks.setdefault(key, threading.Lock())
        # Concurrent resolves of the same reference wait for the first one.
        with lock:
            cached = self._memo.get(key)
            if cached is None:
                with self._track_transient():
                    cached = self._resolve_uncached(bib, key)
                self._memo[key] = cached
        return _copy_ref(cached)

    def _supplied_pdf(self, paper_id: str, doi: str | None, arxiv_id: str | None) -> Path | None:
        for k in identity_keys(paper_id, doi, arxiv_id):
            if k in self.supplied_pdfs:
                return self.supplied_pdfs[k]
        return None

    def _resolve_uncached(self, bib: BibEntry, bib_cache_key: str) -> ResolvedRef:
        # Fast path: return cached result when --no-refetch is active.
        if self._no_refetch:
            cached = self._cached_resolution(bib_cache_key)
            if cached is not None:
                return cached

        # Start with whatever GROBID extracted; merge enrichment in later.
        doi_arxiv = arxiv_id_from_doi(bib.doi)
        meta: dict = {
            "title": bib.title,
            "authors": list(bib.authors),
            "year": bib.year,
            "doi": None if doi_arxiv else normalize_doi(bib.doi),
            "arxiv_id": normalize_arxiv_id(bib.arxiv_id) or doi_arxiv,
            "abstract": None,
        }

        def merge(src: dict | None) -> None:
            for k, v in (src or {}).items():
                if v and not meta.get(k):
                    meta[k] = v

        if meta["arxiv_id"]:
            # Metadata for arXiv entries is free when prefetched.
            merge(self._s2_cached_by_id(f"ARXIV:{meta['arxiv_id']}"))

        # 1. DOI known: exact id lookup (prefetched by the batch call).
        s2_found = False
        if not meta["arxiv_id"] and meta["doi"]:
            s2_meta = self._s2_lookup(bib, search=False)
            merge(s2_meta)
            s2_found = s2_meta is not None

        # 2. Title searches, fastest service first. S2 indexes arXiv, so a
        #    paper S2 knows (by DOI or title) without an arXiv id isn't on
        #    arXiv either: no arXiv search in that case.
        if not meta.get("arxiv_id") and meta.get("title"):
            if self.s2_api_key and not s2_found:
                s2_meta = self._s2_lookup(bib)
                merge(s2_meta)
                s2_found = s2_meta is not None
            if not meta.get("arxiv_id") and not s2_found:
                merge(self._arxiv_search_by_title(meta["title"], bib_year=bib.year))
            if not meta.get("arxiv_id") and not s2_found and not self.s2_api_key:
                # Only fall back to Semantic Scholar if arxiv search failed
                # (paper not on arxiv) — S2 is slow without a key.
                merge(self._s2_lookup(bib))

        # 3. Last resort: OpenReview (covers ICLR / TMLR papers not on arxiv,
        #    none of which have a DOI).
        if not meta.get("arxiv_id") and not meta.get("doi") and meta.get("title"):
            merge(self._openreview_search_by_title(meta["title"]))

        # 4. Metadata enrichment via OpenAlex (if enabled)
        # DOI-based enrichment is deferred to batch_enrich() which issues
        # a single HTTP request for up to 50 DOIs at once.  Only title-
        # based enrichment (papers without a DOI) still runs inline.
        if self._enricher:
            needs = (
                not meta.get("abstract")
                or meta.get("citation_count") is None
                or not meta.get("open_access_url")
            )
            if needs and not meta.get("doi") and meta.get("title"):
                merge(self._enricher.enrich_by_title(meta["title"]))

        paper_id = make_paper_id(
            doi=meta.get("doi"),
            arxiv_id=meta.get("arxiv_id"),
            title=meta.get("title") or bib.raw,
        )
        # If we still have no canonical id but found an openreview id,
        # use it as the paper_id so deduplication works.
        if paper_id.startswith("title:") and meta.get("openreview_id"):
            paper_id = f"openreview:{meta['openreview_id']}"

        # --- PDF download cascade ---
        # 0. User-supplied PDF (highest priority)
        pdf_path = self._supplied_pdf(paper_id, meta.get("doi"), meta.get("arxiv_id"))

        # 1. arXiv
        if pdf_path is None and meta.get("arxiv_id"):
            pdf_path = self._download_arxiv(meta["arxiv_id"])

        # 2. OpenReview
        if pdf_path is None and meta.get("openreview_id"):
            pdf_path = self._download_openreview(meta["openreview_id"])

        # 3. Sci-Hub (by DOI)
        if pdf_path is None and meta.get("doi"):
            pdf_path = self._download_scihub(meta["doi"])

        # 4. S2 open-access PDF URL
        if pdf_path is None and meta.get("open_access_url"):
            pdf_path = self._download_generic_pdf(
                meta["open_access_url"], paper_id,
            )

        # 5. Preprint-specific download
        if pdf_path is None and meta.get("doi"):
            pdf_path = self._try_preprint_download(
                meta["doi"], meta.get("open_access_url"), paper_id,
            )

        url = None
        if meta.get("arxiv_id"):
            url = f"https://arxiv.org/abs/{meta['arxiv_id']}"
        elif meta.get("openreview_id"):
            url = f"https://openreview.net/forum?id={meta['openreview_id']}"
        elif meta.get("doi"):
            url = f"https://doi.org/{meta['doi']}"

        result = ResolvedRef(
            paper_id=paper_id,
            title=meta.get("title") or bib.raw[:120] or "(unknown)",
            authors=meta.get("authors") or bib.authors,
            year=meta.get("year") or bib.year,
            publication_date=meta.get("publication_date"),
            doi=meta.get("doi"),
            arxiv_id=meta.get("arxiv_id"),
            openreview_id=meta.get("openreview_id"),
            abstract=meta.get("abstract"),
            citation_count=meta.get("citation_count"),
            pdf_path=pdf_path,
            url=url,
        )

        # Persist the full result so future --no-refetch runs can skip
        # the entire resolution cascade for this paper — unless the paper
        # came out unavailable because some service was down: that verdict
        # must not stick.
        if pdf_path is not None or not getattr(self._tls, "transient", False):
            self.meta_cache.set("resolved", bib_cache_key, {
                "paper_id": result.paper_id,
                "title": result.title,
                "authors": result.authors,
                "year": result.year,
                "publication_date": result.publication_date,
                "doi": result.doi,
                "arxiv_id": result.arxiv_id,
                "openreview_id": result.openreview_id,
                "abstract": result.abstract,
                "citation_count": result.citation_count,
                "pdf_path": str(result.pdf_path) if result.pdf_path else None,
                "url": result.url,
            })
        else:
            logger.debug("Not caching unavailable %s (transient failure)", paper_id)

        return result

    def _cached_resolution(self, bib_cache_key: str) -> ResolvedRef | None:
        """--no-refetch lookup. Unavailable verdicts expire after the
        negative TTL and are overridden by a user-supplied PDF."""
        hit, data = self.meta_cache.get("resolved", bib_cache_key)
        if not hit or data is None:
            return None
        pdf_path = Path(data["pdf_path"]) if data.get("pdf_path") else None
        ref = ResolvedRef(
            paper_id=data["paper_id"],
            title=data["title"],
            authors=data.get("authors", []),
            year=data.get("year"),
            publication_date=data.get("publication_date"),
            doi=data.get("doi"),
            arxiv_id=data.get("arxiv_id"),
            openreview_id=data.get("openreview_id"),
            abstract=data.get("abstract"),
            citation_count=data.get("citation_count"),
            pdf_path=pdf_path,
            url=data.get("url"),
        )
        if pdf_path is not None:
            # Trust the cache only when the PDF is still on disk.
            return ref if pdf_path.exists() else None
        supplied = self._supplied_pdf(ref.paper_id, ref.doi, ref.arxiv_id)
        if supplied is not None:
            ref.pdf_path = supplied
            return ref
        fresh, _ = self.meta_cache.get(
            "resolved", bib_cache_key, ttl=NEGATIVE_CACHE_TTL_SECONDS,
        )
        return ref if fresh else None

    # ---------- public download helpers ----------
    # Used by source_resolver to download the root paper.

    def download_arxiv(self, arxiv_id: str) -> Path | None:
        return self._download_arxiv(arxiv_id)

    def download_scihub(self, doi: str) -> Path | None:
        return self._download_scihub(doi)

    def download_openreview(self, openreview_id: str) -> Path | None:
        return self._download_openreview(openreview_id)

    def download_generic_pdf(self, url: str, paper_id: str) -> Path | None:
        return self._download_generic_pdf(url, paper_id)

    def s2_by_id(self, id_str: str) -> NormalizedMeta | None:
        return self._s2_by_id(id_str)

    # ---------- Semantic Scholar lookup ----------

    def _s2_lookup(self, bib: BibEntry, search: bool = True) -> NormalizedMeta | None:
        cache_key = self.cache_key(bib)
        hit, cached = self.meta_cache.get(
            "s2", cache_key,
            ttl=METADATA_CACHE_TTL_SECONDS, negative_ttl=NEGATIVE_CACHE_TTL_SECONDS,
        )
        if hit and (cached is not None or search):
            return cached

        meta: NormalizedMeta | None = None
        doi_arxiv = arxiv_id_from_doi(bib.doi)
        arxiv_id = bib.arxiv_id or doi_arxiv
        with self._track_transient() as t:
            if bib.doi and not doi_arxiv:
                meta = self._s2_by_id(f"DOI:{normalize_doi(bib.doi)}")
            if meta is None and arxiv_id:
                meta = self._s2_by_id(f"ARXIV:{normalize_arxiv_id(arxiv_id)}")
            if meta is None and bib.title and search:
                meta = self._s2_search(bib.title, bib_year=bib.year)

        # A miss is only cached when every lookup was actually attempted
        # and answered.
        if meta is not None or (search and not t["failed"]):
            self.meta_cache.set("s2", cache_key, meta)
        return meta

    def _s2_headers(self) -> dict:
        h = {"User-Agent": "citracer"}
        if self.s2_api_key:
            h["x-api-key"] = self.s2_api_key
        return h

    def _s2_throttle(self) -> None:
        # Read-sleep-write must be atomic across threads, otherwise two
        # concurrent callers both see the "clear" timestamp, both sleep the
        # same amount and both fire a request simultaneously.
        with self._s2_lock:
            now = time.time()
            delta = now - self._last_s2_call
            if delta < self.s2_min_interval:
                time.sleep(self.s2_min_interval - delta)
            self._last_s2_call = time.time()

    def _s2_get(self, url: str, label: str, json_body: dict | None = None):
        """GET (or POST with ``json_body``) with throttling + 429/5xx-aware
        backoff that honours ``Retry-After``.

        Returns the decoded JSON, or None. A None caused by anything other
        than a genuine "not found" marks the current lookup as transient.
        If S2 has been rate-limiting us repeatedly, the circuit breaker
        short-circuits the call and returns None immediately.
        """
        if self._s2_breaker.is_open():
            logger.debug("S2 %s skipped (circuit breaker open)", label)
            self._mark_transient()
            return None

        backoff = S2_429_BACKOFF_DELAYS
        extra_wait = 0.0
        saw_429 = False
        for attempt, wait in enumerate(backoff):
            wait = max(wait, extra_wait)
            if wait:
                time.sleep(wait)
            self._s2_throttle()
            try:
                if json_body is None:
                    r = session().get(url, headers=self._s2_headers(), timeout=30)
                else:
                    r = session().post(url, headers=self._s2_headers(), json=json_body,
                                       timeout=60)
            except requests.RequestException as e:
                logger.warning("S2 %s failed: %s", label, e)
                self._mark_transient()
                return None
            if r.status_code == 200:
                self._s2_breaker.record_success()
                try:
                    return r.json()
                except ValueError:
                    self._mark_transient()
                    return None
            if is_transient_status(r.status_code):
                saw_429 = saw_429 or r.status_code == 429
                extra_wait = _retry_after(r)
                logger.debug("S2 %s -> %s (attempt %d/%d)", label, r.status_code,
                             attempt + 1, len(backoff))
                continue
            logger.debug("S2 %s -> HTTP %s", label, r.status_code)
            if r.status_code in (401, 403) and self.s2_api_key and not self._s2_key_warned:
                self._s2_key_warned = True
                logger.warning(
                    "Semantic Scholar rejected the API key (HTTP %s): it is "
                    "probably invalid or expired. Check S2_API_KEY / "
                    "`citracer config set-s2-key`.", r.status_code,
                )
            if r.status_code != 404:
                self._mark_transient()
            return None
        logger.warning("S2 %s exhausted retries", label)
        self._mark_transient()
        if saw_429:
            self._s2_breaker.record_failure()
        return None

    def _s2_cached_by_id(self, id_str: str) -> NormalizedMeta | None:
        hit, cached = self.meta_cache.get(
            "s2id", id_str.lower(),
            ttl=METADATA_CACHE_TTL_SECONDS, negative_ttl=NEGATIVE_CACHE_TTL_SECONDS,
        )
        return cached if hit else None

    def _s2_by_id(self, id_str: str) -> NormalizedMeta | None:
        key = id_str.lower()
        hit, cached = self.meta_cache.get(
            "s2id", key,
            ttl=METADATA_CACHE_TTL_SECONDS, negative_ttl=NEGATIVE_CACHE_TTL_SECONDS,
        )
        if hit:
            return cached
        url = f"{S2_BASE}/paper/{id_str}?fields={S2_FIELDS}"
        with self._track_transient() as t:
            data = self._s2_get(url, f"by-id {id_str}")
        meta = self._normalize_s2(data) if data else None  # type: ignore[arg-type]
        if meta is not None or not t["failed"]:
            self.meta_cache.set("s2id", key, meta)
        return meta

    def _s2_batch(self, ids: list[str]) -> dict[str, NormalizedMeta]:
        """Metadata for many S2 ids (``DOI:x`` / ``ARXIV:y``), cached, via
        ``POST /paper/batch``. Returns {id: meta} for the ids found."""
        out: dict[str, NormalizedMeta] = {}
        todo: list[str] = []
        for sid in dict.fromkeys(ids):
            hit, cached = self.meta_cache.get(
                "s2id", sid.lower(),
                ttl=METADATA_CACHE_TTL_SECONDS, negative_ttl=NEGATIVE_CACHE_TTL_SECONDS,
            )
            if hit:
                if cached is not None:
                    out[sid] = cached
            else:
                todo.append(sid)
        for i in range(0, len(todo), S2_BATCH_SIZE):
            chunk = todo[i:i + S2_BATCH_SIZE]
            with self._track_transient():
                data = self._s2_get(
                    f"{S2_BASE}/paper/batch?fields={S2_FIELDS}",
                    f"batch {len(chunk)} ids", json_body={"ids": chunk},
                )
            if not isinstance(data, list):
                continue  # transient failure: leave uncached
            for sid, paper in zip(chunk, data):
                meta = self._normalize_s2(paper) if paper else None
                self.meta_cache.set("s2id", sid.lower(), meta)
                if meta is not None:
                    out[sid] = meta
            logger.info("S2 batch: %d/%d id(s) found", sum(1 for p in data if p), len(chunk))
        return out

    def _s2_search(self, title: str, bib_year: int | None = None) -> NormalizedMeta | None:
        q = re.sub(r"\s+", " ", title).strip()[:300]
        # /search/match returns S2's single best title match (404 if none).
        url = f"{S2_BASE}/paper/search/match?query={requests.utils.quote(q)}&fields={S2_FIELDS}"
        data = self._s2_get(url, f"match {q[:60]!r}")
        if not data:
            return None
        resp: S2SearchResponse = data  # type: ignore[assignment]
        items = resp.get("data") or []
        if not items:
            return None
        # Validate with fuzzy matching (same as arXiv/OpenReview searches)
        target = normalize_title(title)
        best = None
        best_score = 0.0
        for item in items:
            # Year cross-check: skip results too far from the bib year
            if bib_year is not None and item.get("year"):
                if abs(item["year"] - bib_year) > SEARCH_YEAR_TOLERANCE:
                    continue
            score = _title_score(target, normalize_title(item.get("title") or ""))
            if score > best_score:
                best_score = score
                best = item
        if best is None or best_score < TITLE_FUZZY_MATCH_THRESHOLD:
            logger.debug("S2 search: no good match for %r (best=%s)", title[:60], best_score)
            return None
        logger.info("S2 search hit for %r (score=%d)", title[:50], best_score)
        return self._normalize_s2(best)

    # ---------- arXiv title search fallback ----------

    def _arxiv_search_by_title(self, title: str, bib_year: int | None = None) -> NormalizedMeta | None:
        """Search arxiv.org by title.

        Two strategies, in order:
          1. Phrase search ti:"<cleaned title>" — fast & precise when it works
          2. Keyword search ti:word1 ti:word2 ... — catches papers whose actual
             arxiv title differs slightly (punctuation, spacing) from what was
             cited.

        Both candidate sets are scored with rapidfuzz; we keep the best match
        above a threshold.
        """
        cache_key = normalize_title(title)[:120]
        hit, cached = self.meta_cache.get(
            "arxsearch", cache_key, negative_ttl=NEGATIVE_CACHE_TTL_SECONDS,
        )
        if hit:
            return cached

        with self._track_transient() as t:
            out = self._arxiv_search_uncached(title, bib_year)
        if out is not None or not t["failed"]:
            self.meta_cache.set("arxsearch", cache_key, out)
        return out

    def _arxiv_search_uncached(self, title: str, bib_year: int | None) -> NormalizedMeta | None:
        target = normalize_title(title)
        results = self._arxiv_search_phrase(title)
        if not results:
            results = self._arxiv_search_keywords(title)

        best = None
        best_score = 0.0
        for r in results:
            score = _title_score(target, normalize_title(r.title))
            if score > best_score:
                best_score = score
                best = r

        if best is None or best_score < TITLE_FUZZY_MATCH_THRESHOLD:
            logger.debug("arxiv search: no good match for %r (best=%s)", title[:60], best_score)
            return None

        # Year cross-check: reject if the result's year is too far from the bib entry's year
        if bib_year is not None and getattr(best, 'published', None):
            result_year = best.published.year
            if abs(result_year - bib_year) > SEARCH_YEAR_TOLERANCE:
                logger.debug(
                    "arxiv search: year mismatch for %r (bib=%d, result=%d, gap=%d)",
                    title[:60], bib_year, result_year, abs(result_year - bib_year),
                )
                return None

        arxiv_id = normalize_arxiv_id(best.get_short_id())
        out: NormalizedMeta = {
            "arxiv_id": arxiv_id,
            "title": best.title,
            "doi": normalize_doi(best.doi),
            "abstract": best.summary,
        }
        logger.info("arxiv search hit for %r -> %s (score=%d)", title[:50], arxiv_id, best_score)
        return out

    def _arxiv_query(self, query: str, max_results: int, label: str) -> list:
        if self._arxiv_breaker.is_open():
            self._mark_transient()
            return []
        try:
            search = arxiv.Search(query=query, max_results=max_results,
                                  sort_by=arxiv.SortCriterion.Relevance)
            with self._arxiv_api_lock:
                return list(self._arxiv_client.results(search))
        except Exception as e:
            logger.warning("arxiv %s search failed for %r: %s", label, query[:60], e)
            self._mark_transient()
            self._arxiv_breaker.record_failure()
            return []

    def _arxiv_search_phrase(self, title: str) -> list:
        # Strip punctuation that breaks Lucene phrase queries (notably ':')
        cleaned = re.sub(r"[^\w\s\-]", " ", title)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()[:200]
        if not cleaned:
            return []
        return self._arxiv_query(f'ti:"{cleaned}"', 5, "phrase")

    def _arxiv_search_keywords(self, title: str) -> list:
        # Use distinctive words to build an AND query.
        words = re.findall(
            rf"\b[\w\-]{{{ARXIV_KEYWORD_SEARCH_MIN_WORD_LEN},}}\b",
            title,
        )
        words = [w for w in words if w.lower() not in _STOPWORDS][
            :ARXIV_KEYWORD_SEARCH_MAX_WORDS
        ]
        if not words:
            return []
        return self._arxiv_query(" ".join(f"ti:{w}" for w in words), 10, "keyword")

    def _normalize_s2(self, paper: S2Paper) -> NormalizedMeta:
        ext = paper.get("externalIds") or {}
        oa = paper.get("openAccessPdf")
        doi = normalize_doi(ext.get("DOI"))
        arxiv_id = normalize_arxiv_id(ext.get("ArXiv")) or arxiv_id_from_doi(doi)
        if arxiv_id_from_doi(doi):
            doi = None
        return {
            "title": paper.get("title"),
            "authors": [
                a.get("name") or ""
                for a in (paper.get("authors") or [])
                if a.get("name")
            ],
            "year": paper.get("year"),
            "publication_date": paper.get("publicationDate"),
            "abstract": paper.get("abstract"),
            "doi": doi,
            "arxiv_id": arxiv_id,
            "citation_count": paper.get("citationCount"),
            "open_access_url": oa.get("url") if oa else None,
        }

    # ---------- Citations (reverse trace) ----------

    def get_citations(
        self,
        paper_id: str,
        limit: int = 1000,
        page_size: int = S2_CITATIONS_PAGE_SIZE,
    ) -> list[dict]:
        """Fetch the list of papers that cite ``paper_id``, with their
        citation contexts, from Semantic Scholar.

        ``paper_id`` can be any identifier the S2 endpoint accepts:
        ``ARXIV:2211.14730``, ``DOI:10.48550/arxiv.2211.14730``, an
        OpenAlex id, or S2's own ``paperId``. Pagination is handled
        internally up to ``limit`` total citations. Complete results are
        cached.

        Returns a list of raw citation dicts. Each dict has keys
        ``contexts`` (list[str]), ``intents`` (list[str]), and
        ``citingPaper`` (dict with S2 metadata). Empty list on failure.
        """
        cache_key = f"{paper_id.lower()}|{limit}"
        hit, cached = self.meta_cache.get("s2cit", cache_key, ttl=METADATA_CACHE_TTL_SECONDS)
        if hit and cached is not None:
            logger.info("Fetched %d citing paper(s) for %s (cached)", len(cached), paper_id)
            return cached

        out: list[dict] = []
        offset = 0
        with self._track_transient() as t:
            while offset < limit:
                remaining = limit - offset
                this_page = min(page_size, remaining)
                url = (
                    f"{S2_BASE}/paper/{paper_id}/citations"
                    f"?fields={S2_CITATION_FIELDS}"
                    f"&offset={offset}&limit={this_page}"
                )
                data = self._s2_get(url, f"citations {paper_id} +{offset}")
                if not data:
                    break
                items = data.get("data") or []
                if not items:
                    break
                out.extend(items)
                if len(items) < this_page:
                    break  # last page
                offset += len(items)
        if not t["failed"]:
            self.meta_cache.set("s2cit", cache_key, out)
        logger.info("Fetched %d citing paper(s) for %s", len(out), paper_id)
        return out

    # ---------- OpenReview ----------

    def _openreview_search_by_title(self, title: str) -> NormalizedMeta | None:
        """Search OpenReview by title. Tries v2 then v1 (ICLR<=2022 lives in v1).
        Returns {openreview_id, title, authors, abstract} on success.
        """
        cache_key = normalize_title(title)[:120]
        hit, cached = self.meta_cache.get(
            "orev", cache_key, negative_ttl=NEGATIVE_CACHE_TTL_SECONDS,
        )
        if hit:
            return cached

        if self._orev_breaker.is_open():
            logger.debug("OpenReview search skipped (circuit breaker open)")
            self._mark_transient()
            return None

        target = normalize_title(title)
        candidates: list[OpenReviewCandidate] = []
        failed = False
        for base in (OPENREVIEW_V2, OPENREVIEW_V1):
            try:
                r = session().get(
                    f"{base}/notes/search",
                    params={"term": title[:200], "content": "all", "source": "forum", "limit": 5},
                    headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
                    timeout=OPENREVIEW_TIMEOUT_SECONDS,
                )
                notes = r.json().get("notes", []) if r.status_code == 200 else None
            except (requests.RequestException, ValueError) as e:
                logger.warning("OpenReview %s failed: %s", base, e)
                failed = True
                continue
            if notes is None:
                logger.debug("OpenReview %s -> HTTP %s", base, r.status_code)
                failed = failed or is_transient_status(r.status_code)
                continue
            for n in notes:
                c = n.get("content", {})
                t = _orev_value(c.get("title"))
                a = _orev_value(c.get("abstract"))
                authors = _orev_value(c.get("authors")) or []
                if t:
                    candidates.append({
                        "id": n.get("id"),
                        "title": t,
                        "abstract": a,
                        "authors": authors if isinstance(authors, list) else [],
                    })
            if candidates:
                break

        if not candidates:
            if failed:
                self._orev_breaker.record_failure()
                self._mark_transient()
            else:
                self.meta_cache.set("orev", cache_key, None)
            return None

        self._orev_breaker.record_success()

        best = None
        best_score = 0.0
        for c in candidates:
            score = _title_score(target, normalize_title(c["title"] or ""))
            if score > best_score:
                best_score = score
                best = c

        if best is None or best_score < OPENREVIEW_FUZZY_MATCH_THRESHOLD:
            logger.debug("OpenReview: no good match for %r (best=%s)", title[:60], best_score)
            self.meta_cache.set("orev", cache_key, None)
            return None

        out: NormalizedMeta = {
            "openreview_id": best["id"],
            "title": best["title"],
            "authors": best.get("authors") or [],
            "abstract": best.get("abstract"),
        }
        self.meta_cache.set("orev", cache_key, out)
        logger.info("OpenReview hit for %r -> %s (score=%d)", title[:50], best["id"], best_score)
        return out

    # ---------- downloads ----------

    def _download(self, url: str, out: Path, label: str, headers: dict | None = None) -> Path | None:
        """download_pdf() with transient failures logged and recorded."""
        try:
            path = download_pdf(
                url, out, timeout=PDF_DOWNLOAD_TIMEOUT_SECONDS,
                headers=headers or {"User-Agent": BROWSER_UA},
            )
        except TransientError as e:
            logger.warning("%s download failed: %s", label, e)
            self._mark_transient()
            return None
        if path is None:
            logger.debug("%s: no PDF at %s", label, url)
        return path

    def _download_openreview(self, openreview_id: str) -> Path | None:
        out = self.pdf_dir / f"openreview_{openreview_id}.pdf"
        path = self._download(
            f"https://openreview.net/pdf?id={openreview_id}", out,
            f"openreview:{openreview_id}",
            headers={"User-Agent": BROWSER_UA, "Accept": "application/pdf,*/*"},
        )
        if path is not None:
            logger.info("Downloaded openreview:%s -> %s", openreview_id, out.name)
        return path

    # ---------- Sci-Hub download ----------

    def _download_scihub(self, doi: str) -> Path | None:
        """Try to download a paper from Sci-Hub mirrors by DOI."""
        safe = re.sub(r"[^\w\-.]", "_", doi)[:100]
        out = self.pdf_dir / f"scihub_{safe}.pdf"
        if out.exists() and out.stat().st_size > 0:
            return out
        hit, _ = self.meta_cache.get("scihub", doi, negative_ttl=NEGATIVE_CACHE_TTL_SECONDS)
        if hit:
            return None  # every mirror answered "no PDF" recently
        if self._scihub_breaker.is_open():
            self._mark_transient()
            return None

        answered = False
        for mirror in SCIHUB_MIRRORS:
            try:
                r = session().get(
                    f"{mirror}/{doi}",
                    headers={"User-Agent": BROWSER_UA},
                    timeout=SCIHUB_TIMEOUT_SECONDS,
                    allow_redirects=True,
                )
            except requests.RequestException as e:
                logger.debug("Sci-Hub %s failed for %s: %s", mirror, doi, e)
                continue
            if r.status_code != 200:
                logger.debug("Sci-Hub %s -> HTTP %s for %s", mirror, r.status_code, doi)
                answered = answered or not is_transient_status(r.status_code)
                continue
            answered = True

            pdf_url = self._extract_scihub_pdf_url(r.text, mirror)
            if not pdf_url:
                logger.debug("Sci-Hub %s: no PDF URL found for %s", mirror, doi)
                continue

            try:
                path = download_pdf(
                    pdf_url, out, timeout=SCIHUB_TIMEOUT_SECONDS,
                    headers={"User-Agent": BROWSER_UA},
                )
            except TransientError as e:
                logger.debug("Sci-Hub PDF download failed from %s: %s", pdf_url, e)
                continue
            if path is not None:
                self._scihub_breaker.record_success()
                logger.info("Downloaded via Sci-Hub: %s -> %s", doi, out.name)
                return path

        if answered:
            self._scihub_breaker.record_success()
            self.meta_cache.set("scihub", doi, None)
        else:
            self._scihub_breaker.record_failure()
            self._mark_transient()
        return None

    @staticmethod
    def _extract_scihub_pdf_url(html: str, mirror: str) -> str | None:
        """Extract the PDF URL from a Sci-Hub HTML page.

        Looks for <embed type="application/pdf" src="..."> or a save
        button with onclick="location.href='...'".
        """
        # Try <embed> tag first
        m = re.search(r'<embed[^>]+type="application/pdf"[^>]+src="([^"]+)"', html)
        if not m:
            m = re.search(r'<embed[^>]+src="([^"]+)"[^>]+type="application/pdf"', html)
        if m:
            url = m.group(1)
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/"):
                url = mirror.rstrip("/") + url
            return url

        # Try save button onclick
        m = re.search(r"location\.href='([^']+\.pdf[^']*)'", html)
        if m:
            url = m.group(1).replace("\\/", "/")
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/"):
                url = mirror.rstrip("/") + url
            return url

        return None

    # ---------- generic PDF download ----------

    def _download_generic_pdf(self, url: str, paper_id: str) -> Path | None:
        """Download a PDF from an arbitrary URL (e.g. S2 open-access link)."""
        safe = re.sub(r"[^\w\-.]", "_", paper_id)[:100]
        out = self.pdf_dir / f"oa_{safe}.pdf"
        path = self._download(url, out, f"OA {paper_id}")
        if path is not None:
            logger.info("Downloaded OA PDF: %s -> %s", url[:80], out.name)
        return path

    # ---------- preprint download ----------

    def _try_preprint_download(
        self, doi: str, oa_url: str | None, paper_id: str,
    ) -> Path | None:
        """Try to download a PDF from a preprint server based on the DOI."""
        from .preprint_resolver import build_preprint_pdf_url
        pdf_url = build_preprint_pdf_url(doi, oa_url)
        if pdf_url:
            return self._download_generic_pdf(pdf_url, paper_id)
        return None

    # ---------- arxiv download ----------

    def _download_arxiv(self, arxiv_id: str) -> Path | None:
        arxiv_id = normalize_arxiv_id(arxiv_id)
        if not arxiv_id:
            return None
        out = self.pdf_dir / f"{arxiv_id.replace('/', '_')}.pdf"
        if out.exists() and out.stat().st_size > 0:
            return out

        # rate limit: be polite to arxiv (thread-safe)
        with self._arxiv_dl_lock:
            now = time.time()
            delta = now - self._last_arxiv_call
            if delta < ARXIV_MIN_INTERVAL:
                time.sleep(ARXIV_MIN_INTERVAL - delta)
            self._last_arxiv_call = time.time()

        path = self._download(
            f"https://arxiv.org/pdf/{arxiv_id}.pdf", out, f"arxiv:{arxiv_id}",
            headers={"User-Agent": "citracer"},
        )
        if path is not None:
            logger.info("Downloaded arxiv:%s -> %s", arxiv_id, out.name)
        return path


def _retry_after(r) -> float:
    """Seconds requested by a Retry-After header (0 if absent/unparseable)."""
    value = r.headers.get("Retry-After") if getattr(r, "headers", None) else None
    try:
        return min(max(float(value), 0.0), _MAX_RETRY_AFTER) if value else 0.0
    except (TypeError, ValueError):
        return 0.0
