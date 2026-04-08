"""Tests for citracer.reference_resolver — the arxiv-first cascade.

Every external call (S2 HTTP, arxiv client, OpenReview, PDF download) is
mocked. The real rate-limit state still runs so we can also validate the
thread-safety of the throttle (indirectly, via the lock being held).
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from citracer.models import BibEntry
from citracer.reference_resolver import ReferenceResolver


@pytest.fixture
def resolver(tmp_path: Path) -> ReferenceResolver:
    r = ReferenceResolver(cache_dir=tmp_path, s2_api_key=None, s2_min_interval=0.0)
    # Make PDF downloads deterministic: return a fake file path without HTTP
    def fake_download_arxiv(arxiv_id):
        p = tmp_path / "pdfs" / f"{arxiv_id.replace('/', '_')}.pdf"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"%PDF fake")
        return p
    r._download_arxiv = MagicMock(side_effect=fake_download_arxiv)
    return r


class _FakeArxivResult:
    """Shape vis.js of an arxiv.Result that the resolver reads."""
    def __init__(self, title: str, arxiv_id: str, summary: str = "abstract", doi: str | None = None):
        self.title = title
        self._sid = arxiv_id
        self.summary = summary
        self.doi = doi

    def get_short_id(self) -> str:
        return self._sid


# ---------------------------------------------------------------------------
# GROBID already gave us an arXiv id → direct download, no search needed
# ---------------------------------------------------------------------------

class TestDirectArxivId:
    def test_bib_arxiv_id_downloads_directly(self, resolver, tmp_path):
        bib = BibEntry(
            key="b0",
            title="A time series is worth 64 words",
            arxiv_id="2211.14730",
            year=2022,
        )
        # Arxiv search must NOT be called when we already have an id
        with patch.object(resolver, "_arxiv_search_by_title") as mock_search:
            mock_search.return_value = None
            result = resolver.resolve(bib)

        assert result.arxiv_id == "2211.14730"
        assert result.pdf_path is not None
        resolver._download_arxiv.assert_called_once_with("2211.14730")
        mock_search.assert_not_called()


# ---------------------------------------------------------------------------
# Arxiv-first fallback: title search (phrase, then keywords)
# ---------------------------------------------------------------------------

class TestArxivTitleSearch:
    def test_phrase_search_hit(self, resolver):
        bib = BibEntry(key="b0", title="A time series is worth 64 words long enough", year=2022)
        fake = _FakeArxivResult(
            title="A time series is worth 64 words long enough",
            arxiv_id="2211.14730",
        )
        with patch.object(resolver, "_arxiv_search_phrase", return_value=[fake]), \
             patch.object(resolver, "_arxiv_search_keywords", return_value=[]):
            result = resolver.resolve(bib)

        assert result.arxiv_id == "2211.14730"
        resolver._download_arxiv.assert_called_once_with("2211.14730")

    def test_phrase_fails_keyword_fallback(self, resolver):
        bib = BibEntry(key="b0", title="One Fits All: Power General Time Series", year=2023)
        fake = _FakeArxivResult(
            title="One Fits All:Power General Time Series",
            arxiv_id="2302.11939",
        )
        with patch.object(resolver, "_arxiv_search_phrase", return_value=[]), \
             patch.object(resolver, "_arxiv_search_keywords", return_value=[fake]):
            result = resolver.resolve(bib)

        assert result.arxiv_id == "2302.11939"

    def test_low_fuzzy_score_rejected(self, resolver):
        bib = BibEntry(key="b0", title="Channel-independent time series forecasting")
        # An arxiv result whose title is totally different
        fake = _FakeArxivResult(title="Cryptography primer", arxiv_id="9999.00001")
        with patch.object(resolver, "_arxiv_search_phrase", return_value=[fake]), \
             patch.object(resolver, "_arxiv_search_keywords", return_value=[]), \
             patch.object(resolver, "_s2_lookup", return_value=None), \
             patch.object(resolver, "_openreview_search_by_title", return_value=None):
            result = resolver.resolve(bib)

        # No confident arxiv match → no arxiv_id
        assert result.arxiv_id is None


# ---------------------------------------------------------------------------
# S2 fallback when arxiv has nothing
# ---------------------------------------------------------------------------

class TestS2Fallback:
    def test_s2_reached_only_after_arxiv_fails(self, resolver):
        bib = BibEntry(key="b0", title="Some paper not on arxiv", year=2021)
        s2_meta = {
            "title": "Some paper not on arxiv",
            "authors": ["A. Author"],
            "year": 2021,
            "abstract": None,
            "doi": "10.1/not-on-arxiv",
            "arxiv_id": None,
        }
        with patch.object(resolver, "_arxiv_search_by_title", return_value=None), \
             patch.object(resolver, "_s2_lookup", return_value=s2_meta) as mock_s2, \
             patch.object(resolver, "_openreview_search_by_title", return_value=None):
            result = resolver.resolve(bib)
            mock_s2.assert_called_once()

        assert result.doi == "10.1/not-on-arxiv"
        assert result.pdf_path is None  # no arxiv id, no PDF

    def test_s2_not_reached_when_arxiv_succeeds(self, resolver):
        bib = BibEntry(key="b0", title="A title", arxiv_id="2211.14730")
        # We have an arxiv_id from GROBID, so arxiv search is skipped but
        # we also shouldn't hit S2 (arxiv download is enough).
        with patch.object(resolver, "_s2_lookup") as mock_s2, \
             patch.object(resolver, "_openreview_search_by_title") as mock_orev:
            resolver.resolve(bib)
            mock_s2.assert_not_called()
            mock_orev.assert_not_called()


# ---------------------------------------------------------------------------
# OpenReview — last resort
# ---------------------------------------------------------------------------

class TestOpenReviewFallback:
    def test_openreview_used_when_arxiv_and_s2_fail(self, resolver, tmp_path):
        bib = BibEntry(
            key="b0",
            title="Reversible instance normalization for accurate time-series forecasting",
            year=2021,
        )
        orev = {
            "openreview_id": "cGDAkQo1C0p",
            "title": "Reversible instance normalization for accurate time-series forecasting",
            "authors": ["Taesung Kim"],
            "abstract": "abstract",
        }
        fake_pdf = tmp_path / "pdfs" / "orev.pdf"
        with patch.object(resolver, "_arxiv_search_by_title", return_value=None), \
             patch.object(resolver, "_s2_lookup", return_value=None), \
             patch.object(resolver, "_openreview_search_by_title", return_value=orev), \
             patch.object(resolver, "_download_openreview", return_value=fake_pdf):
            result = resolver.resolve(bib)

        assert result.openreview_id == "cGDAkQo1C0p"
        assert result.pdf_path == fake_pdf
        assert result.url == "https://openreview.net/forum?id=cGDAkQo1C0p"


# ---------------------------------------------------------------------------
# All sources fail → still returns a ResolvedRef with metadata, no pdf
# ---------------------------------------------------------------------------

class TestAllFail:
    def test_graceful_fallback(self, resolver):
        bib = BibEntry(
            key="b0",
            title="A paper that exists nowhere online, apparently",
            authors=["Ghost Author"],
            year=2019,
        )
        with patch.object(resolver, "_arxiv_search_by_title", return_value=None), \
             patch.object(resolver, "_s2_lookup", return_value=None), \
             patch.object(resolver, "_openreview_search_by_title", return_value=None):
            result = resolver.resolve(bib)

        assert result.pdf_path is None
        assert result.arxiv_id is None
        assert result.openreview_id is None
        # Metadata from the bib still propagated
        assert result.title == "A paper that exists nowhere online, apparently"
        assert result.year == 2019
        assert result.authors == ["Ghost Author"]


# ---------------------------------------------------------------------------
# 429 backoff
# ---------------------------------------------------------------------------

class TestS2Backoff:
    def test_429_retried_then_success(self, resolver, monkeypatch):
        calls = {"n": 0}

        class _Resp:
            def __init__(self, status, data=None):
                self.status_code = status
                self._data = data or {}
            def json(self):
                return self._data

        def fake_get(url, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _Resp(429)
            return _Resp(200, {
                "title": "Found",
                "authors": [{"name": "Me"}],
                "year": 2021,
                "externalIds": {},
            })

        # Monkeypatch the requests module inside reference_resolver
        import citracer.reference_resolver as rr
        monkeypatch.setattr(rr.requests, "get", fake_get)
        # Speed up backoff for the test
        monkeypatch.setattr(rr, "S2_429_BACKOFF_DELAYS", (0.0, 0.0, 0.0))

        result = resolver._s2_get("http://x", "test")
        assert result is not None
        assert result["title"] == "Found"
        assert calls["n"] == 2  # one 429, then success


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------

class TestCache:
    def test_s2_lookup_cached(self, resolver):
        bib = BibEntry(key="b0", title="Title X", year=2020)
        meta = {"title": "Title X", "year": 2020, "doi": "10.1/x", "arxiv_id": None,
                "authors": [], "abstract": None}
        with patch.object(resolver, "_s2_by_id", return_value=None), \
             patch.object(resolver, "_s2_search", return_value=meta) as mock_search:
            resolver._s2_lookup(bib)
            resolver._s2_lookup(bib)  # second call — should hit cache
        assert mock_search.call_count == 1  # only first call hit S2

    def test_arxiv_search_cached(self, resolver):
        bib = BibEntry(key="b0", title="Channel independent baseline methods")
        fake = _FakeArxivResult(title="Channel independent baseline methods", arxiv_id="1111.11111")
        with patch.object(resolver, "_arxiv_search_phrase", return_value=[fake]) as mock_phrase:
            resolver._arxiv_search_by_title("Channel independent baseline methods")
            resolver._arxiv_search_by_title("Channel independent baseline methods")
        assert mock_phrase.call_count == 1
