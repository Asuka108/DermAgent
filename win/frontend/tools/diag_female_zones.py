#!/usr/bin/env python3
"""女性模型定位诊断：切女性模型后，在 22 个体表点做真实 onPick 链点击（前/后视各一轮），
记录 label+source+命中 mesh+zoneFromPoint 直验结果 → data/female_diag.json。
同时存白模渲染截图（v29 去贴图置白确认）。

用法：cd client && ./.venv/bin/python tools/diag_female_zones.py
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
        print(f"[console] {sourceID.split('/')[-1]}:{lineNumber} {message[:160]}", flush=True)


# 体表点（归一化世界系：x>0=解剖学左，y∈[-2.5,2.5]，z 前正后负；z 仅引导投影射线）
FRONT_POINTS = [
    ("neck", 0.0, 2.02, 0.20), ("chest_upper", 0.0, 1.55, 0.25), ("chest", 0.0, 0.95, 0.30),
    ("abdomen", 0.0, 0.30, 0.25), ("hip_front", 0.0, -0.05, 0.25),
    ("shoulder_l", 0.50, 1.42, 0.10), ("upper_arm_l", 0.95, 1.10, 0.10),
    ("elbow_l", 1.30, 0.75, 0.08), ("forearm_l", 1.50, 0.45, 0.08),
    ("wrist_l", 1.55, 0.22, 0.06), ("hand_l", 1.55, 0.02, 0.05),
    ("thigh_l", 0.35, -0.80, 0.25), ("knee_l", 0.37, -1.30, 0.22),
    ("calf_l", 0.37, -1.70, 0.20), ("ankle_l", 0.34, -2.15, 0.15), ("foot_l", 0.55, -2.42, 0.20),
]
BACK_POINTS = [
    ("back_upper", 0.0, 1.20, -0.30), ("back_lower", 0.0, 0.30, -0.28),
    ("buttock_l", 0.28, -0.55, -0.28), ("thigh_back_l", 0.33, -1.00, -0.22),
    ("calf_back_l", 0.35, -1.70, -0.20), ("heel_l", 0.33, -2.30, -0.22),
]


def click_js(points):
    pts = ", ".join('["%s", %.3f, %.3f, %.3f]' % p for p in points)
    return """
(() => {
  try {
    if (!modelRoot) return JSON.stringify({ ok: false, reason: 'no model' });
    const canvas = document.querySelector('#stage canvas');
    const rect = canvas.getBoundingClientRect();
    const pts = [%s];
    const out = [];
    function surf(wx, wy, wz) {
      const dir = new THREE.Vector3(wx, wy, wz).sub(camera.position).normalize();
      raycaster.set(camera.position, dir);
      const pickables = [];
      modelRoot.traverse(o => { if (o.isMesh && o.userData.pickable) pickables.push(o); });
      const hits = raycaster.intersectObjects(pickables, false);
      return hits.length ? hits[0].point : null;
    }
    for (const [name, x, y, z] of pts) {
      clearSelection();
      const sp = surf(x, y, z);
      const zn = zoneFromPoint(sp ? sp : new THREE.Vector3(x, y, z));
      const rec = { point: name };
      rec.zoneDirect = zn ? zn.id + '|' + zn.label : null;
      if (!sp) { rec.note = '射线未命中'; out.push(rec); continue; }
      const v = new THREE.Vector3(sp.x, sp.y, sp.z).project(camera);
      const cx = rect.left + (v.x + 1) / 2 * rect.width;
      const cy = rect.top + (1 - (v.y + 1) / 2) * rect.height;
      let clicked = null;
      for (const [dx, dy] of [[0,0],[3,0],[-3,0],[0,3],[0,-3]]) {
        canvas.dispatchEvent(new MouseEvent('click', { clientX: cx + dx, clientY: cy + dy, bubbles: true }));
        if (selectedRegions.length) { clicked = selectedRegions[selectedRegions.length - 1]; break; }
        clearSelection();
      }
      if (clicked) {
        rec.label = clicked.label; rec.source = clicked.source;
        rec.mesh = clicked.hit ? clicked.hit.mesh_name : null;
      } else rec.note = '点击未产生记录';
      out.push(rec);
    }
    clearSelection();
    return JSON.stringify({ ok: true, rows: out });
  } catch (e) { return JSON.stringify({ ok: false, reason: String(e && e.stack || e) }); }
})()
""" % pts


def main():
    app = QApplication(sys.argv)
    view = QWebEngineView()
    page = DebugPage(view)
    view.setPage(page)
    view.setGeometry(100, 100, 1280, 960)
    view.load(QUrl.fromLocalFile(os.path.join(HERE, "web", "index.html")))
    view.show()

    st = {"phase": "boot", "tries": 0, "front": None, "back": None}

    def js(code, cb):
        page.runJavaScript(code, cb)

    def grab(fname):
        view.grab().save(os.path.join(DATA, fname))

    def tick():
        ph = st["phase"]
        if ph == "boot":
            js("typeof modelRoot !== 'undefined' && !!modelRoot && !!fitInfo",
               lambda r: on_ready(bool(r)))
        elif ph == "switch":
            st["tries"] += 1
            js("modelKey()", on_switched)
        elif ph == "front":
            js("setView('front'); 'ok'", lambda _r: QTimer.singleShot(600, lambda: js(click_js(FRONT_POINTS), on_front)))
        elif ph == "shot_front":
            grab("female_white_front.png")
            st["phase"] = "back"
            QTimer.singleShot(300, tick)
        elif ph == "back":
            js("setView('back'); 'ok'", lambda _r: QTimer.singleShot(600, lambda: js(click_js(BACK_POINTS), on_back)))
        elif ph == "shot_back":
            grab("female_white_back.png")
            finish()

    def on_ready(ready):
        if not ready:
            st["tries"] += 1
            if st["tries"] > 60:
                print(json.dumps({"ok": False, "reason": "模型未就绪"}))
                app.exit(2)
                return
            QTimer.singleShot(500, tick)
            return
        js("(() => { const s = document.getElementById('modelSelect');"
           " s.value = '1'; s.dispatchEvent(new Event('change')); return s.value; })()",
           lambda r: print("[diag] modelSelect →", r, flush=True))
        st["phase"] = "switch"
        st["tries"] = 0
        QTimer.singleShot(4000, tick)

    def on_switched(key):
        if "Female3d66" not in str(key):
            if st["tries"] > 40:
                print(json.dumps({"ok": False, "reason": f"切换未生效：{key}"}))
                app.exit(2)
                return
            QTimer.singleShot(500, tick)
            return
        st["phase"] = "front"
        QTimer.singleShot(500, tick)

    def on_front(raw):
        st["front"] = parse(raw)
        st["phase"] = "shot_front"
        QTimer.singleShot(100, tick)

    def on_back(raw):
        st["back"] = parse(raw)
        st["phase"] = "shot_back"
        QTimer.singleShot(100, tick)

    def parse(raw):
        try:
            d = json.loads(raw) if isinstance(raw, str) else {}
        except (TypeError, ValueError):
            d = {}
        return d

    def finish():
        out = {"ok": st["front"] is not None and st["back"] is not None and
                       st["front"].get("ok") and st["back"].get("ok"),
               "front": (st["front"] or {}).get("rows"),
               "back": (st["back"] or {}).get("rows"),
               "reason": (st["front"] or {}).get("reason") or (st["back"] or {}).get("reason")}
        with open(os.path.join(DATA, "female_diag.json"), "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(json.dumps({"ok": out["ok"], "out": "data/female_diag.json",
                          "frontPng": "data/female_white_front.png",
                          "backPng": "data/female_white_back.png"}, ensure_ascii=False))
        app.exit(0 if out["ok"] else 2)

    QTimer.singleShot(800, tick)
    app.exec()


if __name__ == "__main__":
    sys.exit(main())
