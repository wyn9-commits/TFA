"""evals/gate.py — CI accuracy gate.

Fails the build when golden extraction quality regresses below thresholds.
This is what prevents a prompt or model change from silently degrading
production accuracy — the failure mode that killed the POC.

    python -m evals.gate --dataset tfa-goldens-v1 \
        --min-money-exact 0.98 --min-doc-type 0.95
"""
from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger("evals.gate")

THRESHOLD_KEYS = {
    "money_exact": "min_money_exact",
    "doc_type_correct": "min_doc_type",
    "sum_check": "min_sum_check",
    "review_routing": "min_review_routing",
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", required=True)
    p.add_argument("--experiment-prefix", default="ci")
    p.add_argument("--min-money-exact", type=float, default=0.98)
    p.add_argument("--min-doc-type", type=float, default=0.95)
    p.add_argument("--min-sum-check", type=float, default=0.95)
    p.add_argument("--min-review-routing", type=float, default=0.90)
    args = p.parse_args()

    from evals.run_eval import run_eval_collect  # returns {metric: mean_score}

    scores = run_eval_collect(args.dataset, args.experiment_prefix)
    failures: list[str] = []
    for metric, arg_name in THRESHOLD_KEYS.items():
        threshold = getattr(args, arg_name)
        actual = scores.get(metric)
        if actual is None:
            logger.warning("Metric %s missing from results; skipping.", metric)
            continue
        status = "PASS" if actual >= threshold else "FAIL"
        logger.info("%-18s %.4f (min %.2f) %s", metric, actual, threshold, status)
        if actual < threshold:
            failures.append(f"{metric}={actual:.4f} < {threshold:.2f}")

    if failures:
        logger.error("ACCURACY GATE FAILED: %s", "; ".join(failures))
        return 1
    logger.info("Accuracy gate passed.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
