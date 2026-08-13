---
name: assess-bandcamp-label
description: Assess a record label's bandcamp page for adding to bandcampfree's labels.yaml - resolve the right band, find what is actually free, see how compilations are credited, pick a match rule that is not silently dead, then add and scan it. Use whenever Erik proposes a new label or a batch of labels.
---

# Assessing a new bandcampfree label

The recurring task. Erik gives a label name and/or URL; the job is to decide whether it is
worth adding, and — the part that actually goes wrong — which `match:` rule selects its free
compilations. A rule that matches nothing is **silent**: it looks exactly like "this label
posted nothing free this week", potentially for years.

Everything here writes nothing until step 5. Run it beside a sweep safely.

## Paths (all four flags required, or they default back to C:)

```
CFG="-C N:/bandcampfree/labels.yaml -s N:/bandcampfree/state.json \
     --client-secret N:/bandcampfree/client_secret.json --token N:/bandcampfree/token.json"
```
Media root `N:/Bandcamp (FLAC)`; temp `-t N:/_bcf_temp` for any download.
`PYTHONUTF8=1` on anything that prints album titles — the console is cp1252 and titles are not.

## Step 1 — assess

```bash
cd C:/projects/bandcampsync
PYTHONUTF8=1 uv run python .claude/skills/assess-bandcamp-label/assess_label.py \
  "Thumper Punk Records" --url https://thumperpunkrecords.bandcamp.com/ \
  --cache "<scratchpad>/thumper.json"
```

Give `--url` when Erik supplied one. Without it the script searches and takes the top hit —
`is_label` is unreliable and **two bands can answer to one name** (Forward Music resolved to a
Canadian indie for weeks while the intended prog-house label went unfetched), so confirm the
printed URL against what he gave you before trusting it.

`--cache` is resumable: a 429 or a Ctrl-C costs nothing on re-run. Use `--max 60` for a first
look at a catalogue over ~200 releases (a full pass is one request per release at 1.5s).

The output answers, in order:

- **CREDIT STRINGS** — every distinct album artist with a count, and whether the real
  `VARIOUS_ARTISTS_REGEX` accepts it. This is the answer to "how are the comps labelled".
  The regex takes `Various Artists`/`Various Artist`, `V/A`, `V.A.`, `Varios/Vários Artistas`,
  `VV.AA.`, `AA.VV.`, each with optional trailing `!`/`.` — but **not** a bare `Various` or a
  bare `VA`, both plausible band names. Read the actual strings; a label mixes spellings
  release to release (South America Avenue wrote `Varios Artistas` on exactly one entry of a
  series and that was the free one).
- **FREE** — a full offline pricing pass, classified with `album_from_details(...).is_free`,
  **not** `price == 0.0`. Bandcamp reports `price: None` both for genuinely free instant
  downloads and for listings with nothing to download; `probe_labels.py`/`deepcheck.py` print
  both as `paid` and will make you skip a label that has free comps (Sludgelord, 2 of 14).
  A **nonzero** price can be free too: `price` is really `minimum_price` and covers physical
  copies, so a sold-out record can put €9 on a release whose download is free
  (`free_download: True`, i.e. `download_pref == 1`). `_digital_price` normalises that.
  Titles that look compilation-ish are flagged `COMP?` — that flag is advisory output only,
  nothing is selected by it.
- **CANDIDATE RULES** — each candidate run through the real `prefilter()` + `matches()` over
  the free releases, with what it would select and a `DEAD` marker if that is nothing. Add
  your own with `--rule '{"min_tracks": 9}'`. This is the step that prevents the dead-rule
  class of bug at the point of adding rather than an audit months later.
- **LOCAL** — whether the label dir already exists and what is in it. Erik's collection is
  huge; most free comps are already on disk. Never quote a download estimate without this.

## Step 2 — choose the rule

Rules live in `labelconfig.py`, are **all ANDed**, there is no OR and no `artist_regex`.
Only `various_artists` and `title_regex` **prefilter** (evaluated from the one cheap
`band_details` call) — everything else costs a request per release per scan, fine under
~150 releases.

| situation | rule |
|---|---|
| comps credited `Various Artists` (or an accepted variant), consistently | `various_artists: true` |
| comps are a named series (`Group Exhibition`, a sampler line) | `title_regex: "(?i)group exhibition"` |
| big multi-artist comps with real per-track artists | `min_track_artists: 5, min_tracks: 8` |
| small comps/splits, 2-4 artists | `track_artists_vary: true` |
| comps credited **both** ways, or label-credited so `distinct == 1` | `min_tracks: N`, N from the gap in the FREE listing |
| every release is a comp, or nothing separates them | `{}` |

Decision notes worth re-reading:

- **`various_artists: true` is worse than useless on a both-ways label.** It prefilters, so it
  silently keeps the wrong half — at Sludgelord it would have kept only the one £7 V/A comp
  and hidden both free label-credited ones. If the CREDIT STRINGS block shows comps under two
  spellings, do not use it.
- **Label-credited comps usually carry no per-track artists at all** (`distinct == 1`), so
  `min_track_artists` and `track_artists_vary` reject them for a second, independent reason.
- **`min_tracks` is the fallback that works** when credit and title both fail: read the FREE
  listing (sorted by track count) for a gap between the comps and the free single-artist
  releases. Centripetal: comps 18/12 vs a 6-track album → `min_tracks: 8`. Sludgelord: 47 free
  albums topping out at 12, comps 17+ → `min_tracks: 13`.
- **Anchor short `title_regex` alternatives** — a bare `dark` matched a solo EP `N3.0_Dark`;
  `dark4` is the series token. In YAML single-quote any regex with backslashes.
- **`min_track_artists` false-positives on crew albums** where each track credits a different
  permutation of the same 3-4 people (distinct=11 off a 3-person roster). Inspect the per-track
  credits before downloading.
- **"free download" / "(Free Sampler)" in a title does not mean free.** Gate on price, always.
- **A label with no comps is still a legitimate add** as a watcher (`{}` or min rules) — Erik
  chose that for Synth-Me and Digital Recovery. Offer it rather than just "skip".

Scope check: unless Erik says otherwise, the target is **compilations only**, not every free
release. He said exactly that for Sludgelord's other 47.

## Step 3 — report to Erik before adding

Show catalogue size, the free count, the comp titles with their credits and track counts, the
rule you propose and what it selects. Flag anything questionable — wrong page, duplicate label
page, all-comps-priced, a single-artist label. He decides.

## Step 4 — add to labels.yaml

Back it up first: `cp N:/bandcampfree/labels.yaml N:/bandcampfree/labels.yaml.bak-$(date +%Y%m%d)-<slug>`.
Append at the end of `labels:` with a comment explaining the catalogue and why that rule:

```yaml
  # 176 releases, 150 of them free (christian punk, mostly single-artist). Comps are
  # credited both ways: "Standing United with Tina" Vol 1/2 and "turns 10" as Various
  # Artists, "HXCChristian.com Benefit Comp" and "Free Sampler (Recent Releases)" as
  # the label. min_track_artists: 5 catches all of those and excludes the many 2-3
  # artist splits and the 140-odd free solo releases.
  - name: Thumper Punk Records
    url: https://thumperpunkrecords.bandcamp.com/
    band_id: 843651518
    match:
      min_track_artists: 5
    enabled: true
```

`band_id` is not optional in practice — pin it so the label can never re-resolve elsewhere.
Keep `name:` **path-safe** and in the cleaned form (`GIVE/TAKE` → `GIVETAKE`): `LabelIndex`
cleans the name but `freesync.find_duplicate_dirs` joins the **raw** one, so an illegal
character silently disables duplicate detection for that label forever.

Validate:
```bash
uv run python -c "from bandcampsync.labelconfig import load_config; c=load_config('N:/bandcampfree/labels.yaml'); print(len(c.labels))"
```

## Step 5 — scan, then download

```bash
uv run python bin/bandcampfree $CFG -t N:/_bcf_temp --report -l "Thumper Punk Records"
uv run python bin/bandcampfree $CFG -t N:/_bcf_temp --pending-only -l "Thumper Punk Records" --max-gb 10
```

- **`--report` writes state.** `scan_all()` inside it advances `newest_release_seen` and fills
  `state.pending`. That is what lets `--pending-only` download without rescanning.
- **Never run two bandcampfree processes at once** — both rewrite `state.json`. Wait for a
  download to finish before scanning the next label. Never probe concurrently either: the
  429 surfaces as `JSONDecodeError: Expecting value: line 1 column 1` and truncates output.
- Scope `--pending-only` with `-l` so it cannot drain a queue Erik has not approved.
- **If you change a rule on an existing label, follow with `--full-scan -l "<name>"`** — the
  releases the old rule missed sit behind the cutoff and a normal scan will not see them.
  Same after repointing a label's `url`/`band_id`: the repoint inherits the old cutoff.
- Runtime: **1-6 min per item**, Bandcamp-side variance. Quote a range from item count; GB and
  track count predict nothing.
- Transient `curl (56)` failures leave the item in `pending` — just re-run `--pending-only`.
- Clean up `N:\_bcf_temp` afterwards with `rm -rf` (PowerShell `Remove-Item` chokes on it).

## Fetching one album the permanent rule cannot reach

Rules are ANDed with no OR, so a label sometimes has one comp no rule can take alongside the
others (`Free Sampler (Classics)`: 10 tracks, zero per-track artists, title tokens that also
hit unrelated albums). Do **not** hand-roll a download — temporarily narrow the rule, let the
tool's own acquisition path run, then restore:

1. Set `match:` to a `title_regex` that hits only that album — single-quote it in YAML if it
   needs escaping: `title_regex: '(?i)free sampler \(classics\)'`.
2. `--full-scan --report -l "<label>"` — `--full-scan` is required (the album sits behind the
   cutoff), and the regex prefilters, so this costs one request, not a whole catalogue.
3. `--pending-only -l "<label>" --max-gb 5`.
4. Restore the permanent rule. No second `--full-scan` needed: the items the permanent rule
   wants were already fetched, and the downloaded album now has a `bandcamp_item_id.txt`.

Record the one-off in the label's config comment, or the next person will think the rule is
broken for missing it.

## After adding a batch

Re-run the dead-rule audit, which catches prefiltering rules that match 0 of N:
```bash
PYTHONUTF8=1 uv run python N:/bandcampfree/audit_prefilter.py
```
It writes nothing and takes ~7 min over 400+ labels. Step 1 makes this a backstop rather than
the first line of defence, but it still catches a rule that goes stale as a label's habits
change.

## Related tooling

- `N:/bandcampfree/probe_labels.py` — the older quick probe. Faster, but samples only the
  newest ~14 releases and misreports `price=None` as paid. Fine for a first glance at a batch.
- `N:/bandcampfree/triage_labels.py` — for a **bulk unvetted list** (30+ scraped names), 2
  requests per label. Expect near-zero yield; a 66-label commercial post-punk list gave 1.
- `N:/bandcampfree/deepcheck.py` — retries with backoff; the recovery tool during an active
  rate limit. Takes `"Name=BAND_ID"`.
