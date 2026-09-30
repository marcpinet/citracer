"""Regression tests for the bug-fix / performance pass: identity handling,
caching rules, safe downloads, HTML escaping and secret redaction."""
import gzip
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from citracer import analytics, pdf_parser
from citracer.http_client import TransientError, download_pdf
from citracer.manifest import redact_argv
from citracer.models import BibEntry, PaperNode, TracerGraph
from citracer.reference_resolver import ReferenceResolver
from citracer.utils import make_paper_id, normalize_title, plausible_year
from citracer.visualizer import _keyword_patterns_for_js, _script_json


# ---------------------------------------------------------------------------
# Identities
# ---------------------------------------------------------------------------

class TestIdentity:
    def test_non_latin_titles_do_not_collide(self):
        a = make_paper_id(title="深度学习的综述")
        b = make_paper_id(title="Обзор методов")
        assert a != b

    def test_accents_are_folded_not_dropped(self):
        assert normalize_title("Análisis de séries") == "analisis de series"

    def test_arxiv_doi_maps_to_arxiv_id(self):
        assert make_paper_id(doi="10.48550/arXiv.2211.14730") == make_paper_id(arxiv_id="2211.14730")

    def test_unknown_ids_are_unique(self):
        assert make_paper_id() != make_paper_id()

    def test_pre_1970_years_are_plausible(self):
        assert plausible_year(1948)
        assert not plausible_year(1200)


class TestGraphAliases:
    def test_find_by_other_identifier(self):
        g = TracerGraph()
        g.add_node(PaperNode(paper_id="doi:10.1/x", title="T", doi="10.1/x", arxiv_id="2211.14730"))
        assert g.find(paper_id="arxiv:2211.14730", arxiv_id="2211.14730") == "doi:10.1/x"
        assert g.find(doi="10.48550/arxiv.2211.14730") == "doi:10.1/x"

    def test_title_match_rejected_when_dois_conflict(self):
        g = TracerGraph()
        title = "A fairly long and specific paper title"
        g.add_node(PaperNode(paper_id="doi:10.1/a", title=title, doi="10.1/a", year=2020))
        assert g.find(title=title, year=2020) == "doi:10.1/a"
        assert g.find(title=title, doi="10.1/b", year=2020) is None
        assert g.find(title=title, year=2010) is None

    def test_absorb_fills_missing_fields(self):
        g = TracerGraph()
        g.add_node(PaperNode(paper_id="arxiv:1", title="T", arxiv_id="1"))
        g.absorb("arxiv:1", doi="10.1/z", citation_count=5, paper_id="doi:10.1/z")
        assert g.nodes["arxiv:1"].doi == "10.1/z"
        assert g.find(paper_id="doi:10.1/z") == "arxiv:1"


# ---------------------------------------------------------------------------
# Resolver caching rules
# ---------------------------------------------------------------------------

@pytest.fixture
def resolver(tmp_path: Path) -> ReferenceResolver:
    r = ReferenceResolver(cache_dir=tmp_path, s2_api_key=None, s2_min_interval=0.0)
    r._download_scihub = MagicMock(return_value=None)
    r._try_preprint_download = MagicMock(return_value=None)
    yield r
    r.close()


def _unavailable(resolver):
    return (
        patch.object(resolver, "_arxiv_search_by_title", return_value=None),
        patch.object(resolver, "_s2_lookup", return_value=None),
        patch.object(resolver, "_openreview_search_by_title", return_value=None),
    )


class TestResolverMemo:
    def test_same_reference_resolved_once_per_run(self, resolver):
        bib = BibEntry(key="b0", title="A reference cited by many papers", year=2020)
        with patch.object(resolver, "_arxiv_search_by_title", return_value=None) as arx, \
             patch.object(resolver, "_s2_lookup", return_value=None), \
             patch.object(resolver, "_openreview_search_by_title", return_value=None):
            first = resolver.resolve(bib)
            second = resolver.resolve(BibEntry(key="b7", title=bib.title, year=2020))
        assert arx.call_count == 1
        # Callers get independent copies.
        first.year = 1999
        assert second.year == 2020


class TestNoRefetch:
    def test_supplied_pdf_overrides_cached_unavailable(self, tmp_path):
        bib = BibEntry(key="b0", title="Paper nobody could download", year=2020)
        r1 = ReferenceResolver(cache_dir=tmp_path, s2_min_interval=0.0)
        r1._download_scihub = MagicMock(return_value=None)
        p1, p2, p3 = _unavailable(r1)
        with p1, p2, p3:
            first = r1.resolve(bib)
        r1.close()
        assert first.pdf_path is None

        supplied = tmp_path / "mine.pdf"
        supplied.write_bytes(b"%PDF-1.4")
        r2 = ReferenceResolver(cache_dir=tmp_path, s2_min_interval=0.0, no_refetch=True,
                               supplied_pdfs={first.paper_id: supplied})
        try:
            assert r2.resolve(bib).pdf_path == supplied
        finally:
            r2.close()

    def test_transient_failure_is_not_persisted(self, resolver):
        bib = BibEntry(key="b0", title="Paper resolved while S2 was down", year=2020)

        def flaky(*_a, **_k):
            resolver._mark_transient()
            return None

        with patch.object(resolver, "_arxiv_search_by_title", side_effect=flaky), \
             patch.object(resolver, "_s2_lookup", return_value=None), \
             patch.object(resolver, "_openreview_search_by_title", return_value=None):
            assert resolver.resolve(bib).pdf_path is None
        hit, _ = resolver.meta_cache.get("resolved", resolver.cache_key(bib))
        assert hit is False

    def test_genuine_miss_is_persisted(self, resolver):
        bib = BibEntry(key="b0", title="Paper that really is nowhere", year=2020)
        p1, p2, p3 = _unavailable(resolver)
        with p1, p2, p3:
            resolver.resolve(bib)
        hit, _ = resolver.meta_cache.get("resolved", resolver.cache_key(bib))
        assert hit is True


class TestArxivSearchCaching:
    def test_failure_not_negative_cached(self, resolver):
        with patch.object(resolver._arxiv_client, "results", side_effect=RuntimeError("503")):
            assert resolver._arxiv_search_by_title("Some paper title here") is None
        hit, _ = resolver.meta_cache.get("arxsearch", normalize_title("Some paper title here"))
        assert hit is False

    def test_genuine_miss_negative_cached(self, resolver):
        with patch.object(resolver._arxiv_client, "results", return_value=iter([])):
            assert resolver._arxiv_search_by_title("Some paper title here") is None
        hit, value = resolver.meta_cache.get("arxsearch", normalize_title("Some paper title here"))
        assert hit is True and value is None

    def test_arxiv_client_calls_are_serialized(self, resolver):
        active = {"now": 0, "max": 0}
        lock = threading.Lock()

        def slow_results(_search):
            with lock:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            time.sleep(0.05)
            with lock:
                active["now"] -= 1
            return iter([])

        with patch.object(resolver._arxiv_client, "results", side_effect=slow_results):
            threads = [threading.Thread(target=resolver._arxiv_query, args=(f"ti:x{i}", 5, "t"))
                       for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        assert active["max"] == 1


class TestS2Batch:
    def test_prefetch_serves_later_lookups(self, resolver, monkeypatch):
        import citracer.reference_resolver as rr
        calls = []

        class _Resp:
            status_code = 200
            headers = {}
            def json(self):
                return [{"title": "Found", "externalIds": {"DOI": "10.1/a"}, "citationCount": 3},
                        None]

        class _Session:
            def post(self, url, **kw):
                calls.append(kw["json"]["ids"])
                return _Resp()
            def get(self, *a, **k):
                raise AssertionError("per-paper GET should not happen")

        monkeypatch.setattr(rr, "session", lambda: _Session())
        resolver.prefetch([BibEntry(key="a", doi="10.1/a"), BibEntry(key="b", doi="10.1/b")])
        assert calls == [["DOI:10.1/a", "DOI:10.1/b"]]
        assert resolver._s2_by_id("DOI:10.1/a")["citation_count"] == 3
        assert resolver._s2_by_id("DOI:10.1/b") is None  # genuine miss, cached


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------

class _StreamResp:
    def __init__(self, status, chunks=()):
        self.status_code = status
        self._chunks = list(chunks)
    def iter_content(self, chunk_size=None):
        return iter(self._chunks)
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class TestDownloadPdf:
    def _patch(self, monkeypatch, resp):
        import citracer.http_client as hc
        monkeypatch.setattr(hc, "session", lambda: MagicMock(get=MagicMock(return_value=resp)))

    def test_success_is_atomic(self, tmp_path, monkeypatch):
        self._patch(monkeypatch, _StreamResp(200, [b"%PDF-1.7 ", b"rest"]))
        out = tmp_path / "a.pdf"
        assert download_pdf("http://x", out, timeout=1) == out
        assert out.read_bytes() == b"%PDF-1.7 rest"
        assert not list(tmp_path.glob("*.part"))

    def test_not_a_pdf_leaves_nothing(self, tmp_path, monkeypatch):
        self._patch(monkeypatch, _StreamResp(200, [b"<html>nope</html>"]))
        out = tmp_path / "a.pdf"
        assert download_pdf("http://x", out, timeout=1) is None
        assert list(tmp_path.iterdir()) == []

    def test_oversized_is_rejected(self, tmp_path, monkeypatch):
        self._patch(monkeypatch, _StreamResp(200, [b"%PDF-" + b"x" * 100]))
        out = tmp_path / "a.pdf"
        assert download_pdf("http://x", out, timeout=1, max_bytes=50) is None
        assert list(tmp_path.iterdir()) == []

    def test_5xx_is_transient(self, tmp_path, monkeypatch):
        self._patch(monkeypatch, _StreamResp(503))
        with pytest.raises(TransientError):
            download_pdf("http://x", tmp_path / "a.pdf", timeout=1)


# ---------------------------------------------------------------------------
# GROBID
# ---------------------------------------------------------------------------

FIXTURE_TEI = (Path(__file__).parent / "fixtures" / "sample.tei.xml").read_bytes()


class TestGrobid:
    def test_tei_cache_skips_grobid_on_rerun(self, tmp_path):
        pdf = tmp_path / "p.pdf"
        pdf.write_bytes(b"%PDF-1.4 some bytes")
        with patch.object(pdf_parser, "_call_grobid", return_value=FIXTURE_TEI) as call:
            a = pdf_parser.parse(pdf, cache_dir=tmp_path)
            b = pdf_parser.parse(pdf, cache_dir=tmp_path)
        assert call.call_count == 1
        assert a.title == b.title
        cached = list((tmp_path / "tei").glob("*.tei.xml.gz"))
        assert len(cached) == 1 and gzip.decompress(cached[0].read_bytes()) == FIXTURE_TEI

    def test_503_is_retried_not_fallen_back(self, tmp_path, monkeypatch):
        pdf = tmp_path / "p.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        responses = [MagicMock(status_code=503, text="busy"),
                     MagicMock(status_code=200, content=FIXTURE_TEI)]
        fake = MagicMock(post=MagicMock(side_effect=responses))
        monkeypatch.setattr(pdf_parser, "session", lambda: fake)
        monkeypatch.setattr(pdf_parser, "GROBID_503_BACKOFF_DELAYS", (0.0,))
        monkeypatch.setattr(pdf_parser.time, "sleep", lambda _s: None)
        assert pdf_parser._call_grobid(pdf, "http://g", False) == FIXTURE_TEI
        assert fake.post.call_count == 2


# ---------------------------------------------------------------------------
# Output safety
# ---------------------------------------------------------------------------

class TestOutputs:
    def test_script_json_cannot_close_script_tag(self):
        import json
        value = {"abstract": "evil </script><script>alert(1)</script> & co"}
        out = _script_json(value)
        assert "</" not in out and "<" not in out
        assert json.loads(out) == value

    def test_js_flags_follow_case_sensitivity(self):
        specs = {s["keyword"]: s for s in _keyword_patterns_for_js(["GAN", "attention"])}
        assert specs["GAN"]["flags"] == "g"
        assert specs["attention"]["flags"] == "gi"

    def test_manifest_command_redacts_secrets(self):
        argv = ["citracer", "--s2-api-key", "SECRET", "--email=me@x.org", "--keyword", "k"]
        out = redact_argv(argv)
        assert "SECRET" not in out and "--email=me@x.org" not in out
        assert out[-2:] == ["--keyword", "k"]

    def test_timeline_ignores_unavailable_papers(self):
        g = TracerGraph()
        g.add_node(PaperNode(paper_id="a", title="A", year=2020, status="analyzed"))
        g.add_node(PaperNode(paper_id="b", title="B", year=2020, status="unavailable"))
        (entry,) = analytics._timeline(g)
        assert entry["total"] == 1 and entry["keyword_density"] == 1.0


# ---------------------------------------------------------------------------
# Second review pass
# ---------------------------------------------------------------------------

class TestSecondPass:
    def test_doi_known_to_s2_skips_arxiv_search_without_key(self, resolver):
        bib = BibEntry(key="b0", title="A journal-only paper", doi="10.1/j", year=2020)
        s2 = {"title": "A journal-only paper", "doi": "10.1/j", "arxiv_id": None}
        with patch.object(resolver, "_s2_lookup", return_value=s2), \
             patch.object(resolver, "_arxiv_search_by_title") as arx, \
             patch.object(resolver, "_openreview_search_by_title") as orev:
            resolver.resolve(bib)
        arx.assert_not_called()
        orev.assert_not_called()

    def test_batch_enrich_replaces_grobid_title_and_authors(self, resolver):
        from citracer.reference_resolver import ResolvedRef
        ref = ResolvedRef(paper_id="arxiv:1", title="Atimeseriesis worth", arxiv_id="1",
                          authors=["Nie"])
        meta = {"title": "A Time Series is Worth 64 Words", "authors": ["Yuqi Nie", "Nam Nguyen"],
                "citation_count": 7}
        with patch.object(resolver, "_s2_batch", return_value={"ARXIV:1": meta}):
            resolver.batch_enrich([ref])
        assert ref.title == meta["title"]
        assert ref.authors == meta["authors"]
        assert ref.citation_count == 7

    def test_typographic_hyphens_and_hyphenation_normalized(self):
        from citracer.pdf_parser import normalize_text
        assert normalize_text("channel‐independent") == "channel-independent"
        assert normalize_text("in­depen­dent") == "independent"
        assert normalize_text("indepen-\n  dent") == "independent"
        assert normalize_text("2019-\n2020") == "2019-\n2020"  # not a word break

    def test_ref_offsets_valid_after_normalization(self):
        from lxml import etree
        from citracer.pdf_parser import _walk_body
        body = etree.fromstring(
            '<body xmlns="http://www.tei-c.org/ns/1.0"><p>A chan­nel‐inde'
            'pendent model <ref type="bibr" target="#b0">[1]</ref> works.</p></body>'
        )
        text, refs = _walk_body(body)
        assert "channel-independent" in text
        assert text[refs[0].start:refs[0].end] == "[1]"

    def test_keyword_pattern_details(self):
        from citracer.keyword_matcher import build_pattern
        assert build_pattern("analysis").search("two analyses") is not None
        assert build_pattern("analyses").search("one analysis") is not None
        assert build_pattern("k-means").search("k-meaning") is None
        assert build_pattern("k-means").search("k means clustering") is not None
        # S2 citation contexts (reverse mode) keep typographic hyphens.
        assert build_pattern("channel-independent").search("channel‐independent") is not None


def test_semantic_import_disables_tensorflow_backend(monkeypatch):
    # transformers must not import TensorFlow (crashes with Keras 3).
    from citracer import keyword_matcher as km
    monkeypatch.delenv("USE_TF", raising=False)
    monkeypatch.setattr(km, "_semantic_model", None)
    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", None)
    with pytest.raises(ImportError):
        km._get_semantic_model()
    assert __import__("os").environ["USE_TF"] == "0"
