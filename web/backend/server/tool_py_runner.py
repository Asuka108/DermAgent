# -*- coding: utf-8 -*-
"""本地 Python 工具子进程入口（只读契约，非 OS 沙箱）。

用法：
  python tool_py_runner.py --inspect <tool.py>   # 输出 TOOL_SPEC
  python tool_py_runner.py <tool.py>             # stdin 读 {"args": {...}}，stdout 写 {"ok":true,"data":...}

工具文件必须导出：
  TOOL_SPEC = {"name": "...", "description": "...", "input_schema": {...}}
  def run(args: dict) -> dict

run() 返回的数据放进 data；需要报错时直接抛异常，由运行器转成 {"ok": false, "error": ...}。
"""
import contextlib
import importlib.util
import json
import os
import sys

MAX_OUTPUT = 65536
REAL_STDOUT = sys.stdout


def _load_module(path: str):
    module_name = "derm_py_tool_" + str(abs(hash(os.path.realpath(path))))[:12]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError("无法加载工具文件")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _spec(module) -> dict:
    spec = getattr(module, "TOOL_SPEC", None)
    if not isinstance(spec, dict):
        raise ValueError("工具文件必须导出 TOOL_SPEC 字典")
    name = str(spec.get("name") or "").strip()
    description = str(spec.get("description") or "").strip()
    schema = spec.get("input_schema")
    if not name or not description or not isinstance(schema, dict):
        raise ValueError("TOOL_SPEC 需要 name / description / input_schema")
    if not callable(getattr(module, "run", None)):
        raise ValueError("工具文件必须导出 run(args) 函数")
    return {"name": name, "description": description, "input_schema": schema}


def _emit(payload: dict) -> int:
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) > MAX_OUTPUT:
        text = json.dumps({"ok": False, "error": "工具输出超过 64KB，已拒绝"}, ensure_ascii=False)
    REAL_STDOUT.write(text)
    REAL_STDOUT.flush()
    return 0


def main() -> int:
    argv = [x for x in sys.argv[1:] if x]
    if not argv:
        return _emit({"ok": False, "error": "缺少工具文件路径"})
    inspect = "--inspect" in argv
    paths = [x for x in argv if x != "--inspect"]
    if not paths:
        return _emit({"ok": False, "error": "缺少工具文件路径"})
    path = paths[0]
    if not os.path.isfile(path):
        return _emit({"ok": False, "error": "工具文件不存在"})
    try:
        with contextlib.redirect_stdout(sys.stderr):
            module = _load_module(path)
            spec = _spec(module)
        if inspect:
            return _emit({"ok": True, "spec": spec})
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        args = payload.get("args") if isinstance(payload, dict) else None
        if not isinstance(args, dict):
            return _emit({"ok": False, "error": "参数必须是 JSON 对象"})
        with contextlib.redirect_stdout(sys.stderr):
            result = module.run(dict(args))
        if not isinstance(result, dict):
            return _emit({"ok": False, "error": "run(args) 必须返回 JSON 对象"})
        return _emit({"ok": True, "data": result})
    except Exception as exc:  # noqa: BLE001 — 子进程内任何异常都转 JSON
        return _emit({"ok": False, "error": str(exc)[:300] or exc.__class__.__name__})


if __name__ == "__main__":
    raise SystemExit(main())
