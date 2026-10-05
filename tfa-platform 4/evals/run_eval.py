"""evals/run_eval.py — LangSmith evaluation harness.

Workflow for the model bake-off (GPT-5.6 vs Claude Sonnet 5 vs Opus 5):

1. Label goldens: for each sample document put two files in `evals/golden/`:
     <name>.pdf|.xlsx|.png     the document (copied from SharePoint)
     <name>.expected.json      hand-verified FolioExtraction fields, plus
                               optional "expect_needs_review": true
   (Start with the 4 country samples; grow to ~50 for a decisive bake-off.)

2. Push the dataset once:
     python -m evals.run_eval push --dataset tfa-goldens-v1

3. Evaluate a configuration (engine × provider × deployment):
     TFA_LLM_PROVIDER=azure_openai      TFA_LLM_DEPLOYMENT=gpt-5.6 \
       python -m evals.run_eval run --dataset tfa-goldens-v1 --name gpt56
     TFA_LLM_PROVIDER=anthropic_foundry TFA_LLM_DEPLOYMENT=claude-sonnet-5 \
       python -m evals.run_eval run --dataset tfa-goldens-v1 --name sonnet5

4. Compare experiments side-by-side in the LangSmith UI on:
   money_exact / sum_check / doc_type_correct / field_accuracy /
   review_routing (definitions in evaluators.py). Ship the config that wins
   money_exact; break ties on review_routing.

GOVERNANCE: goldens contain Restricted data. Point LANGSMITH_ENDPOINT at the
self-hosted instance inside the VNet, or obtain sign-off before using cloud.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from langsmith import Client, evaluate

from evals.evaluators import ALL_EVALUATORS

logger = logging.getLogger(__name__)
GOLDEN_DIR = Path(__file__).parent / "golden"
DOC_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg", ".xlsx", ".xlsm", ".xls")


def _golden_pairs() -> list[tuple[Path, dict]]:
    pairs = []
    for expected in sorted(GOLDEN_DIR.glob("*.expected.json")):
        stem = expected.name.removesuffix(".expected.json")
        doc = next((GOLDEN_DIR / f"{stem}{sfx}" for sfx in DOC_SUFFIXES
                    if (GOLDEN_DIR / f"{stem}{sfx}").exists()), None)
        if doc is None:
            logger.warning("No document found for golden %s; skipping.", stem)
            continue
        pairs.append((doc, json.loads(expected.read_text())))
    return pairs


def push_dataset(dataset_name: str) -> None:
    client = Client()
    ds = client.create_dataset(dataset_name=dataset_name,
                               description="TFA folio extraction goldens")
    for doc, expected in _golden_pairs():
        client.create_example(
            dataset_id=ds.id,
            inputs={"document_path": str(doc), "filename": doc.name},
            outputs=expected,
        )
    logger.info("Pushed %d golden(s) to dataset '%s'.",
                len(_golden_pairs()), dataset_name)


def _target(inputs: dict) -> dict:
    """Runs the real Stage-1 + Stage-2 stack on one golden document."""
    from tfa_core.config import get_credential, get_settings
    from tfa_core.adapters.excel_reader import analyze_excel
    from tfa_core.adapters.document_intelligence import LayoutExtractor
    from tfa_core.pipeline.extractor import LLMExtractor
    from tfa_core.adapters.llm_provider import build_llm

    s = get_settings()
    cred = get_credential()
    path = Path(inputs["document_path"])
    content = path.read_bytes()
    name = path.name.lower()

    if name.endswith((".xlsx", ".xlsm", ".xls")):
        layout = analyze_excel(content, path.name)
    else:
        extractor = LayoutExtractor(s.docintel_endpoint, cred, s.docintel_model,
                                    render_dpi=s.render_dpi,
                                    max_pages=s.max_pages_per_doc)
        if name.endswith(".pdf"):
            layout = extractor.analyze_pdf(content, path.name)
        else:
            media = "image/png" if name.endswith(".png") else "image/jpeg"
            layout = extractor.analyze_image(content, path.name, media)

    llm = build_llm(s.llm_provider, s.llm_endpoint, s.llm_deployment,
                    s.llm_api_version, cred)
    if __import__("os").environ.get("TFA_EXTRACTION_ENGINE") == "graph":
        from tfa_core.pipeline.graph_engine import GraphExtractor
        engine = GraphExtractor(llm, s.llm_extraction_max_attempts,
                                s.llm_max_output_tokens)
    else:
        engine = LLMExtractor(llm, s.llm_extraction_max_attempts,
                              s.llm_max_output_tokens)

    outcome = engine.extract(layout, path.name)
    result = outcome.extraction.model_dump(mode="json")
    result["needs_review"] = outcome.needs_review
    result["attempts"] = outcome.attempts
    return result


def _wrap(evaluator):
    def _ls_evaluator(run, example):
        return evaluator(run.outputs or {}, example.outputs or {})
    _ls_evaluator.__name__ = evaluator.__name__
    return _ls_evaluator


def run_eval(dataset_name: str, experiment_name: str) -> None:
    evaluate(
        _target,
        data=dataset_name,
        evaluators=[_wrap(e) for e in ALL_EVALUATORS],
        experiment_prefix=experiment_name,
        max_concurrency=2,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("push")
    pp.add_argument("--dataset", required=True)
    rp = sub.add_parser("run")
    rp.add_argument("--dataset", required=True)
    rp.add_argument("--name", required=True)
    args = p.parse_args()
    if args.cmd == "push":
        push_dataset(args.dataset)
    else:
        run_eval(args.dataset, args.name)
