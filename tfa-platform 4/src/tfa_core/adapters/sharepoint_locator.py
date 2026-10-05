"""tfa_core.adapters.sharepoint_locator — paste a URL, get a working config.

Microsoft Graph addresses libraries by `site-id` and `drive-id`, which are
composite GUIDs nobody has to hand. What people *do* have is the URL from the
browser address bar. This module turns one into the other, and derives batch
identifiers from a nested folder structure.

    https://contoso.sharepoint.com/sites/hal_travel_services/Shared%20Documents/Forms/AllItems.aspx?...
    https://contoso.sharepoint.com/sites/hal_travel_services/Shared Documents/Argentina/2025

Both resolve to the same site and library; the second also pins a starting
subfolder so a sync can be scoped to one country or year.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

_GRAPH = "https://graph.microsoft.com/v1.0"
_GRAPH_SCOPE = "https://graph.microsoft.com/.default"

#: SharePoint view pages and system folders that are part of the UI, not the data.
_VIEW_NOISE = re.compile(r"/(Forms|_layouts)(/|$)", re.IGNORECASE)


@dataclass
class SharePointTarget:
    """Everything the sync needs, resolved from a URL."""
    site_id: str
    drive_id: str
    site_name: str
    library_name: str
    #: Optional path inside the library to start from, e.g. "Argentina/2025".
    #: Empty means the whole library.
    root_folder: str = ""

    def as_env(self) -> str:
        lines = [
            f"TFA_SHAREPOINT_SITE_ID={self.site_id}",
            f"TFA_SHAREPOINT_DRIVE_ID={self.drive_id}",
        ]
        if self.root_folder:
            lines.append(f"TFA_SHAREPOINT_ROOT_FOLDER={self.root_folder}")
        return "\n".join(lines)


@dataclass
class ParsedUrl:
    hostname: str
    site_path: str          # e.g. "sites/hal_travel_services"
    library_and_folders: str  # e.g. "Shared Documents/Argentina/2025"


def parse_sharepoint_url(url: str) -> ParsedUrl:
    """Splits a browser URL into hostname, site path and in-library path.

    Handles the three shapes people actually paste: a document-library view
    (`/Forms/AllItems.aspx`), a folder link, and a link with the folder in the
    `id=` query parameter (what "Copy link" produces for a folder).
    """
    parsed = urlparse(url.strip())
    if not parsed.hostname:
        raise ValueError(f"Not a URL: {url!r}")

    path = unquote(parsed.path)

    # "Copy link" on a folder puts the real path in ?id=/sites/x/Shared Documents/...
    query_id = ""
    if parsed.query:
        for part in parsed.query.split("&"):
            key, _, value = part.partition("=")
            if key.lower() in ("id", "rootfolder") and value:
                query_id = unquote(value)
                break
    if query_id:
        path = query_id

    path = _VIEW_NOISE.split(path)[0]
    segments = [s for s in path.split("/") if s]

    # Site path is "sites/<name>" or "teams/<name>"; a root-site URL has neither.
    if len(segments) >= 2 and segments[0].lower() in ("sites", "teams"):
        site_path = f"{segments[0]}/{segments[1]}"
        remainder = segments[2:]
    else:
        site_path = ""
        remainder = segments

    return ParsedUrl(
        hostname=parsed.hostname,
        site_path=site_path,
        library_and_folders="/".join(remainder),
    )


class SharePointLocator:
    """Resolves a URL to Graph identifiers. Requires Graph read access."""

    def __init__(self, credential, timeout_s: float = 30.0):
        self._credential = credential
        self._timeout = timeout_s

    def _get(self, url: str) -> dict:
        import httpx
        token = self._credential.get_token(_GRAPH_SCOPE).token
        response = httpx.get(url, headers={"Authorization": f"Bearer {token}"},
                             timeout=self._timeout)
        if response.status_code == 403:
            raise PermissionError(
                "Graph returned 403. The identity needs Sites.Selected with read "
                "granted on THIS site, or Sites.Read.All. See CONFIGURATION.md §3.")
        response.raise_for_status()
        return response.json()

    def resolve(self, url: str) -> SharePointTarget:
        parsed = parse_sharepoint_url(url)

        site_url = (f"{_GRAPH}/sites/{parsed.hostname}:/{parsed.site_path}"
                    if parsed.site_path else f"{_GRAPH}/sites/{parsed.hostname}")
        site = self._get(site_url)
        site_id, site_name = site["id"], site.get("displayName", parsed.site_path)
        logger.info("Resolved site %s -> %s", parsed.site_path or "(root)", site_id)

        drives = self._get(f"{_GRAPH}/sites/{site_id}/drives")["value"]
        if not drives:
            raise ValueError(f"Site '{site_name}' has no document libraries.")

        wanted, root_folder = self._split_library(parsed.library_and_folders, drives)
        drive = self._match_drive(wanted, drives)
        logger.info("Resolved library '%s' -> %s (root folder %r)",
                    drive["name"], drive["id"], root_folder)

        return SharePointTarget(
            site_id=site_id, drive_id=drive["id"], site_name=site_name,
            library_name=drive["name"], root_folder=root_folder)

    @staticmethod
    def _split_library(path: str, drives: list[dict]) -> tuple[str, str]:
        """Separates the library name from any folder path beneath it.

        Library names contain spaces ("Shared Documents"), so the split cannot
        simply take the first segment — it matches against the libraries the
        site actually has, longest name first.
        """
        if not path:
            return "", ""
        names = sorted((d.get("name", "") for d in drives), key=len, reverse=True)
        for name in names:
            if path == name:
                return name, ""
            if path.startswith(name + "/"):
                return name, path[len(name) + 1:]
        # "Documents" is what Graph calls the library the UI shows as
        # "Shared Documents" — a mismatch that would otherwise look like a
        # missing library.
        first, _, rest = path.partition("/")
        if first.lower() in ("shared documents", "documents", "freigegebene dokumente"):
            return "Documents", rest
        return first, rest

    @staticmethod
    def _match_drive(wanted: str, drives: list[dict]) -> dict:
        if not wanted:
            return drives[0]
        for drive in drives:
            if drive.get("name", "").lower() == wanted.lower():
                return drive
        available = ", ".join(d.get("name", "?") for d in drives)
        raise ValueError(
            f"No library named {wanted!r} on this site. Available: {available}")


# --------------------------------------------------------------------------- #
# Batch identifiers from nested folders
# --------------------------------------------------------------------------- #

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify_segment(value: str) -> str:
    return _SLUG_STRIP.sub("_", value.strip().lower()).strip("_")


def derive_identifier(relative_folder: str, max_depth: int = 0,
                      root_folder: str = "") -> Optional[str]:
    """Turns a folder path into a batch identifier.

        Argentina/2025            -> argentina_2025
        Colombia/2026/Q1          -> colombia_2026_q1
        Mexico/2025/Hotels/Posadas (max_depth=2) -> mexico_2025

    A nested library is the normal case: countries at the top, years beneath,
    sometimes a quarter or vendor below that. The previous implementation took
    only the FIRST segment, so Argentina/2025 and Argentina/2026 collapsed into
    one batch called "Argentina" — two years of folios reconciled together and
    reported as one figure.

    `max_depth` caps how many levels contribute, so a deep tree does not create
    a separate batch per vendor. 0 means use every level.

    Returns None for a file sitting loose at the library root, which has no
    batch to belong to.
    """
    path = relative_folder.strip("/")
    if root_folder:
        prefix = root_folder.strip("/")
        if path == prefix:
            path = ""
        elif path.startswith(prefix + "/"):
            path = path[len(prefix) + 1:]

    segments = [s for s in path.split("/") if s]
    if not segments:
        return None
    # Defensive: this takes a FOLDER path. If a caller passes a file path the
    # last segment carries an extension, which would mint a batch per file.
    if "." in segments[-1] and len(segments[-1].rsplit(".", 1)[-1]) <= 5:
        segments = segments[:-1]
        if not segments:
            return None
    if max_depth > 0:
        segments = segments[:max_depth]

    identifier = "_".join(filter(None, (slugify_segment(s) for s in segments)))
    if not identifier or not identifier[0].isalnum():
        return None
    return identifier[:100]
