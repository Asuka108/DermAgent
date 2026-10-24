#!/usr/bin/env python3
"""失败点插桩探针：女性模型上对诊断中 8 个「点击无记录」的点逐点插桩——
射线命中数、被过滤的 noPick 网格、异常捕获、selectedRegions/regionLabel 状态。"""
import json, os, sys
from PySide6.QtCore import QUrl, QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

class DebugPage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        print(f"[console] {sourceID.split('/')[-1]}:{lineNumber} {message[:200]}", flush=True)

PROBE_JS = r"""
(() => {
  try {
    const canvas = document.querySelector('#stage canvas');
    const rect = canvas.getBoundingClientRect();
    setView('front');
    const pts = {
      upper_arm_l: [0.95, 1.10, 0.10], elbow_l: [1.30, 0.75, 0.08],
      wrist_l: [1.55, 0.22, 0.06], calf_l: [0.37, -1.70, 0.20]
    };
    const out = { canvasRect: [rect.width, rect.height], winErrors: window.__errs || [] };
    out.rows = {};
    for (const [name, [x, y, z]] of Object.entries(pts)) {
      const dir = new THREE.Vector3(x, y, z).sub(camera.position).normalize();
      raycaster.set(camera.position, dir);
      const all = raycaster.intersectObject(modelRoot, true);
      const row = { allHits: all.length,
                    hitInfos: all.slice(0, 4).map(h => ({
                      mesh: h.object.name, mat: (Array.isArray(h.object.material) ? h.object.material[0].name : h.object.material.name),
                      noPick: !!h.object.userData.noPick, pickable: !!h.object.userData.pickable })) };
      clearSelection();
      const sp = all.length ? all[0].point : new THREE.Vector3(x, y, z);
      const v = sp.clone().project(camera);
      const cx = rect.left + (v.x + 1) / 2 * rect.width;
      const cy = rect.top + (1 - (v.y + 1) / 2) * rect.height;
      row.px = [Math.round(cx), Math.round(cy)];
      canvas.dispatchEvent(new MouseEvent('click', { clientX: cx, clientY: cy, bubbles: true }));
      row.selCount = selectedRegions.length;
      row.labelText = document.getElementById('regionLabel').textContent.slice(0, 60);
      // 命中点上的 DOM 元素（是否被浮层挡住）
      const el = document.elementFromPoint(cx, cy);
      row.elAt = el ? (el.id || el.tagName) : 'none';
      out.rows[name] = row;
    }
    return JSON.stringify(out);
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
    st = {"phase": "boot", "tries": 0}

    def tick():
        if st["phase"] == "boot":
            page.runJavaScript("typeof modelRoot !== 'undefined' && !!modelRoot", on_ready)
        elif st["phase"] == "switch":
            st["tries"] += 1
            page.runJavaScript("modelKey()", on_switched)
        elif st["phase"] == "probe":
            page.runJavaScript("window.__errs=[]; window.addEventListener('error', e => window.__errs.push(String(e.message))); 'ok'",
                               lambda _r: page.runJavaScript(PROBE_JS, on_probe))

    def on_ready(r):
        if not r:
            st["tries"] += 1
            if st["tries"] > 60: app.exit(2); return
            QTimer.singleShot(500, tick); return
        page.runJavaScript("(() => { const s = document.getElementById('modelSelect'); s.value='1'; s.dispatchEvent(new Event('change')); return 1; })()",
                           lambda _r: None)
        st["phase"] = "switch"; st["tries"] = 0
        QTimer.singleShot(4000, tick)

    def on_switched(key):
        if "Female3d66" not in str(key):
            if st["tries"] > 40: app.exit(2); return
            QTimer.singleShot(500, tick); return
        st["phase"] = "probe"
        QTimer.singleShot(500, tick)

    def on_probe(raw):
        try:
            d = json.loads(raw)
        except Exception:
            d = {"raw": str(raw)[:300]}
        print(json.dumps(d, ensure_ascii=False, indent=1))
        app.exit(0)

    QTimer.singleShot(800, tick)
    app.exec()

if __name__ == "__main__":
    sys.exit(main())
