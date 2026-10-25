"""Compare two TREC runs with a paired, query-level bootstrap.

The command is intentionally offline and deterministic.  It reuses the same
metric implementation as :mod:`evaluation.evaluate`, samples the identical
query IDs for both runs, and reports the difference as ``run_b - run_a``.
It is suitable for the fixed-hybrid versus adaptive-hybrid comparison once
the runs have been produced through the public HTTP evaluator.

Example::

    python3 -m evaluation.compare --qrels data/test.qrels.tsv \
      --run-a runs/fixed.trec --run-b runs/adaptive.trec \
      --metric ndcg --samples 10000 --seed 2026
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .evaluate import EvaluationError, _metric_for_query, load_qrels, load_run


METRICS = ("ndcg", "recall", "mrr", "map")


@dataclass(frozen=True)
class BootstrapSummary:
    """Paired bootstrap result; all differences are ``run_b - run_a``."""

    metric: str
    n_queries: int
    samples: int
    seed: int
    observed_difference: float
    ci_lower: float
    ci_upper: float

    def to_dict(self) -> dict:
        return asdict(self)


def _docids(run: Mapping[str, Sequence[tuple[str, float] | str]], qid: str) -> list[str]:
    rows = run.get(qid, ())
    return [row[0] if isinstance(row, (tuple, list)) else row for row in rows]


def paired_differences(qrels: Mapping[str, Mapping[str, float]],
                       run_a: Mapping[str, Sequence[tuple[str, float] | str]],
                       run_b: Mapping[str, Sequence[tuple[str, float] | str]],
                       *, metric: str = "ndcg", k: int = 10,
                       map_k: int = 100) -> tuple[list[str], list[float]]:
    """Return query IDs and per-query ``run_b - run_a`` metric differences."""

    if metric not in METRICS:
        raise EvaluationError(f"不支持的比较指标: {metric!r}")
    if type(k) is not int or k <= 0 or type(map_k) is not int or map_k <= 0:
        raise EvaluationError("评测 cutoff 必须是正整数")
    qids = sorted(set(qrels) | set(run_a) | set(run_b))
    if not qids:
        raise EvaluationError("qrels 和 run 均为空")
    values: list[float] = []
    for qid in qids:
        left = _metric_for_query(qrels.get(qid, {}), _docids(run_a, qid), k, map_k)
        right = _metric_for_query(qrels.get(qid, {}), _docids(run_b, qid), k, map_k)
        index = {"ndcg": 0, "recall": 1, "mrr": 2, "map": 3}[metric]
        values.append(right[index] - left[index])
    return qids, values


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        raise EvaluationError("bootstrap 样本为空")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def paired_bootstrap(qrels: Mapping[str, Mapping[str, float]],
                     run_a: Mapping[str, Sequence[tuple[str, float] | str]],
                     run_b: Mapping[str, Sequence[tuple[str, float] | str]],
                     *, metric: str = "ndcg", k: int = 10,
                     map_k: int = 100, samples: int = 10_000,
                     seed: int = 2026, confidence: float = 0.95) -> BootstrapSummary:
    """Compute a reproducible percentile CI over paired query samples."""

    if type(samples) is not int or samples <= 0:
        raise EvaluationError("bootstrap samples 必须是正整数")
    if not math.isfinite(confidence) or not 0 < confidence < 1:
        raise EvaluationError("confidence 必须在 0 和 1 之间")
    if type(seed) is not int:
        raise EvaluationError("seed 必须是整数")
    qids, differences = paired_differences(qrels, run_a, run_b, metric=metric,
                                           k=k, map_k=map_k)
    observed = sum(differences) / len(differences)
    rng = random.Random(seed)
    bootstrap_means = []
    for _ in range(samples):
        total = sum(differences[rng.randrange(len(qids))] for _ in qids)
        bootstrap_means.append(total / len(qids))
    alpha = (1.0 - confidence) / 2.0
    return BootstrapSummary(metric, len(qids), samples, seed, observed,
                            _percentile(bootstrap_means, alpha),
                            _percentile(bootstrap_means, 1.0 - alpha))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="对两个 TREC run 做 paired bootstrap")
    parser.add_argument("--qrels", required=True, type=Path)
    parser.add_argument("--run-a", required=True, type=Path,
                        help="基线 run；差值定义为 run-b 减 run-a")
    parser.add_argument("--run-b", required=True, type=Path,
                        help="对照 run")
    parser.add_argument("--metric", choices=METRICS, default="ndcg")
    parser.add_argument("--k", type=int, default=10, help="nDCG/Recall/MRR cutoff")
    parser.add_argument("--map-k", type=int, default=100, help="MAP cutoff")
    parser.add_argument("--samples", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--confidence", type=float, default=0.95)
    parser.add_argument("--output-json", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = paired_bootstrap(
            load_qrels(args.qrels), load_run(args.run_a), load_run(args.run_b),
            metric=args.metric, k=args.k, map_k=args.map_k, samples=args.samples,
            seed=args.seed, confidence=args.confidence)
        output = json.dumps(summary.to_dict(), ensure_ascii=False, indent=2)
        if args.output_json:
            args.output_json.parent.mkdir(parents=True, exist_ok=True)
            args.output_json.write_text(output + "\n", encoding="utf-8")
        print(output)
    except (EvaluationError, OSError) as error:
        print(f"comparison error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
