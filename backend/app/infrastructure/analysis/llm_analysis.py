# app/infrastructure/analysis/llm_analysis.py

import json

from app.infrastructure.llm.base import LLMMessage
from app.infrastructure.llm.groq_llm import GroqLLMProvider
from app.infrastructure.observability.logging_setup import log

from .base import AnalysisResult


# ── The system prompt ────────────────────────────────────────────────
# This is where the "magic" happens. No matter what kind of document
# the user uploads, the LLM is instructed to return the SAME structure.

SYSTEM_PROMPT = """You are a document analysis and information extraction assistant.

Given the full text of a document, extract structured metadata in JSON format.
Base your answers ONLY on the document text provided. Do NOT fabricate, infer, or add information that is not supported by the text.

Return ONLY valid JSON. Do not include markdown, comments, or explanations.

Use this exact structure:
{
  "summary": "A 2-3 sentence plain-language summary of the document.",
  "labels": ["Label1", "Label2"],
  "entities": [
    {"type": "PERSON", "value": "..."},
    {"type": "ORG", "value": "..."},
    {"type": "DATE", "value": "..."},
    {"type": "MONETARY", "value": "..."},
    {"type": "LOCATION", "value": "..."},
    {"type": "URL", "value": "..."},
    {"type": "DOCUMENT_NUMBER", "value": "..."},
    {"type": "DOCUMENT_REFERENCE", "value": "..."}
  ],
  "risk_flags": [
    {"clause": "description of the risky, ambiguous, or important obligation/deadline", "severity": "low | medium | high"}
  ]
}

Guidelines for each field:
        
- summary:
  Write a concise 2-3 sentence summary.
  Include the document type and document number when present.
  Include the issuing organization when present.
  Include the issue date when present.
  State the main purpose of the document.
  If the document gives instructions, obligations, deadlines, or coverage, include the affected parties and key dates.
  Example style:
  "DepEd Memorandum No. 028, s. 2026, issued by the Department of Education of the Republic of the Philippines on May 6, 2026, provides guidelines for the Basic Education Information System (BEIS) data collection and validation for school year 2025-2026. It directs public and private elementary and secondary schools, including state/local universities and colleges offering basic education, to update school information in BEIS from May 7 to June 15, 2026."

- labels:
  Classify the document using specific labels found or strongly supported by the text.
  Use document-type labels such as:
  "Memorandum", "DepEd Memorandum", "Government Issuance", "Department Circular", "Guidelines", "Advisory", "Order", "Notice", "Contract", "Agreement", "Deed of Sale", "Lease Contract", "Employment Contract", "Service Agreement", "Non-Disclosure Agreement".
  Also include topic labels when supported, such as:
  "Basic Education Information System", "BEIS", "Data Collection", "Data Validation", "School Year 2025-2026", "Education".

- entities:
  Extract named entities exactly as they appear in the text where possible.
  PERSON: named individuals only.
  ORG: agencies, offices, systems, departments, schools, companies, committees, and named platforms/systems.
  DATE: specific dates, date ranges, years, school years, or memorandum/order series years.
  MONETARY: money amounts only.
  LOCATION: places, addresses, cities, provinces, countries, or named complexes used as addresses.
  URL: website links.
  DOCUMENT_NUMBER: document identifiers such as "DepEd Memorandum No. 028, s. 2026".
  DOCUMENT_REFERENCE: cited laws, orders, memoranda, policies, annexes, enclosures, or other referenced documents such as "DepEd Order No. 027, s. 2019" or "Enclosure No. 1".
  Do not duplicate the same entity with identical type and value.

- risk_flags:
  For contracts or legal agreements, identify potentially risky, ambiguous, or unfavorable clauses such as auto-renewal, non-compete, penalties, indemnification, waiver of rights, unilateral termination, or broad liability.
  For government memoranda, orders, notices, or compliance documents, identify important compliance obligations, deadlines, required submissions, validation requirements, or consequences if explicitly stated.
  Do not invent risks. If there are no risky clauses or explicit compliance obligations, return [].
  Use severity:
  "low" for routine informational or administrative requirements,
  "medium" for required actions with deadlines or compliance impact,
  "high" for penalties, sanctions, loss of rights, financial exposure, or serious legal consequences.

If a field has no relevant data, use an empty list [] for arrays or null for summary.
"""


class LLMAnalysisProvider:
    """
    Real analysis provider powered by Groq LLM.

    Accepts raw document text (from the OCR step), sends it to the LLM
    with a structured extraction prompt, and returns an AnalysisResult
    that matches the existing protocol and database schema.
    """

    def __init__(self, model: str = "llama-3.1-8b-instant") -> None:
        self._llm = GroqLLMProvider(model=model)

    async def analyze(self, text: str) -> AnalysisResult:
        # ── Truncate to avoid token limits ──────────────────────────
        # Llama 3.1 8B has an 8k context window.
        # ~4 chars ≈ 1 token → 24,000 chars ≈ 6k tokens (leaves room
        # for the system prompt + output).
        max_chars = 24_000
        truncated = text[:max_chars]
        if len(text) > max_chars:
            log.warning(
                "analysis.text_truncated",
                original_len=len(text),
                truncated_len=max_chars,
            )

        messages = [
            LLMMessage(role="system", content=SYSTEM_PROMPT),
            LLMMessage(role="user", content=truncated),
        ]

        result = await self._llm.generate(
            messages=messages,
            temperature=0.1,   # low temperature = more deterministic
            max_tokens=1024,   # enough for the structured output
        )

        return self._parse(result.answer, result.model)

    def _parse(self, raw_answer: str, model: str) -> AnalysisResult:
        """
        Parse the LLM's JSON response into an AnalysisResult.
        Falls back gracefully if the LLM returns malformed JSON.
        """
        try:
            data = json.loads(raw_answer)
        except json.JSONDecodeError:
            log.error(
                "analysis.json_parse_failed",
                raw_answer=raw_answer[:500],
            )
            return AnalysisResult(
                summary="Analysis failed: could not parse LLM output.",
                labels=[],
                entities=[],
                risk_flags=[],
                confidence=0.0,
                meta={"engine": "groq", "model": model, "error": "json_parse_failed"},
            )

        return AnalysisResult(
            summary=data.get("summary"),
            labels=data.get("labels", []),
            entities=data.get("entities", []),
            risk_flags=data.get("risk_flags", []),
            confidence=0.85,  # fixed for now — LLM doesn't self-score
            meta={
                "engine": "groq",
                "engine_version": model,
            },
        )
