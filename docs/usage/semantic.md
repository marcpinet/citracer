# Semantic matching

By default, citracer matches keywords using a morphological regex: `channel-independent` also matches `channel-independence`, `channel independently`, etc. This misses papers that discuss the same concept with entirely different vocabulary.

`--semantic` adds a second pass that catches these conceptual matches.

## How it works

```bash
pip install citracer[semantic]
citracer --pdf paper.pdf --keyword "channel-independent" --semantic
```

1. **Regex pass** runs first (fast, precise). Every regex match is recorded.
2. **Semantic pass** scans the remaining sentences (those the regex didn't match) using a sentence-transformer embedding model. Each sentence is compared to the keyword by cosine similarity.
3. Sentences above the threshold are added as additional hits.
4. Results are **unioned**: `--semantic` only adds recall, never removes existing regex matches.

For example, tracing `"channel-independent"` with `--semantic` also surfaces passages like:

- *"We process each variate independently without sharing information across channels"*
- *"Cross-channel correlations are entirely decoupled from spatial correlations"*
- *"Each feature map is convolved independently using depthwise separable convolutions"*

None of these contain the literal keyword, but they all describe the same concept.

## Visual indicators

In the info panel, semantic hits are distinguished from regex hits:

- **Regex hits**: keyword is highlighted in yellow in the passage
- **Semantic hits**: prefixed with a purple **SEM** badge and the note *"conceptual match - keyword not literally present"*
- **Header**: shows the breakdown, e.g. *"7 keyword hit(s) (5 regex + 2 semantic)"*

## Model and threshold

| Flag | Default | Description |
|---|---|---|
| `--semantic-model` | `all-mpnet-base-v2` | The sentence-transformer model. ~420MB, ~110M parameters. Benchmarked at F1=0.93 on academic citation text |
| `--semantic-threshold` | `0.40` | Cosine similarity cutoff. Lower = more recall, higher = more precision |

Both flags imply `--semantic`.

For a lighter alternative (80MB, F1=0.86):

```bash
citracer --pdf paper.pdf --keyword "attention" --semantic-model all-MiniLM-L6-v2
```

## Performance

- Model loads once on first use (~5-10s), then stays cached in memory
- Sentence embeddings are batch-encoded: ~50-200ms per paper on CPU
- Overhead is small relative to GROBID parsing (~2-5s per paper) and API rate limits

## Limitations

- Not available in [reverse trace](reverse.md) mode (citation context snippets are too short for reliable embedding matching)
- Quality depends on the model. Domain-specific keywords may benefit from threshold tuning
- Requires `pip install citracer[semantic]` (adds ~500MB for sentence-transformers + PyTorch)
