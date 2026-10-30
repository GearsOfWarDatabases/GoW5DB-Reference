#!/usr/bin/env python3
"""Extract Gears 5 skill cards from screenshots and remove the baked-in text.

Usage:
    python clean_cards.py <screenshot.jpg> [more screenshots...]

For each screenshot, detects/crops card rectangles (or use --grid for the
standard 4x3 card-grid layout), removes the name/description text via
stroke-based inpainting, wipes the footer bar, and writes PNGs to
./cards-clean/.

Best sources: cards WITHOUT the orange selection border, without the
check / "!" badges, and without the "READY TO UPGRADE!" banner.

Requires: pip install opencv-python-headless numpy
"""
import cv2
import numpy as np
import os
import sys

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cards-clean')

# Standard 1920x1080 "CLASS'S SKILL CARDS" grid: 4 columns x 3 rows.
# (col_x, row_y) top-left corners + card size. Row 1 is partially cut by the
# header on some captures; row 3 partially covered by the HUD - prefer row 2.
GRID_COLS = [72, 335, 598, 861]          # x of each column  (col width ~263)
GRID_ROWS = [-60, 300, 690]              # y of each row     (card height ~372)
CARD_W, CARD_H = 263, 372


def clean_card(card):
    """Remove name/description text + footer UI from a cropped card,
    touching as few pixels as possible - only detected strokes/glyphs are
    inpainted, everything else keeps its original pixels."""
    c = card.copy()
    h, w = c.shape[:2]
    g = cv2.cvtColor(c, cv2.COLOR_BGR2GRAY)

    # Thin bright strokes (text) via top-hat; solid bright art survives.
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 11))
    tophat = cv2.morphologyEx(g, cv2.MORPH_TOPHAT, k)

    # Text zone (name + description band): stroke detection only.
    mask = np.zeros((h, w), np.uint8)
    tz = (slice(int(h * 0.50), int(h * 0.86)), slice(int(w * 0.06), int(w * 0.94)))
    mask[tz] = (tophat[tz] > 50).astype(np.uint8) * 255

    # Footer band: also stroke/bright-element detection instead of a blanket
    # wipe - keeps the frame texture, only lifts diamonds/numbers/bar lines.
    fz = (slice(int(h * 0.855), int(h * 0.97)), slice(int(w * 0.03), int(w * 0.97)))
    fg = g[fz].astype(int)
    mask[fz] = ((tophat[fz] > 35) | (fg > 110)).astype(np.uint8) * 255

    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1)
    return cv2.inpaint(c, mask, 3, cv2.INPAINT_TELEA)


def main():
    files = [a for a in sys.argv[1:] if not a.startswith('-')]
    if not files:
        print(__doc__)
        return
    os.makedirs(OUT_DIR, exist_ok=True)
    for f in files:
        img = cv2.imread(f)
        if img is None:
            print('skip (unreadable):', f)
            continue
        base = os.path.splitext(os.path.basename(f))[0]
        n = 0
        for r, y in enumerate(GRID_ROWS):
            for col, x in enumerate(GRID_COLS):
                if y < 0 or y + CARD_H > img.shape[0]:
                    continue  # cut-off row
                card = img[y:y + CARD_H, x:x + CARD_W]
                out = clean_card(card)
                n += 1
                path = os.path.join(OUT_DIR, f'{base} - r{r + 1}c{col + 1}.png')
                cv2.imwrite(path, out)
        print(f'{f}: {n} cards -> {OUT_DIR}')


if __name__ == '__main__':
    main()
