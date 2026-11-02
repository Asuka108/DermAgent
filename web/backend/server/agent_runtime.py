# -*- coding: utf-8 -*-
"""Agent 回合运行时（v76）：单轮预算、事件流、工具容错与诚实收尾。

与 agent_loop 的分工：
- agent_loop 负责系统提示词、工具 schema/实现、LLM HTTP 调用；
- 本模块只负责「一轮怎么跑」：步数/时间/token/工具次数预算、工具参数解析与修复、
  工具失败回注、事件发射（供 SSE 与桌面轮询）、以及失败时的诚实结论。
- 不 import app（避免循环依赖）；不 import agent_loop（工具与调用通过参数注入）。
"""
import hashlib
import inspect
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

try:
    import reflection as _reflection
    import tool_spec as _tool_spec
except ImportError:  # pragma: no cover - 包导入路径
    from server import reflection as _reflection
    from server import tool_spec as _tool_spec

MAX_TOOL_RESULT_CHARS = 4000
DEFAULT_MAX_STEPS = 6


class ToolsUnsupported(Exception):
    """端点不接受 tools/tool_choice 参数。"""


# —— 配置 ——

def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def estimate_tokens(text) -> int:
    """粗估 token：CJK 约 1 字/token，ASCII 约 3.5 字符/token。缺失 usage 时使用。"""
    if not text:
        return 0
    s = str(text)
    cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
    ascii_len = len(s) - cjk
    return int(cjk + ascii_len / 3.5) + 1


@dataclass
class TurnBudget:
    wall_ms: int = field(default_factory=lambda: _env_int("DERM_TURN_WALL_MS", 180_000))
    max_llm_calls: int = field(default_factory=lambda: _env_int("DERM_TURN_MAX_LLM_CALLS", 7))
    max_tool_calls: int = field(default_factory=lambda: _env_int("DERM_TURN_MAX_TOOL_CALLS", 12))
    max_tokens: int = field(default_factory=lambda: _env_int("DERM_TURN_MAX_TOKENS", 60_000))
    max_steps: int = field(default_factory=lambda: _env_int("DERM_AGENT_MAX_STEPS", DEFAULT_MAX_STEPS))
    max_reflection_calls: int = field(
        default_factory=lambda: _env_int("DERM_TURN_MAX_REFLECTION_CALLS", 2))
    started_at: float = field(default_factory=time.monotonic)
    llm_calls: int = 0
    reflection_calls: int = 0
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_estimated: bool = False
    exhausted_reason: str = ""

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    def check(self) -> str:
        """返回耗尽原因；空串表示仍可继续。"""
        if self.exhausted_reason:
            return self.exhausted_reason
        if self.elapsed_ms() >= self.wall_ms:
            return self._exhaust("budget_wall")
        if self.llm_calls >= self.max_llm_calls:
            return self._exhaust("max_llm_calls")
        if self.tool_calls >= self.max_tool_calls:
            return self._exhaust("max_tool_calls")
        if self.tokens_in + self.tokens_out >= self.max_tokens:
            return self._exhaust("max_tokens")
        return ""

    def has_room(self, n: int = 1) -> bool:
        """预约 n 次模型调用：核验不能吃掉自纠与收尾的额度。"""
        if self.exhausted_reason:
            return False
        if self.llm_calls + max(1, int(n)) > self.max_llm_calls:
            return False
        if self.elapsed_ms() >= self.wall_ms:
            return False
        return (self.tokens_in + self.tokens_out) < self.max_tokens

    def _exhaust(self, reason: str) -> str:
        self.exhausted_reason = reason
        return reason

    def add_usage(self, usage, prompt_messages, output: str) -> None:
        if isinstance(usage, dict) and (usage.get("total_tokens") or usage.get("prompt_tokens")):
            self.tokens_in += int(usage.get("prompt_tokens") or 0)
            self.tokens_out += int(usage.get("completion_tokens") or 0)
            if not usage.get("prompt_tokens") and usage.get("total_tokens"):
                self.tokens_in += int(usage["total_tokens"])
        else:
            self.tokens_estimated = True
            self.tokens_in += sum(estimate_tokens(m.get("content") or "")
                                  for m in prompt_messages if isinstance(m, dict))
            self.tokens_out += estimate_tokens(output)
        if self.tokens_in + self.tokens_out >= self.max_tokens:
            self._exhaust("max_tokens")

    def as_dict(self) -> dict:
        return {"wall_ms": self.wall_ms, "elapsed_ms": self.elapsed_ms(),
                "max_llm_calls": self.max_llm_calls, "llm_calls": self.llm_calls,
                "max_reflection_calls": self.max_reflection_calls,
                "reflection_calls": self.reflection_calls,
                "max_tool_calls": self.max_tool_calls, "tool_calls": self.tool_calls,
                "max_tokens": self.max_tokens, "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out, "tokens_estimated": self.tokens_estimated,
                "max_steps": self.max_steps, "exhausted_reason": self.exhausted_reason}


# —— 工具参数：解析、宽松修复、schema 校验 ——

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _schema_map(tools) -> dict:
    out = {}
    for tool in tools or []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            out[fn["name"]] = fn.get("parameters") or {}
    return out


def parse_tool_args(raw, schema: dict | None):
    """返回 (args, error)。error 为空串表示通过；绝不返回半解析的默认参数。"""
    if isinstance(raw, dict):
        args = dict(raw)
    else:
        text = str(raw or "").strip()
        m = _FENCE_RE.match(text)
        if m:
            text = m.group(1).strip()
        if not text:
            args = {}                 # 空参数同样过 schema（必填缺失要在执行前拦住）
        else:
            try:
                args = json.loads(text)
            except (ValueError, TypeError):
                try:
                    args = json.loads(_TRAILING_COMMA_RE.sub(r"\1", text))
                except (ValueError, TypeError) as exc:
                    return {}, f"JSON 解析失败：{str(exc)[:80]}"
    if not isinstance(args, dict):
        return {}, "参数必须是 JSON 对象"
    err = validate_args(args, schema or {})
    return (args, "") if not err else ({}, err)


def validate_args(args: dict, schema: dict) -> str:
    """v77：委托 tool_spec 递归校验（兼容旧调用：直接传 schema，不按工具名）。"""
    return _tool_spec.validate_schema(args, schema)


TOOL_RISK = {s.name: s.risk for s in _tool_spec.all() if s.expose_llm}


def _args_digest(args: dict) -> str:
    try:
        blob = json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        blob = str(args)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


# —— 事件注册表：SSE / 桌面轮询共用 ——

@dataclass
class TurnEvent:
    seq: int
    turn_id: str
    type: str
    payload: dict
    at: str

    def as_dict(self) -> dict:
        return {"seq": self.seq, "turn_id": self.turn_id, "type": self.type,
                "payload": self.payload, "at": self.at}


class TurnState:
    def __init__(self, turn_id: str, case_id: str, client_turn_id: str = ""):
        self.turn_id = turn_id
        self.case_id = case_id
        self.client_turn_id = client_turn_id
        self.state = "running"          # running | done | error
        self.events = []
        self.snapshot = None
        self.error = None
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.seq = 0
        self.done_event = threading.Event()

    def expired(self, ttl_s: int) -> bool:
        return (time.time() - self.updated_at) > ttl_s


class TurnRegistry:
    """进程内 turn 事件缓冲。turn 由后台线程执行，SSE 与轮询只读。"""

    def __init__(self, ttl_s: int | None = None, max_events: int = 500):
        self.ttl_s = int(ttl_s if ttl_s is not None else _env_int("DERM_TURN_REGISTRY_TTL_S", 600))
        self.max_events = max_events
        self._lock = threading.RLock()
        self._turns = {}
        self._by_client = {}
        self._case_running = {}

    def _gc(self) -> None:
        dead = [tid for tid, st in self._turns.items() if st.expired(self.ttl_s)]
        for tid in dead:
            st = self._turns.pop(tid, None)
            if st and st.client_turn_id:
                self._by_client.pop((st.case_id, st.client_turn_id), None)
            if st and self._case_running.get(st.case_id) == tid:
                self._case_running.pop(st.case_id, None)

    def begin(self, case_id: str, client_turn_id: str = "") -> tuple:
        """返回 (TurnState, created)。同 client_turn_id 幂等；同病例在途则不新建。"""
        with self._lock:
            self._gc()
            if client_turn_id:
                existing = self._by_client.get((case_id, client_turn_id))
                if existing:
                    return self._turns[existing], False
            running = self._case_running.get(case_id)
            if running and running in self._turns:
                return self._turns[running], False
            tid = "trn_" + uuid.uuid4().hex[:12]
            st = TurnState(tid, case_id, client_turn_id)
            self._turns[tid] = st
            self._case_running[case_id] = tid
            if client_turn_id:
                self._by_client[(case_id, client_turn_id)] = tid
            return st, True

    def emit(self, turn_id: str, type_: str, payload: dict) -> None:
        with self._lock:
            st = self._turns.get(turn_id)
            if not st:
                return
            st.seq += 1
            st.updated_at = time.time()
            st.events.append(TurnEvent(st.seq, turn_id, type_, payload or {}, _now()))
            if len(st.events) > self.max_events:
                st.events = st.events[-self.max_events:]

    def finish(self, turn_id: str, snapshot=None, error: dict | None = None) -> None:
        with self._lock:
            st = self._turns.get(turn_id)
            if not st:
                return
            st.state = "error" if error else "done"
            st.snapshot = snapshot
            st.error = error
            st.updated_at = time.time()
            if self._case_running.get(st.case_id) == turn_id:
                self._case_running.pop(st.case_id, None)
            st.done_event.set()

    def get(self, turn_id: str) -> TurnState | None:
        with self._lock:
            return self._turns.get(turn_id)

    def read(self, turn_id: str, after_seq: int = 0):
        with self._lock:
            st = self._turns.get(turn_id)
            if not st:
                return None
            events = [e.as_dict() for e in st.events if e.seq > after_seq]
            return {"turn_id": st.turn_id, "case_id": st.case_id, "state": st.state,
                    "events": events, "snapshot": st.snapshot, "error": st.error}

    def wait_events(self, turn_id: str, after_seq: int, timeout_s: float = 1.0):
        st = self.get(turn_id)
        if not st:
            return None
        st.done_event.wait(timeout=timeout_s)
        data = self.read(turn_id, after_seq)
        if data:
            data["done"] = st.done_event.is_set()
        return data

    def is_running(self, case_id: str) -> bool:
        with self._lock:
            tid = self._case_running.get(case_id)
            return bool(tid and tid in self._turns)


_registry = TurnRegistry()


def registry() -> TurnRegistry:
    return _registry


# —— 循环结果 ——

@dataclass
class LoopResult:
    final: str = ""
    tool_trace: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    fallback_reason: str = ""
    unsupported: bool = False
    mutated: bool = False
    reflection: dict = field(default_factory=dict)


_PARTIAL_TEXT = {
    "max_steps": "达到本轮步数上限",
    "max_llm_calls": "达到本轮模型调用上限",
    "max_tool_calls": "达到本轮工具调用上限",
    "max_tokens": "达到本轮 token 预算",
    "budget_wall": "达到本轮时间上限",
    "tool_args_invalid": "工具参数多次无效",
    "tool_failures": "工具连续执行失败",
    "llm_error": "模型连接中断",
    "empty_final": "模型未给出总结",
}


def _partial_text(reasons: list, tool_trace: list) -> str:
    labels = "、".join(_PARTIAL_TEXT.get(r, r) for r in reasons) or "未完成"
    done = len(tool_trace)
    tail = f"本轮未完成（{labels}）。已执行 {done} 个工具步骤，结果都已保存" if done else f"本轮未完成（{labels}）"
    return tail + "；未执行的部分没有改动。请继续对话或稍后重试。"


def _session_citations(sess: dict) -> list:
    """白名单只来自真实产物：本轮工具返回 + 会话已有 draft/doc 引用。"""
    out = []
    sess = sess if isinstance(sess, dict) else {}
    for key in ("draft", "doc"):
        sub = sess.get(key)
        if not isinstance(sub, dict):
            continue
        out.extend(c for c in (sub.get("citations") or []) if isinstance(c, dict))
        out.extend(c for c in (sub.get("book_citations") or []) if isinstance(c, dict))
    out.extend(c for c in (sess.get("book_citations") or []) if isinstance(c, dict))
    return out


def _reflection_bundle(sess: dict, query: str, evidence: list, draft: str = "") -> dict:
    return _reflection.build_bundle(sess, query, evidence, draft=draft,
                                    allowed=_session_citations(sess))


def _reflection_status(kind: str = "unverified", summary: str = "") -> dict:
    return {"verdict": kind, "status": "partial" if kind == "unverified" else "ok",
            "summary": summary, "codes": [], "corrections": 0,
            "at": _now()}


class _DeltaSink:
    """把模型增量合并成可读的 message_delta 事件；单轮事件数受控，不挤爆 500 上限。

    80ms / 80 字符先到即发；单轮超过 180 条后自动放宽阈值。reset 只在本轮确实
    输出过内容时发送，避免空帧干扰前端。任何 emit 失败都吞掉，不影响问诊。
    """

    def __init__(self, emit, min_interval_ms: int = 80, min_chars: int = 80,
                 max_events: int = 180, hard_cap: int = 300):
        self._emit = emit
        self._buf = []
        self._chars = 0
        self._last = 0.0
        self._seq = 0
        self._events = 0
        self._call = 0
        self._min_interval_ms = max(1, int(min_interval_ms))
        self._min_chars = max(1, int(min_chars))
        self._max_events = max(1, int(max_events))
        self._hard_cap = max(self._max_events, int(hard_cap))
        self._had_output = False

    def start_call(self, index: int) -> None:
        self._call = int(index or 0)

    def push(self, text: str) -> None:
        if not text:
            return
        self._buf.append(str(text))
        self._chars += len(str(text))
        now = time.monotonic()
        if (self._chars >= self._min_chars
                or (now - self._last) * 1000 >= self._min_interval_ms):
            self.flush()

    def flush(self, force: bool = False) -> None:
        if not self._buf:
            return
        if self._events >= self._hard_cap and not force:
            return                      # 硬上限：继续缓存，收尾时一次性补发（总数 ≤ cap+1）
        text = "".join(self._buf)
        self._buf = []
        self._chars = 0
        self._last = time.monotonic()
        self._had_output = True
        if self._events >= self._max_events:
            # 连续长输出：放宽合并阈值，保证事件总数远低于 TurnRegistry 的 500。
            self._min_interval_ms = max(self._min_interval_ms, 300)
            self._min_chars = max(self._min_chars, 200)
        self._events += 1
        self._seq += 1
        self._safe_emit({"text": text, "seq": self._seq, "call": self._call,
                         "final": False, "reset": False, "source": "model"})

    def reset(self) -> None:
        self._buf = []
        self._chars = 0
        if not self._had_output:
            return
        self._had_output = False
        self._events += 1
        self._seq += 1
        self._safe_emit({"text": "", "seq": self._seq, "call": self._call,
                         "final": False, "reset": True, "source": "model"})

    def close(self) -> None:
        self.flush(force=True)

    def _safe_emit(self, payload: dict) -> None:
        try:
            self._emit("message_delta", payload)
        except Exception:  # noqa: BLE001 — 展示事件失败不影响回合
            pass


def run_loop(messages: list, sess: dict, ctx: dict, budget: TurnBudget | None = None,
             emit=None, chat=None, execute=None, summarize=None,
             args_repair: bool | None = None, schemas=None,
             allowed_tools=None, reflect=None) -> LoopResult:
    """执行工具循环，返回最终文本/轨迹/元数据；不负责 finalize 与降级决策。"""
    budget = budget or TurnBudget()
    emit = emit or (lambda *_: None)
    chat = chat or ctx.get("chat")
    execute = execute or ctx.get("execute")
    summarize = summarize or ctx.get("tool_summary")
    reflect = reflect or ctx.get("reflect_chat")
    # v79：只有流式 turn 才启用增量；阻塞 /chat 保持整包请求与既有快照口径。
    raw_emit = emit
    sink = _DeltaSink(raw_emit) if ctx.get("stream_deltas") else None
    chat_hooks = False
    if sink is not None:
        try:
            chat_hooks = "on_delta" in inspect.signature(chat).parameters
        except (TypeError, ValueError):
            chat_hooks = False

    def emit(type_: str, payload: dict) -> None:
        """所有非增量事件前先冲掉缓冲，保证 delta 严格早于 tool/reflection/progress。"""
        if sink is not None:
            sink.flush()
        raw_emit(type_, payload)
    schemas = schemas if schemas is not None else ctx.get("tool_schemas") or []
    schema_map = _schema_map(schemas)
    repair_enabled = _env_bool("DERM_ARGS_REPAIR", True) if args_repair is None else bool(args_repair)
    turn_id = str(ctx.get("turn_id") or "trn_local")
    base_url, api_key, model, effort = ctx.get("llm") or ("", "", "", "")
    llm_tuple = (base_url, api_key, model)

    tool_trace, reasons, evidence = [], [], []
    correction_codes = []
    query = str(ctx.get("query") or "")
    if not query:
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "user":
                query = str(msg.get("content") or "")
                break
    mutated = False
    final = ""
    fallback_reason = ""
    unsupported = False
    repair_used = {}
    fail_streak = {}
    steps = 0
    stop = False
    corrections_used = 0
    retrieval_checked = False
    final_checked = False
    reflection_result = {}

    def _account_reflection(ref: dict) -> None:
        """评审真的发起了请求才计费：规则-only / 禁用 / 预算跳过不占额度。"""
        if ref.get("prompt_messages"):
            budget.llm_calls += 1
            budget.reflection_calls += 1
            budget.add_usage(ref.get("usage"), ref.get("prompt_messages") or [],
                             ref.get("raw_output") or "")

    def _emit_reflection(ref: dict, stage: str) -> None:
        payload = _reflection.compact_summary(ref)
        payload["stage"] = stage
        emit("reflection", payload)

    def _run_reflection(stage: str, draft: str, allow_llm: bool) -> dict:
        try:
            return _reflection.evaluate(_reflection_bundle(sess, query, evidence, draft),
                                        stage, reflect=reflect, llm=llm_tuple,
                                        allow_llm=allow_llm)
        except Exception:  # noqa: BLE001 — 核验异常不影响回合
            return {"stage": stage, "verdict": "unverified", "status": "error",
                    "summary": "核验执行异常，本轮未完成模型复核", "codes": [],
                    "issues": [], "blocking": [], "high_count": 0, "corrections": 0,
                    "allowed_citations": [], "at": _now(), "latency_ms": 0}

    for step in range(max(1, budget.max_steps)):
        reason = budget.check()
        if reason:
            reasons.append(reason)
            break
        steps = step + 1
        emit("progress", {"phase": "tools", "message": f"第 {steps} 步：模型决策",
                          "llm_calls": budget.llm_calls, "tool_calls": budget.tool_calls,
                          "tokens": budget.tokens_in + budget.tokens_out,
                          "elapsed_ms": budget.elapsed_ms()})

        if sink is not None:
            sink.start_call(steps)
            if steps > 1:
                sink.reset()          # 新一次模型调用：上一段预览作废（若显示过）
        assistant = None
        for attempt in range(2):
            try:
                if chat_hooks:
                    assistant = chat(base_url, api_key, model, messages, effort,
                                     on_delta=sink.push)
                else:
                    assistant = chat(base_url, api_key, model, messages, effort)
                budget.llm_calls += 1
                break
            except ToolsUnsupported:
                unsupported = True
                break
            except (ConnectionError, TimeoutError, OSError) as exc:
                budget.llm_calls += 1
                if step == 0 and attempt == 0:
                    if sink is not None:
                        sink.reset()   # 断流后重试：清掉可能的半截预览，避免重复
                    emit("progress", {"phase": "tools", "message": "模型首次连接失败，重试一次",
                                      "llm_calls": budget.llm_calls, "tool_calls": budget.tool_calls,
                                      "tokens": budget.tokens_in + budget.tokens_out,
                                      "elapsed_ms": budget.elapsed_ms()})
                    continue
                if sink is not None:
                    sink.reset()   # 中断：半截预览不作为正文
                if not mutated:
                    fallback_reason = f"connection_error:{str(exc)[:120]}"
                else:
                    final = f"（Agent 连接中断：{str(exc)[:120]}。已完成的部分已保存，已回退基础流程。）"
                    reasons.append("llm_error")
                stop = True
                break
            except Exception as exc:  # noqa: BLE001 — 意外错误同样如实降级，不让回合 500
                if sink is not None:
                    sink.reset()
                if not mutated:
                    fallback_reason = f"unexpected_error:{str(exc)[:120]}"
                else:
                    final = f"（Agent 内部异常：{str(exc)[:120]}。已完成的部分已保存，已回退基础流程。）"
                    reasons.append("llm_error")
                stop = True
                break
        if stop or unsupported:
            break
        if assistant is None:
            stop = True
            break

        usage = assistant.get("_usage") if isinstance(assistant, dict) else None
        content = str((assistant or {}).get("content") or "")
        budget.add_usage(usage, messages, content)
        calls = (assistant or {}).get("tool_calls") or []
        if calls and sink is not None:
            # 本次流出的只是「即将调用工具」的前导说明，不是最终回复：清掉预览。
            sink.flush()
            had_preamble = bool(content.strip())
            sink.reset()
            if had_preamble:
                emit("progress", {"phase": "tools", "message": "模型说明了将执行的步骤",
                                  "llm_calls": budget.llm_calls, "tool_calls": budget.tool_calls,
                                  "tokens": budget.tokens_in + budget.tokens_out,
                                  "elapsed_ms": budget.elapsed_ms()})
        if not calls:
            draft_text = content.strip()
            if not draft_text:
                final = ""
                reflection_result = _reflection_status(
                    "unverified", "模型未给出最终回复，未进行核验")
                break
            allow_llm = (bool(reflect) and budget.reflection_calls < budget.max_reflection_calls
                         and budget.has_room(1))
            emit("reflection", {"stage": "final", "verdict": "checking", "status": "ok",
                                "summary": "正在核验回复", "codes": [],
                                "corrections": corrections_used})
            ref = _run_reflection("final", draft_text, allow_llm)
            final_checked = True
            _account_reflection(ref)
            ref["corrections"] = corrections_used
            _emit_reflection(ref, "final")
            if ref.get("blocking"):
                if corrections_used == 0 and budget.has_room(1) and steps < budget.max_steps:
                    corrections_used = 1
                    for code in ref.get("codes") or []:
                        if code not in correction_codes:
                            correction_codes.append(code)
                    ref["corrections"] = 1
                    emit("reflection", {"stage": "final", "verdict": "correcting",
                                        "status": ref.get("status") or "ok",
                                        "summary": "发现 " + str(len(ref["blocking"]))
                                                   + " 项问题，正在修正",
                                        "codes": list(correction_codes), "corrections": 1})
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user",
                                     "content": _reflection.correction_message(
                                         ref["blocking"], "final")})
                    reflection_result = ref
                    continue
                final = _reflection.degrade_text(draft_text, ref["blocking"])
                if sink is not None:
                    sink.reset()          # 未通过核验的草稿不再作为正文显示
                ref["verdict"] = "degraded"
                ref["summary"] = None
                ref["corrections"] = corrections_used
                _emit_reflection(ref, "final")
                reflection_result = ref
                break
            final = draft_text
            reflection_result = ref
            break

        messages.append({"role": "assistant", "content": content, "tool_calls": calls})
        for call in calls:
            reason = budget.check()
            if reason:
                reasons.append(reason)
                stop = True
                break
            fn = str(((call or {}).get("function") or {}).get("name") or "")
            call_id = str((call or {}).get("id") or "")
            raw_args = ((call or {}).get("function") or {}).get("arguments")
            args, arg_err = parse_tool_args(raw_args, schema_map.get(fn))
            trace = {"tool": fn, "call_id": call_id, "step": steps, "risk": TOOL_RISK.get(fn, "read"),
                     "args_digest": _args_digest(args), "at": _now()}
            if arg_err:
                trace.update({"ok": False, "status": "args_invalid", "error": arg_err,
                              "summary": f"参数无效：{arg_err[:60]}"})
                if repair_enabled and repair_used.get(fn, 0) == 0:
                    repair_used[fn] = 1
                    result = {"ok": False, "error": "args_invalid",
                              "detail": arg_err + "；请重新调用并只输出合法 JSON 参数"}
                else:
                    fail_streak[fn] = fail_streak.get(fn, 0) + 1
                    result = {"ok": False, "error": "args_invalid", "detail": arg_err}
                    if fail_streak.get(fn, 0) >= 2:
                        reasons.append("tool_args_invalid")
                        stop = True
                tool_trace.append(trace)
                emit("tool", trace)
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "content": json.dumps(result, ensure_ascii=False)[:MAX_TOOL_RESULT_CHARS]})
                if stop:
                    break
                continue

            if allowed_tools is not None and fn not in allowed_tools:
                result = {"ok": False, "error": "tool_not_in_plan",
                          "detail": "该工具不在本轮已确认的计划内；请按计划执行或先请求医生确认"}
                trace.update({"ok": False, "status": "denied", "error": "tool_not_in_plan",
                              "summary": "工具不在计划内，已拒绝"})
                tool_trace.append(trace)
                emit("tool", trace)
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "content": json.dumps(result, ensure_ascii=False)[:MAX_TOOL_RESULT_CHARS]})
                continue

            t0 = time.monotonic()
            try:
                result = execute(fn, args, sess, ctx)
                mutated = True
                status = "ok" if bool(result.get("ok", True)) else "error"
                err = "" if status == "ok" else str(result.get("error") or "")[:200]
            except Exception as exc:  # noqa: BLE001 — 工具异常回注模型，不让回合 500
                result = {"ok": False, "error": str(exc)[:200]}
                status, err = "error", str(exc)[:200]
                mutated = True        # 可能已部分写入，按「已改动」处理，避免重复并入
            budget.tool_calls += 1
            duration = int((time.monotonic() - t0) * 1000)
            try:
                summary = summarize(fn, args, result) if summarize else fn
            except Exception:  # noqa: BLE001
                summary = fn
            trace.update({"ok": status == "ok", "status": status, "duration_ms": duration,
                          "error": err, "summary": summary})
            tool_trace.append(trace)
            emit("tool", trace)
            try:
                evidence.append(_reflection.evidence_item(fn, args, result,
                                                          status == "ok", steps))
            except Exception:  # noqa: BLE001 — 证据收集失败不影响工具结果
                pass
            if status == "error":
                fail_streak[fn] = fail_streak.get(fn, 0) + 1
                if fail_streak.get(fn, 0) >= 2:
                    reasons.append("tool_failures")
                    stop = True
            else:
                fail_streak.pop(fn, None)
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": json.dumps(result, ensure_ascii=False)[:MAX_TOOL_RESULT_CHARS]})
            if stop:
                break
        if (not stop and not retrieval_checked and reflect and evidence
                and budget.reflection_calls < budget.max_reflection_calls
                and budget.has_room(3)
                and any(item.get("tool") == "search_knowledge_base" and item.get("ok")
                        for item in evidence)):
            retrieval_checked = True
            emit("reflection", {"stage": "retrieval", "verdict": "checking",
                                "status": "ok", "summary": "正在核验检索证据",
                                "codes": [], "corrections": corrections_used})
            ref = _run_reflection("retrieval", "", True)
            _account_reflection(ref)
            _emit_reflection(ref, "retrieval")
            if ref.get("blocking") and corrections_used == 0:
                corrections_used = 1
                for code in ref.get("codes") or []:
                    if code not in correction_codes:
                        correction_codes.append(code)
                messages.append({"role": "user",
                                 "content": _reflection.correction_message(
                                     ref["blocking"], "retrieval")})
                ref["corrections"] = 1
                emit("reflection", {"stage": "retrieval", "verdict": "correcting",
                                    "status": ref.get("status") or "ok",
                                    "summary": "检索证据不足，正在请求补充检索",
                                    "codes": list(correction_codes), "corrections": 1})
        if stop:
            break

    if not reasons and not final and not fallback_reason and not unsupported:
        reasons.append("max_steps" if (tool_trace and steps >= budget.max_steps) else "empty_final")

    status = "partial" if (reasons or fallback_reason) else "completed"
    if unsupported:
        status = "fallback"
    if reasons and not final:
        final = _partial_text(reasons, tool_trace)
    if reasons and not final_checked:
        # 预算/步数打断时终稿是运行时拼的「未完成」文本，没有经过核验，不能报 pass。
        reflection_result = _reflection_status("unverified", "本轮未完成，未进行最终核验")
    if reflection_result and corrections_used and reflection_result.get("verdict") == "pass":
        reflection_result["verdict"] = "corrected"
        reflection_result["codes"] = (list(correction_codes)
                                      or list(reflection_result.get("codes") or []))
        reflection_result["corrections"] = corrections_used
    if not reflection_result:
        reflection_result = _reflection_status("unverified", "本轮未完成，未进行最终核验")
    meta = {"turn_id": turn_id, "status": status, "partial_reasons": reasons,
            "steps": steps, "tool_trace": tool_trace[-8:],
            "budget": budget.as_dict(), "started_at": _now(), "ended_at": _now(),
            "model": model}
    emit("progress", {"phase": "finalize", "message": "整理本轮结果",
                      "llm_calls": budget.llm_calls, "tool_calls": budget.tool_calls,
                      "tokens": budget.tokens_in + budget.tokens_out,
                      "elapsed_ms": budget.elapsed_ms()})
    if sink is not None:
        sink.close()
    return LoopResult(final=final, tool_trace=tool_trace, meta=meta,
                      fallback_reason=fallback_reason, unsupported=unsupported,
                      mutated=mutated,
                      reflection=_reflection.storage_view(reflection_result))
