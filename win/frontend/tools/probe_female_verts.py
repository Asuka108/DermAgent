#!/usr/bin/env python3
"""女性模型锚点实测探针（第 2 步）：切到女性模型后，dump 语义命名 mesh 的顶点云
（眼/指甲/趾甲/唇/耳/躯干等，归一化世界坐标，0.1mm 量化），供 Python 端聚类生成
anatomy-labels-Female3d66.json 的锚点。

用法：cd client && ./.venv/bin/python tools/probe_female_verts.py
输出：data/female_verts.json
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


VERTS_JS = """
(() => {
  try {
    const safe = v => (typeof v === 'number' && isFinite(v)) ? Math.round(v * 10000) / 10000 : 0;
    if (!modelRoot || !fitInfo) return JSON.stringify({ ok: false, reason: 'not ready' });
    const want = ['Sclera', 'Irises', 'Pupils', 'Cornea', 'EyeSocket', 'EyeMoisture',
                  'Fingernails', 'Toenails', 'Lips', 'Mouth', 'Teeth', 'Ears', 'Face',
                  'Torso', 'Eyelashes'];
    const out = {};
    modelRoot.updateWorldMatrix(true, true);
    modelRoot.traverse(o => {
      if (!o.isMesh) return;
      const mats = (Array.isArray(o.material) ? o.material : [o.material]);
      const hit = mats.find(m => want.indexOf(m.name) !== -1);
      if (!hit) return;
      const pos = o.geometry.attributes.position;
      const v = new THREE.Vector3();
      const arr = [];
      for (let i = 0; i < pos.count; i++) {
        v.fromBufferAttribute(pos, i);
        o.localToWorld(v);
        arr.push(safe(v.x), safe(v.y), safe(v.z));
      }
      const key = hit.name;
      if (out[key]) out[key].push(arr); else out[key] = [arr];
    });
    const size = fitInfo.getSize(new THREE.Vector3());
    return JSON.stringify({ ok: true, model: modelKey(),
                            size: size.toArray().map(safe),
                            min: fitInfo.min.toArray().map(safe),
                            max: fitInfo.max.toArray().map(safe),
                            parts: out });
  } catch (e) { return JSON.stringify({ ok: false, reason: String(e && e.stack || e) }); }
})()
"""


def main():
    app = QApplication(sys.argv)
    view = QWebEngineView()
    page = DebugPage(view)
    view.setPage(page)
    view.setGeometry(100, 100, 1280, 960)
    view.load(QUrl.fromLocalFile(os.path.join(HERE, "web", "index.html")))
    view.show()

    state = {"phase": "boot", "tries": 0}

    def js(code, cb):
        page.runJavaScript(code, cb)

    def tick():
        if state["phase"] == "boot":
            js("typeof modelRoot !== 'undefined' && !!modelRoot && !!fitInfo",
               lambda r: on_ready(bool(r)))
        elif state["phase"] == "switch":
            state["tries"] += 1
            js("modelKey()", on_switched)
        elif state["phase"] == "verts":
            js(VERTS_JS, on_verts)

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
        if "Female3d66" not in str(key):
            if state["tries"] > 40:
                print(json.dumps({"ok": False, "reason": f"切换未生效：{key}"}))
                app.exit(2)
                return
            QTimer.singleShot(500, tick)
            return
        state["phase"] = "verts"
        QTimer.singleShot(800, tick)

    def on_verts(raw):
        try:
            dump = json.loads(raw) if isinstance(raw, str) else {}
        except (TypeError, ValueError):
            dump = {}
        if not dump.get("ok"):
            print(json.dumps({"ok": False, "reason": "verts dump 失败", "raw": str(raw)[:200]}))
            app.exit(2)
            return
        out = os.path.join(DATA, "female_verts.json")
        with open(out, "w") as f:
            json.dump(dump, f)
        stats = {k: sum(len(a) // 3 for a in v) for k, v in dump["parts"].items()}
        print(json.dumps({"ok": True, "out": out, "verts": stats}, ensure_ascii=False))
        app.exit(0)

    QTimer.singleShot(800, tick)
    app.exec()


if __name__ == "__main__":
    main()
