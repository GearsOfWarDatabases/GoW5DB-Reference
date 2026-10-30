#!/usr/bin/env python3
"""
sort-screenshots.py - multi-section Gears 5 screenshot sorter.

Sections it recognises and auto-routes (see SECTIONS at the bottom):
  - Class skill unlocks   ("SKILL UNLOCKS" level-up screen)      -> Classes - ... - A - Card Unlock Levels
  - Class skill cards     (grid + side detail panel)             -> Classes - ... - B - Card Upgrades
  - Class perks           (in-Horde D-pad overlay)                -> Class Perks
  - Fabricator            (in-world "NAME LVn UPGRADE: price")    -> Fabricator
  - Store bundle item     (single-item preview, "ENTER PURCHASE") -> Bundles - NN - <bundle name>
  - Store bundle list     (STORE > BUNDLES list, red-glow select) -> Store Pages
  - Difficulty modifiers  ("SELECT DIFFICULTY" screen)            -> Difficulty
  - Characters            ("CHARACTERS" select screen)            -> Characters (always manual review -
                                                                     no name is ever printed on screen)
  - Squad & Honor panel   (selected player's "HONOR X / Y" readout) -> Squad & Honor

How identification works, in general:
  1. Try each section's detector in turn (most distinctive headers first).
  2. A section match returns enough info to build a descriptive filename;
     if only PART of the info is readable, whatever's missing is left off
     the filename rather than guessed.
  3. Anything no section can identify is left in place and logged to the
     CSV ledger as "needs_review" so a person can sort it by hand - and
     once it's logged, it will never be re-picked-up or reprocessed by
     future runs (the point of the hash ledger, see below).

Hash ledger (_sort-log.csv):
  Every file this script ever touches (sorted, imported, or flagged for
  review) gets a row keyed on its SHA-256 hash: hash, original_name,
  section, status, final_path, timestamp. On every run the ledger is
  loaded first and any incoming file whose hash is already present is
  skipped outright - this is what makes it safe to re-run this script
  (or re-import the same Steam screenshots folder) over and over without
  ever double-processing or duplicating a file. "needs_review" rows are
  NOT retried automatically even if you later improve a detector - once
  you've sorted a flagged file by hand, its ledger row is what remembers
  that it's been dealt with, permanently.

Steam screenshot import:
  Run with no flags (or explicitly --import) and, before sorting, the
  script copies (never moves, never deletes) any screenshot from
        C:\\Program Files (x86)\\Steam\\userdata\\110099628\\760\\remote\\1097840\\screenshots
  that isn't already in the ledger by hash into this ref/ folder, ready
  to be sorted in the same run. Nothing is ever read from or written to
  any other location on your machine. Pass --no-import to skip this step
  entirely (e.g. if that folder doesn't exist on the machine you're on).

Usage:
  python sort-screenshots.py                # import from Steam, then auto-sort everything loose in ref/
  python sort-screenshots.py --no-import     # skip the Steam import step, just sort what's already here
  python sort-screenshots.py --dry-run       # preview only - copies/moves/writes nothing, ledger untouched
  python sort-screenshots.py --workers 8     # classify N images concurrently (default: CPU count, max 16)
  python sort-screenshots.py --workers 1     # classify one at a time (old single-threaded behaviour)

Concurrency: each image's classification (the OCR-heavy part - up to 60-100+
individual Tesseract calls for one that matches nothing) runs in a thread
pool, since pytesseract shells out to tesseract.exe as a subprocess and
blocks waiting for it - that wait releases Python's GIL, so multiple
images' OCR really does run in parallel across CPU cores, not just
interleave. File moves and CSV ledger writes still happen one at a time
back on the main thread as each result comes in, so there's no risk of
two threads racing to rename into the same target path or writing to the
CSV at the same time.

Requirements (once):
  pip install pillow pytesseract
  + Tesseract OCR: https://github.com/UB-Mannheim/tesseract/wiki
"""

import atexit
import csv
import hashlib
import os
import re
import shutil
import sys
import threading
import time
import difflib
import concurrent.futures
import concurrent.futures.thread
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageChops, ImageOps, ImageStat
import pytesseract

# ThreadPoolExecutor registers an atexit hook that blocks interpreter
# shutdown until every worker thread finishes - which makes Ctrl+C look
# like it does nothing when there's a big backlog of queued images, since
# Python won't actually unwind until the whole pool drains. Unregistering
# it (and, in main(), avoiding the `with ThreadPoolExecutor(...)` form -
# its __exit__ calls shutdown(wait=True), which has the same blocking
# problem) makes Ctrl+C responsive again.
atexit.unregister(concurrent.futures.thread._python_exit)

# If tesseract.exe is not on your PATH, point to it here, e.g.:
# TESSERACT_EXE = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
TESSERACT_EXE = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if TESSERACT_EXE and not Path(TESSERACT_EXE).exists():
    # Falls back to whatever `tesseract` resolves to on PATH - keeps this
    # script runnable as-is on a non-Windows box (e.g. testing in a Linux
    # sandbox) without having to touch this machine-specific setting.
    _on_path = shutil.which("tesseract")
    if _on_path:
        TESSERACT_EXE = _on_path
if TESSERACT_EXE:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_EXE

REF = Path(__file__).resolve().parent
CLASSES_DIR = REF.parent / "_classes"
FABRICATOR_DATA = REF.parent / "_data" / "fabricator.yml"
MAPS_DATA = REF.parent / "_data" / "maps.yml"
MAPS_IMAGES_DIR = REF.parent / "images" / "maps"
LEDGER_PATH = REF / "_sort-log.csv"
LEDGER_FIELDS = ["hash", "original_name", "section", "status", "final_path", "timestamp"]

# A manually-created catch-all bucket (not something this script ever made
# on its own) that needs_review screenshots have historically been swept
# into by hand once they piled up loose in ref/ - its name says "not sorted
# yet", not "sorted here on purpose", so it's treated as a second source of
# not-yet-classified images alongside ref/'s own loose top level, NOT as
# just another already-sorted subfolder the way reconcile_sorted_files()
# treats everything else under ref/. See main()'s images list and
# reconcile_sorted_files()'s own exclusion check below for where this
# actually matters.
UNSORTED_DIR = REF / "Unsorted - Old Screenshots"

# The ONLY place screenshots are ever imported from. Never touched/deleted -
# only ever read and copied.
STEAM_SCREENSHOTS_DIR = Path(r"C:\Program Files (x86)\Steam\userdata\110099628\760\remote\1097840\screenshots")

CLASSES = {
    "BLADEMASTER":     ("Classes - 01 - Assault - 01 - Blademaster",          "blademaster.md"),
    "DEMOLITIONS":     ("Classes - 01 - Assault - 02 - Demolitions",          "demolitions.md"),
    "INFILTRATOR":     ("Classes - 01 - Assault - 03 - Infiltrator",          "infiltrator.md"),
    "MARKSMAN":        ("Classes - 01 - Assault - 04 - Marksman",             "marksman.md"),
    "NOMAD":           ("Classes - 01 - Assault - 05 - Nomad",                "nomad.md"),
    "ANCHOR":          ("Classes - 02 - Tank - 01 - Anchor",                  "anchor.md"),
    "BRAWLER":         ("Classes - 02 - Tank - 02 - Brawler",                 "brawler.md"),
    "GUNNER":          ("Classes - 02 - Tank - 03 - Gunner",                  "gunner.md"),
    "PILOT":           ("Classes - 02 - Tank - 04 - Pilot",                   "pilot.md"),
    "VETERAN":         ("Classes - 02 - Tank - 05 - Veteran",                 "veteran.md"),
    "COMBAT MEDIC":    ("Classes - 03 - Support - 01 - Combat Medic",         "combat-medic.md"),
    "JACK":            ("Classes - 03 - Support - 02 - Jack",                 "jack.md"),
    "MECHANIC":        ("Classes - 03 - Support - 03 - Mechanic",             "engineer.md"),
    "ROBOTICS EXPERT": ("Classes - 03 - Support - 04 - Robotics Expert",      "scout.md"),
    "TACTICIAN":       ("Classes - 03 - Support - 05 - Tactician",            "tactician.md"),
    "ARCHITECT":       ("Classes - 04 - Promotional - 01 - Architect",        "architect.md"),
    "PROTECTOR":       ("Classes - 04 - Promotional - 02 - Protector",        "protector.md"),
    "SLUGGER":         ("Classes - 04 - Promotional - 03 - Slugger",          "slugger.md"),
    "STRIKER":         ("Classes - 04 - Promotional - 04 - Striker",          "striker.md"),
}
ALT_FILES = {"engineer.md": "mechanic.md", "scout.md": "robotics-expert.md"}

# grid geometry (1920x1080 screenshots, normalised): 4 columns x 2 rows
GRID_COLS = [(0.038, 0.175), (0.188, 0.325), (0.338, 0.475), (0.488, 0.625)]
GRID_ROWS = [(0.15, 0.47), (0.49, 0.82)]

_card_cache = {}
_perk_cache = {}
_fab_names_cache = None
_map_names_cache = None

# Assigns each worker thread a stable, small display number (1..workers) the
# first time it picks up a task, purely for the "[wN]" tag on progress lines -
# ThreadPoolExecutor reuses the same fixed pool of threads for every task, so
# a thread's number stays the same all run. Threads only ever add their own
# entry (never read another's), so the lock is just to make the read-modify-
# write of "how many do we have so far" atomic - it's not protecting against
# any real contention.
_worker_ids = {}
_worker_ids_lock = threading.Lock()


def _worker_label():
    ident = threading.get_ident()
    with _worker_ids_lock:
        if ident not in _worker_ids:
            _worker_ids[ident] = len(_worker_ids) + 1
        return _worker_ids[ident]


# ─────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────

def ocr(img, threshold=None, psm=6, whitelist=None, scale=1):
    if scale != 1:
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    g = ImageOps.grayscale(img)
    if threshold:
        g = g.point(lambda p: 255 if p > threshold else 0)
    cfg = f"--psm {psm}"
    if whitelist:
        cfg += f" -c tessedit_char_whitelist={whitelist}"
    return pytesseract.image_to_string(g, config=cfg)


def crop_frac(im, box):
    """box = (x0, y0, x1, y1) as fractions of the image size."""
    w, h = im.size
    return im.crop((int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h)))


def best_digits(text):
    """Longest run of digits/apostrophes/commas in text, cleaned to a plain int string."""
    runs = re.findall(r"[\d][\d',]*\d|\d", text)
    if not runs:
        return None
    longest = max(runs, key=len)
    return re.sub(r"[^\d]", "", longest) or None


def slugify(name):
    """Mirrors Jekyll's `| slugify` filter closely enough to match the
    images/maps/<slug>.jpg naming convention (see _data/maps.yml's own
    header comment): lowercase, non-alphanumeric runs collapsed to a
    single hyphen, no leading/trailing hyphen. "Blood Drive" -> "blood-drive"."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# Windows forbids <>:"/\|?* and control chars anywhere in a filename - this
# is exactly what crashed a real run (OSError WinError 123) on a mark
# literally named "Competitive: Legend Mark", since build_marks joins OCR'd
# mark names straight into the target filename with no sanitizing in between.
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name):
    """Strips characters Windows won't allow in a filename rather than
    trying to substitute something meaningful per-character (a colon in a
    mark's own name isn't information worth preserving in the filename
    anyway). Also collapses any doubled-up spacing the stripping leaves
    behind and trims stray trailing dots/spaces (also invalid at the end of
    a Windows filename)."""
    cleaned = _INVALID_FILENAME_CHARS.sub("", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "unnamed"


def unique_target(folder, base, ext):
    base = sanitize_filename(base)
    tgt = folder / f"{base}{ext}"
    n = 1
    while tgt.exists():
        n += 1
        tgt = folder / f"{base}_{n}{ext}"
    return tgt


# ─────────────────────────────────────────────────────────────────────────
# Class skill cards (existing, proven logic)
# ─────────────────────────────────────────────────────────────────────────

def card_names_for(cls):
    if cls in _card_cache:
        return _card_cache[cls]
    fname = CLASSES[cls][1]
    path = CLASSES_DIR / fname
    if not path.exists():
        path = CLASSES_DIR / ALT_FILES.get(fname, fname)
    names = []
    if path.exists():
        names = [n.strip() for n in
                 re.findall(r"^- name: (.+)$", path.read_text(encoding="utf-8"), re.M)]
    _card_cache[cls] = names
    return names


def distinctive_names():
    """Card names that appear in at most 2 classes (Score Boost etc. excluded)."""
    counts = {}
    for cls in CLASSES:
        for n in card_names_for(cls):
            counts[n] = counts.get(n, 0) + 1
    return {cls: [n for n in card_names_for(cls) if counts[n] <= 2]
            for cls in CLASSES}


def fuzzy_class_match(run, ratio=0.72, min_frac=0.75):
    """Fuzzy-match an OCR'd run of letters against CLASSES, guarding against
    the exact failure mode found while testing against a Horde end-of-wave
    scoreboard: a short 4-letter fragment ("ETER") is a literal contiguous
    substring of a much longer class name ("VETERAN"), so its difflib ratio
    (0.73) clears a 0.72 threshold despite being mostly garbage context.
    Real on-screen class names are always printed in full, never truncated,
    so requiring the run to cover most (75%) of the candidate's length
    blocks short-fragment false positives without losing genuine matches."""
    run = run.strip()
    if not run:
        return None
    best = difflib.get_close_matches(run, CLASSES.keys(), 1, ratio)
    if best and len(run) >= min_frac * len(best[0]):
        return best[0]
    return None



def detect_class_header(im, extra_headers=()):
    """OCR the top-of-screen header and return the class name if found, plus
    whether one of extra_headers (e.g. 'SKILL UNLOCKS') was also seen.

    Extra-header boxes are scanned in a separate pass from the class-name
    boxes: the class scan returns as soon as it finds a name, and if it
    were the same pass an early class match (found in an earlier box)
    would skip the boxes further down the screen where an extra header
    like "SKILL UNLOCKS" actually lives - silently losing it."""
    w, h = im.size
    found_extra = set()

    extra_boxes = [(0.03, 0.32, 0.35, 0.42), (0.04, 0.35, 0.30, 0.40)]
    if extra_headers:
        for box in extra_boxes:
            crop = im.crop((int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h)))
            for th in (None, 150, 190):
                text = ocr(crop, threshold=th).upper()
                squished = text.replace(" ", "")
                for extra in extra_headers:
                    if extra in text or extra.replace(" ", "") in squished:
                        found_extra.add(extra)

    for box in [(0.03, 0.02, 0.75, 0.14), (0.0, 0.0, 0.85, 0.13),
                (0.0, 0.0, 0.5, 0.12), (0.03, 0.08, 0.85, 0.22)]:
        crop = im.crop((int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h)))
        for th in (None, 150, 190):
            text = ocr(crop, threshold=th).upper()
            squished = text.replace(" ", "")
            for extra in extra_headers:
                if extra in text or extra.replace(" ", "") in squished:
                    found_extra.add(extra)
            for cls in CLASSES:
                if cls in text or cls.replace(" ", "") in squished:
                    return cls, found_extra
            for run in re.findall(r"[A-Z][A-Z ]{4,}", text):
                best = fuzzy_class_match(run)
                if best:
                    return best, found_extra

    # Dedicated pass for the "SKILL UNLOCKS" level-up screen's class-name
    # text, which sits lower and further right than the boxes above and
    # was found (by trial) to need a wider threshold/scale sweep - the
    # blurred in-world background behind it defeats a single setting.
    box = (0.14, 0.14, 0.40, 0.21)
    crop = im.crop((int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h)))
    for th in (None, 100, 140, 150, 190, 200):
        for sc in (1, 2):
            text = ocr(crop, threshold=th, scale=sc).upper()
            squished = text.replace(" ", "")
            for cls in CLASSES:
                if cls in text or cls.replace(" ", "") in squished:
                    return cls, found_extra
            for run in re.findall(r"[A-Z][A-Z ]{4,}", text):
                best = fuzzy_class_match(run)
                if best:
                    return best, found_extra

    return None, found_extra


def detect_class_by_cards(im):
    """Fallback: identify the class from card names visible in the grid."""
    w, h = im.size
    grid = im.crop((int(0.02 * w), int(0.10 * h), int(0.66 * w), int(0.90 * h)))
    blob = ""
    for th in (150, None):
        blob += ocr(grid, threshold=th).upper() + "\n"
    squished = blob.replace(" ", "")
    scores = {}
    for cls, names in distinctive_names().items():
        s = sum(1 for n in names
                if len(n) >= 5 and (n.upper() in blob
                                    or n.upper().replace(" ", "") in squished))
        if s:
            scores[cls] = s
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    if ranked[0][1] >= 2 and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
        return ranked[0][0]
    return None


def match_card_line(text, candidates):
    lines = [re.sub(r"[^A-Za-z' \-]", "", l).strip()
             for l in text.splitlines() if l.strip()]
    lines = [l for l in lines if len(l) >= 4]
    probes = lines + [f"{a} {b}" for a, b in zip(lines, lines[1:])]
    for p in probes:
        best = difflib.get_close_matches(p.title(), candidates, 1, 0.75)
        if best:
            return best[0]
    return None


def find_selected_cell(im):
    """Locate the card highlighted with the red glow border (skill-card grid)."""
    w, h = im.size
    r, g, b = im.convert("RGB").split()
    m = ImageChops.multiply(
        ImageChops.subtract(r, g).point(lambda p: 255 if p > 70 else 0),
        ImageChops.subtract(r, b).point(lambda p: 255 if p > 60 else 0))
    m = ImageChops.multiply(m, r.point(lambda p: 255 if p > 160 else 0))
    best = (0, None)
    for ci, (x0, x1) in enumerate(GRID_COLS):
        for ri, (y0, y1) in enumerate(GRID_ROWS):
            cell = m.crop((int((x0 - 0.012) * w), int((y0 - 0.02) * h),
                           int((x1 + 0.012) * w), int((y1 + 0.02) * h)))
            cnt = cell.histogram()[255]
            if cnt > best[0]:
                best = (cnt, (ci, ri))
    return best[1] if best[0] > 2500 else None


def red_glow_mask(im, r_gt=160, rg_gt=70, rb_gt=60):
    """Generic red-glow-border isolation, reused by cards, bundles, etc."""
    r, g, b = im.convert("RGB").split()
    m = ImageChops.multiply(
        ImageChops.subtract(r, g).point(lambda p: 255 if p > rg_gt else 0),
        ImageChops.subtract(r, b).point(lambda p: 255 if p > rb_gt else 0))
    return ImageChops.multiply(m, r.point(lambda p: 255 if p > r_gt else 0))


def detect_card(im, cls):
    """Card name + current LEVEL. Panel first, red-glow card as fallback."""
    w, h = im.size
    candidates = card_names_for(cls)
    name, level = None, None

    panel = im.crop((int(0.62 * w), int(0.24 * h), int(0.99 * w), int(0.80 * h)))
    for th in (None, 140, 150, 180):
        text = ocr(panel, threshold=th)
        if level is None:
            m = re.search(r"LEVEL\s*(\d)", text.upper())
            if m:
                level = int(m.group(1))
        if name is None:
            name = match_card_line(text, candidates)
        if name and level:
            return name, level

    if name is None:
        cell = find_selected_cell(im)
        if cell:
            ci, ri = cell
            x0, x1 = GRID_COLS[ci]
            y0, y1 = GRID_ROWS[ri]
            for band in ((0.50, 0.75), (0.55, 0.82), (0.42, 0.68)):
                crop = im.crop((int((x0 + 0.006) * w), int((y0 + (y1 - y0) * band[0]) * h),
                                int((x1 - 0.006) * w), int((y0 + (y1 - y0) * band[1]) * h)))
                for th in (150, None, 180):
                    name = match_card_line(ocr(crop, threshold=th), candidates)
                    if name:
                        break
                if name:
                    break
    return name, level


SCOREBOARD_TITLE_RE = re.compile(r"\bHORDE\b(?:\s+\w+)?\s+ON\s+\w+")


def looks_like_horde_scoreboard(im):
    """Detects the post-wave/pause Horde scoreboard - used both as an
    exclusion check (so a class name appearing in the results table doesn't
    get mistaken for a class-card screen's own title, see section_classes)
    and, via section_scoreboard below, as a positive match to route these
    screenshots into the existing Horde Scoreboard folder automatically.

    Two independent signals, either is enough:
    1. The results table's own distinctive column/section headers (COG /
       PING / KILLS / ASSISTS / REVIVES / DAMAGE DONE, or the pause-menu's
       MASTER MODIFIERS panel) - reliable on the full-size pause-menu
       variant of this screen, where the text is large and high-contrast.
    2. The screen's title, which always reads "HORDE ... ON <map>" (e.g.
       "HORDE ON NEXUS", "HORDE FRENZY ON DISTRICT") - added because the
       compact end-of-wave popup variant is smaller/blurrier, and its
       table headers often don't OCR cleanly even though this bold title
       text usually still does. The regex requires "ON" within one word of
       "HORDE" specifically so this can't accidentally match unrelated
       screens that merely mention Horde somewhere (e.g. the Gamemodes
       options list, which lists "HORDE", "HORDE FRENZY", "CLASSIC HORDE"
       etc as plain menu entries with no "ON" nearby)."""
    w, h = im.size
    crop = im.crop((0, 0, w, int(h * 0.45)))
    text = ""
    for th in (None, 150, 190, 210):
        text += ocr(crop, threshold=th).upper() + "\n"
    squished = text.replace(" ", "")
    markers = ("ASSISTS", "REVIVES", "DAMAGEDONE", "TEAMWAVESCORE", "MASTERMODIFIERS")
    if any(m in squished for m in markers):
        return True

    for th in (150, 190):
        title = ocr(im.crop((0, 0, w, int(h * 0.12))), threshold=th).upper()
        if SCOREBOARD_TITLE_RE.search(title):
            return True
    return False


def section_scoreboard(im):
    """Post-wave/pause Horde scoreboard - see looks_like_horde_scoreboard for
    the detection logic. Filed into the existing Horde Scoreboard folder
    with a generic sequential name (None base): nothing about which players
    or map it is matters for reference purposes, and there's no reliable
    single name to extract anyway (the title varies per map/mode)."""
    if looks_like_horde_scoreboard(im):
        return {}
    return None


def build_scoreboard(info):
    return REF / "Horde Scoreboard", None


def section_classes(im):
    """Skill card grid (upgrade browsing) OR the 'SKILL UNLOCKS' level-up screen."""
    # Belt-and-suspenders: section_scoreboard above already runs first in
    # SECTIONS and claims scoreboard screens before execution ever reaches
    # here. This check stays as a second line of defense in case SECTIONS
    # order ever changes - the original bug this guards against (a class
    # name in the results table getting mistaken for this screen's own
    # title) is exactly what looks_like_horde_scoreboard was written for.
    if looks_like_horde_scoreboard(im):
        return None
    cls, extra = detect_class_header(im, extra_headers=("SKILL UNLOCKS",))
    if not cls:
        cls = detect_class_by_cards(im)
    if not cls:
        return None

    if "SKILL UNLOCKS" in extra:
        return {"kind": "unlocks", "cls": cls}

    name, level = detect_card(im, cls)
    return {"kind": "upgrade", "cls": cls, "name": name, "level": level}


def build_classes(info):
    folder_name, _ = CLASSES[info["cls"]]
    if info["kind"] == "unlocks":
        folder = REF / f"{folder_name} - A - Card Unlock Levels"
        return folder, None  # None base = keep a generic sequential name, see main()
    folder = REF / f"{folder_name} - B - Card Upgrades"
    if info["name"] and info["level"]:
        return folder, f"{info['name']} - {info['level']}"
    if info["name"]:
        return folder, info["name"]
    return folder, None


# ─────────────────────────────────────────────────────────────────────────
# Class perks (in-Horde D-pad overlay)
# ─────────────────────────────────────────────────────────────────────────
# NOTE: this screen never prints the class name anywhere on screen (unlike
# the skill-card menu), so the class is guessed by matching the 4 visible
# perk names against each class's perks: {up,right,down,left} in
# _classes/*.md. That data has drifted out of date for at least one class
# already (see ref/CLAUDE.md) - so a perk-name match here is treated as
# low-confidence and ALWAYS sent to needs_review, never auto-filed. This
# still saves the OCR legwork of reading out the 4 names/levels/costs.

PERK_QUADRANTS = {
    "up":    (0.415, 0.215, 0.585, 0.39),
    "left":  (0.255, 0.405, 0.415, 0.60),
    "right": (0.585, 0.405, 0.745, 0.60),
    "down":  (0.415, 0.60,  0.585, 0.775),
}


def perk_names_for(cls):
    if cls in _perk_cache:
        return _perk_cache[cls]
    fname = CLASSES[cls][1]
    path = CLASSES_DIR / fname
    if not path.exists():
        path = CLASSES_DIR / ALT_FILES.get(fname, fname)
    names = []
    if path.exists():
        text = path.read_text(encoding="utf-8")
        m = re.search(r"^perks:\n(.*?)(?:\ncards:|\Z)", text, re.S | re.M)
        if m:
            names = re.findall(r"name:\s*(.+)", m.group(1))
    _perk_cache[cls] = names
    return names


def read_perk_quadrant(im, box):
    crop = crop_frac(im, box)
    w, h = crop.size
    name_crop = crop.crop((0, 0, w, int(h * 0.35)))
    level_crop = crop.crop((0, int(h * 0.55), w, int(h * 0.80)))

    name = None
    for th in (None, 150, 190):
        text = ocr(name_crop, threshold=th, psm=7).strip()
        text = re.sub(r"[^A-Za-z ]", "", text).strip()
        if len(text) >= 3:
            name = text.title()
            break

    level = None
    for th in (None, 150, 190):
        text = ocr(level_crop, threshold=th, psm=7).upper()
        if "MAX" in text:
            level = "MAX"
            break
        m = re.search(r"LEVEL\s*(\d+)\s*/\s*10", text)
        if m:
            level = m.group(1)
            break
    return name, level


def section_perks(im):
    w, h = im.size
    # The top-left HUD (WAVE/SCORE/CARRYING/FABRICATOR) is visible on every
    # in-Horde screen, including this perk D-pad overlay - cheap sanity
    # check before spending time on 4 OCR passes. Box needs to reach down
    # to ~0.20h to cover "SCORE"; a shallower box only catches "WAVE N".
    header = ""
    for th in (None, 190):
        header += ocr(crop_frac(im, (0.0, 0.0, 0.30, 0.24)), threshold=th).upper()
    if "SCORE" not in header and "WAVE" not in header:
        return None

    quadrants = {}
    for direction, box in PERK_QUADRANTS.items():
        name, level = read_perk_quadrant(im, box)
        if name:
            quadrants[direction] = (name, level)
    if len(quadrants) < 2:
        return None  # not actually a perk overlay, or too little readable

    # best-effort class guess purely for a friendlier filename - NOT trusted
    # for auto-filing (see note above).
    read_names = {n for n, _ in quadrants.values()}
    best_cls, best_score = None, 0
    for cls in CLASSES:
        score = sum(1 for n in perk_names_for(cls)
                    if difflib.get_close_matches(n, read_names, 1, 0.75))
        if score > best_score:
            best_cls, best_score = cls, score

    return {"quadrants": quadrants, "guessed_cls": best_cls if best_score >= 2 else None}


def build_perks(info):
    # Always None, None - see the note above section_perks(). The class
    # guess is low-confidence (perk data has drifted stale before) and
    # there's no per-class subfolder here to get wrong, but a bad guess
    # baked into the filename would still mislead silently. Better to
    # always leave these for a person to name by hand; main() logs the
    # OCR'd quadrant names/levels nowhere else, so print them here so
    # they're not wasted effort even though the file goes to needs_review.
    parts = []
    for direction in ("up", "right", "down", "left"):
        if direction in info["quadrants"]:
            name, level = info["quadrants"][direction]
            parts.append(f"{name} {level}" if level else name)
    guess = info["guessed_cls"].title() if info["guessed_cls"] else "unknown class"
    if parts:
        print(f"   (perk overlay read: {', '.join(parts)} - guessed class: {guess})")
    return None, None


# ─────────────────────────────────────────────────────────────────────────
# Fabricator (in-world build/upgrade prompt)
# ─────────────────────────────────────────────────────────────────────────
# The prompt text ("BARRIER LV2 UPGRADE: 2,550") sits over a busy in-world
# background (not a clean UI panel), so OCR here is noisier than the menu
# screens - matched with fuzzy name-matching and digit-only price/level
# extraction, tolerant of a few misread characters.

def fabricator_fortifications():
    """[{name, levels: [{price, engineer}, ...]}, ...] parsed straight out of
    _data/fabricator.yml, used both for the name list and for sanity-checking
    a noisily-OCR'd price against the known real values."""
    global _fab_names_cache
    if _fab_names_cache is not None:
        return _fab_names_cache
    forts = []
    if FABRICATOR_DATA.exists():
        text = FABRICATOR_DATA.read_text(encoding="utf-8")
        m = re.search(r"^fortifications:\n(.*?)\nmodifiers:", text, re.S | re.M)
        if m:
            for block in re.split(r"\n(?=  - name:)", m.group(1)):
                nm = re.search(r'name:\s*"([^"]+)"', block)
                if not nm:
                    continue
                levels = [{"price": p, "engineer": e} for p, e in
                          re.findall(r'price:\s*"([^"]+)",\s*engineer:\s*"([^"]+)"', block)]
                forts.append({"name": nm.group(1), "levels": levels})
    _fab_names_cache = forts
    return forts


def section_fabricator(im):
    # The floating world-prompt text's exact position shifts a little with
    # camera distance/angle, so a couple of crop boxes are tried (tightest
    # first), each upscaled + reduced to a near-white mask (busy dirt/metal
    # backgrounds defeat plain thresholding). The first threshold whose text
    # contains a recognisable fortification name, "UPGRADE", or "BUILD"
    # wins - anything else is almost certainly noise, not worth keeping.
    # ("BUILD" is the prompt for a fortification's first construction;
    # "UPGRADE" only appears once it already exists at a lower level.)
    fort_names_upper = [f["name"].upper() for f in fabricator_fortifications()]
    text = None
    for box in [(0.22, 0.69, 0.78, 0.74), (0.20, 0.68, 0.80, 0.75)]:
        crop = crop_frac(im, box)
        crop = crop.resize((crop.width * 3, crop.height * 3), Image.LANCZOS)
        r, g, b = crop.convert("RGB").split()
        mn = ImageChops.darker(ImageChops.darker(r, g), b)
        # Was 10 values 5 apart (235 down to 190) - each OCR call here costs
        # ~a quarter-second (subprocess spawn dominates, not the actual OCR),
        # and a 5-unit shift in an 8-bit threshold essentially never flips
        # enough pixels to change what a near-white text mask reads as, so
        # most of those 10 were never adding a real chance of a different
        # result. 4 values spanning the same 190-235 range evenly keeps the
        # same coverage for well under half the calls.
        for th in (235, 220, 205, 190):
            mask = mn.point(lambda p: 0 if p > th else 255)
            candidate = ocr(mask, psm=7, scale=1).upper()
            if "UPGRADE" in candidate or "BUILD" in candidate or any(n in candidate for n in fort_names_upper):
                text = candidate
                break
        if text:
            break
    if text is None:
        return None

    forts = fabricator_fortifications()
    fort = None
    for f in forts:
        if f["name"].upper() in text:
            fort = f
            break
    if not fort:
        for run in re.findall(r"[A-Z][A-Z ]{3,}", text):
            best = difflib.get_close_matches(run.strip(), fort_names_upper, 1, 0.7)
            if best:
                fort = next(f for f in forts if f["name"].upper() == best[0])
                break
    if not fort:
        return None

    lvl_m = re.search(r"L?V\s*(\d)", text)  # OCR frequently drops the L in "LV2"
    level = int(lvl_m.group(1)) if lvl_m else None
    is_upgrade = "UPGRADE" in text
    is_build = "BUILD" in text

    # Only ever attempt a price when "UPGRADE" or "BUILD" is actually
    # present - other HUD states (e.g. a plain "(current/max HP)" readout)
    # print digits here too, and reporting one of those as a price would be
    # actively wrong.
    price = None
    if (is_upgrade or is_build) and level and 1 <= level <= len(fort["levels"]):
        marker = "UPGRADE" if is_upgrade else "BUILD"
        raw_price = best_digits(text.split(marker)[-1])
        expected = fort["levels"][level - 1]
        for candidate in (expected["price"], expected["engineer"]):
            digits = re.sub(r"[^\d]", "", candidate)
            # require both a close character match AND matching length -
            # a truncated read (e.g. "400" for "3400") must not slip through.
            if digits and raw_price and len(raw_price) == len(digits) and \
                    difflib.SequenceMatcher(None, raw_price, digits).ratio() > 0.7:
                price = digits
                break

    return {"name": fort["name"], "level": level, "is_upgrade": is_upgrade, "price": price}


def build_fabricator(info):
    folder = REF / "Fabricator"
    if not info["level"]:
        return folder, info["name"]
    label = "Upgrade" if info["is_upgrade"] else "Build"
    if info["price"]:
        return folder, f"{info['name']} LV{info['level']} ({label} {info['price']})"
    return folder, f"{info['name']} LV{info['level']}"


def section_fabricator_menu(im):
    """The full FABRICATOR menu (opened by holding the interact button),
    showing every fortification as a price grid plus a detail panel below
    for whichever tile is currently selected. Distinct from
    section_fabricator above, which only ever sees the small in-world
    build/upgrade prompt: this is the deliberate menu screen, with much
    larger/cleaner text that OCRs far more reliably, and it's what actually
    shows a fortification's LIVE price (including any active discount
    modifier, e.g. "2,060 (-47%)") rather than just the base table value."""
    header = ocr(crop_frac(im, (0.27, 0.07, 0.47, 0.12)), threshold=None, psm=7).upper()
    if "FABRICATOR" not in header.replace(" ", ""):
        return None

    forts = fabricator_fortifications()
    fort_names_upper = [f["name"].upper() for f in forts]

    name_text = ""
    for th in (None, 140, 170, 180):
        name_text = ocr(crop_frac(im, (0.28, 0.535, 0.55, 0.575)), threshold=th, psm=7).upper()
        if name_text.strip():
            break

    fort = None
    for f in forts:
        if f["name"].upper() in name_text:
            fort = f
            break
    if not fort:
        for run in re.findall(r"[A-Z][A-Z ]{3,}", name_text):
            best = difflib.get_close_matches(run.strip(), fort_names_upper, 1, 0.7)
            if best:
                fort = next(f for f in forts if f["name"].upper() == best[0])
                break

    lvl_m = re.search(r"L?V\s*(\d)", name_text)
    level = int(lvl_m.group(1)) if lvl_m else None

    # The COST line's price here is whatever's live on screen right now -
    # which can legitimately be a discounted price under an active modifier
    # (e.g. "(-47%)"), so unlike section_fabricator this deliberately does
    # NOT cross-check it against the base fabricator.yml price before
    # accepting it - a mismatch there doesn't mean a misread, it can just
    # mean a modifier was active. It's reported for what it visibly is: the
    # live in-menu price, not necessarily the base price.
    cost_text = ocr(crop_frac(im, (0.42, 0.69, 0.60, 0.74)), threshold=None, psm=7).upper()
    price = best_digits(cost_text) if "COST" in cost_text else None

    return {"fort": fort, "level": level, "price": price}


def build_fabricator_menu(info):
    folder = REF / "Fabricator"
    if not info["fort"]:
        return folder, None
    name = info["fort"]["name"]
    if info["level"] and info["price"]:
        return folder, f"{name} LV{info['level']} (Menu {info['price']})"
    if info["level"]:
        return folder, f"{name} LV{info['level']} (Menu)"
    return folder, f"{name} (Menu)"


# ─────────────────────────────────────────────────────────────────────────
# Maps: hero/loading-screen splash art + the in-match top-down "layout"
# callout diagram used by the Maps page (images/maps/<slug>.jpg and
# images/maps/<slug>-layout.jpg - see that file's own header comment for
# the full naming convention, including -small.jpg which is just a resize
# of the hero shot and isn't a distinct capture worth detecting here).
#
# UNVALIDATED: unlike every other section above, this one hasn't been
# tuned against a real screenshot of either target screen (no sample was
# available while writing it) - the caption boxes and the schematic/
# saturation heuristic below are a best-effort starting point, not proven.
# Per the project's own "favor leaving it alone over guessing wrong"
# standard (see ref/CLAUDE.md), that's exactly why this section ALWAYS
# routes into ref/Maps/ for a person to check rather than ever touching
# images/maps/ directly - nothing in this script writes into images/ or
# _data/ anywhere, by design, and this section is no exception. If a
# match here turns out wrong once real captures are available, tighten
# the boxes/threshold rather than the destination.
# ─────────────────────────────────────────────────────────────────────────

def map_names():
    """Every map name from _data/maps.yml (both the PVP/PVE-Horde and
    PVE-Escape groups) - the group headers themselves ("PVP and PVE
    (Horde)", "PVE (Escape)") are 0-indent `- name:` lines, while actual
    maps are nested/indented under each group's `maps:` list, so requiring
    at least one leading space is what tells them apart here."""
    global _map_names_cache
    if _map_names_cache is not None:
        return _map_names_cache
    names = []
    if MAPS_DATA.exists():
        text = MAPS_DATA.read_text(encoding="utf-8")
        names = [n.strip() for n in re.findall(r"^[ \t]+- name:\s*(.+)$", text, re.M)]
    _map_names_cache = names
    return names


def fuzzy_map_match(run, ratio=0.72, min_frac=0.7):
    """Same shape as fuzzy_class_match above, and guards against the same
    short-fragment false-positive failure mode - see that function's
    docstring for the concrete example that motivated the min_frac check."""
    run = run.strip()
    if not run:
        return None
    names = map_names()
    best = difflib.get_close_matches(run, names, 1, ratio)
    if best and len(run) >= min_frac * len(best[0]):
        return best[0]
    return None


def map_asset_exists(slug, suffix):
    return (MAPS_IMAGES_DIR / f"{slug}{suffix}").exists()


def section_map(im):
    # Layout/tactical top-down variant is visually distinctive - a mostly
    # desaturated blueprint-style schematic (line-art room outlines on a
    # flat background) rather than a normal rendered scene. Average HSV
    # saturation is a cheap proxy for that; the threshold is a guess (see
    # module note above) and will need calibrating against a real capture.
    hsv = im.convert("HSV")
    _, s_channel, _ = hsv.split()
    # ImageStat.Stat().mean is the built-in way to average a channel's pixel
    # values - .getdata() summed by hand works too, but it's deprecated as
    # of Pillow 14 (due for removal 2027-10-15) in favor of this.
    avg_sat = ImageStat.Stat(s_channel).mean[0]
    is_schematic = avg_sat < 40

    # Try a handful of plausible caption positions (top banner, bottom
    # third/left, centered) rather than one fixed box, same approach as
    # detect_class_header above - a loading screen's map-name caption
    # placement isn't confirmed, so cast a wider net than usual here.
    name = None
    for box in [(0.0, 0.0, 1.0, 0.12), (0.0, 0.85, 1.0, 1.0), (0.0, 0.0, 0.40, 1.0),
                (0.30, 0.85, 0.70, 0.97), (0.0, 0.30, 0.30, 0.55)]:
        crop = crop_frac(im, box)
        for th in (None, 150, 190):
            text = ocr(crop, threshold=th).upper()
            for run in re.findall(r"[A-Z][A-Z' ]{3,}", text):
                best = fuzzy_map_match(run.title())
                if best:
                    name = best
                    break
            if name:
                break
        if name:
            break

    # A map name has to actually be read before this claims anything -
    # low saturation alone isn't enough signal on its own (other desaturated
    # menus/loading spinners could false-positive), consistent with every
    # other detector here never guessing off a single weak signal.
    if not name:
        return None

    return {"name": name, "kind": "layout" if is_schematic else "hero"}


def build_map(info):
    folder = REF / "Maps"
    slug = slugify(info["name"])
    kind = info["kind"]
    suffix = "-layout.jpg" if kind == "layout" else ".jpg"
    tag = "existing on site, verify before replacing" if map_asset_exists(slug, suffix) \
        else "MISSING on site"
    return folder, f"{info['name']} ({kind} candidate - {tag})"


# ─────────────────────────────────────────────────────────────────────────
# Store: single-item bundle preview ("ENTER  PURCHASE SET / SELECT")
# ─────────────────────────────────────────────────────────────────────────

def section_bundle_item(im):
    header = ocr(crop_frac(im, (0.05, 0.10, 0.50, 0.155)), threshold=200, psm=7).strip()
    footer = ocr(crop_frac(im, (0.0, 0.80, 0.55, 0.88)), threshold=200, psm=6).upper()
    if "PURCHASE" not in footer and "SELECT" not in footer:
        return None
    if len(re.sub(r"[^A-Za-z]", "", header)) < 4:
        return None

    item = ocr(crop_frac(im, (0.64, 0.62, 0.81, 0.73)), threshold=180, psm=6)
    item = " ".join(w for w in re.findall(r"[A-Za-z']+", item) if len(w) > 1).title()
    return {"bundle": header.title(), "item": item or None}


def build_bundle_item(info):
    # Reuses whichever "Bundles - NN - <name>" folder already exists for
    # this bundle name; if none exists yet this is new territory and goes
    # to needs_review instead of inventing a numbered folder on its own.
    matches = [p for p in REF.glob("Bundles - * - *") if p.is_dir()
              and difflib.SequenceMatcher(None, p.name.split(" - ", 2)[-1].upper(),
                                          info["bundle"].upper()).ratio() > 0.8]
    if not matches:
        return None, None
    return matches[0], info["item"]


# ─────────────────────────────────────────────────────────────────────────
# Store: bundle list page (STORE > BUNDLES, red-glow selection)
# ─────────────────────────────────────────────────────────────────────────

def section_store_list(im):
    header = ocr(crop_frac(im, (0.02, 0.06, 0.20, 0.13)), threshold=200, psm=7).upper()
    if "STORE" not in header:
        return None

    mask = red_glow_mask(im)
    w, h = im.size
    # scan a horizontal strip of store-card-sized cells for the glow border
    best = (0, None)
    cols = 6
    card_w = 1.0 / cols
    for ci in range(cols):
        x0, x1 = ci * card_w, (ci + 1) * card_w
        cell = mask.crop((int(x0 * w), int(0.25 * h), int(x1 * w), int(0.75 * h)))
        cnt = cell.histogram()[255]
        if cnt > best[0]:
            best = (cnt, ci)
    if best[0] < 2000:
        return None
    ci = best[1]
    x0, x1 = ci * card_w, (ci + 1) * card_w
    cell_w = x1 - x0
    caption = crop_frac(im, (x0 + 0.25 * cell_w, 0.672, x0 + 0.87 * cell_w, 0.706))
    text = ocr(caption, threshold=180, psm=6)
    name = " ".join(w for w in re.findall(r"[A-Za-z']+", text) if len(w) > 1).title()
    return {"name": name or None}


def build_store_list(info):
    return REF / "Store Pages", info["name"]


# ─────────────────────────────────────────────────────────────────────────
# Marks (cosmetic banner-mark collection grid, "MARKS" header + Q/E tabs)
# ─────────────────────────────────────────────────────────────────────────
# A 3-column grid of unlockable cosmetic Marks (Hammer & Sickle Mark, Xbox
# Mark, 83% Mark, etc.), reached from the same Q/E tab bar as CLASSES/
# WEAPONS (see the "MissJamball7618" screenshots this was built from - the
# user remembered these as "from the shop page", but the on-screen header
# reads "MARKS", not "STORE", so this is filed as its own section rather
# than folded into store_list/bundle_item above). Every card's caption ends
# in the literal word "MARK" (singular, even for "83% MARK" or a two-line
# "COMPETITIVE: HERO MARK"), which is a much more reliable anchor than
# trying to whitelist every possible Mark name up front - this section has
# no existing _data/*.yml name list to cross-check against (unlike classes/
# fabricator/maps), since nothing on the site tracked Marks before now.

MARKS_HEADER_BOX = (0.02, 0.03, 0.25, 0.12)
MARKS_GRID_COLS = [(0.05, 0.195), (0.20, 0.335), (0.355, 0.49)]
MARKS_GRID_ROWS = [(0.395, 0.455), (0.715, 0.80)]
MARKS_NAME_RE = re.compile(r"[A-Z0-9][A-Z0-9%:&' \-]{1,40}?MARK\b")


def _header_says_marks(im):
    """"MARKS" header check, swept across a few psm/threshold combos - one
    real capture of this screen (see section_marks docstring) needed the
    plain color crop at psm 7 to read clearly, another needed a binarized
    threshold pass instead; no single setting caught both, so this tries
    both like detect_class_header's own multi-pass sweep does for the
    CLASSES header. Checked with spaces stripped since a stray leading
    glyph (the back-arrow icon bleeding into the crop) sometimes lands
    right against the "M" with no space OCR'd between them."""
    crop = crop_frac(im, MARKS_HEADER_BOX)
    for psm in (6, 7):
        text = pytesseract.image_to_string(crop, config=f"--psm {psm}").upper()
        if "MARKS" in text.replace(" ", ""):
            return True
    g = ImageOps.grayscale(crop)
    for th in (150, 180, 190, 200):
        mask = g.point(lambda p: 255 if p > th else 0)
        for psm in (6, 7):
            text = pytesseract.image_to_string(mask, config=f"--psm {psm}").upper()
            if "MARKS" in text.replace(" ", ""):
                return True
    return False


def _read_mark_cell(im, w, h, x0, x1, y0, y1):
    """Best-effort card-name OCR for one grid cell. Tries a threshold sweep
    (busy in-world background behind the semi-transparent card, same problem
    section_fabricator solves the same way) and returns the first clean
    "...MARK" match found, title-cased with stray single-letter OCR noise
    (icon glyphs bleeding into the crop) dropped. Returns None rather than a
    garbled guess if nothing clean turns up in any pass - a missing name is
    fine (falls back to a generic sequential filename, see build_marks),
    a wrong one baked into a "sorted" filename is not."""
    crop = im.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
    crop = crop.resize((crop.width * 2, crop.height * 2), Image.LANCZOS)
    g = ImageOps.grayscale(crop)
    for th in (210, 200, 190, 180, 150, None):
        mask = g.point(lambda p: 255 if p > th else 0) if th else g
        text = pytesseract.image_to_string(mask, config="--psm 6").upper()
        text = re.sub(r"\s+", " ", text)
        m = MARKS_NAME_RE.search(text)
        if not m:
            continue
        words = [w for w in m.group(0).split(" ") if not (len(w) == 1 and w.isalpha())]
        if words:
            return " ".join(words).title()
    return None


def section_marks(im):
    if not _header_says_marks(im):
        return None

    w, h = im.size
    names = []
    for y0, y1 in MARKS_GRID_ROWS:
        for x0, x1 in MARKS_GRID_COLS:
            name = _read_mark_cell(im, w, h, x0, x1, y0, y1)
            if name:
                names.append(name)
    return {"names": names}


def build_marks(info):
    folder = REF / "Marks"
    if not info["names"]:
        return folder, None  # header matched but no card name read cleanly
    label = ", ".join(info["names"][:4])
    if len(info["names"]) > 4:
        label += f" +{len(info['names']) - 4} more"
    return folder, label


# ─────────────────────────────────────────────────────────────────────────
# Difficulty modifiers ("SELECT DIFFICULTY" screen)
# ─────────────────────────────────────────────────────────────────────────

def section_difficulty(im):
    header = ocr(crop_frac(im, (0.07, 0.11, 0.50, 0.16)), threshold=200, psm=7).upper()
    if "DIFFICULT" in header:
        tier = ocr(crop_frac(im, (0.07, 0.16, 0.30, 0.20)), threshold=None, psm=7)
        tier = re.sub(r"[^A-Za-z]", "", tier).title() or None

        caption = ocr(crop_frac(im, (0.30, 0.79, 0.70, 0.85)), threshold=180, psm=6)
        name = caption.split("|")[0]
        name = " ".join(w for w in re.findall(r"[A-Za-z']+", name) if len(w) > 1).title()
        return {"tier": tier, "name": name or None}

    # Fallback for the pre-match "SELECT DIFFICULTY" modifier-wheel screen -
    # a different layout entirely (map subtitle instead of a tier, no
    # bottom caption), whose title also uses a more stylized/bold rendering
    # that the box above (calibrated against the in-match tab header)
    # doesn't read reliably at 1x scale - needs 2-3x upscaling to come
    # through clean, found by direct trial against a real screenshot this
    # always missed. Since the tier/caption boxes above don't apply to this
    # layout, this deliberately returns no tier/name rather than pointing
    # them at the wrong regions and baking a wrong guess into the filename.
    crop = crop_frac(im, (0.03, 0.06, 0.45, 0.13))
    header = ""
    for th in (None, 150):
        for sc in (2, 3):
            header = ocr(crop, threshold=th, psm=7, scale=sc).upper()
            if "DIFFICULT" in header:
                break
        if "DIFFICULT" in header:
            break
    if "DIFFICULT" not in header:
        return None
    return {"tier": None, "name": None}


def build_difficulty(info):
    folder = REF / "Difficulty"
    if info["tier"] and info["name"]:
        return folder, f"{info['tier']} Modifier - {info['name']}"
    return folder, info["name"]


# ─────────────────────────────────────────────────────────────────────────
# Characters (identifiable as the section, never as WHICH character)
# ─────────────────────────────────────────────────────────────────────────

def section_characters(im):
    header = ocr(crop_frac(im, (0.08, 0.075, 0.32, 0.135)), threshold=220, psm=7).upper()
    # the leading "C" often blends into the back-arrow icon and gets dropped
    if "CHARACTER" not in header and "HARACTER" not in header:
        return None
    return {}  # matched the section, but deliberately no name - see build_characters


def build_characters(info):
    # No filename is ever generated here on purpose: nothing on this screen
    # identifies WHICH character is selected. Returning None routes this to
    # needs_review with a clear reason instead of silently mis-sorting it.
    return None, None


# ─────────────────────────────────────────────────────────────────────────
# Squad & Honor (the squad/friends roster's own right-side detail panel,
# shown for whichever player is currently highlighted in the list)
# ─────────────────────────────────────────────────────────────────────────
# This is the SAME underlying screen as the "Friends List" out-of-scope
# marker below (both start from the "MY SQUAD"/"PLAYING GEARS"/"ONLINE"
# roster) - the difference is whether a player happens to be highlighted
# with their detail panel open. A bare roster with no panel showing has
# nothing account-wide worth extracting and stays out-of-scope; a panel
# showing "HONOR X / Y" is exactly the confirmed-level data the Honor
# section on classes/index.html (#honor-debug) is built from, so it gets
# its own section and its own folder instead of being silently discarded.
# Ordered BEFORE out_of_scope in SECTIONS so a squad screenshot that DOES
# have this panel open is claimed here first; one that doesn't still falls
# through to the Friends List marker exactly as before.
#
# The panel sits to the right of the roster list, but the list itself can
# be anywhys from 1 to 6+ rows tall (My Squad / Pending Invites / Playing
# Gears / Online / In Match, any subset of which may be present), so the
# panel's vertical position shifts screen to screen - unlike most other
# detectors here there's no single fixed y-box to crop. Scanning the whole
# right-half height band avoids needing to locate the panel's edges first.
HONOR_PANEL_BOX = (0.34, 0.0, 0.68, 1.0)
HONOR_LEVEL_TITLE_RE = re.compile(r"LVL\s*(\d+)\s+([A-Z][A-Z ]+?ALLY)")
HONOR_PROGRESS_RE = re.compile(r"HONOR\s+([\d,]+)\s*/\s*([\d,]+)")
HONOR_BONUS_RE = re.compile(r"\+\s*(\d+)\s*%")
# Lines that are real content in the panel but would otherwise look like a
# plausible all-caps "player name" line if not excluded explicitly.
HONOR_NON_NAME_WORDS = ("HONOR", "LVL", "ALLY", "ALLIES", "CAMPAIGN", "ESCAPE",
                         "CHAPTERS", "CLEARED", "VERSUS", "ROUNDS", "WON",
                         "HORDE", "WAVES", "BONUS", "XP", "PLAYING", "TOGETHER",
                         "INCREASES", "YOUR")


def section_honor(im):
    crop = crop_frac(im, HONOR_PANEL_BOX)
    passes = []
    any_honor = False
    for th in (None, 200, 170):
        text = ocr(crop, threshold=th, psm=6).upper()
        if "HONOR" in text:
            any_honor = True
        passes.append(text)
    if not any_honor:
        return None

    level, title, honor_cur, honor_max, bonus = None, None, None, None, None
    name_candidates = []
    for text in passes:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            lvl_m = HONOR_LEVEL_TITLE_RE.search(line)
            if lvl_m and level is None:
                level = int(lvl_m.group(1))
                title = lvl_m.group(2).strip().title()
            # Compare with spaces stripped from BOTH sides - a threshold pass
            # that reads this line cleanly still sometimes drops or doubles
            # a space around "YET"/"ALLIES", and the un-stripped search term
            # used before this fix could never match a stripped-only line.
            not_yet = "NOTYETALLIES" in line.replace(" ", "")
            if not_yet and level is None:
                level = 0
                title = "Not Yet an Ally"
            prog_m = HONOR_PROGRESS_RE.search(line)
            if prog_m and honor_cur is None:
                honor_cur = prog_m.group(1).replace(",", "")
                honor_max = prog_m.group(2).replace(",", "")
            # The player's name is always the line directly above whichever
            # line carries "LVL N TITLE" / "HONOR X / Y" / "NOT YET ALLIES" -
            # scoping the search to i-1 (rather than "first plausible
            # all-caps line anywhere in the panel") is what keeps this from
            # grabbing unrelated UI chrome above it, e.g. a matchmaking
            # timer's "COMPARING SKILL RATINGS" banner on a screenshot taken
            # while queuing. A small cosmetic icon (crown, gamerpic) right
            # next to the name often bleeds a stray leading glyph into this
            # same crop, so that's stripped before validating - but a
            # cleaner threshold pass can still misread a whole extra word
            # onto the front of an otherwise-correct line (not just a stray
            # symbol), which stripping can't fix. Every pass's candidate for
            # this same panel is kept rather than stopping at the first
            # match, and the shortest wins at the end - a name with genuine
            # garbage prepended is always longer than the same name read
            # cleanly elsewhere, and two different real names are never
            # length-ambiguous enough for this to pick the wrong player.
            if (lvl_m or not_yet or prog_m) and i > 0:
                candidate = re.sub(r"^[^A-Z0-9]+", "", lines[i - 1]).strip()
                letters_only = re.sub(r"[^A-Z]", "", candidate)
                if len(letters_only) >= 3 and not any(w in candidate for w in HONOR_NON_NAME_WORDS) \
                        and re.match(r"^[A-Z0-9][A-Z0-9 _'\-]+$", candidate):
                    name_candidates.append(candidate)
        if bonus is None:
            bonus_m = HONOR_BONUS_RE.search(text)
            if bonus_m:
                bonus = bonus_m.group(1)
        if level is not None and honor_cur is not None and name_candidates:
            break  # this pass read everything cleanly - no need to try a noisier one

    name = min(name_candidates, key=len).title() if name_candidates else None

    # Nothing beyond the bare "HONOR" keyword actually read cleanly - not
    # enough to file this with any useful name, and risky to even claim as
    # a match (could be a stray OCR misread of some other screen entirely).
    if level is None and honor_cur is None:
        return None

    # Best-effort only: a highlighted row still under "PENDING INVITES"
    # (rather than an accepted squad member) gets called out in the
    # filename same as it was by hand for the reference screenshots this
    # was built from - checked across the whole roster column since the
    # highlighted row's own section header can be several rows above it.
    pending = False
    roster_text = ocr(crop_frac(im, (0.0, 0.0, 0.36, 1.0)), threshold=None, psm=6).upper()
    if "PENDING" in roster_text or "INVITED YOU" in roster_text:
        pending = True

    return {"name": name, "level": level, "title": title, "honor_cur": honor_cur,
            "honor_max": honor_max, "bonus": bonus, "pending": pending}


def build_honor(info):
    folder = REF / "Squad & Honor"
    name = info["name"] or "Unknown"
    detail = []
    if info["honor_cur"] is not None and info["honor_max"] is not None:
        detail.append(f"Honor {info['honor_cur']}-{info['honor_max']}")
    if info["level"] is not None:
        detail.append(f"LVL {info['level']}")
    if info["pending"]:
        detail.append("Pending Invite")
    label = name if not detail else f"{name} ({', '.join(detail)})"
    return folder, f"Squad - {label}"


# ─────────────────────────────────────────────────────────────────────────
# Out of scope (profile/social/progression screens this site doesn't cover)
# ─────────────────────────────────────────────────────────────────────────
# Tour Rewards, the Tour overview ("OVERALL PROGRESS"), Medals/achievements,
# the post-match MVP card, and the friends/squad list are all real Gears 5
# screens, but none of them are Horde class/card/perk/fabricator reference
# material - nothing here will ever feed the site. Unlike an unrecognised
# screen (which MIGHT turn out to matter once a person looks at it), these
# are confidently identified AND confidently irrelevant, so - unusually for
# this script - a positive match here is auto-filed rather than left for
# review: there's no ambiguity to protect against, and it saves a person
# from re-discovering "oh, this one's just a Tour Rewards screenshot" over
# and over. Each marker's crop box + keyword was picked and verified against
# a real screenshot of that screen, same as every other detector here.
#
# "Friends List" only ever catches a bare roster with no player's detail
# panel open - one WITH a "HONOR X / Y" panel open is claimed by
# section_honor above instead (see that section's own comment), since
# section_honor runs first in SECTIONS.
OUT_OF_SCOPE_MARKERS = [
    ("Tour Rewards",  (0.03, 0.07, 0.35, 0.16), "TOUR REWARDS",     (None, 200, 150)),
    ("Medals",        (0.03, 0.07, 0.30, 0.16), "MEDALS",           (200, None, 150)),
    ("Tour Overview", (0.03, 0.07, 0.35, 0.17), "OVERALL PROGRESS", (None, 200, 150)),
    ("Match MVP",     (0.03, 0.34, 0.35, 0.46), "MATCH MVP",        (220, None, 150)),
    ("Friends List",  (0.02, 0.03, 0.36, 0.10), "MY SQUAD",         (None, 200, 150)),
]


def section_out_of_scope(im):
    for label, box, keyword, thresholds in OUT_OF_SCOPE_MARKERS:
        crop = crop_frac(im, box)
        crop = crop.resize((crop.width * 3, crop.height * 3), Image.LANCZOS)
        text = ""
        for th in thresholds:
            text = ocr(crop, threshold=th, psm=6, scale=1).upper()
            if keyword in text:
                return {"label": label}
    return None


def build_out_of_scope(info):
    return REF / "Not Used - Profile & Progression", info["label"]


# ─────────────────────────────────────────────────────────────────────────
# Section registry - order matters, most distinctive/cheap checks first
# ─────────────────────────────────────────────────────────────────────────

SECTIONS = [
    # Must run before out_of_scope: a squad/roster screenshot WITH a
    # player's Honor panel open needs to be claimed here, not fall into
    # out_of_scope's "Friends List" marker (which only fires on the bare
    # roster with no panel open) - see section_honor's own comment.
    {"name": "honor",        "detect": section_honor,        "build": build_honor},
    {"name": "out_of_scope", "detect": section_out_of_scope, "build": build_out_of_scope},
    {"name": "scoreboard",   "detect": section_scoreboard,   "build": build_scoreboard},
    # marks is checked before classes: its "MARKS" header + card grid can
    # otherwise fool detect_class_by_cards's fuzzy name matching (a real bug
    # hit while building this - a Marks screenshot got mis-filed as a
    # Marksman skill-card screen before this was moved up), the same
    # failure mode scoreboard above already guards against for the results
    # table's class column.
    {"name": "marks",        "detect": section_marks,        "build": build_marks},
    {"name": "classes",      "detect": section_classes,      "build": build_classes},
    {"name": "perks",        "detect": section_perks,        "build": build_perks},
    {"name": "fabricator",   "detect": section_fabricator,   "build": build_fabricator},
    {"name": "fabricator_menu", "detect": section_fabricator_menu, "build": build_fabricator_menu},
    {"name": "map",          "detect": section_map,          "build": build_map},
    {"name": "bundle_item",  "detect": section_bundle_item,  "build": build_bundle_item},
    {"name": "store_list",   "detect": section_store_list,   "build": build_store_list},
    {"name": "difficulty",   "detect": section_difficulty,   "build": build_difficulty},
    {"name": "characters",   "detect": section_characters,   "build": build_characters},
]

# Sections whose build_*() intentionally returns (None, None) EVERY time it
# matches, not just when it fails to place something - "perks" (low-
# confidence class guess, see build_perks's own comment) and "characters"
# (no name is ever printed on screen, see build_characters's own comment)
# always want a human to sort them by hand, on purpose. classify_one's
# generic "folder is None -> keep trying other sections" fallback doesn't
# know that distinction though, so a real perk-overlay or characters-screen
# screenshot was falling through to be tested against every later section
# (fabricator, map, bundle_item, store_list, difficulty) instead of going
# straight to needs_review - and a false-positive match there would file it
# into a completely wrong folder rather than leaving it for review. This
# set is what tells classify_one to stop immediately instead of continuing
# once one of these two has positively identified the screenshot.
ALWAYS_REVIEW_SECTIONS = {"perks", "characters"}


def classify_one(img_path):
    """Run every detector against a single image and return what to do with
    it. This is the OCR-heavy, CPU/subprocess-bound part - pure and
    side-effect-free (it never touches disk beyond reading the image) so
    it's safe to run concurrently across many images at once. Actually
    moving the file and writing the ledger row happens back on the main
    thread once this returns, one at a time - see main().

    Timed with time.perf_counter() from the moment this worker actually
    starts on the image, not from when it was submitted - with several
    images in flight at once, "how long did this one take" is what's
    useful for judging detector cost, not how long it sat queued behind
    other workers. Also reports which pool thread (1..workers) handled it,
    via _worker_label() - purely cosmetic, so you can see the N workers
    actually churning through images in parallel rather than taking it on
    faith.

    Wrapped in try/except: dedupe_loose_images() already filters out
    corrupt/empty files before this ever runs, but that's not a guarantee
    nothing bad ever reaches here (a freshly-imported file could still be
    corrupt, for instance) - and a single bad image raising here used to
    crash the entire batch, killing the whole thread pool and losing
    whatever hadn't been logged yet. One unreadable image should mean one
    needs_review row, never a dead run."""
    t0 = time.perf_counter()
    worker = _worker_label()
    try:
        im = Image.open(img_path)
        for section in SECTIONS:
            info = section["detect"](im)
            if info is None:
                continue
            folder, base = section["build"](info)
            if folder is None:
                if section["name"] in ALWAYS_REVIEW_SECTIONS:
                    break  # positively identified - always needs_review, don't let a later section mis-claim it
                continue  # section matched but couldn't place it - keep trying others
            return img_path, section["name"], folder, base, time.perf_counter() - t0, worker
    except Exception as e:
        print(f"   (error reading {img_path.name}: {e})")
    return img_path, None, None, None, time.perf_counter() - t0, worker


def prewarm_caches():
    """Populate the lazy per-class caches (card names, perk names) and the
    fabricator fortification list up front, synchronously, before handing
    work to the thread pool. These caches are simple dicts that fill in on
    first use; without this, several threads could race to compute the
    same class's data for the first time simultaneously. That wouldn't
    corrupt anything (the computation is idempotent and dict writes are
    atomic under the GIL) but it would mean redundant duplicate work -
    prewarming just avoids that waste."""
    distinctive_names()
    fabricator_fortifications()
    for cls in CLASSES:
        perk_names_for(cls)


# ─────────────────────────────────────────────────────────────────────────
# Hash ledger
# ─────────────────────────────────────────────────────────────────────────

def file_hash(path):
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def load_ledger():
    if not LEDGER_PATH.exists():
        return {}
    with LEDGER_PATH.open(newline="", encoding="utf-8") as f:
        return {row["hash"]: row for row in csv.DictReader(f)}


class LedgerWriter:
    """Keeps the CSV file open for the whole run instead of reopening it on
    every single row (append_ledger() used to open+close per call - cheap
    in isolation but needless overhead across hundreds of files). Each
    write is still flushed immediately, so a row is durable on disk as
    soon as it's written - if the run is interrupted partway (Ctrl+C,
    crash, closed terminal), everything sorted or logged so far is still
    safely recorded and won't be reprocessed on the next run. This is
    NOT "collect everything then write once at the end" - that would be
    faster to open/close but would lose the whole run's progress on any
    interruption, which matters more here than the (very small) savings
    from batching, since the OCR work per image is the real cost anyway
    (see ref/CLAUDE.md - a single unmatched image can trigger 60-100+
    individual Tesseract calls; the CSV write is microseconds either way)."""

    def __init__(self, path, fieldnames, dry=False):
        self.dry = dry
        self._file = None
        self._writer = None
        if dry:
            return
        is_new = not path.exists()
        self._file = path.open("a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=fieldnames)
        if is_new:
            self._writer.writeheader()
            self._file.flush()

    def append(self, row):
        if self.dry:
            return
        self._writer.writerow(row)
        self._file.flush()

    def close(self):
        if self._file:
            self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def ledger_row(h, original_name, section, status, final_path):
    return {
        "hash": h,
        "original_name": original_name,
        "section": section,
        "status": status,
        "final_path": final_path,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ─────────────────────────────────────────────────────────────────────────
# Steam screenshot import
# ─────────────────────────────────────────────────────────────────────────

def existing_ref_hashes():
    """Hashes of every image already anywhere under ref/ - loose or already
    filed into a subfolder. import_from_steam() used to dedupe against the
    ledger ONLY, which has a gap: a screenshot gets copied into ref/ during
    the import step, but only gets a ledger row once it's actually been
    classified. If a run is interrupted (Ctrl+C, closed terminal) between
    those two steps, that copy sits in ref/ with no ledger row - so the next
    run's import didn't recognise it as already here and copied the exact
    same screenshot again from Steam under a new "_2"/"_3" name, forever
    accumulating byte-identical duplicates on every interrupted run. Hashing
    everything already under ref/ and folding it into the "already have
    this" set closes that gap - this is the "check the hash, and if it
    matches, don't re-import it" fix."""
    return {file_hash(p) for p in REF.rglob("*")
            if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png")
            and is_valid_image(p)}


def import_from_steam(ledger, dry=False):
    if not STEAM_SCREENSHOTS_DIR.exists():
        print(f"(Steam screenshots folder not found - skipping import: {STEAM_SCREENSHOTS_DIR})")
        return
    known = set(ledger.keys()) | existing_ref_hashes()
    copied = 0
    sources = sorted(p for ext in ("*.jpg", "*.jpeg", "*.png")
                     for p in STEAM_SCREENSHOTS_DIR.glob(ext))
    for src in sources:
        h = file_hash(src)
        if h in known:
            continue
        dest = unique_target(REF, src.stem, src.suffix.lower())
        print(f"IMPORT {src.name} -> {dest.name}")
        if not dry:
            shutil.copy2(src, dest)  # copy only - source is never modified/deleted
        copied += 1
        known.add(h)
    print(f"Imported {copied} new screenshot(s) from Steam.\n")


def is_valid_image(path):
    """True if PIL can actually decode this file. Guards against zero-byte
    or truncated files - these can end up in ref/ if a copy/write gets cut
    off mid-write (e.g. a forced process kill during an old, since-fixed
    Ctrl+C hang). A broken file still has a real, well-defined hash - every
    empty file hashes to the same constant value - so without this check a
    pile of unrelated broken files can look like "duplicates" of each other
    to dedupe_loose_images() below, and one of them can get kept as the
    "original" even though it has no actual image content."""
    try:
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:
        return False


def dedupe_loose_images(dry=False):
    """Collapse byte-identical duplicates among the loose (not yet sorted)
    images directly in ref/ down to one copy each. This cleans up the actual
    mess the import gap above caused - every interrupted run left behind
    exact duplicate copies (same hash, "_2"/"_3" names) of screenshots that
    hadn't been classified yet. The original Steam screenshot is never
    touched by this (or anything else in this script) - only the redundant
    extra copies inside ref/ itself are removed, and only ones that are
    100% byte-identical to a copy that's kept; nothing is ever guessed at.
    Runs every time, not just after a fix, since the same interrupted-run
    gap could theoretically still leave a duplicate or two behind even with
    the import fix (e.g. two images mid-flight when Ctrl+C lands).

    Corrupt/empty files (see is_valid_image) are handled separately, before
    any hash grouping: they're removed outright rather than ever being
    treated as a group's "keeper", since a broken file has nothing worth
    keeping and letting one survive as a stand-in for a hash group is what
    crashed a real run here - every genuinely different broken file happened
    to share the same (empty-file) hash, so the survivor was itself just
    another empty file, not a real screenshot."""
    seen = {}
    removed = 0
    corrupt = 0
    loose = sorted(p for p in REF.iterdir()
                   if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    for p in loose:
        if not is_valid_image(p):
            size = p.stat().st_size
            print(f"CORRUPT {p.name} ({size} bytes, unreadable) - removing"
                  f"{' [dry-run]' if dry else ''}")
            if not dry:
                p.unlink()
            corrupt += 1
            continue
        h = file_hash(p)
        if h in seen:
            print(f"DUPLICATE {p.name} (identical to {seen[h].name}){' [dry-run]' if dry else ''}")
            if not dry:
                p.unlink()
            removed += 1
        else:
            seen[h] = p
    if removed or corrupt:
        print(f"Removed {removed} duplicate + {corrupt} corrupt loose image(s).\n")
    return removed + corrupt


def reconcile_sorted_files(ledger, ledger_writer, dry=False):
    """Startup reconciliation pass over every file already filed away into a
    subfolder under ref/ (Marks/, Classes - ... - A - ..., etc.) - the
    already-sorted counterpart to dedupe_loose_images above, which only ever
    looks at *loose* top-level files still waiting to be classified. Loose
    files are deliberately excluded here (p.parent == REF, skipped) - a
    loose file's hash being absent from the ledger is exactly what tells the
    main loop below it still needs to be classified and moved, so adding it
    to the ledger *before* that would make the whole file invisible to
    sorting without ever actually sorting it.

    Two jobs, run in this order since the second one needs the first one's
    groups to have already been deduped:
      1. Collapse byte-identical duplicates found anywhere across the sorted
         subfolders (not just within the same one - two different sections
         could each independently end up holding a copy of the exact same
         screenshot) down to one - the oldest by creation time wins ("Date
         created" in Explorer; NOT mtime, which instead reflects whenever a
         file was last touched/moved and would pick whichever copy happened
         to be renamed most recently rather than whichever has actually
         existed the longest).
      2. Whatever single file now represents that hash gets a ledger row
         added if it doesn't already have one. This closes a real gap: a
         file can be sitting correctly sorted on disk (e.g. filed by hand,
         or left over from before the ledger existed) while being totally
         invisible to every hash-ledger check elsewhere in this script,
         since load_ledger() only ever knows about hashes that were present
         the moment a row got written for them.

    Mutates `ledger` in place as rows get added, so the caller's own
    in-memory dict - and every check against it later in the same run
    (import dedup, the main sort loop) - immediately sees the result without
    needing a fresh load_ledger() disk read."""
    by_hash = {}
    for p in REF.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            continue
        if p.parent == REF or p.parent == UNSORTED_DIR:
            continue  # loose, or sitting in the not-actually-sorted catch-all
                      # bucket (see UNSORTED_DIR's own comment) - not this
                      # function's job, both get classified in main() instead
        if not is_valid_image(p):
            continue  # corrupt sorted file - left alone, same as a corrupt loose one
        by_hash.setdefault(file_hash(p), []).append(p)

    added = 0
    removed = 0
    for h, paths in by_hash.items():
        paths.sort(key=lambda p: p.stat().st_ctime)
        keeper = paths[0]
        for dup in paths[1:]:
            print(f"DUPLICATE (sorted) {dup.relative_to(REF)} (identical to "
                  f"{keeper.relative_to(REF)}){' [dry-run]' if dry else ''}")
            if not dry:
                dup.unlink()
            removed += 1
        if h not in ledger:
            print(f"LEDGER+ {keeper.relative_to(REF)} (already sorted, wasn't in the ledger yet)"
                  f"{' [dry-run]' if dry else ''}")
            row = ledger_row(h, keeper.name, keeper.parent.name, "sorted (reconciled)",
                              str(keeper.relative_to(REF)))
            if not dry:
                ledger_writer.append(row)
            ledger[h] = row
            added += 1

    if added or removed:
        print(f"Reconciled sorted files: added {added} missing ledger row(s), "
              f"removed {removed} duplicate(s).\n")
    return added, removed


# ─────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────

def _parse_workers(argv):
    """--workers N, defaulting to CPU count (capped at 16). Each worker
    spends nearly all its time waiting on its own tesseract.exe subprocess
    (releasing the GIL for that whole wait - see the module docstring's
    Concurrency section), so this scales close to linearly with real cores
    up to the point disk I/O (every OCR call round-trips through a temp
    file) or Tesseract's own internal threading starts eating the gains.
    Was capped at 8 with no real machine to benchmark past that point - 16
    gives real headroom on the 8+ physical-core desktops/laptops common
    today without letting a very high core count machine spin up so many
    concurrent tesseract processes that disk I/O itself becomes the
    bottleneck. Pass --workers N explicitly to go higher still (or lower,
    e.g. --workers 1 for the old single-threaded behaviour)."""
    for i, arg in enumerate(argv):
        if arg == "--workers" and i + 1 < len(argv):
            try:
                return max(1, int(argv[i + 1]))
            except ValueError:
                pass
        if arg.startswith("--workers="):
            try:
                return max(1, int(arg.split("=", 1)[1]))
            except ValueError:
                pass
    return min(os.cpu_count() or 4, 16)


def main():
    dry = "--dry-run" in sys.argv
    no_import = "--no-import" in sys.argv
    workers = _parse_workers(sys.argv)

    ledger = load_ledger()

    # LedgerWriter now wraps the whole run, not just the classify loop below
    # - reconcile_sorted_files() needs to be able to append rows too, and it
    # has to run first (before import/dedupe/classify), so it can't share a
    # separate, later-opened writer the way the classify loop used to have
    # all to itself.
    with LedgerWriter(LEDGER_PATH, LEDGER_FIELDS, dry=dry) as ledger_writer:
        reconcile_sorted_files(ledger, ledger_writer, dry=dry)

        if not no_import:
            import_from_steam(ledger, dry=dry)
            ledger = load_ledger()

        dedupe_loose_images(dry=dry)

        images = sorted(p for p in REF.iterdir()
                        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png"))
        # UNSORTED_DIR is the manually-swept "not actually sorted yet"
        # bucket (see its own comment) - included here as a second source
        # of images to classify, same as ref/'s own loose top level.
        if UNSORTED_DIR.is_dir():
            images += sorted(p for p in UNSORTED_DIR.iterdir()
                             if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png"))
        if not images:
            print("No loose images found in", REF)
            return

        # Hashing is cheap (just reading bytes off disk) compared to OCR, so
        # it stays single-threaded/up-front - this is also where "already
        # sorted before" gets filtered out, before any OCR work is even
        # considered.
        #
        # A hash already in the ledger normally means "permanently done,
        # never retry" (see the module docstring) - that's the right call
        # for a file sitting loose in ref/'s own top level, since nothing
        # else will ever move it out of the way on its own. But a
        # needs_review file that's since been swept into UNSORTED_DIR by
        # hand is exactly the opposite signal: someone put it there BECAUSE
        # it still needs sorting, not because it's settled - so those get a
        # fresh classification attempt every run instead of being skipped
        # forever on their original needs_review row (e.g. logged before a
        # detector bug fix, like the perks/characters fallthrough above).
        hashed = [(p, file_hash(p)) for p in images]
        to_process = [(p, h) for p, h in hashed
                      if h not in ledger
                      or (ledger[h]["status"] == "needs_review" and p.parent == UNSORTED_DIR)]
        skipped_count = len(hashed) - len(to_process)

        total = len(to_process)
        if total == 0:
            print(f"Nothing new to sort - {skipped_count} loose image(s) already in the ledger.")
            return

        print(f"{total} image(s) to classify ({skipped_count} already in the ledger, skipped) "
              f"- {workers} worker(s).\n")

        prewarm_caches()

        sorted_count = 0
        review_count = 0
        done = 0

        # Deliberately NOT `with ThreadPoolExecutor(...) as executor:` - its
        # __exit__ calls shutdown(wait=True), which blocks Ctrl+C exactly
        # like the atexit hook unregistered above. Shut it down manually
        # instead, non-blocking, so an interrupt can actually take effect.
        executor = ThreadPoolExecutor(max_workers=workers)
        # classify_one() only reads the image and runs the (pure,
        # side-effect-free) detectors - the actual file move and CSV
        # write happen below, back on the main thread, one at a time,
        # as each thread's result comes in. That's what keeps this
        # safe to parallelize: no two threads ever touch the
        # filesystem or the ledger at the same moment.
        future_to_hash = {executor.submit(classify_one, p): h for p, h in to_process}

        try:
            for future in as_completed(future_to_hash):
                h = future_to_hash[future]
                img_path, matched_section, folder, base, took, worker = future.result()
                done += 1
                progress = f"[{done}/{total}] [w{worker}]"

                if matched_section is None:
                    print(f"{progress} ?? {img_path.name}: no section matched - "
                          f"left in place, logged for manual review (took {took:.1f}s)", flush=True)
                    ledger_writer.append(ledger_row(h, img_path.name, "", "needs_review", ""))
                    review_count += 1
                    continue

                folder.mkdir(exist_ok=True)
                base = base or img_path.stem
                tgt = unique_target(folder, base, img_path.suffix.lower())
                print(f"{progress} OK [{matched_section}] {img_path.name} -> "
                      f"{tgt.relative_to(REF)} (took {took:.1f}s)", flush=True)
                if not dry:
                    img_path.rename(tgt)
                ledger_writer.append(ledger_row(h, img_path.name, matched_section, "sorted",
                                                 str(tgt.relative_to(REF))))
                sorted_count += 1
        except KeyboardInterrupt:
            # cancel_futures=True drops anything not yet started; whatever
            # a worker is mid-OCR on right now keeps running in the
            # background, but we don't wait around for it - everything
            # logged up to this point is already safely flushed to disk
            # (see LedgerWriter), so nothing already done is lost.
            executor.shutdown(wait=False, cancel_futures=True)
            print(f"\nInterrupted - {sorted_count} sorted, {review_count} need manual review "
                  f"so far ({done}/{total} classified before stopping). Safe to resume any time; "
                  f"nothing already logged will be reprocessed.", flush=True)
            # sys.exit() only unwinds the main thread - it does NOT terminate
            # the process while other threads are still alive. That's a
            # separate mechanism from the atexit hook unregistered above:
            # CPython's own interpreter shutdown waits for every non-daemon
            # thread to finish before the process actually closes, so with
            # workers still mid-OCR in the background, sys.exit(1) here would
            # print this message and then appear to hang for however long
            # those in-flight images take (up to the 60-100+ Tesseract calls
            # per unmatched image mentioned in ref/CLAUDE.md - tens of
            # seconds). os._exit() skips all of that and kills the process
            # immediately. That's safe specifically because everything
            # already logged is flushed to disk row-by-row (LedgerWriter) -
            # there's no unflushed state an abrupt exit could lose.
            os._exit(1)

        executor.shutdown(wait=False)

    print(f"\n{sorted_count} sorted, {review_count} need manual review, "
          f"{skipped_count} already in the ledger (skipped).")


if __name__ == "__main__":
    # Running this by double-clicking (no terminal already open) closes the
    # console window the instant the script ends, before there's any chance
    # to read the summary - or a crash's traceback. Pausing on the way out
    # fixes that. This is skipped on Ctrl+C specifically: that path already
    # calls os._exit(1) inside main() (see the comment there), which is a
    # hard process kill that bypasses `finally` entirely - intentional,
    # since Ctrl+C only happens when someone's already at the keyboard
    # watching it run, so there's nothing to lose by not pausing there too.
    try:
        main()
    finally:
        input("\nPress Enter to close...")
