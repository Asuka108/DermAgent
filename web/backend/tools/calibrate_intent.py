# -*- coding: utf-8 -*-
"""第二层阈值校准：在已安装的真实 BGE 上，用标注集标定 L2 执行阈值。

为什么需要：向量相似度分布随模型（small/base/large/m3）变化，阈值不能拍脑袋。
本脚本以「已执行样本精度 ≥ 0.97」为硬约束，最大化已执行样本数（覆盖率），
把结果写回 shared/intent_spec.json 的 thresholds.per_model（可用 --dry-run 先看）。

用法（在 web/backend 下）：
    python tools/calibrate_intent.py                 # 只打印建议（当前激活模型）
    python tools/calibrate_intent.py --dry-run       # 同上，显式说明不写盘
    python tools/calibrate_intent.py --write         # 写回 intent_spec.json
前提：设置页已下载并激活 BGE；未就绪时脚本如实退出，不生成伪阈值。
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
for _p in (BACKEND, os.path.join(BACKEND, "server"), os.path.join(BACKEND, "shared")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import intent_core as core          # noqa: E402
import rag_embed                    # noqa: E402
import intent as router             # noqa: E402

_GOLDEN = os.path.join(BACKEND, "tests-server", "data", "intent_golden.jsonl")
_SPEC = os.path.join(BACKEND, "shared", "intent_spec.json")
_MIN_PRECISION = 0.97


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


def _samples():
    """只校准 L2 的目标样本：第一层必须漏掉的那些行。"""
    out = []
    for row in _rows():
        if row.get("expect_layer") == 1:
            continue
        ranked = router.rank_intents(row["text"], _state(row), timeout_ms=5000)
        if not ranked:
            return None
        top, score = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        out.append({"expect": row["expect"], "top": top, "score": score,
                    "margin": score - second,
                    "medical": core.intent_class(top) == "medical"})
    return out


def _grid(lo, hi, step=0.01):
    n = int(round((hi - lo) / step))
    return [round(lo + i * step, 2) for i in range(n + 1)]


def _best(samples):
    best = None
    for default_th in _grid(0.60, 0.95):
        for medical_th in _grid(0.65, 0.98):
            for margin in (0.03, 0.05, 0.08, 0.10):
                executed = [s for s in samples
                            if s["score"] >= (medical_th if s["medical"] else default_th)
                            and s["margin"] >= margin]
                if not executed:
                    continue
                correct = sum(1 for s in executed if s["top"] == s["expect"])
                precision = correct / len(executed)
                if precision < _MIN_PRECISION:
                    continue
                # 约束：精度达标后优先覆盖更多样本；同覆盖下取更高精度
                key = (len(executed), precision)
                if best is None or key > best[0]:
                    best = (key, {"layer2_execute_default": default_th,
                                  "layer2_execute_medical": medical_th,
                                  "layer2_margin": margin,
                                  "executed": len(executed), "precision": round(precision, 4),
                                  "coverage": round(len(executed) / len(samples), 4)})
    return best[1] if best else None


def _replace_per_model(text, per_model):
    """只替换 thresholds.per_model 的值，保持文件其余格式不动。"""
    key = '"per_model"'
    i = text.find(key)
    if i < 0:
        raise SystemExit("intent_spec.json 缺少 thresholds.per_model")
    j = text.find("{", i)
    depth, k, in_str, esc = 0, j, False, False
    while k < len(text):
        ch = text[k]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        k += 1
    block = json.dumps(per_model, ensure_ascii=False, indent=2).replace("\n", "\n    ")
    return text[:j] + block + text[k + 1:]


def main() -> int:
    ap = argparse.ArgumentParser(description="BGE 意图阈值校准")
    ap.add_argument("--write", action="store_true", help="写回 shared/intent_spec.json")
    ap.add_argument("--dry-run", action="store_true", help="只打印建议，不写盘")
    args = ap.parse_args()

    if not rag_embed.is_ready():
        print("BGE 未就绪：请先在设置页下载并激活 embedding 模型（本脚本不生成伪阈值）。")
        return 2
    short = rag_embed.model_short()
    samples = _samples()
    if not samples:
        print("模型无法编码标注集，已退出。")
        return 2
    best = _best(samples)
    if not best:
        print(f"没有满足精度 ≥ {_MIN_PRECISION} 的阈值组合——请检查原型句质量或补充标注。")
        return 1
    print(f"模型：{short}")
    print(f"L2 目标样本：{len(samples)}｜建议阈值：{best}")
    if args.write and not args.dry_run:
        with open(_SPEC, encoding="utf-8") as fh:
            text = fh.read()
        spec = json.loads(text)
        per_model = dict((spec.get("thresholds") or {}).get("per_model") or {})
        per_model[short] = {k: best[k] for k in
                            ("layer2_execute_default", "layer2_execute_medical", "layer2_margin")}
        with open(_SPEC, "w", encoding="utf-8") as fh:
            fh.write(_replace_per_model(text, per_model))
        print("已写回 thresholds.per_model")
    else:
        print("未写盘（加 --write 写回 intent_spec.json）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
