"""Escape-hatch behavior: unknown/edge documents must degrade to human
review or correct handling — never silent bad data, never a crash loop."""
from datetime import date
from decimal import Decimal

from tfa_core.adapters.excel_reader import analyze_excel
from tfa_core.domain.validation import validate_extraction
from tfa_core.domain.models import AncillaryCharge, FolioExtraction


def test_credit_note_with_negative_amounts_passes():
    x = FolioExtraction(
        document_type="credit_note",
        hotel_name="ECO BOX INT GROUP S.A.",
        country="Argentina",
        currency="ARS",
        invoice_number="NC-0001-00000088",
        references_invoice_number="00003-00001507",
        ancillary_charges=[
            AncillaryCharge(description="Reverso habitación",
                            english_description="Room charge reversal",
                            amount=Decimal("-91476.00"), category="other"),
        ],
        total_amount=Decimal("-91476.00"),
    )
    r = validate_extraction(x)
    assert not r.fatal and not r.needs_review


def test_credit_note_with_positive_total_flagged():
    x = FolioExtraction(
        document_type="credit_note", hotel_name="V", currency="ARS",
        ancillary_charges=[AncillaryCharge(
            description="x", english_description="x",
            amount=Decimal("100"), category="other")],
        total_amount=Decimal("100"),
    )
    r = validate_extraction(x)
    assert r.needs_review and any("sign convention" in w for w in r.warnings)


def test_not_an_invoice_goes_straight_to_review():
    x = FolioExtraction(
        document_type="not_an_invoice",
        hotel_name="unknown",
        currency="USD",
        unreadable_reason="File is a boarding pass, not an invoice.",
        total_amount=None,
    )
    r = validate_extraction(x)
    assert not r.fatal          # no arithmetic checks run
    assert r.needs_review
    assert any("boarding pass" in w for w in r.warnings)


def test_multiple_documents_flag_forces_review_but_first_doc_still_validates():
    x = FolioExtraction(
        document_type="air_ticket_invoice", hotel_name="Wings",
        currency="USD", contains_multiple_documents=True,
        ancillary_charges=[AncillaryCharge(
            description="Fare", english_description="Fare",
            amount=Decimal("100"), category="airfare")],
        total_amount=Decimal("100"),
    )
    r = validate_extraction(x)
    assert not r.fatal
    assert r.needs_review
    assert any("splitting" in w for w in r.warnings)


def test_excel_workbook_renders_to_markdown_layout():
    from openpyxl import Workbook
    import io
    wb = Workbook()
    ws = wb.active
    ws.title = "Folio"
    ws.append(["Hotel", "Eco Hotel Añelo"])
    ws.append(["Noche 1", 257700.96])
    ws.append(["Noche 2", 257700.96])
    buf = io.BytesIO()
    wb.save(buf)

    layout = analyze_excel(buf.getvalue(), "folio.xlsx")
    assert "## Sheet: Folio" in layout.markdown_text
    assert "257700.96" in layout.markdown_text
    assert layout.page_images_b64 == []
    assert layout.ocr_confidence_floor == 1.0
