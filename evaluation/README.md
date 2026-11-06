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

### Frozen unsupported-request policy and coverage

The default remains strict: any HTTP error aborts evaluation. For a separately
predeclared capability evaluation, `--unsupported-policy policy.json` allows
only the specific dense-mode rejection of explicit lexical constraints. It
still sends every query to the public `POST /search` endpoint, without changing
query text, parsing, routing, or API semantics. No other HTTP error is ignored.

The policy JSON has exactly these fields (digest placeholders below are a
schema example, not a runnable policy):

```json
{
  "schema_version": 1,
  "queries_sha256": "<SHA256 of original query file bytes>",
  "preflight_parser_sha256": "<parser SHA256 recorded before evaluation>",
  "request": {"mode": "dense", "scorer": "bm25", "expander": "none"},
  "expected_unsupported": {
    "query_ids": ["predeclared-query-id"],
    "status": 400,
    "body": {"error": "dense 模式无法保证显式词法约束，请使用 lexical、hybrid 或 adaptive"}
  }
}
```

Digests must be lowercase 64-character hex strings. Unknown or duplicate JSON
fields, duplicate/absent qids, changed query bytes or records, and mismatched
request parameters are rejected before creating outputs or sending requests.
The parser digest is **preflight provenance only**; this independent evaluator
does not import the parser or claim to verify the server's runtime parser.
The exact observed HTTP status and JSON error body form the behavioral check.
A predeclared qid returning HTTP200, another error, or malformed JSON aborts.

```bash
python3 -m evaluation.evaluate \
  --endpoint http://127.0.0.1:8000/search \
  --queries queries.jsonl --qrels qrels.tsv --mode dense \
  --unsupported-policy policy.json --output-run runs/dense.trec \
  --output-json runs/dense.json --include-per-query
```

Existing output paths are rejected, including sidecars and pending `.tmp`
files. Use fresh paths for each attempt. With `--output-run`, request evidence
defaults to `OUTPUT_RUN.requests.json`; `--output-requests` can choose another
path. A policy requires one of these durable request outputs. Alongside the
snapshot, `OUTPUT_REQUESTS.events.jsonl` is exclusively created as an append-only
journal. Its `START` line stores the ordered query universe and full policy once;
each request appends a sequenced `BEGIN` followed by its terminal `OUTCOME`,
including unexpected errors. Every event is written as one complete JSON line
and flushed. Per-step atomic `INCOMPLETE` snapshots contain only counters,
query-universe SHA256, compact policy identity, the last/inflight outcome, and
journal basename; they never rewrite the growing full history. Serialization
and logical write volume grow linearly with request count.

Only after all requests and metrics finish does the snapshot become `COMPLETE`
and include full query IDs/outcomes and the final journal SHA256. Unexpected
failures leave a partial run and an `INCOMPLETE` snapshot, and CLI exit status
is nonzero; there is no successful summary JSON. Recover history from the
complete journal lines: validate `START`'s universe digest, monotonically
increasing sequence numbers, attempt/qid matching, and each BEGIN/OUTCOME pair.
An unpaired BEGIN means an attempted request with unknown terminal outcome,
never a successful response or an empty ranking. A crash can leave a partial
final journal line; disregard only that trailing incomplete line and preserve
it as crash evidence. A malformed complete line, missing START, or inconsistent
sequence is an invalid receipt. Snapshot replacement can lag a flushed event,
so the journal is authoritative for history; it does not authorize automatic
resumption or treating an interrupted run as complete. Preserve pending `.tmp`
files and start a new attempt with fresh paths. Do not score partial TREC
artifacts as completed experiments. Existing journals are included in stale
output and path-collision checks.

An exactly matching HTTP400 is recorded as `EXPECTED_UNSUPPORTED`, not HTTP
success or fallback. Its ranking is empty, all four ranking metrics are zero,
and routing and success latency are null. The original query remains in the
full evaluation denominator. The `requests` summary and sidecar retain the
policy bytes SHA256, provenance, ordered qids/outcomes, attempted requests,
validated `http_200` responses, expected rejections, unexpected errors, and
coverage. `http_success_coverage` is validated HTTP200 / all input queries;
`protocol_accounted_coverage` is (validated HTTP200 + expected rejection) / all
input queries. The latter is not an HTTP success rate. Existing latency
aggregates use validated successful HTTP200 requests only; rejection latency
is retained separately in request outcomes. Routing rates use only responses
with validated routing, with their query count explicitly reported.

Empty rankings create no TREC rows. To reproduce full-query metrics from a
TREC file, preserve the sidecar's query universe and build selected qrels with
an entry (possibly empty) for every input qid. Standalone scoring otherwise
uses the union of qrels/run IDs and cannot distinguish missing requests from
empty rankings. A secondary common-support comparison must freeze its qid
subset before looking at relevance or outcomes, and filter **both runs and
qrels** to that same subset before using `evaluation.compare`. Report it
alongside full-query metrics and request coverage, not as a replacement for
the full-query result. Never select the subset from whichever requests happened
to succeed or from favorable scores.

An existing run can be scored without a server:

```bash
python3 -m evaluation.evaluate --qrels qrels.tsv --run runs/bm25.trec
```

For a bounded JSONL corpus that fits in memory, normalize and freeze an
external release without committing the data. Supply its actual expected counts:

```bash
python3 -m evaluation.prepare_dataset \
  --corpus data/raw/corpus.jsonl --queries data/raw/queries.jsonl \
  --qrels data/raw/qrels.tsv --out data/duretrieval
```

Ordinary queries retain TSV output. Queries containing tabs, newlines or Unicode
line separators automatically select escaped JSONL; explicit `--query-format tsv`
rejects these inputs. All output files use LF, including on Windows.

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

After corpus validation, split just the prepared queries and qrels without
loading or copying the 8M-document corpus. This uses the same SHA256 seed/query-ID
assignment as the normalizer and reproduces the six frozen internal-split files:

```bash
python3 -m evaluation.prepare_dataset --queries-only \
  --queries /data/prepared/duretrieval-official-dev/queries.dev.jsonl \
  --qrels /data/prepared/duretrieval-official-dev/qrels.dev.tsv \
  --out /data/prepared/official-internal-splits \
  --seed 20261028 --dev-fraction 0.5 --test-fraction 0.4 \
  --expected-queries 2000 --expected-qrels 9839
```

This entry validates query IDs, qrel query references, input counts and source
hashes, then publishes a fresh directory. It does not validate corpus membership;
its manifest explicitly records that boundary. Run the corpus adapter first.

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
