"""Tests for citracer.metadata_enrichment — OpenAlex batch enrichment.

All HTTP calls are mocked via ``_get`` so no network access is required.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from citracer.metadata_cache import MetadataCache
from citracer.metadata_enrichment import MetadataEnricher


@pytest.fixture
def cache(tmp_path: Path) -> MetadataCache:
    return MetadataCache(tmp_path / "metadata.sqlite")


@pytest.fixture
def enricher(cache: MetadataCache) -> MetadataEnricher:
    return MetadataEnricher(cache, email="test@example.com")


def _oa_work(doi: str, title: str, year: int, cited: int, abstract_words=None):
    """Build a minimal OpenAlex work dict."""
    aii = None
    if abstract_words:
        aii = {w: [i] for i, w in enumerate(abstract_words)}
    return {
        "doi": f"https://doi.org/{doi}",
        "title": title,
        "publication_year": year,
        "cited_by_count": cited,
        "abstract_inverted_index": aii,
        "authorships": [{"author": {"display_name": f"Author of {title}"}}],
        "best_oa_location": None,
    }


# ---------------------------------------------------------------------------
# enrich_batch_by_dois
# ---------------------------------------------------------------------------

class TestBatchEnrichByDois:
    def test_empty_list(self, enricher):
        assert enricher.enrich_batch_by_dois([]) == {}

    def test_all_cached(self, enricher, cache):
        meta = {
            "title": "Cached",
            "authors": ["A"],
            "year": 2023,
            "abstract": "abs",
            "doi": "10.1/a",
            "citation_count": 5,
            "open_access_url": None,
        }
        cache.set("openalex", "doi:10.1/a", meta)

        with patch.object(enricher, "_get") as mock_get:
            result = enricher.enrich_batch_by_dois(["10.1/a"])

        mock_get.assert_not_called()
        assert result["10.1/a"]["title"] == "Cached"

    def test_single_batch(self, enricher):
        response = {
            "results": [
                _oa_work("10.1/a", "Paper A", 2023, 10, ["hello", "world"]),
                _oa_work("10.1/b", "Paper B", 2022, 5),
            ]
        }
        with patch.object(enricher, "_get", return_value=response) as mock_get:
            result = enricher.enrich_batch_by_dois(["10.1/a", "10.1/b"])

        # Only one HTTP call for both DOIs.
        mock_get.assert_called_once()
        assert result["10.1/a"]["citation_count"] == 10
        assert result["10.1/a"]["abstract"] == "hello world"
        assert result["10.1/b"]["citation_count"] == 5

    def test_negative_cache_on_miss(self, enricher, cache):
        with patch.object(enricher, "_get", return_value={"results": []}):
            enricher.enrich_batch_by_dois(["10.1/missing"])

        hit, cached = cache.get("openalex", "doi:10.1/missing")
        assert hit is True
        assert cached is None

    def test_api_failure_caches_negatives(self, enricher, cache):
        with patch.object(enricher, "_get", return_value=None):
            result = enricher.enrich_batch_by_dois(["10.1/x"])

        assert result == {}
        hit, _ = cache.get("openalex", "doi:10.1/x")
        assert hit is True

    def test_partial_results(self, enricher):
        response = {
            "results": [_oa_work("10.1/a", "Paper A", 2023, 10)]
        }
        with patch.object(enricher, "_get", return_value=response):
            result = enricher.enrich_batch_by_dois(["10.1/a", "10.1/gone"])

        assert "10.1/a" in result
        assert "10.1/gone" not in result

    def test_mixed_cached_and_uncached(self, enricher, cache):
        cache.set("openalex", "doi:10.1/cached", {
            "title": "C", "authors": [], "year": 2020,
            "abstract": None, "doi": "10.1/cached",
            "citation_count": 1, "open_access_url": None,
        })
        response = {
            "results": [_oa_work("10.1/fresh", "Fresh", 2024, 3)]
        }
        with patch.object(enricher, "_get", return_value=response) as mock_get:
            result = enricher.enrich_batch_by_dois(["10.1/cached", "10.1/fresh"])

        # HTTP call should only include the uncached DOI.
        assert "10.1/cached" in result
        assert "10.1/fresh" in result
        call_url = mock_get.call_args[0][0]
        assert "10.1/cached" not in call_url
        assert "10.1/fresh" in call_url


# ---------------------------------------------------------------------------
# enrich_by_doi (unchanged, sanity check)
# ---------------------------------------------------------------------------

class TestEnrichByDoi:
    def test_single_doi(self, enricher):
        work = _oa_work("10.1/x", "Paper X", 2023, 7, ["some", "abstract"])
        with patch.object(enricher, "_get", return_value=work):
            result = enricher.enrich_by_doi("10.1/x")

        assert result is not None
        assert result["citation_count"] == 7
        assert result["abstract"] == "some abstract"
