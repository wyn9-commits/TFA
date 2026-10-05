"""tfa_core.config — typed settings, fail-fast at startup.

All secrets come from Managed Identity / Key Vault references, never from
username+password pairs. The legacy SQL_USERNAME/SQL_PASSWORD auth is gone:
SQL access uses Entra ID access tokens acquired by the same identity that
accesses Blob and Foundry.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal, Optional

from azure.identity import DefaultAzureCredential
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TFA_", case_sensitive=False)

    environment: Literal["dev", "test", "prod"] = "dev"

    # --- Storage -----------------------------------------------------------
    storage_account_url: str = Field(..., description="https://<acct>.blob.core.windows.net")
    raw_container: str = "travel-folios"          # immutable source documents
    queue_account_url: str = Field(..., description="https://<acct>.queue.core.windows.net")
    work_queue: str = "folio-extract"
    poison_queue: str = "folio-extract-poison"

    # --- Document Intelligence / Content Understanding ---------------------
    docintel_endpoint: str
    docintel_model: str = "prebuilt-layout"       # layout + tables + kv pairs, all pages

    # --- LLM (Microsoft Foundry) -------------------------------------------
    llm_provider: Literal["azure_openai", "anthropic_foundry"] = "azure_openai"
    llm_endpoint: str
    llm_deployment: str = "gpt-5.6"               # or claude-sonnet-5 with anthropic_foundry
    llm_api_version: str = "2025-04-01-preview"
    llm_max_output_tokens: int = 16000
    llm_extraction_max_attempts: int = 3          # schema-repair retry budget
    llm_translation_deployment: Optional[str] = None  # cheaper tier for bulk translation; None = same model
    extraction_engine: Literal["loop", "graph"] = "loop"  # graph = LangGraph (traced in LangSmith)

    # --- SQL ---------------------------------------------------------------
    sql_server: str                                # <server>.database.windows.net
    sql_database: str
    sql_table: str = "tfa_folio_lines"

    # --- Cosmos (processing status) ---------------------------------------
    cosmos_endpoint: str
    cosmos_database: str = "tfa"
    cosmos_status_container: str = "processing_status"

    # --- SharePoint source (Microsoft Graph) --------------------------------
    sharepoint_enabled: bool = True
    sharepoint_site_id: Optional[str] = None    # {hostname},{site-guid},{web-guid}
    sharepoint_drive_id: Optional[str] = None   # document library drive id
    sharepoint_sync_cron: str = "0 */5 * * * *" # every 5 minutes

    # --- Behaviour ---------------------------------------------------------
    render_dpi: int = 300                          # legacy used 150; too low for scanned folios
    max_pages_per_doc: int = 25
    user_assigned_client_id: Optional[str] = None  # UAMI; None → system-assigned


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  (env-driven)


@lru_cache(maxsize=1)
def get_credential() -> DefaultAzureCredential:
    s = get_settings()
    return DefaultAzureCredential(managed_identity_client_id=s.user_assigned_client_id)
