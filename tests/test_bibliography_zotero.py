"""Tests for the bibliography exports (BibTeX / RIS / CSV) and the Zotero push."""
import csv
import io

import pytest

from citracer import bibliography as bib
from citracer import zotero
from citracer.exporter import export_graph
from citracer.models import PaperNode, TracerGraph


@pytest.fixture
def graph() -> TracerGraph:
    g = TracerGraph()
    g.add_node(PaperNode(paper_id="doi:10.1/root", title="Root paper", authors=["Marc Pinet"],
                         year=2024, doi="10.1/root", status="root", depth=0))
    g.add_node(PaperNode(
        paper_id="arxiv:2211.14730",
        title="A Time Series is Worth 64 Words: PatchTST & 50% of_it",
        authors=["Yuqi Nie", "Laurens van der Maaten", "Élodie Durand"],
        year=2022, publication_date="2022-11-27", arxiv_id="2211.14730",
        status="analyzed", depth=1, keyword_hits=['We use "channel-independent" models, cheaply.'],
        url="https://arxiv.org/abs/2211.14730", abstract="An abstract.",
    ))
    g.add_node(PaperNode(paper_id="title:x", title="A lost book", authors=["Yuqi Nie"],
                         year=2022, status="unavailable", depth=1))
    return g


class TestBibliography:
    def test_split_name(self):
        assert bib.split_name("Laurens van der Maaten") == ("Laurens", "van der Maaten")
        assert bib.split_name("Nie, Yuqi") == ("Yuqi", "Nie")
        assert bib.split_name("Plato") == ("", "Plato")

    def test_citation_keys_unique_and_ascii(self):
        nodes = [PaperNode(paper_id=str(i), title="Deep models", authors=["Élodie Durand"], year=2020)
                 for i in range(3)]
        keys = bib.citation_keys(nodes)
        assert list(keys.values()) == ["durand2020deep", "durand2020deepa", "durand2020deepb"]

    def test_bibtex_entry(self, graph):
        text = bib.to_bibtex(bib.select_papers(graph), ["channel-independent"])
        assert text.startswith("@article{pinet2024root,")  # root first, DOI -> article
        assert "@misc{nie2022time," in text                 # arXiv-only -> misc
        assert "eprint = {2211.14730}" in text and "archiveprefix = {arXiv}" in text
        assert r"{PatchTST} \& 50\% of\_it" in text          # escaped, caps protected
        assert "author = {Nie, Yuqi and van der Maaten, Laurens and Durand, Élodie}" in text
        assert "keywords = {citracer, analyzed, channel-independent}" in text
        assert "annote = {We use" in text

    def test_status_filter(self, graph):
        papers = bib.select_papers(graph, {"analyzed", "root"})
        assert [p.paper_id for p in papers] == ["doi:10.1/root", "arxiv:2211.14730"]

    def test_ris_record(self, graph):
        ris = bib.ris_record(graph.nodes["arxiv:2211.14730"], ["k"])
        lines = ris.splitlines()
        assert lines[0] == "TY  - GEN" and lines[-1] == "ER  -"
        assert "AU  - van der Maaten, Laurens" in lines
        assert "DA  - 2022/11/27" in lines
        assert any(line.startswith("N1  - We use") for line in lines)

    def test_csv_roundtrip(self, graph):
        text = bib.to_csv(bib.select_papers(graph),
                          {"node_metrics": {"arxiv:2211.14730": {"pagerank": 0.5}}})
        rows = list(csv.DictReader(io.StringIO(text)))
        assert len(rows) == 3
        row = next(r for r in rows if r["id"] == "arxiv:2211.14730")
        assert row["passages"] == 'We use "channel-independent" models, cheaply.'
        assert row["pagerank"] == "0.5"

    @pytest.mark.parametrize("ext", ["bib", "ris", "csv"])
    def test_export_graph_formats(self, graph, tmp_path, ext):
        out = export_graph(graph, tmp_path / f"papers.{ext}",
                           manifest={"parameters": {"keywords": ["kw"]}},
                           statuses={"analyzed"})
        raw = out.read_bytes()
        if ext == "csv":
            assert raw.startswith(b"\xef\xbb\xbf")  # BOM for Excel
        assert b"2211.14730" in raw and b"Root paper" not in raw


# ---------------------------------------------------------------------------
# Zotero (HTTP mocked)
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code = status
        self._data = data if data is not None else {}
        self.headers = headers or {}
        self.text = str(self._data)

    def json(self):
        return self._data


class _FakeZotero:
    """Minimal in-memory Zotero Web API."""

    def __init__(self):
        self.collections = []
        self.items = []
        self.posts = []

    def request(self, method, url, headers=None, params=None, json=None, **_kw):
        assert headers["Zotero-API-Key"] == "KEY"
        path = url.replace(zotero.ZOTERO_API, "")
        if path == "/keys/KEY":
            return _Resp(data={"userID": 42})
        if method == "GET" and path == "/users/42/collections":
            return _Resp(data=self.collections, headers={"Total-Results": str(len(self.collections))})
        if method == "POST" and path == "/users/42/collections":
            key = f"C{len(self.collections)}"
            self.collections.append({"key": key, "data": {"name": json[0]["name"]}})
            return _Resp(data={"successful": {"0": {"key": key}}})
        if method == "GET" and path.endswith("/items/top"):
            return _Resp(data=self.items, headers={"Total-Results": str(len(self.items))})
        if method == "POST" and path == "/users/42/items":
            self.posts.append(json)
            ok = {}
            for i, obj in enumerate(json):
                key = f"I{len(self.posts)}_{i}"
                ok[str(i)] = {"key": key}
                if obj["itemType"] != "note":
                    self.items.append({"key": key, "data": obj})
            return _Resp(data={"successful": ok, "failed": {}})
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def fake_zotero(monkeypatch):
    fake = _FakeZotero()
    monkeypatch.setattr(zotero, "session", lambda: fake)
    return fake


class TestZotero:
    def test_push_creates_collection_items_and_notes(self, graph, fake_zotero):
        client = zotero.ZoteroClient("KEY")
        result = zotero.push_papers(client, bib.select_papers(graph), "citracer: k", ["k"])
        assert result.created == 3 and result.skipped == 0 and result.notes == 1
        items = fake_zotero.posts[0]
        preprint = next(i for i in items if i["title"].startswith("A Time Series"))
        assert preprint["itemType"] == "preprint"
        assert preprint["archiveID"] == "arXiv:2211.14730"
        assert preprint["collections"] == ["C0"]
        assert {"creatorType": "author", "firstName": "Laurens",
                "lastName": "van der Maaten"} in preprint["creators"]
        assert {"tag": "citracer:analyzed"} in preprint["tags"]
        note = fake_zotero.posts[1][0]
        assert note["itemType"] == "note" and "channel-independent" in note["note"]

    def test_rerun_skips_papers_already_in_collection(self, graph, fake_zotero):
        zotero.push_papers(zotero.ZoteroClient("KEY"), bib.select_papers(graph), "c", [])
        again = zotero.push_papers(zotero.ZoteroClient("KEY"), bib.select_papers(graph), "c", [])
        assert again.created == 0 and again.skipped == 3
        assert len(fake_zotero.collections) == 1

    def test_rejected_key_raises_clear_error(self, monkeypatch):
        class _Denied:
            def request(self, *a, **k):
                return _Resp(status=403)
        monkeypatch.setattr(zotero, "session", lambda: _Denied())
        with pytest.raises(zotero.ZoteroError, match="write access"):
            zotero.ZoteroClient("KEY", library="users/1").find_or_create_collection("c")

    def test_rate_limit_is_retried(self, monkeypatch):
        calls = []

        class _Busy:
            def request(self, *a, **k):
                calls.append(1)
                if len(calls) == 1:
                    return _Resp(status=429, headers={"Retry-After": "0"})
                return _Resp(data=[], headers={"Total-Results": "0"})
        monkeypatch.setattr(zotero, "session", lambda: _Busy())
        monkeypatch.setattr(zotero.time, "sleep", lambda _s: None)
        assert zotero.ZoteroClient("KEY", library="users/1")._paged("/collections", {}) == []
        assert len(calls) == 2
