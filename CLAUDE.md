# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BandcampSync is a Python CLI tool (and Docker service) that synchronizes music purchased on Bandcamp with a local directory. It authenticates via exported session cookies, indexes local media and remote purchases, then downloads missing items (defaulting to FLAC format).

**This repo is standalone.** It began as a fork of `meeb/bandcampsync` and left the fork network
on 2026-08-06, having diverged by 33 commits / 43 files (+7,659 −1,406) — the whole `bandcampfree`
tool does not exist upstream. **All PRs target `talvola/bandcampsync`**; `gh repo set-default` is
set to it. Before detaching, `gh pr create` silently defaulted to the parent and opened a PR on
meeb's repo that carried the entire fork as its diff — 34 files over 34 commits — and was closed
as unreviewable. The `upstream` remote is deliberately kept: `git fetch upstream` still works for
picking up meeb's fixes (currently 30 commits ahead of us), but contributing *back* would now need
a fresh fork, since a detached repo cannot open a PR into the network it left.

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

**Albums that grow after release are invisible to the sync.** Running yearly collections
(`Unwoman - 2026 Subscriber-Only Originals`) and monthly tribute comps (`PRF Monthly Tribute
Series`, 145 dirs, **not** a bandcampfree label — it syncs as a purchase) gain tracks for weeks
after they first appear. `sync_item` skips on `is_locally_downloaded*`, a yes/no on the
directory existing, and the `item_id` does not change when tracks are added, so the first
partial download is final. `--check-growth` (report-only) finds them: the collection payload
already carries `num_streamable_tracks`, so comparing it against a local audio-file count costs
no extra request. **It nominates candidates, it does not prove anything** — streamable ≠
downloadable, so bonus/hidden tracks skew both directions. There is no repair path on this side
yet; `bandcampfree --repair ITEM_ID` exists only for labels. Beware acting on a row flagged
`no id file` or `AMBIGUOUS` — PRF's 145 dirs have zero `bandcamp_item_id.txt` files, so matching
falls back to the lenient name path and a re-fetch could write a duplicate instead of topping up.

**The report is lenient, the sync is strict — `Missing: N` UNDERCOUNTS what a sync will fetch.**
In zip mode `report.classify_item` accepts three ways of being "downloaded" (id file/ignore
file, then `get_expected_name_for_zip` against `item_names`, then `find_zip_item_by_title`),
but `sync.sync_item` only accepts the first. Anything matched by name alone is reported as
downloaded and then re-downloaded. Verified 2026-08-03: report said `Missing: 1`, the sync
that followed a minute later fetched 3.
The two extras landed as near-duplicate folders because the on-disk name differed from what
zip mode derives — `Opium Warlock and Green Hog Band - …` vs `Green Hog Band and Opium
Warlock - …` (artist order), and a label-subdir copy `Stargazing at Blank Skies\… Bring the
Noise` vs a new root-level `… Bring The Noise` (case). Self-limiting: the fetch writes the id
to the ignore file, so each such item duplicates at most once.

**Scheduled: task "BandcampSync Weekly Collection"** runs `N:\bandcampsync\weekly-sync.ps1`
daily at 04:45 — after the 03:15 free sweep, and it skips if any bandcampsync *or*
bandcampfree process is running (they share the media root and the NAS link). Same
daily-trigger/6.5-day-due/`logs\.last-success` shape as the free sweep, so an unmounted N:
retries tomorrow. Unlike the free sweep it **downloads** (items are already paid for; no
approval step). Quiet weeks write only a one-line `logs\sync.log` entry; a dated file in
`N:\bandcampsync\logs\` means something happened. `-ReportOnly` and `-Force` are for testing —
`-ReportOnly` deliberately writes no success stamp.
**`Start-Process -ArgumentList` does not quote**, so `N:\Bandcamp (FLAC)` splits into three
arguments and argparse dies with `unrecognized arguments: (FLAC)`. The script's `Format-Arg`
handles it; the free sweep never hit this because none of its paths contain spaces.
Stale cookies are the expected failure mode — the script greps the tail for
`cookies|identity|authenticat` and says so in the log.

## Free / Pay-What-You-Want Label Downloader (`bandcampfree`)

A second, independent tool that watches record label pages for free albums. It cannot reuse
the collection sync: **claiming an album at $0 does not add it to the Bandcamp collection**
(Bandcamp requires ~€0.50 minimum), so `load_purchases()` never sees these items.

**Modules:** `labels.py` (mobile API client, `FreeAlbum`, classification), `labelconfig.py`
(YAML + match predicates), `freesync.py` (scan orchestration, `FreeState`, `LabelIndex`,
report), `freedownload.py` (acquisition + download), `gmail.py` (emailed link retrieval).

```bash
# Config/state/creds/scripts live on N: — pass all four flags or they default back to C:
FREE="-C N:/bandcampfree/labels.yaml -s N:/bandcampfree/state.json \
  --client-secret N:/bandcampfree/client_secret.json --token N:/bandcampfree/token.json"

uv run python bin/bandcampfree $FREE -t N:/_bcf_temp --report        # no side effects
uv run python bin/bandcampfree $FREE -t N:/_bcf_temp --pending-only --max-gb 15
uv run python bin/bandcampfree --gmail-auth                          # one-time
```
`-t N:/_bcf_temp` is required for any sizeable run — C: fills unpredictably.

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
- **Label directory names must be derived by `media.clean_label_dir_name` on BOTH sides** —
  `freesync.LabelIndex.__init__` and `freedownload._target_path`. It is narrower than
  `clean_path_component` (which `LocalMedia` shares — do not widen that one). Diverging made
  a label's whole catalogue re-download. Keep every label `name:` path-safe (no `? : * " < > |`).

**Adding new labels (the recurring task):** Erik hands over batches of ~10-12 label names.
Vet each with `N:/bandcampfree/probe_labels.py "Name" ...` (deep-check candidates with
`N:/bandcampfree/deepcheck.py "Label=BAND_ID"`; resolves subdomain
from the search API's `item_url_root`, band_id, catalogue size, VA count, a newest-~14
sample, and the FULL comp-ish title list), show him counts + samples to catch wrong pages,
then add with a per-label comment and scan with `--report -l "Name"`. Full operational
detail (with the gotchas) is in the project memory `adding-new-labels-workflow.md`.
**Two bands can answer to one name, and the search API may return the wrong one.** "Forward
Music" resolved to Forward Music Group (Canadian indie, `forwardmusic.bandcamp.com`,
band_id 1229744517) when the intended label was the progressive-house one at
`forwardmusiclabel.bandcamp.com` (4192895757) — so four free comps were never fetched, and the
per-label comment described whichever page had been found. **Repointing is safer than renaming**:
the fix changed `url`/`band_id` in place and kept `name: Forward Music`, because the label dir
already held 33 name-matched prog-house albums (no id files) that a rename would have orphaned
into a full re-download. **Repointing inherits the old band's `newest_release_seen`** — here a
future-dated 2026-08-28 that hid the entire new catalogue behind the cutoff, reporting
`73 releases, 0 examined`. Always follow a repoint with `--full-scan -l "<label>"`.

**Run probes SERIALLY** — two concurrent probes hit HTTP 429, which surfaces as
`JSONDecodeError: Expecting value: line 1 column 1` (an HTML error page) and silently truncates
a label's output. Recover with `deepcheck.py`, which retries with backoff. Do not pass `--url`
for a label search can already find: that path leaves `band_id=None` and every fetch fails.
**For a BULK unvetted list (30+ scraped names), triage first:** `triage_labels.py <file>` spends
2 requests/label (vs ~15) and marks SKIP for labels with no comp-ish titles and no VA credits;
then price only survivors with `deepcheck.py --max 6`. Expect near-zero yield — a 66-label
post-punk/goth list gave 1 usable label, because commercial vinyl labels sell their comps
($7 flat at Echozone, $10 at Goth City). See `commercial-labels-have-no-free-comps.md`.

**Match-rule selection** (rules live in `labelconfig.py`; all ANDed; empty = take all free):
- `min_track_artists: N, min_tracks: M` — multi-artist comps whose tracks carry real
  per-track artists (most comps). Lower `min_tracks` to ~6 for short comps.
  **False-positives on crew albums** where each track credits a different permutation of the same
  3-4 people (distinct=11 off a 3-person roster) — inspect per-track `band_name`s before downloading.
- `track_artists_vary: true` — small comps/splits (2-4 distinct artists) that `min_track_artists: 5` would reject.
- `various_artists: true` — comps credited to "Various Artists"; **prefilters** on cheap
  `band_details`, so essential for large catalogues.
  Because it prefilters it **hides free comps that are not VA-credited** — if the probe shows a
  free title absent from the comp-ish list, use `{}` or `title_regex` instead (Sahel: 14 VA comps
  all paid, the one free item label-credited, so the rule returned nothing).
  Labels often credit comps several ways at once (`Various Artists`/`V/A`/`V.A.`/label) — match on title.
  **The predicate is anchored**: it matches `Various Artists`/`Various Artist`/`V/A`/`V.A.`,
  the non-English `Varios/Vários Artistas`, `VV.AA.`/`AA.VV.`, and any of those with trailing
  `!`/`.` — but NOT bare `Various` or `VA`, both plausible real band names. Read the probe's
  actual credit string; use `title_regex` when it is one of those two (hit on Paranoia Musique,
  pomogite, WORLD END COLLAPSE, Munster, Dorog).
  **A label mixes spellings release to release**, so one entry in a series can vanish while the
  rest match: South America Avenue credited the free `Progressive Pulse 012` to `Varios Artistas`
  while 011/010/09 all say `Various Artists`. It was rejected at *prefilter*, so it never even
  appeared in the report as filtered-by-rule — widened 2026-08-08.
- `title_regex: "(?i)..."` — label-credited comp *series* (tracks show `distinct == 1`, so
  artist-count rules fail): samplers, tributes, "compilation". Also prefilters.
  **Anchor short generic alternatives** — a bare `dark` matched a solo EP "N3.0_Dark"; use `dark4`.
  In YAML, single-quote regexes containing backslashes: `'(?i)e\.b\.m\.'` (double quotes reject `\.`).
- empty `{}` — every release is a comp, or comps are label-credited (distinct=1) and can't
  be told apart by rule.

**Expect near-total dedup.** Erik's collection already holds most free comps — batches routinely
report 37/39 or 68/79 already downloaded. Always `--report` first, and check the label directory on
disk (with a *loose* substring) before quoting any size estimate.

**Estimate runtime from ITEM COUNT at 1-6 min each — the spread is Bandcamp-side, so quote a
range.** Measured: 9 items/50 min and 8/53 min one evening, 12 items/18 min the next morning,
same machine. `[needs email]` is no longer the driver (Gmail resolves in <1 min now); GB and
track count predict nothing (a 6-track album was 1.15 GB, a 20-track comp 0.46 GB).

**Recurring sweep (set up 2026-07-28, made self-downloading 2026-08-07).** Scheduled task
**"BandcampFree Weekly Sweep"** runs `N:\bandcampfree\weekly-report.ps1` daily at 03:15; it
scans all labels only when ~7 days have passed (`reports\.last-success` stamp), then drains
the queue it just filled with `--pending-only --max-gb 15`. Output lands in
`N:\bandcampfree\reports\`, one summary line per run in `sweep.log`, and **a sweep that finds
nothing writes no report file**. The dated report holds both what the scan wanted and a
`=== download ===` section of what landed. A daily trigger plus the due-check is the retry
mechanism: when N: is unmounted the run logs a SKIP, exits 0 and leaves the stamp alone, so it
retries tomorrow. Guards: share reachable, sweep due, and no other bandcampfree process
running (`scan_all()` rewrites `state.json`, so an overlapping manual download would corrupt
the pending queue).
**The stamp is written on a successful SCAN, not a successful download** — the scan is the
rate-limited half, so a failed download must not trigger a 425-label re-scan every morning.
Whatever did not land stays in `state.pending`. **A not-due run still drains a non-empty
queue** (guard 3, "not due ... but N queued - draining"): draining issues no per-label
requests, so an item queued on Monday goes out that night instead of waiting up to a week for
the next scan. Its log goes to `reports\drain-<stamp>.txt` and it writes no stamp — a drain is
not a scan. The concurrency guard therefore runs *before* the due check, since both paths
download. Anything past `--max-gb` waits for the next run the same way. `-NoDownload` restores the old approve-first behaviour; task
`ExecutionTimeLimit` is `PT4H` to cover a 90-min scan plus a 120-min download.
**This removed the human check on two documented failure modes** — match-rule false positives
on crew albums, and same-title comp series that overwrite each other on extract. The report is
now read after the fact rather than before, so scan `sweep.log` when adding volatile labels.

**`--report` writes state.** `generate_free_report()` has no `state.save()`, but `scan_all()`
inside it does — a report advances each label's `newest_release_seen` cutoff and fills
`state.pending`. That is what lets a later `--pending-only` run download without rescanning.

**More verified gotchas:**
- **Directory names truncate at 64 chars and zero-width spaces count.** Cityman's
  `ＩＳＵＺＵ　ＰＩＡＺＺＡ` landed with 28 U+200B of its 64 chars, cut mid-word. Cosmetic only —
  `bandcamp_item_id.txt` still keys dedup, so it will not re-download (the My Grito duplicate
  happened because the *pre-existing* copy had no id file). Shell globs can't match these dirs.
- Free **pre-releases** (future release date) download fine; `freedownload` needs no
  `is_preorder` check, unlike the collection syncer. Transient `curl (56)` failures leave the
  item in `pending` — just re-run `--pending-only`.
- "free download" / "(Free Sampler)" in a *title* does NOT imply `price == 0` — several
  labels title samplers that way but charge for them. Gate on price, never the title.
- Same-titled comp series (e.g. Wiretap's ~15 identical "ATTENTION!" titles) collide in the
  `Artist - Title` folder scheme and overwrite each other on extract — never bulk-download
  them; watch-for-new via a high-water mark instead (see `wiretap-same-title-collision.md`).

## Key Details

- Python 3.10+ required (`.python-version` specifies 3.10)
- Only two runtime dependencies: `beautifulsoup4` and `curl-cffi`
- `Syncer.__init__` performs all work (sync + notify) — it's not a two-step init-then-run pattern
- Concurrency uses `asyncio.run()` with `run_in_executor` for blocking I/O
