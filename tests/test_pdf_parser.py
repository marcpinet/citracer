"""Tests for citracer.pdf_parser — TEI walker, figure-noise filter,
paragraph merge, narrative ref supplementation.

All tests operate on a pre-baked TEI XML fixture (tests/fixtures/sample.tei.xml)
so they never need to contact GROBID.
"""
from pathlib import Path

import pytest

from citracer.models import BibEntry, InlineRef, ParsedPaper
from citracer.pdf_parser import (
    _first_surname,
    _looks_like_figure_noise,
    _parse_tei,
    _supplement_inline_refs,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def parsed() -> ParsedPaper:
    tei_bytes = (FIXTURES / "sample.tei.xml").read_bytes()
    return _parse_tei(tei_bytes)


# ---------------------------------------------------------------------------
# Header extraction
# ---------------------------------------------------------------------------

class TestHeader:
    def test_title(self, parsed):
        assert parsed.title == "A Survey of Channel-Independence for Time Series Forecasting"

    def test_year(self, parsed):
        assert parsed.year == 2024

    def test_doi(self, parsed):
        assert parsed.doi == "10.48550/arxiv.9999.99999"

    def test_arxiv_id(self, parsed):
        assert parsed.arxiv_id == "9999.99999"


# ---------------------------------------------------------------------------
# Bibliography
# ---------------------------------------------------------------------------

class TestBibliography:
    def test_five_entries(self, parsed):
        # b0..b4 in the fixture
        assert set(parsed.bibliography.keys()) == {"b0", "b1", "b2", "b3", "b4"}

    def test_entry_title(self, parsed):
        assert parsed.bibliography["b0"].title == "Channel-independent baselines for time series"

    def test_entry_year(self, parsed):
        assert parsed.bibliography["b2"].year == 2023
        assert parsed.bibliography["b3"].year == 2022

    def test_entry_arxiv_id(self, parsed):
        assert parsed.bibliography["b3"].arxiv_id == "2211.14730"
        assert parsed.bibliography["b0"].arxiv_id is None

    def test_entry_authors(self, parsed):
        authors = parsed.bibliography["b0"].authors
        assert any("Smith" in a for a in authors)
        assert any("Kim" in a for a in authors)

    def test_raw_reference_captured(self, parsed):
        assert "Smith" in parsed.bibliography["b0"].raw


# ---------------------------------------------------------------------------
# Body text + inline refs
# ---------------------------------------------------------------------------

class TestBody:
    def test_introduction_is_present(self, parsed):
        assert "Early work on channel-independent forecasting" in parsed.text

    def test_figure_noise_is_skipped(self, parsed):
        # The <figure> contains a math-heavy <p> ("𝑥 ∈ ℝ ..." etc).
        # The heuristic should drop it while keeping the surrounding prose.
        assert "𝑥 ∈ ℝ" not in parsed.text
        assert "⊗" not in parsed.text

    def test_fragmented_paragraph_is_merged(self, parsed):
        # GROBID-style fragmentation: "Since<ref/></p><p>and<ref/></p><p>have
        # verified..." must be glued back into a single sentence so the keyword
        # matcher can associate refs and keyword.
        assert "have verified that channel-independence works well" in parsed.text
        # The text between "Since" and "have verified" should be on one line,
        # i.e. no newline separator inside that fragment.
        start = parsed.text.find("Task Definition")
        end   = parsed.text.find("have verified")
        assert start != -1 and end != -1
        # No intermediate newline within the merged span
        assert "\n" not in parsed.text[start:end]

    def test_inline_refs_b0_b1_extracted(self, parsed):
        keys = {r.bib_key for r in parsed.inline_refs}
        assert "b0" in keys
        assert "b1" in keys

    def test_inline_refs_b2_b3_extracted(self, parsed):
        keys = {r.bib_key for r in parsed.inline_refs}
        assert "b2" in keys
        assert "b3" in keys

    def test_inline_refs_have_valid_offsets(self, parsed):
        for ref in parsed.inline_refs:
            assert 0 <= ref.start < ref.end <= len(parsed.text)


# ---------------------------------------------------------------------------
# Narrative citation supplementation
# ---------------------------------------------------------------------------

class TestNarrativeSupplementation:
    def test_brown_2019_recovered(self, parsed):
        """The 'Brown et al. (2019)' citation is only in the prose, not as a
        TEI <ref> element. The supplementation pass must recover it."""
        # Find the offset of "Brown" in the parsed text
        idx = parsed.text.find("Brown")
        assert idx != -1
        # At least one inline ref should point to b4 near that offset
        brown_refs = [
            r for r in parsed.inline_refs
            if r.bib_key == "b4" and abs(r.start - idx) < 60
        ]
        assert len(brown_refs) >= 1


class TestSupplementInlineRefsUnit:
    """Direct unit tests for the supplementation algorithm."""

    def test_narrative_et_al(self):
        text = "Earlier, Smith et al. (2020) showed results."
        bib = {"b0": BibEntry(key="b0", title="T", authors=["John Smith"], year=2020)}
        refs = _supplement_inline_refs(text, bib, existing=[])
        assert len(refs) == 1
        assert refs[0].bib_key == "b0"

    def test_two_authors(self):
        text = "Jones and Lee (2021) extended the approach."
        bib = {"b1": BibEntry(key="b1", title="T", authors=["Pat Jones", "Sam Lee"], year=2021)}
        refs = _supplement_inline_refs(text, bib, existing=[])
        assert len(refs) == 1
        assert refs[0].bib_key == "b1"

    def test_single_author(self):
        text = "Kim (2019) first proposed this."
        bib = {"b2": BibEntry(key="b2", title="T", authors=["Alice Kim"], year=2019)}
        refs = _supplement_inline_refs(text, bib, existing=[])
        assert len(refs) == 1

    def test_ambiguous_surname_year_skipped(self):
        # Two different 2022 Zhou papers -> surname/year signature is ambiguous
        text = "Zhou et al. (2022) reported baseline accuracy."
        bib = {
            "b1": BibEntry(key="b1", title="A", authors=["Tian Zhou"], year=2022),
            "b2": BibEntry(key="b2", title="B", authors=["Haoyi Zhou"], year=2022),
        }
        refs = _supplement_inline_refs(text, bib, existing=[])
        assert refs == []  # ambiguous -> skipped

    def test_no_overlap_with_existing(self):
        text = "Brown et al. (2019) showed results."
        bib = {"b4": BibEntry(key="b4", title="T", authors=["Aaron Brown"], year=2019)}
        # Pretend GROBID already covered this area
        existing = [InlineRef(bib_key="b4", start=0, end=len(text))]
        refs = _supplement_inline_refs(text, bib, existing=existing)
        assert refs == []

    def test_year_mismatch_is_not_recovered(self):
        # Text says 2020, bib says 2019 -> no match, don't recover
        text = "Smith et al. (2020) proposed X."
        bib = {"b0": BibEntry(key="b0", title="T", authors=["John Smith"], year=2019)}
        refs = _supplement_inline_refs(text, bib, existing=[])
        assert refs == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestFirstSurname:
    def test_basic(self):
        assert _first_surname("John Smith") == "Smith"

    def test_with_initial(self):
        assert _first_surname("J. Smith") == "Smith"

    def test_jr_suffix_stripped(self):
        assert _first_surname("John Smith Jr.") == "Smith"
        assert _first_surname("John Smith III") == "Smith"

    def test_comma_form(self):
        assert _first_surname("Smith, John") == "John"  # splits on comma/space

    def test_empty(self):
        assert _first_surname("") == ""

    def test_trailing_punctuation(self):
        assert _first_surname("Smith,") == "Smith"


class TestFigureNoiseDetector:
    def test_empty(self):
        assert _looks_like_figure_noise("") is False

    def test_plain_prose(self):
        assert _looks_like_figure_noise(
            "A perfectly normal paragraph with no math symbols at all."
        ) is False

    def test_single_math_char_passes(self):
        # 1 math char < threshold (3) -> still considered prose
        assert _looks_like_figure_noise("Let x ∈ R be a real number.") is False

    def test_heavy_math_is_noise(self):
        assert _looks_like_figure_noise(
            "𝑥 ∈ ℝ 𝑛×𝑑 𝑀 ⊗ 𝑥 encoder ℝ output 𝑦 ∈ ℝ"
        ) is True
