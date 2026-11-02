"""Optional real vector build using pinned BGE and the upstream Qdrant writer."""

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from adapters.embedding import BGEEncoder
from contracts.retrieval import VECTOR_MANIFEST
from offline.corpus_documents import CorpusDocuments
from offline.disk_documents import DiskDocuments
from offline.vector_build import QdrantSnapshotWriter,build_vectors


def _positive_int(value):
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError('must be a positive integer') from error
    if result <= 0:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return result


def _sha256(value):
    if len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise argparse.ArgumentTypeError('must be a lowercase SHA256 digest')
    return value


def _nonnegative_int(value):
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError('must be a nonnegative integer') from error
    if result < 0:
        raise argparse.ArgumentTypeError('must be a nonnegative integer')
    return result


def main(argv=None):
    p = argparse.ArgumentParser()
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--index')
    source.add_argument('--corpus')
    p.add_argument('--corpus-sha256', type=_sha256)
    p.add_argument('--corpus-n-docs', type=_positive_int)
    p.add_argument('--out', required=True)
    p.add_argument('--collection', required=True)
    p.add_argument('--url', default='http://127.0.0.1:16353')
    p.add_argument('--model-path', required=True)
    p.add_argument('--device', default='cpu')
    p.add_argument('--batch-size', type=_positive_int, default=64)
    p.add_argument('--hnsw-m', type=_nonnegative_int, default=16)
    p.add_argument('--hnsw-ef-construct', type=_positive_int, default=100)
    p.add_argument('--hnsw-full-scan-threshold-kb', type=_nonnegative_int)
    p.add_argument('--hnsw-max-indexing-threads', type=_nonnegative_int)
    p.add_argument('--hnsw-on-disk', action=argparse.BooleanOptionalAction, default=None)
    args = p.parse_args(argv)
    # Qdrant 1.16 REST HnswConfigDiff is stricter than its core config.
    if args.hnsw_ef_construct < 4:
        p.error('--hnsw-ef-construct must be >= 4 for Qdrant REST')
    if (args.hnsw_full_scan_threshold_kb is not None
            and args.hnsw_full_scan_threshold_kb < 10):
        p.error('--hnsw-full-scan-threshold-kb must be >= 10 for Qdrant REST')
    if args.corpus is not None:
        if args.corpus_sha256 is None or args.corpus_n_docs is None:
            p.error('--corpus requires --corpus-sha256 and --corpus-n-docs')
    elif args.corpus_sha256 is not None or args.corpus_n_docs is not None:
        p.error('--corpus-sha256 and --corpus-n-docs require --corpus')
    if not args.collection:
        p.error('--collection must not be empty')
    output = Path(args.out)
    if any(path.exists() and not path.is_dir() for path in (output, *output.parents)):
        p.error('--out and its existing parents must be directories')
    if (output / VECTOR_MANIFEST).exists():
        p.error('--out already contains a published vector manifest')
    index_params = {'m': args.hnsw_m, 'ef_construct': args.hnsw_ef_construct}
    for name, value in (
            ('full_scan_threshold', args.hnsw_full_scan_threshold_kb),
            ('max_indexing_threads', args.hnsw_max_indexing_threads),
            ('on_disk', args.hnsw_on_disk)):
        if value is not None:
            index_params[name] = value
    start = time.perf_counter()
    documents = (DiskDocuments(args.index) if args.index is not None else CorpusDocuments(
        args.corpus, expected_sha256=args.corpus_sha256,
        expected_n_docs=args.corpus_n_docs))
    with documents as reader:
        # Validate the input before model loading or GPU allocation. The reader
        # is closed even if encoder initialization or vector building fails.
        encoder=BGEEncoder(model_path=args.model_path,device=args.device,batch_size=args.batch_size)
        class ProgressEncoder:
            spec=encoder.spec;seen=0
            def encode_documents(self,texts):
                result=encoder.encode_documents(texts);self.seen+=len(texts)
                if self.seen%4096==0:print('encoded',self.seen,'/',reader.n_docs,flush=True)
                return result
        manifest=build_vectors(reader,ProgressEncoder(),QdrantSnapshotWriter(args.url,Path(args.out),timeout=180),
            Path(args.out),args.collection,batch_size=args.batch_size,index_params=index_params)
    print(json.dumps(dict(n_docs=manifest.n_docs,seconds=time.perf_counter()-start,collection=manifest.collection)),flush=True)


if __name__=='__main__':main()
