# -*- coding: utf-8 -*-
"""v77 FastAPI 接口契约：统一错误封套、请求 ID、OpenAPI 元数据、错误码目录。

兼容原则：
- 现有路径与成功响应体**一律不动**；
- 错误响应在保留旧 `detail`（字符串或 dict）的前提下，追加机器可读的 `error` 对象；
- 现有端点不启用运行时 `response_model`（避免字段被过滤），只补 OpenAPI 文档元数据。
"""
import re
import uuid
from contextvars import ContextVar

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse

API_VERSION = "1.0"

# 分页约定：默认不裁剪（返回全量）；只声明上限供客户端参考
LIMITS = {"list_default_limit": None, "list_max_limit": 500, "chat_text_chars": 2000,
          "upload_bytes": 80 * 1024 * 1024}

# chat/stream 的 SSE 事件名（与 app.py _run_turn_worker 一致）
SSE_EVENTS = ("turn_started", "progress", "plan_ready", "step", "tool",
              "reflection", "message_delta", "heartbeat", "partial", "done", "error")

# 显式计划对象状态（plan.py）
PLAN_STATES = ("proposed", "approved", "executing", "completed", "failed",
               "cancelled", "superseded")

# 错误码目录：状态码 / 是否可重试 / 默认中文文案
ERROR_CODES = {
    "bad_request": (400, False, "请求参数不合法"),
    "validation_error": (422, False, "请求参数校验失败"),
    "invalid_field": (422, False, "包含未知病例字段"),
    "unauthorized": (401, False, "未通过鉴权"),
    "forbidden": (403, False, "当前状态不允许该操作"),
    "record_export_forbidden": (403, False, "病历尚未确认，禁止导出"),
    "not_found": (404, False, "资源不存在"),
    "case_not_found": (404, False, "病例不存在"),
    "turn_expired": (404, False, "turn 不存在或已过期"),
    "stream_disabled": (404, False, "流式接口未启用"),
    "mcp_disabled": (404, False, "MCP 接口未启用"),
    "conflict": (409, False, "状态冲突，请刷新后重试"),
    "turn_in_progress": (409, True, "该病例已有进行中的对话"),
    "intent_conflict": (409, False, "确认卡不存在或已失效"),
    "plan_not_confirmed": (409, False, "计划尚未确认"),
    "memory_unavailable": (503, True, "记忆层不可用"),
    "kb_unavailable": (503, True, "知识库不可用"),
    "llm_unavailable": (503, True, "LLM 未配置或不可达"),
    "multipart_unavailable": (400, False, "缺少 multipart 上传支持"),
    "method_not_allowed": (405, False, "请求方法不允许"),
    "payload_too_large": (413, False, "上传内容过大"),
    "mcp_policy_denied": (403, False, "MCP 策略拒绝"),
    "unavailable": (503, True, "依赖服务暂不可用"),
    "internal": (500, True, "服务端处理失败"),
}

_TAG_RULES = (
    ("/api/cases", "cases"),
    ("/api/records", "cases"),
    ("/api/patients", "patients"),
    ("/api/kb", "kb"),
    ("/api/rag", "kb"),
    ("/api/memory", "memory"),
    ("/api/llm", "llm"),
    ("/api/anatomy", "imaging"),
    ("/api/imaging", "imaging"),
    ("/api/nav", "navigation"),
    ("/api/artifacts", "navigation"),
    ("/api/tools", "meta"),
    ("/api/meta", "meta"),
    ("/api/health", "meta"),
    ("/mcp", "mcp"),
    ("/api/mcp", "mcp"),
)

_request_id = ContextVar("derm_request_id", default="")


def current_request_id() -> str:
    return _request_id.get() or ""


def tag_for(path: str) -> str:
    for prefix, tag in _TAG_RULES:
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix):
            return tag
    return "default"


class ApiError(Exception):
    """带错误码的业务异常；由全局 handler 转成统一封套。"""

    def __init__(self, status_code: int, code: str, message: str = "",
                 retryable: bool = None, detail=None, extra: dict = None):
        self.status_code = int(status_code)
        self.code = str(code or "internal")
        meta = ERROR_CODES.get(self.code)
        self.message = message or (meta[2] if meta else "请求失败")
        self.retryable = bool(retryable) if retryable is not None else bool(meta[1] if meta else False)
        self.detail = detail          # 严格保留旧 detail（str 或 dict）
        self.extra = extra or {}
        super().__init__(self.message)


def error_payload(code: str, message: str, detail=None, retryable: bool = False,
                  extra: dict = None, request_id: str = "") -> dict:
    err = {"code": code, "message": message, "retryable": bool(retryable),
           "request_id": request_id}
    if extra:
        err.update(extra)
    return {"ok": False, "detail": detail if detail is not None else message, "error": err}


def _legacy_code(status: int, detail) -> tuple:
    if isinstance(detail, dict) and detail.get("code"):
        return str(detail["code"]), str(detail.get("message") or detail["code"])
    return {400: "bad_request", 401: "unauthorized", 403: "forbidden",
            404: "not_found", 409: "conflict", 413: "payload_too_large",
            422: "validation_error", 503: "unavailable"}.get(status, "internal"), \
        str(detail) if not isinstance(detail, dict) else "请求失败"


class RequestIdMiddleware:
    """纯 ASGI 中间件：只补响应头，不缓冲流式响应（SSE 心跳不受影响）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        rid = headers.get(b"x-request-id", b"").decode("latin-1").strip()
        if not rid or len(rid) > 128:
            rid = "req_" + uuid.uuid4().hex[:12]

        async def send_with_id(message):
            if message.get("type") == "http.response.start":
                out = MutableHeaders(scope=message)
                out["X-Request-Id"] = rid
                out["X-API-Version"] = API_VERSION
            await send(message)

        token = _request_id.set(rid)
        try:
            await self.app(scope, receive, send_with_id)
        finally:
            _request_id.reset(token)


def paginate(items, limit, offset, response):
    """limit/offset 分页：默认全量返回（兼容旧客户端），总数走 X-Total-Count 头。"""
    items = list(items)
    response.headers["X-Total-Count"] = str(len(items))
    start = max(0, int(offset or 0))
    if limit is None:
        return items[start:]
    end = start + max(0, int(limit))
    return items[start:end]


def _install_openapi(app) -> None:
    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version,
                             description=app.description, routes=app.routes)
        comps = schema.setdefault("components", {}).setdefault("schemas", {})
        comps.setdefault("ErrorEnvelope", {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "detail": {"description": "兼容旧客户端的原始 detail"},
                "error": {"type": "object", "properties": {
                    "code": {"type": "string"}, "message": {"type": "string"},
                    "retryable": {"type": "boolean"}, "request_id": {"type": "string"}}},
            },
        })
        for path, methods in (schema.get("paths") or {}).items():
            tag = tag_for(path)
            for method, op in methods.items():
                if method.lower() not in ("get", "post", "put", "patch", "delete"):
                    continue
                op.setdefault("tags", [tag])
                if not op.get("operationId"):
                    op["operationId"] = re.sub(r"[^a-zA-Z0-9]+", "_",
                                               f"{method}_{path}").strip("_")
                if not op.get("summary"):
                    first = str(op.get("description") or "").strip().splitlines()
                    op["summary"] = (first[0][:80] if first else f"{method.upper()} {path}")
                responses = op.setdefault("responses", {})
                responses.setdefault("422", {"description": "参数校验失败",
                                             "content": {"application/json": {
                                                 "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}}}})
                if method.lower() in ("post", "put", "patch", "delete"):
                    responses.setdefault("500", {"description": "服务端错误",
                                                 "content": {"application/json": {
                                                     "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}}}})
        schema["x-api-version"] = API_VERSION
        schema["x-error-codes"] = {k: {"status": v[0], "retryable": v[1], "message": v[2]}
                                   for k, v in ERROR_CODES.items()}
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi


def install(app, spa_index: str = "") -> None:
    """注册中间件与异常处理器；spa_index 为空则不接管 404 页面回退。"""
    app.add_middleware(RequestIdMiddleware)

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):  # noqa: ANN001
        return JSONResponse(error_payload(exc.code, exc.message, exc.detail,
                                          exc.retryable, exc.extra,
                                          current_request_id()),
                            status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):  # noqa: ANN001
        fields = []
        for item in exc.errors():
            loc = ".".join(str(x) for x in (item.get("loc") or []) if x != "body")
            fields.append({"loc": loc or "body", "msg": str(item.get("msg") or ""),
                           "type": str(item.get("type") or "")})
        first = fields[0] if fields else {"loc": "body", "msg": "参数不合法"}
        message = f"参数校验失败：{first['loc']} {first['msg']}".strip()
        return JSONResponse(error_payload("validation_error", message, message, False,
                                          {"fields": fields}, current_request_id()),
                            status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):  # noqa: ANN001
        path = request.url.path
        if (exc.status_code == 404 and spa_index
                and path not in ("/api", "/mcp")
                and not path.startswith(("/api/", "/mcp/"))
                and request.method in ("GET", "HEAD")):
            return FileResponse(spa_index)
        code, message = _legacy_code(exc.status_code, exc.detail)
        detail = exc.detail if isinstance(exc.detail, dict) or isinstance(exc.detail, str) else message
        retryable = exc.status_code >= 500
        return JSONResponse(error_payload(code, message, detail, retryable,
                                          None, current_request_id()),
                            status_code=exc.status_code)

    _install_openapi(app)
