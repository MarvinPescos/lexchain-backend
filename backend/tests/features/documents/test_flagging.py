"""Deterministic OCR review flags."""

from app.features.documents.extraction.flagging import build_flags


def _amount_flags(text: str) -> list[dict]:
    blocks = [{"type": "text", "text": text}]
    return [f for f in build_flags(blocks) if f["kind"] == "amount_mismatch"]


def test_matching_amount_is_not_flagged():
    assert _amount_flags("a monthly rental of TEN THOUSAND PESOS (PHP 10,000.00)") == []


def test_number_words_earlier_in_the_sentence_are_not_counted():
    text = (
        "for a term of ONE (1) YEAR, commencing on 01 July 2026, at a monthly "
        "rental of TEN THOUSAND PESOS (PHP 10,000.00), payable in advance"
    )
    assert _amount_flags(text) == []


def test_wrong_amount_in_words_is_flagged():
    flags = _amount_flags("the price of FIVE HUNDRED THOUSAND PESOS (PHP 50,000.00)")
    assert len(flags) == 1
    assert "500,000" in flags[0]["message"]
