"""Shared rate-context loader for the operator scripts.

Mirrors what the worker does at runtime (`functions/function_app.py`), kept in
one place so a script and the worker cannot disagree about where reference data
lives or how it is parsed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from tfa_core.domain.reconciliation import FxRate, NegotiatedRate, RateDirectory


@dataclass
class RateContext:
    directory: RateDirectory
    fx_by_currency: dict[str, FxRate]


def load_rate_context(container) -> RateContext:
    """Reads rates/negotiated_rates.json and rates/fx.json from the raw container.

    Missing files are not fatal here: extraction still works, the folio simply
    reconciles with no matched rate. The script reports that plainly rather
    than failing, because 'no rate configured' is a common and recoverable
    first-run state.
    """
    raw = container.settings.raw_container
    try:
        rates_json = container.blobs.download(raw, "rates/negotiated_rates.json")
        rates = [
            NegotiatedRate(hotel_name=r["hotel_name"], country=r.get("country"),
                           nightly_rate_usd=Decimal(str(r["nightly_rate_usd"])))
            for r in json.loads(rates_json)
        ]
    except Exception:
        rates = []

    try:
        fx_json = container.blobs.download(raw, "rates/fx.json")
        fx = {
            f["currency"].upper(): FxRate(currency=f["currency"].upper(),
                                          usd_per_unit=Decimal(str(f["usd_per_unit"])))
            for f in json.loads(fx_json)
        }
    except Exception:
        fx = {}

    fx.setdefault("USD", FxRate(currency="USD", usd_per_unit=Decimal("1")))
    return RateContext(directory=RateDirectory(rates), fx_by_currency=fx)
