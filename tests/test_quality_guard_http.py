from hashlib import sha256
import json
import sys
from types import SimpleNamespace

import pytest

import evaluation.audit_quality_guard_http as quality_audit
from evaluation.audit_quality_guard_http import FIXED, GUARD, request_pairs
from evaluation.benchmark_dureader_policies_http import capture_parity, policy_ce_budget, quality_guard_heavy


def test_quality_guard_uses_frozen_threshold_and_constraint_fallback():
    assert quality_guard_heavy(0.154, 0.153)
    assert not quality_guard_heavy(0.153, 0.153)
    assert not quality_guard_heavy(0.9, 0.153, explicit_constraints=True)


def test_quality_guard_budget_is_fixed_or_heavy():
    assert policy_ce_budget('quality-guard-40', 33, False) == 33
    assert policy_ce_budget('quality-guard-40', 33, True) == 40
    assert policy_ce_budget('fixed-pair-budget', 33, False) == 33
    assert policy_ce_budget('ce28', 33, False) == 28


def test_quality_guard_audit_requires_exact_fixed_control_and_unique_pairs():
    queries = {"q": "apple"}
    gate = SimpleNamespace(predict=lambda features: [features[0][0]])
    evidence = {
        "query_id": "q", "client_wall_ms": 1, "fallback": False,
        "explicit_constraints": False, "vector_attempt": True,
        "gate_features": [0.2, 0, 0, 5], "results": [str(i) for i in range(40)],
    }
    guard = {**evidence, "policy": GUARD, "gate_heavy": True, "ce_pairs": 40}
    fixed = {**evidence, "policy": FIXED, "gate_heavy": False, "ce_pairs": 33}
    assert request_pairs([fixed, guard], queries, gate, 0.153)[FIXED] == [fixed]
    for bad in ({**fixed, "ce_pairs": 40}, {**fixed, "fallback": True},
                {**fixed, "vector_attempt": None}, {**fixed, "query_id": "different"}, guard):
        with pytest.raises(ValueError):
            request_pairs([guard, bad], queries, gate, 0.153)

    historical = {key: value for key, value in fixed.items() if key not in ("fallback", "explicit_constraints")}
    with pytest.raises(ValueError):
        request_pairs([guard, historical], queries, gate, 0.153)
    assert request_pairs([guard, historical], queries, gate, 0.153, historical=True)[FIXED] == [historical]


def test_existing_benchmark_policies_retain_capture_parity():
    recorded = {"heavy": False, "actions": {"bm25": ["d"], "learned-router": ["d"]}}
    row = {"results": ["d"], "gate_heavy": False}
    assert capture_parity(row, "bm25", recorded) is True
    assert capture_parity({**row, "results": ["other"]}, "bm25", recorded) is False
    assert capture_parity(row, GUARD, recorded) is None
    with pytest.raises(ValueError, match="Router decision"):
        capture_parity({**row, "gate_heavy": True}, "learned-router", recorded)


@pytest.mark.parametrize(
    ("historical", "expected_source", "expected_exit"),
    [(True, "historical", 0), (False, "unbound_recapture", 3), (False, None, None)],
)
def test_quality_guard_cli_preserves_recomputed_quality_without_accepting_unbound_recapture(
    tmp_path, monkeypatch, capsys, historical, expected_source, expected_exit
):
    queries = tmp_path / "queries.tsv"
    qrels = tmp_path / "qrels.txt"
    models = tmp_path / "models.json"
    requests = tmp_path / "requests.jsonl"
    model = {"mean": [0] * 4, "scale": [1] * 4, "coef": [1, 0, 0, 0], "intercept": 0}
    models.write_text(json.dumps({"gate": {"model": model}}), encoding="utf-8")
    qids = [f"q{i}" for i in range(808)]
    queries.write_text("".join(f"{qid}\tapple\n" for qid in qids), encoding="utf-8")
    qrels.write_text("".join(f"{qid} d39 1\n" for qid in qids), encoding="utf-8")
    ranking = [f"d{i}" for i in range(40)]
    with requests.open("w", encoding="utf-8") as handle:
        for index, qid in enumerate(qids):
            heavy = index < 100
            evidence = {"query_id": qid, "vector_attempt": True, "client_wall_ms": 1}
            if not historical:
                evidence.update(explicit_constraints=False, fallback=expected_source is None)
            fixed = dict(evidence, policy=FIXED, gate_heavy=False, ce_pairs=33, results=ranking)
            guard = dict(
                evidence, policy=GUARD, gate_heavy=heavy, ce_pairs=40 if heavy else 33,
                results=[ranking[-1], *ranking[:-1]] if heavy else ranking,
                gate_features=[float(heavy), 0, 0, 40],
            )
            for row in (fixed, guard):
                handle.write(json.dumps(row) + "\n")
    request_digest = sha256(requests.read_bytes()).hexdigest()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "training_model_sha256": sha256(models.read_bytes()).hexdigest(),
        "holdout_sha256": {
            "queries": sha256(queries.read_bytes()).hexdigest(),
            "qrels": sha256(qrels.read_bytes()).hexdigest(),
        },
        "http_requests_sha256": request_digest if historical else "0" * 64,
        "threshold": 0.5,
        "cost_gate": {"max_mean_ce_pairs": 36.49},
    }), encoding="utf-8")
    monkeypatch.setattr(quality_audit, "AUDIT_CONFIG", config)
    monkeypatch.setattr(sys, "argv", [
        "audit_quality_guard_http", "--folder", str(tmp_path), "--qrels", str(qrels),
        "--models", str(models), "--queries", str(queries), "--requests-sha256", request_digest,
    ])

    if expected_source is None:
        qrels.unlink()
        with pytest.raises(ValueError, match="fallback occurred"):
            quality_audit.main()
        assert not list(tmp_path.glob("audit-*.json"))
        return
    assert quality_audit.main() == expected_exit
    filename = "audit-verified.json" if historical else "audit-unbound.json"
    result = json.loads((tmp_path / filename).read_text(encoding="utf-8"))
    assert (tmp_path / "audit-verified.json").is_file() is historical
    assert result == json.loads(capsys.readouterr().out)
    assert result["quality_guard_source"] == expected_source
    assert result["fallback_field_required"] is not historical
    assert result["quality_gate_passed"] is True
    assert result["ce_pairs_mean"] == {GUARD: pytest.approx(33 + 700 / 808), FIXED: 33.0}
