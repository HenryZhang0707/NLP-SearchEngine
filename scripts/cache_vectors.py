"""Optional offline producer/consumer CLI; all model files must already be local.

Two separately scheduled producers use --shard 0/1 and the same cache settings.
The consumer uses the unchanged public build_vectors/QdrantSnapshotWriter.
No runtime paths, endpoints, commands or exception messages are persisted or
printed by this entry point. Existing build_vectors.py remains unchanged.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.embedding import BGEEncoder, bge_spec
from contracts.index_format import DOCS, load_manifest
from offline.corpus_documents import CorpusDocuments
from offline.disk_documents import DiskDocuments
from offline.vector_build import QdrantSnapshotWriter, build_vectors
from offline.vector_cache import CacheConfig, CachedEncoder, produce_chunks


BGE_CACHE_CONTRACT = "bge-small-zh-v1.5-cls-fp32-right-l2-v1"


def bge_cache_contract():
    """Bind actual encoder/weight-verification code without storing any paths."""
    digest = hashlib.sha256()
    root = Path(__file__).resolve().parents[1]
    for name in ("embedding.py", "model_files.py"):
        digest.update(name.encode("ascii") + b"\0")
        digest.update((root / "adapters" / name).read_bytes())
    return BGE_CACHE_CONTRACT + "." + digest.hexdigest()


class _ArgumentError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error can echo untrusted values, including paths.
        raise _ArgumentError("invalid arguments")


def _integer(value):
    try:
        return int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("integer required") from error


def _number(value):
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("number required") from error
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("finite number required")
    return number


def _parser():
    parser = _Parser(prog="cache_vectors")
    parser.add_argument("mode", choices=("produce", "consume"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--corpus")
    source.add_argument("--index")
    parser.add_argument("--corpus-sha256")
    parser.add_argument("--corpus-n-docs", type=_integer)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--chunk-size", type=_integer, default=50000)
    parser.add_argument("--batch-size", type=_integer, default=16)
    parser.add_argument("--shards", type=_integer, default=2)
    parser.add_argument("--shard", type=_integer, default=0)
    parser.add_argument("--model-path")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--wait-timeout", type=_number, default=0.0)
    parser.add_argument("--poll-interval", type=_number, default=0.25)
    parser.add_argument("--out")
    parser.add_argument("--collection")
    parser.add_argument("--url", default="http://127.0.0.1:16353")
    parser.add_argument("--hnsw-m", type=_integer, default=16)
    parser.add_argument("--hnsw-ef-construct", type=_integer, default=100)
    parser.add_argument("--hnsw-full-scan-threshold-kb", type=_integer)
    parser.add_argument("--hnsw-max-indexing-threads", type=_integer)
    parser.add_argument("--hnsw-on-disk", action=argparse.BooleanOptionalAction, default=None)
    return parser


def _run(args):
    if args.corpus:
        if args.corpus_sha256 is None or args.corpus_n_docs is None:
            raise _ArgumentError("corpus identity required")
    elif args.corpus_sha256 is not None or args.corpus_n_docs is not None:
        raise _ArgumentError("corpus identity requires corpus")
    if (args.batch_size <= 0 or args.chunk_size <= 0
            or args.chunk_size % args.batch_size or args.shards <= 0
            or not 0 <= args.shard < args.shards
            or args.wait_timeout < 0 or args.poll_interval <= 0):
        raise _ArgumentError("invalid cache configuration")
    if args.mode == "produce" and not args.model_path:
        raise _ArgumentError("local model required")
    if args.mode == "consume" and (not args.out or not args.collection):
        raise _ArgumentError("build destination required")
    if (args.hnsw_m < 0 or args.hnsw_ef_construct < 4
            or (args.hnsw_full_scan_threshold_kb is not None
                and args.hnsw_full_scan_threshold_kb < 10)
            or (args.hnsw_max_indexing_threads is not None
                and args.hnsw_max_indexing_threads < 0)):
        raise _ArgumentError("invalid public writer configuration")
    source = (CorpusDocuments(args.corpus, expected_sha256=args.corpus_sha256,
                              expected_n_docs=args.corpus_n_docs)
              if args.corpus else DiskDocuments(args.index))
    with source as reader:
        # CorpusDocuments/DiskDocuments finish their checksum gates before any
        # encoder/model allocation. For an index, bind the verified raw DOCS
        # inventory hash; this is intentionally not the C1 semantic digest.
        digest = (args.corpus_sha256 if args.corpus else
                  load_manifest(Path(args.index)).files[DOCS])
        config = CacheConfig(
            digest, reader.n_docs, bge_spec(), bge_cache_contract(),
            source_kind="corpus-jsonl" if args.corpus else "index-documents-jsonl",
            chunk_size=args.chunk_size, batch_size=args.batch_size, shards=args.shards)
        if args.mode == "produce":
            # BGEEncoder verifies the existing pinned SHA inventory and uses
            # local_files_only=True. This CLI never invokes model download.
            encoder = BGEEncoder(spec=config.embedding, model_path=args.model_path,
                                 device=args.device, batch_size=args.batch_size)
            outcome = produce_chunks(reader, encoder, args.cache_dir, config,
                                     shard=args.shard, verified_source_sha256=digest)
            return {"status": "ok", "code": "chunks_ready", "n_docs": reader.n_docs,
                    "completed_count": len(outcome["completed"]),
                    "reused_count": len(outcome["reused"]),
                    "cache_sha256": config.identity}
        params = {"m": args.hnsw_m, "ef_construct": args.hnsw_ef_construct}
        for name, value in (("full_scan_threshold", args.hnsw_full_scan_threshold_kb),
                            ("max_indexing_threads", args.hnsw_max_indexing_threads),
                            ("on_disk", args.hnsw_on_disk)):
            if value is not None:
                params[name] = value
        with CachedEncoder(args.cache_dir, reader, config,
                           wait_timeout=args.wait_timeout,
                           poll_interval=args.poll_interval) as encoder:
            manifest = build_vectors(
                reader, encoder, QdrantSnapshotWriter(args.url, Path(args.out), timeout=180),
                Path(args.out), args.collection, batch_size=args.batch_size,
                index_params=params)
        return {"status": "ok", "code": "vectors_built", "n_docs": manifest.n_docs,
                "cache_sha256": config.identity,
                "corpus_sha256": manifest.corpus_sha256}


def main(argv=None):
    try:
        outcome = _run(_parser().parse_args(argv))
    except _ArgumentError:
        print(json.dumps({"status": "error", "code": "invalid_arguments"}), flush=True)
        return 2
    except Exception:
        # Do not disclose exception text, source/model paths or endpoint values.
        print(json.dumps({"status": "error", "code": "cache_operation_failed"}), flush=True)
        return 1
    print(json.dumps(outcome, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
