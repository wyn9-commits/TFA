"""Cost model for the TFA platform.

Consumption is derived from the ACTUAL code paths, not from guesses:
  * document_intelligence.analyze_pdf -> 1 prebuilt-layout call over all pages
  * needs_page_images() -> images attached only when OCR is unreliable
  * prompting/AttemptPolicy -> 1 LLM call, +1 on validator failure

Rates are Azure list prices (Aug 2026, East US, pay-as-you-go). Substitute your
EA-discounted rates before budgeting; large enterprise agreements commonly land
15-40% below list.

    python scripts/estimate_cost.py --docs 5000
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

# ---------------------------------------------------------------- rates (USD)
# Document Intelligence: prebuilt-LAYOUT is $10/1k pages. Read OCR is $1.50/1k
# but returns no tables, and folio line items are tables — so Layout is
# required, not a convenience. This is the single biggest rate decision here.
DOCINTEL_LAYOUT_PER_1K_PAGES = 10.00

# GPT-5.6 tiers on Foundry. Azure list runs above OpenAI direct; Terra is the
# production default, Luna is the cheap tier worth testing for extraction.
MODELS = {
    "gpt-5.6-terra": (2.75, 16.50),   # (input, output) per 1M tokens, Azure
    "gpt-5.6-luna":  (1.10, 6.60),
    "gpt-5.6-sol":   (6.25, 37.50),
}

# Flat monthly platform costs (dev-sized; see notes in the output).
FIXED = {
    "Azure SQL (GP_S_Gen5 2 vCore, serverless)": 180.0,
    "Cosmos DB (autoscale 4000 RU max, low use)": 35.0,
    "Storage (blob + queue, 100 GB, GZRS)":        25.0,
    "Log Analytics + App Insights (5 GB/mo)":      30.0,
    "Functions Flex Consumption (idle baseline)":  20.0,
    "Private endpoints (7 x ~$7.50)":              53.0,
    "VNet + Private DNS":                          10.0,
}
REDIS_MONTHLY = 55.0     # Standard C1, only above one API replica
API_MONTHLY = 75.0       # Container Apps, 1-2 replicas


@dataclass
class Profile:
    """Measured against the four real LATAM samples."""
    pages: float = 2.0
    born_digital_share: float = 0.85
    chars_per_page: int = 3500
    image_tokens_per_page: int = 1500
    output_tokens: int = 1200
    repair_rate: float = 0.12
    system_prompt_tokens: int = 900

    def tokens(self) -> tuple[float, float]:
        text_in = self.pages * self.chars_per_page * 0.25
        img_in = self.pages * self.image_tokens_per_page
        avg_in = (self.born_digital_share * (text_in + self.system_prompt_tokens)
                  + (1 - self.born_digital_share) * (text_in + img_in + self.system_prompt_tokens))
        return avg_in * (1 + self.repair_rate), self.output_tokens * (1 + self.repair_rate)


def estimate(docs: int, model: str, redis: bool, api: bool) -> None:
    p = Profile()
    tin, tout = p.tokens()
    in_rate, out_rate = MODELS[model]

    docintel = docs * p.pages / 1000 * DOCINTEL_LAYOUT_PER_1K_PAGES
    llm_in = docs * tin / 1e6 * in_rate
    llm_out = docs * tout / 1e6 * out_rate
    fixed = sum(FIXED.values()) + (REDIS_MONTHLY if redis else 0) + (API_MONTHLY if api else 0)

    print(f"\n{'='*62}\n  {docs:,} documents/month · {model}\n{'='*62}")
    print(f"  Document Intelligence (layout, {docs*p.pages:,.0f} pages) {docintel:>14,.2f}")
    print(f"  LLM input  ({docs*tin/1e6:>6.2f}M tokens @ ${in_rate}/M)      {llm_in:>14,.2f}")
    print(f"  LLM output ({docs*tout/1e6:>6.2f}M tokens @ ${out_rate}/M)     {llm_out:>14,.2f}")
    variable = docintel + llm_in + llm_out
    print(f"  {'-'*60}\n  Variable subtotal{' ':27}{variable:>14,.2f}")
    print(f"  Per document{' ':32}{variable/docs:>14,.4f}")
    print()
    for k, v in FIXED.items():
        print(f"  {k:<52}{v:>10,.2f}")
    if redis:
        print(f"  {'Azure Cache for Redis (Standard C1)':<52}{REDIS_MONTHLY:>10,.2f}")
    if api:
        print(f"  {'Container Apps (API, 1-2 replicas)':<52}{API_MONTHLY:>10,.2f}")
    print(f"  {'-'*60}\n  Fixed subtotal{' ':30}{fixed:>14,.2f}")
    print(f"\n  MONTHLY TOTAL{' ':31}{variable+fixed:>14,.2f}")
    print(f"  ANNUAL{' ':38}{(variable+fixed)*12:>14,.2f}\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", type=int, default=5000)
    ap.add_argument("--model", choices=MODELS, default="gpt-5.6-terra")
    ap.add_argument("--no-redis", action="store_true")
    ap.add_argument("--no-api", action="store_true")
    a = ap.parse_args()
    estimate(a.docs, a.model, not a.no_redis, not a.no_api)
