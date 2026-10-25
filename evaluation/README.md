# HTTP evaluator

`evaluation/evaluate.py` evaluates the public `POST /search` API without importing
the search engine implementation. It reads TREC qrels, sends each query over
HTTP, optionally writes a TREC run, and reports nDCG, Recall, MRR, and latency.

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

Run the evaluator tests with:

```bash
python3 -m unittest evaluation.test_evaluate
```
