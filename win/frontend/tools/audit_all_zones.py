#!/usr/bin/env python3
"""v24 全量分区审计：语义表每个分区逐区探针点击（真实 onPick 链）。

方法（v24a 修订，含两处关键方法学修正）：
  1. 从 face-semantics.json 的 segments 收集每个 zone 的候选面（按段独立采样，主段优先）；
  2. 每候选面给 7 个相机方向：几何外法线（离开中轴校正）+ 全局 ±x/±y/±z；
  3. 探针相机置于候选点 + 方向*1.6，将候选点投影到**整数像素**（关键修正①：
     MouseEvent 的 clientX/clientY 按规范截断为整数，探针若用浮点像素预验证、
     click 用整数像素，0.5px 偏移在细面区足以跨面 → 52 个假阴性），在该整数像素
     上做射线预验证；命中目标 zone 的像素才派发真实 click；
  4. 整数像素 ±2 网格重试（关键修正②，恢复 v23 方法学）：目标点像素在非凸部位
     （手指内侧/腋窝/耳）或碎面交错区可能差 1-2px 命中邻面，网格内找任一命中
     目标 zone 的像素即视为该分区可正确点击；
  5. click 后断言 selectedRegions 末条 id === "fsem:目标zone"（与产品完全同链）。

判定：PASS = click 记录即目标 zone；UNREACHABLE = 所有候选/方向/网格像素都命不中
目标 zone（记录 got 分布，供 Python 端区分「覆盖层遮挡（产品正确）」与「真边界
缺陷」）；click 后记录不符理论不会发生（同像素同射线），发生则记 anomaly。

分批执行：每批 ~25 个 zone 单独 runJavaScript，防渲染进程长时间阻塞。
输出：data/audit_zones_report.json（全量记录）+ 控制台失败清单。
用法：cd client && ./.venv/bin/python tools/audit_all_zones.py [--max-per-zone N]
"""
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "tools"))

from PySide6.QtCore import QUrl, QTimer, Qt, QEventLoop  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtWebEngineCore import QWebEnginePage  # noqa: E402
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402


class DebugPage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        if level == 3:  # error
            print(f"[console-err] {message[:200]}", flush=True)


# ============ 阶段 1：本地算候选面（不依赖浏览器） ============
def build_candidates(max_per_zone=4):
    import numpy as np
    from glb_io import GLB, register_transform

    sem_path = os.path.join(HERE, "web", "face-semantics.json")
    sem = json.load(open(sem_path, encoding="utf-8"))
    glb_path = os.path.join(HERE, "web", "vendor", "models", "Custom3d66.glb")
    g = GLB(glb_path)
    scale, offset, _, _ = register_transform(g)

    inst_by_name = {}
    for inst in g.mesh_instances():
        name = inst["mesh_name"]
        mesh = g.json["meshes"][inst["mesh_index"]]
        if len(mesh["primitives"]) > 1:
            for pi in range(len(mesh["primitives"])):
                inst_by_name[f"{name}_{pi + 1}"] = (inst, pi)
        else:
            inst_by_name[name] = (inst, 0)

    zone_cands = defaultdict(list)   # zoneId -> [{mesh, fi, p, dirs}]
    global_dirs = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
    for mname, (inst, pi) in inst_by_name.items():
        ent = sem["meshes"].get(mname)
        if not ent:
            continue
        mesh = g.json["meshes"][inst["mesh_index"]]
        prim = mesh["primitives"][pi]
        pos, _ = g._accessor_array(prim["attributes"]["POSITION"])
        pos = pos.astype(np.float64)
        if "indices" in prim:
            idx, _ = g._accessor_array(prim["indices"])
            idx = np.asarray(idx, dtype=np.int64).reshape(-1)  # SCALAR accessor 是 (N,1) 需摊平
            tri = idx.reshape(-1, 3)
        else:
            tri = np.arange(len(pos), dtype=np.int64).reshape(-1, 3)
        nm = np.asarray(inst["matrix"], dtype=np.float64)
        world = pos @ nm[:3, :3].T + nm[:3, 3]
        world = world * scale + offset
        # 按 zone 独立采样：v25 修正为「各段轮转取样」——不再按分位点先到先得被主段
        # 吃满，保证碎段（如腰背高位段 y 0.35-0.62，低位段被内裤遮盖）也有代表面。
        per_zone_target = max_per_zone * 2
        by_zone_fi = {}
        seg_lists = {}
        for s0, s1, zid in ent["segments"]:
            if zid:
                seg_lists.setdefault(zid, []).append((s0, s1))
        for zid, spans in seg_lists.items():
            rotated = []
            idxs = [s0 for s0, _ in spans]
            live = True
            while live and len(rotated) < per_zone_target * 2:
                live = False
                for si, (s0, s1) in enumerate(spans):
                    if idxs[si] < s1:
                        rotated.append(idxs[si])
                        idxs[si] += 1
                        live = True
                        if len(rotated) >= per_zone_target * 2:
                            break
            by_zone_fi[zid] = rotated[:per_zone_target * 2]
        for zid, fis in by_zone_fi.items():
            if len(zone_cands[zid]) >= per_zone_target:
                continue
            for fi in fis:
                if len(zone_cands[zid]) >= per_zone_target:
                    break
                a, b, c = world[tri[fi, 0]], world[tri[fi, 1]], world[tri[fi, 2]]
                n = np.cross(b - a, c - a)
                nn = np.linalg.norm(n)
                if nn < 1e-12:
                    continue
                n = n / nn
                centroid = (a + b + c) / 3.0
                # 内法线校正：离开模型中轴（x,z 距离增大）= 外法线方向
                outward = np.array([centroid[0], 0.0, centroid[2]])
                ol = np.linalg.norm(outward)
                outward = outward / ol if ol > 1e-9 else np.array([0.0, 0.0, 1.0])
                if np.dot(n, outward) < 0:
                    n = -n
                dirs = [tuple(np.round(n, 4).tolist())] + global_dirs
                zone_cands[zid].append({
                    "mesh": mname, "fi": int(fi),
                    "p": [round(float(v), 4) for v in centroid.tolist()],
                    "dirs": [[round(float(v), 4) for v in d] for d in dirs],
                })
    return sem, zone_cands


# ============ 阶段 2：浏览器内探针点击（每批一次调用） ============
AUDIT_JS = """
(function () {
  try {
    var zones = __BATCH__;
    var out = {};
    // 整数像素网格：中心 + ±1/±2（v23 方法学恢复；click 与探针用同一整数像素）
    var GRID = [[0,0],[1,0],[-1,0],[0,1],[0,-1],[1,1],[-1,1],[1,-1],[-1,-1],
                [2,0],[-2,0],[0,2],[0,-2]];
    function raycastAt(cx, cy) {
      var rect = renderer.domElement.getBoundingClientRect();
      var pointer = new THREE.Vector2(
        ((cx - rect.left) / rect.width) * 2 - 1,
        -((cy - rect.top) / rect.height) * 2 + 1
      );
      raycaster.setFromCamera(pointer, camera);
      var hits = modelRoot ? raycaster.intersectObject(modelRoot, true).filter(function (h) {
        return h.object && h.object.isMesh && !h.object.userData.noPick;
      }) : [];
      return hits.length ? hits[0] : null;
    }
    function zoneAt(h) {
      if (!h) return null;
      var fs = faceSemFor(recordHit(h));
      return fs ? fs.id : null;
    }
    function attempt(a, zoneId) {
      var D = 1.6;
      var gotSeen = {};
      /* 关键：先清选区再进入相机/像素计算。点击成功后 regionLabel 文字变长会使
       * toolbar 换行长高（~16.5px），canvas rect.top 随之下移；若在探针算像素之后
       * 才 clearSelection，onPick 里现读的 rect 与探针所用 rect 不同 → 射线整体
       * 偏移 ~16.5px 命中邻面（假阴性）。此处先复位布局，探针与 click 之间无任何
       * DOM 变动，两者读到的 rect 恒一致。 */
      clearSelection();
      for (var di = 0; di < a.dirs.length; di++) {
        var n = a.dirs[di];
        camera.position.set(a.p[0]+n[0]*D, a.p[1]+n[1]*D, a.p[2]+n[2]*D);
        controls.target.set(a.p[0], a.p[1], a.p[2]);
        controls.update();
        renderer.render(scene, camera);
        var v = new THREE.Vector3(a.p[0], a.p[1], a.p[2]).project(camera);
        var rect = renderer.domElement.getBoundingClientRect();
        var bx = Math.round(rect.left + (v.x + 1) / 2 * rect.width);
        var by = Math.round(rect.top + (1 - (v.y + 1) / 2) * rect.height);
        for (var gi = 0; gi < GRID.length; gi++) {
          var cx = bx + GRID[gi][0], cy = by + GRID[gi][1];
          var h = raycastAt(cx, cy);
          var z = zoneAt(h);
          if (z) gotSeen[z] = (gotSeen[z] || 0) + 1;
          if (z !== zoneId) continue;
          // 同一整数像素派发真实 click（与产品 onPick 完全同链；此处无 DOM 变动，
          // onPick 现读 rect 与探针一致）。点击会新增记录，取末条断言。
          renderer.domElement.dispatchEvent(new MouseEvent("click",
            { clientX: cx, clientY: cy, bubbles: true }));
          var rec = selectedRegions[selectedRegions.length - 1] || null;
          if (rec && rec.id === "fsem:" + zoneId)
            return { ok: true, id: rec.id, label: rec.label, desc: rec.desc,
                     conf: rec.confidence, prov: rec.source, dir: di, px: GRID[gi] };
          // 同像素同射线结果不同 = 异常（不应发生），记录后继续网格
        }
      }
      var best = null, bestN = 0;
      for (var k in gotSeen) if (gotSeen[k] > bestN) { best = k; bestN = gotSeen[k]; }
      return { ok: false, why: best ? "unreachable" : "never-hit", got: best,
               gotSeen: gotSeen, mesh: a.mesh, fi: a.fi };
    }
    for (var zi = 0; zi < zones.length; zi++) {
      var zoneId = zones[zi].zone;
      var res = null, lastFail = null;
      for (var ai = 0; ai < zones[zi].attempts.length; ai++) {
        res = attempt(zones[zi].attempts[ai], zoneId);
        if (res && res.ok) { res.try = ai; break; }
        if (res) lastFail = res;
      }
      out[zoneId] = (res && res.ok) ? res : (lastFail || { ok: false, why: "no-attempt" });
    }
    return JSON.stringify({ ok: true, results: out });
  } catch (e) {
    return JSON.stringify({ ok: false, err: String(e && e.message || e) });
  }
})()
"""

BATCH_SIZE = 25


def run_js_sync(page, js, timeout_ms=600000):
    loop = QEventLoop()
    holder = {}

    def cb(v):
        if not loop.isRunning():
            return
        holder["v"] = v
        loop.quit()

    page.runJavaScript(js, cb)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    return holder.get("v")


def main():
    max_per_zone = 4
    if "--max-per-zone" in sys.argv:
        max_per_zone = int(sys.argv[sys.argv.index("--max-per-zone") + 1])

    print(f"[audit] 构建 GLB 候选面（max_per_zone={max_per_zone}）…", flush=True)
    sem, zone_cands = build_candidates(max_per_zone)
    cands_js = []
    for zid, cands in sorted(zone_cands.items()):
        if not zid:
            continue
        attempts = cands[:max_per_zone]
        if not attempts:
            continue
        cands_js.append({"zone": zid, "attempts": attempts})
    print(f"[audit] zones={len(cands_js)} 候选总数={sum(len(z['attempts']) for z in cands_js)}", flush=True)

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication([])
    view = QWebEngineView()
    view.resize(1100, 900)
    page = DebugPage(view)
    view.setPage(page)

    result = {"ok": False, "reason": "timeout"}

    def finish(code):
        nonlocal result
        report_path = os.path.join(HERE, "data", "audit_zones_report.json")
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=1)
        app.exit(code)

    def on_loaded(ok):
        if not ok:
            result.update({"ok": False, "reason": "load failed"})
            finish(3)
            return

        def check_ready():
            # 就绪条件：modelRoot 已装配 + 语义表已启用（glbLoaded 变量不存在，勿用）
            page.runJavaScript(
                "typeof modelRoot !== 'undefined' && modelRoot !== null && FACE_SEM_ACTIVE === true",
                on_ready)

        def on_ready(v):
            if v is True:
                QTimer.singleShot(800, run_batches)
            else:
                print("[audit] 模型/语义表未就绪，2s 后重试", flush=True)
                QTimer.singleShot(2000, check_ready)

        QTimer.singleShot(1500, check_ready)

    def run_batches():
        all_results = {}
        batches = [cands_js[i:i + BATCH_SIZE] for i in range(0, len(cands_js), BATCH_SIZE)]
        for bi, batch in enumerate(batches):
            js = AUDIT_JS.replace("__BATCH__", json.dumps(batch))
            payload = run_js_sync(page, js)
            try:
                data = json.loads(payload) if isinstance(payload, str) else {}
            except Exception as exc:
                print(f"[audit-FAIL] 批次 {bi} JSON 解析失败: {exc}", flush=True)
                break
            if not data.get("ok"):
                print(f"[audit-FAIL] 批次 {bi} 出错: {data}", flush=True)
                break
            all_results.update(data.get("results", {}))
            print(f"[audit] 批次 {bi + 1}/{len(batches)} 完成（累计 {len(all_results)} zone）", flush=True)
        if not all_results:
            result.update({"ok": False, "reason": "no results"})
            finish(3)
            return
        fails, unreachable, passes = [], [], 0
        for zid, r in sorted(all_results.items()):
            if r and r.get("ok") and r.get("id") == f"fsem:{zid}":
                passes += 1
            elif (r or {}).get("why") in ("unreachable", "never-hit"):
                unreachable.append((zid, r))
            else:
                fails.append((zid, r))
        print(f"\n[audit] PASS {passes}/{len(all_results)}", flush=True)
        if fails:
            print("---- 异常（同像素探针/点击结果不一致，需排查） ----", flush=True)
            for zid, r in fails:
                brief = {k: v for k, v in (r or {}).items() if k in ("why", "got", "try", "mesh", "fi")}
                print(f"  ANOM {zid:32s} {brief}", flush=True)
        print("---- UNREACHABLE（候选均未命中目标区；待区分覆盖层遮挡 vs 真边界缺陷） ----", flush=True)
        for zid, r in unreachable:
            brief = {k: v for k, v in (r or {}).items() if k in ("why", "got", "gotSeen")}
            print(f"  UNREACH {zid:32s} {brief}", flush=True)
        result.update({"ok": True, "results": all_results,
                       "summary": {"pass": passes, "total": len(all_results),
                                   "anomaly": len(fails), "unreachable": len(unreachable)}})
        finish(0 if not fails and not unreachable else 4)

    view.loadFinished.connect(on_loaded)
    view.load(QUrl.fromLocalFile(os.path.join(HERE, "web", "index.html")))
    QTimer.singleShot(3600000, lambda: finish(2))
    view.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
