# HTTP evaluator

`evaluation/evaluate.py` evaluates the public `POST /search` API without importing
the search engine implementation. It reads TREC qrels, sends each query over
HTTP, optionally writes a TREC run, and reports nDCG@10, Recall@10, MRR@10,
MAP@100, and latency.

Query files accept `qid<TAB>query` lines or JSONL records such as
`{"qid":"q1","query":"heart attack"}`. Qrels use the usual TREC columns:
`qid iteration docid relevance`.

```bash
python3 -m evaluation.evaluate \
  --endpoint http://127.0.0.1:8000/search \
  --queries queries.tsv --qrels qrels.tsv \
  --output-run runs/bm25.trec \
  --include-per-query
```

### Retrieval depth and routing telemetry

HTTP requests fetch `max(--k, --map-k)` results (100 by default), while
nDCG/Recall/MRR and MAP apply their own cutoffs (10 and 100 by default).
The TREC run retains the full retrieved list, and per-query `retrieved` counts
that list rather than only the top-10 metric window. The public API permits
at most 200 results per request; non-lexical modes also require the server's
`candidate_k` to cover the requested depth. Latency measures this full-depth
HTTP request, including serialization and transfer, not a separate top-10 call.

API v2 routing fields are validated and retained with `--include-per-query`.
The aggregate `routing` object always reports query count, `vector_attempts`,
`vector_successes`, `fallbacks`, and their rates. Attempt and fallback rates use
all observed queries as the denominator; vector success rate uses attempts and
is `null` if no vector call was attempted. An adaptive run with missing vector
dependencies is therefore visibly degraded rather than silently labeled as a
successful hybrid experiment. Add `--fail-on-fallback` to reject any such query
with a nonzero exit status.

Standalone TREC runs contain no routing or latency evidence: those fields are
`null`, not zero failures or zero latency.

An existing run can be scored without a server:

```bash
python3 -m evaluation.evaluate --qrels qrels.tsv --run runs/bm25.trec
```

Before a full experiment, normalize and freeze an externally downloaded
DuRetrieval release without committing the data:

```bash
python3 -m evaluation.prepare_dataset \
  --corpus data/raw/corpus.jsonl --queries data/raw/queries.jsonl \
  --qrels data/raw/qrels.tsv --out data/duretrieval \
  --expected-docs 100001 --expected-queries 2000 --expected-qrels 9839
```

For the public RocketQA DuReader-Retrieval baseline archive, use the dedicated
streaming adapter instead of treating the archive as generic JSONL. It writes
`corpus.jsonl`, `queries.dev.jsonl`, `qrels.dev.tsv`, and a SHA256/count manifest;
the output remains an official `dev` split because the archive has no official
test labels. Queries use escaped JSONL rather than TSV so embedded newlines,
tabs, and Unicode line separators round-trip without creating extra query IDs.
Pass `queries.dev.jsonl` directly to the evaluator's `--queries` option.
`--archive-sha256` requires `--archive`; missing archives or mismatched hashes
are rejected before any output files or directory are created:

```bash
python3 -m evaluation.prepare_dureader \
  --root /data/dureader-retrieval-baseline-dataset \
  --out /data/prepared/duretrieval-official-dev \
  --archive /data/dureader-retrieval-baseline-dataset.tar.gz \
  --archive-sha256 <archive-sha256> \
  --expected-queries 2000 --expected-qrels 9839
```

Compare two runs with a deterministic query-level paired bootstrap. The
reported difference is `run-b - run-a`:

```bash
python3 -m evaluation.compare --qrels data/duretrieval/qrels.test.tsv \
  --run-a runs/hybrid.trec --run-b runs/adaptive.trec \
  --metric ndcg --samples 10000 --seed 2026
```

Run the evaluator tests with:

```bash
python3 -m unittest discover -s evaluation -p 'test_*.py'
```

## Durable collection of unlabelled web candidates

For a long crawl, use the persistent FIFO workflow rather than keeping the
frontier in process memory. It enforces same-host HTTP(S), fail-closed robots,
redirect validation before the redirected request, and pacing for every request
including robots, failed fetches and leaves. Site crawl-delay/request-rate may
increase the configured interval.

```bash
python -m evaluation.crawl_resume --state-dir /data/news-crawl \
  --seed https://www.news.cn/ --target-documents 20000 --max-depth 6 \
  --max-attempts 100000 --delay-seconds 0.5 --timeout 8 --max-bytes 1000000
```

Repeat exactly the same command with the same frozen code after interruption.
SQLite is authoritative; completed pages retain their IDs, interrupted attempts
remain charged, and an OS file lock rejects concurrent writers. The binding
includes seeds, settings, both crawler and cleaner source hashes, and robots
policy. A temporary robots fetch failure produces `INTERRUPTED` so the page can
be retried after connectivity returns; it is not treated as a permanent denial.
`corpus.jsonl` and `receipt.json` are atomic exported snapshots. If a hard stop
occurs between their replacements, re-export from SQLite on recovery rather
than treating the two files as a joint transaction.

The 20k target counts raw, URL-distinct candidate pages. It does not establish
20k cleaned, deduplicated, searchable documents. These candidates still require
the RFC-0002 cleaning/source review, frozen IDs, query design and pooled
judgments. This command creates no qrels or retrieval-quality claims.

## Cleaned article collection

For the new Xinhua article version, count accepted unique articles directly:

```bash
python -m evaluation.crawl_articles --state-dir /data/news-clean \
  --seed https://www.news.cn/ --target-documents 20000 --max-depth 6 \
  --max-attempts 100000 --delay-seconds 1 --timeout 8 --max-bytes 1000000
python -m unittest evaluation.test_clean_news evaluation.test_crawl_resume \
  evaluation.test_crawl_articles -v
```

The frozen `news-cn-body-v1` rule accepts article `/c.html` URLs on `www.news.cn`,
extracts `#detailContent` (fallback `#detail`), removes specified template/editor
nodes and requires at least 80 nonspace body characters, 40 CJK characters and
no more than 1% replacement characters. Navigation, filtered and duplicate pages
still discover links; they do not consume accepted document IDs. Exact dedup uses
NFKC/whitespace-normalized body SHA256, and near dedup requires SimHash64 Hamming
distance <=3 plus length ratio >=0.9. These rules never consult qrels.

Raw response bytes are stored as SHA-addressed deterministic gzip before decoding;
sidecars distinguish that SHA from decoded UTF-8 HTML and cleaned-body identities.
The corpus keeps only `doc_id/title/text`; `article_sidecar.jsonl` records source,
time, selector, filtering/duplicate reasons and capture identity. Reuse exactly
the same command and frozen code to resume. Optional `--seed-file` takes JSONL
URLs, binds its bytes SHA and treats imported seeds as depth 0 in a new version.
A raw crawl state cannot be silently upgraded to this policy.

SQLite remains authoritative. Corpus, sidecar and receipt are separate atomic
exports; readers must check both hashes/counts from a matching extended receipt
and refuse an incomplete/intermediate combination. Freeze only a completed,
reviewed export before indexing and qrel pooling. Public availability and robots
permission do not establish redistribution rights; full text/caches stay outside
Git. The collector creates no queries, labels or quality scores.
