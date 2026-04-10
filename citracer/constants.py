"""Centralized tuning constants for citracer.

Every threshold / magic number used across the codebase lives here so they
can be audited, documented and eventually exposed via CLI flags or a config
file without hunting for them in the source.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# PDF parser (pdf_parser.py)
# ---------------------------------------------------------------------------

#: Minimum number of math Unicode characters in a <p> element for it to be
#: classified as "figure noise" and skipped from the body text. GROBID
#: sometimes promotes figure diagrams to regular paragraphs; those contain
#: mathematical Unicode (𝑥, ℝ, ∈, ⊗, ...) that pollutes the keyword matcher.
FIGURE_NOISE_MATH_CHAR_THRESHOLD: int = 3

#: GROBID request timeout, in seconds.
GROBID_TIMEOUT_SECONDS: float = 300.0

#: Default number of concurrent GROBID parse requests. The upstream Docker
#: image has 10 workers by default, so 4-8 is a safe sweet spot.
GROBID_DEFAULT_WORKERS: int = 4


# ---------------------------------------------------------------------------
# Keyword matcher (keyword_matcher.py)
# ---------------------------------------------------------------------------

#: Minimum token length (in characters) beyond which the last keyword token
#: gets its suffix truncated to allow morphological variants (e.g.
#: "independent" -> "independe\\w*" matches "independence", "independently").
KEYWORD_MORPHO_MIN_LEN: int = 4

#: Default sentence-transformer model for --semantic matching.
#: all-mpnet-base-v2 (420MB) outperforms all-MiniLM-L6-v2 (80MB)
#: on academic text: F1=0.93 vs 0.86 at threshold 0.30, with zero
#: false positives on our benchmark. The load-time cost (~11s on
#: first call) is one-time and negligible for deep traces.
SEMANTIC_DEFAULT_MODEL: str = "all-mpnet-base-v2"

#: Cosine similarity threshold for semantic matching. Benchmarked
#: at 0.40 on real academic text from deep traces (depth 5): zero
#: false positives while keeping 4/7 true conceptual matches. Lower
#: values (0.30-0.35) match generic DL/ML sentences about layers,
#: attention, and decoders that have nothing to do with the keyword.
SEMANTIC_SIMILARITY_THRESHOLD: float = 0.40


# ---------------------------------------------------------------------------
# Reference resolver (reference_resolver.py)
# ---------------------------------------------------------------------------

#: Minimum interval between Semantic Scholar requests when an API key is set.
#: S2 enforces ~1 req/sec per key; we stay safely under the limit.
S2_MIN_INTERVAL_WITH_KEY: float = 1.1

#: Minimum interval between S2 requests without an API key. The public
#: endpoint is strictly rate-limited.
S2_MIN_INTERVAL_WITHOUT_KEY: float = 3.5

#: Backoff delays (in seconds) for retrying after a 429 Too Many Requests.
#: Length of the list == number of retry attempts.
S2_429_BACKOFF_DELAYS: tuple[float, ...] = (0.0, 5.0, 15.0, 40.0)

#: Number of consecutive 429s after which the resolver gives up on S2 and
#: short-circuits subsequent calls for the cooldown period below. Without
#: an API key, S2 hammering wastes ~60s per failed lookup; this caps the
#: damage at ``threshold * worst_case_per_call``.
S2_429_CIRCUIT_BREAKER_THRESHOLD: int = 3

#: After tripping the S2 circuit breaker, skip S2 calls for this many
#: seconds. After the cooldown the breaker resets and we try again.
S2_CIRCUIT_BREAKER_COOLDOWN_SECONDS: float = 120.0

#: After an arxiv 429 / 503, skip arxiv search calls for this many seconds.
#: arxiv recovers slowly from rate-limits and aggressive retry just makes
#: it worse for everyone.
ARXIV_COOLDOWN_AFTER_FAILURE_SECONDS: float = 60.0

#: Number of consecutive OpenReview failures (timeouts, HTTP errors) after
#: which we skip OpenReview for the cooldown period. Each failed search
#: tries v2 + v1 endpoints (2 × timeout), so 2 consecutive failures
#: waste ~40s. Tripping after 2 saves minutes on deep traces.
OPENREVIEW_CIRCUIT_BREAKER_THRESHOLD: int = 2

#: After tripping the OpenReview circuit breaker, skip OpenReview calls
#: for this many seconds.
OPENREVIEW_CIRCUIT_BREAKER_COOLDOWN_SECONDS: float = 120.0

#: OpenReview search request timeout (seconds). Reduced from 20s to 10s
#: to fail faster when the service is unresponsive.
OPENREVIEW_TIMEOUT_SECONDS: float = 10.0

#: Minimum delay (seconds) between arxiv.org API requests. arxiv asks users
#: to stay under ~3 seconds between requests; the `arxiv` package enforces
#: this internally but we also use it for downloads.
ARXIV_MIN_INTERVAL: float = 3.0

#: rapidfuzz token_set_ratio threshold for accepting an arXiv search hit
#: as the correct paper (0-100 scale).
ARXIV_FUZZY_MATCH_THRESHOLD: int = 85

#: Same threshold for OpenReview hits.
OPENREVIEW_FUZZY_MATCH_THRESHOLD: int = 85

#: Maximum number of results to request from arXiv per page.
ARXIV_PAGE_SIZE: int = 5

#: Number of retries on arxiv client failures.
ARXIV_NUM_RETRIES: int = 3

#: PDF download timeout (seconds) for arxiv and OpenReview. Kept short
#: enough that a Ctrl+C interrupts within ~1 minute even if a worker is
#: stuck on a slow download.
PDF_DOWNLOAD_TIMEOUT_SECONDS: float = 60.0

#: Max number of distinctive title words used when the phrase search on
#: arXiv fails and we fall back to a non-phrase keyword query.
ARXIV_KEYWORD_SEARCH_MAX_WORDS: int = 8

#: Minimum length for a title word to count in the keyword fallback query.
ARXIV_KEYWORD_SEARCH_MIN_WORD_LEN: int = 4

#: Sci-Hub mirror URLs, tried in order. The first one that responds wins.
SCIHUB_MIRRORS: tuple[str, ...] = (
    "https://www.sci-hub.in",
    "https://sci-hub.hlgczx.com",
)

#: Timeout for Sci-Hub page fetch and PDF download (seconds).
SCIHUB_TIMEOUT_SECONDS: float = 30.0


# ---------------------------------------------------------------------------
# OpenAlex metadata enrichment (metadata_enrichment.py)
# ---------------------------------------------------------------------------

#: Minimum interval between OpenAlex requests with an email (polite pool).
OPENALEX_MIN_INTERVAL_WITH_EMAIL: float = 0.2

#: Minimum interval between OpenAlex requests without an email (anonymous).
OPENALEX_MIN_INTERVAL_WITHOUT_EMAIL: float = 1.0

#: OpenAlex request timeout (seconds).
OPENALEX_TIMEOUT_SECONDS: float = 15.0


# ---------------------------------------------------------------------------
# Tracer (tracer.py)
# ---------------------------------------------------------------------------

#: Max plausible gap between two candidate years for the same paper.
#: GROBID's bibliography parser occasionally produces garbage years (e.g.
#: it grabs a page number, or the year of a neighbouring citation in the
#: raw text). We only honour the older candidate when it's within this
#: window — typical preprint -> final-publication gaps are 0-2 years.
YEAR_GAP_THRESHOLD: int = 2

#: rapidfuzz threshold used when matching a bibliography entry to an
#: existing graph node by fuzzy title comparison in the secondary-edge pass.
CROSS_CITATION_FUZZY_THRESHOLD: int = 88

#: Minimum normalized title length below which fuzzy matching is skipped
#: (very short titles are too ambiguous and produce false positives).
CROSS_CITATION_MIN_TITLE_LEN: int = 15


# ---------------------------------------------------------------------------
# Visualizer (visualizer.py + overlay template)
# ---------------------------------------------------------------------------

#: Starting node size for new nodes before in-degree-based resizing kicks in.
NODE_INITIAL_SIZE: int = 20

#: Minimum size for the root node (overrides any smaller computed value).
NODE_ROOT_MIN_SIZE: int = 30
