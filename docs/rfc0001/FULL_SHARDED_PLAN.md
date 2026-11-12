# Full corpus sharded BM25 plan

The single-directory builder cannot stay below the 30 GiB cgroup memory limit for the 8,096,668-document corpus. The full normalized corpus is therefore split into 81 deterministic 100,000-document JSONL shards (the last shard is smaller). Each shard is built with the existing repository `offline.build` contract, producing its own docs, postings, offsets, stats and manifest. A future full-corpus reader will query every shard and merge the shard Top-k lists by the existing deterministic `(score, docid)` order.

`evaluation/build_full_sharded_bm25.py` builds the shards sequentially and skips only a shard with an already verified manifest. The first shard has completed successfully in about 151 seconds with peak RSS about 4.4 GiB; the full run is intentionally long-running and its output remains outside the PR until all shard manifests are audited.
