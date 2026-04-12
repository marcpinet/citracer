# Export formats

## Visual exports (from the browser)

The interactive graph includes **PNG** and **SVG** export buttons in the control panel:

- **PNG**: raster image at 2x, 3x, or 4x the screen resolution. Good for slides and reports.
- **SVG**: vector graphic with lossless zoom. Ideal for LaTeX figures, posters, and publications. Nodes, edges, labels, and arrowheads are all vector elements.

Both export only the currently visible nodes and edges (respecting legend filters).

## Data exports (from the CLI)

```bash
citracer --pdf paper.pdf --keyword "..." --export graph.json --export graph.graphml
```

Use `--export` multiple times to export in several formats in one run.

## JSON

The citracer JSON format includes all metadata:

```json
{
  "metadata": { ... },
  "analytics": { ... },
  "nodes": [
    {
      "id": "arxiv:2211.14730",
      "title": "A Time Series Is Worth 64 Words",
      "authors": ["Yuqi Nie", "..."],
      "year": 2023,
      "publication_date": "2023-01-30",
      "status": "analyzed",
      "depth": 1,
      "doi": "...",
      "arxiv_id": "2211.14730",
      "abstract": "...",
      "citation_count": 450,
      "url": "https://arxiv.org/abs/2211.14730",
      "keyword_hits": ["passage where keyword was found..."],
      "is_new": false
    }
  ],
  "edges": [
    {
      "source": "arxiv:root",
      "target": "arxiv:2211.14730",
      "type": "primary",
      "depth": 1,
      "context": "citation context passage...",
      "is_new": false
    }
  ]
}
```

The JSON export is also the format used as a baseline for [`--diff`](../usage/diff.md).

## GraphML

Standard XML format understood by Gephi, networkx, yEd, and Cytoscape:

- Node attributes: title, authors, year, status, depth, DOI, arXiv ID, abstract, citation count, keyword hits count, betweenness, pagerank, is_pivot, is_new, publication_date
- Edge attributes: edge_type, depth, context, is_new

```bash
# Load in Python with networkx
import networkx as nx
G = nx.read_graphml("graph.graphml")
```

## Reproducibility manifest

Every trace writes a `manifest.json` alongside the graph output:

- **citracer version**, **timestamp**, **full CLI command**
- **Source paper**: type, raw input, resolved title/DOI/arXiv ID
- **Parameters**: keywords, match mode, depth, context window, consolidate, reverse, enrich, GROBID URL
- **Environment**: Python version, platform, GROBID availability, API key/email status
- **Results**: node/edge counts, status breakdown, analytics summary

The manifest is also embedded in JSON exports under the `"metadata"` key.
