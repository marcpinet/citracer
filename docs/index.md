# citracer

**Trace citation chains for any keyword or concept across research papers.**

Given a source PDF and a keyword, citracer parses the bibliography, finds every occurrence of the keyword in the body, identifies the references cited nearby, downloads those papers, and recursively walks the resulting citation graph. The output is an interactive HTML page.

With `--semantic`, matching goes beyond literal keywords: a sentence-transformer model catches passages that express the same concept with different vocabulary.

![citracer interactive graph](https://raw.githubusercontent.com/marcpinet/citracer/main/readme_data/graph.png)

## Get started in 30 seconds

```bash
pip install citracer
docker run --rm -p 8070:8070 lfoppiano/grobid:0.9.0
citracer --pdf paper.pdf --keyword "channel-independent"
```

This downloads cited papers, traces the keyword through the citation graph, and opens an interactive visualization in your browser.

## Key features

- **Recursive BFS tracing** through citation graphs at configurable depth
- **Keyword + concept matching** via regex morphology and optional sentence-transformer embeddings
- **10+ paper sources**: arXiv, Semantic Scholar, OpenReview, Sci-Hub, bioRxiv, medRxiv, ChemRxiv, SSRN, PsyArXiv, AgriXiv, engrXiv
- **Reverse trace**: find papers that *cite* a source while mentioning the keyword
- **Literature monitoring**: diff against a previous trace to spot new papers
- **Interactive HTML output** with layout controls, search, filtering, and analytics
- **Bibliometric analytics**: PageRank, betweenness centrality, pivot detection, keyword density timeline
- **Export** to JSON and GraphML for downstream analysis
- **Reproducibility manifest** encoding all trace parameters

## Next steps

- [Installation](installation.md) - set up citracer and GROBID
- [Quick start](usage/quick-start.md) - run your first trace
- [CLI reference](usage/cli-reference.md) - all available flags
