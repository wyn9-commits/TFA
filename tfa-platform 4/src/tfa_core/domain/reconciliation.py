"""tfa_core.domain.reconciliation — deterministic business logic.

Turns a validated FolioExtraction into denormalised FolioRecord rows:
- matches the folio hotel to the negotiated-rate directory (exact + fuzzy)
- converts nightly prices to USD with the effective FX rate
- computes over/undercharge per night vs. the negotiated rate
- derives reconciliation month / year / quarter

Everything here is pure-Python and unit-testable — the LLM never does
arithmetic. (In the POC the model was implicitly trusted for derived values.)
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
from typing import Optional

from tfa_core.domain.models import FolioExtraction, FolioRecord, RateIssue, ReviewStatus

logger = logging.getLogger(__name__)

_CENT = Decimal("0.01")


@dataclass(frozen=True)
class NegotiatedRate:
    hotel_name: str
    country: Optional[str]
    nightly_rate_usd: Decimal


@dataclass(frozen=True)
class FxRate:
    currency: str          # ISO 4217
    usd_per_unit: Decimal  # multiply folio amount by this to get USD


class RateDirectory:
    """Negotiated-rate lookup with normalised exact match then fuzzy fallback."""

    def __init__(self, rates: list[NegotiatedRate], fuzzy_threshold: float = 0.87):
        self._rates = rates
        self._by_norm = { _norm(r.hotel_name): r for r in rates }
        self._threshold = fuzzy_threshold

    def match(self, folio_hotel_name: str,
              country: Optional[str]) -> Optional[NegotiatedRate]:
        key = _norm(folio_hotel_name)
        if key in self._by_norm:
            return self._by_norm[key]
        best: tuple[float, Optional[NegotiatedRate]] = (0.0, None)
        for r in self._rates:
            if country and r.country and _norm(country) != _norm(r.country):
                continue
            score = SequenceMatcher(None, key, _norm(r.hotel_name)).ratio()
            if score > best[0]:
                best = (score, r)
        if best[0] >= self._threshold:
            logger.info("Fuzzy-matched '%s' -> '%s' (%.2f)",
                        folio_hotel_name, best[1].hotel_name, best[0])
            return best[1]
        return None


def _norm(s: str) -> str:
    return " ".join(s.lower().replace("-", " ").split())


def build_records(
    extraction: FolioExtraction,
    *,
    file_name: str,
    identifier_name: str,
    uploading_person_name: str,
    blob_md5: str,
    rate: Optional[NegotiatedRate],
    fx: Optional[FxRate],
    needs_review: bool,
    warnings: list[str],
) -> list[FolioRecord]:
    """One FolioRecord per nightly charge (plus one for late/early if present)."""

    checkout = extraction.checkout_date
    year_month = f"{checkout.year}-{checkout.month:02d}" if checkout else None
    number_of_days = (
        (extraction.checkout_date - extraction.checkin_date).days
        if extraction.checkin_date and extraction.checkout_date else None
    )
    document_id = hashlib.sha256(
        f"{identifier_name}/{file_name}/{blob_md5}".encode()
    ).hexdigest()[:32]

    usd = (lambda amt: (amt * fx.usd_per_unit).quantize(_CENT, ROUND_HALF_UP)) if fx else None

    # Per-night audit ----------------------------------------------------- #
    over_days: list[str] = []
    under_days: list[str] = []
    over_total = Decimal("0")
    under_total = Decimal("0")

    per_night_usd: list[Optional[Decimal]] = []
    day_no = 0
    for line in extraction.nightly_charges:
        per_night_amt = (line.amount / line.nights).quantize(_CENT, ROUND_HALF_UP)
        n_usd = usd(per_night_amt) if usd else None
        per_night_usd.append(n_usd)
        for _ in range(line.nights):
            day_no += 1
            if n_usd is not None and rate is not None:
                delta = n_usd - rate.nightly_rate_usd
                if delta > 0:
                    over_days.append(f"Day-{day_no}")
                    over_total += delta
                elif delta < 0:
                    under_days.append(f"Day-{day_no}")
                    under_total += -delta

    issue = RateIssue.OVERCHARGED if over_days else RateIssue.NONE
    net = (over_total - under_total).quantize(_CENT, ROUND_HALF_UP)

    late_early = [
        c for c in extraction.ancillary_charges
        if c.category in ("late_checkout", "early_checkin")
    ]
    late_early_desc = "; ".join(c.english_description for c in late_early) or None
    late_early_usd = (
        sum((usd(c.amount) for c in late_early), Decimal("0")).quantize(_CENT, ROUND_HALF_UP)
        if usd and late_early else None
    )

    status = ReviewStatus.NEEDS_REVIEW if needs_review else ReviewStatus.AUTO_APPROVED
    warn_str = "; ".join(warnings) or None

    common = dict(
        file_name=file_name,
        identifier_name=identifier_name,
        uploading_person_name=uploading_person_name,
        document_id=document_id,
        folio_hotel_name=extraction.hotel_name,
        hotel_address=extraction.hotel_address,
        country=extraction.country,
        folio_date=extraction.folio_date,
        invoice_number=extraction.invoice_number,
        booking_number=extraction.booking_number,
        nro_reserv=extraction.reservation_number,
        gds_record_locator=extraction.gds_record_locator,
        document_type=extraction.document_type.value,
        supplier_tax_id=extraction.supplier_tax_id,
        customer_name=extraction.customer_name,
        customer_tax_id=extraction.customer_tax_id,
        e_invoice_id=extraction.e_invoice_id,
        travel_route=extraction.travel_route,
        guest=extraction.guest_name,
        checkin=extraction.checkin_date,
        checkin_timestamp=extraction.checkin_time.isoformat() if extraction.checkin_time else None,
        checkout=extraction.checkout_date,
        checkout_timestamp=extraction.checkout_time.isoformat() if extraction.checkout_time else None,
        currency=extraction.currency,
        number_of_days=number_of_days,
        year_month=year_month,
        matched_hotel_name=rate.hotel_name if rate else None,
        negotiated_rate=rate.nightly_rate_usd if rate else None,
        currency_multiplier_rate=fx.usd_per_unit if fx else None,
        late_early_english_description=late_early_desc,
        usd_late_early_unit_price=late_early_usd,
        issue_found=issue,
        overcharged_days=";".join(over_days) or None,
        undercharged_days=";".join(under_days) or None,
        number_of_nights_overcharged=len(over_days),
        number_of_nights_undercharged=len(under_days),
        usd_total_overcharged_amount=over_total.quantize(_CENT, ROUND_HALF_UP),
        usd_total_undercharged_amount=under_total.quantize(_CENT, ROUND_HALF_UP),
        usd_total_amount_differ=net,
        review_status=status,
        extraction_warnings=warn_str,
    )

    records: list[FolioRecord] = []
    for line, n_usd in zip(extraction.nightly_charges, per_night_usd):
        per_night_amt = (line.amount / line.nights).quantize(_CENT, ROUND_HALF_UP)
        records.append(FolioRecord(
            **common,
            english_description=(
                line.english_description if line.nights == 1
                else f"{line.english_description} ({line.nights} nights)"
            ),
            original_per_day_price=per_night_amt,
            usd_per_day_price=n_usd,
        ))

    if not records:
        # Image-only receipts sometimes carry a single total and no per-night
        # breakdown; still persist one header row so the folio is visible.
        records.append(FolioRecord(
            **common,
            english_description=None,
            original_per_day_price=None,
            usd_per_day_price=None,
        ))
    return records


def derive_year_and_quarter(year_month: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not year_month or "-" not in year_month:
        return None, None
    y, m = year_month.split("-", 1)
    try:
        q = (int(m) - 1) // 3 + 1
    except ValueError:
        return None, None
    return y, f"{y}-Q{q}"
