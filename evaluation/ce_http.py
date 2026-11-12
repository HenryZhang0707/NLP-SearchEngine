"""Capture paired fixed RRF and cross-encoder controls through public HTTP."""

import argparse
from contextlib import closing
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import random
import sqlite3
import statistics
import tempfile
import time
import unicodedata
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from .compare import paired_bootstrap
from .evaluate import (
    EvaluationError, _decode_search_response, _percentile, evaluate_run,
    load_qrels, load_queries,
)


REQUEST = {"k": 100, "mode": "hybrid", "scorer": "bm25", "expander": "none"}
SEED = 2026
BOOTSTRAP_SAMPLES = 10000
STAGES = ("lexical", "router", "encode", "vector", "fusion")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def endpoint(url):
    parts = urlsplit(url)
    host = parts.hostname
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if (parts.scheme != "http" or not local or parts.port is None or not parts.port
            or parts.username is not None or parts.query or parts.fragment
            or parts.path.rstrip("/") not in ("", "/search")):
        raise EvaluationError("use an explicit loopback HTTP URL with a port")
    return f"http://{parts.netloc}"


def http_json(client, base, path, body=None):
    payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(base + path, data=payload, headers={
        "Content-Type": "application/json", "Accept": "application/json",
    })
    started = time.perf_counter()
    with client.open(request, timeout=120) as response:
        if response.status != 200:
            raise EvaluationError(f"{path} returned HTTP {response.status}")
        raw = response.read()
    elapsed = (time.perf_counter() - started) * 1000
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise EvaluationError(f"{path} response must be a JSON object")
    return value, raw, elapsed


def check_services(client, arms):
    common = None
    model = None
    identities = {}
    for name, (depth, base) in arms.items():
        info, _, _ = http_json(client, base, "/index/info")
        retrieval = info.get("retrieval")
        if not isinstance(retrieval, dict) or retrieval.get("vector_ready") is not True:
            raise EvaluationError(f"{name} requires a ready vector index")
        for key in ("index_identity", "vector_identity", "search_config", "policy", "serving"):
            if not isinstance(retrieval.get(key), dict) or not retrieval[key]:
                raise EvaluationError(f"{name} missing retrieval.{key} identity")
        candidate_k = retrieval["search_config"].get("candidate_k")
        if type(candidate_k) is not int or candidate_k < REQUEST["k"]:
            raise EvaluationError(f"{name} candidate pool cannot support the fixed request")
        stable = {**info, "retrieval": {
            key: value for key, value in retrieval.items() if key != "reranker"
        }}
        stable["retrieval"]["serving"] = {
            key: value for key, value in retrieval["serving"].items() if key != "port"
        }
        if common is None:
            common = stable
        if stable != common:
            raise EvaluationError(f"{name} service identity differs from baseline")
        reranker = retrieval.get("reranker")
        if depth:
            if (not isinstance(reranker, dict) or type(reranker.get("depth")) is not int
                    or reranker["depth"] != depth or reranker.get("candidate_source") != "hybrid_rrf"
                    or not isinstance(reranker.get("model"), dict)
                    or not all(reranker["model"].get(key) for key in
                               ("model_id", "revision", "weights_sha256", "inventory_sha256"))):
                raise EvaluationError(f"{name} has an invalid fixed CE identity")
            if model is None:
                model = reranker["model"]
            if reranker["model"] != model:
                raise EvaluationError(f"{name} CE model identity differs across arms")
        elif reranker is not None:
            raise EvaluationError("RRF baseline has an enabled reranker")
        identities[name] = reranker
    return {"common": common, "rerankers": identities}


def search(client, base, depth, record, identity):
    response, raw, latency = http_json(client, base, "/search", {
        **REQUEST, "query": record.query,
    })
    ranking, routing = _decode_search_response(raw, "hybrid")
    items = response["results"]
    route_info = response["routing"]
    parsed = response.get("parsed")
    explicit = parsed.get("explicit_constraints") if isinstance(parsed, dict) else None
    if (routing.fallback or route_info.get("score_kind") != "rrf"
            or route_info.get("candidate_k") != identity["search_config"]["candidate_k"]
            or route_info.get("policy_version") != identity["policy"]["policy_version"]
            or response.get("expanded") != []
            or type(explicit) is not bool
            or not len(ranking) == len(items) <= REQUEST["k"]
            or any(type(item.get("rank")) is not int or item["rank"] != rank
                   or type(item.get("score")) not in (int, float)
                   or not item["doc_id"] or any(char.isspace() for char in item["doc_id"])
                   for rank, item in enumerate(items, 1))):
        raise EvaluationError("invalid fixed hybrid response")
    timings = response.get("timings_ms")
    expect_rerank = depth and not explicit and bool(ranking)
    expected_stages = {*STAGES, "rerank"} if expect_rerank else set(STAGES)
    if (not isinstance(timings, dict) or timings.keys() != expected_stages
            or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                   for value in timings.values())):
        raise EvaluationError("invalid fixed hybrid stage timings")
    rerank = response.get("rerank")
    scored = 0
    if expect_rerank:
        if (not isinstance(rerank, dict) or type(rerank.get("depth")) is not int
                or rerank["depth"] != depth or type(rerank.get("scored")) is not int
                or rerank["scored"] != min(depth, len(ranking))):
            raise EvaluationError("CE response has missing or inconsistent pair counts")
        scored = rerank["scored"]
    elif "rerank" in response:
        raise EvaluationError("response reranked candidates where CE must be bypassed")
    return {"ranking": ranking, "http_ms": latency, "timings_ms": timings, "ce_pairs": scored,
            "explicit_constraints": explicit}


def capture_query(client, arms, record, index, order, identity):
    shift = index % len(order)
    observations = {}
    for name in order[shift:] + order[:shift]:
        depth, base = arms[name]
        try:
            observations[name] = search(client, base, depth, record, identity)
        except (OSError, ValueError, TypeError) as error:
            raise EvaluationError(f"qid={record.qid!r}, arm={name}: {error}") from error
    baseline = observations["rrf"]
    for name, result in observations.items():
        if result["explicit_constraints"] != baseline["explicit_constraints"]:
            raise EvaluationError(f"qid={record.qid!r}, arm={name}: query parsing differs from baseline")
        prefix = result["ce_pairs"]
        ranking = result["ranking"]
        original = baseline["ranking"]
        if ranking[prefix:] != original[prefix:] or set(ranking[:prefix]) != set(original[:prefix]):
            raise EvaluationError(f"qid={record.qid!r}, arm={name}: changed the fixed RRF candidate pool")
    return observations


def journal(db, protocol):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS protocol (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS queries (
            qid TEXT PRIMARY KEY, epoch TEXT NOT NULL, arms TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invalid_epochs (epoch TEXT PRIMARY KEY, reason TEXT NOT NULL);
    """)
    body = json.dumps(protocol, sort_keys=True, ensure_ascii=False, allow_nan=False)
    with db:
        db.execute("INSERT OR IGNORE INTO protocol VALUES (1, ?)", (body,))
        if db.execute("SELECT body FROM protocol WHERE id=1").fetchone()[0] != body:
            raise EvaluationError("output journal belongs to a different capture protocol")
        if db.execute("SELECT 1 FROM invalid_epochs LIMIT 1").fetchone():
            raise EvaluationError("capture journal contains an invalid service epoch")
    return {qid: (epoch, json.loads(arms)) for qid, epoch, arms in
            db.execute("SELECT qid, epoch, arms FROM queries ORDER BY rowid")}


def completed_identity(db, expected_count):
    """Return the saved identity when the verified final query is committed."""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"protocol", "queries", "invalid_epochs"} <= tables:
        return None
    if db.execute("SELECT COUNT(*) FROM queries").fetchone()[0] != expected_count:
        return None
    row = db.execute("SELECT body FROM protocol WHERE id=1").fetchone()
    return json.loads(row[0])["identities"] if row is not None else None


def distribution(values):
    return {"mean": statistics.fmean(values), "p50": _percentile(values, .50),
            "p95": _percentile(values, .95)}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized_query(query):
    return " ".join(unicodedata.normalize("NFKC", query).casefold().split())


def publish(path, contents):
    """Publish complete bytes once; on resume require identical output."""
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        pending = Path(stream.name)
        try:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            pending.unlink()
            raise
    try:
        try:
            os.link(pending, path)
        except FileExistsError:
            if path.read_bytes() != contents:
                raise EvaluationError(f"existing output differs from completed capture: {path}")
    finally:
        pending.unlink()


def summarize(out, protocol, names, qrels, captured):
    runs = {}
    arms = {}
    for name in names:
        run = {qid: data[name]["ranking"] for qid, (_, data) in captured.items()}
        runs[name] = run
        lines = (f"{qid} Q0 {doc_id} {rank} {score:.8f} {name}\n"
                 for qid, ranking in run.items()
                 for rank, (doc_id, score) in enumerate(ranking, 1))
        publish(out / f"{name}.trec", "".join(lines).encode("utf-8"))
        epochs = {}
        ce_pairs = 0
        for epoch, data in captured.values():
            epochs.setdefault(epoch, []).append((data[name], data["rrf"]))
            ce_pairs += data[name]["ce_pairs"]
        latency = {}
        for epoch, pairs in epochs.items():
            rows = [row for row, _ in pairs]
            latency[epoch] = {
                "n_queries": len(rows),
                "covers_all_queries": len(rows) == len(captured),
                "http_ms": distribution([row["http_ms"] for row in rows]),
                "paired_mean_http_delta_vs_rrf_ms": statistics.fmean(
                    row["http_ms"] - baseline["http_ms"] for row, baseline in pairs),
                "timings_ms": {
                    stage: distribution([row["timings_ms"][stage] for row in rows
                                         if stage in row["timings_ms"]])
                    for stage in (*STAGES, "rerank")
                    if any(stage in row["timings_ms"] for row in rows)
                },
            }
        arms[name] = {
            "quality": evaluate_run(qrels, run).to_dict(),
            "ce_pairs": ce_pairs,
            "mean_ce_pairs": ce_pairs / len(captured),
            "latency_by_epoch": latency,
        }
    comparisons = {
        name: {
            metric: paired_bootstrap(qrels, runs["rrf"], runs[name], metric=metric,
                                     samples=BOOTSTRAP_SAMPLES, seed=SEED).to_dict()
            for metric in ("ndcg", "recall", "mrr", "map")
        }
        for name in names if name != "rrf"
    }
    summary = {
        "protocol": protocol, "arms": arms, "paired_vs_rrf": comparisons,
        "explicit_constraints_queries": sum(
            data["rrf"]["explicit_constraints"] for _, data in captured.values()),
    }
    publish(out / "summary.json", (json.dumps(summary, ensure_ascii=False, indent=2,
                                             sort_keys=True) + "\n").encode("utf-8"))


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--baseline", required=True)
    cli.add_argument("--rerank", action="append", nargs=2, metavar=("DEPTH", "URL"), required=True)
    for name in ("queries", "warmup-queries", "qrels", "out"):
        cli.add_argument(f"--{name}", type=Path, required=True)
    args = cli.parse_args(argv)
    arms = {"rrf": (0, endpoint(args.baseline))}
    for depth, url in sorted(args.rerank):
        if depth not in ("28", "40") or f"ce{depth}" in arms:
            cli.error("--rerank requires each of depth 28 and 40 at most once")
        arms[f"ce{depth}"] = (int(depth), endpoint(url))
    if len({base for _, base in arms.values()}) != len(arms):
        cli.error("each arm requires its own endpoint")

    inputs = {"queries": args.queries, "warmup_queries": args.warmup_queries, "qrels": args.qrels}
    fingerprints = {name: sha256(path) for name, path in inputs.items()}
    records = load_queries(args.queries)
    warmup = load_queries(args.warmup_queries)
    if any(sha256(inputs[name]) != fingerprints[name] for name in ("queries", "warmup_queries")):
        raise EvaluationError("query inputs changed while loading")
    query_ids = [record.qid for record in records]
    if any(any(char.isspace() for char in qid) for qid in query_ids):
        raise EvaluationError("query IDs must be compatible with TREC runs")
    if set(query_ids).intersection(record.qid for record in warmup) or (
        {normalized_query(record.query) for record in records}
        & {normalized_query(record.query) for record in warmup}
    ):
        raise EvaluationError("warmup and evaluated queries must be disjoint by ID and normalized text")

    client = build_opener(ProxyHandler({}), NoRedirect())
    order = list(arms)
    random.Random(SEED).shuffle(order)
    protocol = {
        "version": 3, "request": REQUEST, "seed": SEED, "base_arm_order": order,
        "bootstrap_samples": BOOTSTRAP_SAMPLES,
        "depths": {name: depth for name, (depth, _) in arms.items()},
        "inputs_sha256": fingerprints,
        "query_count": len(records), "warmup_query_count": len(warmup),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(args.out / "capture.sqlite3")) as db:
        identities = completed_identity(db, len(records))
        if identities is None:
            identities = check_services(client, arms)
        protocol["identities"] = identities
        captured = journal(db, protocol)
        if list(captured) != query_ids[:len(captured)] or any(
            data.keys() != arms.keys() for _, data in captured.values()
        ):
            raise EvaluationError("journal contains an unexpected query or incomplete paired arms")
        if len(captured) < len(records):
            identity = identities["common"]["retrieval"]
            for i, record in enumerate(warmup):
                capture_query(client, arms, record, i, order, identity)
            epoch = uuid4().hex
            for i in range(len(captured), len(records)):
                record = records[i]
                data = capture_query(client, arms, record, i, order, identity)
                if i + 1 == len(records) and check_services(client, arms) != identities:
                    with db:
                        db.execute("INSERT INTO invalid_epochs VALUES (?, ?)",
                                   (epoch, "service identity changed before final query commit"))
                    raise EvaluationError("service identity changed during capture")
                with db:
                    db.execute("INSERT INTO queries VALUES (?, ?, ?)", (
                        record.qid, epoch, json.dumps(data, allow_nan=False, ensure_ascii=False),
                    ))
                captured[record.qid] = (epoch, data)
                if (i + 1) % 100 == 0:
                    print(f"Captured {i + 1}/{len(records)} paired queries", flush=True)
    if sha256(args.qrels) != fingerprints["qrels"]:
        raise EvaluationError("qrels changed during capture")
    qrels = load_qrels(args.qrels)
    if sha256(args.qrels) != fingerprints["qrels"]:
        raise EvaluationError("qrels changed while loading")
    missing_qrels = set(query_ids) - qrels.keys()
    if missing_qrels:
        raise EvaluationError(f"{len(missing_qrels)} evaluated queries have no qrels")
    summarize(args.out, protocol, arms, {qid: qrels[qid] for qid in query_ids}, captured)
    print(args.out / "summary.json")


if __name__ == "__main__":
    main()
