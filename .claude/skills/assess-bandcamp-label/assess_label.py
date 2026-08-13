"""Assess one bandcamp label for addition to bandcampfree's labels.yaml.

One command that answers the three questions that decide whether a label is worth adding
and which match rule it needs:

  1. Is the page the right band?              (search candidates + catalogue size)
  2. Is anything actually free?               (full offline pricing pass)
  3. How are the compilations credited?       (every distinct credit string, and whether
                                               VARIOUS_ARTISTS_REGEX accepts it)

and then evaluates candidate match rules against the real data, so a rule that would
match nothing is caught here instead of looking like "nothing free this week" for months.

Writes NOTHING - no state.json, no config, no downloads. Safe to run beside a sweep.

    cd C:/projects/bandcampsync
    PYTHONUTF8=1 uv run python .claude/skills/assess-bandcamp-label/assess_label.py \
        "Thumper Punk Records" --url https://thumperpunkrecords.bandcamp.com/ \
        --cache "$SCRATCH/thumper.json"

Key correctness note: free/paid is decided by labels.album_from_details(...).is_free, NOT
by `price == 0.0`. Bandcamp reports price=None both for genuinely free instant downloads
(download_pref == 1) and for listings with nothing to download; probe_labels.py and
deepcheck.py print both as "paid" and will make you skip a label that has free comps.
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from curl_cffi import requests

from bandcampsync.labelconfig import (
    VARIOUS_ARTISTS_REGEX,
    matches,
    prefilter,
)
from bandcampsync.labels import (
    BandcampAPI,
    DiscoEntry,
    album_from_details,
    list_discography,
)
from bandcampsync.media import clean_label_dir_name

SEARCH = "https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic"

# Deliberately loose: this only decides which titles get FLAGGED as comp-ish in the
# output for a human to read. Nothing is selected or rejected by it.
COMP_RE = re.compile(
    r"(?i)compilation|sampler|\bcomp\b|\bv/?a\b|various|tribute|benefit|mixtape|"
    r"\bvol\.?\b|volume|split|presents|anniversary|collection|series|showcase"
)

CANDIDATE_RULES = [
    ("{}  (take every free release)", {}),
    ("various_artists: true", {"various_artists": True}),
    ("track_artists_vary: true", {"track_artists_vary": True}),
    ("min_track_artists: 5, min_tracks: 8", {"min_track_artists": 5, "min_tracks": 8}),
    ("min_tracks: 6", {"min_tracks": 6}),
    ("min_tracks: 8", {"min_tracks": 8}),
    ("min_tracks: 13", {"min_tracks": 13}),
]


def search_bands(session, text, limit=5):
    body = {
        "search_text": text,
        "search_filter": "b",
        "full_page": False,
        "fan_id": None,
    }
    response = session.post(SEARCH, json=body, timeout=60)
    response.raise_for_status()
    results = json.loads(response.text).get("auto", {}).get("results") or []
    return [
        {
            "name": r.get("name"),
            "url": r.get("item_url_root"),
            "band_id": r.get("id"),
            "is_label": r.get("is_label"),
            "loc": r.get("location"),
        }
        for r in results[:limit]
        if r.get("type") == "b"
    ]


def fetch(api, band_id, entries, cache_path, delay_note):
    """Price every entry, resuming from and appending to a JSON cache."""
    cache = {}
    if cache_path and Path(cache_path).is_file():
        cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
        print(f"  cache: {len(cache)} releases already priced in {cache_path}")
    todo = [e for e in entries if str(e.item_id) not in cache]
    if todo:
        print(f"  fetching {len(todo)} release(s){delay_note} ...", flush=True)
    for n, entry in enumerate(todo, 1):
        try:
            cache[str(entry.item_id)] = api.tralbum_details(
                band_id, entry.item_id, entry.item_type
            )
        except Exception as e:  # noqa: BLE001
            print(f"    ERR {entry.title!r}: {type(e).__name__} {e}", flush=True)
            continue
        if cache_path and n % 10 == 0:
            Path(cache_path).write_text(json.dumps(cache), encoding="utf-8")
        if n % 25 == 0:
            print(f"    .. {n}/{len(todo)}", flush=True)
    if cache_path:
        Path(cache_path).write_text(json.dumps(cache), encoding="utf-8")
    return cache


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "name", help="Label name, as it would be written in labels.yaml"
    )
    parser.add_argument("--url", help="Label URL, skips the search step")
    parser.add_argument(
        "--band-id", type=int, help="Known band_id, skips search entirely"
    )
    parser.add_argument(
        "--max",
        type=int,
        help="Price only the newest N releases instead of the whole catalogue",
    )
    parser.add_argument(
        "--delay", type=float, default=1.5, help="Seconds between requests"
    )
    parser.add_argument("--cache", help="JSON file to resume the pricing pass from")
    parser.add_argument(
        "--rule",
        action="append",
        default=[],
        help="Extra candidate rule to evaluate, as JSON: --rule '{\"min_tracks\": 9}'",
    )
    parser.add_argument(
        "--media-dir",
        default="N:/Bandcamp (FLAC)",
        help="Media root, to check what is already on disk",
    )
    args = parser.parse_args()

    api = BandcampAPI(delay=args.delay)
    session = requests.Session(impersonate="chrome")

    # --- 1. resolve the band ------------------------------------------------------
    print("=" * 78)
    print(f"LABEL: {args.name}")
    band_id = args.band_id
    if not band_id:
        if args.url:
            band_id = api.resolve_band_id(args.url)
            print(f"  resolved from {args.url} -> band_id={band_id}")
        else:
            candidates = search_bands(session, args.name)
            if not candidates:
                sys.exit(f"No search hit for {args.name!r}; retry with --url")
            for i, c in enumerate(candidates):
                tag = "TOP" if i == 0 else "   "
                print(
                    f"  [{tag}] {c['name']!r} is_label={c['is_label']} {c['url']} "
                    f"band_id={c['band_id']} loc={c['loc']}"
                )
            band_id = candidates[0]["band_id"]
            print(
                "  !! is_label is unreliable and two bands can share a name - confirm"
            )
            print("     the URL above is the page you were given before trusting this.")

    entries = list_discography(api, band_id)
    print(f"  band_id={band_id}  catalogue={len(entries)} releases")

    # --- 2. how are things credited? ----------------------------------------------
    print("\n-- CREDIT STRINGS (album artist, from the one cheap band_details call) --")
    credits = Counter(e.artist for e in entries)
    for credit, count in credits.most_common(15):
        va = "VA-regex MATCHES" if VARIOUS_ARTISTS_REGEX.match(credit or "") else ""
        print(f"  {count:4d}x {credit!r:45s} {va}")
    if len(credits) > 15:
        print(f"  ... and {len(credits) - 15} more distinct credit(s)")
    va_total = sum(
        c for a, c in credits.items() if VARIOUS_ARTISTS_REGEX.match(a or "")
    )
    print(
        f"  -> {va_total}/{len(entries)} releases would survive `various_artists: true`"
    )
    print("     (that rule PREFILTERS: anything it rejects is never even priced)")

    # --- 3. price everything -------------------------------------------------------
    targets = entries[: args.max] if args.max else entries
    est = len(targets) * args.delay / 60
    print(f"\n-- PRICING PASS: {len(targets)} release(s), ~{est:.0f} min --")
    if not args.max and len(entries) > 200:
        print("  (large catalogue - consider --max 60 for a first look)")
    details = fetch(api, band_id, targets, args.cache, f" at {args.delay}s apart")

    albums = {}
    for entry in targets:
        raw = details.get(str(entry.item_id))
        if raw:
            albums[entry.item_id] = album_from_details(raw, args.name)

    free = [
        (e, albums[e.item_id])
        for e in targets
        if e.item_id in albums and albums[e.item_id].is_free
    ]
    print(f"\n-- FREE: {len(free)} of {len(albums)} priced --")
    for entry, album in sorted(free, key=lambda p: -p[1].num_tracks):
        flag = "COMP?" if COMP_RE.search(album.title) else "     "
        print(
            f"  {flag} tracks={album.num_tracks:3d} distinct_artists="
            f"{len(album.distinct_track_artists):2d} {album.artist!r} :: {album.title!r}"
        )
    dud = [a for a in albums.values() if a.price == 0.0 and not a.has_digital_download]
    if dud:
        print(f"  ({len(dud)} price=None listing(s) with has_digital_download=False -")
        print("   vinyl-only or placeholder, correctly excluded, do not chase them)")

    # --- 4. would a rule actually work? --------------------------------------------
    print("\n-- CANDIDATE RULES, evaluated against the free releases above --")
    rules = list(CANDIDATE_RULES)
    for raw in args.rule:
        rules.append((f"custom {raw}", json.loads(raw)))
    for token, count in _title_tokens(free).most_common(6):
        rules.append((f'title_regex: "(?i){token}"', {"title_regex": f"(?i){token}"}))

    for label, rule in rules:
        selected = []
        for entry, album in free:
            disco = DiscoEntry(
                item_id=entry.item_id,
                item_type=entry.item_type,
                title=album.title,
                artist=album.artist,
            )
            ok, _ = prefilter(disco, rule)
            if ok:
                ok, _ = matches(album, rule)
            if ok:
                selected.append(album.title)
        mark = "  DEAD - matches nothing" if not selected else ""
        print(f"  {len(selected):3d}/{len(free)}  {label}{mark}")
        for title in selected[:6]:
            print(f"           {title!r}")
        if len(selected) > 6:
            print(f"           ... +{len(selected) - 6} more")

    # --- 5. what is already on disk ------------------------------------------------
    label_dir = Path(args.media_dir) / clean_label_dir_name(args.name)
    print(f"\n-- LOCAL: {label_dir}")
    if label_dir.is_dir():
        existing = sorted(p.name for p in label_dir.iterdir() if p.is_dir())
        print(f"  exists, {len(existing)} album dir(s); newest few:")
        for name in existing[-5:]:
            print(f"    {name}")
    else:
        print("  does not exist yet (nothing downloaded from this label)")
    if clean_label_dir_name(args.name) != args.name:
        print(
            f"  !! name is not path-safe; configure it as {clean_label_dir_name(args.name)!r}"
        )
        print(
            "     (freesync.find_duplicate_dirs joins the RAW name and dies silently)"
        )


def _title_tokens(free):
    """Words shared by several free comp-ish titles - title_regex candidates."""
    counts = Counter()
    for _, album in free:
        if not COMP_RE.search(album.title):
            continue
        for word in re.findall(r"[A-Za-z]{4,}", album.title.lower()):
            counts[word] += 1
    return Counter({w: c for w, c in counts.items() if c >= 2})


if __name__ == "__main__":
    main()
