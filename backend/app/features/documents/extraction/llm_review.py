"""Tier 4: ask an LLM for the inconsistencies rules cannot express.

The deterministic tiers in `flagging.py` already own everything countable —
amounts, dates, title numbers, confidence, glue errors. Those must stay with the
rules, because an LLM asked to check arithmetic will produce confident wrong
answers. This tier is for judgement only: names that drift between clauses,
clauses that contradict each other, obligations referenced but never defined.

Runs on demand rather than during extraction, so a document can be reviewed with
zero inference cost and the LLM used only when the issuer wants a second look.
"""

import json
import re
from typing import Any

from app.infrastructure.nlp.llm.base import LLMMessage, LLMProvider
from app.infrastructure.observability.logging_setup import log

from .flagging import SEVERITY_LOW, SEVERITY_MEDIUM

# Keep the prompt well inside context while covering a long agreement.
MAX_CHARS = 24_000

_SYSTEM = (
    "You review OCR'd Philippine legal documents for a notary. You are given "
    "numbered text blocks. Identify only SEMANTIC problems: a party's name "
    "spelled differently between clauses, dates or obligations that contradict "
    "each other, a term used but never defined, a clause that references a "
    "schedule or annex that is absent, or a sentence so garbled its meaning is "
    "unclear.\n\n"
    "Do NOT report: spelling or spacing, formatting, missing punctuation, or "
    "arithmetic on amounts — those are checked separately and reporting them is "
    "noise.\n\n"
    'Reply with JSON only: {"findings": [{"block_index": <int>, '
    '"severity": "medium"|"low", "message": "<one sentence>"}]}. '
    'If nothing is genuinely wrong, reply {"findings": []}. '
    "Never invent problems to seem useful."
)


def _render_blocks(blocks: list[dict[str, Any]]) -> str:
    lines, total = [], 0
    for b in blocks:
        text = (b.get("text") or "").strip()
        if not text:
            continue
        line = f"[{b.get('index')}] {text}"
        if total + len(line) > MAX_CHARS:
            lines.append("... (truncated)")
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def _parse_findings(answer: str) -> list[dict[str, Any]]:
    """Pull the JSON object out of the reply, tolerating fenced code blocks."""
    text = answer.strip()

    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    else:
        brace = text.find("{")
        if brace > 0:
            text = text[brace:]

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("extraction.llm_review.unparseable", preview=answer[:200])
        return []

    findings = data.get("findings") if isinstance(data, dict) else None
    return findings if isinstance(findings, list) else []


async def llm_flags(
    llm: LLMProvider,
    blocks: list[dict[str, Any]],
    max_findings: int = 20,
) -> list[dict[str, Any]]:
    """Return semantic flags in the same shape as the deterministic tiers."""
    if not blocks:
        return []

    rendered = _render_blocks(blocks)
    if not rendered.strip():
        return []

    try:
        result = await llm.generate(
            messages=[
                LLMMessage(role="system", content=_SYSTEM),
                LLMMessage(role="user", content=rendered),
            ],
            temperature=0.0,
            max_tokens=1024,
        )
    except Exception as e:
        # A failed second opinion must never block a review.
        log.error("extraction.llm_review.failed", error=str(e))
        return []

    valid_indexes = {b.get("index") for b in blocks}
    allowed_severity = {SEVERITY_MEDIUM, SEVERITY_LOW}

    flags = []
    for f in _parse_findings(result.answer)[:max_findings]:
        if not isinstance(f, dict):
            continue
        message = str(f.get("message") or "").strip()
        if not message:
            continue

        idx = f.get("block_index")
        # Drop hallucinated block references rather than pointing the issuer at
        # a block that does not exist.
        if idx is not None and idx not in valid_indexes:
            log.warning("extraction.llm_review.bad_index", index=idx)
            idx = None

        severity = str(f.get("severity") or SEVERITY_LOW).lower()
        if severity not in allowed_severity:
            severity = SEVERITY_LOW

        excerpt = ""
        if idx is not None:
            match = next((b for b in blocks if b.get("index") == idx), None)
            excerpt = (match or {}).get("text", "")

        flags.append(
            {
                "block_index": idx,
                "kind": "llm_inconsistency",
                "severity": severity,
                "message": message,
                "excerpt": excerpt[:200],
            }
        )

    log.info("extraction.llm_review.done", findings=len(flags), model=result.model)
    return flags
