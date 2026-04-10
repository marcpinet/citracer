"""Tests for citracer.source_resolver — input routing and validation.

We don't call the network here; the resolver's download methods are
provided by a fake that records calls and returns canned results.
"""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from citracer.source_resolver import resolve_source


class _FakeResolver:
    """Stand-in for ReferenceResolver that records the methods called and
    returns canned results from a local path."""

    def __init__(self, *, tmp_path: Path, fake_pdf_name: str = "fake.pdf",
                 arxiv_meta: dict | None = None,
                 scihub_returns: bool = False):
        self.tmp_path = tmp_path
        self.pdf_dir = tmp_path / "pdfs"
        self.pdf_dir.mkdir(exist_ok=True)
        self.fake_pdf = tmp_path / fake_pdf_name
        self.fake_pdf.write_bytes(b"%PDF-1.4 fake content")
        self.arxiv_meta = arxiv_meta
        self.scihub_returns = scihub_returns
        self.arxiv_calls: list[str] = []
        self.openreview_calls: list[str] = []
        self.s2_calls: list[str] = []
        self.scihub_calls: list[str] = []
        self.generic_calls: list[str] = []

    def download_arxiv(self, arxiv_id: str) -> Path | None:
        self.arxiv_calls.append(arxiv_id)
        return self.fake_pdf

    def download_openreview(self, forum_id: str) -> Path | None:
        self.openreview_calls.append(forum_id)
        return self.fake_pdf

    def s2_by_id(self, id_str: str) -> dict | None:
        self.s2_calls.append(id_str)
        return self.arxiv_meta

    def download_scihub(self, doi: str) -> Path | None:
        self.scihub_calls.append(doi)
        return self.fake_pdf if self.scihub_returns else None

    def download_generic_pdf(self, url: str, paper_id: str) -> Path | None:
        self.generic_calls.append(url)
        return None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_no_source_raises(self, tmp_path):
        r = _FakeResolver(tmp_path=tmp_path)
        with pytest.raises(ValueError, match="Exactly one"):
            resolve_source(pdf=None, doi=None, arxiv_id=None, url=None, resolver=r)

    def test_multiple_sources_raises(self, tmp_path):
        r = _FakeResolver(tmp_path=tmp_path)
        with pytest.raises(ValueError, match="Exactly one"):
            resolve_source(
                pdf="a.pdf", doi="10.1/abc", arxiv_id=None, url=None, resolver=r
            )


# ---------------------------------------------------------------------------
# --pdf
# ---------------------------------------------------------------------------

class TestPdfInput:
    def test_existing_local_pdf(self, tmp_path):
        p = tmp_path / "local.pdf"
        p.write_bytes(b"%PDF fake")
        r = _FakeResolver(tmp_path=tmp_path)
        out = resolve_source(pdf=str(p), doi=None, arxiv_id=None, url=None, resolver=r)
        assert out == p
        # No network calls
        assert r.arxiv_calls == []
        assert r.s2_calls == []

    def test_missing_local_pdf_raises(self, tmp_path):
        r = _FakeResolver(tmp_path=tmp_path)
        with pytest.raises(ValueError, match="PDF not found"):
            resolve_source(
                pdf=str(tmp_path / "nope.pdf"),
                doi=None, arxiv_id=None, url=None, resolver=r,
            )


# ---------------------------------------------------------------------------
# --arxiv
# ---------------------------------------------------------------------------

class TestArxivInput:
    def test_plain_id(self, tmp_path):
        r = _FakeResolver(tmp_path=tmp_path)
        out = resolve_source(
            pdf=None, doi=None, arxiv_id="2211.14730", url=None, resolver=r
        )
        assert out == r.fake_pdf
        assert r.arxiv_calls == ["2211.14730"]

    def test_id_with_version_normalized(self, tmp_path):
        r = _FakeResolver(tmp_path=tmp_path)
        resolve_source(
            pdf=None, doi=None, arxiv_id="2211.14730v2", url=None, resolver=r
        )
        assert r.arxiv_calls == ["2211.14730"]

    def test_download_failure_raises(self, tmp_path):
        r = _FakeResolver(tmp_path=tmp_path)
        r.download_arxiv = lambda aid: None  # type: ignore[assignment]
        with pytest.raises(ValueError, match="Could not download"):
            resolve_source(
                pdf=None, doi=None, arxiv_id="9999.99999", url=None, resolver=r
            )


# ---------------------------------------------------------------------------
# --doi
# ---------------------------------------------------------------------------

class TestDoiInput:
    def test_arxiv_doi_shortcut(self, tmp_path):
        # arxiv DOIs (10.48550/arxiv.*) should route directly to _download_arxiv
        # without a S2 lookup.
        r = _FakeResolver(tmp_path=tmp_path)
        resolve_source(
            pdf=None,
            doi="10.48550/arxiv.2211.14730",
            arxiv_id=None, url=None, resolver=r,
        )
        assert r.arxiv_calls == ["2211.14730"]
        assert r.s2_calls == []

    def test_generic_doi_via_s2(self, tmp_path):
        # Generic DOI: we consult S2 to find an arxiv id, then download.
        r = _FakeResolver(
            tmp_path=tmp_path,
            arxiv_meta={"arxiv_id": "2211.14730", "title": "PatchTST"},
        )
        resolve_source(
            pdf=None,
            doi="10.1145/99999.88888",
            arxiv_id=None, url=None, resolver=r,
        )
        assert r.s2_calls == ["DOI:10.1145/99999.88888"]
        assert r.arxiv_calls == ["2211.14730"]

    def test_generic_doi_no_arxiv_raises(self, tmp_path):
        r = _FakeResolver(tmp_path=tmp_path, arxiv_meta=None)
        with pytest.raises(ValueError, match="Could not find a downloadable PDF"):
            resolve_source(
                pdf=None, doi="10.1/unresolvable",
                arxiv_id=None, url=None, resolver=r,
            )


# ---------------------------------------------------------------------------
# --url
# ---------------------------------------------------------------------------

class TestUrlInput:
    def test_arxiv_abs_url(self, tmp_path):
        r = _FakeResolver(tmp_path=tmp_path)
        resolve_source(
            pdf=None, doi=None, arxiv_id=None,
            url="https://arxiv.org/abs/2211.14730",
            resolver=r,
        )
        assert r.arxiv_calls == ["2211.14730"]

    def test_arxiv_pdf_url(self, tmp_path):
        r = _FakeResolver(tmp_path=tmp_path)
        resolve_source(
            pdf=None, doi=None, arxiv_id=None,
            url="https://arxiv.org/pdf/2211.14730v2.pdf",
            resolver=r,
        )
        assert r.arxiv_calls == ["2211.14730"]

    def test_doi_url(self, tmp_path):
        r = _FakeResolver(
            tmp_path=tmp_path,
            arxiv_meta={"arxiv_id": "2211.14730"},
        )
        resolve_source(
            pdf=None, doi=None, arxiv_id=None,
            url="https://doi.org/10.1145/99999.88888",
            resolver=r,
        )
        assert r.s2_calls == ["DOI:10.1145/99999.88888"]

    def test_openreview_url(self, tmp_path):
        r = _FakeResolver(tmp_path=tmp_path)
        resolve_source(
            pdf=None, doi=None, arxiv_id=None,
            url="https://openreview.net/forum?id=cGDAkQo1C0p",
            resolver=r,
        )
        assert r.openreview_calls == ["cGDAkQo1C0p"]

    def test_unknown_url_raises(self, tmp_path):
        r = _FakeResolver(tmp_path=tmp_path)
        with pytest.raises(ValueError, match="Unrecognised URL"):
            resolve_source(
                pdf=None, doi=None, arxiv_id=None,
                url="https://example.com/paper.pdf", resolver=r,
            )
