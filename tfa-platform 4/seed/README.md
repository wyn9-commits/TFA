# Seed reference data

The extraction worker reads negotiated rates and FX from two blobs. Without
them every document reconciles with **no matched rate and no USD conversion** —
extraction still succeeds, but the overcharge columns stay null.

Upload once per environment:

```bash
az storage blob upload --auth-mode login \
  --account-name <storage-account> --container-name travel-folios \
  --name rates/negotiated_rates.json --file seed/negotiated_rates.json --overwrite

az storage blob upload --auth-mode login \
  --account-name <storage-account> --container-name travel-folios \
  --name rates/fx.json --file seed/fx.json --overwrite
```

**Replace the sample values.** `negotiated_rates.json` must list the hotels in
your contract at their contracted USD nightly rate; the two entries here are
from the Argentina samples and exist only to prove the format.

`hotel_name` is matched against the vendor name extracted from the folio —
exact match first, then token-blocked fuzzy match. Use the name as it appears
on invoices, not the internal procurement name.

FX values are `usd_per_unit`: multiply a folio amount by this to get USD.

Production replaces both with the SAP feed; `RateProvider` in
`src/tfa/domain/ports.py` is the seam. Note the open item in `REVIEW.md`: FX is
currently looked up by currency only, not by folio date.
