#!/usr/bin/env python3
"""Push ONE real document through the full pipeline, with every step visible.

    python scripts/process_one.py /path/to/factura.pdf --identifier pilot_001

This is the single most important step between "well-engineered code" and
"a product that works". Until a real folio has been through Document
Intelligence and the LLM, every accuracy claim about this platform is
theoretical.

It runs the production pipeline — the same analyzer, prompt, validators,
reconciliation and persistence the workers use — but synchronously, printing
what happened at each stage so a failure is diagnosable instead of just a
message on a poison queue.

    --dry-run   extract and reconcile, print the result, write nothing to SQL
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

BOLD, DIM, GREEN, RED, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m")


def load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        sys.exit("No .env found. Run scripts/configure.sh first.")
    import os
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def step(n: int, title: str) -> None:
    print(f"\n{BOLD}[{n}] {title}{RESET}")


def money(value) -> str:
    return "—" if value is None else f"{Decimal(str(value)):,.2f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("document", type=Path)
    ap.add_argument("--identifier", default="pilot_001",
                    help="Batch identifier (lowercase, digits, _ and - only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Do not write to SQL or upload to Blob")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not args.document.exists():
        sys.exit(f"Not found: {args.document}")

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format=f"{DIM}%(name)s: %(message)s{RESET}")
    load_env()

    from tfa_core.config import get_credential, get_settings
    from tfa_core.adapters.analyzer import CompositeDocumentAnalyzer
    from tfa_core.adapters.document_intelligence import LayoutExtractor
    from tfa_core.pipeline.extractor import LLMExtractor
    from tfa_core.adapters.llm_provider import build_llm
    from tfa_core.adapters.blob_cosmos import BlobRepository
    from tfa_core.domain.errors import UnsupportedDocumentType
    from tfa_core.domain.identity import compute_document_id, content_hash
    from tfa_core.domain.reconciliation import build_records
    from tfa_core.domain.rules import build_blob_path
    from tfa_core.pipeline.extractor import ExtractionFailed
    from tfa_core.pipeline.folio_rows import build_rows

    content = args.document.read_bytes()
    print(f"{BOLD}Document:{RESET} {args.document.name}  ({len(content):,} bytes)")

    step(0, "Validate the batch identifier and filename")
    try:
        blob_path = build_blob_path(args.identifier, args.document.name)
        print(f"    {GREEN}ok{RESET}  storage path: {blob_path}")
    except Exception as exc:
        print(f"    {RED}rejected{RESET}  {exc}")
        return 1

    sha = content_hash(content)
    document_id = compute_document_id(args.identifier, args.document.name, sha)
    print(f"    document_id: {document_id}  {DIM}(content-addressed){RESET}")

    print(f"\n{DIM}Connecting to Azure...{RESET}")

    # Collaborators built here rather than through a composition root: this
    # repository has no API tier, and a script should not drag one in.
    settings = get_settings()
    credential = get_credential()

    class _Container:
        pass

    container = _Container()
    container.settings = settings
    container.analyzer = CompositeDocumentAnalyzer(
        LayoutExtractor(settings.docintel_endpoint, credential,
                        getattr(settings, "docintel_model", "prebuilt-layout")))
    container.llm = build_llm(settings.llm_provider, settings.llm_endpoint,
                              settings.llm_deployment,
                              getattr(settings, "llm_api_version", None), credential)
    container.engine = LLMExtractor(container.llm)
    container.blobs = BlobRepository(settings.storage_account_url, credential)
    container.folio_writes = None
    container.status = None

    # -- 1. Layout ---------------------------------------------------------- #
    step(1, "Document Intelligence — layout extraction")
    t0 = time.perf_counter()
    try:
        layout = container.analyzer.analyze(content, args.document.name)
    except UnsupportedDocumentType as exc:
        print(f"    {RED}unsupported{RESET}  {exc}")
        return 1
    except Exception as exc:
        print(f"    {RED}failed{RESET}  {type(exc).__name__}: {exc}")
        print(f"    {DIM}Private endpoint? You must be on the VNet or VPN.{RESET}")
        return 1
    t_layout = time.perf_counter() - t0

    print(f"    pages              {layout.page_count}")
    print(f"    markdown           {len(layout.markdown_text):,} chars")
    print(f"    languages          {', '.join(layout.languages) or 'undetected'}")
    print(f"    OCR floor          {layout.ocr_confidence_floor:.2f}")
    print(f"    page images        "
          f"{'omitted (text sufficient)' if layout.images_omitted else len(layout.page_images_b64)}")
    print(f"    elapsed            {t_layout:.1f}s")
    if args.verbose:
        print(f"{DIM}{layout.markdown_text[:1200]}{RESET}")

    # -- 2. Extraction ------------------------------------------------------ #
    step(2, "LLM extraction + validation + repair loop")
    t0 = time.perf_counter()
    try:
        outcome = container.engine.extract(layout, args.document.name)
    except ExtractionFailed as exc:
        print(f"    {RED}failed{RESET}  {exc}")
        print(f"    {DIM}The repair budget was exhausted. The message above is the "
              f"validator complaint the model could not satisfy.{RESET}")
        return 1
    except Exception as exc:
        print(f"    {RED}failed{RESET}  {type(exc).__name__}: {exc}")
        return 1
    t_llm = time.perf_counter() - t0
    x = outcome.extraction

    print(f"    attempts           {outcome.attempts}"
          f"{'  (repaired)' if outcome.attempts > 1 else ''}")
    print(f"    elapsed            {t_llm:.1f}s")
    print(f"    document type      {BOLD}{x.document_type.value}{RESET}")
    print(f"    vendor             {x.hotel_name}")
    print(f"    country            {x.country}")
    print(f"    invoice            {x.invoice_number}")
    print(f"    currency           {x.currency}")
    print(f"    total              {money(x.total_amount)}")
    if x.total_amount_from_words is not None:
        agree = x.total_amount_from_words == x.total_amount
        print(f"    total in words     {money(x.total_amount_from_words)} "
              f"{GREEN + 'agrees' + RESET if agree else RED + 'DISAGREES' + RESET}")
    print(f"    nightly lines      {len(x.nightly_charges)}")
    print(f"    ancillary lines    {len(x.ancillary_charges)}")

    line_sum = (sum((c.amount for c in x.nightly_charges), Decimal("0"))
                + sum((c.amount for c in x.ancillary_charges), Decimal("0")))
    print(f"    lines sum to       {money(line_sum)} "
          f"{GREEN + 'matches total' + RESET if x.total_amount and abs(line_sum - x.total_amount) <= 1 else YELLOW + 'check' + RESET}")

    if x.low_confidence_fields:
        print(f"    {YELLOW}low confidence{RESET}     {', '.join(x.low_confidence_fields)}")
    for w in outcome.warnings:
        print(f"    {YELLOW}warning{RESET}            {w}")
    print(f"    review status      "
          f"{(YELLOW + 'NEEDS REVIEW' + RESET) if outcome.needs_review else (GREEN + 'auto-approved' + RESET)}")

    # -- 3. Reconciliation --------------------------------------------------- #
    step(3, "Reconciliation against negotiated rates")
    from scripts._rates import load_rate_context   # local helper, see below
    rates = load_rate_context(container)
    rate = rates.directory.match(x.hotel_name, x.country) if x.document_type.value == "hotel_folio" else None
    fx = rates.fx_by_currency.get(x.currency)

    print(f"    matched hotel      {rate.hotel_name if rate else DIM + 'no match' + RESET}")
    print(f"    negotiated rate    {money(rate.nightly_rate_usd) if rate else '—'} USD/night")
    print(f"    FX ({x.currency})          {fx.usd_per_unit if fx else DIM + 'none' + RESET}")

    warnings = list(outcome.warnings)
    if rate is None and x.document_type.value == "hotel_folio":
        warnings.append(f"No negotiated rate matched for '{x.hotel_name}'.")
    if fx is None and x.currency != "USD":
        warnings.append(f"No FX rate for {x.currency}; USD fields will be null.")

    records = build_records(
        x, file_name=args.document.name, identifier_name=args.identifier,
        uploading_person_name="pilot", blob_md5=sha, rate=rate, fx=fx,
        needs_review=outcome.needs_review or bool(warnings), warnings=warnings)
    first = records[0]

    print(f"    nights overcharged {first.number_of_nights_overcharged}")
    print(f"    overcharged        {money(first.usd_total_overcharged_amount)} USD")
    print(f"    undercharged       {money(first.usd_total_undercharged_amount)} USD")
    print(f"    {BOLD}net difference     {money(first.usd_total_amount_differ)} USD{RESET}")

    # -- 4. Persist ---------------------------------------------------------- #
    step(4, "Persist" + (" (SKIPPED — dry run)" if args.dry_run else ""))
    if args.dry_run:
        print(f"    {DIM}Nothing written. Re-run without --dry-run to store.{RESET}")
    else:
        if container.folio_writes is None:
            print(f"    {DIM}No SQL store wired in this script — use --dry-run, "
                  f"or run the Function App for persistence.{RESET}")
            return 0
        container.blobs.upload(container.settings.raw_container, blob_path,
                               content, overwrite=True)
        header, lines = build_rows(
            extraction=x, records=records,
            blob_container=container.settings.raw_container, blob_path=blob_path,
            content_md5=sha,
            extraction_model=container.settings.llm_deployment,
            extraction_provider=container.settings.llm_provider,
            prompt_version=getattr(outcome, "prompt_version", None),
            extraction_attempts=outcome.attempts,
            ocr_confidence_floor=layout.ocr_confidence_floor)
        n = container.folio_writes.upsert_document(header=header, lines=lines,
                                                   actor="pilot:process_one")
        container.status.mark(document_id=document_id, identifier=args.identifier,
                              file_name=args.document.name,
                              status=header["review_status"],
                              attempts=outcome.attempts)
        print(f"    {GREEN}stored{RESET}  {n} line(s) under document_id {document_id}")
        print(f"    {DIM}Re-running this command replaces those rows rather than "
              f"duplicating them (content-addressed id).{RESET}")

    print(f"\n{BOLD}Total: {t_layout + t_llm:.1f}s "
          f"(layout {t_layout:.1f}s, extraction {t_llm:.1f}s){RESET}")
    if args.verbose:
        print(f"\n{DIM}{json.dumps(x.model_dump(mode='json'), indent=2)[:3000]}{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
