"""Export a blinded human-review packet from frozen candidate JSONL files."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .evaluate import EvaluationError, _unique_json_object


_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Candidate:
    query_id: str
    doc_id: str
    query: str
    title: str
    text: str
    document_sha256: str
    query_sha256: str


def _json_bytes(value, *, indent: int | None = None) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=indent) + "\n").encode("utf-8")


def _parse_candidate(raw: bytes, path: Path, line_no: int) -> Candidate:
    try:
        row = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (ValueError, EvaluationError) as error:
        raise EvaluationError(f"{path}:{line_no}: invalid candidate JSON: {error}") from error
    if not isinstance(row, dict):
        raise EvaluationError(f"{path}:{line_no}: candidate must be a JSON object")
    fields = {}
    for name in Candidate.__annotations__:
        value = row.get(name)
        if not isinstance(value, str) or (name != "title" and not value.strip()):
            raise EvaluationError(f"{path}:{line_no}: invalid candidate {name}")
        fields[name] = value
    for name in ("document_sha256", "query_sha256"):
        if not _SHA256.fullmatch(fields[name]):
            raise EvaluationError(f"{path}:{line_no}: {name} must be a lowercase SHA256")
    candidate = Candidate(**fields)
    doc_content = json.dumps([candidate.doc_id, candidate.title, candidate.text],
                             ensure_ascii=False, separators=(",", ":"))
    if hashlib.sha256(doc_content.encode("utf-8")).hexdigest() != candidate.document_sha256:
        raise EvaluationError(f"{path}:{line_no}: document hash does not match content")
    if hashlib.sha256(candidate.query.encode("utf-8")).hexdigest() != candidate.query_sha256:
        raise EvaluationError(f"{path}:{line_no}: query hash does not match content")
    return candidate


def _read_candidates(paths: Sequence[Path], max_pairs: int) -> tuple[dict, list[dict]]:
    pairs = {}
    queries = {}
    documents = {}
    sources = []
    for path in paths:
        digest = hashlib.sha256()
        count = 0
        with path.open("rb") as handle:
            for line_no, raw in enumerate(handle, 1):
                digest.update(raw)
                if not raw.strip():
                    continue
                if len(pairs) >= max_pairs:
                    raise EvaluationError(f"{path}:{line_no}: exceeded max_pairs={max_pairs}")
                row = _parse_candidate(raw, path, line_no)
                pair = (row.query_id, row.doc_id)
                if pair in pairs:
                    raise EvaluationError(f"{path}:{line_no}: duplicate candidate pair {pair!r}")
                query_identity = (row.query, row.query_sha256)
                if queries.setdefault(row.query_id, query_identity) != query_identity:
                    raise EvaluationError(f"{path}:{line_no}: inconsistent query identity for {row.query_id!r}")
                doc_identity = (row.title, row.text, row.document_sha256)
                if documents.setdefault(row.doc_id, doc_identity) != doc_identity:
                    raise EvaluationError(f"{path}:{line_no}: inconsistent document identity for {row.doc_id!r}")
                pairs[pair] = row
                count += 1
        if count == 0:
            raise EvaluationError(f"{path}: candidate file is empty")
        sources.append({"filename": path.name, "sha256": digest.hexdigest(), "pairs": count})
    return pairs, sources


def _write_outputs(packet: Path, private_map: Path, public: bytearray, private: bytes) -> None:
    created = []
    try:
        # Create the mapping first so a packet is published only after its mapping exists.
        for path, content in ((private_map, private), (packet, public)):
            with path.open("xb") as handle:
                created.append(path)
                handle.write(content)
    except OSError:
        for path in reversed(created):
            with suppress(OSError):
                path.unlink()
        raise


def export_annotation_packet(candidates: Sequence[str | Path], packet: str | Path,
                             private_map: str | Path, *, seed: int = 20261028,
                             max_pairs: int = 20000) -> dict:
    """Write a blinded packet and a private mapping to two fresh files."""
    if not candidates or type(seed) is not int or type(max_pairs) is not int or max_pairs <= 0:
        raise EvaluationError("candidate files, an integer seed and a positive max_pairs are required")
    packet, private_map = Path(packet), Path(private_map)
    if packet.resolve() == private_map.resolve():
        raise EvaluationError("packet and private map must have distinct paths")
    for path in (packet, private_map):
        if path.exists() or path.is_symlink():
            raise EvaluationError(f"output path already exists: {path}")

    pairs, sources = _read_candidates([Path(path) for path in candidates], max_pairs)
    key = hashlib.sha256(_json_bytes([seed, [source["sha256"] for source in sources]])).digest()
    shuffled = [(hmac.digest(key, _json_bytes(pair), "sha256").hex(), row)
                for pair, row in pairs.items()]
    shuffled.sort(key=lambda item: item[0])
    public = bytearray()
    mapping = {}
    for digest, row in shuffled:
        pair_id = f"p-{digest[:24]}"
        if pair_id in mapping:
            raise EvaluationError("opaque pair ID collision")
        public.extend(_json_bytes({"pair_id": pair_id, "query": row.query,
                                   "title": row.title, "text": row.text}))
        mapping[pair_id] = {"query_id": row.query_id, "doc_id": row.doc_id,
                            "document_sha256": row.document_sha256, "query_sha256": row.query_sha256}
    receipt = {"schema_version": 1, "seed": seed, "inputs": sources,
               "packet_sha256": hashlib.sha256(public).hexdigest(), "pair_count": len(mapping)}
    _write_outputs(packet, private_map, public, _json_bytes({**receipt, "pairs": mapping}, indent=2))
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Send only the packet to reviewers. Keep the mapping and candidate files private.")
    parser.add_argument("--candidates", action="append", required=True, type=Path,
                        help="frozen candidate JSONL file, repeat in frozen input order")
    parser.add_argument("--packet", required=True, type=Path, help="fresh reviewer JSONL path")
    parser.add_argument("--private-map", required=True, type=Path, help="fresh private JSON path")
    parser.add_argument("--seed", default=20261028, type=int, help="deterministic shuffle seed")
    parser.add_argument("--max-pairs", default=20000, type=int, help="maximum total candidate pairs")
    args = parser.parse_args(argv)
    try:
        receipt = export_annotation_packet(args.candidates, args.packet, args.private_map,
                                           seed=args.seed, max_pairs=args.max_pairs)
    except (OSError, UnicodeError, EvaluationError) as error:
        parser.exit(2, f"annotation packet export failed: {error}\n")
    print(json.dumps({"pair_count": receipt["pair_count"], "packet_sha256": receipt["packet_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
