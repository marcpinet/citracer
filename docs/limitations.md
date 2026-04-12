# Limitations

- **GROBID sub-citations**: GROBID misclassifies a small fraction of references, particularly sub-citations with letter suffixes like `Liu et al., 2024b`. These are silently dropped.

- **Ambiguous narrative citations**: The supplementation pass skips ambiguous `(surname, year)` signatures (e.g. two different Zhou 2022 papers in the bibliography). Rare but possible in survey papers.

- **Sentence splitting**: pysbd handles most academic abbreviations but can occasionally split mid-sentence. Falling back to `--context-window 300` sometimes helps.

- **arXiv rate limits**: arXiv enforces ~3 seconds between requests. The first run on a deep trace can take several minutes. Subsequent runs are fast thanks to the local cache.

- **Unavailable papers**: Papers not on arXiv, OpenReview, Sci-Hub, S2 open-access, or any supported preprint server appear as red `unavailable` nodes. Books and some workshop proceedings are typically not retrievable. Use `--supply-pdf` to provide PDFs manually (local path or URL).

- **Fruchterman-Reingold layout**: Implemented via vis.js's `forceAtlas2Based` solver, which is the closest available approximation. A proper Kamada-Kawai implementation isn't offered because vis.js doesn't ship one.

- **Semantic matching model quality**: The default `all-mpnet-base-v2` was benchmarked at F1=0.93 on academic citation text. Domain-specific keywords may benefit from threshold tuning. Not available in reverse trace mode.

- **Diff paper_id instability**: `paper_id` is not fully stable across runs. A paper resolved by title hash in one run and by DOI in another may falsely appear as "new" in a diff. Using the same cache directory minimizes this.
