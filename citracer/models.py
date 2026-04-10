"""Data models for citracer."""
from __future__ import annotations
from dataclasses import dataclass, field


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


@dataclass
class PaperNode:
    paper_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    arxiv_id: str | None = None
    doi: str | None = None
    abstract: str | None = None
    citation_count: int | None = None
    keyword_hits: list[str] = field(default_factory=list)
    status: str = "pending"  # "analyzed" | "unavailable" | "no_match" | "root"
    depth: int = 0
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


@dataclass
class TracerGraph:
    nodes: dict[str, PaperNode] = field(default_factory=dict)
    edges: list[CitationEdge] = field(default_factory=list)
    _edge_index: set[tuple[str, str, str]] = field(default_factory=set, repr=False)

    def add_node(self, node: PaperNode) -> None:
        if node.paper_id not in self.nodes:
            self.nodes[node.paper_id] = node

    def add_edge(self, edge: CitationEdge) -> None:
        key = (edge.source_id, edge.target_id, edge.edge_type)
        if key in self._edge_index:
            return
        self._edge_index.add(key)
        self.edges.append(edge)

    def has_edge(self, source_id: str, target_id: str, edge_type: str = "primary") -> bool:
        return (source_id, target_id, edge_type) in self._edge_index
