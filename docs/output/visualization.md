# Visualization

Citracer produces an interactive HTML graph. Nodes are papers, edges are citations.

## Node colors

| Color | Status | Meaning |
|---|---|---|
| Blue | `root` | The source PDF |
| Green | `analyzed` | PDF retrieved and the keyword (or concept) was found |
| Gray | `no_match` | PDF retrieved and parsed, but no keyword match |
| Red | `unavailable` | PDF could not be retrieved |
| Orange | `new` | Not in the `--diff` baseline and/or after the `--since` date |

## Edge styles

| Style | Type | Meaning |
|---|---|---|
| Solid dark | keyword-associated | Paper A cites paper B near a keyword match |
| Dashed blue | bibliographic link | Paper A cites paper B, but not near any keyword. Hidden by default |

## Control panel

The top-left panel provides:

| Control | Description |
|---|---|
| **Search** | Fuzzy match by title or author. Click a result to focus the node |
| **Layout** | Sugiyama by year (default), Sugiyama by depth, Force-directed, Fruchterman-Reingold |
| **Node size** | Scale by in-graph citations, keyword hits, PageRank, or betweenness |
| **Spread** | Slider (0.3x to 3.0x) to stretch or compress the layout |
| **Curved edges** | Toggle between curved and straight edge rendering |
| **Export PNG** | Export the current view as a high-resolution raster image (2x, 3x, or 4x scale) |
| **Export SVG** | Export as a vector file (lossless zoom, ideal for LaTeX figures and posters) |
| **Nodes legend** | Click to show/hide nodes by status |
| **Edges legend** | Click to show/hide keyword-associated or bibliographic edges |

## Interactions

- **Hover** a node to see its info panel (title, authors, year, status, metrics, keyword hits, abstract)
- **Click** a node to pin the panel. Click again or press x to unpin
- **Right-click** a node for Hide, Pin/Unpin, or Open link
- **Drag** any node to reposition it
- **LaTeX** in passages is rendered with KaTeX

## State persistence

Node positions, filters, pin state, and all control settings are saved to `localStorage`. Refreshing the page restores the exact view. Use *"reset saved state"* at the bottom of the legend to clear everything.
