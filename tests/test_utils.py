"""Tests for citracer.utils — ID normalization + paper_id builder."""
import pytest

from citracer.utils import (
    make_paper_id,
    normalize_arxiv_id,
    normalize_doi,
    normalize_title,
    title_hash,
)


class TestNormalizeDoi:
    def test_none(self):
        assert normalize_doi(None) is None

    def test_empty(self):
        assert normalize_doi("") is None
        assert normalize_doi("   ") is None

    def test_simple(self):
        assert normalize_doi("10.1000/abc") == "10.1000/abc"

    def test_uppercase(self):
        assert normalize_doi("10.1000/AbC") == "10.1000/abc"

    def test_http_prefix(self):
        assert normalize_doi("https://doi.org/10.1000/abc") == "10.1000/abc"
        assert normalize_doi("http://doi.org/10.1000/abc") == "10.1000/abc"
        assert normalize_doi("https://dx.doi.org/10.1000/abc") == "10.1000/abc"

    def test_whitespace(self):
        assert normalize_doi("  10.1000/abc  ") == "10.1000/abc"


class TestNormalizeArxivId:
    def test_none(self):
        assert normalize_arxiv_id(None) is None

    def test_empty(self):
        assert normalize_arxiv_id("") is None

    def test_modern_id(self):
        assert normalize_arxiv_id("2211.14730") == "2211.14730"

    def test_with_version(self):
        assert normalize_arxiv_id("2211.14730v2") == "2211.14730"
        assert normalize_arxiv_id("2211.14730v10") == "2211.14730"

    def test_with_prefix(self):
        assert normalize_arxiv_id("arXiv:2211.14730") == "2211.14730"
        assert normalize_arxiv_id("ARXIV:2211.14730") == "2211.14730"

    def test_old_style(self):
        assert normalize_arxiv_id("cs.LG/0501001") == "cs.lg/0501001"


class TestNormalizeTitle:
    def test_none(self):
        assert normalize_title(None) == ""

    def test_empty(self):
        assert normalize_title("") == ""

    def test_lowercase(self):
        assert normalize_title("Hello World") == "hello world"

    def test_strips_punctuation(self):
        assert normalize_title("Foo: Bar, Baz!") == "foo bar baz"

    def test_collapses_whitespace(self):
        assert normalize_title("foo    bar\n\tbaz") == "foo bar baz"

    def test_alphanumeric_preserved(self):
        assert normalize_title("GPT-4 is a 2023 paper") == "gpt4 is a 2023 paper"


class TestMakePaperId:
    def test_prefers_doi(self):
        pid = make_paper_id(doi="10.1/abc", arxiv_id="2211.14730", title="Foo")
        assert pid == "doi:10.1/abc"

    def test_falls_back_to_arxiv(self):
        pid = make_paper_id(arxiv_id="2211.14730", title="Foo")
        assert pid == "arxiv:2211.14730"

    def test_falls_back_to_title_hash(self):
        pid = make_paper_id(title="A Sample Paper Title")
        assert pid.startswith("title:")
        # deterministic: same title -> same hash
        assert make_paper_id(title="A Sample Paper Title") == pid

    def test_title_hash_case_insensitive(self):
        a = make_paper_id(title="Foo Bar")
        b = make_paper_id(title="FOO BAR")
        assert a == b

    def test_title_hash_punctuation_insensitive(self):
        a = make_paper_id(title="Foo: Bar!")
        b = make_paper_id(title="Foo Bar")
        assert a == b

    def test_all_none(self):
        pid = make_paper_id()
        assert pid.startswith("unknown:")

    def test_doi_normalization(self):
        a = make_paper_id(doi="https://doi.org/10.1/ABC")
        b = make_paper_id(doi="10.1/abc")
        assert a == b


class TestTitleHash:
    def test_deterministic(self):
        assert title_hash("Hello") == title_hash("Hello")

    def test_length(self):
        # We use sha256[:16]
        assert len(title_hash("Anything")) == 16

    def test_case_insensitive(self):
        assert title_hash("Foo") == title_hash("FOO")
