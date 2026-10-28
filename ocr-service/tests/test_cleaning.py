"""Pre-OCR cleaning: coloured ink and handwriting come off, printed text stays."""

import numpy as np

from ocr_service.cleaning import erase_signature, remove_coloured_ink

WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
BLUE = (30, 60, 200)


def page(height: int = 200, width: int = 400) -> np.ndarray:
    return np.full((height, width, 3), 255, dtype=np.uint8)


def printed_text(img: np.ndarray, top: int, bottom: int, left: int, right: int) -> np.ndarray:
    """Letter-like black bars with gaps, the shape of a printed line."""
    mask = np.zeros(img.shape[:2], dtype=bool)
    for x in range(left, right, 12):
        mask[top:bottom, x : x + 7] = True
    img[mask] = BLACK
    return mask


def test_blue_stroke_over_text_is_removed_and_text_survives():
    img = page()
    text = printed_text(img, 80, 100, 40, 360)
    stroke = np.zeros(img.shape[:2], dtype=bool)
    for x in range(150, 300):  # a slanted blue signature crossing the text
        y = 60 + (x - 150) // 3
        stroke[y : y + 4, x] = True
    img[stroke] = BLUE

    cleaned, regions = remove_coloured_ink(img)

    assert (cleaned[stroke] == WHITE).all()
    untouched_text = text & ~stroke
    assert (cleaned[untouched_text] == BLACK).all()
    assert len(regions) == 1
    x0, y0, x1, y1 = regions[0]
    assert x0 <= 150 and x1 >= 300 and y0 <= 60 and y1 >= 110


def test_seal_ghost_inside_the_region_is_cleared():
    img = page()
    img[40:120, 40:120] = (232, 238, 250)  # pale wash of a seal, too faint to be ink
    img[40:120, 40:120][[0, 1, 2, 3, -4, -3, -2, -1], :] = BLUE  # its inked ring
    img[40:120, 40:120][:, [0, 1, 2, 3, -4, -3, -2, -1]] = BLUE

    cleaned, regions = remove_coloured_ink(img)

    assert len(regions) == 1
    assert (cleaned[40:120, 40:120] == WHITE).all()


def test_dark_print_with_high_saturation_but_low_chroma_is_kept():
    img = page()
    text = printed_text(img, 80, 100, 40, 360)
    img[text] = (20, 18, 16)  # HSV saturation ~51, chroma 4

    cleaned, regions = remove_coloured_ink(img)

    assert regions == []
    assert (cleaned == img).all()


def test_page_without_ink_is_unchanged():
    img = page()
    printed_text(img, 80, 100, 40, 360)

    cleaned, regions = remove_coloured_ink(img)

    assert regions == []
    assert (cleaned == img).all()


def signature_block() -> tuple[np.ndarray, np.ndarray]:
    img = page()
    for x in range(120, 280):  # handwritten stroke
        y = 40 + int(20 * np.sin(x / 12))
        img[y : y + 3, x] = (40, 40, 40)
    img[90:92, 60:340] = BLACK  # signature line
    text = printed_text(img, 110, 125, 120, 280)  # printed name
    return img, text


def test_signature_above_the_line_is_erased_and_name_kept():
    img, text = signature_block()
    before = img.copy()

    erased = erase_signature(img, (40, 10, 360, 140))

    assert erased == (40, 10, 360, 92)
    assert (img[10:92, 40:360] == 255).all()
    assert (img[92:] == before[92:]).all()
    assert (img[text] == BLACK).all()


def test_block_without_a_signature_line_is_left_alone():
    img, _ = signature_block()
    img[90:92, 60:340] = WHITE
    img[90:92, 150:200] = BLACK  # too short to be the line
    before = img.copy()

    assert erase_signature(img, (40, 10, 360, 140)) is None
    assert (img == before).all()
