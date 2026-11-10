"""OCR re-reads must not be reported as tampering."""

from app.features.blockchain.tamper import SEVERITY_CRITICAL, SEVERITY_MINOR, diff_blocks


def blocks(text: str) -> list[dict]:
    return [{"text": text, "index": 0, "page_idx": 0, "bbox": [0, 0, 1, 1]}]


def test_spacing_alone_is_not_a_change():
    diff = diff_blocks(
        blocks("Appointment No.2025-11 untilDecember 31, 2026 IBP No.123456 Tel. (02)8687-5309"),
        blocks("Appointment No. 2025-11 until December 31, 2026 IBP No. 123456 Tel. (02) 8687-5309"),
    )
    assert diff["segments"] == []


def test_punctuation_alone_is_a_possible_ocr_inconsistency():
    [segment] = diff_blocks(
        blocks("PTR No. 4567890 / San Isidro, Rizal Office Address:15"),
        blocks("PTR No. 4567890 / San Isidro. Rizal Office Address: 15"),
    )["segments"]
    assert segment["severity"] == SEVERITY_MINOR
    assert "Possible OCR inconsistency" in segment["reason"]


def test_stamp_misread_on_a_rescan_is_a_possible_ocr_inconsistency():
    # Read by MinerU from the same notary stamp at 300 and 200 dpi.
    [segment] = diff_blocks(
        blocks("IBP No. 123456 / 01-15-2025 / Rizal Chapter Email: Icastillonotary@gmail.com"),
        blocks("IBP No. 123456 / 01-15–2025 / Rizal Chapter Email: leastillonotary(@gmail.com"),
    )["segments"]
    assert segment["severity"] == SEVERITY_MINOR
    assert "Possible OCR inconsistency" in segment["reason"]


def test_changed_digit_in_the_stamp_is_still_an_edit():
    [segment] = diff_blocks(
        blocks("Roll of Attorneys No. 78945"), blocks("Roll of Attorneys No. 78845")
    )["segments"]
    assert segment["severity"] == SEVERITY_CRITICAL


def test_changed_word_is_still_an_edit():
    [segment] = diff_blocks(
        blocks("The affiant lost the original title"),
        blocks("The affiant sold the original title"),
    )["segments"]
    assert "Possible OCR inconsistency" not in segment["reason"]


def test_moved_decimal_point_is_still_an_edit():
    [segment] = diff_blocks(
        blocks("Price: PHP 1,000.00"), blocks("Price: PHP 10,00.0")
    )["segments"]
    assert segment["severity"] == SEVERITY_CRITICAL


def test_changed_reference_number_is_still_an_edit():
    [segment] = diff_blocks(blocks("IBP No. 123456"), blocks("IBP No. 123457"))["segments"]
    assert segment["severity"] == SEVERITY_CRITICAL
