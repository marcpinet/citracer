"""Shared HTTP plumbing: pooled sessions and safe PDF downloads.

Every module used to call ``requests.get`` directly, which opens a fresh
TCP + TLS connection per request. A per-thread ``requests.Session`` keeps
connections alive across calls to the same host (``Session`` objects are
not guaranteed thread-safe, hence one per thread).
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

from .constants import PDF_MAX_BYTES

logger = logging.getLogger(__name__)

_local = threading.local()


class TransientError(Exception):
    """The remote service failed in a way that may succeed later (network
    error, timeout, HTTP 429 / 5xx). Callers must not cache such failures."""


def session() -> requests.Session:
    """Return this thread's pooled ``requests.Session``."""
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _local.session = s
    return s


def is_transient_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


# One lock per destination file, so two threads resolving the same paper
# don't download it twice or interleave their writes.
_path_locks: dict[str, threading.Lock] = {}
_path_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _path_locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = _path_locks[key] = threading.Lock()
        return lock


def download_pdf(
    url: str,
    out: Path,
    *,
    timeout: float,
    headers: dict | None = None,
    max_bytes: int = PDF_MAX_BYTES,
) -> Path | None:
    """Stream ``url`` into ``out`` if it is a PDF.

    The body is streamed to a temporary file and atomically renamed, so an
    interrupted download never leaves a truncated PDF that later runs would
    trust. Returns ``out`` on success (or if it already exists), None if the
    server answered with something that isn't a usable PDF.

    Raises:
        TransientError: network error, timeout, HTTP 429 or 5xx.
    """
    with _lock_for(out):
        if out.exists() and out.stat().st_size > 0:
            return out
        tmp = out.with_name(f"{out.name}.{os.getpid()}.{threading.get_ident()}.part")
        try:
            with session().get(
                url, headers=headers, timeout=timeout,
                stream=True, allow_redirects=True,
            ) as r:
                if is_transient_status(r.status_code):
                    raise TransientError(f"HTTP {r.status_code} from {url}")
                if r.status_code != 200:
                    logger.debug("PDF download %s -> HTTP %s", url, r.status_code)
                    return None
                head = b""
                size = 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > max_bytes:
                            logger.warning("PDF at %s exceeds %d bytes, skipped", url, max_bytes)
                            return None
                        if len(head) < 5:
                            head += chunk[:5]
                            if len(head) >= 5 and not head.startswith(b"%PDF"):
                                logger.debug("Not a PDF at %s (starts with %r)", url, head)
                                return None
                        f.write(chunk)
                if not head.startswith(b"%PDF"):
                    return None
            os.replace(tmp, out)
            return out
        except requests.RequestException as e:
            raise TransientError(str(e)) from e
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass


def write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a temporary file + rename."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.part")
    tmp.write_bytes(data)
    os.replace(tmp, path)
