# Reverse trace

The default (forward) trace walks **down** from a root paper into its bibliography. `--reverse` walks **up**: it finds papers that *cite* the source while mentioning the keyword in their citation context.

## Usage

```bash
citracer --arxiv 2211.14730 --keyword "channel-independent" --reverse
```

No PDFs are downloaded. The entire trace runs on Semantic Scholar metadata.

## How it works

Semantic Scholar's `/paper/{id}/citations` endpoint returns a `contexts` field for each citing paper: 1-2 sentence snippets around each place the paper cites the source. Citracer applies the keyword regex to these snippets locally:

- **Match**: the citing paper is added to the graph with the snippet as its keyword hit
- **No match**: the citing paper is skipped

For a paper with 2000+ citations, this runs in ~10-30 seconds and typically surfaces 20-100 relevant papers.

## Options

| Flag | Default | Description |
|---|---|---|
| `--reverse` | off | Enable reverse trace |
| `--reverse-limit` | `500` | Max citations fetched per level. Protects against papers with thousands of citations |
| `--depth` | `1` | Recursion depth. `--depth 2` finds citers of citers (can expand quickly) |

!!! warning "Deep recursion"
    `--depth > 2` can expand combinatorially. Each level multiplies the number of S2 API calls. Use `--reverse-limit` to cap growth.

## Graph topology

Reverse traces produce a **star topology** (many nodes pointing to one root), so the visualizer defaults to a force-directed layout instead of Sugiyama. You can switch at runtime.

## Limitations

- Depends entirely on Semantic Scholar having indexed the citation contexts
- Papers S2 doesn't know about won't appear
- No cross-graph bibliographic links (bibliographies are not parsed)
- `--semantic` is not available in reverse mode (snippets are too short for embedding-based matching)
