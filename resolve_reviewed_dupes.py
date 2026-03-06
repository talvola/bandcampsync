"""Resolve duplicates identified in 'duplicate review.txt'."""

import io
import shutil
import os
import sys
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

base = Path(r"N:\Bandcamp (FLAC)")
IDFILE = "bandcamp_item_id.txt"


def move_tracking(src_dir, dst_dir):
    src_id = src_dir / IDFILE
    dst_id = dst_dir / IDFILE
    if src_id.exists() and not dst_id.exists():
        content = src_id.read_text().strip()
        dst_id.write_text(f"{content}\n")
        print(f"    Wrote tracking ID {content} to keep dir")
    elif src_id.exists() and dst_id.exists():
        print(f"    Both have tracking (keep:{dst_id.read_text().strip()}, remove:{src_id.read_text().strip()})")


JUNK_FILES = {"Thumbs.db", "desktop.ini", ".DS_Store"}


def cleanup_parent(path):
    parent = path.parent
    if parent != base:
        _remove_if_empty(parent)


def _remove_if_empty(d):
    """Remove directory if it only contains junk files (Thumbs.db etc.)."""
    if not d.exists():
        return
    try:
        remaining = [f for f in d.iterdir() if f.name not in JUNK_FILES]
        if not remaining:
            shutil.rmtree(str(d))
            print(f"    Removed parent: {d.name}")
    except OSError:
        pass


def _force_rmtree(path):
    """Remove directory tree, fixing permissions if needed."""
    import stat

    def on_error(func, fpath, exc_info):
        try:
            os.chmod(fpath, stat.S_IRWXU)
            func(fpath)
        except OSError:
            pass

    shutil.rmtree(str(path), onerror=on_error)


def resolve(keep_path, remove_path):
    keep = Path(keep_path)
    remove = Path(remove_path)
    if not keep.exists():
        print(f"  [SKIP] Keep dir not found: {keep}")
        return False
    if not remove.exists():
        print(f"  [SKIP] Remove dir not found: {remove}")
        return False
    move_tracking(remove, keep)
    _force_rmtree(remove)
    cleanup_parent(remove)
    print(f"  [OK] Kept: .../{keep.parent.name}/{keep.name}" if keep.parent != base else f"  [OK] Kept: {keep.name}")
    print(f"       Removed: .../{remove.parent.name}/{remove.name}" if remove.parent != base else f"       Removed: {remove.name}")
    return True


def find_dir(parent, *keywords):
    """Find a directory under parent containing all keywords."""
    for d in parent.iterdir():
        if d.is_dir() and all(k in d.name for k in keywords):
            return d
    return None


def find_dirs(parent, *keywords):
    """Find all directories under parent containing all keywords."""
    return [d for d in parent.iterdir() if d.is_dir() and all(k in d.name for k in keywords)]


def main():
    swamp = base / "The Swamp Records"
    alfa = base / "Alfa Matrix"
    at = base / "AnalogueTrash"
    upr = base / "UNKNOWN PLEASURES RECORDS"
    dt = base / "darkTunes"
    va = base / "VA"

    print("=== The Swamp Records duplicates ===\n")

    # A.J. Kaufmann pairs
    for d in list(base.iterdir()):
        if not d.is_dir() or d == swamp:
            continue
        if d.name.startswith("A.J. Kaufmann") and d.is_dir():
            has_subdirs = any(s.is_dir() for s in d.iterdir())
            if not has_subdirs:
                continue
            for sub in list(d.iterdir()):
                if sub.is_dir():
                    swamp_match = find_dir(swamp, *sub.name.split(" - ", 1))
                    if swamp_match:
                        resolve(swamp_match, sub)
            if d.exists():
                remaining = [f for f in d.iterdir() if f.name not in {"Thumbs.db", "desktop.ini", ".DS_Store"}]
                if not remaining:
                    shutil.rmtree(str(d))
                    print(f"    Removed parent: {d.name}")

    # Bog Wizard & Dust Lord
    for d in find_dirs(base, "Bog Wizard", "Dust Lord"):
        if d.parent == base:
            for sub in list(d.iterdir()):
                if sub.is_dir() and "Four Tales" in sub.name:
                    swamp_match = find_dir(swamp, "Bog Wizard", "Four Tales")
                    if swamp_match:
                        resolve(swamp_match, sub)
            _remove_if_empty(d)

    # Pink Elephant Music volumes
    for d in list(base.iterdir()):
        if "Pink Elephant Music Vol" in d.name and d.is_dir() and d.parent == base:
            d_digits = "".join(c for c in d.name if c.isdigit())
            for sw in swamp.iterdir():
                if "Pink Elephant Music Vol" in sw.name:
                    sw_digits = "".join(c for c in sw.name if c.isdigit())
                    if d_digits and sw_digits and d_digits == sw_digits:
                        resolve(sw, d)
                        break

    # MurderNotSuicide
    for d in list(base.iterdir()):
        if "MurderNotSuicide" in d.name and d.is_dir() and d.parent == base:
            for sub in list(d.iterdir()):
                if sub.is_dir() and "INTO THE BLACK" in sub.name:
                    swamp_match = find_dir(swamp, "MurderNotSuicide", "INTO THE BLACK")
                    if swamp_match:
                        resolve(swamp_match, sub)
            _remove_if_empty(d)

    # Green Hog Band
    for d in list(base.iterdir()):
        if "Green Hog Band" in d.name and "Crypt of Doom" in d.name and d.parent == base:
            swamp_match = find_dir(swamp, "Green Hog Band", "Crypt of Doom")
            if swamp_match:
                resolve(swamp_match, d)

    # Merlock
    for d in list(base.iterdir()):
        if "Merlock" in d.name and "that which speaks" in d.name and d.parent == base:
            swamp_match = find_dir(swamp, "Merlock", "that which speaks")
            if swamp_match:
                resolve(swamp_match, d)

    print("\n=== Alfa Matrix duplicates ===\n")

    # MATRIX REB00TED volumes
    for d in list(base.iterdir()):
        if "MATRIX" in d.name and "REB00TED" in d.name and d.is_dir() and d.parent == base:
            for key in ["SIMON CARTER", "PSY"]:
                if key in d.name:
                    for am in alfa.iterdir():
                        if key in am.name:
                            # More specific match for PSY'AVIAH volumes
                            if "Trip" in d.name and "Trip" not in am.name:
                                continue
                            if "Electro Dance" in d.name and "Electro Dance" not in am.name:
                                continue
                            if "Hard Dance" in d.name and "Hard Dance" not in am.name:
                                continue
                            resolve(am, d)
                            break
                    break

    # Alfa Matrix Re-covered volumes
    for d in list(base.iterdir()):
        if "Alfa Matrix Re-covered" in d.name and d.is_dir() and d.parent == base:
            if "Vol.2" in d.name or "Vol. 2" in d.name:
                am = find_dir(alfa, "Re-covered", "Vol. 2")
                if not am:
                    am = find_dir(alfa, "Re-covered", "Vol.2")
                if am:
                    resolve(am, d)
            elif "Vol.1" in d.name or ("Bonus Tracks Version)" in d.name and "Vol" not in d.name):
                # Vol 1 or the original (no vol number)
                for am in alfa.iterdir():
                    if "Re-covered" in am.name and "Vol. 2" not in am.name and "Vol.2" not in am.name:
                        resolve(am, d)
                        break

    print("\n=== AnalogueTrash ===\n")
    top_bliss = base / "This Bliss - Grave of Sound"
    label_bliss = at / "This Bliss - Grave of Sound"
    if top_bliss.exists() and label_bliss.exists():
        resolve(label_bliss, top_bliss)

    print("\n=== Brown Bear Records ===\n")
    bbr = base / "Brown Bear Records"
    for d in list(base.iterdir()):
        if "Primal Beast" in d.name and d.is_dir() and d.parent == base and d != bbr:
            for sub in list(d.iterdir()):
                if sub.is_dir() and "Jurassic Park" in sub.name:
                    br_match = find_dir(bbr, "Primal Beast", "Jurassic Park")
                    if br_match:
                        resolve(br_match, sub)
            _remove_if_empty(d)

    print("\n=== Collector's Series DIY ===\n")
    csd = base / "Collector's Series DIY"
    for d in list(base.iterdir()):
        if "Varios Artistas" in d.name and d.is_dir() and d.parent == base:
            for sub in list(d.iterdir()):
                if sub.is_dir() and "Discography complete" in sub.name:
                    cs_match = find_dir(csd, "Discography complete")
                    if cs_match:
                        resolve(cs_match, sub)
            _remove_if_empty(d)

    print("\n=== Amanda Palmer - apostrophe fix ===\n")
    # Find the one with apostrophe and the one without
    palmer_keep = None
    palmer_remove = None
    for d in base.iterdir():
        if "Amanda Palmer" in d.name and "Rhiannon" in d.name and "Fire" in d.name:
            if "It's" in d.name or "\u2019" in d.name:
                palmer_keep = d
            elif "Its" in d.name:
                palmer_remove = d
    if palmer_keep and palmer_remove:
        resolve(palmer_keep, palmer_remove)

    print("\n=== darkTunes ===\n")
    for d in list(base.iterdir()):
        if "Gothic Music Orgy" in d.name and d.is_dir() and d.parent == base:
            dt_match = find_dir(dt, "Gothic Music Orgy")
            if dt_match:
                resolve(dt_match, d)

    print("\n=== KICKING RECORDS vs VA ===\n")
    kr = base / "KICKING RECORDS"
    if va.exists() and kr.exists():
        for vad in list(va.iterdir()):
            if "CAFZIC" in vad.name:
                kr_match = find_dir(kr, "CAFZIC")
                if kr_match:
                    resolve(kr_match, vad)
    _remove_if_empty(va)

    print("\n=== UNKNOWN PLEASURES RECORDS ===\n")

    # CHRIS SHAPE + DAVE INOX
    for d in list(base.iterdir()):
        if "CHRIS SHAPE" in d.name and "Fake Truths" in d.name and d.parent == base:
            u = find_dir(upr, "CHRIS SHAPE", "Fake Truths")
            if u:
                resolve(u, d)

    # NOIR DESIR TRIBUTE
    for d in list(base.iterdir()):
        if d.name.startswith("NOIR DESIR") and d.is_dir() and d.parent == base:
            if " - " not in d.name:
                # It's the parent dir
                for sub in list(d.iterdir()):
                    if sub.is_dir() and "Filles" in sub.name:
                        u = find_dir(upr, "NOIR DESIR", "Filles")
                        if u:
                            resolve(u, sub)
                _remove_if_empty(d)

    # EUROPEAN GHOST
    for d in list(base.iterdir()):
        if "EUROPEAN GHOST" in d.name and "Collection Of Shadows" in d.name and d.parent == base:
            u = find_dir(upr, "EUROPEAN GHOST")
            if u:
                resolve(u, d)

    # FOLLOW ME NOT
    for d in list(base.iterdir()):
        if "FOLLOW ME NOT" in d.name and "If The Sky Remains" in d.name and d.parent == base:
            u = find_dir(upr, "FOLLOW ME NOT")
            if u:
                resolve(u, d)

    # HIV+ albums
    hiv_keys = [
        "Empire of Chaos",
        "Abstract & Harsh",
        "Anthology Of Noise",
        "Censored Frequencies",
        "Hypnoise Movement",
        "Interferencias",
        "Overdose Kill Me",
        "Rotten Beat",
    ]
    for d in list(base.iterdir()):
        if d.name.startswith("HIV+") and d.is_dir() and d.parent == base:
            for key in hiv_keys:
                if key in d.name:
                    u = find_dir(upr, "HIV+", key)
                    if u:
                        resolve(u, d)
                    break

    print("\n=== Depressive Illusions Records vs VA ===\n")
    di = base / "Depressive Illusions Records"
    if va.exists() and di.exists():
        for vad in list(va.iterdir()):
            if "KISS" in vad.name and "Black Diamond" in vad.name:
                di_match = find_dir(di, "Black Diamond", "KISS")
                if di_match:
                    resolve(di_match, vad)
    _remove_if_empty(va)

    print("\n=== Cosa Magnetica ===\n")
    cm = base / "Cosa Magnetica"
    for d in list(base.iterdir()):
        if "Olivier" in d.name and "Julie Rass" in d.name and d.is_dir() and d != cm:
            for sub in list(d.iterdir()):
                if sub.is_dir() and "Honey" in sub.name:
                    cm_match = find_dir(cm, "Honey")
                    if cm_match:
                        resolve(cm_match, sub)
            _remove_if_empty(d)

    print("\n=== Lycia - Bleak Vane ===\n")
    lycia_keep = None
    lycia_remove = None
    for d in base.iterdir():
        if "Lycia" in d.name and "Bleak" in d.name and "Vane" in d.name:
            if "~" in d.name:
                lycia_keep = d
            else:
                lycia_remove = d
    if lycia_keep and lycia_remove:
        resolve(lycia_keep, lycia_remove)

    print("\n=== Mellow Beast - pre-order ===\n")
    mellow_keep = None
    mellow_remove = None
    for d in base.iterdir():
        if "Mellow Beast" in d.name and "Grimble" in d.name:
            if "pre-order" in d.name:
                mellow_remove = d
            else:
                mellow_keep = d
    if mellow_keep and mellow_remove:
        resolve(mellow_keep, mellow_remove)

    print("\n=== Scortor - typo fix ===\n")
    scortor_keep = None
    scortor_remove = None
    for d in base.iterdir():
        if "Scortor" in d.name and "Moist Tales" in d.name:
            if d.name.endswith("Kingdom"):
                scortor_keep = d
            elif d.name.endswith("Kingdo"):
                scortor_remove = d
    if scortor_keep and scortor_remove:
        resolve(scortor_keep, scortor_remove)

    print("\n=== The Content Label ===\n")
    tcl = base / "The Content Label"
    for d in list(base.iterdir()):
        if "Content L" in d.name and "Sampler 5" in d.name and d.is_dir() and d.parent == base:
            tc_match = find_dir(tcl, "Sampler 5")
            if tc_match:
                resolve(tc_match, d)

    print("\n=== Done ===")


if __name__ == "__main__":
    main()
