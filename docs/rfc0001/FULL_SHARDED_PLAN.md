# Full corpus sharded BM25 plan

The in-memory builder cannot stay below the container memory limit on the full corpus. The normalized corpus is split into 81 deterministic shards of at most 100,000 documents. Each shard retains the existing index contract (docs, postings, offsets, stats, manifest). The full build remains incomplete until all shards and their input identities are audited.

Run the resumable launcher from the repository root:

```bash
python -u -m evaluation.build_full_sharded_bm25 \
  --corpus-dir /path/to/corpus100k --out /path/to/index100k --workers 3
```

The launcher uses the current interpreter, a process lock, and at most three child builders. Before reusing a manifest it verifies every required file hash, analyzer identity, dataset name, and BM25 parameters. A corrupt completed shard fails closed. File hashes alone do not establish correspondence with source corpus records; that needs the final corpus/index identity audit.

## Global scoring invariant

Shard-local BM25 scores must **not** be merged directly. Every shard must score with the same full-corpus document count N, summed document frequency df(term), and document-weighted global average length. Only then are each shard's Top-k results sufficient to obtain exact global Top-k. Global document IDs use cumulative shard sizes and ties use ascending global document ID.

`evaluation/sharded_bm25.py` implements this scoring view and deterministic merge without materializing all shard readers simultaneously. The parity regression compares unequal shard sizes, unequal term distributions, repeated query terms, absent terms, and several cutoffs against a monolithic index. This proves the scoring primitive on the test corpus; it is not full-corpus retrieval or latency evidence. The remaining work includes building global statistics, binding full-corpus inputs, querying all shards, and independent end-to-end audit.

## Query-scoped global statistics

After every shard is built, prepare statistics with:

```bash
python -m evaluation.prepare_sharded_bm25_statistics \
  --corpus-dir /path/to/corpus100k --index-dir /path/to/index100k \
  --queries /path/to/queries.jsonl --output /path/to/global-statistics.json
```

The generator verifies every shard's required file hashes, computes average length from the integer document lengths, and sums query-term df across all shards. Its output binds query bytes, source shard bytes, manifest bytes and cumulative document offsets. It fails before publishing output if any declared shard is missing. It deliberately records `source_record_parity_verified=false`: the final audit must still compare actual source records to indexed documents. This output covers only the declared query terms, so serving arbitrary new query terms requires a full vocabulary statistics artifact or an equivalent exact lookup.

## Source identity audit

After build completion, run:

```bash
python -m evaluation.audit_sharded_bm25_sources \
  --corpus-dir /path/to/corpus100k --index-dir /path/to/index100k \
  --full-corpus /path/to/full-corpus.jsonl --expected-documents 8096668 \
  --output /path/to/source-parity-audit.json
```

This streams source and indexed documents in lockstep, checking all document IDs, titles, texts and contiguous internal IDs. It also hashes the raw concatenation of ordered source shards and compares it with the full normalized corpus hash. The report is published only when every shard is complete, required index hashes verify, the document counts agree, and source parity passes. It does not claim semantic conformance of every posting or correctness of the upstream normalization; those remain separate audit requirements.
