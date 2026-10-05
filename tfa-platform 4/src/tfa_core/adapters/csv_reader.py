"""tfa_core.adapters.csv_reader — CSV → DocumentLayout.

Travel libraries hold exported ledgers and agency statements as CSV alongside
scanned folios. Like Excel, a CSV is already structured, so it bypasses
Document Intelligence entirely and is rendered to a markdown table for the
same Stage-2 extraction.

Two decisions worth stating:

* **Delimiter and encoding are sniffed, not assumed.** LATAM exports are
  frequently semicolon-delimited (a comma is the decimal separator in
  es-AR/es-CO) and often arrive as cp1252 rather than UTF-8. Assuming a comma
  would collapse every row into one cell and produce silent nonsense rather
  than an error.
* **Numbers are passed through verbatim.** Deciding that "1.234,56" means
  1234.56 is the extraction model's job, guided by the locale rules in the
  prompt — not something to guess here, where the country context is absent.
"""
from __future__ import annotations

import csv
import io
import logging

from tfa_core.domain.values import DocumentLayout

logger = logging.getLogger(__name__)

MAX_ROWS = 2000
MAX_COLS = 40
_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


def _decode(content: bytes) -> str:
    for encoding in _ENCODINGS:
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    # latin-1 cannot fail, but be explicit rather than relying on that.
    return content.decode("latin-1", errors="replace")


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        # Sniffer fails on single-column files and short samples; fall back to
        # whichever candidate appears most in the first lines.
        head = "\n".join(sample.splitlines()[:5])
        counts = {d: head.count(d) for d in (";", ",", "\t", "|")}
        best = max(counts, key=counts.get)
        return best if counts[best] else ","


def analyze_csv(content: bytes, filename: str) -> DocumentLayout:
    text = _decode(content)
    delimiter = _sniff_delimiter(text[:4096])
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)

    rows: list[list[str]] = []
    truncated = False
    for index, row in enumerate(reader):
        if index >= MAX_ROWS:
            truncated = True
            break
        cells = [(c or "").strip() for c in row[:MAX_COLS]]
        if any(cells):
            rows.append(cells)

    if not rows:
        markdown = "(the file contains no data rows)"
    else:
        width = max(len(r) for r in rows)
        padded = [r + [""] * (width - len(r)) for r in rows]
        header = "| " + " | ".join(padded[0]) + " |"
        divider = "| " + " | ".join(["---"] * width) + " |"
        body = "\n".join("| " + " | ".join(r) + " |" for r in padded[1:])
        markdown = f"## CSV: {filename}\n\n{header}\n{divider}\n{body}"
        if truncated:
            markdown += f"\n\n(truncated at {MAX_ROWS} rows)"

    logger.info("CSV %s: delimiter %r, %d row(s), %d chars.",
                filename, delimiter, len(rows), len(markdown))
    return DocumentLayout(
        markdown_text=markdown,
        page_count=1,
        page_images_b64=[],          # text-only input
        languages=[],
        ocr_confidence_floor=1.0,    # native data, no OCR uncertainty
        images_omitted=True,
    )
