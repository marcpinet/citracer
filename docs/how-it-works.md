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

The keyword is compiled to a morphological regex (e.g. `channel-independent` matches `channel-independence`, `channelindependently`; `model` matches `modelling` but not `modern`). Matches must end on a word boundary and acronyms (`GAN`, `LSTM`) are case-sensitive. Paragraphs containing a match are segmented into sentences with [pysbd](https://github.com/nipunsadvilkar/pySBD), and each match is associated with references in the same sentence or the next. A sentence contributes one passage, however many times the keyword appears in it.

With `--semantic`, a second pass embeds remaining sentences with a sentence-transformer and compares them to the keyword by cosine similarity, catching conceptual matches the regex missed. See [Semantic matching](usage/semantic.md).

## 4. Reference resolution

Each cited paper is resolved through a cascade:

1. **GROBID-extracted arXiv ID or DOI** (direct, most reliable). DOIs are looked up on Semantic Scholar by ID, in batches of up to 500 per request.
2. **Title search**: Semantic Scholar's title-match endpoint first when an API key is set, otherwise arXiv first (phrase, then keyword fallback) with Semantic Scholar as fallback. Fuzzy title validation and year cross-check ±3 years. S2 calls use 429/5xx-aware backoff honouring `Retry-After`, plus a circuit breaker.
3. **OpenReview** (covers ICLR/TMLR papers, which have no DOI; circuit breaker on timeouts)
4. **OpenAlex** (optional, via `--enrich`)

Each reference is resolved once per run, however many papers cite it.

PDF download cascade: user-supplied PDF > arXiv > OpenReview > Sci-Hub > S2 open-access URL > preprint servers (bioRxiv, medRxiv, ChemRxiv, SSRN, PsyArXiv, AgriXiv, engrXiv).

All resolved PDFs, GROBID outputs (TEI) and metadata are cached locally in `./cache/`: re-running a trace skips GROBID for every PDF already parsed. A genuine "not found" is cached for 7 days; failures caused by timeouts, rate limits or outages are never cached. Citation counts are refreshed after 30 days.

## 5. BFS recursion

The BFS runs as a streaming pipeline. Parsing and keyword matching run in a thread pool (`--grobid-workers`, default 4); a parsed paper's references are resolved right away in a second pool, and each resolved PDF is sent to GROBID immediately. The graph is still built in strict BFS order, so depths are the same as a level-by-level walk. Deduplication uses a canonical ID (DOI > arXiv > OpenReview > title hash) plus an alias index: a paper reached by DOI and by arXiv ID (or by title) is one node. When the same paper is reached via a second path, only a new edge is added.

Year anchoring: bibliography years can backfill a node's year when older (e.g. preprint 2022 vs publication 2023), but only within a ±2 year window to prevent parser error propagation.

## 6. Cross-graph bibliographic links

After the BFS, a post-processing pass matches every parsed paper's bibliography against every other node in the graph. Matches (by DOI, arXiv ID, or fuzzy title) are added as dashed "bibliographic link" edges. No API calls needed.

## 7. Analytics

Per-node centrality metrics (PageRank, betweenness) and graph-wide statistics are computed with [networkx](https://networkx.org/). Pivot papers are automatically detected. See [Analytics](output/analytics.md).

## 8. Rendering

The graph is rendered as an interactive HTML page with [pyvis](https://pyvis.readthedocs.io/), with a custom overlay providing controls, legend, info panel, keyword highlighting, and KaTeX math rendering. See [Visualization](output/visualization.md).
