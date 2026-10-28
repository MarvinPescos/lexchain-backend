"""
Pre-OCR cleaning of a rendered page (an RGB uint8 array).

Surya 2 turns any region holding handwriting or a stamp into a bare `<img/>`
and drops the printed text inside it, so signatures, seals and stamps come off
the page before Marker reads it. Detection is ink colour plus the boxes
Marker's first pass could not read; there is no detection model.
"""

import math

import numpy as np

# x0, y0, x1, y1 in pixels, end-exclusive.
Box = tuple[int, int, int, int]

# Coloured ink: clearly saturated, clearly chromatic, and not paper. Dark print
# can reach a high HSV saturation while its chroma stays near 0–4, which is why
# chroma is checked too.
_INK_MIN_SATURATION = 35
_INK_MIN_CHROMA = 25
_PAPER_MIN_CHANNEL = 245

# Faint stroke edges are caught by growing the mask, but never into neutral
# print, so letters under a signature are not eaten.
_GROW_PX = 3
_PRINT_MAX_SATURATION = 25
_PRINT_MAX_VALUE = 200

# Inside a removed region, light pixels are the pale ghost a seal leaves.
_GHOST_MIN_VALUE = 200

# Strokes of one signature or seal are rarely connected; ink this close
# together is one region. Specks smaller than a region are scan noise.
_GROUP_TILE_PX = 12
_MIN_REGION_PX = 30

# Signature line: a run of dark pixels across most of the block.
_DARK_MAX_GRAY = 220
_LINE_MIN_FRACTION = 0.6


def remove_coloured_ink(page: np.ndarray) -> tuple[np.ndarray, list[Box]]:
    """Whiten seals, stamps and coloured signatures. Returns the cleaned page
    and the box of each removed region."""
    saturation, value, chroma, low = _hsv(page)
    ink = (
        (saturation >= _INK_MIN_SATURATION)
        & (chroma >= _INK_MIN_CHROMA)
        & (low <= _PAPER_MIN_CHANNEL)
    )
    if not ink.any():
        return page.copy(), []

    print_ = (saturation < _PRINT_MAX_SATURATION) & (value < _PRINT_MAX_VALUE)
    removed = _grow(ink, _GROW_PX) & ~print_

    cleaned = page.copy()
    regions: list[Box] = []
    for component in _components(removed):
        if np.count_nonzero(component & ink) < _MIN_REGION_PX:
            continue
        ys, xs = np.nonzero(component)
        box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
        x0, y0, x1, y1 = box
        ghost = value[y0:y1, x0:x1] >= _GHOST_MIN_VALUE
        cleaned[component] = 255
        cleaned[y0:y1, x0:x1][ghost] = 255
        regions.append(box)
    return cleaned, regions


def erase_signature(page: np.ndarray, box: Box) -> Box | None:
    """
    White out the handwriting above the signature line in `box`, in place.

    The line is the lowest row whose longest dark run spans most of the box;
    everything from the top of the box down to it goes, so the printed name
    below stays. Without such a line nothing is touched. Returns the erased box.
    """
    height, width = page.shape[:2]
    x0, y0 = max(0, box[0]), max(0, box[1])
    x1, y1 = min(width, box[2]), min(height, box[3])
    if x1 <= x0 or y1 <= y0:
        return None

    dark = _gray(page[y0:y1, x0:x1]) < _DARK_MAX_GRAY
    min_run = math.ceil((x1 - x0) * _LINE_MIN_FRACTION)
    for row in range(dark.shape[0] - 1, -1, -1):
        if _longest_run(dark[row]) >= min_run:
            page[y0 : y0 + row + 1, x0:x1] = 255
            return (x0, y0, x1, y0 + row + 1)
    return None


def _hsv(page: np.ndarray):
    rgb = page.astype(np.int32)
    high = rgb.max(axis=2)
    low = rgb.min(axis=2)
    chroma = high - low
    saturation = chroma * 255 // np.maximum(high, 1)
    return saturation, high, chroma, low


def _gray(page: np.ndarray) -> np.ndarray:
    rgb = page.astype(np.int32)
    return (rgb[..., 0] * 299 + rgb[..., 1] * 587 + rgb[..., 2] * 114) // 1000


def _grow(mask: np.ndarray, radius: int) -> np.ndarray:
    """Square dilation, done as row then column shifts."""
    rows = mask.copy()
    for d in range(1, radius + 1):
        rows[d:] |= mask[:-d]
        rows[:-d] |= mask[d:]
    grown = rows.copy()
    for d in range(1, radius + 1):
        grown[:, d:] |= rows[:, :-d]
        grown[:, :-d] |= rows[:, d:]
    return grown


def _components(mask: np.ndarray):
    """Split `mask` into groups of nearby pixels, by flood-filling a coarse
    grid of tiles so strokes a few pixels apart land in one group."""
    tile = _GROUP_TILE_PX
    height, width = mask.shape
    rows, cols = -(-height // tile), -(-width // tile)
    padded = np.zeros((rows * tile, cols * tile), dtype=bool)
    padded[:height, :width] = mask
    tiles = padded.reshape(rows, tile, cols, tile).any(axis=(1, 3))

    seen = np.zeros_like(tiles)
    for start in zip(*np.nonzero(tiles)):
        if seen[start]:
            continue
        group = np.zeros_like(tiles)
        stack = [start]
        seen[start] = True
        while stack:
            r, c = stack.pop()
            group[r, c] = True
            for nr in range(max(0, r - 1), min(rows, r + 2)):
                for nc in range(max(0, c - 1), min(cols, c + 2)):
                    if tiles[nr, nc] and not seen[nr, nc]:
                        seen[nr, nc] = True
                        stack.append((nr, nc))
        pixels = np.repeat(np.repeat(group, tile, axis=0), tile, axis=1)
        yield pixels[:height, :width] & mask


def _longest_run(row: np.ndarray) -> int:
    edges = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)
    return int((ends - starts).max()) if len(starts) else 0
