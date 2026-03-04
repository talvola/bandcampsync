# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BandcampSync is a Python CLI tool (and Docker service) that synchronizes music purchased on Bandcamp with a local directory. It authenticates via exported session cookies, indexes local media and remote purchases, then downloads missing items (defaulting to FLAC format).

## Common Commands

```bash
make test          # Run all tests (uv run python -m pytest -v)
make lint          # Lint with ruff (uvx ruff check)
make format        # Format with ruff (uvx ruff format)
make build         # Build package (uv build)
make container     # Build Docker image

# Run a single test
uv run python -m pytest tests/test_syncer.py::test_skips_preorder -v
```

Package manager is `uv`. Dev dependencies (pytest, pytest-mock, ruff) are in `pyproject.toml` under `[dependency-groups] dev`.

## Architecture

**Entry points:**
- `bin/bandcampsync` — CLI script that parses args, reads cookies, calls `do_sync()`
- `bin/bandcampsync-service` — Docker service runner (scheduled daily sync)
- `bandcampsync/__init__.py` — Public API exposing `do_sync()` and `Syncer`

**Core modules:**
- `sync.py` — `Syncer` class orchestrates the entire sync flow in `__init__`: indexes local media, authenticates to Bandcamp, loads purchases, syncs items (with async concurrency support), then sends notifications. All work happens during construction.
- `bandcamp.py` — `Bandcamp` class handles HTTP sessions (curl-cffi with Chrome impersonation), cookie parsing (SimpleCookie and Netscape formats), purchase loading via HTML scraping (BeautifulSoup), and download URL resolution. `BandcampItem` is the data class for purchases.
- `media.py` — `LocalMedia` indexes the local filesystem (`Artist/Album/` structure), tracks downloads via `bandcamp_item_id.txt` files, and handles path sanitization.
- `download.py` — Streaming file download, ZIP extraction, file move/copy operations. Custom exceptions: `DownloadBadStatusCode`, `DownloadInvalidContentType`, `DownloadExpired`.
- `ignores.py` — `Ignores` manages an ignore file (alternative to `bandcamp_item_id.txt`) and substring-based ignore patterns.
- `notify.py` — `NotifyURL` sends HTTP GET/POST notifications (e.g., to trigger Plex/Jellyfin library refresh).

**Sync flow:** Authenticate → index local media → load all Bandcamp purchases → for each purchase: check ignored/preorder/already-downloaded → download archive → extract/copy to `Artist/Album/` → write tracking file or update ignore file → optionally notify external service.

**Deduplication:** Two strategies — `bandcamp_item_id.txt` files in each album directory (default), or a centralized ignore file (`--ignore-file`). The `--skip-item-index` flag skips filesystem traversal entirely, relying solely on the ignore file.

## Testing

Tests are in `tests/` using pytest with pytest-mock. Test fixtures (JSON payloads) are in `tests/data/`. Tests mock HTTP responses and verify sync logic, Bandcamp API parsing, and download behavior.

## Key Details

- Python 3.10+ required (`.python-version` specifies 3.10)
- Only two runtime dependencies: `beautifulsoup4` and `curl-cffi`
- `Syncer.__init__` performs all work (sync + notify) — it's not a two-step init-then-run pattern
- Concurrency uses `asyncio.run()` with `run_in_executor` for blocking I/O
