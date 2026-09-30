# Export formats

## Visual exports (from the browser)

The interactive graph includes **PNG** and **SVG** export buttons in the control panel:

- **PNG**: raster image at 2x, 3x, or 4x the screen resolution. Good for slides and reports.
- **SVG**: vector graphic with lossless zoom. Ideal for LaTeX figures, posters, and publications. Nodes, edges, labels, and arrowheads are all vector elements.

Both export only the currently visible nodes and edges (respecting legend filters).

The **papers: BibTeX / RIS / CSV** buttons export the currently visible papers as a bibliography, with the same entries as the CLI exports below. Filter the graph first (e.g. hide `unavailable` and `no_match` in the legend) to export only the papers that discuss the keyword.

## Data exports (from the CLI)

```bash
citracer --pdf paper.pdf --keyword "..." --export graph.json --export graph.graphml
citracer --pdf paper.pdf --keyword "..." --export refs.bib --export refs.ris --export refs.csv
```

Use `--export` multiple times to export in several formats in one run. `--export-status root,analyzed` restricts the `.bib`, `.ris` and `.csv` exports (and `--zotero`) to papers with those statuses (`root`, `analyzed`, `no_match`, `unavailable`, `new`).

## BibTeX (`.bib`)

One entry per paper, root first. Papers with a DOI are `@article`, arXiv-only papers `@misc` with `eprint` / `archiveprefix` (the usual arXiv style). Keys look like `nie2022time` (surname, year, first title word, with `a`/`b` suffixes on collisions). Capitalised words (`PatchTST`, `GANs`) are brace-protected. Each entry carries `keywords = {citracer, <status>, <keywords>}`, a `note` with the status and depth, and the keyword passages in `annote` (imported as a note by Zotero and JabRef).

## RIS (`.ris`)

The most portable import format: drag the file into Zotero, Mendeley or EndNote. Authors, year, date, DOI, URL, arXiv ID (`AN`), abstract, keywords (`KW`) and the keyword passages as `N1` notes.

## CSV (`.csv`)

One row per paper: `id, title, authors, year, publication_date, status, depth, doi, arxiv_id, url, citation_count, keyword_hits, in_degree, pagerank, betweenness, is_pivot, is_new, passages, abstract`. UTF-8 with a BOM so Excel displays accents correctly.

## Zotero (`--zotero`)

```bash
citracer config set-zotero-key <key>   # once; key with write access from zotero.org/settings/keys
citracer --pdf paper.pdf --keyword "attention" --zotero --zotero-collection "Attention survey"
```

Papers are added to the collection (created if missing) through the Zotero Web API: `preprint` items for arXiv-only papers, `journalArticle` otherwise, tagged `citracer`, `citracer:<status>` and the traced keywords, with the keyword passages attached as a child note. Papers already in the collection (same DOI, arXiv ID or title) are skipped, so re-running a trace, or a [`--diff`](../usage/diff.md) monitoring run, only adds the new papers. `--zotero-library groups/<id>` writes to a group library instead of your personal one.

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
