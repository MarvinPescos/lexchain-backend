"""Preview copies of a signed PDF, watermarked on the server.

Pages are rasterized and the mark is drawn into the pixels, so the preview
holds no text layer, form fields or embedded files from the original and the
mark cannot be lifted off as a separate PDF layer. It discourages misuse; it
cannot stop someone saving or photographing the preview.
"""

import io
import math

from pdf2image import convert_from_bytes
from PIL import Image, ImageDraw, ImageFont

# Readable on screen, small enough to serve on every preview request.
PREVIEW_DPI = 100


def watermark_pdf(content: bytes, label: str, dpi: int = PREVIEW_DPI) -> bytes:
    """Return a raster PDF of `content` with `label` tiled across every page."""
    pages = convert_from_bytes(content, dpi=dpi)
    marked = [_mark(page.convert("RGB"), label) for page in pages]
    out = io.BytesIO()
    marked[0].save(
        out, "PDF", save_all=True, append_images=marked[1:], resolution=float(dpi)
    )
    return out.getvalue()


def _mark(page: Image.Image, label: str) -> Image.Image:
    width, height = page.size
    font = ImageFont.load_default(size=max(14, width // 30))
    left, top, right, bottom = (int(v) for v in font.getbbox(label))
    text_w, text_h = right - left, bottom - top

    # One rotated tile, repeated over the page so cropping cannot remove it.
    tile = Image.new("RGBA", (text_w + 40, text_h + 20), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text(
        (20 - left, 10 - top), label, font=font, fill=(200, 30, 30, 70)
    )
    tile = tile.rotate(30, expand=True)

    overlay = Image.new("RGBA", page.size, (0, 0, 0, 0))
    step_x, step_y = tile.width, max(tile.height, text_h * 4)
    for row in range(math.ceil(height / step_y) + 1):
        offset = (row % 2) * step_x // 2
        for col in range(-1, math.ceil(width / step_x) + 1):
            overlay.alpha_composite(tile, (col * step_x + offset, row * step_y))
    return Image.alpha_composite(page.convert("RGBA"), overlay).convert("RGB")
