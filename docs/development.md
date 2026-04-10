# Development

## Setup

```bash
git clone https://github.com/marcpinet/citracer
cd citracer
pip install -e ".[dev]"
```

## Running tests

```bash
pytest tests/ -v
```

The test suite is hermetic: no GROBID, no network. GROBID output is tested via a pre-baked TEI fixture in `tests/fixtures/sample.tei.xml`. All external APIs are mocked. Runs in under 2 seconds.

## CI

GitHub Actions runs the suite on Python 3.10, 3.11, and 3.12 on every push to `main`, every pull request, and on manual dispatch. See `.github/workflows/tests.yml`.

## Project structure

```
citracer/
├── cli.py                  # argparse entry point, GROBID health check, .env loader
├── pdf_parser.py           # GROBID + TEI walking, figure-noise filter, paragraph merge, pymupdf fallback
├── keyword_matcher.py      # morphological regex, sentence-based ref association, semantic search
├── reference_resolver.py   # arXiv-first cascade resolver with SQLite cache
├── source_resolver.py      # routes --pdf / --doi / --arxiv / --url to a local PDF
├── preprint_resolver.py    # maps DOIs to preprint server PDF URLs
├── metadata_enrichment.py  # OpenAlex API client
├── metadata_cache.py       # SQLite key/value store, thread-safe
├── analytics.py            # PageRank, betweenness, pivot detection, timeline
├── cross_citation.py       # post-trace bibliographic link discovery
├── diff.py                 # --diff / --since comparison logic
├── tracer.py               # BFS recursion, parallel parsing, deduplication
├── visualizer.py           # pyvis rendering pipeline
├── exporter.py             # JSON and GraphML export
├── manifest.py             # reproducibility manifest
├── models.py               # dataclasses (PaperNode, CitationEdge, TracerGraph, etc.)
├── api_types.py            # TypedDicts for external API payloads
├── constants.py            # all tunable thresholds and timeouts
├── user_config.py          # persistent user config (~/.citracer/config.json)
├── utils.py                # ID normalization, hashing, tqdm-safe logging
└── templates/
    └── overlay.html.tmpl   # interactive control panel (HTML/CSS/JS)
```

## Dependencies

| Package | Used for |
|---|---|
| [GROBID](https://github.com/kermitt2/grobid) | PDF structural parsing (external Docker service) |
| [lxml](https://lxml.de/) | TEI XML processing |
| [pymupdf](https://pymupdf.readthedocs.io/) | PDF text extraction (fallback) |
| [arxiv](https://github.com/lukasschwab/arxiv.py) | arXiv search and download |
| [pysbd](https://github.com/nipunsadvilkar/pySBD) | Sentence boundary detection |
| [pyvis](https://pyvis.readthedocs.io/) | Interactive HTML graph rendering |
| [rapidfuzz](https://github.com/rapidfuzz/RapidFuzz) | Fuzzy title matching |
| [networkx](https://networkx.org/) | Graph analytics |
| [requests](https://requests.readthedocs.io/) | HTTP client |
| [tqdm](https://github.com/tqdm/tqdm) | Progress bar |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | `.env` file loading |
| [sentence-transformers](https://www.sbert.net/) | Semantic matching (optional: `pip install citracer[semantic]`) |

Frontend (CDN, no install):

| Library | Used for |
|---|---|
| [vis-network](https://visjs.github.io/vis-network/docs/network/) | Interactive network rendering (via pyvis) |
| [KaTeX](https://katex.org) | LaTeX math rendering |

External APIs: [arXiv](https://info.arxiv.org/help/api/index.html), [Semantic Scholar](https://api.semanticscholar.org/api-docs/graph), [OpenReview](https://docs.openreview.net/reference/api-v2), [OpenAlex](https://docs.openalex.org/), [Sci-Hub](https://sci-hub.in/), and preprint servers (bioRxiv, medRxiv, ChemRxiv, SSRN, PsyArXiv, AgriXiv, engrXiv).
