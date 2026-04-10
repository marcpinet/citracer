# Quick start

After [installing citracer](../installation.md), run your first trace:

## From a local PDF

```bash
citracer --pdf paper.pdf --keyword "channel-independent" --depth 3
```

## From an arXiv ID

```bash
citracer --arxiv 2211.14730 --keyword "self-attention"
```

## From a DOI or URL

```bash
citracer --doi 10.48550/arxiv.2211.14730 --keyword "patching"
citracer --url https://openreview.net/forum?id=cGDAkQo1C0p --keyword "instance normalization"
```

## Multiple keywords

```bash
citracer --pdf paper.pdf --keyword "channel-independent" --keyword "patching"
```

By default (`--match-mode any`), a paper is matched if *any* keyword appears. Use `--match-mode all` to require *every* keyword.

## What happens

1. The source PDF is parsed with GROBID
2. Every sentence containing the keyword is found, along with the references cited nearby
3. Those references are downloaded (arXiv, Semantic Scholar, OpenReview, Sci-Hub, preprint servers)
4. The process repeats recursively up to `--depth` levels
5. An interactive HTML graph opens in your browser

## Next steps

- Add `--semantic` to catch conceptual matches beyond literal keywords: [Semantic matching](semantic.md)
- Trace *who cites* a paper instead of *what it cites*: [Reverse trace](reverse.md)
- Compare traces over time: [Literature monitoring](diff.md)
- See all available flags: [CLI reference](cli-reference.md)
