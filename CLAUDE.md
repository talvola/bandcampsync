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
- `bin/bandcampfree` — CLI for the free/pay-what-you-want label watcher (see below)
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

## Sync Operations (Erik's Setup)

When asked to sync Bandcamp purchases, use these settings:

```bash
# Report mode (check what's new without downloading)
uv run python bin/bandcampsync \
  -c "bandcamp.com_cookies (1).txt" \
  -d "N:/Bandcamp (FLAC)" \
  -I "N:/Bandcamp (FLAC)/.bandcamp-ignore" \
  --dir-format zip --report

# Full sync (download missing items)
uv run python bin/bandcampsync \
  -c "bandcamp.com_cookies (1).txt" \
  -d "N:/Bandcamp (FLAC)" \
  -I "N:/Bandcamp (FLAC)/.bandcamp-ignore" \
  --dir-format zip -f flac -j 3
```

- **Cookies file:** `bandcamp.com_cookies (1).txt` (exported from browser)
- **Music directory:** `N:\Bandcamp (FLAC)`
- **Directory format:** `zip` — "Artist - Album" for regular artists, label subdirectory for compilations/label releases
- **Ignore file:** `N:\Bandcamp (FLAC)\.bandcamp-ignore` — contains IDs of non-downloadable pages (e.g. submission forms, placeholder pages)
- **Format:** FLAC
- **Concurrency:** 3
- **Manually downloaded items:** The report auto-detects them by name/ID match; no manual tracking needed before syncing.
- **Report runtime:** ~5 minutes (indexes all local media first). Always run report before full sync.

## Free / Pay-What-You-Want Label Downloader (`bandcampfree`)

A second, independent tool that watches record label pages for free albums. It cannot reuse
the collection sync: **claiming an album at $0 does not add it to the Bandcamp collection**
(Bandcamp requires ~€0.50 minimum), so `load_purchases()` never sees these items.

**Modules:** `labels.py` (mobile API client, `FreeAlbum`, classification), `labelconfig.py`
(YAML + match predicates), `freesync.py` (scan orchestration, `FreeState`, `LabelIndex`,
report), `freedownload.py` (acquisition + download), `gmail.py` (emailed link retrieval).

```bash
# Report what would be downloaded (no side effects)
uv run python bin/bandcampfree -C labels.yaml --report

# One-time Gmail authorisation
uv run python bin/bandcampfree --gmail-auth

# Download, bounded
uv run python bin/bandcampfree -C labels.yaml --limit 5 --max-gb 10
```

**Non-obvious details, all verified against live Bandcamp:**

- Discovery uses the undocumented mobile API (`bandcamp.com/api/mobile/24/band_details` and
  `tralbum_details`). No HTML scraping — it returns prices, per-track artists and release
  dates as clean JSON. Scraping `/music` requires unioning static `<li>` elements with a
  disjoint `data-client-items` blob, and `?page=2` is a no-op.
- **Free detection:** `price == 0.0 and not is_set_price`. Do **not** gate on
  `free_download` — that maps to `download_pref == 1` and is False for ordinary
  name-your-price-at-$0 albums.
- **`/email_download` requires `country` and `postcode`.** Omitting them returns
  `"Sorry, this item is no longer available for free."`, which means bad location data, not
  quota exhaustion. No cookies or reCAPTCHA token are needed.
- **Compilation detection is per-label; no signal generalises.** Future Avenue uses
  `Various Artists` with per-track artists; Tadpole Records uses the label as album artist,
  leaves every track artist null, and encodes it in the title as `Artist : Title`.
- **Rate limiting is real** — a full scan of a 674-album label hits HTTP 429. `prefilter()`
  rejects albums using the artist/title from the single `band_details` call before spending
  a per-album request (674 → 70 for Future Avenue). Keep `request_delay` at 1s or higher.
- **Scan cutoff is the newest release date already examined**, stored in state — never file
  mtime, which updates on download and would hide older un-fetched releases. Wanted-but-not-
  downloaded albums are kept in `state.pending` and reconsidered regardless of cutoff.

## Key Details

- Python 3.10+ required (`.python-version` specifies 3.10)
- Only two runtime dependencies: `beautifulsoup4` and `curl-cffi`
- `Syncer.__init__` performs all work (sync + notify) — it's not a two-step init-then-run pattern
- Concurrency uses `asyncio.run()` with `run_in_executor` for blocking I/O
