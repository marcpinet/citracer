# Contributing to citracer

## Setup

```bash
git clone https://github.com/marcpinet/citracer
cd citracer
pip install -e ".[dev]"

# Optional: also install semantic matching dependencies
pip install -e ".[dev,semantic]"
```

## Running tests

```bash
pytest tests/ -v
```

The test suite is hermetic (no GROBID, no network). All external APIs are mocked. Runs in under 2 seconds.

## Code style

- No linter enforced, but keep it consistent with the existing code
- Type hints on all public functions
- Docstrings on all modules and public functions

## Pull requests

1. Fork the repo and create a branch from `main`
2. Make your changes
3. Run `pytest tests/ -v` and ensure all tests pass
4. Open a PR with a clear description of what changed and why

## Reporting bugs

Use the [bug report template](https://github.com/marcpinet/citracer/issues/new?template=bug_report.yml). Run with `-v` and include the relevant logs.

## Paper resolution issues

If citracer downloads the wrong paper or links to the wrong source, use the [paper resolution template](https://github.com/marcpinet/citracer/issues/new?template=paper_resolution.yml). These help improve the fuzzy matching and resolution cascade.
