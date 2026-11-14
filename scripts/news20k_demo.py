"""Replay the RFC-0002 engineering demo through the frozen news service."""
import argparse
from pathlib import Path
import json
import sys
from urllib.parse import urlsplit

from scripts.online_acceptance import (
    EmbeddingExpectation, Expectation, Probe, QueryEncoderRuntimeExpectation,
    _health, _index_identity, _routing, _sha256_digest, _vector_identity,
)


BGE = EmbeddingExpectation("BAAI/bge-small-zh-v1.5", "7999e1d3359715c523056ef9478215996d62a620", 512)
CORPUS = "1480ab10cbefcb57bc1132871420b1f88d18ac5f260150080953f9441377a861"
VECTOR_BY_MODE = {"lexical": False, "dense": True, "hybrid": True, "adaptive": False}
THEATRE = "抚州采茶戏如何培养青年演员并抢救散落的老剧本和曲谱？"
INSURANCE = "保险企业可以怎样利用信息技术改善投资数据监控和风险评估？"
CONSTRAINED = '"抚州采茶戏" AND "青年演员"'
HOLIDAY = "为什么节日期间的送礼和吃喝问题会成为党员干部作风建设的切入口？"
BAD_SYNTAX = '"抚州采茶戏'


def search(probe, case, query, mode, uses_vector, *, reason=None, first=None, k=3):
    body = probe.search(query, mode, k=k)
    rows = _routing(probe, body, mode, vector_used=uses_vector)
    assert len(rows) == k, f"expected {k} results"
    if reason:
        assert body["routing"]["reason"] == reason, "the router took an unexpected branch"
    if first:
        assert rows[0]["doc_id"] == first, "the reference document was not ranked first"
    return {"case": case, "mode": mode, "routing": body["routing"],
            "doc_ids": [row["doc_id"] for row in rows]}


def run(probe):
    _health(probe)
    _index_identity(probe)
    _vector_identity(probe)
    results = [search(probe, "newsq-0009", THEATRE, mode, uses_vector, first="crawl-006915",
                      reason="lexical_sufficient" if mode == "adaptive" else None)
               for mode, uses_vector in VECTOR_BY_MODE.items()]
    results.append(search(probe, "newsq-0013", HOLIDAY, "adaptive", True,
                          reason="small_relative_gap"))
    results.append(search(probe, "explicit_phrase_and", CONSTRAINED, "adaptive", False,
                          reason="explicit_constraints", first="crawl-006915", k=1))
    dense_error = probe.search(CONSTRAINED, "dense", expected=400)
    assert "无法保证显式词法约束" in dense_error.get("error", ""), "missing dense constraint error"
    bad_syntax = probe.search(BAD_SYNTAX, "adaptive", expected=400)
    assert "未闭合短语" in bad_syntax.get("error", ""), "missing phrase syntax error"
    pair = [search(probe, "newsq-0088", INSURANCE, mode, mode == "hybrid", first="crawl-008448",
                   reason="lexical_sufficient" if mode == "adaptive" else None, k=10) for mode in ("hybrid", "adaptive")]
    assert pair[0]["doc_ids"] != pair[1]["doc_ids"], "expected a lower-rank difference"
    results.extend(pair)
    _index_identity(probe)
    return {"status": "PASS_NEWS20K_LIVE_DEMO", "service_instance": probe.instance_id,
            "corpus_sha256": CORPUS, "searches": results,
            "expected_errors": {"dense_constraints": 400, "unclosed_phrase": 400}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--encoder-inventory-sha256", type=_sha256_digest, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    endpoint = urlsplit(args.endpoint)
    if (endpoint.scheme not in ("http", "https") or not endpoint.hostname
            or endpoint.username is not None or endpoint.query or endpoint.fragment
            or endpoint.path not in ("", "/")):
        parser.error("--endpoint must be a bare HTTP(S) service origin without credentials")
    if not __debug__:
        parser.error("demo checks require assertions enabled")
    if args.out.exists():
        parser.error("--out requires a new receipt path")
    expect = Expectation("rfc4-clean-news-20k", 20_000, CORPUS, "news_rfc4_clean20k", 256,
                         200, 60.0, "rules-v1", True, BGE,
                         QueryEncoderRuntimeExpectation("bge-cls-torch", "cpu", "float32",
                                                        args.encoder_inventory_sha256))
    probe = Probe(args.endpoint, expectation=expect, query=THEATRE, oov_query="",
                  run_tag="rfc0002-news20k-demo", timeout=30, require_instance_id=True)
    try:
        receipt = run(probe)
    except (AssertionError, KeyError, TypeError, ValueError) as error:
        print(f"Demo rejected the service or its results ({type(error).__name__})", file=sys.stderr)
        return 1
    receipt["encoder_inventory_sha256"] = args.encoder_inventory_sha256
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(f"{receipt['status']} ({len(receipt['searches'])} accepted searches, 2 expected HTTP 400)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
