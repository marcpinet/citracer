# How it works

Citracer runs a breadth-first search over the citation graph. Here is the pipeline for each paper:

## 1. PDF parsing

GROBID processes the PDF into TEI XML. Citracer walks the `<body>` to reconstruct plain text while recording the character offset of every inline citation. The bibliography is extracted from `<listBibl>`.

Two cleanup passes improve quality:

- **Figure noise filtering**: paragraphs with dense mathematical Unicode characters (likely diagram text promoted to prose by GROBID) are skipped
- **Paragraph merging**: paragraphs that GROBID splits mid-sentence around narrative citations are glued back together with a length-preserving regex, so sentence-based matching still works correctly

## 2. Inline ref recovery

GROBID misses some narrative citations like `"DLinear Zeng et al. (2023)"`. A supplementary pass scans for canonical author-year patterns (`Surname et al. (Year)`, `Surname & Other (Year)`, `Surname (Year)`) and adds them when the (surname, year) signature matches a unique bibliography entry. This typically recovers dozens of references per paper.

## 3. Keyword matching

The keyword is compiled to a morphological regex (e.g. `channel-independent` matches `channel-independence`, `channelindependently`). The text is segmented into sentences with [pysbd](https://github.com/nipunsadvilkar/pySBD), and each match is associated with references in the same sentence or the next.

With `--semantic`, a second pass embeds remaining sentences with a sentence-transformer and compares them to the keyword by cosine similarity, catching conceptual matches the regex missed. See [Semantic matching](usage/semantic.md).

## 4. Reference resolution

Each cited paper is resolved through a cascade:

1. **GROBID-extracted DOI/arXiv ID** (direct, most reliable)
2. **arXiv title search** (phrase first, then keyword fallback, with fuzzy title validation and year cross-check ±3 years)
3. **Semantic Scholar** (same title + year validation, with 429-aware backoff and circuit breaker)
4. **OpenReview** (covers ICLR/TMLR papers, with circuit breaker on timeouts)
5. **OpenAlex** (optional, via `--enrich`)

PDF download cascade: user-supplied PDF > arXiv > OpenReview > Sci-Hub > S2 open-access URL > preprint servers (bioRxiv, medRxiv, ChemRxiv, SSRN, PsyArXiv, AgriXiv, engrXiv).

All resolved PDFs and metadata are cached locally in `./cache/`.

## 5. BFS recursion

Papers are processed in BFS order. Each level's PDFs are parsed in parallel (`--grobid-workers`, default 4). Deduplication uses a canonical ID (DOI > arXiv > OpenReview > title hash). When the same paper is reached via a second path, only a new edge is added.

Year anchoring: bibliography years can backfill a node's year when older (e.g. preprint 2022 vs publication 2023), but only within a ±2 year window to prevent parser error propagation.

## 6. Cross-graph bibliographic links

After the BFS, a post-processing pass matches every parsed paper's bibliography against every other node in the graph. Matches (by DOI, arXiv ID, or fuzzy title) are added as dashed "bibliographic link" edges. No API calls needed.

## 7. Analytics

Per-node centrality metrics (PageRank, betweenness) and graph-wide statistics are computed with [networkx](https://networkx.org/). Pivot papers are automatically detected. See [Analytics](output/analytics.md).

## 8. Rendering

The graph is rendered as an interactive HTML page with [pyvis](https://pyvis.readthedocs.io/), with a custom overlay providing controls, legend, info panel, keyword highlighting, and KaTeX math rendering. See [Visualization](output/visualization.md).
