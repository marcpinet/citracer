# Analytics

Every trace automatically computes bibliometric metrics on the citation graph.

## Per-node metrics

| Metric | Description |
|---|---|
| **PageRank** | Importance relative to the citation structure |
| **Betweenness centrality** | Identifies "bridge" papers connecting different clusters |
| **In/out degree** | Number of incoming/outgoing edges in the graph |
| **Pivot** | Flagged on the earliest keyword-matched paper in each connected component, and on high-betweenness papers with the keyword |

Per-node metrics appear in the info panel when hovering or clicking a node.

## Global metrics

| Metric | Description |
|---|---|
| **Graph density** | Ratio of actual edges to maximum possible edges |
| **Avg degree** | Mean number of connections per node |
| **Connected components** | Number of weakly connected subgraphs |

## Keyword density timeline

A per-year breakdown of keyword usage across the graph:

- **total**: papers published that year in the graph
- **with_keyword**: papers where the keyword was found
- **keyword_density**: ratio of the two

The timeline is displayed as a table with mini bar charts in the analytics section of the control panel.

## Pivot detection

A paper is flagged as a **pivot** if:

1. It is the earliest keyword-matched paper in its connected component (the paper that likely introduced the concept to that sub-community), or
2. It has high betweenness centrality (>2x mean) and the keyword was found in it

Pivot papers are shown with a **PIVOT** badge in the info panel and listed in the analytics section.

## In exports

- **JSON**: analytics are under the `"analytics"` key
- **GraphML**: betweenness, pagerank, and is_pivot are node attributes
- **Manifest**: global metrics, timeline, and pivot list are in the results section
