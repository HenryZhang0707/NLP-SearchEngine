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
  --expected-docs 100001 --expected-queries 2000
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
