"""Heuristics that point the issuer at text worth checking.

The goal is triage, not correctness: a 20-page agreement is unreadable in
practice, so every flag carries the block it belongs to (and therefore a page
and bbox) to turn reviewing into "check these 6 spots" instead of "read
everything".

Three tiers run here, all deterministic and free:

  confidence  — the OCR engine itself was unsure about the block
  garbled     — the text shows classic OCR corruption (glue errors, 0/O swaps)
  consistency — domain rules for Philippine legal documents (amounts, dates,
                title numbers, notarial block)

A fourth LLM tier lives in `llm_review.py` and runs only when asked.
"""

import html
import re
from typing import Any

_HTML_TAG = re.compile(r"<[^>]+>")

# Blocks scoring below this are surfaced for a look.
LOW_CONFIDENCE_THRESHOLD = 0.85

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"

# --- garbled-text patterns -------------------------------------------------
# "20thday", "June2026" — the space between a word and a number is a very
# common OCR loss and reads as a single token. The {3,} keeps bare ordinals
# like "20th" from matching, since those are correct as written.
_GLUE_DIGIT_ALPHA = re.compile(r"\b\d+[A-Za-z]{3,}\b")
_GLUE_ALPHA_DIGIT = re.compile(r"\b[A-Za-z]{2,}\d{2,}\b")
# A lowercase letter immediately followed by an uppercase one mid-token.
_GLUE_CASE = re.compile(r"\b[a-z]+[A-Z][a-z]+\b")
# Digits substituted into words (l/1, O/0) and vice versa.
_MIXED_ALNUM = re.compile(r"\b(?=[A-Za-z]*\d)(?=\d*[A-Za-z])[A-Za-z0-9]{3,}\b")
# Ordinals ("20th", "1st") are correct as written and must not be reported.
_ORDINAL = re.compile(r"^\d+(?:st|nd|rd|th)$", re.IGNORECASE)
# Runs of punctuation or lone symbols are usually noise from the scan.
_PUNCT_NOISE = re.compile(r"[^\w\s]{3,}")

_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALE_WORDS = {"hundred": 100, "thousand": 1_000, "million": 1_000_000}

_AMOUNT_FIGURE = re.compile(
    r"(?:PHP|P|₱|PESOS?)\s*([\d,]+(?:\.\d{2})?)", re.IGNORECASE
)
_TCT_PATTERN = re.compile(r"TCT\s*No\.?\s*([A-Z]?-?[\w-]+)", re.IGNORECASE)
_DATE_PATTERN = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s*day\s*of\s*([A-Za-z]+)\s*,?\s*(\d{4})\b",
    re.IGNORECASE,
)
_MONTHS = {
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
}
# Notarial block fields a Philippine deed is expected to carry.
_NOTARIAL_FIELDS = ("doc. no", "page no", "book no", "series of")
# "SERIES OF (YEAR) : 2026" / "Series of: 2026"
_SERIES_YEAR = re.compile(
    r"series\s*of\s*(?:\(year\))?\s*[:\-]?\s*(\d{4})", re.IGNORECASE
)


def _block_text(block: dict[str, Any]) -> str:
    """Readable text for a block.

    Table blocks hold HTML. Running the garbling heuristics over markup reports
    the tags themselves — `</td><td>` as punctuation noise, `&#x27;` as a
    letter/digit mix — so tags and entities are stripped first and only the cell
    values are checked.
    """
    text = block.get("text") or ""
    if not block.get("is_html") and block.get("type") != "table":
        return text
    return html.unescape(_HTML_TAG.sub(" ", text))


def _flag(
    block_index: int | None,
    kind: str,
    severity: str,
    message: str,
    excerpt: str = "",
) -> dict[str, Any]:
    return {
        "block_index": block_index,
        "kind": kind,
        "severity": severity,
        "message": message,
        "excerpt": excerpt[:200],
    }


def _words_to_number(phrase: str) -> int | None:
    """Best-effort 'FIVE HUNDRED THOUSAND' -> 500000.

    Returns None when the phrase contains nothing recognisable, so callers can
    skip the comparison rather than report a bogus mismatch.
    """
    tokens = re.findall(r"[a-z]+", phrase.lower())
    total = current = 0
    seen = False

    for tok in tokens:
        if tok in _NUMBER_WORDS:
            current += _NUMBER_WORDS[tok]
            seen = True
        elif tok in _SCALE_WORDS:
            scale = _SCALE_WORDS[tok]
            if current == 0:
                current = 1
            if scale >= 1000:
                total += current * scale
                current = 0
            else:
                current *= scale
            seen = True
        elif tok in ("and", "only", "pesos", "peso"):
            continue
        else:
            # Any other word ends the amount: only the run of number words
            # right before the figure counts ("ONE (1) YEAR … TEN THOUSAND").
            total = current = 0
            seen = False

    if not seen:
        return None
    return total + current


# --- tier 1: engine confidence --------------------------------------------

def _confidence_flags(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flags = []
    for i, b in enumerate(blocks):
        score = b.get("score")
        if score is None:
            continue
        if score < LOW_CONFIDENCE_THRESHOLD:
            flags.append(
                _flag(
                    i,
                    "low_confidence",
                    SEVERITY_HIGH if score < 0.6 else SEVERITY_MEDIUM,
                    f"OCR confidence {score:.2f} is below {LOW_CONFIDENCE_THRESHOLD}",
                    b.get("text", ""),
                )
            )
    return flags


# --- tier 2: garbled text --------------------------------------------------

def _garbled_flags(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flags = []
    for i, b in enumerate(blocks):
        text = _block_text(b)
        if not text.strip():
            continue

        glued = set(
            _GLUE_DIGIT_ALPHA.findall(text)
            + _GLUE_ALPHA_DIGIT.findall(text)
            + _GLUE_CASE.findall(text)
        )
        if glued:
            flags.append(
                _flag(
                    i,
                    "missing_space",
                    SEVERITY_MEDIUM,
                    f"Words may be run together: {', '.join(sorted(glued)[:5])}",
                    text,
                )
            )

        # Anything already reported as a glue error would only be noise here.
        mixed = [
            m for m in _MIXED_ALNUM.findall(text)
            if m not in glued
            and not _ORDINAL.match(m)
            and not re.fullmatch(r"[A-Z]?-?\d+[A-Z]?", m)  # skip title/ref numbers
        ]
        if mixed:
            flags.append(
                _flag(
                    i,
                    "mixed_alnum",
                    SEVERITY_LOW,
                    f"Letters and digits mixed in a token (possible 0/O or 1/l): "
                    f"{', '.join(sorted(set(mixed))[:5])}",
                    text,
                )
            )

        if _PUNCT_NOISE.search(text):
            flags.append(
                _flag(i, "punct_noise", SEVERITY_LOW,
                      "Run of punctuation characters — likely scan noise", text)
            )

    return flags


# --- tier 3: domain consistency -------------------------------------------

def _consistency_flags(
    blocks: list[dict[str, Any]], full_text: str
) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []

    # Amount in words must agree with the figure beside it.
    for i, b in enumerate(blocks):
        text = _block_text(b)
        figure_match = _AMOUNT_FIGURE.search(text)
        if not figure_match:
            continue
        try:
            figure = float(figure_match.group(1).replace(",", ""))
        except ValueError:
            continue

        # Words normally precede the parenthesised figure.
        words_part = text[: figure_match.start()]
        spelled = _words_to_number(words_part)
        if spelled is not None and spelled > 0 and abs(spelled - figure) > 0.01:
            flags.append(
                _flag(
                    i,
                    "amount_mismatch",
                    SEVERITY_HIGH,
                    f"Amount in words reads {spelled:,.0f} but the figure says "
                    f"{figure:,.2f}",
                    text,
                )
            )

    # Dates must name a real month.
    for i, b in enumerate(blocks):
        text = _block_text(b)
        for day, month, year in _DATE_PATTERN.findall(text):
            if month.lower() not in _MONTHS:
                flags.append(
                    _flag(i, "invalid_date", SEVERITY_HIGH,
                          f"'{month}' is not a recognised month", text)
                )
            if not (1 <= int(day) <= 31):
                flags.append(
                    _flag(i, "invalid_date", SEVERITY_HIGH,
                          f"Day '{day}' is out of range", text)
                )

    # A title number that came through as pure noise is worth a look.
    for i, b in enumerate(blocks):
        text = _block_text(b)
        for tct in _TCT_PATTERN.findall(text):
            if not re.search(r"\d", tct):
                flags.append(
                    _flag(i, "suspect_title_no", SEVERITY_MEDIUM,
                          f"Title number '{tct}' contains no digits", text)
                )

    # A notarised instrument is entered in the register for the year it was
    # executed, so the series year and the execution year must agree. A mismatch
    # is a filing error worth catching before the document is anchored.
    series_years = {int(y) for y in _SERIES_YEAR.findall(full_text)}
    executed_years = {int(y) for _, _, y in _DATE_PATTERN.findall(full_text)}
    if series_years and executed_years and not (series_years & executed_years):
        flags.append(
            _flag(
                None,
                "series_year_mismatch",
                SEVERITY_HIGH,
                "Series year "
                + ", ".join(str(y) for y in sorted(series_years))
                + " does not match the execution year "
                + ", ".join(str(y) for y in sorted(executed_years)),
            )
        )

    # Notarial fields are document-level, so they carry no block index.
    lowered = full_text.lower()
    missing = [f for f in _NOTARIAL_FIELDS if f not in lowered]
    if missing and len(missing) < len(_NOTARIAL_FIELDS):
        # Only meaningful if the document looks notarised at all.
        flags.append(
            _flag(
                None,
                "incomplete_notarial",
                SEVERITY_MEDIUM,
                "Notarial block looks incomplete; missing: "
                + ", ".join(m.title() for m in missing),
            )
        )

    return flags


def build_flags(
    blocks: list[dict[str, Any]], full_text: str = ""
) -> list[dict[str, Any]]:
    """Run every deterministic tier and return flags, most severe first."""
    if not blocks:
        return []

    text = full_text or "\n".join(_block_text(b) for b in blocks)

    flags = (
        _confidence_flags(blocks)
        + _garbled_flags(blocks)
        + _consistency_flags(blocks, text)
    )

    order = {SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 1, SEVERITY_LOW: 2}
    flags.sort(key=lambda f: (order.get(f["severity"], 3), f["block_index"] or 0))
    return flags
