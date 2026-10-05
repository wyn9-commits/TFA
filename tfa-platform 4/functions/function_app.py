"""functions/function_app.py — Azure Functions v2 programming model.

Replaces the legacy APScheduler-inside-Flask + tracking-txt-file poller:

  Blob created (Event Grid) ──> enqueue_folio ──> Storage Queue
  Storage Queue ──> process_folio ──> pipeline ──> SQL + Cosmos status
  5 failed deliveries ──> automatic *-poison queue ──> handle_poison

Retries, back-off, and dead-lettering come from the platform, not hand-rolled
loops. Every document is tracked individually in Cosmos, so a single bad PDF
can never stall a whole batch again.
"""
from __future__ import annotations

import json
import logging
import os
from decimal import Decimal

import azure.functions as func

from tfa_core.config import get_credential, get_settings
from tfa_core.container import build_pipeline
from tfa_core.pipeline.ingest import RateContext, WorkItem
from tfa_core.domain.reconciliation import FxRate, NegotiatedRate, RateDirectory

app = func.FunctionApp()
logger = logging.getLogger("tfa.functions")

_pipeline: FolioPipeline | None = None


def _get_pipeline() -> FolioPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = build_pipeline()
    return _pipeline


def _load_rate_context() -> RateContext:
    """Load negotiated rates + FX.

    DEV: seeded from a JSON blob (rates/negotiated_rates.json, rates/fx.json).
    PROD: replace with the SAP-fed SQL tables once the integration lands —
    the RateContext interface is the seam.
    """
    from tfa_core.adapters.blob_cosmos import BlobRepository
    s = get_settings()
    blobs = BlobRepository(s.storage_account_url, get_credential())
    rates_raw = blobs.download(s.raw_container, "rates/negotiated_rates.json")
    fx_raw = blobs.download(s.raw_container, "rates/fx.json")
    rates = [
        NegotiatedRate(hotel_name=r["hotel_name"], country=r.get("country"),
                       nightly_rate_usd=Decimal(str(r["nightly_rate_usd"])))
        for r in json.loads(rates_raw)
    ]
    fx = {
        f["currency"].upper(): FxRate(currency=f["currency"].upper(),
                                      usd_per_unit=Decimal(str(f["usd_per_unit"])))
        for f in json.loads(fx_raw)
    }
    return RateContext(directory=RateDirectory(rates), fx_by_currency=fx)


# --------------------------------------------------------------------------- #
# 0. SharePoint → Blob incremental sync (every 5 min, Graph delta query)
# --------------------------------------------------------------------------- #

@app.function_name("sync_sharepoint")
@app.timer_trigger(arg_name="timer", schedule="%TFA_SHAREPOINT_SYNC_CRON%",
                   run_on_startup=False)
def sync_sharepoint(timer: func.TimerRequest) -> None:
    from azure.cosmos import CosmosClient, PartitionKey
    from tfa_core.adapters.sharepoint import (
        SharePointSource, SharePointSyncState, sync_sharepoint_to_blob,
    )
    from tfa_core.adapters.blob_cosmos import BlobRepository

    s = get_settings()
    if not (s.sharepoint_enabled and s.sharepoint_site_id and s.sharepoint_drive_id):
        logger.info("SharePoint sync disabled or not configured; skipping.")
        return

    cred = get_credential()
    cosmos = CosmosClient(s.cosmos_endpoint, credential=cred)
    db = cosmos.create_database_if_not_exists(s.cosmos_database)
    container = db.create_container_if_not_exists(
        id=s.cosmos_status_container, partition_key=PartitionKey(path="/identifier"),
    )

    source = SharePointSource(cred, s.sharepoint_site_id, s.sharepoint_drive_id)
    state = SharePointSyncState(container)
    blobs = BlobRepository(s.storage_account_url, cred)
    result = sync_sharepoint_to_blob(source, state, blobs, s.raw_container)
    logger.info("SharePoint sync: ingested=%d skipped=%d deleted_flagged=%d errors=%d",
                result.ingested, result.skipped, result.deleted_flagged, result.errors)
    # The blob uploads above raise BlobCreated events, which fan into
    # enqueue_folio → process_folio automatically. No direct coupling here.


# --------------------------------------------------------------------------- #
# 1. Blob created -> enqueue
# --------------------------------------------------------------------------- #

@app.function_name("enqueue_folio")
@app.event_grid_trigger(arg_name="event")
@app.queue_output(arg_name="outq",
                  queue_name="%TFA_WORK_QUEUE%",
                  connection="TFA_QUEUE_CONNECTION")
def enqueue_folio(event: func.EventGridEvent, outq: func.Out[str]) -> None:
    data = event.get_json()
    url: str = data.get("url", "")
    # https://<acct>.blob.core.windows.net/<container>/<identifier>/<file>
    # (SharePoint-sourced blobs; uploader is resolved from sync state below.
    #  Legacy "<identifier>-<user>/<file>" paths are still parsed for
    #  backwards compatibility during migration.)
    try:
        _, _, _, container, *path_parts = url.split("/", 4) + [""]
        blob_path = path_parts[0]
        folder, file_name = blob_path.split("/", 1)
    except ValueError:
        logger.warning("Ignoring blob event with unparseable url: %s", url)
        return
    if blob_path.startswith("rates/"):
        return  # config data, not a folio

    if "-" in folder and folder.rsplit("-", 1)[1]:
        identifier, _, person = folder.partition("-")        # legacy layout
    else:
        identifier, person = folder, _uploader_from_sync_state(blob_path)

    item = WorkItem(container=container, blob_path=blob_path,
                    identifier=identifier, uploading_person_name=person,
                    file_name=file_name)
    outq.set(json.dumps(item.__dict__))
    logger.info("Enqueued %s/%s", identifier, file_name)


def _uploader_from_sync_state(blob_path: str) -> str:
    """Look up who dropped this file in SharePoint (recorded at sync time)."""
    try:
        from azure.cosmos import CosmosClient
        s = get_settings()
        cosmos = CosmosClient(s.cosmos_endpoint, credential=get_credential())
        container = cosmos.get_database_client(s.cosmos_database) \
                          .get_container_client(s.cosmos_status_container)
        rows = list(container.query_items(
            "SELECT c.file_name, c.blob_path, c.id FROM c "
            "WHERE c.identifier = 'sharepoint-sync' AND c.blob_path = @p",
            parameters=[{"name": "@p", "value": blob_path}],
            partition_key="sharepoint-sync",
        ))
        if rows:
            # uploader was logged at ingest; stored on the sp-* item
            item = container.read_item(rows[0]["id"], partition_key="sharepoint-sync")
            return item.get("uploader") or "UnknownUser"
    except Exception:
        logger.warning("Could not resolve uploader for %s.", blob_path, exc_info=True)
    return "UnknownUser"


# --------------------------------------------------------------------------- #
# 2. Queue -> process
# --------------------------------------------------------------------------- #

@app.function_name("process_folio")
@app.queue_trigger(arg_name="msg",
                   queue_name="%TFA_WORK_QUEUE%",
                   connection="TFA_QUEUE_CONNECTION")
def process_folio(msg: func.QueueMessage) -> None:
    payload = json.loads(msg.get_body().decode())
    item = WorkItem(**payload)
    rates = _load_rate_context()
    status = _get_pipeline().process(item, rates)
    logger.info("Processed %s -> %s (dequeue_count=%s)",
                item.file_name, status, msg.dequeue_count)


# --------------------------------------------------------------------------- #
# 3. Poison queue -> record permanent failure
# --------------------------------------------------------------------------- #

@app.function_name("handle_poison")
@app.queue_trigger(arg_name="msg",
                   queue_name="%TFA_WORK_QUEUE%-poison",
                   connection="TFA_QUEUE_CONNECTION")
def handle_poison(msg: func.QueueMessage) -> None:
    from tfa_core.adapters.blob_cosmos import ProcessingStatusRepository
    s = get_settings()
    payload = json.loads(msg.get_body().decode())
    status_repo = ProcessingStatusRepository(
        s.cosmos_endpoint, s.cosmos_database, s.cosmos_status_container,
        get_credential(),
    )
    status_repo.mark(
        document_id=f"poison-{payload.get('identifier')}-{payload.get('file_name')}"[:255],
        identifier=payload.get("identifier", "unknown"),
        file_name=payload.get("file_name", "unknown"),
        status="failed",
        detail="Exceeded max delivery attempts; moved to poison queue for triage.",
    )
    logger.error("POISON: %s", payload)
