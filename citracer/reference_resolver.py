"""Resolve a BibEntry to enriched metadata + a downloadable PDF when possible.

Strategy (in order):
  1. If GROBID already extracted an arXiv id, download directly from arXiv.
  2. Otherwise search arXiv.org by title — fast, no rate-limit pain, and
     returns arxiv_id + abstract + clean title in one call.
  3. Only if arXiv has nothing (paper not on arXiv), fall back to Semantic
     Scholar with 429-aware backoff.
  4. Cache PDFs and metadata locally to avoid redundant API calls.
"""
from __future__ import annotations
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
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
    OPENREVIEW_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    OPENREVIEW_CIRCUIT_BREAKER_THRESHOLD,
    OPENREVIEW_FUZZY_MATCH_THRESHOLD,
    OPENREVIEW_TIMEOUT_SECONDS,
    PDF_DOWNLOAD_TIMEOUT_SECONDS,
    SEARCH_YEAR_TOLERANCE,
    S2_429_BACKOFF_DELAYS,
    S2_429_CIRCUIT_BREAKER_THRESHOLD,
    S2_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    S2_MIN_INTERVAL_WITH_KEY,
    S2_MIN_INTERVAL_WITHOUT_KEY,
    SCIHUB_MIRRORS,
    SCIHUB_TIMEOUT_SECONDS,
)
from .models import BibEntry
from .utils import make_paper_id, normalize_arxiv_id, normalize_doi, normalize_title

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


class ReferenceResolver:
    def __init__(
        self,
        cache_dir: str | Path = "./cache",
        s2_api_key: str | None = None,
        s2_min_interval: float | None = None,
        supplied_pdfs: dict[str, Path] | None = None,
        enrich: bool = False,
        email: str | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.pdf_dir = self.cache_dir / "pdfs"
        self.pdf_dir.mkdir(parents=True, exist_ok=True)
        self.meta_cache = MetadataCache(self.cache_dir / "metadata.sqlite")
        # One-time self-heal: drop any cached "search returned nothing"
        # entries from previous runs. Those can become stale when the
        # upstream service was briefly flaky, or when we fix a bug in our
        # search logic. Positive (non-null) entries are untouched.
        purged = self.meta_cache.purge_negatives("arxsearch", "orev")
        if purged:
            logger.info("Purged %d stale negative cache entries", purged)
        self.s2_api_key = s2_api_key
        # Semantic Scholar enforces ~1 req/sec for free API keys, and even
        # stricter throttling on the unauthenticated public endpoint.
        if s2_min_interval is None:
            s2_min_interval = (
                S2_MIN_INTERVAL_WITH_KEY if s2_api_key else S2_MIN_INTERVAL_WITHOUT_KEY
            )
        self.s2_min_interval = s2_min_interval
        self._last_s2_call = 0.0
        self._last_arxiv_call = 0.0
        # Guards all rate-limit state so the resolver is safe to share
        # across threads when callers parallelize resolve() invocations.
        self._s2_lock = threading.Lock()
        self._arxiv_dl_lock = threading.Lock()
        # Circuit breakers: when an upstream service rate-limits us
        # repeatedly, stop hammering it for a cooldown period instead of
        # paying the full backoff cost on every subsequent call.
        self._s2_consecutive_429s = 0
        self._s2_breaker_tripped_at: float | None = None
        self._arxiv_breaker_tripped_at: float | None = None
        self._orev_consecutive_failures = 0
        self._orev_breaker_tripped_at: float | None = None
        self._breaker_lock = threading.Lock()
        self._arxiv_client = arxiv.Client(
            page_size=ARXIV_PAGE_SIZE,
            delay_seconds=ARXIV_MIN_INTERVAL,
            num_retries=ARXIV_NUM_RETRIES,
        )
        self.supplied_pdfs = supplied_pdfs or {}
        self._enricher = None
        if enrich or email:
            from .metadata_enrichment import MetadataEnricher
            self._enricher = MetadataEnricher(
                self.meta_cache, email=email,
            )

    # ---------- circuit breakers ----------

    def _s2_circuit_open(self) -> bool:
        """True iff S2 calls should be skipped right now."""
        with self._breaker_lock:
            if self._s2_breaker_tripped_at is None:
                return False
            if time.time() - self._s2_breaker_tripped_at > S2_CIRCUIT_BREAKER_COOLDOWN_SECONDS:
                # Cooldown elapsed, give it another chance
                self._s2_breaker_tripped_at = None
                self._s2_consecutive_429s = 0
                return False
            return True

    def _s2_record_429(self) -> None:
        with self._breaker_lock:
            self._s2_consecutive_429s += 1
            if (self._s2_consecutive_429s >= S2_429_CIRCUIT_BREAKER_THRESHOLD
                    and self._s2_breaker_tripped_at is None):
                self._s2_breaker_tripped_at = time.time()
                logger.warning(
                    "S2 rate-limited %d times in a row; skipping S2 for %.0fs. "
                    "Get a free API key for much faster resolves: "
                    "https://www.semanticscholar.org/product/api#api-key",
                    self._s2_consecutive_429s,
                    S2_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
                )

    def _s2_record_success(self) -> None:
        with self._breaker_lock:
            self._s2_consecutive_429s = 0

    def _arxiv_circuit_open(self) -> bool:
        with self._breaker_lock:
            if self._arxiv_breaker_tripped_at is None:
                return False
            if time.time() - self._arxiv_breaker_tripped_at > ARXIV_COOLDOWN_AFTER_FAILURE_SECONDS:
                self._arxiv_breaker_tripped_at = None
                return False
            return True

    def _arxiv_record_failure(self) -> None:
        with self._breaker_lock:
            if self._arxiv_breaker_tripped_at is None:
                self._arxiv_breaker_tripped_at = time.time()
                logger.warning(
                    "arxiv API rate-limited; skipping arxiv search for %.0fs",
                    ARXIV_COOLDOWN_AFTER_FAILURE_SECONDS,
                )

    def _orev_circuit_open(self) -> bool:
        with self._breaker_lock:
            if self._orev_breaker_tripped_at is None:
                return False
            if time.time() - self._orev_breaker_tripped_at > OPENREVIEW_CIRCUIT_BREAKER_COOLDOWN_SECONDS:
                self._orev_breaker_tripped_at = None
                self._orev_consecutive_failures = 0
                return False
            return True

    def _orev_record_failure(self) -> None:
        with self._breaker_lock:
            self._orev_consecutive_failures += 1
            if (self._orev_consecutive_failures >= OPENREVIEW_CIRCUIT_BREAKER_THRESHOLD
                    and self._orev_breaker_tripped_at is None):
                self._orev_breaker_tripped_at = time.time()
                logger.warning(
                    "OpenReview failed %d times in a row; skipping for %.0fs",
                    self._orev_consecutive_failures,
                    OPENREVIEW_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
                )

    def _orev_record_success(self) -> None:
        with self._breaker_lock:
            self._orev_consecutive_failures = 0

    # ---------- public ----------

    def close(self) -> None:
        """Close the underlying metadata cache."""
        self.meta_cache.close()

    def resolve(self, bib: BibEntry) -> ResolvedRef:
        # Start with whatever GROBID extracted; merge enrichment in later.
        meta: dict = {
            "title": bib.title,
            "authors": list(bib.authors),
            "year": bib.year,
            "doi": bib.doi,
            "arxiv_id": bib.arxiv_id,
            "abstract": None,
        }

        # 1. If GROBID didn't already give us an arxiv id, try arxiv search
        #    by title first — much faster than S2 when no API key, and the
        #    arxiv API also returns title/abstract so we get enrichment too.
        if not meta.get("arxiv_id") and meta.get("title"):
            arx = self._arxiv_search_by_title(meta["title"], bib_year=bib.year)
            if arx:
                for k, v in arx.items():
                    if v and not meta.get(k):
                        meta[k] = v

        # 2. Only fall back to Semantic Scholar if arxiv search failed
        #    (paper not on arxiv) — S2 is slower and prone to rate limits.
        if not meta.get("arxiv_id"):
            s2_meta = self._s2_lookup(bib)
            if s2_meta:
                for k, v in s2_meta.items():
                    if v and not meta.get(k):
                        meta[k] = v

        # 3. Last resort: OpenReview (covers ICLR / TMLR papers not on arxiv)
        if not meta.get("arxiv_id") and meta.get("title"):
            orev = self._openreview_search_by_title(meta["title"])
            if orev:
                for k, v in orev.items():
                    if v and not meta.get(k):
                        meta[k] = v

        # 4. Metadata enrichment via OpenAlex (if enabled)
        if self._enricher:
            needs = (
                not meta.get("abstract")
                or not meta.get("citation_count")
                or not meta.get("open_access_url")
            )
            if needs and meta.get("doi"):
                enriched = self._enricher.enrich_by_doi(meta["doi"])
                if enriched:
                    for k, v in enriched.items():
                        if v and not meta.get(k):
                            meta[k] = v
            if needs and not meta.get("doi") and meta.get("title"):
                enriched = self._enricher.enrich_by_title(meta["title"])
                if enriched:
                    for k, v in enriched.items():
                        if v and not meta.get(k):
                            meta[k] = v

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
        pdf_path = None

        # 0. User-supplied PDF (highest priority)
        if paper_id in self.supplied_pdfs:
            pdf_path = self.supplied_pdfs[paper_id]

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

        return ResolvedRef(
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

    def _s2_lookup(self, bib: BibEntry) -> NormalizedMeta | None:
        cache_key = make_paper_id(doi=bib.doi, arxiv_id=bib.arxiv_id, title=bib.title or bib.raw)
        hit, cached = self.meta_cache.get("s2", cache_key)
        if hit:
            return cached

        meta: NormalizedMeta | None = None
        if bib.doi:
            meta = self._s2_by_id(f"DOI:{bib.doi}")
        if meta is None and bib.arxiv_id:
            meta = self._s2_by_id(f"ARXIV:{bib.arxiv_id}")
        if meta is None and bib.title:
            meta = self._s2_search(bib.title, bib_year=bib.year)

        # Note: only cache positive hits for S2 — the throttle is expensive
        # and the negative case is rare enough that we'd rather retry it.
        if meta is not None:
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

    def _s2_get(self, url: str, label: str) -> dict | None:
        """GET with throttling + 429-aware exponential backoff.

        If S2 has been rate-limiting us repeatedly, the circuit breaker
        short-circuits the call and returns None immediately. This caps the
        worst-case latency for users without an S2 API key.
        """
        if self._s2_circuit_open():
            logger.debug("S2 %s skipped (circuit breaker open)", label)
            return None

        backoff = S2_429_BACKOFF_DELAYS
        for attempt, wait in enumerate(backoff):
            if wait:
                time.sleep(wait)
            self._s2_throttle()
            try:
                r = requests.get(url, headers=self._s2_headers(), timeout=30)
            except Exception as e:
                logger.warning("S2 %s failed: %s", label, e)
                return None
            if r.status_code == 200:
                self._s2_record_success()
                return r.json()
            if r.status_code == 429:
                logger.debug("S2 %s -> 429 (attempt %d/%d)", label, attempt + 1, len(backoff))
                continue
            logger.debug("S2 %s -> HTTP %s", label, r.status_code)
            return None
        logger.warning("S2 %s exhausted retries (rate-limited)", label)
        self._s2_record_429()
        return None

    def _s2_by_id(self, id_str: str) -> NormalizedMeta | None:
        url = f"{S2_BASE}/paper/{id_str}?fields={S2_FIELDS}"
        data = self._s2_get(url, f"by-id {id_str}")
        return self._normalize_s2(data) if data else None  # type: ignore[arg-type]

    def _s2_search(self, title: str, bib_year: int | None = None) -> NormalizedMeta | None:
        q = re.sub(r"\s+", " ", title).strip()[:300]
        url = f"{S2_BASE}/paper/search?query={requests.utils.quote(q)}&limit=3&fields={S2_FIELDS}"
        data = self._s2_get(url, f"search {q[:60]!r}")
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
            candidate = normalize_title(item.get("title") or "")
            score = min(
                fuzz.token_set_ratio(target, candidate),
                fuzz.token_sort_ratio(target, candidate),
            )
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
        hit, cached = self.meta_cache.get("arxsearch", cache_key)
        if hit:
            return cached

        target = normalize_title(title)
        results = self._arxiv_search_phrase(title)
        if not results:
            results = self._arxiv_search_keywords(title)

        best = None
        best_score = 0.0
        for r in results:
            candidate = normalize_title(r.title)
            # Use the minimum of token_set_ratio and token_sort_ratio.
            # token_set_ratio alone is too permissive: two papers sharing
            # common domain vocabulary (e.g. "time series ... neural networks")
            # can score >85 despite being completely different papers.
            # token_sort_ratio catches this by penalizing structural differences.
            score = min(
                fuzz.token_set_ratio(target, candidate),
                fuzz.token_sort_ratio(target, candidate),
            )
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
        self.meta_cache.set("arxsearch", cache_key, out)
        logger.info("arxiv search hit for %r -> %s (score=%d)", title[:50], arxiv_id, best_score)
        return out

    def _arxiv_search_phrase(self, title: str) -> list:
        if self._arxiv_circuit_open():
            return []
        # Strip punctuation that breaks Lucene phrase queries (notably ':')
        cleaned = re.sub(r"[^\w\s\-]", " ", title)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()[:200]
        if not cleaned:
            return []
        try:
            search = arxiv.Search(query=f'ti:"{cleaned}"', max_results=5,
                                  sort_by=arxiv.SortCriterion.Relevance)
            return list(self._arxiv_client.results(search))
        except Exception as e:
            logger.warning("arxiv phrase search failed for %r: %s", cleaned[:60], e)
            self._arxiv_record_failure()
            return []

    def _arxiv_search_keywords(self, title: str) -> list:
        if self._arxiv_circuit_open():
            return []
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
        query = " ".join(f"ti:{w}" for w in words)
        try:
            search = arxiv.Search(query=query, max_results=10,
                                  sort_by=arxiv.SortCriterion.Relevance)
            return list(self._arxiv_client.results(search))
        except Exception as e:
            logger.warning("arxiv keyword search failed for %r: %s", title[:60], e)
            self._arxiv_record_failure()
            return []

    def _normalize_s2(self, paper: S2Paper) -> NormalizedMeta:
        ext = paper.get("externalIds") or {}
        oa = paper.get("openAccessPdf")
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
            "doi": normalize_doi(ext.get("DOI")),
            "arxiv_id": normalize_arxiv_id(ext.get("ArXiv")),
            "citation_count": paper.get("citationCount"),
            "open_access_url": oa.get("url") if oa else None,
        }

    # ---------- Citations (reverse trace) ----------

    def get_citations(
        self,
        paper_id: str,
        limit: int = 1000,
        page_size: int = 100,
    ) -> list[dict]:
        """Fetch the list of papers that cite ``paper_id``, with their
        citation contexts, from Semantic Scholar.

        ``paper_id`` can be any identifier the S2 endpoint accepts:
        ``ARXIV:2211.14730``, ``DOI:10.48550/arxiv.2211.14730``, an
        OpenAlex id, or S2's own ``paperId``. Pagination is handled
        internally up to ``limit`` total citations.

        Returns a list of raw citation dicts. Each dict has keys
        ``contexts`` (list[str]), ``intents`` (list[str]), and
        ``citingPaper`` (dict with S2 metadata). Empty list on failure.
        """
        out: list[dict] = []
        offset = 0
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
        logger.info("Fetched %d citing paper(s) for %s", len(out), paper_id)
        return out

    # ---------- OpenReview ----------

    def _openreview_search_by_title(self, title: str) -> NormalizedMeta | None:
        """Search OpenReview by title. Tries v2 then v1 (ICLR<=2022 lives in v1).
        Returns {openreview_id, title, authors, abstract} on success.
        """
        if self._orev_circuit_open():
            logger.debug("OpenReview search skipped (circuit breaker open)")
            return None

        cache_key = normalize_title(title)[:120]
        hit, cached = self.meta_cache.get("orev", cache_key)
        if hit:
            return cached

        target = normalize_title(title)
        candidates: list[OpenReviewCandidate] = []
        failed = False
        for base in (OPENREVIEW_V2, OPENREVIEW_V1):
            try:
                r = requests.get(
                    f"{base}/notes/search",
                    params={"term": title[:200], "content": "all", "source": "forum", "limit": 5},
                    headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
                    timeout=OPENREVIEW_TIMEOUT_SECONDS,
                )
            except Exception as e:
                logger.warning("OpenReview %s failed: %s", base, e)
                failed = True
                continue
            if r.status_code != 200:
                logger.debug("OpenReview %s -> HTTP %s", base, r.status_code)
                continue
            for n in r.json().get("notes", []):
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
                self._orev_record_failure()
            return None

        self._orev_record_success()

        best = None
        best_score = 0.0
        for c in candidates:
            candidate = normalize_title(c["title"] or "")
            score = min(
                fuzz.token_set_ratio(target, candidate),
                fuzz.token_sort_ratio(target, candidate),
            )
            if score > best_score:
                best_score = score
                best = c

        if best is None or best_score < OPENREVIEW_FUZZY_MATCH_THRESHOLD:
            logger.debug("OpenReview: no good match for %r (best=%s)", title[:60], best_score)
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

    def _download_openreview(self, openreview_id: str) -> Path | None:
        out = self.pdf_dir / f"openreview_{openreview_id}.pdf"
        if out.exists() and out.stat().st_size > 0:
            return out
        try:
            r = requests.get(
                f"https://openreview.net/pdf?id={openreview_id}",
                headers={"User-Agent": BROWSER_UA, "Accept": "application/pdf,*/*"},
                timeout=PDF_DOWNLOAD_TIMEOUT_SECONDS,
                allow_redirects=True,
            )
        except Exception as e:
            logger.warning("OpenReview pdf download failed for %s: %s", openreview_id, e)
            return None
        if r.status_code != 200 or not r.content.startswith(b"%PDF"):
            logger.warning("OpenReview pdf bad response for %s (HTTP %s)", openreview_id, r.status_code)
            return None
        out.write_bytes(r.content)
        logger.info("Downloaded openreview:%s -> %s", openreview_id, out.name)
        return out

    # ---------- Sci-Hub download ----------

    def _download_scihub(self, doi: str) -> Path | None:
        """Try to download a paper from Sci-Hub mirrors by DOI."""
        safe = re.sub(r"[^\w\-.]", "_", doi)[:100]
        out = self.pdf_dir / f"scihub_{safe}.pdf"
        if out.exists() and out.stat().st_size > 0:
            return out

        for mirror in SCIHUB_MIRRORS:
            try:
                page_url = f"{mirror}/{doi}"
                r = requests.get(
                    page_url,
                    headers={"User-Agent": BROWSER_UA},
                    timeout=SCIHUB_TIMEOUT_SECONDS,
                    allow_redirects=True,
                )
            except Exception as e:
                logger.debug("Sci-Hub %s failed for %s: %s", mirror, doi, e)
                continue
            if r.status_code != 200:
                logger.debug("Sci-Hub %s -> HTTP %s for %s", mirror, r.status_code, doi)
                continue

            pdf_url = self._extract_scihub_pdf_url(r.text, mirror)
            if not pdf_url:
                logger.debug("Sci-Hub %s: no PDF URL found for %s", mirror, doi)
                continue

            try:
                pr = requests.get(
                    pdf_url,
                    headers={"User-Agent": BROWSER_UA},
                    timeout=SCIHUB_TIMEOUT_SECONDS,
                    allow_redirects=True,
                )
            except Exception as e:
                logger.debug("Sci-Hub PDF download failed from %s: %s", pdf_url, e)
                continue
            if pr.status_code == 200 and pr.content.startswith(b"%PDF"):
                out.write_bytes(pr.content)
                logger.info("Downloaded via Sci-Hub: %s -> %s", doi, out.name)
                return out
            logger.debug("Sci-Hub PDF bad response from %s (HTTP %s)", pdf_url, pr.status_code)

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
        if out.exists() and out.stat().st_size > 0:
            return out
        try:
            r = requests.get(
                url,
                headers={"User-Agent": BROWSER_UA},
                timeout=PDF_DOWNLOAD_TIMEOUT_SECONDS,
                allow_redirects=True,
            )
        except Exception as e:
            logger.warning("Generic PDF download failed for %s: %s", url, e)
            return None
        if r.status_code != 200 or not r.content.startswith(b"%PDF"):
            logger.debug("Generic PDF bad response from %s (HTTP %s)", url, r.status_code)
            return None
        out.write_bytes(r.content)
        logger.info("Downloaded OA PDF: %s -> %s", url[:80], out.name)
        return out

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

        url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        try:
            r = requests.get(
                url,
                timeout=PDF_DOWNLOAD_TIMEOUT_SECONDS,
                headers={"User-Agent": "citracer"},
            )
        except Exception as e:
            logger.warning("arxiv download failed for %s: %s", arxiv_id, e)
            return None
        if r.status_code != 200 or not r.content.startswith(b"%PDF"):
            logger.warning("arxiv download bad response for %s (HTTP %s)", arxiv_id, r.status_code)
            return None
        out.write_bytes(r.content)
        logger.info("Downloaded arxiv:%s -> %s", arxiv_id, out.name)
        return out
