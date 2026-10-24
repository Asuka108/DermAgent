#!/usr/bin/env python3
"""女性模型探针：在真机 QtWebEngine 里切到女性模型，dump 归一化包围盒与逐 mesh
世界包围盒（锚点实测输入），并存前/后/左视图截图。

用法：cd client && ./.venv/bin/python tools/probe_female.py
输出：data/female_probe.json + data/female_{front,back,left}.png
"""

import json
import os
import sys

from PySide6.QtCore import QUrl, QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)


class DebugPage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        print(f"[console] L{level} {sourceID.split('/')[-1]}:{lineNumber} {message[:200]}", flush=True)


DUMP_JS = """
(() => {
  try {
    const safe = v => (typeof v === 'number' && isFinite(v)) ? +v.toFixed(4) : null;
    if (!modelRoot || !fitInfo) return { ok: false, reason: 'modelRoot/fitInfo 未就绪' };
    const size = fitInfo.getSize(new THREE.Vector3());
    const meshes = [];
    modelRoot.traverse(o => {
      if (!o.isMesh) return;
      const b = new THREE.Box3().setFromObject(o);
      const idx = o.geometry.index ? o.geometry.index.count : o.geometry.attributes.position.count;
      const matNames = (Array.isArray(o.material) ? o.material : [o.material]).map(m => m.name || '?');
      meshes.push({ name: o.name, faces: idx / 3, mat: matNames.join(','),
                    min: b.min.toArray().map(safe), max: b.max.toArray().map(safe) });
    });
    return { ok: true, model: modelKey(), size: size.toArray().map(safe),
             min: fitInfo.min.toArray().map(safe), max: fitInfo.max.toArray().map(safe),
             meshes };
  } catch (e) { return { ok: false, reason: String(e && e.stack || e) }; }
})()
"""  # 结果经 JSON.stringify 传输（本 PySide6 runJavaScript 回调只可靠回传标量/字符串，对象/数组会变空）
DUMP_JS = "JSON.stringify(" + DUMP_JS.rstrip() + ")"


def main():
    app = QApplication(sys.argv)
    view = QWebEngineView()
    page = DebugPage(view)
    view.setPage(page)
    view.setGeometry(100, 100, 1280, 960)
    view.load(QUrl.fromLocalFile(os.path.join(HERE, "web", "index.html")))
    view.show()
    page.renderProcessTerminated.connect(
        lambda status, code: print(f"[probe] 渲染进程终止 status={status} code={code}", flush=True))

    # 显式计划表：boot → switch → dump → (view/shot)×3 → done
    state = {"phase": "boot", "tries": 0, "plan_i": 0}
    PLAN = [("view", "front"), ("shot", "female_front.png"),
            ("view", "back"), ("shot", "female_back.png"),
            ("view", "left"), ("shot", "female_left.png"),
            ("done", None)]

    def js(code, cb=lambda _: None):
        page.runJavaScript(code, cb)

    def shot(fname):
        view.grab().save(os.path.join(DATA, fname))

    def advance():
        state["plan_i"] += 1
        QTimer.singleShot(600, tick)

    def tick():
        if state["phase"] == "boot":
            js("typeof modelRoot !== 'undefined' && !!modelRoot && !!fitInfo",
               lambda r: on_ready(bool(r)))
            return
        if state["phase"] == "switch":
            state["tries"] += 1
            js("modelKey()", lambda r: on_switched(str(r)))
            return
        if state["phase"] == "dump":
            js(DUMP_JS, on_dump)
            return
        if state["plan_i"] >= len(PLAN):
            return
        action, arg = PLAN[state["plan_i"]]
        if action == "view":
            js(f'setView("{arg}")', lambda _: advance())
        elif action == "shot":
            shot(arg)
            advance()
        else:
            print(json.dumps({"ok": True, "out": os.path.join(DATA, "female_probe.json")}))
            app.exit(0)

    def on_ready(ready):
        if not ready:
            state["tries"] += 1
            if state["tries"] > 60:
                print(json.dumps({"ok": False, "reason": "模型未就绪"}))
                app.exit(2)
                return
            QTimer.singleShot(500, tick)
            return
        js("(() => { const s = document.getElementById('modelSelect');"
          " s.value = '1'; s.dispatchEvent(new Event('change')); return s.value; })()",
          lambda r: print("[probe] modelSelect →", r, flush=True))
        state["phase"] = "switch"
        state["tries"] = 0
        QTimer.singleShot(4000, tick)

    def on_switched(key):
        if "Female3d66" not in key:
            if state["tries"] > 40:
                print(json.dumps({"ok": False, "reason": f"切换未生效：{key}"}))
                app.exit(2)
                return
            QTimer.singleShot(500, tick)
            return
        state["phase"] = "dump"
        shot("female_switch.png")   # 切换后立即截图：若渲染进程崩溃可从画面看出
        QTimer.singleShot(800, tick)

    def on_dump(dump):
        try:
            dump = json.loads(dump) if isinstance(dump, str) else (dump or {})
        except (TypeError, ValueError):
            dump = {}
        if not dump.get("ok"):
            print(json.dumps({"ok": False, "reason": "dump 失败", "raw": dump}))
            app.exit(2)
            return
        with open(os.path.join(DATA, "female_probe.json"), "w") as f:
            json.dump(dump, f, ensure_ascii=False, indent=1)
        state["phase"] = "plan"
        QTimer.singleShot(100, tick)

    QTimer.singleShot(800, tick)
    app.exec()


if __name__ == "__main__":
    main()
