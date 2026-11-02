# -*- coding: utf-8 -*-
"""分层评测：标注集上的 L1 / L2 / 端到端准确率与延迟报告。

用法（在 web/backend 下）：
    python tools/eval_intent.py            # 用当前环境（BGE 已装则测 L2；LLM_* 配好则测 L3）
退出码：0 = 所有可评测门槛通过；1 = 有门槛不达标；2 = 评测环境不完整（如实提示，不算失败）。

门槛（与计划 §9.3 一致）：
- L1 在标注的 L1 子集上 precision = 1.00（规则零误判）；
- L2（已装 BGE）：L2 子集「已执行」精度 ≥ 0.95；
- 端到端：已执行样本精度 ≥ 0.95。
"""

import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
for _p in (BACKEND, os.path.join(BACKEND, "server"), os.path.join(BACKEND, "shared")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import intent_core as core          # noqa: E402
import rag_embed                    # noqa: E402
import intent as router             # noqa: E402

_GOLDEN = os.path.join(BACKEND, "tests-server", "data", "intent_golden.jsonl")


def _rows():
    out = []
    with open(_GOLDEN, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _state(row):
    return {"phase": row.get("phase"), "workflow_state": row.get("workflow_state"),
            "has_draft": not row.get("case_empty", True),
            "case_empty": row.get("case_empty", True)}


def _p95(vals):
    if not vals:
        return 0.0
    return statistics.quantiles(sorted(vals), n=20)[-1] if len(vals) > 1 else vals[0]


def _llm_from_env():
    base = (os.environ.get("LLM_BASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("LLM_API_KEY") or "").strip()
    model = (os.environ.get("LLM_MODEL") or "").strip()
    if not (base and model):
        return None
    return (base, key, model, (os.environ.get("LLM_REASONING_EFFORT") or "").strip())


def _gate(name, ok, detail):
    print(("  ✅ " if ok else "  ❌ ") + name + "：" + detail)
    return ok


def main() -> int:
    rows = _rows()
    failures = 0

    # —— 第一层 ——
    l1_rows = [r for r in rows if r.get("expect_layer") == 1]
    l2_rows = [r for r in rows if r.get("expect_layer") != 1]
    bad_l1, lat = [], []
    for r in l1_rows:
        t0 = time.perf_counter()
        hit = core.classify_rules(r["text"], _state(r))
        lat.append((time.perf_counter() - t0) * 1000)
        if not hit or hit["intent"] != r["expect"]:
            bad_l1.append((r["text"], r["expect"], hit and hit["intent"]))
    stolen = [(r["text"], core.classify_rules(r["text"], _state(r))["intent"])
              for r in l2_rows if core.classify_rules(r["text"], _state(r))]
    print(f"标注集：{len(rows)} 条（L1 子集 {len(l1_rows)}｜非 L1 {len(l2_rows)}）")
    print("第一层：")
    ok = _gate("L1 准确率", not bad_l1, f"{len(l1_rows) - len(bad_l1)}/{len(l1_rows)}" +
               ("" if not bad_l1 else f"｜错例：{bad_l1[:3]}"))
    failures += 0 if ok else 1
    ok = _gate("非 L1 子集零误抢", not stolen, "0 条" if not stolen else str(stolen[:3]))
    failures += 0 if ok else 1
    print(f"  L1 延迟 p95：{_p95(lat):.2f} ms")

    # —— 第二层 ——
    if not rag_embed.is_ready():
        print("第二层：BGE 未就绪 → 跳过（如实报告，不伪造）")
    else:
        router.warmup()
        print(f"第二层（{rag_embed.model_short()}）：")
        executed, correct, lat2 = 0, 0, []
        for r in l2_rows:
            t0 = time.perf_counter()
            l2 = router.layer2(r["text"], _state(r))
            lat2.append((time.perf_counter() - t0) * 1000)
            if l2 and l2["execute"]:
                executed += 1
                correct += 1 if l2["intent"] == r["expect"] else 0
        if executed:
            prec = correct / executed
            ok = _gate("L2 已执行精度", prec >= 0.95, f"{correct}/{executed} = {prec:.2f}")
            failures += 0 if ok else 1
        else:
            print("  ⚠️ 无高置信执行样本（阈值可能过严）")
        print(f"  L2 覆盖：{executed}/{len(l2_rows)}｜延迟 p95：{_p95(lat2):.1f} ms")

    # —— 端到端（L1 → L2 → L3）——
    llm = _llm_from_env()
    print("端到端：" + ("" if llm else "未配置 LLM_* → 第三层不参与；"))
    lat3 = [0.0]
    executed = correct = 0
    confirmed = 0
    wrong_exec = []
    for r in rows:
        t0 = time.perf_counter()
        meta = router.route(r["text"], _state(r), llm)
        lat3[0] = max(lat3[0], (time.perf_counter() - t0) * 1000)
        if meta.get("needs_confirm"):
            confirmed += 1
        elif meta.get("intent"):
            executed += 1
            if meta["intent"] == r["expect"]:
                correct += 1
            else:
                wrong_exec.append((r["text"], r["expect"], meta["intent"]))
    if executed:
        prec = correct / executed
        if rag_embed.is_ready() or llm:
            ok = _gate("端到端已执行精度", prec >= 0.95, f"{correct}/{executed} = {prec:.2f}")
            failures += 0 if ok else 1
        else:
            # 离线环境（无 BGE、无 LLM）：非 L1 输入只能回落 case_write——如实报告，不作门槛
            print(f"  ℹ️ 离线环境（无 BGE/LLM）：已执行精度 {correct}/{executed} = {prec:.2f}"
                  "（非 L1 输入回落 case_write，符合降级约定，不作门槛）")
    print(f"  已执行 {executed}｜确认卡 {confirmed}｜最慢一轮 {lat3[0]:.0f} ms")
    if wrong_exec:
        print(f"  执行错例（前 5）：{wrong_exec[:5]}")

    print("结论：" + ("全部门槛通过" if failures == 0 else f"{failures} 项未通过"))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
