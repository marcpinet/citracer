"""Data models for citracer."""
from __future__ import annotations
from dataclasses import dataclass, field

from .utils import make_paper_id, normalize_arxiv_id, normalize_doi, normalize_title


@dataclass
class BibEntry:
    """A bibliography entry parsed from a paper."""
    key: str  # internal key, e.g. "b36" or "Nie2023"
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    raw: str = ""  # raw text fallback


@dataclass
class InlineRef:
    """An inline citation occurrence in the text."""
    bib_key: str  # references a BibEntry.key
    start: int    # character offset in the full text
    end: int


@dataclass
class ParsedPaper:
    """Output of pdf_parser."""
    text: str
    bibliography: dict[str, BibEntry]  # key -> entry
    inline_refs: list[InlineRef]
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    doi: str | None = None
    arxiv_id: str | None = None
    year: int | None = None


@dataclass
class KeywordHit:
    """A passage in the text where the keyword was found."""
    passage: str            # contextual snippet
    match_start: int        # absolute offset of the match in the full text
    match_end: int
    ref_keys: list[str]     # bib keys of references within the context window
    keyword: str = ""       # which keyword produced this hit (multi-keyword mode)
    match_type: str = "regex"  # "regex" or "semantic"
    semantic_score: float = 0.0  # cosine similarity (only set for semantic hits)


@dataclass
class PaperNode:
    paper_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    publication_date: str | None = None  # YYYY-MM-DD from S2, finer than year
    arxiv_id: str | None = None
    doi: str | None = None
    abstract: str | None = None
    citation_count: int | None = None
    keyword_hits: list[str] = field(default_factory=list)
    # Parallel lists for each keyword_hit. Only used by the visualizer; not exported.
    keyword_hit_types: list[str] = field(default_factory=list, repr=False)   # "regex" or "semantic"
    keyword_hit_scores: list[float] = field(default_factory=list, repr=False)  # cosine sim (0 for regex)
    status: str = "pending"  # "analyzed" | "unavailable" | "no_match" | "root"
    depth: int = 0
    is_new: bool = False     # set by --diff / --since, rendering overlay only
    url: str | None = None
    # Populated only for nodes we actually parsed (root + analyzed + no_match).
    # Used to discover cross-graph citations when --show-all-citations is on.
    bibliography: dict[str, "BibEntry"] = field(default_factory=dict)
    # The year this node was first assigned — frozen so that repeated
    # backfill attempts compare against a stable anchor and can't cascade
    # away from the truth. `year` may drift from this; `original_year`
    # does not.
    original_year: int | None = None


@dataclass
class CitationEdge:
    source_id: str
    target_id: str
    context: str = ""
    depth: int = 0
    # "primary"   = citation associated with a keyword occurrence (solid line)
    # "secondary" = bibliographic-only link between two graph nodes, added
    #               when --show-all-citations is set (rendered dashed)
    edge_type: str = "primary"
    is_new: bool = False  # set by --diff, rendering overlay only


#: Shortest normalized title allowed to identify a paper on its own. Short
#: titles ("Deep learning") are shared by distinct works.
TITLE_ALIAS_MIN_LEN = 15

#: Max year gap between two records merged on title alone.
TITLE_ALIAS_MAX_YEAR_GAP = 2


def identity_keys(
    paper_id: str | None = None,
    doi: str | None = None,
    arxiv_id: str | None = None,
) -> list[str]:
    """Every id-style key under which a paper can be known."""
    keys = []
    if paper_id:
        keys.append(paper_id)
    if doi:
        keys.append(make_paper_id(doi=doi))
    if arxiv_id:
        keys.append(make_paper_id(arxiv_id=arxiv_id))
    return keys


@dataclass
class TracerGraph:
    nodes: dict[str, PaperNode] = field(default_factory=dict)
    edges: list[CitationEdge] = field(default_factory=list)
    _edge_index: set[tuple[str, str, str]] = field(default_factory=set, repr=False)
    # Alias index: every known id of a paper (doi:..., arxiv:..., title:...)
    # -> the node id it was first added under. Lets the tracer recognise a
    # paper reached once by DOI and once by arXiv id as the same node.
    _aliases: dict[str, str] = field(default_factory=dict, repr=False)
    _title_aliases: dict[str, str] = field(default_factory=dict, repr=False)

    def add_node(self, node: PaperNode) -> None:
        if node.paper_id not in self.nodes:
            self.nodes[node.paper_id] = node
            self.register_aliases(node.paper_id, doi=node.doi, arxiv_id=node.arxiv_id,
                                  title=node.title)

    def register_aliases(
        self,
        node_id: str,
        doi: str | None = None,
        arxiv_id: str | None = None,
        title: str | None = None,
        extra_ids: list[str] | None = None,
    ) -> None:
        for key in identity_keys(node_id, doi, arxiv_id) + list(extra_ids or []):
            self._aliases.setdefault(key, node_id)
        nt = normalize_title(title)
        if len(nt) >= TITLE_ALIAS_MIN_LEN:
            self._title_aliases.setdefault(nt, node_id)

    def find(
        self,
        paper_id: str | None = None,
        doi: str | None = None,
        arxiv_id: str | None = None,
        title: str | None = None,
        year: int | None = None,
    ) -> str | None:
        """Return the id of the node that is the same paper, if any.

        Identifiers are tried first. A title match is only accepted when no
        identifier contradicts it (two different DOIs or two different arXiv
        ids) and the years, when both known, are close.
        """
        for key in identity_keys(paper_id, doi, arxiv_id):
            node_id = self._aliases.get(key)
            if node_id is not None and node_id in self.nodes:
                return node_id
        nt = normalize_title(title)
        if len(nt) < TITLE_ALIAS_MIN_LEN:
            return None
        node_id = self._title_aliases.get(nt)
        node = self.nodes.get(node_id) if node_id else None
        if node is None:
            return None
        d, nd = normalize_doi(doi), normalize_doi(node.doi)
        a, na = normalize_arxiv_id(arxiv_id), normalize_arxiv_id(node.arxiv_id)
        if (d and nd and d != nd) or (a and na and a != na):
            return None
        if year and node.year and abs(year - node.year) > TITLE_ALIAS_MAX_YEAR_GAP:
            return None
        return node_id

    def absorb(self, node_id: str, **fields) -> None:
        """Merge metadata of another record of the same paper into
        ``node_id``: fill missing fields and register its identifiers."""
        node = self.nodes[node_id]
        for name in ("doi", "arxiv_id", "abstract", "citation_count",
                     "publication_date", "url"):
            value = fields.get(name)
            if value is not None and getattr(node, name) is None:
                setattr(node, name, value)
        if not node.authors and fields.get("authors"):
            node.authors = list(fields["authors"])
        self.register_aliases(node_id, doi=fields.get("doi"),
                              arxiv_id=fields.get("arxiv_id"),
                              title=fields.get("title"),
                              extra_ids=[fields["paper_id"]] if fields.get("paper_id") else None)

    def add_edge(self, edge: CitationEdge) -> None:
        if edge.source_id == edge.target_id:
            return  # reject self-citations
        key = (edge.source_id, edge.target_id, edge.edge_type)
        if key in self._edge_index:
            return
        self._edge_index.add(key)
        self.edges.append(edge)

    def has_edge(self, source_id: str, target_id: str, edge_type: str = "primary") -> bool:
        return (source_id, target_id, edge_type) in self._edge_index
