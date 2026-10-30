# ref/ - screenshot reference library

This folder holds every screenshot used as source material for the site,
already organised into subfolders by section (Classes, Class Perks,
Fabricator, Store Pages, Bundles, Difficulty, Characters, etc.). Loose
files sitting directly in `ref/` (not in a subfolder) are unsorted and
waiting to be filed.

## The golden rule

**Every time a new screenshot-sorting need comes up - a new section, a
screen the script doesn't recognise yet, or a detector that's guessing
wrong - extend `sort-screenshots.py` to handle it. Don't just sort the
file by hand and move on.** A one-off manual sort solves today's file;
teaching the script solves every future file like it, and keeps this
folder self-maintaining instead of accumulating manual work that has to
be redone by memory every time. If you do have to sort something by hand
because extending the script isn't practical right now, still update this
file with a note about the gap.

## Running it

```
python sort-screenshots.py                # import from Steam, then auto-sort everything loose in ref/
python sort-screenshots.py --no-import     # skip the Steam import step, just sort what's already here
python sort-screenshots.py --dry-run       # preview only - copies/moves/writes nothing, ledger untouched
python sort-screenshots.py --workers 8     # classify N images concurrently (default: CPU count, max 8)
```

Each image is classified (the OCR-heavy part) in a thread pool - `pytesseract`
shells out to `tesseract.exe` as a subprocess and blocks waiting for it,
which releases Python's GIL, so this genuinely runs multiple images' OCR in
parallel rather than just interleaving. File moves and the CSV ledger write
still happen one at a time on the main thread as each result comes back, so
there's no race risk. Output shows a running `[X/Z]` count so you can see how
far a batch has gotten. If a run gets interrupted partway (closed terminal,
Ctrl+C), everything already logged is safe and won't be reprocessed - just
run the script again to pick up where it left off.

Requirements (once): `pip install pillow pytesseract`, plus a local
Tesseract OCR install (https://github.com/UB-Mannheim/tesseract/wiki).
If `tesseract.exe` isn't on PATH, point `TESSERACT_EXE` near the top of
the script at it - that's the only machine-specific setting in the file,
and it should stay exactly where it is (don't move/rename it; other
tooling and this doc both assume it's there).

## What it does, each run

1. **Import.** Unless `--no-import` is passed, copies (never moves,
   never deletes) any screenshot from
   `C:\Program Files (x86)\Steam\userdata\110099628\760\remote\1097840\screenshots`
   that isn't already in the ledger by hash into `ref/`. This is the
   *only* place screenshots are ever read from automatically - nothing
   else on the machine is touched. If that folder doesn't exist (e.g.
   you're not on the machine with the Steam library), the script just
   prints a note and skips the step instead of erroring.
2. **Sort.** Every loose image directly in `ref/` (not already in a
   subfolder) that isn't already in the ledger gets run through each
   section detector in `SECTIONS` (see below) in order. The first one
   that both recognises the screen *and* can place it goes on to build a
   descriptive filename and move the file into the right subfolder.
3. **Log.** Every file the script ever touches - sorted or not - gets a
   row in `_sort-log.csv` keyed by its SHA-256 hash. On the next run,
   anything already in the ledger is skipped outright, whether it was
   sorted or flagged `needs_review`. This is what makes it safe to
   re-run the script (or re-import the same Steam folder) over and over
   without ever double-processing a file, and it's what makes a manual
   review "stick" - once you've sorted a `needs_review` file by hand,
   its ledger row is what remembers that, permanently. Nothing will
   re-pick it up even after a detector improves later.

## The ledger (`_sort-log.csv`)

Columns: `hash, original_name, section, status, final_path, timestamp`.

- `status` is `sorted` (auto-filed with confidence) or `needs_review`
  (left in place in `ref/`, a person needs to look at it and file it by
  hand). There's no in-between status - see "Design philosophy" below
  for why.
- To actually resolve a `needs_review` file: move/rename it into the
  right subfolder yourself. The ledger row already marks it as dealt
  with, so the script will never touch that file again regardless of
  what you do with it.
- If you ever want a detector to get a *second* try at a batch of
  `needs_review` files (e.g. after fixing a bug), you'd need to remove
  their rows from the CSV first - the script has no automatic way to
  "retry" a review row on purpose, since that's what makes review status
  durable.

## Design philosophy: favor leaving it alone over guessing wrong

A wrong auto-sort (wrong class, wrong name, wrong folder) is worse than
one more file sitting in `needs_review`, because a wrong sort is much
harder to notice later. Every detector in this script is written to that
standard:

- `build_characters` **always** returns "no name" (routes to review) -
  nothing on that screen ever identifies which character is selected, so
  there is no confident answer to give, ever.
- `build_perks` **always** returns "no name" too, even though it makes a
  best-effort class guess for a friendlier console message. The perk
  D-pad overlay never prints a class name on screen, so the guess comes
  from fuzzy-matching the 4 visible perk names against `_classes/*.md` -
  and that data has drifted out of date before (see "Known gaps" below),
  so it's treated as informational only, never as something to file by.
- `build_bundle_item` only reuses an *existing* `Bundles - NN - <name>`
  folder (fuzzy name match); if no matching folder exists yet, it backs
  off to `needs_review` instead of inventing a new numbered folder,
  since the numbering is meaningful and shouldn't be guessed.
- Fabricator prices are only ever reported when the OCR'd digit string
  matches a real price from `_data/fabricator.yml` in both length and
  fuzzy-ratio - a partial or truncated read (e.g. "400" where the real
  price is "3,400") is dropped rather than reported as-is.
- The class-name fuzzy matcher (`fuzzy_class_match`) requires an OCR'd
  text run to cover at least ~75% of the candidate class name's length
  before accepting a fuzzy match. This was added after a real bug: a
  Horde end-of-wave scoreboard screen (listing players' classes in a
  table - "DEMOLITIONS", "ANCHOR", "ARCHITECT", etc.) produced a 4-letter
  OCR fragment ("ETER") that is a literal substring of "VETERAN" and
  scored just above the fuzzy-match threshold, mis-filing the scoreboard
  screenshot as a Veteran skill-card screen. If you extend the fuzzy
  matching anywhere in this script, keep that failure mode in mind -
  short fragments can score deceptively high against long names purely
  because there's little room for them to disagree.

If you add a new section, follow the same standard: if a detector can't
be reasonably confident, have it return `None`/`(None, None)` rather
than a best guess.

## Sections it currently recognises (`SECTIONS`, in check order)

| Section | Trigger | Destination | Reliability |
|---|---|---|---|
| `out_of_scope` | Title/header OCR match for one of five known non-reference screens: "TOUR REWARDS", "MEDALS", "OVERALL PROGRESS" (Tour overview), "MATCH MVP", or "MY SQUAD" (friends list) | `Not Used - Profile & Progression` | Checked first - these are common, cheap to rule out, and unlike every other section here a positive match is auto-filed with **no** `needs_review` fallback, because there's no ambiguity to protect against: each marker is both confidently identified (exact on-screen title text) and confidently irrelevant to the site (profile/social/progression screens, not class/card/perk/fabricator data). Resolves the "Honor / friends-list screen" known gap below |
| `scoreboard` | Post-wave/pause Horde scoreboard - either the results table's own column headers (ASSISTS/REVIVES/etc, or MASTER MODIFIERS), or a title reading "HORDE ... ON <map>" | `Horde Scoreboard` | Checked first, since it's cheap and otherwise this table's player CLASS column can fool `classes` (see the fuzzy-match note above). The title-regex fallback exists because the compact end-of-wave popup variant is small/blurry enough that the table's own column headers often don't OCR cleanly on their own - between the two signals this caught roughly 1 in 6 of a real backlog of `needs_review` screenshots in testing; the rest were plain gameplay shots with no menu at all, not a detector gap (see "Known gaps" below) |
| `classes` | Skill-card grid header, or "SKILL UNLOCKS" level-up screen | `Classes - ... - A - Card Unlock Levels` / `- B - Card Upgrades` | Proven - regression-tested against all 19 classes on both screen types |
| `perks` | Top-left HUD (WAVE/SCORE) + 2+ readable D-pad quadrants | `Class Perks` | Detection is reliable; filing is **always** `needs_review` by design (see above) |
| `fabricator` | In-world "NAME LVn UPGRADE:" or "NAME LVn BUILD:" prompt | `Fabricator` | Reasonably safe - favors omitting the price/level over reporting a wrong one; camera angle can still defeat OCR entirely (falls to `needs_review`). Originally only matched "UPGRADE"; extended to also match "BUILD" since a not-yet-built fortification's prompt uses that word instead |
| `fabricator_menu` | Full FABRICATOR menu grid ("FABRICATOR" header + selected tile's "NAME LVn" caption + "COST" line) | `Fabricator` | Filename is `<Name> LVn (Menu <price>)` - the "Menu" tag distinguishes it from the in-world prompt's price, since this price is the *live/discounted* on-screen number and is deliberately **not** cross-checked against `_data/fabricator.yml`'s base price table (an active discount modifier legitimately makes them differ) |
| `map` | A map name (fuzzy-matched against every map in `_data/maps.yml`) read from one of several candidate caption positions, plus a low-saturation "does this look like a schematic" heuristic to guess hero vs. layout | `Maps` | **Unvalidated** - written with no real sample of either target screen (Gears 5's loading/map-select screen for the hero shot, its in-match top-down map overlay for the layout shot) to test the caption boxes or the saturation threshold against. Always routes here regardless of confidence - this script never writes into `images/maps/` itself, so `ref/Maps` *is* the manual-review folder; filenames are tagged `(hero/layout candidate - MISSING on site)` or `(... - existing on site, verify before replacing)` by checking `images/maps/<slug>.jpg` / `-layout.jpg` directly, so a person can prioritize genuine gaps over maps that already have art. See "Known gaps" below before trusting this one |
| `marks` | "MARKS" header (top-left, same Q/E tab bar as CLASSES/WEAPONS) | `Marks` | Checked before `classes` specifically because it can otherwise fool `detect_class_by_cards`'s fuzzy name matching (a real bug hit while building this - see "Known gaps" below). Header detection is solid (validated against a 40-file sample with zero false positives); the 3x2 card-name grid OCR is best-effort like `bundle_item`/`store_list` - each card's caption reliably ends in the literal word "MARK", which the detector anchors on, but a card can still come back unread (falls back to a generic filename) rather than risk a wrong name |
| `bundle_item` | Single-item preview with "PURCHASE"/"SELECT" footer | `Bundles - NN - <name>` (must already exist) | Name/caption OCR is the least reliable part of this script - expect a fair number of `needs_review` hits, verify before trusting a "sorted" bundle-item filename |
| `store_list` | "STORE" header + red-glow-selected card | `Store Pages` | Same caveat as `bundle_item` - caption OCR over the store grid is noisy |
| `difficulty` | "DIFFICULT" header (in-match tab), or the SELECT DIFFICULTY wheel-select screen as a fallback | `Difficulty` | Reliable for the in-match tab (tier + caption both extracted). The wheel-select screen doesn't share that screen's tier/caption layout, so on that fallback path `tier`/`name` are deliberately left `None` and the file is just filed generically into `Difficulty` - same "favor omission over guessing wrong" standard as everywhere else in this script |
| `characters` | "CHARACTER"/"HARACTER" header | `Characters` | Detection is reliable; filing is **always** `needs_review` by design (no name ever shown) |

Anything that matches none of these is left in place and logged
`needs_review` with an empty `section`.

## Known gaps / next things worth teaching the script

- **`map` is untested against real screenshots.** It was added on request
  (crop/file map + minimap/layout screenshots for the Maps page) without a
  sample of either target screen available to calibrate against - the
  caption-box positions it tries and the `avg_sat < 40` schematic-vs-hero
  threshold are both best-effort guesses, not verified. First time a real
  loading-screen or in-match map-overlay screenshot comes through: check
  what actually lands in `ref/Maps/`, confirm the hero/layout guess and the
  read map name are both right, and tighten the boxes/threshold from there.
  Also worth revisiting once real captures exist: this section only ever
  files the *whole* screenshot (no destructive crop is attempted) since
  there was nothing to calibrate a crop box against either - see the
  module comment above `section_map` in `sort-screenshots.py`.
- **Store bundle-item and bundle-list name OCR** are the weakest
  detectors here - garbled captions are common even after two rounds of
  calibration. If you're sorting a batch of Store screenshots by hand
  anyway, that's a good time to also improve these crop boxes/thresholds
  rather than just filing the images.
- **`section_fabricator` can mistake a REPAIR prompt for an UPGRADE/BUILD
  prompt.** Found via a real bad data point: `Weapon Locker LV4 (Upgrade
  4'250).jpg` (since renamed to `Weapon Locker LV4 (Repair -4250, not an
  upgrade).jpg`) was actually a repair interaction - a floating "-4,250"
  Energy deduction near the fortification, not a genuine
  "WEAPON LOCKER LV4 UPGRADE: 4,250" prompt - but it still produced a
  confident "sorted" filename with a price. Root cause: the detector's
  fallback match (`any(n in candidate for n in fort_names_upper)`) accepts
  a crop just for containing a fortification's name, even with no
  "UPGRADE"/"BUILD" text present; a real price only gets attached if
  `is_upgrade or is_build` later, but a stray "-NNNN" floating deduction
  near the name in the same crop can apparently still get OCR'd in and
  digit-match against the (in this case, itself wrong) price table. This
  bad screenshot and the wrong `_data/fabricator.yml` LV4 entries for
  Weapon Locker/Forge likely reinforced each other when the data was first
  entered. Confirmed correct behavior via other real LV4 captures for both
  (`Weapon Locker LV4.jpg`, `Forge LV4 (Perk +30%).jpg`): once a
  fortification is at LV4, only a repair prompt is available, same as
  every other fortification - LV4 is never a real purchase for any of
  them. If extending this detector, tighten the match to require
  "UPGRADE:"/"BUILD:" immediately following the fortification name/level,
  not just present anywhere in the crop.
- **Perk data staleness**: at least one class's `perks:` block in
  `_classes/*.md` has drifted out of date relative to the current
  in-game perks, which is part of why perk-screen class guesses are
  never trusted for auto-filing. If you notice a class's perk names look
  wrong while reviewing `Class Perks`, fix the source `_classes/*.md`
  file, not just the sort.
- **Gamemodes, Weapons, and other existing top-level `ref/` folders**
  (see the ones without a `SECTIONS` entry - Horde Scoreboard was the
  first of these to get one, see above) aren't auto-sorted yet - they
  were organised by hand before this script existed. If new screenshots
  for any of those start showing up regularly, that's a signal to add a
  new section for it here rather than keep sorting them by hand.
- **Don't assume a `needs_review` backlog is just gameplay noise - check
  it.** Earlier review of a real backlog wrongly wrote off most of the
  non-scoreboard remainder as plain in-round action shots with nothing
  reference-worthy in them. That was wrong: closer review turned up a
  genuine Fabricator MENU grid screen (distinct from the in-world
  "UPGRADE" prompt `fabricator` already handled - now `fabricator_menu`,
  see above), fortification prompts using "BUILD" instead of "UPGRADE"
  (now also matched by `fabricator`), and the SELECT DIFFICULTY
  wheel-select screen (now a fallback path on `difficulty`). The
  takeaway: if a screenshot displays a *type* of data a folder already
  exists for (fabricator prices, difficulty, etc.) even on an unfamiliar
  screen layout, that's a detector gap worth closing, not noise -
  route it into the *existing* folder rather than inventing a new one.
  Genuinely-plain gameplay shots (pure HUD, no menu/prompt of any kind)
  still have no reference-worthy pattern to extract and should stay in
  `needs_review` for a person to keep or delete by hand - just don't
  assume that's what a file is without actually looking.
- **`marks` was added after a screenshot got mis-filed as a Marksman skill-card screen.** The Marks grid's card captions and background text apparently OCR close enough to some skill card names that `detect_class_by_cards`'s fuzzy matcher (ratio 0.75) picked up a false positive - moving `marks` ahead of `classes` in `SECTIONS` fixed it (matched screens now never reach the `classes` detector at all), the same fix pattern `scoreboard` already uses for the same failure mode against its own results-table class column. If another section ever turns out to fool `classes`, check whether it needs the same reordering rather than tightening the fuzzy-match ratio again.
- **No real Iron (premium currency) screenshot has turned up yet.** The site owner remembered capturing both Marks and Iron screens; only Marks screens (see above) were actually found in a full scan of the loose backlog. No detector exists for Iron yet - there's nothing to calibrate boxes/thresholds against, same situation `map` was in before real captures existed (see that entry above). First real Iron screenshot (a wallet/balance readout, or the Store's "GET MORE IRON" screen) should get its own section here, filed into a new `Iron` folder.
- ~~**"Honor" / friends-list screen**~~ Resolved - now caught by
  `out_of_scope` ("MY SQUAD" header match) along with Tour Rewards,
  Medals, Tour Overview, and Match MVP screens, all filed into
  `Not Used - Profile & Progression`.
- **The Steam import step can only be exercised on a machine that
  actually has that Steam userdata folder** (i.e. not in a sandboxed
  dev/test environment) - if you're verifying a change to
  `import_from_steam`, you'll need to test it locally, or pass
  `--no-import` and test the sort logic against files already in `ref/`.
