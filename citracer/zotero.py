"""Push the papers of a trace into a Zotero library (Zotero Web API v3).

Each paper becomes a Zotero item (``preprint`` for arXiv-only papers,
``journalArticle`` otherwise) in a collection, tagged ``citracer``,
``citracer:<status>`` and with the traced keywords. The passages where the
keyword was found are attached as a child note, so the reason a paper is in
the library travels with it.

Re-running is safe: papers already in the target collection (same DOI,
arXiv id or title) are skipped, so a ``--diff`` monitoring run only adds
the new papers.

Authentication uses a Zotero API key with write access
(https://www.zotero.org/settings/keys). The library defaults to the key
owner's personal library; pass ``groups/<id>`` for a group library.
"""
from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass, field

import requests

from .http_client import session
from .models import PaperNode
from .utils import normalize_arxiv_id, normalize_doi, normalize_title

logger = logging.getLogger(__name__)

ZOTERO_API = "https://api.zotero.org"
#: The write API accepts at most 50 objects per request.
ZOTERO_BATCH_SIZE = 50
_MAX_RETRIES = 4


class ZoteroError(RuntimeError):
    pass


@dataclass
class ZoteroResult:
    collection_key: str | None = None
    created: int = 0
    skipped: int = 0
    notes: int = 0
    failed: list[str] = field(default_factory=list)


class ZoteroClient:
    def __init__(self, api_key: str, library: str | None = None) -> None:
        """``library``: ``users/<id>`` or ``groups/<id>``; None resolves the
        key owner's personal library."""
        self.api_key = api_key
        self._library = library

    # ---------- HTTP ----------

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        headers = {
            "Zotero-API-Key": self.api_key,
            "Zotero-API-Version": "3",
            "User-Agent": "citracer",
        }
        url = path if path.startswith("http") else f"{ZOTERO_API}{path}"
        for attempt in range(_MAX_RETRIES):
            try:
                r = session().request(method, url, headers=headers, timeout=30, **kwargs)
            except requests.RequestException as e:
                raise ZoteroError(f"Zotero unreachable: {e}") from e
            # Zotero asks clients to slow down with Backoff / Retry-After.
            wait = _seconds(r.headers.get("Retry-After") or r.headers.get("Backoff"))
            if r.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
                time.sleep(wait or 2 ** attempt)
                continue
            if wait and r.status_code < 400:
                time.sleep(wait)
            if r.status_code in (401, 403):
                raise ZoteroError(
                    f"Zotero refused the request (HTTP {r.status_code}): check that "
                    "the API key is valid and has write access to this library."
                )
            if r.status_code >= 400:
                raise ZoteroError(f"Zotero {method} {path} -> HTTP {r.status_code}: {r.text[:200]}")
            return r
        raise ZoteroError(f"Zotero {method} {path}: still rate-limited after retries")

    @property
    def library(self) -> str:
        if self._library is None:
            data = self._request("GET", f"/keys/{self.api_key}").json()
            user_id = data.get("userID")
            if not user_id:
                raise ZoteroError("Could not determine the Zotero user of this API key.")
            self._library = f"users/{user_id}"
        return self._library

    def _paged(self, path: str, params: dict) -> list[dict]:
        out: list[dict] = []
        start = 0
        while True:
            r = self._request("GET", f"/{self.library}{path}",
                              params={**params, "limit": 100, "start": start})
            page = r.json()
            out.extend(page)
            total = int(r.headers.get("Total-Results", len(out)))
            start += len(page)
            if not page or start >= total:
                return out

    # ---------- collections ----------

    def find_or_create_collection(self, name: str) -> str:
        for c in self._paged("/collections", {}):
            data = c.get("data", {})
            if data.get("name") == name and not data.get("parentCollection"):
                return c["key"]
        r = self._request("POST", f"/{self.library}/collections", json=[{"name": name}])
        created = r.json().get("successful", {}).get("0")
        if not created:
            raise ZoteroError(f"Could not create collection {name!r}: {r.text[:200]}")
        logger.info("Created Zotero collection %r", name)
        return created["key"]

    def existing_keys(self, collection_key: str) -> set[str]:
        """Identity keys (DOI / arXiv / title) of the collection's items."""
        keys: set[str] = set()
        for item in self._paged(f"/collections/{collection_key}/items/top", {"format": "json"}):
            data = item.get("data", {})
            keys |= _identity(
                doi=data.get("DOI"),
                arxiv_id=_arxiv_from_item(data),
                title=data.get("title"),
            )
        return keys

    # ---------- items ----------

    def create(self, objects: list[dict]) -> list[str | None]:
        """Create items/notes in batches; returns the new key of each object
        (None where Zotero rejected it)."""
        keys: list[str | None] = []
        for i in range(0, len(objects), ZOTERO_BATCH_SIZE):
            chunk = objects[i:i + ZOTERO_BATCH_SIZE]
            data = self._request("POST", f"/{self.library}/items", json=chunk).json()
            ok = data.get("successful", {})
            for j in range(len(chunk)):
                entry = ok.get(str(j))
                keys.append(entry["key"] if entry else None)
            for j, err in (data.get("failed") or {}).items():
                logger.warning("Zotero rejected %r: %s",
                               chunk[int(j)].get("title", "note"), err.get("message"))
        return keys


def _seconds(value: str | None) -> float:
    """Delay from a Backoff / Retry-After header, capped at 60s."""
    try:
        return min(max(float(value), 0.0), 60.0) if value else 0.0
    except (TypeError, ValueError):
        return 0.0


def _identity(doi: str | None = None, arxiv_id: str | None = None, title: str | None = None) -> set[str]:
    keys = set()
    if normalize_doi(doi):
        keys.add(f"doi:{normalize_doi(doi)}")
    if normalize_arxiv_id(arxiv_id):
        keys.add(f"arxiv:{normalize_arxiv_id(arxiv_id)}")
    if normalize_title(title):
        keys.add(f"title:{normalize_title(title)}")
    return keys


def _arxiv_from_item(data: dict) -> str | None:
    for field_name in ("archiveID", "extra"):
        value = data.get(field_name) or ""
        for line in value.splitlines():
            line = line.strip()
            if line.lower().startswith("arxiv:"):
                return line.split(":", 1)[1].strip()
    return None


def node_to_item(node: PaperNode, collection_key: str | None, keywords: list[str]) -> dict:
    """Zotero item JSON for a paper."""
    from .bibliography import split_name

    preprint = bool(node.arxiv_id and not node.doi)
    item: dict = {
        "itemType": "preprint" if preprint else "journalArticle",
        "title": node.title or "(untitled)",
        "creators": [],
        "abstractNote": node.abstract or "",
        "date": node.publication_date or (str(node.year) if node.year else ""),
        "DOI": node.doi or "",
        "url": node.url or "",
        "tags": [{"tag": "citracer"}, {"tag": f"citracer:{node.status}"}]
                + [{"tag": kw} for kw in keywords],
        "collections": [collection_key] if collection_key else [],
        "extra": "\n".join(filter(None, [
            f"arXiv: {node.arxiv_id}" if node.arxiv_id and not preprint else "",
            f"Citations: {node.citation_count}" if node.citation_count is not None else "",
        ])),
    }
    if preprint:
        item["repository"] = "arXiv"
        item["archiveID"] = f"arXiv:{node.arxiv_id}"
    for name in node.authors:
        given, family = split_name(name)
        item["creators"].append({"creatorType": "author", "firstName": given, "lastName": family})
    return item


def node_note(node: PaperNode, keywords: list[str]) -> str:
    """HTML body of the child note listing the keyword passages."""
    head = (f"<p><b>citracer</b> — {html.escape(node.status.replace('_', ' '))} "
            f"at depth {node.depth}, keyword(s): {html.escape(', '.join(keywords))}</p>")
    passages = "".join(f"<blockquote>{html.escape(p)}</blockquote>" for p in node.keyword_hits)
    return head + passages


def push_papers(
    client: ZoteroClient,
    papers: list[PaperNode],
    collection: str,
    keywords: list[str],
    notes: bool = True,
) -> ZoteroResult:
    """Add ``papers`` to the ``collection`` (created if needed), skipping
    those already in it; each gets a child note with its keyword passages."""
    result = ZoteroResult()
    result.collection_key = client.find_or_create_collection(collection)
    existing = client.existing_keys(result.collection_key)

    todo: list[PaperNode] = []
    for n in papers:
        ids = _identity(n.doi, n.arxiv_id, n.title)
        if ids & existing:
            result.skipped += 1
            continue
        existing |= ids
        todo.append(n)
    if not todo:
        return result

    keys = client.create([node_to_item(n, result.collection_key, keywords) for n in todo])
    note_objects = []
    for n, key in zip(todo, keys):
        if key is None:
            result.failed.append(n.title)
            continue
        result.created += 1
        if notes and n.keyword_hits:
            note_objects.append({
                "itemType": "note", "parentItem": key,
                "note": node_note(n, keywords), "tags": [{"tag": "citracer"}],
            })
    if note_objects:
        result.notes = sum(1 for k in client.create(note_objects) if k)
    return result
