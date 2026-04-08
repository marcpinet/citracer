"""Utility functions: id normalization, hashing, logging."""
from __future__ import annotations
import hashlib
import logging
import re

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
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r"[^a-z0-9\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def title_hash(title: str) -> str:
    return hashlib.sha256(normalize_title(title).encode("utf-8")).hexdigest()[:16]


def make_paper_id(
    doi: str | None = None,
    arxiv_id: str | None = None,
    title: str | None = None,
) -> str:
    d = normalize_doi(doi)
    if d:
        return f"doi:{d}"
    a = normalize_arxiv_id(arxiv_id)
    if a:
        return f"arxiv:{a}"
    if title:
        return f"title:{title_hash(title)}"
    return "unknown:" + hashlib.sha256(b"unknown").hexdigest()[:8]
