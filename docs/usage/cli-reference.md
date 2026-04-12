# CLI reference

## Source (exactly one required)

| Flag | Description |
|---|---|
| `--pdf` | Path to a local source PDF |
| `--doi` | DOI of the source paper (e.g. `10.48550/arxiv.2211.14730`). Resolved via S2 + Sci-Hub + OA links + preprint servers |
| `--arxiv` | arXiv ID of the source paper (e.g. `2211.14730`). Downloaded directly from arxiv.org |
| `--url` | URL of the source paper (arxiv.org, doi.org, openreview.net, biorxiv.org, medrxiv.org, ssrn.com) |

## Trace options

| Flag | Default | Description |
|---|---|---|
| `--keyword` | *required* | Term or concept to trace. Matches morphological variants by default; with `--semantic`, also matches conceptual synonyms. Repeat for multiple keywords |
| `--match-mode` | `any` | `any`: at least one keyword must match. `all`: every keyword must match |
| `--depth` | `3` | Maximum recursion depth (default `1` in reverse mode) |
| `--context-window` | sentence | If set to an integer, use ±N character window instead of sentence-based ref association |
| `--consolidate` | off | Ask GROBID to consolidate references against CrossRef (more accurate, ~2-5s extra per PDF) |
| `--grobid-workers` | `4` | Concurrent GROBID parse requests per BFS level |
| `--grobid-url` | `http://localhost:8070` | GROBID service URL |
| `--s2-api-key` | none | Semantic Scholar API key ([priority order](../installation.md#semantic-scholar-api-key)) |
| `--reverse` | off | [Reverse trace](reverse.md): walk UP to papers that cite the source |
| `--reverse-limit` | `500` | Max citations fetched per level in reverse mode |
| `--enrich` | off | Enrich unavailable nodes with metadata via [OpenAlex](https://openalex.org/) |
| `--email` | none | Email for OpenAlex polite pool (10 req/s). Implies `--enrich` |
| `--supply-pdf` | none | Supply a PDF for a node as local path or URL: `ID=PATH` or `ID=URL`. Repeat for multiple |
| `--diff` | none | [Diff](diff.md) against a previous JSON export, highlighting new nodes in orange |
| `--since` | none | Highlight nodes published on or after `YYYY` or `YYYY-MM`. Works alone or with `--diff` |
| `--semantic` | off | Enable [semantic matching](semantic.md). Requires `pip install citracer[semantic]` |
| `--semantic-model` | `all-mpnet-base-v2` | Sentence-transformer model name. Implies `--semantic` |
| `--semantic-threshold` | `0.40` | Cosine similarity threshold (0.0-1.0). Implies `--semantic` |

## Output options

| Flag | Default | Description |
|---|---|---|
| `--output` | `./output/graph.html` | Output HTML file path |
| `--export` | none | Export graph to `.json` or `.graphml`. Repeat for multiple formats |
| `--details` | off | Show passages directly in node tooltips |
| `--cache-dir` | `./cache` | Local cache directory for PDFs and metadata |
| `--no-open` | off | Do not open the result in a browser |
| `-v, --verbose` | off | Verbose logging |
