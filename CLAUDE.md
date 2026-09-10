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

**Ad-hoc scripts that print album names need `PYTHONUTF8=1`** — the Windows console is cp1252 and
titles are full of unicode (`Hüsker Dü`, `Affaire Française`), so a bare `uv run python -c ...`
dies with `UnicodeEncodeError: 'charmap' codec`. `weekly-report.ps1` already sets it.
**Lint has a pre-existing baseline (~71 errors repo-wide, 11 in the commonly-touched files)** —
`git stash && uvx ruff check <files>` before blaming your change. Format only the files you
changed: `uvx ruff format` on the whole tree reformats unrelated ones and pollutes the diff.
**Never `find` the whole media tree** — it times out over SMB. Use `Get-ChildItem -Directory
-Filter` at depth 1-2, or build a dict from one `iterdir()` pass.
**Never patch files through a bash heredoc when the content contains Windows paths.**
`python - <<'PY'` collapses `\\` to `\` in transit, so `"N:\\bandcampsync"` reaches Python as
`N:\bandcampsync` — where `\b` is a *valid* escape (backspace) and `\w` an invalid one.
`str.replace()` then silently no-ops and the edit looks like it worked. Use the Edit tool, or
`Write` a `.py` file and run it. Always `assert s.count(old) == 1` before replacing — without
it a failed match is indistinguishable from success.
**Task Scheduler needs PowerShell, not Bash.** `schtasks /Query` via the Bash tool dies with
`Invalid argument/option - 'C:/Program Files/Git/Query'` — Git Bash rewrites `/Query` as a
path. Use `Get-ScheduledTask` / `Get-ScheduledTaskInfo`.
**Squash-merge PRs** (the `(#N)` suffix on most of the history) — Erik pushes after each piece
of work, so one PR is normally one logical change. Rebase-merge only when a branch genuinely
carries two unrelated pieces worth keeping apart; either way the history stays linear.

## Architecture

**Entry points:**
- `bin/bandcampsync` — CLI script that parses args, reads cookies, calls `do_sync()`
- `bin/bandcampsync-service` — Docker service runner (scheduled daily sync)
- `bin/bandcampfree` — CLI for the free/pay-what-you-want label watcher (see below)
- `bin/bandcampblog` — CLI for the Fuzzy Cracklins blog downloader (see below)
- `bandcampsync/__init__.py` — Public API exposing `do_sync()` and `Syncer`

**Core modules:**
- `sync.py` — `Syncer` class orchestrates the entire sync flow in `__init__`: indexes local media, authenticates to Bandcamp, loads purchases, syncs items (with async concurrency support), then sends notifications. All work happens during construction.
- `bandcamp.py` — `Bandcamp` class handles HTTP sessions (curl-cffi with Chrome impersonation), cookie parsing (SimpleCookie and Netscape formats), purchase loading via HTML scraping (BeautifulSoup), and download URL resolution. `BandcampItem` is the data class for purchases.
- `media.py` — `LocalMedia` indexes the local filesystem (`Artist/Album/` structure), tracks downloads via `bandcamp_item_id.txt` files, and handles path sanitization.
- `download.py` — Streaming file download, ZIP extraction, file move/copy operations. Custom exceptions: `DownloadBadStatusCode`, `DownloadInvalidContentType`, `DownloadExpired`.
- `ignores.py` — `Ignores` manages an ignore file (alternative to `bandcamp_item_id.txt`) and substring-based ignore patterns.
- `notify.py` — `NotifyURL` sends HTTP GET/POST notifications (e.g., to trigger Plex/Jellyfin library refresh).
- `blogsync.py` — Fuzzy Cracklins post extraction, pricing, local dedup index, download, report.
- `blogplex.py` — Plex collection membership for blog items; drains a queue, never drives a scan.

**Plex tooling lives in a sibling repo**, `C:\projects\plex-mcp-server`: `.env`
(`PLEX_URL`/`PLEX_TOKEN`, the one place to rotate), `folder_collection_sync.py` (folder→
collection jobs, with the batching and dead-ratingKey lessons already learned), and an MCP
server. Music is section 1.

**Sync flow:** Authenticate → index local media → load all Bandcamp purchases → for each purchase: check ignored/preorder/already-downloaded → download archive → extract/copy to `Artist/Album/` → write tracking file or update ignore file → optionally notify external service.

**Deduplication:** Two strategies — `bandcamp_item_id.txt` files in each album directory (default), or a centralized ignore file (`--ignore-file`). The `--skip-item-index` flag skips filesystem traversal entirely, relying solely on the ignore file.

## Testing

Tests are in `tests/` using pytest with pytest-mock. Test fixtures (JSON payloads) are in `tests/data/`. Tests mock HTTP responses and verify sync logic, Bandcamp API parsing, and download behavior.

`blogsync`/`blogplex` tests stub the network with small hand-rolled classes (`FakeAPI`,
`FakePlex`) rather than mocking HTTP, `mocker.patch` module-level functions by path
(`bandcampsync.blogsync.fetch_post`), and build fake media trees under `tmp_path`.

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

**To refetch an album — the collection side has no `--repair`:** move its directory to
**`T:\_superseded\<date>\`**, then run the normal sync. **Quarantine outside the media share, not
under `N:`** — Plex scans the Music share, so an old copy parked at `N:\_superseded\` gets indexed
and every replaced album shows up twice in the library. **Deleting only `bandcamp_item_id.txt`
does not work**: in zip mode the directory *name* alone satisfies `is_locally_downloaded_by_id`.
Move rather than delete — it is the only undo. Expect relocation: 9 of 23 refetched albums came
back under a *different* parent (root ↔ label subdirectory), because `get_path_for_zip_purchase`
decides from the zip filename once the old directory is gone.
**With `-I`, completed downloads are recorded in the ignore file, not per-directory id files**,
as `<id>  # Artist / Title`. Grep `"^<id> "` — an anchored `"^<id>$"` matches nothing and reads
as "never tracked" when it is.

**Albums that grow after release are invisible to the sync.** Running yearly collections
(`Unwoman - 2026 Subscriber-Only Originals`) and monthly tribute comps (`PRF Monthly Tribute
Series`, 146 dirs — its back catalogue arrived as collection purchases, but it has ALSO been a
bandcampfree label with `watch_growth: true` since 2026-08-08, which is what monitors it now)
gain tracks for weeks after they first appear. `sync_item` skips on `is_locally_downloaded*`, a yes/no on the
directory existing, and the `item_id` does not change when tracks are added, so the first
partial download is final. `--check-growth` (report-only) finds them: the collection payload
already carries `num_streamable_tracks`, so comparing it against a local audio-file count costs
no extra request. **In the one full run to date it was right on all 28 candidates** (verified
2026-08-08 against actual downloads), so treat a flagged album as real until proven otherwise.
**Do NOT "verify" a candidate against the public mobile API** (`tralbum_details`,
`num_downloadable_tracks`): that undercounts, because bonus tracks, demos and `-Single Version-`
extras ship in the download but are not publicly streamable, and a withdrawn album reports 0
while the purchase still downloads fine. Doing so wrongly excluded 5 of 28 — every one of which
turned out to need refetching. The only authoritative check is the **collection download link**
(`get_download_file_url`), the same path the sync itself uses. There is no repair path on this side
yet; `bandcampfree --repair ITEM_ID` exists only for labels. Beware acting on a row flagged
`no id file` or `AMBIGUOUS` — only 18 of PRF's 146 dirs carry a `bandcamp_item_id.txt` (the
hand-downloaded back catalogue has none), so matching falls back to the lenient name path and a
re-fetch could write a duplicate instead of topping up.

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
daily at 05:30 — **before** the 06:00 free sweep, and it skips if any bandcampsync *or*
bandcampfree process is running (they share the media root and the NAS link). Both were moved
later on 2026-08-08 to clear Plex's ~02:00 database-backup window, and the order was flipped
at the same time: the sweep can run 3.5 h, and while it does, this job would skip and lose a
whole day. Short job first. Same
daily-trigger/due-check/`logs\.last-success` shape as the free sweep, so an unmounted N:
retries tomorrow. **Interval is 0.8 days as of 2026-08-30 — it syncs every day** (was 6.5).
Under 1.0 rather than exactly 1.0 because the stamp is written at the *end* of the run, so the
age at the next 05:30 trigger is 1.0 minus the run length; `1.0` would skip every other day.
0.8 covers a run of up to 4.8 h, the whole `TimeoutMinutes` window. **Run length is 3 min at a
quiet 05:30 but 15 min midday** (2,601 purchases, contended SMB) — don't quote 3 min as typical.
Unlike the free sweep it **downloads** (items are already paid for; no
approval step). Quiet days write only a one-line `logs\sync.log` entry; a dated file in
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
- **Free detection:** `price == 0.0 and not is_set_price and has_digital_download and
  num_tracks > 0`. Do **not** *require* `free_download` — that maps to `download_pref == 1`
  and is False for ordinary name-your-price-at-$0 albums, the common case.
  **But a NONZERO price can still be free.** `price` is really `minimum_price` and covers
  physical copies, so a sold-out record can put €9 on a release whose download is free
  (`download_pref == 1` / `freeDownloadPage: true`). `labels._digital_price` zeroes the price
  of any `free_download` item for exactly this; normalised there, not in `is_free`, so
  freesync's `price > MAX_PRICE` ceiling keeps comparing against the real digital price.
  Found 2026-08-13 on My Proud Mountain's `15 Songs 15 Years` (Erik saw it free in the UI
  after the tool called it paid); re-pricing the corpus turned up **142** such albums.
  The `num_tracks > 0` half retires the long-standing junk class — vinyl/CD/cassette
  listings with `has_digital_download: true` and nothing to download, which used to reach
  the queue and fail with "Release offers no downloads".
  **When scoring a catalogue by hand, always classify with `album_from_details(...).is_free`,
  never a raw price comparison** — `probe_labels.py` and `deepcheck.py` both get this wrong.
- **`/email_download` requires `country` and `postcode`.** Omitting them returns
  `"Sorry, this item is no longer available for free."`, which means bad location data, not
  quota exhaustion. No cookies or reCAPTCHA token are needed.
- **Compilation detection is per-label; no signal generalises.** Future Avenue uses
  `Various Artists` with per-track artists; Tadpole Records uses the label as album artist,
  leaves every track artist null, and encodes it in the title as `Artist : Title`.
- **Rate limiting is real** — a full scan of a 674-album label hits HTTP 429. `prefilter()`
  rejects albums using the artist/title from the single `band_details` call before spending
  a per-album request (674 → 70 for Future Avenue). Keep `request_delay` at 1s or higher.
- **`FreeState.items` and `.pending` are keyed by `int`**, not str — `state.items.get("123")`
  silently returns None, and a batch script reports every album as missing.
- **Scan cutoff is the newest release date already examined**, stored in state — never file
  mtime, which updates on download and would hide older un-fetched releases. Wanted-but-not-
  downloaded albums are kept in `state.pending` and reconsidered regardless of cutoff.
- **Label directory names must be derived by `media.clean_label_dir_name` on BOTH sides** —
  `freesync.LabelIndex.__init__` and `freedownload._target_path`. It is narrower than
  `clean_path_component` (which `LocalMedia` shares — do not widen that one). Diverging made
  a label's whole catalogue re-download. Keep every label `name:` path-safe (no `? : * " < > |`).

**Adding new labels (the recurring task) — use the `assess-bandcamp-label` skill**
(`.claude/skills/assess-bandcamp-label/`). Its `assess_label.py` replaces the ad-hoc probe
dance for a single label: one command, writes nothing, prices the whole catalogue through
`album_from_details(...).is_free`, prints every distinct credit string tested against the
real `VARIOUS_ARTISTS_REGEX`, and evaluates candidate match rules through the actual
`prefilter()`/`matches()` so a rule that selects nothing is caught before it is added rather
than months later by `audit_prefilter.py`. The rest of this section is the background.

Erik hands over batches of ~10-12 label names.
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
  **A wholly dead rule is silent — it looks exactly like "nothing free this week".** The
  recurring mistake is reading "the comp is credited to the label" as a reason to use
  `various_artists`; it is the reason NOT to, since the predicate only accepts the literal
  *Various Artists*. `N:\bandcampfree\audit_prefilter.py` catches it: one `band_details` call
  per prefiltering label, runs that label's own `prefilter()` over its catalogue, flags any
  that survive 0 of N. It writes nothing, so it is safe beside a running download. The first
  run (2026-08-09, 217 labels, 0 errors, ~7 min) found 6 dead rules — Pest Records, Willowtip,
  File Under_ Records, Dune Altar (all losing free comps, some for years), plus Neon Retro
  Compilations and LaVideoteque (dead but harmless, everything priced). In four of the six the
  config comment *stated* the content was label-credited. Re-run it after adding a batch.
  It only catches total wipeouts — a rule matching 3 of 12 comps still looks healthy, and
  `min_track_artists`/`track_artists_vary` can't be evaluated at prefilter time at all.
  **Fixing the rule is only half of it: the missed releases sit behind `newest_release_seen`,
  so a normal scan still won't see them — always follow with `--full-scan -l "<label>"`.**
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
- `watch_growth: true` — **not a selection rule**; it never decides what is wanted. Re-checks
  already-downloaded releases for tracks the label added later and reports them as `GROWN`
  with a `--repair ITEM_ID` line. Never queues: repair re-fetches a whole archive, so it stays
  manual. **`--repair` only works when the label APPENDED tracks.** Filenames embed the track
  number (`<Artist> - <Album> - NN <Track>.flac`), so tracks *inserted* at the top renumber
  everything and no local name matches the archive; `extract_missing` then refuses with
  "would add 44 files but only 10 were expected", which is the guard working, not a bug — it
  is what stops a directory being duplicated. Nothing is modified, but the ~3 GB archive has
  already been fetched by then. The only fix is quarantine to `T:\_superseded\<date>\` and a
  clean re-fetch (Willowtip Sampler 2023, 2026-08-09: 39 local vs 44 remote, 0 filenames in
  common because 5 tracks went in ahead of the old track 01).
  **It disables the cutoff for that label** (the cutoff skips exactly what needs
  re-examining, and a release stops being newest long before it stops growing), so every scan
  costs one request per release — PRF's 146 add ~4 min to a sweep. Small catalogues only.

**Expect near-total dedup.** Erik's collection already holds most free comps — batches routinely
report 37/39 or 68/79 already downloaded. Always `--report` first, and check the label directory on
disk (with a *loose* substring) before quoting any size estimate.

**Estimate runtime from ITEM COUNT at 1-6 min each — the spread is Bandcamp-side, so quote a
range.** Measured: 9 items/50 min and 8/53 min one evening, 12 items/18 min the next morning,
same machine. `[needs email]` is no longer the driver (Gmail resolves in <1 min now); GB and
track count predict nothing (a 6-track album was 1.15 GB, a 20-track comp 0.46 GB).

**Recurring sweep (set up 2026-07-28, made self-downloading 2026-08-07).** Scheduled task
**"BandcampFree Weekly Sweep"** runs `N:\bandcampfree\weekly-report.ps1` daily at 06:00, then
drains the queue it just filled with `--pending-only --max-gb 15`.
**Interval is 0.8 days as of 2026-08-30 — it scans every day** (was 6.5, `reports\.last-success`
stamp). The old interval was never about release completeness, it was API politeness; a scan is
still one pass at `request_delay` 1s and measured 20-67 min for 436 labels. **The thing to watch
is `errors=N` on the scan line in `sweep.log`** — HTTP 429 truncates a label's catalogue
*silently*, so a sustained nonzero count means back the interval off rather than push through.
Output lands in `N:\bandcampfree\reports\`, one summary line per run in `sweep.log`, and **a sweep that finds
nothing writes no report file**. The dated report holds both what the scan wanted and a
`=== download ===` section of what landed. A daily trigger plus the due-check is the retry
mechanism: when N: is unmounted the run logs a SKIP, exits 0 and leaves the stamp alone, so it
retries tomorrow. Guards: share reachable, sweep due, and no other bandcampfree process
running (`scan_all()` rewrites `state.json`, so an overlapping manual download would corrupt
the pending queue).
**The stamp is written on a successful SCAN, not a successful download** — the scan is the
rate-limited half, so a failed download must not cost a second 436-label scan on top of the
one the next morning already does. Whatever did not land stays in `state.pending`. **A not-due run still drains a non-empty
queue** (guard 3, "not due ... but N queued - draining"): draining issues no per-label
requests, so an item queued between scans goes out that night rather than waiting for
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
  **That high-water mark IS the release-date cutoff, so `--full-scan` disarms it.** A
  corpus-wide `--full-scan` on 2026-08-14 re-queued 17 Wiretap comps and the unattended
  06:00 sweep merged two into one directory — 50 files, track numbers 01-20 doubled, one
  `bandcamp_item_id.txt`. **Exclude cutoff-protected labels from any all-label full scan**,
  and verify afterwards by comparing each new directory's file count against the API's
  `num_tracks` (23 of 24 matched; the mismatch was the merge).
- **Hand-downloaded back catalogue has no `bandcamp_item_id.txt`, so dedup falls back to
  title matching and re-downloads it.** All three Wiretap comps fetched that night were
  already on disk from 2022 under manual date-suffixed names (`… Charity Compilation
  (2018-08-28)`), which no title match can reach. Fix: quarantine the new copy and write the
  id file into the *existing* directory. `--backfill-ids` automates this but resolves ids by
  title, so dry-run it before `--apply` on any same-title series.
- **Retiring a release needs `state.skipped`, not just a `move`.** Dedup keys on the id file
  *inside* the directory, so quarantining a folder makes the next sweep re-download it. Put
  the item id in `state.skipped` with a free-text reason (`scan_label` reports it as an
  error instead of queueing it). Used 2026-08-14 to retire two omnibus releases that
  duplicated individual volumes already on disk — labels publish both, each with its own
  item id, and the rules legitimately match both. Check the volumes cover the omnibus
  (113 tracks vs 113, 42 vs 42) before moving anything.

**Corpus re-pricing audit** (`N:\bandcampfree\audit-free-download.ps1`, one-off 2026-08-13):
**`--full-scan` does NOT bypass the state cache.** `freesync.scan_label` short-circuits on
any entry cached not-free within `RECHECK_DAYS = 90` without re-fetching (**except entries
with `num_tracks == 0`, which get `EMPTY_RECHECK_HOURS = 12` since 2026-09-03** — an empty page
is not a price, and PRF publishes each month's comp empty), so re-pricing means
dropping those entries first — `N:\bandcampfree\invalidate_priced_cache.py` drops the
not-free-with-price>0 ones (12,611 of 20,057) and leaves price-0 entries alone. Budget more
than `ExecutionTimeLimit PT8H`: the run re-priced 19,631 items and was killed at the limit
while writing its report, losing the report but not the results. It fills `state.pending`,
which the 06:00 sweep then drains unattended — review that queue before the next 06:00.

## Fuzzy Cracklins Blog Downloader (`bandcampblog`)

A third tool. The monthly post at `fuzzycracklins.substack.com` lists albums that are
free or name-your-price; this fetches them and queues them for a Plex collection. It is
neither of the other two: the items are not purchases (so the collection sync cannot see
them) and they belong to no label (so `bandcampfree`, which is keyed by label, cannot
either). Everything except discovery is shared code.

```bash
# --report changes nothing: no downloads, no state written.
uv run python bin/bandcampblog \
  -C N:/bandcampfree/labels.yaml -d "N:/Bandcamp (FLAC)" \
  -s N:/bandcampfree/bandcampblog-state.json \
  --client-secret N:/bandcampfree/client_secret.json --token N:/bandcampfree/token.json \
  -t N:/_bcf_temp --report "https://fuzzycracklins.substack.com/p/best-free-music-september-2026"

# Then the same line without --report. --no-plex to skip the collection step;
# --plex-only (no URL) to drain the queue later, once Plex has scanned.
```
**Pass all four `N:` flags**, exactly as for `bandcampfree` — config, state, client secret
AND token. Missing the last two silently defaults them to `C:\Users\erik\.bandcampfree\`
and every item dies with "Gmail client secret not found" *after* its `/email_download`
POST has already fired, so the inbox fills with links for a run that downloaded nothing.

**Non-obvious details, all verified against the live September 2026 post:**

- **Extraction is exact, not fuzzy.** Substack wraps each embed in
  `<div data-attrs="{...}" data-component-name="BandcampToDOM">`, whose oEmbed JSON holds
  the canonical URL, artist, title and - inside `embed_url` as `album=4123378065` - the
  numeric **item id**. No album page is scraped and no title is matched back to an id.
  The post body is emitted twice (HTML and a JSON blob), so every embed is seen at least
  twice; `item_id` de-duplicates. 18/18 items extracted.
- **oEmbed `title` is `"<Album>, by <Artist>"`.** Left as-is it poisons every name
  comparison and the Plex title search, which look for the bare title - dedup finds
  nothing and re-downloads albums already owned. Worse, **the suffix is not always the
  `author` field**: Nosferator's release carries a Latin author (`Nosferator`) and a
  Cyrillic suffix (`by Носфератор`), so an exact-author strip alone leaves it attached.
  `_strip_by_suffix` tries the exact suffix, then the last `", by "`, and keeps the
  original if either would leave the title empty.
- **`tralbum_details` answers HTTP 200 with `{"error": true}` for a bad band_id**, and
  `album_from_details` turns that into `price=0.0/num_tracks=0`, which reads as NOT FREE.
  So a lookup failure is indistinguishable from "now paid" unless `details.get("error")`
  is checked explicitly. Since the paid list is the entire point of the report, the tool
  keeps a third bucket - `Could not check` - and never folds errors into `No longer free`.
- **Pricing goes through `album_from_details(...).is_free`**, the same predicate
  `bandcampfree` uses, so the `free_download` / `num_tracks` fixes are inherited rather
  than re-implemented. Cost is 2 requests per album (one `/music` fetch for the band id,
  cached per artist; one `tralbum_details`).
- **Dedup is three independent layers, because none suffices alone.** `bandcamp_item_id.txt`
  is exact but only 3,083 of the 11,939 indexed dirs carry one (the rest are hand-
  downloaded back catalogue and label grouping dirs); directory names are lenient but
  miss re-orderings and date suffixes; **Plex is the naming-independent check**, since it
  indexes tags rather than paths and so sees an album whatever its folder is called and
  wherever it sits. `LocalIndex` is a depth-2 `iterdir` pass (~2.5 min, 11,939 dirs) -
  never walk the whole tree.
- **"Already on disk" means skip the download, NOT skip the item.** It is still queued
  for the collection: an album you already own is exactly the case where nothing else
  would ever add it.
- **New albums land at the ROOT** as `Artist - Album`, matching where 349 of the
  collection's 409 members already live, via `resolve_and_download(root_level=True)`.
  That flag exists because `clean_label_dir_name("")` returns `"unknown-label"`, not `""`
  - do NOT widen `clean_label_dir_name` to fix this, every bandcampfree label directory
  is named by it. A `Fuzzy Cracklins\` subdirectory was deliberately rejected: it would
  create a second place for duplicates to hide from the root-level check.

**Plex collection `472199` ("Fuzzy Cracklins", static, subtype=album).** Static matters -
pushing a static item list into a smart collection yields `subtype=unknown` and crashes
Plex Web. Credentials come from `plex-mcp-server\.env` (`PLEX_URL`/`PLEX_TOKEN`), one
place to rotate. Section 1 is Music.
**`folder_collection_sync.py` cannot maintain this one**, unlike the other seven jobs: its
members are scattered - 349 at the media root and 60 across ~15 label subdirectories - so
no single folder contains the collection, and it has been curated by hand. Membership is
therefore resolved per album and PUT by ratingKey. **One dead ratingKey makes Plex answer
the whole PUT with HTTP 400, adding nothing**, hence small batches.
**The collection step is a QUEUE, deliberately.** An album has no ratingKey until Plex has
scanned it, and this tool does not drive Plex's scanner; downloads land in
`state.pending`, and a later run adds whatever Plex has since caught up on. Anything not
yet scanned simply stays queued. Same shape as `freesync`'s `state.pending`.

**Runs to date.** September post: 18/18, 5.24 GB, 12 min, 0 failures, 87 FLAC files.
August post: 21 items - 14 downloaded (3.59 GB), 4 already on disk, **3 had gone paid**
($1, $2, $9). Every track count matched the post's own figures both times. Estimate
~40 s/album when Gmail has to resolve a link.

**Posts overlap, so cross-month dedup is load-bearing.** `The Slow Blade / There Is No
Other Shore` appears in BOTH the August and September posts; running them an hour apart,
the id file written by the first run is what stopped the second re-downloading it. Two
more August items matched inside *label* subdirectories (`RumbleMusicPromotions\`,
`Weedian\`) rather than at the root - which is why the local index has to descend a
level rather than just listing the root.

**Plex's tags routinely disagree with bandcamp, so `find_album` cannot just compare
names.** Weedian's June compilation is `WEEDIAN / The Best Releases of June 2026` on
bandcamp and `Various Artists / Weedian: The Best Releases of June 2026` in Plex - a
label prefix on the title and a VA credit for the artist, so neither field matches.
Matching therefore goes strongest-evidence first: **the candidate's own file path against
the directory we downloaded to** (decisive, and immune to retagging - hence
`LOCAL_MEDIA_ROOT`/`PLEX_MEDIA_ROOT`, since `N:` and `/share/Music` are the same files),
then exact artist+title, then a *unique* candidate whose title contains ours or vice
versa. Requiring uniqueness in that last step is what keeps it safe.

**A standalone track can never join this collection.** Collection 472199 is
`subtype=album`, and a `track`-type release has no album row in Plex at all - a title
search returns 0 hits forever, not "not scanned yet". `November Fire / Doom Blues` (the
one `item_type: "t"` in the August post) is the worked example. Such items are retired to
`state.skipped` with a reason rather than retried on every run, the same convention
[[retiring-a-release-needs-state-skipped]] uses on the bandcampfree side. **So an
unresolved item means one of two quite different things** - not yet scanned, or never
addable - and only the queue distinguishes them.


## Key Details

- Python 3.10+ required (`.python-version` specifies 3.10)
- Only two runtime dependencies: `beautifulsoup4` and `curl-cffi`
- `Syncer.__init__` performs all work (sync + notify) — it's not a two-step init-then-run pattern
- Concurrency uses `asyncio.run()` with `run_in_executor` for blocking I/O
