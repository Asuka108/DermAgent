#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 k 离线评估用的 30 条医学查询（6–40 字），输出 queries.jsonl。

- 默认调用服务端已配置的 LLM（server/data/llm_config.json，Key 不落日志）；
- `--from-file` 可直接导入人工整理的查询（每行一条或 JSONL 带 query 字段）；
- LLM 未配置/调用失败时如实报错，不自动编造“医学查询”。
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

DEFAULT_OUT = os.path.join(common.DATA_DIR, "kb", "eval", "queries.jsonl")
_SYSTEM = ("你是皮肤科循证检索评测集设计助手。只输出 JSON，不要解释。"
           "查询是医生在本地医学书籍库中检索时可能输入的自然语言，6–40 个汉字，"
           "覆盖不同疾病、症状、鉴别、治疗与用药主题；不得包含任何患者身份信息。")


def _book_titles() -> list:
    try:
        from kb.store import SQLiteMirrorStore
        rows = SQLiteMirrorStore().list_books(status="ready")
        return [str(r.get("title") or r.get("filename") or "") for r in rows][:30]
    except Exception:  # noqa: BLE001 — 镜像不可用则生成通用查询
        return []


def _prompt(count: int) -> str:
    titles = _book_titles()
    corpus = ("当前书目：" + "；".join(t for t in titles if t)) if titles else "当前书目：皮肤科通用教材（书目未知）"
    return (f"{corpus}\n请生成 {count} 条互不重复的检索查询，输出 JSON 数组，"
            '元素形如 {"query": "...", "topic": "疾病/症状/鉴别/治疗/用药之一"}。')


def _parse_queries(raw: str) -> list:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        return []
    try:
        payload = json.loads(match.group(0))
    except ValueError:
        return []
    out = []
    for item in payload if isinstance(payload, list) else []:
        query = str((item or {}).get("query") if isinstance(item, dict) else item or "").strip()
        if 6 <= len(query) <= 40:
            out.append({"query": query, "topic": str((item or {}).get("topic") or "")[:20]
                        if isinstance(item, dict) else ""})
    return out


def _call_llm(prompt: str, timeout: int = 120) -> tuple:
    import llm_config
    base, key, model, _ = llm_config.active_tuple()
    if not (base and key and model):
        return None, "llm_unavailable"
    url = base.rstrip("/") + "/chat/completions"
    body = json.dumps({"model": model, "temperature": 0.4, "messages": [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": prompt}]}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        import certifi
        import ssl
        ctx = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return None, f"llm_http_{exc.code}"
    except Exception as exc:  # noqa: BLE001 — 错误里可能带 URL，只保留类型
        return None, exc.__class__.__name__
    try:
        return str(payload["choices"][0]["message"]["content"]), ""
    except (KeyError, IndexError, TypeError):
        return None, "llm_bad_response"


def _from_file(path: str) -> list:
    rows = []
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if line.startswith("{"):
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                query = str(item.get("query") or "").strip()
                topic = str(item.get("topic") or "")[:20]
            else:
                query, topic = line, ""
            if 6 <= len(query) <= 40:
                rows.append({"query": query, "topic": topic})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="生成或导入 k 评估查询")
    ap.add_argument("--count", type=int, default=30)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--from-file", default="", help="导入人工整理的查询，不调用 LLM")
    ap.add_argument("--dry-run", action="store_true", help="只打印将发送的 prompt")
    args = ap.parse_args()

    if args.from_file:
        rows = _from_file(args.from_file)
    elif args.dry_run:
        print(_prompt(args.count))
        return 0
    else:
        raw, reason = _call_llm(_prompt(args.count))
        if raw is None:
            print(json.dumps({"ok": False, "reason": reason}, ensure_ascii=False))
            return 2
        rows = _parse_queries(raw)

    seen, unique = set(), []
    for row in rows:
        if row["query"] not in seen:
            seen.add(row["query"])
            unique.append(row)
    unique = unique[:args.count]
    if not unique:
        print(json.dumps({"ok": False, "reason": "no_valid_queries"}, ensure_ascii=False))
        return 2
    for idx, row in enumerate(unique, 1):
        row["query_id"] = f"q{idx:03d}"
    common.write_jsonl(args.out, [{"query_id": r["query_id"], "query": r["query"],
                                   "topic": r["topic"]} for r in unique])
    print(json.dumps({"ok": True, "count": len(unique), "out": args.out}, ensure_ascii=False))
    if len(unique) < args.count:
        print(json.dumps({"warning": "fewer_queries_than_requested",
                          "requested": args.count, "got": len(unique)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
