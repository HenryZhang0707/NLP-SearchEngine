"""Validate frozen blind-review submissions and export TREC qrels."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path

from .annotation_packet import _SHA256, _json_bytes, _parse_candidate, _write_outputs
from .evaluate import EvaluationError, _unique_json_object


_PAIR_ID = re.compile(r"p-[0-9a-f]{24}")
_PLAN_FIELDS = {"schema_version", "packet_sha256", "private_map_sha256", "reviewers"}
_PACKET_FIELDS = {"pair_id", "query", "title", "text"}
_MAP_PAIR_FIELDS = {"query_id", "doc_id", "document_sha256", "query_sha256"}


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _check_sha256(raw: bytes, expected: object, name: str) -> str:
    if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
        raise EvaluationError(f"{name}: expected SHA256 must be 64 lowercase hex digits")
    actual = _sha256(raw)
    if actual != expected:
        raise EvaluationError(f"{name}: SHA256 mismatch")
    return actual


def _object(raw: bytes, source: str) -> dict:
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (ValueError, EvaluationError) as error:
        raise EvaluationError(f"{source}: invalid JSON: {error}") from error
    if not isinstance(result, dict):
        raise EvaluationError(f"{source}: expected a JSON object")
    return result


def _jsonl(raw: bytes, path: Path):
    for line_no, line in enumerate(raw.splitlines(), 1):
        if line.strip():
            yield line_no, _object(line, f"{path}:{line_no}")


def _valid_trec_id(value: str) -> bool:
    return not value.startswith("#") and not any(char.isspace() for char in value)


def _load_pairs(packet: Path, packet_raw: bytes, private_map: Path, map_raw: bytes,
                packet_sha256: str) -> dict[str, tuple[str, str]]:
    mapping = _object(map_raw, str(private_map))
    if type(mapping.get("schema_version")) is not int or mapping["schema_version"] != 1:
        raise EvaluationError(f"{private_map}: unsupported schema_version")
    if mapping.get("packet_sha256") != packet_sha256:
        raise EvaluationError(f"{private_map}: packet SHA256 mismatch")
    private_pairs = mapping.get("pairs")
    count = mapping.get("pair_count")
    if (not isinstance(private_pairs, dict) or not private_pairs
            or type(count) is not int or count != len(private_pairs)):
        raise EvaluationError(f"{private_map}: invalid pair_count or pairs")

    pairs = {}
    seen = set()
    for line_no, row in _jsonl(packet_raw, packet):
        source = f"{packet}:{line_no}"
        pair_id = row.get("pair_id")
        if set(row) != _PACKET_FIELDS or not isinstance(pair_id, str) or not _PAIR_ID.fullmatch(pair_id):
            raise EvaluationError(f"{source}: invalid blinded pair")
        if pair_id in pairs:
            raise EvaluationError(f"{source}: duplicate packet pair {pair_id}")
        private = private_pairs.get(pair_id)
        if not isinstance(private, dict) or set(private) != _MAP_PAIR_FIELDS:
            raise EvaluationError(f"{private_map}: invalid mapping for packet pair {pair_id}")
        # Use the same content binding as the packet exporter.
        candidate = _parse_candidate(_json_bytes({**row, **private}), packet, line_no)
        pair = (candidate.query_id, candidate.doc_id)
        if not all(_valid_trec_id(value) for value in pair):
            raise EvaluationError(f"{source}: invalid TREC query or document ID")
        if pair in seen:
            raise EvaluationError(f"{private_map}: duplicate original query/document pair for {pair_id}")
        pairs[pair_id] = pair
        seen.add(pair)
    _check_coverage(set(pairs), set(private_pairs), "packet/private map")
    return pairs


def _check_coverage(actual: set[str], expected: set[str], source: str) -> None:
    missing = expected - actual
    unexpected = actual - expected
    if missing or unexpected:
        raise EvaluationError(
            f"{source}: coverage mismatch, missing {len(missing)} "
            f"(first five: {sorted(missing)[:5]}), unexpected {len(unexpected)} "
            f"(first five: {sorted(unexpected)[:5]})"
        )


def _assignments(plan: dict, pairs: set[str]) -> tuple[dict[str, set[str]], int]:
    reviewers = plan["reviewers"]
    if not isinstance(reviewers, dict) or len(reviewers) < 2:
        raise EvaluationError("review plan: at least two distinct reviewer IDs are required")
    assigned = {}
    covered = set()
    overlapping = set()
    for reviewer_id, pair_ids in reviewers.items():
        if not reviewer_id.strip() or reviewer_id != reviewer_id.strip():
            raise EvaluationError("review plan: invalid reviewer ID")
        if not isinstance(pair_ids, list) or not pair_ids or any(
            not isinstance(pair_id, str) for pair_id in pair_ids
        ):
            raise EvaluationError(f"review plan: invalid assignments for {reviewer_id!r}")
        selection = set(pair_ids)
        if len(selection) != len(pair_ids):
            raise EvaluationError(f"review plan: duplicate assignment for {reviewer_id!r}")
        assigned[reviewer_id] = selection
        overlapping.update(covered & selection)
        covered.update(selection)
    _check_coverage(covered, pairs, "review plan")
    if not overlapping:
        raise EvaluationError("review plan: at least one pair must be assigned to multiple reviewers")
    return assigned, len(overlapping)


def _load_labels(path: Path, expected: set[str]) -> tuple[dict[str, int], dict]:
    raw = path.read_bytes()
    labels = {}
    for line_no, row in _jsonl(raw, path):
        source = f"{path}:{line_no}"
        pair_id = row.get("pair_id")
        relevance = row.get("relevance")
        if set(row) != {"pair_id", "relevance"} or not isinstance(pair_id, str):
            raise EvaluationError(f"{source}: expected pair_id and relevance")
        if type(relevance) is not int or relevance not in (0, 1, 2):
            raise EvaluationError(f"{source}: relevance must be the integer 0, 1, or 2")
        if pair_id in labels:
            raise EvaluationError(f"{source}: duplicate judgment for {pair_id}")
        labels[pair_id] = relevance
    _check_coverage(set(labels), expected, str(path))
    return labels, {"filename": path.name, "sha256": _sha256(raw), "pair_count": len(labels)}


def import_human_judgments(
    packet: Path, private_map: Path, plan: Path, plan_sha256: str,
    reviewer_files: Sequence[tuple[str, Path]], qrels: Path, receipt: Path,
    *, adjudication: Path | None = None,
) -> dict:
    """Export fully covered and adjudicated judgments to two fresh files."""
    if qrels.resolve() == receipt.resolve():
        raise EvaluationError("qrels and receipt must have distinct paths")
    for output in (qrels, receipt):
        if output.exists() or output.is_symlink():
            raise EvaluationError(f"output path already exists: {output}")

    plan_raw = plan.read_bytes()
    plan_digest = _check_sha256(plan_raw, plan_sha256, str(plan))
    review_plan = _object(plan_raw, str(plan))
    if (set(review_plan) != _PLAN_FIELDS or type(review_plan["schema_version"]) is not int
            or review_plan["schema_version"] != 1):
        raise EvaluationError(f"{plan}: invalid review plan v1")
    packet_raw = packet.read_bytes()
    map_raw = private_map.read_bytes()
    packet_digest = _check_sha256(packet_raw, review_plan["packet_sha256"], str(packet))
    map_digest = _check_sha256(map_raw, review_plan["private_map_sha256"], str(private_map))
    pairs = _load_pairs(packet, packet_raw, private_map, map_raw, packet_digest)
    assigned, overlap_count = _assignments(review_plan, set(pairs))

    submissions = dict(reviewer_files)
    if len(submissions) != len(reviewer_files):
        raise EvaluationError("duplicate reviewer input")
    _check_coverage(set(submissions), set(assigned), "reviewer inputs")
    reviewed = {pair_id: set() for pair_id in pairs}
    reviewer_receipts = {}
    seen_files = set()
    for reviewer_id, selection in sorted(assigned.items()):
        path = submissions[reviewer_id]
        file_stat = path.stat()
        identity = (file_stat.st_dev, file_stat.st_ino)
        if identity in seen_files:
            raise EvaluationError("reviewers must submit separate judgment files")
        seen_files.add(identity)
        labels, input_receipt = _load_labels(path, selection)
        reviewer_receipts[reviewer_id] = input_receipt
        for pair_id, grade in labels.items():
            reviewed[pair_id].add(grade)

    disputes = {pair_id for pair_id, grades in reviewed.items() if len(grades) > 1}
    if disputes and adjudication is None:
        raise EvaluationError(f"adjudication is required for {len(disputes)} disputed pairs")
    resolved, adjudication_receipt = {}, None
    if adjudication is not None:
        file_stat = adjudication.stat()
        if (file_stat.st_dev, file_stat.st_ino) in seen_files:
            raise EvaluationError("adjudication must be a separate judgment file")
        resolved, adjudication_receipt = _load_labels(adjudication, disputes)
    final = {pair_id: resolved[pair_id] if pair_id in disputes else next(iter(grades))
             for pair_id, grades in reviewed.items()}

    rows = sorted((*pairs[pair_id], grade) for pair_id, grade in final.items())
    qrels_raw = "".join(f"{qid}\t0\t{doc_id}\t{grade}\n" for qid, doc_id, grade in rows).encode("utf-8")
    result = {
        "schema_version": 1,
        "label_source": "reviewer_submissions",
        "plan": {"filename": plan.name, "sha256": plan_digest},
        "packet": {"filename": packet.name, "sha256": packet_digest},
        "private_map": {"filename": private_map.name, "sha256": map_digest},
        "reviewers": reviewer_receipts,
        "adjudication": adjudication_receipt,
        "pair_count": len(pairs),
        "overlap_pair_count": overlap_count,
        "disputed_pair_count": len(disputes),
        "qrels": {"filename": qrels.name, "sha256": _sha256(qrels_raw)},
    }
    _write_outputs(qrels, receipt, qrels_raw, _json_bytes(result, indent=2))
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Freeze the review plan and record its SHA256 before collecting labels.
Plan JSON: {"schema_version": 1, "packet_sha256": "...", "private_map_sha256": "...",
            "reviewers": {"reviewer-a": ["p-..."], "reviewer-b": ["p-..."]}}
Assignments must cover every packet pair and have at least one overlapping pair.
Collect each reviewer's grades independently. Review and adjudication JSONL rows
each contain exactly {"pair_id": "p-...", "relevance": 0}, with grades 0, 1, or 2.
The adjudicator must grade exactly the disputed pairs. Keep all original inputs
and the receipt private so the exported qrels can be traced to their submissions.""",
    )
    parser.add_argument("--packet", required=True, type=Path)
    parser.add_argument("--private-map", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--plan-sha256", required=True, help="previously recorded review plan SHA256")
    parser.add_argument("--reviewer", action="append", nargs=2, metavar=("REVIEWER_ID", "JSONL"),
                        required=True, help="repeat for each reviewer in the frozen plan")
    parser.add_argument("--adjudication", type=Path, help="explicit adjudication JSONL for disputed pairs")
    parser.add_argument("--qrels", required=True, type=Path, help="fresh TREC qrels path")
    parser.add_argument("--receipt", required=True, type=Path, help="fresh private provenance JSON path")
    args = parser.parse_args(argv)
    try:
        result = import_human_judgments(
            args.packet, args.private_map, args.plan, args.plan_sha256,
            [(reviewer_id, Path(path)) for reviewer_id, path in args.reviewer],
            args.qrels, args.receipt, adjudication=args.adjudication,
        )
    except (OSError, UnicodeError, EvaluationError) as error:
        parser.exit(2, f"annotation intake failed: {error}\n")
    print(json.dumps({key: result[key] for key in ("pair_count", "overlap_pair_count", "disputed_pair_count")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
