"""Audit the frozen full-corpus CE-33/40 comparison."""

import argparse
from hashlib import sha256
import json
import math
from pathlib import Path

import numpy as np

from evaluation.dureader_policy_features import FrozenLinear
from evaluation.evaluate import load_qrels, load_queries
from evaluation.prepare_bm25_router import metrics
from evaluation.router_metrics import group_bootstrap, percentile
from online.parser import parse


GUARD = "quality-guard-40"
FIXED = "fixed-pair-budget"
POLICIES = (GUARD, FIXED)
AUDIT_CONFIG = Path(__file__).resolve().parents[1] / "docs/rfc0001/full-quality-guard-http-v1.json"


def verify_bytes(path, expected):
    raw = Path(path).read_bytes()
    if sha256(raw).hexdigest() != expected:
        raise ValueError(f"Frozen input differs: {Path(path).name}")
    return raw


def request_pairs(rows, queries, gate, threshold, *, historical=False):
    """Return exact audit pairs, in frozen query order."""
    by_key = {}
    for row in rows:
        key = row["query_id"], row["policy"]
        if key[0] not in queries or key[1] not in POLICIES or key in by_key:
            raise ValueError("Unexpected or duplicate query/policy pair")
        expected_explicit = bool(parse(queries[key[0]]).get("explicit_constraints", False))
        fallback = row.get("fallback", False if historical else None)
        explicit = row.get("explicit_constraints", expected_explicit if historical else None)
        if row.get("unsupported") or fallback is not False:
            raise ValueError("Missing success evidence or fallback occurred")
        if explicit is not expected_explicit or row.get("vector_attempt") is not (not expected_explicit):
            raise ValueError("HTTP query constraints differ from the frozen input")
        if type(row["gate_heavy"]) is not bool:
            raise ValueError("Missing or invalid routing evidence")
        latency = row["client_wall_ms"]
        if type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0:
            raise ValueError("Invalid HTTP latency")
        ranking = row["results"]
        if (not isinstance(ranking, list) or not 33 <= len(ranking) <= 100
                or any(not isinstance(doc, str) or not doc for doc in ranking)
                or len(ranking) != len(set(ranking))):
            raise ValueError("Invalid ranking for the fixed-depth comparison")
        if key[1] == GUARD:
            features = row["gate_features"]
            if (not isinstance(features, list) or len(features) != 4
                    or any(type(value) not in (int, float) or not math.isfinite(value) for value in features)):
                raise ValueError("Invalid frozen gate features")
            heavy = bool(not expected_explicit and gate.predict([features])[0] > threshold)
        else:
            heavy = False
        depth = 40 if heavy else 33
        if row["gate_heavy"] is not heavy or type(row["ce_pairs"]) is not int or row["ce_pairs"] != depth or len(ranking) < depth:
            raise ValueError("Action or CE work differs from the declared comparison")
        by_key[key] = row
    if len(by_key) != len(queries) * len(POLICIES):
        raise ValueError("Incomplete paired HTTP capture")
    return {policy: [by_key[qid, policy] for qid in queries] for policy in POLICIES}


def audit(folder, qrels_path, models_path, queries_path, requests_sha256):
    folder = Path(folder)
    config = json.loads(AUDIT_CONFIG.read_bytes())
    original_run = requests_sha256 == config["http_requests_sha256"]
    requests = verify_bytes(folder / "requests.jsonl", requests_sha256)
    model = json.loads(verify_bytes(models_path, config["training_model_sha256"]))
    frozen = config["holdout_sha256"]
    verify_bytes(queries_path, frozen["queries"])
    verify_bytes(qrels_path, frozen["qrels"])
    queries = {row.qid: row.query for row in load_queries(queries_path)}
    qrels = load_qrels(qrels_path)
    verify_bytes(queries_path, frozen["queries"])
    verify_bytes(qrels_path, frozen["qrels"])
    if len(queries) != 808 or set(queries) != set(qrels):
        raise ValueError("Frozen holdout/query relevance coverage differs")
    runs = request_pairs((json.loads(line) for line in requests.splitlines() if line.strip()),
                         queries, FrozenLinear(model["gate"]["model"]), config["threshold"],
                         historical=original_run)
    result = {
        "queries": len(queries), "quality_guard_source": "historical" if original_run else "recapture",
        "heavy_count": sum(row["gate_heavy"] for row in runs[GUARD]),
        "ce_pairs_mean": {arm: float(np.mean([row["ce_pairs"] for row in runs[arm]])) for arm in POLICIES},
        "requests_sha256": requests_sha256, "inputs_sha256": frozen,
        "training_model_sha256": config["training_model_sha256"],
        "fallback_field_required": not original_run,
    }
    if result["ce_pairs_mean"][GUARD] > config["cost_gate"]["max_mean_ce_pairs"]:
        raise ValueError("Absolute CE cost gate failed")
    scored = {arm: [metrics(row["results"], qrels[qid]) for qid, row in zip(queries, rows, strict=True)]
              for arm, rows in runs.items()}
    query_ids = np.asarray(list(queries))
    for metric in ("ndcg", "recall", "mrr", "map"):
        delta = np.array([a[metric] - b[metric]
                          for a, b in zip(scored[GUARD], scored[FIXED], strict=True)])
        bootstrap = group_bootstrap(delta, query_ids)
        bootstrap["unit"] = "paired queries"
        result[metric] = {"delta": float(delta.mean()), "paired_query_ci95": bootstrap}
    result["quality_gate_passed"] = all(result[name]["paired_query_ci95"]["ci95"][0] > 0 for name in ("ndcg", "recall"))
    result["latency_ms"] = {
        arm: {label: percentile([row["client_wall_ms"] for row in rows], quantile)
              for label, quantile in (("p50", .5), ("p95", .95))}
        for arm, rows in runs.items()
    }
    with (folder / "audit-verified.json").open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
        output.write("\n")
    return result


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    for name in ("folder", "qrels", "models", "queries"):
        cli.add_argument("--" + name, type=Path, required=True)
    cli.add_argument("--requests-sha256", required=True, help="Digest recorded before scoring")
    args = cli.parse_args()
    result = audit(args.folder, args.qrels, args.models, args.queries, args.requests_sha256)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["quality_gate_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
