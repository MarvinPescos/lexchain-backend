"""Localise *what* changed between the attested text and the current one.

A hash is a smoke alarm, not a security camera: it proves a document changed but
not where. This turns "TAMPERED" into "this clause changed, and the amount went
from PHP 500,000.00 to PHP 50,000.00".

Comparison runs at line level so whole clauses light up, with a word-level pass
inside each changed block so a client can highlight the exact edit. Everything
here is `difflib` from the standard library — no new dependency.
"""

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

SEVERITY_CRITICAL = "critical"
SEVERITY_MAJOR = "major"
SEVERITY_MINOR = "minor"

# [reconstructed from bytecode — original comments were lost]

_AMOUNT = re.compile(r"(?:PHP|P|₱|USD|\$)\s*([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)
_NUMBER = re.compile(r"\b\d[\d,]*(?:\.\d+)?\b")

_DATE = re.compile(
    r"\b\d{1,2}\s*(?:st|nd|rd|th)?\s*(?:day\s*of\s*)?[A-Za-z]+\s*,?\s*\d{4}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)

_IDENTIFIER = re.compile(
    r"\b(?:TCT|CCT|PTR|IBP|MCLE|Roll(?:\s+of\s+Attorneys)?|Doc|Page|Book|"
    r"(?:Transfer\s+|Condominium\s+)?Certificate\s+of\s+Title|Title)\.?\s*No\.?\s*:?\s*[\w-]+",
    re.IGNORECASE,
)


_WHITESPACE = re.compile(r"\s+")


def _comparable(text: str) -> str:
    """The text as the diff compares it.

    OCR spaces the same print differently between runs ("Tel.No." against
    "Tel. No."), and folds look-alike characters inconsistently. Neither is an
    edit, so both are ignored; every other character still counts.
    """
    return _WHITESPACE.sub("", unicodedata.normalize("NFKC", text))


def _amounts(text: str) -> list[str]:
    return [m.replace(",", "") for m in _AMOUNT.findall(text)]


def _numbers(text: str) -> list[str]:
    return [m.replace(",", "") for m in _NUMBER.findall(text)]


def _classify(original: str, current: str) -> tuple[str, str]:
    """Rank a change by what it would do to the agreement.

    Returns (severity, human-readable reason). Money moving is the worst case,
    then other figures, dates and reference numbers, then wording.
    """
    orig_amounts, cur_amounts = _amounts(original), _amounts(current)
    if orig_amounts != cur_amounts:
        if orig_amounts and cur_amounts:
            return (
                SEVERITY_CRITICAL,
                f"Monetary amount changed: {', '.join(orig_amounts)} → "
                f"{', '.join(cur_amounts)}",
            )
        if orig_amounts and not cur_amounts:
            return SEVERITY_CRITICAL, f"Monetary amount removed: {', '.join(orig_amounts)}"
        return SEVERITY_CRITICAL, f"Monetary amount inserted: {', '.join(cur_amounts)}"

    orig_ids, cur_ids = _IDENTIFIER.findall(original), _IDENTIFIER.findall(current)
    if orig_ids != cur_ids:
        return (
            SEVERITY_CRITICAL,
            f"Reference number changed: {', '.join(orig_ids) or '—'} → "
            f"{', '.join(cur_ids) or '—'}",
        )

    orig_dates, cur_dates = _DATE.findall(original), _DATE.findall(current)
    if orig_dates != cur_dates:
        return (
            SEVERITY_MAJOR,
            f"Date changed: {', '.join(orig_dates) or '—'} → "
            f"{', '.join(cur_dates) or '—'}",
        )

    orig_nums, cur_nums = _numbers(original), _numbers(current)
    if orig_nums != cur_nums:
        return (
            SEVERITY_MAJOR,
            f"Figures changed: {', '.join(orig_nums) or '—'} → "
            f"{', '.join(cur_nums) or '—'}",
        )

    return SEVERITY_MINOR, "Wording changed"


def _word_diff(original: str, current: str) -> list[dict[str, str]]:
    """Word-level ops inside a changed block, for inline highlighting."""
    a, b = original.split(), current.split()
    parts = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b).get_opcodes():
        if tag == "equal":
            parts.append({"op": "equal", "text": " ".join(a[i1:i2])})
        elif tag == "replace":
            parts.append({"op": "removed", "text": " ".join(a[i1:i2])})
            parts.append({"op": "added", "text": " ".join(b[j1:j2])})
        elif tag == "delete":
            parts.append({"op": "removed", "text": " ".join(a[i1:i2])})
        elif tag == "insert":
            parts.append({"op": "added", "text": " ".join(b[j1:j2])})
    return [p for p in parts if p["text"]]


def _split(text: str) -> list[str]:
    """Split into comparable segments, dropping blank lines.

    Legal text is line/clause structured, so lines are the right granularity —
    a whole clause lights up rather than a stray character.
    """
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _block_texts(blocks: list[dict[str, Any]]) -> list[str]:
    """Editable text per block, in reading order. Figures/seals contribute nothing."""
    return [
        (b.get("text") or "").strip()
        for b in blocks
        if b.get("editable", True) and (b.get("text") or "").strip()
    ]


def _editable_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        b
        for b in blocks
        if b.get("editable", True) and (b.get("text") or "").strip()
    ]


def diff_blocks(
    original_blocks: list[dict[str, Any]],
    current_blocks: list[dict[str, Any]],
    max_segments: int = 200,
) -> dict[str, Any]:
    """Compare attested blocks against current blocks.

    Same shape as `diff_text`, plus the page coordinates of each change, so a
    client can outline the tampered region on the page instead of only naming a
    line number. `bbox` comes from the *current* document — that is where the
    altered text physically sits now — except for a delete, whose text is only
    in the attested document; `location` says which one.
    """
    a_blocks = _editable_blocks(original_blocks)
    b_blocks = _editable_blocks(current_blocks)
    a = [(b.get("text") or "").strip() for b in a_blocks]
    b = [(b.get("text") or "").strip() for b in b_blocks]
    a_key = [_comparable(t) for t in a]
    b_key = [_comparable(t) for t in b]

    segments: list[dict[str, Any]] = []
    counts = {"replace": 0, "insert": 0, "delete": 0}
    truncated = False

    def emit(tag: str, i1: int, i2: int, j1: int, j2: int) -> None:
        nonlocal truncated
        counts[tag] += 1
        if len(segments) >= max_segments:
            truncated = True
            return
        original_block = "\n".join(a[i1:i2])
        current_block = "\n".join(b[j1:j2])

        if tag == "insert":
            severity, reason = _classify("", current_block)
            reason = f"Text inserted. {reason}"
        elif tag == "delete":
            severity, reason = _classify(original_block, "")
            reason = f"Text removed. {reason}"
        else:
            severity, reason = _classify(original_block, current_block)

        anchor = b_blocks[j1] if j1 < len(b_blocks) and tag != "delete" else None
        original_anchor = a_blocks[i1] if i1 < len(a_blocks) else None
        pos = anchor or original_anchor or {}

        segments.append(
            {
                "type": tag,
                "severity": severity,
                "reason": reason,
                "original_text": original_block,
                "current_text": current_block,
                "original_line_start": i1 + 1,
                "original_line_end": i2,
                "current_line_start": j1 + 1,
                "current_line_end": j2,
                "block_index": pos.get("index"),
                "original_block_index": (original_anchor or {}).get("index"),
                "location": "current" if anchor else "original",
                "page_idx": pos.get("page_idx"),
                "bbox": pos.get("bbox"),
                "word_diff": _word_diff(original_block, current_block),
            }
        )

    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a_key, b_key).get_opcodes():
        if tag == "equal":
            continue
        # Equal-length replacements are reported block by block, skipping
        # blocks that did not change, so each segment outlines one region.
        if tag == "replace" and i2 - i1 == j2 - j1:
            for k in range(i2 - i1):
                if a_key[i1 + k] != b_key[j1 + k]:
                    emit("replace", i1 + k, i1 + k + 1, j1 + k, j1 + k + 1)
        else:
            emit(tag, i1, i2, j1, j2)

    order = {SEVERITY_CRITICAL: 0, SEVERITY_MAJOR: 1, SEVERITY_MINOR: 2}
    segments.sort(key=lambda s: (order[s["severity"]], s["original_line_start"]))

    similarity = SequenceMatcher(None, "".join(a_key), "".join(b_key)).ratio()

    return {
        "segments": segments,
        "total_changes": len(segments),
        "critical_changes": sum(1 for s in segments if s["severity"] == SEVERITY_CRITICAL),
        "replaced": counts["replace"],
        "inserted": counts["insert"],
        "deleted": counts["delete"],
        "similarity": round(similarity, 4),
        "truncated": truncated,
        "localized": True,
    }


def diff_text(original: str, current: str, max_segments: int = 200) -> dict[str, Any]:
    """Compare attested text against current text.

    Returns the changed segments with positions, severity and a word-level
    breakdown, plus counts for a summary line.
    """
    a, b = _split(original), _split(current)
    a_key = [_comparable(t) for t in a]
    b_key = [_comparable(t) for t in b]

    segments: list[dict[str, Any]] = []
    counts = {"replaced": 0, "inserted": 0, "deleted": 0}

    def add(tag: str, oa: int, ob: int, ca: int, cb: int) -> None:
        original_block = "\n".join(a[oa:ob])
        current_block = "\n".join(b[ca:cb])

        if tag == "insert":
            severity, reason = _classify("", current_block)
            reason = f"Text inserted. {reason}"
        elif tag == "delete":
            severity, reason = _classify(original_block, "")
            reason = f"Text removed. {reason}"
        else:
            severity, reason = _classify(original_block, current_block)

        segments.append(
            {
                "type": tag,
                "severity": severity,
                "reason": reason,
                "original_text": original_block,
                "current_text": current_block,
                "original_line_start": oa + 1,
                "original_line_end": ob,
                "current_line_start": ca + 1,
                "current_line_end": cb,
                "word_diff": _word_diff(original_block, current_block),
            }
        )

    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a_key, b_key).get_opcodes():
        if tag == "equal":
            continue

        if tag == "replace":
            counts["replaced"] += 1
            # [reconstructed] equal-length replacements are reported line by
            # line, skipping lines that did not change.
            if i2 - i1 == j2 - j1:
                for offset in range(i2 - i1):
                    if a_key[i1 + offset] == b_key[j1 + offset]:
                        continue
                    add(
                        "replace", i1 + offset, i1 + offset + 1,
                        j1 + offset, j1 + offset + 1,
                    )
                    if len(segments) >= max_segments:
                        break
            else:
                add("replace", i1, i2, j1, j2)
        elif tag == "insert":
            counts["inserted"] += 1
            add("insert", i1, i2, j1, j2)
        elif tag == "delete":
            counts["deleted"] += 1
            add("delete", i1, i2, j1, j2)

        if len(segments) >= max_segments:
            break

    order = {SEVERITY_CRITICAL: 0, SEVERITY_MAJOR: 1, SEVERITY_MINOR: 2}
    segments.sort(key=lambda s: (order[s["severity"]], s["original_line_start"]))

    similarity = SequenceMatcher(None, _comparable(original), _comparable(current)).ratio()

    return {
        "segments": segments,
        "total_changes": len(segments),
        "critical_changes": sum(1 for s in segments if s["severity"] == SEVERITY_CRITICAL),
        "replaced": counts["replaced"],
        "inserted": counts["inserted"],
        "deleted": counts["deleted"],
        "similarity": round(similarity, 4),
        "localized": False,
    }
