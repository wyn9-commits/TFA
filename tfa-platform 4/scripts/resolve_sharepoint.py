#!/usr/bin/env python3
"""Paste a SharePoint URL, get the settings the sync needs.

    python scripts/resolve_sharepoint.py "https://contoso.sharepoint.com/sites/hal_travel_services/Shared%20Documents/Forms/AllItems.aspx?viewid=..."

Graph addresses libraries by composite GUIDs that nobody has to hand. This
resolves the URL from your browser's address bar into those ids, previews the
folder structure, and shows the batch identifier each folder will produce —
so you can confirm the grouping before any document is ingested.

    --write     append the resolved ids to .env
    --depth N   how many folder levels form the identifier (0 = all)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

BOLD, DIM, GREEN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--depth", type=int, default=0,
                    help="Folder levels forming the batch identifier (0 = all)")
    ap.add_argument("--write", action="store_true", help="Append ids to .env")
    args = ap.parse_args()

    from azure.identity import DefaultAzureCredential

    from tfa_core.adapters.sharepoint_locator import (
        SharePointLocator, derive_identifier,
    )

    credential = DefaultAzureCredential()
    locator = SharePointLocator(credential)

    print(f"\n{BOLD}Resolving{RESET} {args.url[:96]}\n")
    try:
        target = locator.resolve(args.url)
    except PermissionError as exc:
        print(f"{YELLOW}Permission denied{RESET}\n  {exc}")
        return 1
    except Exception as exc:
        print(f"{YELLOW}Could not resolve{RESET}\n  {type(exc).__name__}: {exc}")
        return 1

    print(f"  site      {target.site_name}")
    print(f"  library   {target.library_name}")
    if target.root_folder:
        print(f"  scoped to {target.root_folder}")
    print(f"\n{BOLD}Settings{RESET}\n")
    for line in target.as_env().splitlines():
        print(f"  {line}")
    if args.depth:
        print(f"  TFA_SHAREPOINT_IDENTIFIER_DEPTH={args.depth}")

    # Preview the structure so the grouping is confirmed before ingestion.
    print(f"\n{BOLD}Folder structure and the batches it produces{RESET}\n")
    try:
        import httpx
        token = credential.get_token("https://graph.microsoft.com/.default").token
        headers = {"Authorization": f"Bearer {token}"}
        base = f"https://graph.microsoft.com/v1.0/drives/{target.drive_id}"
        start = (f"{base}/root:/{target.root_folder}:/children"
                 if target.root_folder else f"{base}/root/children")

        def walk(url: str, prefix: str, depth: int) -> None:
            if depth > 2:
                return
            items = httpx.get(url, headers=headers, timeout=30).json().get("value", [])
            for item in items[:12]:
                name = item.get("name", "")
                path = f"{prefix}/{name}".strip("/")
                if "folder" in item:
                    count = item["folder"].get("childCount", 0)
                    identifier = derive_identifier(path, args.depth, target.root_folder)
                    label = (f"{GREEN}{identifier}{RESET}" if identifier
                             else f"{DIM}(no batch){RESET}")
                    print(f"  {'  ' * depth}{name}/  {DIM}{count} items{RESET}  -> {label}")
                    walk(f"{base}/items/{item['id']}/children", path, depth + 1)
                elif depth == 0:
                    print(f"  {name}  {YELLOW}<- at the library root, will be skipped{RESET}")

        walk(start, target.root_folder, 0)
    except Exception as exc:
        print(f"  {DIM}(could not preview: {exc}){RESET}")

    print(f"\n{DIM}Files must sit inside a folder. Anything at the library root has"
          f"\nno batch to belong to and is skipped.{RESET}")

    if args.write:
        env = ROOT / ".env"
        existing = env.read_text() if env.exists() else ""
        lines = [l for l in existing.splitlines()
                 if not l.startswith(("TFA_SHAREPOINT_SITE_ID",
                                      "TFA_SHAREPOINT_DRIVE_ID",
                                      "TFA_SHAREPOINT_ROOT_FOLDER",
                                      "TFA_SHAREPOINT_IDENTIFIER_DEPTH"))]
        lines.append(target.as_env())
        if args.depth:
            lines.append(f"TFA_SHAREPOINT_IDENTIFIER_DEPTH={args.depth}")
        env.write_text("\n".join(lines).rstrip() + "\n")
        print(f"\n{GREEN}Written to .env{RESET}")
    else:
        print(f"\n{DIM}Re-run with --write to save these to .env.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
