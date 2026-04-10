# Literature monitoring

Citracer can compare a new trace against a previous one to highlight what changed, turning a one-shot snapshot into a monitoring workflow.

## Usage

```bash
# Step 1: initial trace with JSON export
citracer --pdf paper.pdf --keyword "attention" --depth 3 --export baseline.json

# Step 2: months later, re-run and diff
citracer --pdf paper.pdf --keyword "attention" --depth 3 --diff baseline.json
```

New nodes are colored **orange** and labeled **NEW** in the info panel. The legend gains a clickable *"new (since last run)"* row to toggle them.

## Date filtering

`--since` highlights nodes by publication date, with or without `--diff`:

```bash
# Papers published 2025 or later (standalone)
citracer --pdf paper.pdf --keyword "attention" --since 2025

# Papers published June 2025+ AND not in the baseline (intersection)
citracer --pdf paper.pdf --keyword "attention" --diff baseline.json --since 2025-06
```

| Flags | A node is "new" when |
|---|---|
| `--diff` only | Not in baseline |
| `--since` only | Published on or after the date |
| Both | Both conditions met |

Month-level precision uses the `publicationDate` field from Semantic Scholar (YYYY-MM-DD). When only the year is known, `--since 2024-06` falls back to year-only comparison.

!!! note
    Nodes with no known publication date are skipped by `--since` and a warning is logged with the count.

## Export

Both `is_new` flags (on nodes and edges) are included in JSON and GraphML exports, so downstream scripts can consume the diff without re-running citracer.

## Known limitation

`paper_id` is not fully stable across runs. If a paper was resolved by title hash in one run and by DOI in another, it may falsely appear as "new". Re-running both traces from the same cache directory minimizes this.
