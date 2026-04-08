"""PDF parsing via GROBID with pymupdf fallback.

GROBID returns TEI XML. We walk the <body> tree to reconstruct plain text
while recording the character offset of every inline <ref type="bibr">,
so the keyword matcher can later associate hits with citations by position.
"""
from __future__ import annotations
import logging
import re
from pathlib import Path

import requests
from lxml import etree

from .constants import FIGURE_NOISE_MATH_CHAR_THRESHOLD, GROBID_TIMEOUT_SECONDS
from .models import BibEntry, InlineRef, ParsedPaper
from .utils import normalize_arxiv_id, normalize_doi

logger = logging.getLogger(__name__)

TEI_NS = "http://www.tei-c.org/ns/1.0"
NS = {"tei": TEI_NS}


class GrobidError(RuntimeError):
    pass


def parse(
    pdf_path: str | Path,
    grobid_url: str = "http://localhost:8070",
    consolidate_citations: bool = False,
) -> ParsedPaper:
    """Parse a PDF, trying GROBID first then falling back to pymupdf.

    Args:
        pdf_path: Path to the PDF file.
        grobid_url: URL of the GROBID service.
        consolidate_citations: If True, ask GROBID to consolidate each
            bibliographic reference against CrossRef (much more accurate
            titles/DOIs, but ~2-5s extra per PDF).
    """
    pdf_path = Path(pdf_path)
    try:
        tei = _call_grobid(pdf_path, grobid_url, consolidate_citations)
        return _parse_tei(tei)
    except Exception as e:
        logger.warning("GROBID failed for %s (%s); falling back to pymupdf", pdf_path.name, e)
        return _parse_fallback(pdf_path)


# ---------- GROBID ----------

def _call_grobid(pdf_path: Path, grobid_url: str, consolidate_citations: bool) -> bytes:
    url = f"{grobid_url.rstrip('/')}/api/processFulltextDocument"
    with open(pdf_path, "rb") as f:
        files = {"input": (pdf_path.name, f, "application/pdf")}
        data = {
            "consolidateHeader": "1",
            "consolidateCitations": "1" if consolidate_citations else "0",
            "includeRawCitations": "1",
            "segmentSentences": "0",
        }
        resp = requests.post(url, files=files, data=data, timeout=GROBID_TIMEOUT_SECONDS)
    if resp.status_code != 200:
        raise GrobidError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.content


def _parse_tei(tei_bytes: bytes) -> ParsedPaper:
    root = etree.fromstring(tei_bytes)

    title, authors, doi, arxiv_id, year = _extract_header(root)
    bibliography = _extract_bibliography(root)

    body = root.find(".//tei:text/tei:body", NS)
    if body is None:
        text, inline_refs = "", []
    else:
        text, inline_refs = _walk_body(body)

    # GROBID occasionally misses narrative citations like "DLinear Zeng et al.
    # (2023)" — particularly when the author name is preceded by something
    # other than a parenthesis. Recover those by scanning the body text for
    # (Author, Year) patterns matching unambiguous bibliography entries.
    extra = _supplement_inline_refs(text, bibliography, inline_refs)
    if extra:
        inline_refs = sorted(inline_refs + extra, key=lambda r: r.start)
        logger.debug("Recovered %d inline ref(s) missed by GROBID", len(extra))

    return ParsedPaper(
        text=text,
        bibliography=bibliography,
        inline_refs=inline_refs,
        title=title,
        authors=authors,
        doi=doi,
        arxiv_id=arxiv_id,
        year=year,
    )


def _first_surname(author: str) -> str:
    """Return the surname (last token) of an author string. Handles trailing
    punctuation and 'Jr.' / 'III' suffixes."""
    if not author:
        return ""
    tokens = author.replace(",", " ").split()
    # strip generational suffixes
    while tokens and tokens[-1].rstrip(".").lower() in {"jr", "sr", "ii", "iii", "iv"}:
        tokens.pop()
    return tokens[-1].rstrip(".,;:") if tokens else ""


def _supplement_inline_refs(
    text: str,
    bibliography: dict[str, BibEntry],
    existing: list[InlineRef],
) -> list[InlineRef]:
    """Find narrative-style citations GROBID missed.

    For each bibliography entry with a unique (surname, year) signature,
    search the text for canonical patterns:
        - "Surname et al. (Year)"
        - "Surname et al., Year"
        - "Surname & Other (Year)" / "Surname and Other (Year)"
        - "Surname (Year)"
    and add an InlineRef for each new occurrence that doesn't overlap with
    a ref already extracted by GROBID. Skip ambiguous (surname, year) pairs.
    """
    # Group bib entries by (surname, year) to detect ambiguity
    sig_to_keys: dict[tuple[str, int], list[str]] = {}
    for k, b in bibliography.items():
        if not b.authors or not b.year:
            continue
        surname = _first_surname(b.authors[0])
        if not surname or len(surname) < 2:
            continue
        sig = (surname.lower(), b.year)
        sig_to_keys.setdefault(sig, []).append(k)

    existing_ranges = [(r.start, r.end) for r in existing]

    def overlaps(a: int, b: int) -> bool:
        return any(a < e and s < b for s, e in existing_ranges)

    out: list[InlineRef] = []
    for (surname_lc, year), keys in sig_to_keys.items():
        if len(keys) != 1:
            continue  # ambiguous; skip rather than add the wrong ref
        bib_key = keys[0]
        s_re = re.escape(surname_lc)
        y_re = str(year)
        patterns = [
            rf"\b{s_re}\s+et\s+al\.?\s*[,\s]\s*\(?\s*{y_re}\s*\)?",
            rf"\b{s_re}\s+(?:&|and)\s+\w+\s*[,\s]?\s*\(?\s*{y_re}\s*\)?",
            rf"\b{s_re}\s*\(\s*{y_re}\s*\)",
        ]
        for pat in patterns:
            for m in re.finditer(pat, text, re.IGNORECASE):
                start, end = m.start(), m.end()
                if overlaps(start, end):
                    continue
                out.append(InlineRef(bib_key=bib_key, start=start, end=end))
                existing_ranges.append((start, end))
    return out


def _extract_header(root) -> tuple[str | None, list[str], str | None, str | None, int | None]:
    header = root.find(".//tei:teiHeader", NS)
    if header is None:
        return None, [], None, None, None

    title_el = header.find(".//tei:titleStmt/tei:title", NS)
    title = _text(title_el)

    authors: list[str] = []
    for pers in header.findall(".//tei:sourceDesc//tei:author/tei:persName", NS):
        forenames = " ".join(_text(f) or "" for f in pers.findall("tei:forename", NS))
        surname = _text(pers.find("tei:surname", NS)) or ""
        full = (forenames + " " + surname).strip()
        if full:
            authors.append(full)

    doi = None
    for idno in header.findall(".//tei:idno", NS):
        idtype = (idno.get("type") or "").lower()
        if idtype == "doi":
            doi = normalize_doi(_text(idno))
            break

    arxiv_id = None
    for idno in header.findall(".//tei:idno", NS):
        if (idno.get("type") or "").lower() == "arxiv":
            arxiv_id = normalize_arxiv_id(_text(idno))
            break

    year = None
    date_el = header.find(".//tei:publicationStmt/tei:date", NS)
    if date_el is not None:
        when = date_el.get("when") or _text(date_el) or ""
        m = re.search(r"\b(19|20)\d{2}\b", when)
        if m:
            year = int(m.group(0))

    return title, authors, doi, arxiv_id, year


def _extract_bibliography(root) -> dict[str, BibEntry]:
    out: dict[str, BibEntry] = {}
    for bib in root.findall(".//tei:listBibl/tei:biblStruct", NS):
        key = bib.get("{http://www.w3.org/XML/1998/namespace}id") or bib.get("id") or ""
        if not key:
            continue

        title_el = bib.find(".//tei:title[@type='main']", NS)
        if title_el is None:
            title_el = bib.find(".//tei:title", NS)
        title = _text(title_el)

        authors: list[str] = []
        for pers in bib.findall(".//tei:author/tei:persName", NS):
            forenames = " ".join(_text(f) or "" for f in pers.findall("tei:forename", NS))
            surname = _text(pers.find("tei:surname", NS)) or ""
            full = (forenames + " " + surname).strip()
            if full:
                authors.append(full)

        year = None
        date_el = bib.find(".//tei:date", NS)
        if date_el is not None:
            when = date_el.get("when") or _text(date_el) or ""
            m = re.search(r"\b(19|20)\d{2}\b", when)
            if m:
                year = int(m.group(0))

        doi = None
        arxiv_id = None
        for idno in bib.findall(".//tei:idno", NS):
            t = (idno.get("type") or "").lower()
            v = _text(idno)
            if t == "doi" and v:
                doi = normalize_doi(v)
            elif t == "arxiv" and v:
                arxiv_id = normalize_arxiv_id(v)

        raw_el = bib.find(".//tei:note[@type='raw_reference']", NS)
        raw = _text(raw_el) or ""

        out[key] = BibEntry(
            key=key,
            title=title,
            authors=authors,
            year=year,
            doi=doi,
            arxiv_id=arxiv_id,
            raw=raw,
        )
    return out


# TEI elements that are always pure noise and never contain prose we want:
# rendered math equations. Figures and tables USED to live here too, but we
# discovered (see 2302.11939 "One Fits All" section D.2) that GROBID sometimes
# misclassifies legitimate prose paragraphs as table notes — causing us to
# silently drop real text. Instead, we walk into figures/tables and trust the
# per-paragraph math-noise heuristic below to filter out actual diagram junk.
_SKIP_TAGS = {"formula"}

# GROBID sometimes promotes figure-diagram text into regular <p> elements when
# it can't classify them. Detect such paragraphs by looking for mathematical
# Unicode symbols that almost never appear in prose: math italic/bold letters
# (U+1D400-U+1D7FF) and common equation symbols (ℝ ℕ ℤ ∈ ∉ ⊂ ⊆ ∀ ∃ ⊕ ⊗ etc.).
_MATH_CHAR_RE = re.compile(
    r"[\U0001D400-\U0001D7FF"     # math alphanumeric symbols (𝑥, 𝑀, ...)
    r"\u2102\u210D\u2115\u2119\u211A\u211D\u2124"  # ℂ ℍ ℕ ℙ ℚ ℝ ℤ
    r"\u2208\u2209\u220B\u2200\u2203\u2205"        # ∈ ∉ ∋ ∀ ∃ ∅
    r"\u2282\u2283\u2286\u2287\u2295\u2297"        # ⊂ ⊃ ⊆ ⊇ ⊕ ⊗
    r"]"
)
def _looks_like_figure_noise(text: str) -> bool:
    if not text:
        return False
    return len(_MATH_CHAR_RE.findall(text)) >= FIGURE_NOISE_MATH_CHAR_THRESHOLD


def _walk_body(body) -> tuple[str, list[InlineRef]]:
    """Recursively walk the TEI body, accumulating plain text and recording
    the character offsets of inline bibliographic refs.

    Skips <figure>, <table> and <formula> subtrees entirely (and emits a
    space + the element's tail so surrounding text remains well-separated).
    """
    parts: list[str] = []
    refs: list[InlineRef] = []
    pos = [0]  # mutable counter

    def emit(s: str) -> None:
        if not s:
            return
        parts.append(s)
        pos[0] += len(s)

    def walk(el) -> None:
        tag = etree.QName(el).localname

        if tag in _SKIP_TAGS:
            # Drop entire subtree but keep a separator + tail so neighbouring
            # paragraphs don't get glued together.
            emit(" ")
            if el.tail:
                emit(el.tail)
            return

        # Heuristic: if a <p> looks like math/figure noise, skip it whole.
        if tag == "p" and _looks_like_figure_noise("".join(el.itertext())):
            emit(" ")
            if el.tail:
                emit(el.tail)
            return

        # Inline bib reference: record (start, end) and emit its text
        if tag == "ref" and (el.get("type") == "bibr"):
            target = (el.get("target") or "").lstrip("#")
            start = pos[0]
            txt = "".join(el.itertext())
            emit(txt)
            end = pos[0]
            if target:
                refs.append(InlineRef(bib_key=target, start=start, end=end))
            if el.tail:
                emit(el.tail)
            return

        block = tag in {"p", "div", "head", "item", "list"}

        if el.text:
            emit(el.text)

        for child in el:
            walk(child)

        if block:
            emit("\n")

        if el.tail:
            emit(el.tail)

    walk(body)
    text = "".join(parts)

    # GROBID sometimes splits a single paragraph into several <p> elements
    # around inline citations (seen in "One Fits All" 2302.11939 section D.2:
    # "Task Definition Since[Zeng et al. 2023] and[Nie et al. 2022] have
    # verified that channel-independence works well..." emitted as 3 <p>s).
    # When a newline (surrounded by optional indentation whitespace) sits
    # between a non-terminating character and a lowercase continuation,
    # it's almost certainly one of these artefacts — smooth it out. The
    # replacement keeps the exact character count so InlineRef offsets
    # stay valid. Note that ')' is NOT in the exclusion set: citations
    # end with ')' and we DO want to merge across them.
    text = re.sub(
        r"(?<=[^.!?\n\"])\s*\n\s*(?=[a-z(])",
        lambda m: " " * len(m.group(0)),
        text,
    )
    # Deliberately do NOT collapse runs of whitespace here — that would
    # invalidate the character offsets recorded in `refs`. Display code
    # already collapses whitespace per-snippet via keyword_matcher.search().
    return text, refs


def _text(el) -> str | None:
    if el is None:
        return None
    s = "".join(el.itertext()).strip()
    return s or None


# ---------- pymupdf fallback ----------

def _parse_fallback(pdf_path: Path) -> ParsedPaper:
    import fitz  # pymupdf

    doc = fitz.open(pdf_path)
    full_text = "\n".join(page.get_text() for page in doc)
    doc.close()

    # Try to split body / references
    body_text, refs_text = _split_references(full_text)

    bibliography = _parse_refs_fallback(refs_text)
    inline_refs = _find_inline_refs_fallback(body_text, bibliography)

    title = pdf_path.stem
    return ParsedPaper(
        text=body_text,
        bibliography=bibliography,
        inline_refs=inline_refs,
        title=title,
    )


def _split_references(text: str) -> tuple[str, str]:
    m = re.search(r"\n\s*(References|Bibliography|REFERENCES)\s*\n", text)
    if m:
        return text[: m.start()], text[m.end() :]
    return text, ""


def _parse_refs_fallback(refs_text: str) -> dict[str, BibEntry]:
    out: dict[str, BibEntry] = {}
    if not refs_text:
        return out
    # Split numbered refs like "[1] ..." or "1. ..."
    pattern = re.compile(r"\n\s*\[(\d+)\]\s*|\n\s*(\d+)\.\s+")
    parts = pattern.split("\n" + refs_text)
    # parts: ["", num_or_None, num_or_None, content, ...]
    i = 1
    while i + 2 < len(parts):
        num = parts[i] or parts[i + 1]
        content = parts[i + 2].strip()
        if num and content:
            key = f"b{num}"
            year_match = re.search(r"\b(19|20)\d{2}\b", content)
            year = int(year_match.group(0)) if year_match else None
            out[key] = BibEntry(key=key, raw=content, year=year, title=content[:200])
        i += 3
    return out


def _find_inline_refs_fallback(text: str, bib: dict[str, BibEntry]) -> list[InlineRef]:
    refs: list[InlineRef] = []
    # Numeric: [1], [1, 2], [1-3]
    for m in re.finditer(r"\[(\d+(?:\s*[-,]\s*\d+)*)\]", text):
        nums_str = m.group(1)
        nums: list[int] = []
        for token in re.split(r",", nums_str):
            token = token.strip()
            if "-" in token:
                a, b = token.split("-", 1)
                try:
                    nums.extend(range(int(a), int(b) + 1))
                except ValueError:
                    pass
            else:
                try:
                    nums.append(int(token))
                except ValueError:
                    pass
        for n in nums:
            key = f"b{n}"
            if key in bib:
                refs.append(InlineRef(bib_key=key, start=m.start(), end=m.end()))
    return refs
