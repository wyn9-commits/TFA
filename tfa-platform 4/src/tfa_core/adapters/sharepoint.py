"""tfa_core.adapters.sharepoint — SharePoint → Blob ingestion via Microsoft Graph.

Design:
- The SharePoint document library is where users drop folios; **Blob remains the
  immutable processing source** (per the end-state diagram). This module mirrors
  new/changed PDFs from SharePoint into the raw container, and the existing
  Event Grid → queue → pipeline flow handles everything downstream unchanged.

- **Auth:** the Function App's Managed Identity is granted Microsoft Graph
  `Sites.Selected` application permission, then given access to ONLY the TFA
  site (least privilege — a governance-review-friendly alternative to
  Sites.Read.All):
      POST /sites/{site-id}/permissions  { roles: ["read"], grantedToIdentities: [...] }

- **Incremental:** Graph *delta queries*. The delta link is persisted in Cosmos,
  so each timer run fetches only items created/changed since the previous run —
  no full library rescans, no missed files between runs.

- **Idempotent:** each driveItem's `id` + `eTag` form the sync key. Unchanged
  items are skipped; changed items overwrite their blob (content hash then
  changes, so the pipeline's document_id changes and rows are re-upserted).

- **Identifier mapping:** library layout is expected to be
      <library root>/<identifier>/<file.pdf>
  The uploader recorded in SharePoint (`createdBy.user.displayName`) becomes
  `uploading_person_name`, replacing the legacy "-<user>" folder-name suffix.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterator, Optional

import httpx

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from azure.core.credentials import TokenCredential

logger = logging.getLogger(__name__)

_GRAPH = "https://graph.microsoft.com/v1.0"
_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
_SUPPORTED = re.compile(r"\.(pdf|png|jpe?g|xlsx?)$", re.IGNORECASE)


@dataclass
class SharePointItem:
    item_id: str
    etag: str
    name: str
    identifier: str            # first-level folder under the library root
    uploader: str
    size: int
    download_url: str
    deleted: bool = False


class SharePointSource:
    def __init__(self, credential: "TokenCredential", site_id: str, drive_id: str,
                 timeout_s: float = 120.0):
        self._credential = credential
        self._site_id = site_id
        self._drive_id = drive_id
        self._timeout = timeout_s

    # ------------------------------------------------------------------ #

    def _headers(self) -> dict[str, str]:
        token = self._credential.get_token(_GRAPH_SCOPE)
        return {"Authorization": f"Bearer {token.token}"}

    def delta(self, delta_link: Optional[str]) -> tuple[list[SharePointItem], str]:
        """Fetch changes since `delta_link` (None = initial full enumeration).

        Returns (items, new_delta_link). Follows @odata.nextLink pagination
        within the run; the final @odata.deltaLink is persisted by the caller.
        """
        url = delta_link or f"{_GRAPH}/drives/{self._drive_id}/root/delta"
        items: list[SharePointItem] = []
        with httpx.Client(timeout=self._timeout) as client:
            while True:
                resp = client.get(url, headers=self._headers())
                resp.raise_for_status()
                page = resp.json()
                for raw in page.get("value", []):
                    parsed = self._parse(raw)
                    if parsed:
                        items.append(parsed)
                if "@odata.nextLink" in page:
                    url = page["@odata.nextLink"]
                    continue
                return items, page["@odata.deltaLink"]

    def download(self, item: SharePointItem) -> bytes:
        with httpx.Client(timeout=self._timeout, follow_redirects=True) as client:
            resp = client.get(item.download_url, headers=self._headers())
            resp.raise_for_status()
            return resp.content

    # ------------------------------------------------------------------ #

    def _parse(self, raw: dict[str, Any]) -> Optional[SharePointItem]:
        if "folder" in raw:                      # folders themselves: ignore
            return None
        name = raw.get("name", "")
        if not _SUPPORTED.search(name):
            return None

        deleted = "deleted" in raw
        parent_path = (raw.get("parentReference") or {}).get("path", "")
        # ".../drive/root:/<identifier>[/subfolders]" → first path segment
        _, _, rel = parent_path.partition("root:")
        segments = [s for s in rel.split("/") if s]
        if not segments:
            logger.debug("Skipping %s: not inside an identifier folder.", name)
            return None
        identifier = segments[0]

        uploader = (
            ((raw.get("createdBy") or {}).get("user") or {}).get("displayName")
            or "UnknownUser"
        )
        return SharePointItem(
            item_id=raw["id"],
            etag=raw.get("eTag", ""),
            name=name,
            identifier=identifier,
            uploader=uploader,
            size=raw.get("size", 0),
            download_url=raw.get("@microsoft.graph.downloadUrl", ""),
            deleted=deleted,
        )


# --------------------------------------------------------------------------- #
# Sync state + orchestration
# --------------------------------------------------------------------------- #

class SharePointSyncState:
    """Delta link + per-item eTags, stored in the existing Cosmos container
    (partition 'sharepoint-sync') so no new infrastructure is needed."""

    _PARTITION = "sharepoint-sync"

    def __init__(self, cosmos_container):
        self._c = cosmos_container

    def get_delta_link(self) -> Optional[str]:
        try:
            item = self._c.read_item("delta-link", partition_key=self._PARTITION)
            return item.get("delta_link")
        except Exception:
            return None

    def save_delta_link(self, link: str) -> None:
        self._c.upsert_item({
            "id": "delta-link", "identifier": self._PARTITION, "delta_link": link,
        })

    def known_etag(self, item_id: str) -> Optional[str]:
        try:
            doc = self._c.read_item(f"sp-{item_id}", partition_key=self._PARTITION)
            return doc.get("etag")
        except Exception:
            return None

    def remember(self, item: SharePointItem, blob_path: str) -> None:
        self._c.upsert_item({
            "id": f"sp-{item.item_id}", "identifier": self._PARTITION,
            "etag": item.etag, "blob_path": blob_path, "file_name": item.name,
            "uploader": item.uploader,
        })


@dataclass
class SyncResult:
    ingested: int = 0
    skipped: int = 0
    deleted_flagged: int = 0
    errors: int = 0


def sync_sharepoint_to_blob(
    source: SharePointSource,
    state: SharePointSyncState,
    blob_repo,                     # BlobRepository
    raw_container: str,
) -> SyncResult:
    """One incremental sync run. Blob write path: <identifier>/<file name>."""
    result = SyncResult()
    delta_link = state.get_delta_link()
    items, new_link = source.delta(delta_link)
    logger.info("SharePoint delta returned %d item(s) (initial=%s).",
                len(items), delta_link is None)

    for item in items:
        try:
            if item.deleted:
                # Immutable source: never delete blobs on SharePoint deletion.
                # Flag it so operators can decide (governance: Restricted data).
                logger.warning("SharePoint item deleted upstream: %s/%s "
                               "(blob retained).", item.identifier, item.name)
                result.deleted_flagged += 1
                continue
            if state.known_etag(item.item_id) == item.etag:
                result.skipped += 1
                continue
            if not item.download_url:
                logger.warning("No download URL for %s; will retry next run.", item.name)
                result.errors += 1
                continue

            content = source.download(item)
            blob_path = f"{item.identifier}/{item.name}"
            blob_repo.upload(raw_container, blob_path, content, overwrite=True)
            state.remember(item, blob_path)
            result.ingested += 1
            logger.info("Ingested %s (%d bytes) → %s/%s [uploader=%s]",
                        item.name, len(content), raw_container, blob_path,
                        item.uploader)
        except Exception:
            logger.exception("Failed to ingest %s/%s; continuing.",
                             item.identifier, item.name)
            result.errors += 1

    # Persist the delta link only after the pass, so a crashed run replays.
    state.save_delta_link(new_link)
    logger.info("Sync complete: %s", result)
    return result
