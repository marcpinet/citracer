"""Bibliography exports: one entry per paper of the graph.

- **BibTeX** (``.bib``): for LaTeX / reference managers. arXiv-only papers
  become ``@misc`` entries with ``eprint`` / ``archivePrefix`` (the usual
  arXiv style), papers with a DOI ``@article``. The keyword passages go in
  ``annote``, which Zotero and JabRef import as a note.
- **RIS** (``.ris``): the most portable import format (Zotero, Mendeley,
  EndNote); keyword passages go in ``N1`` notes.
- **CSV** (``.csv``): a spreadsheet of the papers with their status and
  metrics, UTF-8 with BOM so Excel opens accents correctly.

The same per-paper entries are embedded in the HTML page so the papers
currently visible in the graph can be exported from the browser.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata

from .models import PaperNode, TracerGraph

#: Lowercase name particles kept with the family name ("van der Maaten").
_PARTICLES = {"van", "von", "der", "den", "de", "del", "della", "di", "da",
              "du", "le", "la", "dos", "das", "ter", "ten", "zu", "af"}

_TITLE_STOPWORDS = {"a", "an", "the", "on", "of", "for", "and", "in", "to",
                    "with", "towards", "toward", "via", "is", "are"}


def select_papers(graph: TracerGraph, statuses: set[str] | None = None) -> list[PaperNode]:
    """Papers to export, root first then by depth, year and title.

    ``statuses`` keeps only nodes with one of these statuses (``root``,
    ``analyzed``, ``no_match``, ``unavailable``, or ``new`` for nodes flagged
    by ``--diff`` / ``--since``). None keeps everything.
    """
    def keep(n: PaperNode) -> bool:
        return not statuses or n.status in statuses or ("new" in statuses and n.is_new)

    return sorted(
        (n for n in graph.nodes.values() if keep(n)),
        key=lambda n: (n.status != "root", n.depth, n.year or 9999, (n.title or "").lower()),
    )


def split_name(name: str) -> tuple[str, str]:
    """``"Laurens van der Maaten"`` -> ``("Laurens", "van der Maaten")``;
    ``"Nie, Yuqi"`` -> ``("Yuqi", "Nie")``. Returns (given, family)."""
    name = " ".join(name.split())
    if "," in name:
        family, given = (x.strip() for x in name.split(",", 1))
        return given, family
    tokens = name.split(" ")
    if len(tokens) == 1:
        return "", tokens[0]
    i = len(tokens) - 1
    while i > 1 and tokens[i - 1].lower() in _PARTICLES:
        i -= 1
    return " ".join(tokens[:i]), " ".join(tokens[i:])


def _ascii_word(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)).lower())


def citation_keys(nodes: list[PaperNode]) -> dict[str, str]:
    """Stable, unique BibTeX keys: ``<surname><year><first title word>``,
    with a/b/c suffixes on collisions."""
    keys: dict[str, str] = {}
    used: dict[str, int] = {}
    for n in nodes:
        surname = _ascii_word(split_name(n.authors[0])[1]) if n.authors else ""
        words = [_ascii_word(w) for w in (n.title or "").split()]
        word = next((w for w in words if len(w) > 2 and w not in _TITLE_STOPWORDS), "")
        base = f"{surname or 'anon'}{n.year or ''}{word}" or "paper"
        count = used.get(base, 0)
        used[base] = count + 1
        keys[n.paper_id] = base if count == 0 else f"{base}{_suffix(count)}"
    return keys


def _suffix(i: int) -> str:
    out = ""
    while i > 0:
        i, r = divmod(i - 1, 26)
        out = chr(ord("a") + r) + out
    return out


# ---------------------------------------------------------------------------
# BibTeX
# ---------------------------------------------------------------------------

_BIB_ESCAPES = {
    "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "&": r"\&", "%": r"\%",
    "$": r"\$", "#": r"\#", "_": r"\_", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_BIB_ESCAPE_RE = re.compile("|".join(re.escape(c) for c in _BIB_ESCAPES))
# Words with two or more capitals (GAN, PatchTST, LSTMs) keep their case.
_CAPS_WORD_RE = re.compile(r"(?<![\w{\\])([^\W\d_]*[A-Z][^\W_]*[A-Z][^\W_]*)")


def _bib_escape(s: str) -> str:
    return _BIB_ESCAPE_RE.sub(lambda m: _BIB_ESCAPES[m.group(0)], " ".join(s.split()))


def _bib_title(s: str) -> str:
    return _CAPS_WORD_RE.sub(r"{\1}", _bib_escape(s))


def _status_note(n: PaperNode) -> str:
    hits = len(n.keyword_hits)
    return (f"citracer: {n.status.replace('_', ' ')} at depth {n.depth}"
            + (f", {hits} keyword passage(s)" if hits else ""))


def bibtex_entry(n: PaperNode, key: str, keywords: list[str] | None = None) -> str:
    entry_type = "article" if n.doi else "misc"
    fields: list[tuple[str, str]] = [("title", _bib_title(n.title or "(untitled)"))]
    if n.authors:
        names = []
        for a in n.authors:
            given, family = split_name(a)
            names.append(_bib_escape(f"{family}, {given}" if given else family))
        fields.append(("author", " and ".join(names)))
    if n.year:
        fields.append(("year", str(n.year)))
    if n.publication_date:
        fields.append(("date", n.publication_date))
    if n.doi:
        fields.append(("doi", n.doi))
    if n.arxiv_id:
        fields += [("eprint", n.arxiv_id), ("archiveprefix", "arXiv")]
    if n.url:
        fields.append(("url", n.url))
    if n.abstract:
        fields.append(("abstract", _bib_escape(n.abstract)))
    tags = ["citracer", n.status] + list(keywords or [])
    fields.append(("keywords", _bib_escape(", ".join(tags))))
    fields.append(("note", _bib_escape(_status_note(n))))
    if n.keyword_hits:
        fields.append(("annote", _bib_escape(" || ".join(n.keyword_hits))))
    body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields)
    return f"@{entry_type}{{{key},\n{body}\n}}"


def to_bibtex(nodes: list[PaperNode], keywords: list[str] | None = None) -> str:
    keys = citation_keys(nodes)
    return "\n\n".join(bibtex_entry(n, keys[n.paper_id], keywords) for n in nodes) + "\n"


# ---------------------------------------------------------------------------
# RIS
# ---------------------------------------------------------------------------

def ris_record(n: PaperNode, keywords: list[str] | None = None) -> str:
    lines = [("TY", "JOUR" if n.doi else "GEN"), ("TI", " ".join((n.title or "").split()))]
    for a in n.authors:
        given, family = split_name(a)
        lines.append(("AU", f"{family}, {given}" if given else family))
    if n.year:
        lines.append(("PY", str(n.year)))
    if n.publication_date:
        lines.append(("DA", n.publication_date.replace("-", "/")))
    if n.doi:
        lines.append(("DO", n.doi))
    if n.url:
        lines.append(("UR", n.url))
    if n.arxiv_id:
        lines.append(("AN", f"arXiv:{n.arxiv_id}"))
    if n.abstract:
        lines.append(("AB", " ".join(n.abstract.split())))
    for kw in ["citracer", n.status] + list(keywords or []):
        lines.append(("KW", kw))
    lines.append(("N1", _status_note(n)))
    for passage in n.keyword_hits:
        lines.append(("N1", " ".join(passage.split())))
    lines.append(("ER", ""))
    return "\n".join(f"{tag}  - {value}".rstrip() for tag, value in lines)


def to_ris(nodes: list[PaperNode], keywords: list[str] | None = None) -> str:
    return "\n\n".join(ris_record(n, keywords) for n in nodes) + "\n"


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "id", "title", "authors", "year", "publication_date", "status", "depth",
    "doi", "arxiv_id", "url", "citation_count", "keyword_hits", "in_degree",
    "pagerank", "betweenness", "is_pivot", "is_new", "passages", "abstract",
]


def csv_row(n: PaperNode, metrics: dict | None = None) -> list[str]:
    m = metrics or {}

    def fmt(v) -> str:
        return "" if v is None else str(v)

    return [
        n.paper_id, n.title or "", "; ".join(n.authors), fmt(n.year),
        fmt(n.publication_date), n.status, str(n.depth), fmt(n.doi),
        fmt(n.arxiv_id), fmt(n.url), fmt(n.citation_count), str(len(n.keyword_hits)),
        fmt(m.get("in_degree")), fmt(m.get("pagerank")), fmt(m.get("betweenness")),
        fmt(m.get("is_pivot")), str(n.is_new), " || ".join(n.keyword_hits),
        " ".join((n.abstract or "").split()),
    ]


def to_csv(nodes: list[PaperNode], analytics: dict | None = None) -> str:
    metrics = (analytics or {}).get("node_metrics", {})
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for n in nodes:
        writer.writerow(csv_row(n, metrics.get(n.paper_id)))
    return buf.getvalue()
