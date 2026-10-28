"""
Signature and seal removal around Marker.

Coloured ink comes off every page first. Marker's first pass then shows where
black signatures are: blocks it could only read as a bare image. Handwriting
above the signature line in those blocks is erased and Marker runs once more,
so a document without signatures costs one pass. Every removed region is kept
as a crop of the original page, so the output still shows it was signed and
sealed.

Marker itself is injected as `convert`, which keeps this module free of it.
"""

import base64
import io
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image

from .cleaning import Box, erase_signature, remove_coloured_ink

DPI = 200
_PX_PER_PT = DPI / 72

# A flagged block is widened so the signature line's ends fall inside it.
_PAD_X_PT = 15
_PAD_TOP_PT = 3

COLOURED_INK = "coloured ink"
SIGNATURE = "signature"


@dataclass
class MarkerPass:
    rendered: Any
    # (page index in the converted PDF, bbox in points) of every non-Picture
    # block whose HTML has no visible text.
    empty_blocks: list[tuple[int, tuple[float, float, float, float]]]


@dataclass
class RemovedRegion:
    page: int
    bbox_pt: list[float]
    kind: str
    image: Image.Image


@dataclass
class PipelineResult:
    rendered: Any
    removed: list[RemovedRegion]


def run(
    pages: list[Image.Image],
    convert: Callable[[bytes], MarkerPass],
    page_numbers: list[int] | None = None,
) -> PipelineResult:
    """`pages` are rendered at `DPI`; `page_numbers` maps them back to the
    source document when only a page range was rendered."""
    numbers = page_numbers or list(range(len(pages)))
    originals = [np.asarray(p.convert("RGB")) for p in pages]
    removed: list[RemovedRegion] = []

    cleaned = []
    for idx, original in enumerate(originals):
        page, boxes = remove_coloured_ink(original)
        cleaned.append(page)
        removed += [_region(original, numbers[idx], box, COLOURED_INK) for box in boxes]

    first = convert(_to_pdf(cleaned))

    erased = [page.copy() for page in cleaned]
    signatures = []
    for idx, bbox in first.empty_blocks:
        box = erase_signature(erased[idx], _padded_px(bbox))
        if box is not None:
            signatures.append(_region(originals[idx], numbers[idx], box, SIGNATURE))
    if not signatures:
        return PipelineResult(first.rendered, removed)

    second = convert(_to_pdf(erased))
    return PipelineResult(second.rendered, removed + signatures)


def build_response(
    output_format: str,
    output: str,
    images: dict[str, str],
    metadata: dict,
    removed: list[RemovedRegion],
) -> dict:
    """The `/marker/upload` success body. `images` are Marker's own, already
    base64-encoded; removed regions are added as PNGs."""
    images = dict(images)
    listed = []
    for n, region in enumerate(removed):
        name = f"removed_page_{region.page}_{n}.png"
        buffer = io.BytesIO()
        region.image.save(buffer, format="PNG")
        images[name] = base64.b64encode(buffer.getvalue()).decode()
        listed.append(
            {"page": region.page, "bbox_pt": region.bbox_pt, "kind": region.kind, "image": name}
        )

    if listed and output_format == "markdown":
        output += "\n\n## Signatures and seals\n\n" + "\n\n".join(
            f"![{_alt(r)}]({r['image']})" for r in listed
        )
    elif listed and output_format == "html":
        output += "\n<h2>Signatures and seals</h2>\n" + "\n".join(
            f"<p><img src=\"{r['image']}\" alt=\"{_alt(r)}\"/></p>" for r in listed
        )

    return {
        "format": output_format,
        "output": output,
        "images": images,
        "metadata": {**metadata, "removed_regions": listed},
        "success": True,
    }


def _region(original: np.ndarray, page: int, box: Box, kind: str) -> RemovedRegion:
    x0, y0, x1, y1 = box
    return RemovedRegion(
        page=page,
        bbox_pt=[round(v / _PX_PER_PT, 2) for v in box],
        kind=kind,
        image=Image.fromarray(original[y0:y1, x0:x1]),
    )


def _padded_px(bbox: tuple[float, float, float, float]) -> Box:
    x0, y0, x1, y1 = bbox
    return (
        math.floor((x0 - _PAD_X_PT) * _PX_PER_PT),
        math.floor((y0 - _PAD_TOP_PT) * _PX_PER_PT),
        math.ceil((x1 + _PAD_X_PT) * _PX_PER_PT),
        math.ceil(y1 * _PX_PER_PT),
    )


def _to_pdf(pages: list[np.ndarray]) -> bytes:
    """Image-only PDF whose pages keep the source size in points."""
    images = [Image.fromarray(page) for page in pages]
    buffer = io.BytesIO()
    images[0].save(
        buffer, format="PDF", save_all=True, append_images=images[1:], resolution=DPI, quality=95
    )
    return buffer.getvalue()


def _alt(region: dict) -> str:
    return f"{region['kind']}, page {region['page'] + 1}"
