# Installation

## Requirements

- Python 3.10+
- Docker (for GROBID)

## From PyPI

```bash
pip install citracer
docker pull lfoppiano/grobid:0.9.0
docker run --rm -p 8070:8070 lfoppiano/grobid:0.9.0
```

To enable [semantic matching](usage/semantic.md) (optional, adds ~500MB):

```bash
pip install citracer[semantic]
```

## From source

```bash
git clone https://github.com/marcpinet/citracer
cd citracer
pip install -e .
docker run --rm -p 8070:8070 lfoppiano/grobid:0.9.0
```

## Verify GROBID

GROBID must be reachable at `http://localhost:8070`:

```bash
curl http://localhost:8070/api/isalive
```

If GROBID is unavailable, citracer falls back to pymupdf + regex parsing (lower quality, with a confirmation prompt).

## Semantic Scholar API key

Optional but recommended. Without one, lookups are throttled to ~3.5s per call. With a key, ~1.1s.

The key is resolved in this order:

1. `--s2-api-key <key>` CLI flag
2. `S2_API_KEY` environment variable
3. User config at `~/.citracer/config.json`:
   ```bash
   citracer config set-s2-key <your-key>
   ```
4. `.env` file in the working directory

Get a free key at [semanticscholar.org/product/api](https://www.semanticscholar.org/product/api#api-key).

## OpenAlex email

Optional. Activates the [polite pool](https://docs.openalex.org/how-to-use-the-api/rate-limits-and-authentication) (10 req/s vs 1 req/s) when using `--enrich`:

```bash
citracer config set-email your@email.com
```

Or pass via `--email` or the `OPENALEX_EMAIL` environment variable.

## Config commands

| Command | Description |
|---|---|
| `citracer config show` | Show current config (secrets masked) |
| `citracer config set-s2-key <key>` | Save Semantic Scholar API key |
| `citracer config get-s2-key` | Print saved key (masked) |
| `citracer config clear-s2-key` | Remove saved key |
| `citracer config set-email <email>` | Save OpenAlex email |
| `citracer config get-email` | Print saved email |
| `citracer config clear-email` | Remove saved email |
| `citracer config path` | Print config file path |

The config file is created with mode `600` on POSIX systems.
