"""Helpers for the block list the issuer reviews."""

from typing import Any


def blocks_from_text(text: str) -> list[dict[str, Any]]:
    """Build reviewable blocks from plain text.

    Used for OCR engines that report no layout (pypdf, plain tesseract). The
    blocks carry no bbox, so the client cannot highlight a region on the page,
    but the review and edit flow still works paragraph by paragraph.
    """
    blocks: list[dict[str, Any]] = []
    for para in text.split("\n\n"):
        if not para.strip():
            continue
        blocks.append(
            {
                "index": len(blocks),
                "editable": True,
                "type": "text",
                "text": para,
                "original_text": para,
                "bbox": None,
                "page_idx": None,
                "text_level": None,
                "score": None,
                "is_html": False,
                "edited": False,
            }
        )
    return blocks


def blocks_to_text(blocks: list[dict[str, Any]]) -> str:
    """Rebuild the document text from blocks, preserving order."""
    return "\n\n".join(
        (b.get("text") or "").strip()
        for b in blocks
        if b.get("editable", True) and (b.get("text") or "").strip()
    )


def count_edited(blocks: list[dict[str, Any]]) -> int:
    return sum(1 for b in blocks if b.get("edited"))
