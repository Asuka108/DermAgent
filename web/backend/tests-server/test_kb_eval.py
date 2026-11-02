# -*- coding: utf-8 -*-
"""k 评估工具离线测试：无标注占位、NDCG 饱和点、聚类产物、apply dry-run。"""
import importlib
import json
import os
import sys

import pytest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))), "tools", "kb_eval")
sys.path.insert(0, TOOLS)

common = importlib.import_module("common")
run_eval = importlib.import_module("run_eval")
apply_spec = importlib.import_module("apply_spec")


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _fixture(tmp_path, labels=True):
    queries = [{"query_id": f"q{i}", "query": f"查询{i}", "topic": "疾病"} for i in range(1, 5)]
    _write_jsonl(tmp_path / "queries.jsonl", queries)
    candidates = []
    for q in queries:
        for rank in range(1, 11):
            candidates.append({"query_id": q["query_id"], "query": q["query"], "rank": rank,
                               "source": "milvus", "book_id": "bk1",
                               "chunk_id": f"{q['query_id']}-c{rank}",
                               "chunk_anchor": f"a{rank}", "content": "x",
                               "vector_score": round(1.0 / rank, 4), "bm25_score": None})
    _write_jsonl(tmp_path / "candidates.jsonl", candidates)
    with open(tmp_path / "query_vectors.jsonl", "w", encoding="utf-8") as fh:
        for i, q in enumerate(queries):
            vec = [0.0] * 768
            vec[i] = 1.0
            vec[(i + 1) % 768] = 0.5
            fh.write(json.dumps({"query_id": q["query_id"], "vector": vec}) + "\n")
    labels_path = tmp_path / "labels.csv"
    with open(labels_path, "w", encoding="utf-8") as fh:
        fh.write("query_id,query,book_id,chunk_anchor,grade\n")
        if labels:
            for q in queries:
                for anchor, grade in (("a1", 2), ("a2", 1), ("a3", 1)):
                    fh.write(f'{q["query_id"]},{q["query"]},bk1,{anchor},{grade}\n')
    return queries, labels_path


def _run(monkeypatch, module, argv):
    monkeypatch.setattr(sys, "argv", [module.__name__] + argv)
    return module.main()


def test_ndcg_uses_fixed_ideal_and_saturation_point():
    curve = common.ndcg_curve([2, 0, 1, 0] + [0] * 46, 50, ideal_grades=[2, 1])
    assert curve[1] == pytest.approx(3 / (3 + 1 / 1.5849625), rel=1e-4)
    assert curve[3] == pytest.approx(3.5 / (3 + 1 / 1.5849625), rel=1e-4)
    assert common.saturation_k(curve, 50) == 4     # 相对 NDCG@50 饱和：k=3 达标但增益仍大
    ordered = common.ndcg_curve([2, 1] + [0] * 48, 50, ideal_grades=[2, 1])
    assert ordered[2] == pytest.approx(1.0, rel=1e-6)
    assert common.saturation_k(ordered, 50) == 3    # k=2 达阈值但增益仍大，k=3 稳定


def test_dry_run_without_labels_keeps_defaults(tmp_path, monkeypatch):
    _fixture(tmp_path, labels=False)
    out = tmp_path / "kspec.json"
    code = _run(monkeypatch, run_eval, [
        "--queries", str(tmp_path / "queries.jsonl"),
        "--labels", str(tmp_path / "labels.csv"),
        "--out", str(out), "--dry-run"])
    assert code == 0
    spec = json.loads(out.read_text(encoding="utf-8"))
    assert spec["labels_present"] is False
    assert spec["global_k"] == 20
    assert spec["clusters"] == []


def test_labeled_eval_produces_clusters_and_respects_unlabeled(tmp_path, monkeypatch):
    _fixture(tmp_path)
    out = tmp_path / "kspec.json"
    code = _run(monkeypatch, run_eval, [
        "--queries", str(tmp_path / "queries.jsonl"),
        "--candidates", str(tmp_path / "candidates.jsonl"),
        "--vectors", str(tmp_path / "query_vectors.jsonl"),
        "--labels", str(tmp_path / "labels.csv"),
        "--out", str(out), "--clusters", "2"])
    assert code == 0
    spec = json.loads(out.read_text(encoding="utf-8"))
    assert spec["labels_present"] is True
    assert spec["global_k"] == 4
    assert {v["k_star"] for v in spec["queries"].values()} == {4}
    assert len(spec["clusters"]) == 2
    assert all(len(c["centroid"]) == 768 for c in spec["clusters"])
    assert spec["eval"]["clustering"] == "ok"
    assert spec["eval"]["queries_labeled"] == 4


def test_apply_spec_dry_run_and_unlabeled_guard(tmp_path, monkeypatch, capsys):
    _fixture(tmp_path, labels=False)
    out = tmp_path / "kspec.json"
    assert _run(monkeypatch, run_eval, [
        "--queries", str(tmp_path / "queries.jsonl"),
        "--labels", str(tmp_path / "labels.csv"),
        "--out", str(out), "--dry-run"]) == 0
    capsys.readouterr()
    assert _run(monkeypatch, apply_spec, [str(out), "--dry-run"]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "labels_present_false"
    assert _run(monkeypatch, apply_spec,
                [str(out), "--dry-run", "--allow-unlabeled"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
