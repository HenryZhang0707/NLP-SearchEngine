"""Validate an external TREC run against a frozen evaluation dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Sequence

from .evaluate import (EvaluationError, _parse_run_row, _sha256, _unique_json_object,
                       evaluate_run, load_qrels, load_queries)
from .prepare_dataset import _record_id


_SCORE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def _preview(ids) -> str:
    return ", ".join(repr(value[:100]) for value in sorted(ids)[:5])


def _read_run(path: Path, qids: set[str]) -> tuple[dict, str | None, str]:
    digest = hashlib.sha256()
    ranks = {}
    doc_ids = {}
    tag = None
    with path.open("rb") as handle:
        for line_no, raw in enumerate(handle, 1):
            digest.update(raw)
            fields = raw.decode("utf-8").split()
            if not fields:
                continue
            if len(fields) != 6 or fields[1] != "Q0":
                raise EvaluationError(f"{path}:{line_no}: expected qid Q0 docid rank score tag")
            if not (fields[3].isascii() and fields[3].isdigit() and _SCORE.fullmatch(fields[4])):
                raise EvaluationError(f"{path}:{line_no}: invalid rank or score format")
            qid, doc_id, rank, score = _parse_run_row(fields, line_no, path)
            if qid not in qids:
                raise EvaluationError(f"{path}:{line_no}: unknown query ID {qid!r}")
            if tag is None:
                tag = fields[5]
            elif tag != fields[5]:
                raise EvaluationError(f"{path}:{line_no}: expected a single run tag")
            ranking = ranks.setdefault(qid, {})
            seen = doc_ids.setdefault(qid, set())
            if doc_id in seen:
                raise EvaluationError(f"{path}:{line_no}: duplicate document for query {qid!r}")
            if rank in ranking:
                raise EvaluationError(f"{path}:{line_no}: duplicate rank for query {qid!r}")
            ranking[rank] = (doc_id, score)
            seen.add(doc_id)
    result = {}
    for qid, ranking in ranks.items():
        if max(ranking) != len(ranking):
            raise EvaluationError(f"{path}: ranks must be contiguous from 1 for query {qid!r}")
        result[qid] = [ranking[rank] for rank in range(1, len(ranking) + 1)]
    return result, tag, digest.hexdigest()


def _check_corpus(path: Path, required: set[str]) -> tuple[int, str]:
    """Check requested IDs with memory proportional to the run and qrels."""
    digest = hashlib.sha256()
    found = set()
    n_docs = 0
    with path.open("rb") as handle:
        for line_no, raw in enumerate(handle, 1):
            digest.update(raw)
            if not raw.strip() or raw.lstrip().startswith(b"#"):
                continue
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeError) as error:
                raise EvaluationError(f"{path}:{line_no}: invalid corpus JSON") from error
            if not isinstance(record, dict):
                raise EvaluationError(f"{path}:{line_no}: expected a corpus object")
            raw_id = record.get("doc_id", record.get("id", record.get("_id")))
            doc_id = _record_id(raw_id, "doc_id", line_no, path)
            n_docs += 1
            if doc_id in required:
                if doc_id in found:
                    raise EvaluationError(f"{path}:{line_no}: duplicate referenced corpus ID {doc_id!r}")
                found.add(doc_id)
    if n_docs == 0:
        raise EvaluationError(f"{path}: empty corpus")
    missing = required - found
    if missing:
        raise EvaluationError(f"unknown corpus document IDs ({len(missing)}): {_preview(missing)}")
    return n_docs, digest.hexdigest()


def _load_exceptions(path: Path | None, hashes: dict, qids: set[str],
                     depth: int) -> tuple[dict, str | None]:
    if path is None:
        return {}, None
    raw = path.read_bytes()
    policy = json.loads(raw, object_pairs_hook=_unique_json_object)
    identities = {f"{key}_sha256": hashes[key] for key in ("corpus", "queries", "qrels")}
    expected = {"schema_version", "depth", "short_queries", *identities}
    if not isinstance(policy, dict) or policy.keys() != expected:
        raise EvaluationError(f"{path}: unexpected exceptions schema")
    if type(policy["schema_version"]) is not int or policy["schema_version"] != 1:
        raise EvaluationError(f"{path}: unsupported exceptions schema version")
    if type(policy["depth"]) is not int or policy["depth"] != depth:
        raise EvaluationError(f"{path}: exceptions depth does not match")
    for key, digest in identities.items():
        if policy[key] != digest:
            raise EvaluationError(f"{path}: exceptions {key} does not match")
    short = policy["short_queries"]
    if not isinstance(short, dict):
        raise EvaluationError(f"{path}: short_queries must be an object")
    for qid, entry in short.items():
        if qid not in qids:
            raise EvaluationError(f"{path}: unknown exceptions query ID {qid!r}")
        if not isinstance(entry, dict) or entry.keys() != {"expected_count", "reason"}:
            raise EvaluationError(f"{path}: invalid exception for query {qid!r}")
        count, reason = entry["expected_count"], entry["reason"]
        if type(count) is not int or not 0 <= count < depth:
            raise EvaluationError(f"{path}: invalid expected_count for query {qid!r}")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise EvaluationError(f"{path}: reason must contain 1–500 characters for query {qid!r}")
    return short, hashlib.sha256(raw).hexdigest()


def _check_depth(rankings: dict, qids: Sequence[str], short: dict, depth: int) -> None:
    invalid = []
    for qid in qids:
        count = len(rankings.get(qid, ()))
        if qid in short:
            if count != short[qid]["expected_count"]:
                invalid.append(f"{qid[:100]!r} (exception expected {short[qid]['expected_count']}, got {count})")
        elif count < depth:
            invalid.append(f"{qid[:100]!r} (required {depth}, got {count})")
    if invalid:
        raise EvaluationError(f"invalid ranking depth for {len(invalid)} queries: {', '.join(invalid[:5])}")


def validate_external_run(corpus: str | Path, queries: str | Path, qrels: str | Path,
                          run: str | Path, *, depth: int = 100, k: int = 10, map_k: int = 100,
                          exceptions: str | Path | None = None) -> dict:
    """Validate and score over all frozen queries, including unjudged queries.

    The corpus should be prepared and frozen before use. The membership scan
    checks uniqueness for referenced IDs and does not recertify the corpus.
    """
    if any(type(value) is not int or value <= 0 for value in (k, map_k, depth)):
        raise EvaluationError("depth and metric cutoffs must be positive integers")
    if max(k, map_k) > depth:
        raise EvaluationError("depth must cover both metric cutoffs")
    paths = {"corpus": Path(corpus), "queries": Path(queries), "qrels": Path(qrels), "run": Path(run)}
    hashes = {name: _sha256(paths[name]) for name in ("queries", "qrels")}
    ordered_qids = [record.qid for record in load_queries(paths["queries"])]
    qid_set = set(ordered_qids)
    if any(len(qid.split()) != 1 or qid.startswith("#") for qid in qid_set):
        raise EvaluationError("query IDs must be TREC tokens and may not start with '#'")
    judgments = load_qrels(paths["qrels"])
    if any(_sha256(paths[name]) != digest for name, digest in hashes.items()):
        raise EvaluationError("query or qrels input changed during validation")
    foreign = judgments.keys() - qid_set
    if foreign:
        raise EvaluationError(f"unknown qrels query IDs ({len(foreign)}): {_preview(foreign)}")
    rankings, tag, run_sha = _read_run(paths["run"], qid_set)
    required = {doc_id for docs in judgments.values() for doc_id in docs}
    required.update(doc_id for rows in rankings.values() for doc_id, _ in rows)
    n_docs, corpus_sha = _check_corpus(paths["corpus"], required)
    hashes.update(corpus=corpus_sha, run=run_sha)
    short, policy_sha = _load_exceptions(Path(exceptions) if exceptions is not None else None,
                                         hashes, qid_set, depth)
    _check_depth(rankings, ordered_qids, short, depth)

    selected_qrels = {qid: judgments.get(qid, {}) for qid in ordered_qids}
    metrics = evaluate_run(selected_qrels, rankings, k=k, map_k=map_k).to_dict(include_per_query=True)
    for name in ("ndcg", "recall", "mrr"):
        metrics[f"{name}_at_{k}"] = metrics.pop(f"{name}_at_10")
    metrics[f"map_at_{map_k}"] = metrics.pop("map_at_100")
    for query in metrics["per_query"]:
        query[f"map_at_{map_k}"] = query.pop("map_at_100")
    return {
        "schema_version": 1,
        "input_sha256": {**hashes, "exceptions": policy_sha},
        "run_tag": tag,
        "depth": depth,
        "cutoffs": {"k": k, "map_k": map_k},
        "counts": {
            "corpus_documents": n_docs,
            "queries": len(ordered_qids),
            "queries_with_qrels": len(judgments),
            "qrels": sum(len(docs) for docs in judgments.values()),
            "run_rows": sum(map(len, rankings.values())),
            "queries_with_run_rows": len(rankings),
            "short_queries": len(short),
        },
        "short_queries": short,
        "metrics": metrics,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="All input queries are scored in their original order. Queries without qrels "
               "contribute zero to the ranking metrics.")
    for name in ("corpus", "queries", "qrels", "run"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--depth", type=int, default=100, help="minimum run depth per query")
    parser.add_argument("--k", type=int, default=10, help="nDCG, recall and MRR cutoff")
    parser.add_argument("--map-k", type=int, default=100, help="MAP cutoff")
    parser.add_argument("--exceptions", type=Path, help="frozen SHA256-bound short-query policy")
    parser.add_argument("--output-json", type=Path, help="write receipt to a new file instead of stdout")
    args = parser.parse_args(argv)
    try:
        if args.output_json is not None and args.output_json.exists():
            raise EvaluationError(f"output path already exists: {args.output_json}")
        result = validate_external_run(args.corpus, args.queries, args.qrels, args.run,
                                       depth=args.depth, k=args.k, map_k=args.map_k,
                                       exceptions=args.exceptions)
        output = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        if args.output_json is None:
            print(output, end="")
        else:
            with args.output_json.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(output)
    except (OSError, ValueError, EvaluationError) as error:
        parser.exit(2, f"external run validation failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
