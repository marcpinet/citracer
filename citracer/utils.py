"""Utility functions: id normalization, hashing, logging."""
from __future__ import annotations
import hashlib
import logging
import re
import unicodedata
import uuid
from datetime import date

from tqdm import tqdm


class _TqdmSafeHandler(logging.StreamHandler):
    """A logging handler that routes output through ``tqdm.write`` so that
    log lines don't tear through an active progress bar."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            tqdm.write(msg)
            self.flush()
        except Exception:
            self.handleError(record)


def setup_logging(level: int = logging.INFO) -> None:
    handler = _TqdmSafeHandler()
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))

    root = logging.getLogger()
    # Replace any handlers from a previous setup (re-running in tests etc).
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)

    # Quiet down third-party loggers that are chatty at INFO:
    #   - `arxiv` logs "Requesting page", "Sleeping", etc. on every call
    #   - `urllib3` is similar for connection pool events
    for noisy in ("arxiv", "urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = doi.strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    return doi or None


_ARXIV_DOI_RE = re.compile(r"^10\.48550/arxiv\.(.+)$", re.IGNORECASE)


def arxiv_id_from_doi(doi: str | None) -> str | None:
    """Return the arXiv id behind an arXiv DataCite DOI (``10.48550/arXiv.X``),
    or None for any other DOI."""
    d = normalize_doi(doi)
    if not d:
        return None
    m = _ARXIV_DOI_RE.match(d)
    return normalize_arxiv_id(m.group(1)) if m else None


def normalize_arxiv_id(arxiv_id: str | None) -> str | None:
    if not arxiv_id:
        return None
    s = arxiv_id.strip().lower()
    # Drop "arxiv:" scheme prefix if present
    s = re.sub(r"^arxiv:\s*", "", s)
    # Drop category hints like "[cs.lg]" that GROBID sometimes keeps attached
    s = re.sub(r"\s*\[[^\]]*\]\s*", "", s)
    # Drop trailing version suffix "v1", "v10", etc.
    s = re.sub(r"v\d+$", "", s)
    s = s.strip()
    return s or None


def normalize_title(title: str | None) -> str:
    """Lowercase, accent-folded, punctuation-free form of a title.

    Letters of every script are kept (a Chinese or Cyrillic title must not
    normalize to the empty string), diacritics are folded ("Análisis" ->
    "analisis") and everything else that isn't a letter, digit or space
    is dropped.
    """
    if not title:
        return ""
    t = unicodedata.normalize("NFKD", title)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.casefold()
    t = re.sub(r"[^\w\s]|_", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def title_hash(title: str) -> str:
    # A title made only of punctuation normalizes to "": hash the raw
    # string instead so such titles don't all collide on sha256("").
    key = normalize_title(title) or title.strip().casefold()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def make_paper_id(
    doi: str | None = None,
    arxiv_id: str | None = None,
    title: str | None = None,
) -> str:
    # arXiv DataCite DOIs identify the preprint itself: map them to the
    # arxiv id so the same paper doesn't get both "doi:10.48550/arxiv.X"
    # and "arxiv:X" nodes.
    doi_arxiv = arxiv_id_from_doi(doi)
    if doi_arxiv:
        arxiv_id = arxiv_id or doi_arxiv
        doi = None
    d = normalize_doi(doi)
    if d:
        return f"doi:{d}"
    a = normalize_arxiv_id(arxiv_id)
    if a:
        return f"arxiv:{a}"
    if title and title.strip():
        return f"title:{title_hash(title)}"
    # Nothing identifies this paper: give it a unique id rather than a
    # constant one, otherwise every such paper collapses into one node.
    return f"unknown:{uuid.uuid4().hex[:12]}"


#: Oldest year accepted as a publication year. Older values are almost
#: always parser noise; newer ones may be real (Shannon 1948, Bayes 1763).
MIN_PLAUSIBLE_YEAR = 1600


def plausible_year(y: int | None) -> bool:
    if y is None:
        return False
    return MIN_PLAUSIBLE_YEAR <= y <= date.today().year + 1
