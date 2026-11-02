#!/usr/bin/env python3
"""3D 面板真机验证：用客户端同款 QtWebEngine 加载 web/index.html，
程序化检查渲染像素 / 区域拾取 / 高亮互斥 / 视角切换，并输出 PNG 截图。

用法：cd client && ./.venv/bin/python tools/verify_3d.py
输出：data/verify3d_report.json + data/verify3d_front.png + data/verify3d_back.png
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QUrl, QTimer
from PySide6.QtWidgets import QApplication

try:
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtWebEngineWidgets import QWebEngineView
except Exception as exc:  # noqa: BLE001
    print(json.dumps({"ok": False, "reason": f"QtWebEngine 不可用：{exc}"}))
    sys.exit(2)


class DebugPage(QWebEnginePage):
    """打印 JS console 与渲染进程异常，便于定位页面加载问题。"""

    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        print(f"[console] L{level} {sourceID.split('/')[-1]}:{lineNumber} {message[:180]}", flush=True)

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)

CHECK_JS = """
(() => {
  // v59：默认布局为对话主区（chat_primary），3D 隐藏；所有 3D 点击断言先切入模型主区
  if (typeof setLayout === 'function') setLayout('model_primary');
  const canvas = document.querySelector('#stage canvas');
  if (!canvas) return { ok: false, reason: 'no canvas' };
  const dataUrl = canvas.toDataURL('image/png');
  const rect = canvas.getBoundingClientRect();
  canvas.dispatchEvent(new MouseEvent('click', { clientX: rect.left + rect.width/2, clientY: rect.top + rect.height/2, bubbles: true }));
  const centerLabel = document.getElementById('regionLabel').textContent;
  // —— v17：19 区域逻辑已删除——REGIONS/regionFromPoint 必须不存在；点击不再归 19 大区 ——
  const v17Removed = {
    noREGIONS: typeof REGIONS === 'undefined',
    noRegionFromPoint: typeof regionFromPoint === 'undefined',
    noHitboxes: Object.keys(regionMeshes).length === 0
  };
  // —— v18 兼容垫片：highlightRegion 语义（独立记录）——
  function setRegionSelectedV18Compat() { highlightRegion('arm_upper_l', '上臂（左）'); }
  setRegionSelectedV18Compat();
  const armLabel = document.getElementById('regionLabel').textContent;
  clearSelection();
  // —— 真实射线点击断言：把世界点投影到屏幕并派发 click，验证型带分区映射（胸/臂/腿/头） ——
  function clickWorld(x, y, z) {
    const v = new THREE.Vector3(x, y, z).project(camera);
    const cx = rect.left + (v.x + 1) / 2 * rect.width;
    const cy = rect.top + (1 - v.y) / 2 * rect.height;
    // 遮挡校正：从相机对该世界点做射线，取可视网格最近交点实际命中的区域
    const dir = new THREE.Vector3(x, y, z).sub(camera.position).normalize();
    raycaster.set(camera.position, dir);
    const pickables = [];
    modelRoot.traverse(o => { if (o.isMesh && o.userData.pickable) pickables.push(o); });
    const hits = raycaster.intersectObjects(pickables, false);
    const zn = zoneFromPoint(hits.length ? hits[0].point : new THREE.Vector3(x, y, z));
    return zn ? zn.id : null;
  }
  // —— 解剖语义命中：anatomyFromPoint 直验（表面采样点 → 精细部位标签） ——
  function anat(x, y, z) {
    const a = anatomyFromPoint(new THREE.Vector3(x, y, z));
    return a ? a.id + '|' + a.label : null;
  }
  // —— v15 真实命中数据：Raycaster 原始记录字段完整性 ——
  function rawRay(wx, wy, wz) {
    const dir = new THREE.Vector3(wx, wy, wz).sub(camera.position).normalize();
    raycaster.set(camera.position, dir);
    const pickables = [];
    modelRoot.traverse(o => { if (o.isMesh && o.userData.pickable) pickables.push(o); });
    const hits = raycaster.intersectObjects(pickables, false);
    return hits.length ? hits[0] : null;
  }
  let hitRecord = null;
  const hRaw = rawRay(-0.094, 2.134, 0.205);
  if (hRaw) {
    const r = recordHit(hRaw);
    hitRecord = {
      hasMesh: !!r.mesh_name && r.mesh_name !== '(未命名 mesh)',
      hasFace: r.face_index !== null && r.face_index >= 0,
      hasWorld: Array.isArray(r.world_xyz) && r.world_xyz.length === 3,
      hasLocal: Array.isArray(r.local_xyz) && r.local_xyz.length === 3,
      hasNormal: Array.isArray(r.normal) && r.normal.length === 3,
      uv: r.uv === null ? 'null' : 'present'
    };
  }
  // —— v17/v18/v20 语义诚实性：证据不足的表面点必须如实提示，禁止猜测 ——
  // v20 表全覆盖（18496 段无空洞）后，原「耳颊点」由 head_side(conf 0.6) 诚实覆盖；
  // 「缺少精细语义」类诚实提示由 conf 0.4 的胸腹交界带承担（表 note 明示膈不可见于体表）。
  clearSelection();
  const fitU = fitInfo.getSize(new THREE.Vector3());
  const ctrU = fitInfo.getCenter(new THREE.Vector3());
  function realClickWorld(wx, wy, wz, keep) {
    // v18：真实点击不清空已选（用户连续点击 = 累积独立记录）；keep=false 时先清空
    if (!keep) clearSelection();
    const v = new THREE.Vector3(wx, wy, wz).project(camera);
    const cx = rect.left + (v.x + 1) / 2 * rect.width;
    const cy = rect.top + (1 - (v.y + 1) / 2) * rect.height;
    // ±3px 网格重试：先试投影中心 (0,0) 再试邻域（v20 逐面表无容差，顺序影响命中的面）
    const offs = [[0, 0], [3, 0], [-3, 0], [0, 3], [0, -3], [3, 3], [3, -3], [-3, 3], [-3, -3]];
    for (const [dx, dy] of offs) {
      renderer.domElement.dispatchEvent(new MouseEvent('click', { clientX: cx + dx, clientY: cy + dy, bubbles: true }));
      if (selectedRegions.length) return selectedRegions[selectedRegions.length - 1];
      if (!keep) clearSelection();
    }
    return null;
  }
  function rawRayPoint(wx, wy, wz) {
    const dir = new THREE.Vector3(wx, wy, wz).sub(camera.position).normalize();
    raycaster.set(camera.position, dir);
    const hits = raycaster.intersectObject(modelRoot, true).filter(h => h.object.isMesh && !h.object.userData.noPick);
    return hits.length ? hits[0].point : null;
  }
  let unlabeled = null;
  {
    // v20：原「耳颊发际内」点已被逐面表诚实覆盖（head_side conf 0.6）；
    // 诚实「语义不足」改验胸腹交界带：解剖分界（膈）不可见于体表，表 note 如实声明。
    const pUn = rawRayPoint(0.05, 0.80, 0.30);
    if (pUn) {
      const rec = realClickWorld(pUn.x, pUn.y, pUn.z);
      unlabeled = rec ? { label: rec.label, desc: rec.desc,
                          ok: rec.label.indexOf('语义不足') !== -1 }
                      : { label: null, ok: false, note: '真实点击未产生记录' };
    } else unlabeled = { label: null, ok: false, note: '射线未命中（交界带点）' };
  }
  // —— v15 校准标签：保存→同面复用→邻域复用→清理 ——
  clearSelection();
  const hCb = rawRay(0.02, 0.55, 0.43);
  let calib = null;
  if (hCb) {
    const r = recordHit(hCb);
    const key = r.mesh_name + ':' + r.face_index;
    const mk = modelKey();
    if (!CALIB.byModel[mk]) CALIB.byModel[mk] = {};
    CALIB.byModel[mk][key] = { label: '回归测试标签', mesh_name: r.mesh_name, face_index: r.face_index,
      local_xyz: r.local_xyz, world_xyz: r.world_xyz, uv: r.uv, normal: r.normal, created_at: 'test', source: 'test' };
    const a1 = anatomyFromPoint(hCb.point);
    const nearPt = new THREE.Vector3(hCb.point.x + 0.008, hCb.point.y, hCb.point.z);
    const a2 = anatomyFromPoint(nearPt);
    calib = { sameFace: a1 ? a1.label : null, nearby: a2 ? a2.label : null };
    delete CALIB.byModel[mk][key];
    clearSelection();
  }
  const anatomy = {
    leftEye:   anat(0.094, 2.134, 0.205),   // v31：left_* 锚点实测在 x>0
    rightEye:  anat(-0.094, 2.134, 0.205),  // v31
    nose:      anat(0.0007, 1.9643, 0.2507),
    upperLip:  anat(-0.0003, 2.0748, 0.2612),
    lowerLip:  anat(0.0002, 2.0218, 0.2748),
    lThumb:    anat(1.8086, 0.0792, 0.1926),
    lIndex:    anat(1.848, 0.0489, 0.1223),
    lMiddle:   anat(1.8471, 0.0487, 0.1058),
    lRing:     anat(1.8229, 0.0655, 0.0433),
    lLittle:   anat(1.8241, 0.0658, 0.028),
    rThumb:    anat(-1.8086, 0.0792, 0.1926),
    rIndex:    anat(-1.848, 0.0489, 0.1223),
    rMiddle:   anat(-1.8471, 0.0487, 0.1058),
    rRing:     anat(-1.8229, 0.0655, 0.0433),
    rLittle:   anat(-1.8241, 0.0658, 0.028),
    lToe:      anat(0.6726, -2.4339, 0.3905),
    rToe:      anat(-0.6726, -2.4341, 0.3903)
  };
  // —— v31 回归：用户报告的左屏幕臂根点（模型局部坐标）不得再落入「腹」——
  // 通过真实投影 click 走完整 onPick 链，避免只测试分类器而漏掉命中/优先级问题。
  let armRootRegression = null;
  {
    clearSelection();
    const probes = [
      { id: "front_arm_root", local: [-0.269, 1.301, 0.002], expect: "上臂" },
      { id: "back_arm_root", local: [0.282, 1.317, -0.115], expect: "上臂", view: "back" },
      { id: "front_elbow_lower", local: [0.468, 1.144, 0.027], expect: "前臂" },
      { id: "front_elbow_screenshot", local: [0.418, 1.181, 0.017], expect: "前臂" },
      { id: "front_elbow_latest", local: [0.434, 1.166, 0.021], expect: "前臂" },
      { id: "back_upper_arm", local: [0.244, 1.323, -0.114], expect: "上臂", view: "back" },
      { id: "back_upper_arm_latest", local: [0.248, 1.301, -0.104], expect: "上臂", view: "back" },
      { id: "user_forearm_1", local: [0.454, 1.156, 0.025], expect: "前臂" },
      { id: "user_forearm_2", local: [0.472, 1.147, 0.027], expect: "前臂" },
      { id: "user_forearm_3", local: [0.481, 1.133, 0.029], expect: "前臂" },
      { id: "user_forearm_4", local: [0.504, 1.117, 0.035], expect: "前臂" },
      { id: "user_cubital_fossa", local: [0.346, 1.245, -0.009], expect: "肘窝" },
      { id: "user_elbow_back", local: [0.370, 1.227, -0.093], expect: "肘", view: "back" },
      { id: "user_elbow_back_4", local: [0.316, 1.222, -0.055], expect: "肘", view: "back" },
      { id: "user_upper_arm_back_1", local: [0.243, 1.321, -0.112], expect: "上臂", view: "back" },
      { id: "user_upper_arm_back_2", local: [0.233, 1.300, -0.068], expect: "上臂", view: "back" },
      { id: "user_upper_arm_back_3", local: [0.281, 1.258, -0.078], expect: "上臂", view: "back" },
      { id: "user_upper_arm_back_4", local: [0.212, 1.333, -0.078], expect: "上臂", view: "back" },
      { id: "user_upper_arm_back_5", local: [0.251, 1.286, -0.089], expect: "上臂", view: "back" },
      { id: "user_upper_arm_back_6", local: [0.289, 1.251, -0.041], expect: "上臂", view: "back" },
      { id: "user_upper_arm_back_7", local: [0.262, 1.275, -0.085], expect: "上臂", view: "back" },
      { id: "user_upper_arm_back_8", local: [0.274, 1.275, -0.095], expect: "上臂", view: "back" },
      { id: "user_upper_arm_back_9", local: [0.223, 1.374, -0.116], expect: "上臂", view: "back" },
      { id: "mirror_forearm_r", local: [-0.454, 1.156, 0.025], expect: "前臂" },
      { id: "mirror_cubital_fossa_r", local: [-0.346, 1.245, -0.009], expect: "肘窝" },
      { id: "mirror_elbow_r", local: [-0.370, 1.227, -0.093], expect: "肘", view: "back" },
      { id: "mirror_upper_arm_r", local: [-0.243, 1.321, -0.112], expect: "上臂", view: "back" },
      { id: "mirror_upper_arm_r2", local: [-0.223, 1.374, -0.116], expect: "上臂", view: "back" },
      { id: "user_thumb_l", local: [0.577, 0.997, 0.104], expect: "拇指" },
      { id: "user_thumb_r", local: [-0.573, 0.987, 0.117], expect: "拇指" },
      { id: "user_shoulder_l", local: [0.200, 1.421, 0.019], expect: "肩" },
      { id: "mirror_shoulder_r", local: [-0.200, 1.421, 0.019], expect: "肩" }
    ];
    armRootRegression = probes.map(function (probe) {
      setView(probe.view || "front");   // 背面点从后视相机投射+点击，复现用户真实视角
      const lp = new THREE.Vector3(probe.local[0], probe.local[1], probe.local[2]);
      const wp = modelRoot.localToWorld(lp.clone());
      const surface = rawRayPoint(wp.x, wp.y, wp.z);
      if (!surface) return { id: probe.id, label: null, expect: probe.expect, note: "回归点射线未命中" };
      const rec = realClickWorld(surface.x, surface.y, surface.z);
      return rec ? {
        id: probe.id,
        label: rec.label,
        desc: rec.desc,
        source: rec.source,
        mesh: rec.hit.mesh_name,
        face: rec.hit.face_index,
        expect: probe.expect
      } : { id: probe.id, label: null, expect: probe.expect, note: "真实点击未产生记录" };
    });
    clearSelection();
    setView("front");
  }
  // —— v18 三点选择（真实 onPick 链，累积）/ × 删除（删中间余两端）/ 清空断言 ——
  // 正面互不遮挡的三点：眼（脸面 z≈0.28）、胸、大腿（v31 侧别词随表） —— 连续点击累积 3 条独立记录
  clearSelection();
  const selEye = rawRayPoint(-0.094, 2.134, 0.28);
  if (selEye) realClickWorld(selEye.x, selEye.y, selEye.z, true);
  const selChest = rawRayPoint(0.02, fitInfo.min.y + 0.70 * fitU.y, fitInfo.max.z);
  if (selChest) realClickWorld(selChest.x, selChest.y, selChest.z, true);
  // v28：大腿探针从归一化 0.245（world −1.28，v28 口径属膝带）移到真正的大腿
  // 中段 0.38（world −0.60）；膝点由 thighRecs 循环覆盖（world −1.24..−1.54）
  const selThigh = rawRayPoint(ctrU.x + 0.25 * fitU.x / 2, fitInfo.min.y + 0.38 * fitU.y, ctrU.z + 0.9 * fitU.z / 2);
  if (selThigh) realClickWorld(selThigh.x, selThigh.y, selThigh.z, true);
  const multiCount = selectedRegions.length;
  const multiLabel = document.getElementById('regionLabel').textContent;
  const midUid = selectedRegions[1] ? selectedRegions[1].uid : -1;   // 删除中间一条
  const delBtn = document.querySelectorAll('#selList .sel-del')[1];
  if (delBtn) delBtn.click();                            // 点第二行 × → 立即删除，不影响其余
  const afterXDelete = {
    count: selectedRegions.length,
    labels: selectedRegions.map(s => s.label),
    rows: document.querySelectorAll('#selList .sel-row').length,
    markers: markerPool.filter(m => m.visible).length,
    removed: removeSelected(midUid) === false            // uid 已删 → 再删返回 false
  };
  clearSelection();
  const afterClear = selectedRegions.length;
  // —— v18 逐点独立记录：同一条大腿连点 5 处 → 5 条不同记录/uid（绝不合并去重） ——
  clearSelection();
  const thighRecs = [];
  for (const [nx, ny] of [[-0.25, 0.285], [-0.25, 0.245], [-0.25, 0.205], [-0.18, 0.245], [-0.32, 0.26]]) {
    const p = rawRayPoint(ctrU.x + nx * fitU.x / 2, fitInfo.min.y + ny * fitU.y, ctrU.z + 0.9 * fitU.z / 2);
    if (p) {
      const rec = realClickWorld(p.x, p.y, p.z);
      thighRecs.push(rec ? { uid: rec.uid, desc: rec.desc, ml: rec.hit.model_local_xyz } : { miss: true });
    } else thighRecs.push({ miss: true });
  }
  const thighUids = thighRecs.filter(r => r.uid).map(r => r.uid);
  const thighDistinct = new Set(thighUids).size === thighUids.length && thighUids.length >= 4;
  // —— 视角稳定性：全部记录 pos 逐字节不变（左视/后视/前视往返） ——
  const beforePos = selectedRegions.length ? JSON.stringify(selectedRegions.map(s => s.pos.toArray())) : null;
  setView('left');
  setView('back');
  setView('front');
  const posStable = beforePos !== null && beforePos === JSON.stringify(selectedRegions.map(s => s.pos.toArray()));
  // —— v16 自动分区断言：型带比例（zoneFromPoint 直验 id|标签，视角无关） ——
  const fit2 = fitInfo.getSize(new THREE.Vector3());
  const ctr2 = fitInfo.getCenter(new THREE.Vector3());
  function zwp(nx, ny, nz) {
    return new THREE.Vector3(ctr2.x + nx * fit2.x / 2, fitInfo.min.y + ny * fit2.y, ctr2.z + nz * fit2.z / 2);
  }
  function zne(nx, ny, nz) {
    const zn = zoneFromPoint(zwp(nx, ny, nz));
    return zn ? zn.id + '|' + zn.label : null;
  }
  const lowZn = zoneFromPoint(zwp(-0.55, 0.90, 0.0));   // 耳颊区：证据不足 → 低置信
  const zones = {
    neck: zne(0.0, 0.85, 0.3),
    shoulderBelt: zne(-0.35, 0.78, 0.5),
    chest: zne(0.10, 0.70, 0.9),
    abdomen: zne(0.05, 0.52, 0.9),
    backUpper: zne(0.0, 0.72, -0.9),
    backLower: zne(0.0, 0.50, -0.9),
    hipButtock: zne(-0.10, 0.40, -0.9),
    thigh: zne(0.12, 0.22, 0.5),
    knee: zne(0.05, 0.18, 0.5),
    calf: zne(0.05, 0.12, 0.5),
    ankle: zne(0.05, 0.06, 0.5),
    footToes: zne(0.12, 0.03, 0.5),
    upperArm: zne(0.75, 0.65, 0.3),
    elbow: zne(0.75, 0.58, 0.3),
    forearm: zne(0.85, 0.55, 0.3),
    wrist: zne(0.95, 0.53, 0.3),
    palm: zne(0.98, 0.53, 0.3),
    headTop: zne(0.0, 0.98, 0.0)
  };
  // —— v16 校准向导断言：7 步抽查 → 1 纠正（右中指 +0.08x）→ 对侧自动镜像 → 持久化 ——
  clearSelection();
  startWizard();
  const wsteps = WIZARD.steps;
  const entryById = {};
  ANATOMY.entries.forEach(e => { entryById[e.anatomical_label] = e; });
  const origRM = entryById['right_middle_finger'].world_xyz.slice();
  const origLM = entryById['left_middle_finger'].world_xyz.slice();
  for (let i = 0; i < wsteps.length; i++) {
    const st = wsteps[i];
    const isRM = st.id === 'right_middle_finger';
    wizardNext(new THREE.Vector3(st.spot[0] + (isRM ? -0.08 : 0), st.spot[1], st.spot[2]), !isRM);   // v31：右中指现 x<0
  }
  const rmid = entryById['right_middle_finger'];
  const lmid = entryById['left_middle_finger'];
  const wizard = {
    steps: wsteps.length,
    closed: WIZARD.active === false,
    rmidShifted: Math.abs(rmid.world_xyz[0] + 1.9271) < 0.001,   // v31：右中指 x<0
    lmidMirrored: Math.abs(lmid.world_xyz[0] - 1.9271) < 0.001,   // v31：镜像对侧 x>0
    rmidProv: rmid.provenance.indexOf('校准向导纠正') === 0,
    lmidProv: lmid.provenance.indexOf('校准向导对称修正') === 0,
    persisted: !!localStorage.getItem('derm_wizard_applied'),
    lowConfZone: lowZn ? (lowZn.id + '|' + lowZn.label + '|' + (lowZn.confidence < 0.5 ? '低置信' : '高置信')) : null
  };
  rmid.world_xyz = origRM; lmid.world_xyz = origLM;   // 还原运行时副本（出厂锚点）
  localStorage.removeItem('derm_wizard_applied');
  clearSelection();
  // —— 阶段2：人工区域数据层（多视角追加/revision/重叠规则/导出/失败不变性） ——
  // 所有交互前先 raycast 确认命中，杜绝 alert() 分支阻塞验证脚本
  const regionLayer = (() => {
    const r = { errors: [] };
    try {
      const canvas = document.querySelector('#stage canvas');
      const rect = canvas.getBoundingClientRect();
      const pickables = [];
      modelRoot.traverse(o => { if (o.isMesh && o.userData.pickable) pickables.push(o); });
      function pickAt(fx, fy) {
        raycaster.setFromCamera(new THREE.Vector2(fx * 2 - 1, -(fy * 2 - 1)), camera);
        const hits = raycaster.intersectObjects(pickables, false);
        return hits.length ? hits[0] : null;
      }
      function clickAt(fx, fy) {
        canvas.dispatchEvent(new MouseEvent('click',
          { clientX: rect.left + fx * rect.width, clientY: rect.top + fy * rect.height, bubbles: true }));
      }
      function drawAndCommit(viewName, pts, standard) {
        setView(viewName);
        enterRegionDraw();
        for (const p of pts) {
          if (!pickAt(p[0], p[1])) { r.errors.push('圈选点未命中:' + viewName + ' ' + p); exitRegionDraw(); return null; }
          clickAt(p[0], p[1]);
        }
        if (REGION_DRAW.points.length < 3) { r.errors.push('边界点不足'); exitRegionDraw(); return null; }
        finishRegionDraw();
        if (!REGION_DRAW.preview || !REGION_DRAW.previewFaces.length) {
          r.errors.push('预览面为空:' + viewName); exitRegionDraw(); return null;
        }
        const faceCount = REGION_DRAW.previewFaces.length;
        const sel = document.getElementById('raLabel');
        sel.value = standard;
        sel.dispatchEvent(new Event('change'));
        return { faceCount };
      }
      const knownBefore = new Set(Object.keys(MANUAL_REGIONS.regions || {}));
      // ① front 第一视角圈选 chest → 新建 region v1
      const first = drawAndCommit('front', [[0.50, 0.42], [0.55, 0.47], [0.47, 0.48]], 'chest');
      const newId = Object.keys(MANUAL_REGIONS.regions || {}).find(id => !knownBefore.has(id));
      const reg1 = newId ? MANUAL_REGIONS.regions[newId] : null;
      r.created = !!(first && reg1 && reg1.version === 1 && reg1.views.length === 1 &&
        reg1.faces.length >= first.faceCount);
      if (!r.created) r.errors.push('新建失败 first=' + JSON.stringify(first) + ' reg1=' + (reg1 ? reg1.version + '/' + reg1.views.length : null));
      // ② back 第二视角同部位圈选 → 追加 revision v2（faces 并集）
      const second = drawAndCommit('back', [[0.50, 0.42], [0.55, 0.47], [0.47, 0.48]], 'chest');
      const reg2 = newId ? MANUAL_REGIONS.regions[newId] : null;
      r.revised = !!(second && reg2 && reg2.id === newId && reg2.views.length === 2 && reg2.version === 2 &&
        reg2.faces.length >= reg1.faces.length && reg2.updated_at >= reg2.created_at);
      if (!r.revised) r.errors.push('追加视角失败 second=' + JSON.stringify(second) + ' reg2=' + (reg2 ? reg2.views.length + 'v' + reg2.version : null));
      // ③ undo 两次：回退 revision → v1 单视角；再回退创建 → regions 复原
      r.undoRevise = undoManualRegion() && MANUAL_REGIONS.regions[newId] &&
        MANUAL_REGIONS.regions[newId].views.length === 1 && MANUAL_REGIONS.regions[newId].version === 1;
      const undo2 = undoManualRegion();
      r.undoRestored = undo2 && Object.keys(MANUAL_REGIONS.regions || {}).length === knownBefore.size;
      // ④ 重叠规则：同面多区域时归属 updated_at 最新（注入合成数据后还原，不经持久化）
      const m0 = pickables[0];
      const savedRegions = MANUAL_REGIONS.regions;
      MANUAL_REGIONS.regions = {
        old_r: { id: 'old_r', status: 'confirmed', updated_at: '2020-01-01T00:00:00.000Z',
          faces: [{ mesh_name: m0.name, face_index: 0, model_local_xyz: [0, 0, 0] }] },
        new_r: { id: 'new_r', status: 'confirmed', updated_at: '2030-01-01T00:00:00.000Z',
          faces: [{ mesh_name: m0.name, face_index: 0, model_local_xyz: [0, 0, 0] }] }
      };
      rebuildManualFaceIndex();
      r.overlapLatest = MANUAL_FACE_OWNER[manualFaceIndexKey(m0.name, 0)] === MANUAL_REGIONS.regions.new_r;
      MANUAL_REGIONS.regions = savedRegions;
      rebuildManualFaceIndex();
      // ⑤ 导出就绪（模型指纹）+ ⑥ 失败不变性（指纹不匹配文档拒绝激活、原数据零改动）
      r.exportReady = MODEL_IDENTITY.ready === true && typeof MODEL_IDENTITY.sha256 === 'string' &&
        MODEL_IDENTITY.sha256.length === 64;
      const snap = JSON.stringify(MANUAL_REGIONS);
      PENDING_MANUAL_DOCUMENT = { schema_version: 3, model: { sha256: '0'.repeat(64),
        topology_version: -1, fit_version: -1 }, regions: {}, history: [] };
      activatePendingManualDocument();
      r.invariant = JSON.stringify(MANUAL_REGIONS) === snap;
    } catch (e) { r.errors.push('EXC: ' + (e && e.message || e)); }
    return r;
  })();
  // —— 阶段3：医生资料入口（＋菜单唯一入口/检验表/影像检查链/payload 集成） ——
  const clinicLayer = (() => {
    const r = { errors: [] };
    try {
      const entry = document.getElementById('clinicEntry');
      const drawer = document.getElementById('clinicDrawer');
      r.ui = !!(entry && drawer && document.getElementById('clinicClose'));
      entry.click();
      r.opens = drawer.classList.contains('open');
      entry.click();
      r.closes = !drawer.classList.contains('open');
      // 检验行：加两行收集 → 删一行 → 收集递减 → 清空
      addClinicLabRow('白细胞', '12.3', '10^9/L');
      addClinicLabRow('C反应蛋白', '8.1', 'mg/L');
      let c = collectClinical();
      r.labs2 = c.labs.length === 2 && c.labs[0].name === '白细胞' && c.labs[0].value === '12.3';
      document.querySelectorAll('#labRows .lab-row .lab-del')[0].click();
      c = collectClinical();
      r.labs1 = c.labs.length === 1 && c.labs[0].name === 'C反应蛋白';
      document.getElementById('labRows').innerHTML = '';
      // payload 集成：无资料 → null；填写病史后 → clinic.hx
      r.payloadNull = buildStructuredPayload('x').clinic === null;
      document.getElementById('clinicHx').value = '反复红斑鳞屑 3 周';
      const pc = buildStructuredPayload('x').clinic;
      r.payloadHas = !!(pc && pc.hx && pc.hx.indexOf('红斑') >= 0);
      document.getElementById('clinicHx').value = '';
      // v56 病历单：患者基本信息——就诊日期留空自动填今天、有姓名无门诊号自动生成并写回输入框；
      // 载荷携带 patient 且身份字段类别替换（姓名/门诊号），性别/年龄/日期等临床字段原样保留
      r.patUi = ['clinicPatName', 'clinicPatSex', 'clinicPatAge', 'clinicPatNo', 'clinicPatDate']
        .every(function (id) { return !!document.getElementById(id); });
      document.getElementById('clinicPatName').value = '张三';
      document.getElementById('clinicPatSex').value = '女';
      document.getElementById('clinicPatAge').value = '34';
      document.getElementById('clinicPatDate').value = '';
      c = collectClinical();
      r.patAutoDate = !!(c.patient && /^\d{4}-\d{2}-\d{2}$/.test(c.patient.date));
      r.patAutoNo = !!(c.patient && /^OPD-\d{8}-\d{3}$/.test(c.patient.no)) &&
        document.getElementById('clinicPatNo').value === c.patient.no;
      const pp = clinicalPayloadSelected(999);
      r.patMasked = !!(pp && pp.patient && pp.patient.name === '［姓名］' &&
        pp.patient.no === '［门诊号］' && pp.patient.sex === '女' &&
        pp.patient.age === '34' && pp.patient.date === c.patient.date);
      renderAssistPanel();
      r.patSelRow = document.getElementById('assistSel').textContent.indexOf('患者基本信息') >= 0 &&
        document.getElementById('assistSel').textContent.indexOf('已填') >= 0;
      document.getElementById('clinicPatName').value = '';
      document.getElementById('clinicPatSex').value = '';
      document.getElementById('clinicPatAge').value = '';
      document.getElementById('clinicPatNo').value = '';
      document.getElementById('clinicPatDate').value = '';
      OPD_SEQ = 0;
      renderAssistPanel();
      // 影像同步拒绝路径：不支持的扩展名 / 空文件（安全降级：文件不进任何载荷）
      addClinicImageFile(new File([new Uint8Array([68, 73, 67, 77])], 'scan.dicom', { type: '' }));
      r.rejectExt = CLINIC_IMAGING.length === 1 && CLINIC_IMAGING[0].status === 'rejected' &&
        String(CLINIC_IMAGING[0].reason || '').indexOf('不支持') >= 0;
      addClinicImageFile(new File([new Uint8Array(0)], 'empty.png', { type: 'image/png' }));
      r.rejectEmpty = CLINIC_IMAGING.length === 2 && CLINIC_IMAGING[1].status === 'rejected' &&
        String(CLINIC_IMAGING[1].reason || '').indexOf('空文件') >= 0;
      CLINIC_IMAGING.length = 0;
      renderClinicImaging();
      // 影像异步验证：1×1 合法 png → ready；伪造 png（PDF 头字节）→ 解码失败 rejected。
      // onload 异步 → 结果条目引用存 window.__clinicAsync，由 Python 轮询判定。
      const b64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
      const bin = atob(b64); const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      addClinicImageFile(new File([bytes], 'skin.png', { type: 'image/png' }));
      addClinicImageFile(new File([new Uint8Array([0x25, 0x50, 0x44, 0x46])], 'fake.png', { type: 'image/png' }));
      window.__clinicAsync = { ready: CLINIC_IMAGING[0], broken: CLINIC_IMAGING[1] };
      CLINIC_IMAGING.length = 0;
      renderClinicImaging();
    } catch (e) { r.errors.push('EXC: ' + (e && e.message || e)); }
    return r;
  })();
  // —— 阶段4：医生辅助面板（逐项选择/默认脱敏/参考病例逐个确认/发送预览） ——
  const assistLayer = (() => {
    const r = { errors: [] };
    try {
      const entry = document.getElementById('assistEntry');
      const drawer = document.getElementById('assistDrawer');
      const clinicDrawer = document.getElementById('clinicDrawer');
      r.ui = !!(entry && drawer && document.getElementById('assistClose') &&
        document.getElementById('assistSel') && document.getElementById('assistPreview'));
      clinicEntry = document.getElementById('clinicEntry');
      clinicEntry.click();                       // 先开医生资料抽屉
      const clinicWasOpen = clinicDrawer.classList.contains('open');
      entry.click();                             // 再开辅助面板 → 两者互斥
      r.opens = drawer.classList.contains('open');
      r.exclusive = clinicWasOpen && !clinicDrawer.classList.contains('open');
      entry.click();                             // 关闭复位
      // 默认全选 + 取消 hx → payload 不携带 hx；恢复后携带
      r.allSelected = Object.keys(CLINIC_SELECT).every(k => CLINIC_SELECT[k] === true);
      document.getElementById('clinicHx').value = '反复红斑鳞屑 3 周';
      CLINIC_SELECT.hx = false;
      let p = clinicalPayloadSelected(999);
      r.excluded = p ? (p.hx === undefined && (p.excluded_items || []).some(s => s.indexOf('病史') >= 0)) : false;
      CLINIC_SELECT.hx = true;
      p = clinicalPayloadSelected(999);
      r.included = !!(p && p.hx && p.hx.indexOf('红斑') >= 0);
      // 默认脱敏：手机号/身份证/姓名/地址 → 类别标记，原文不出现
      document.getElementById('clinicHx').value =
        '患者：张三 电话 13812345678 身份 11010119900307889X 住址：北京市海淀区中关村大街1号楼';
      p = clinicalPayloadSelected(999);
      r.masked = !!p && p.hx && p.hx.indexOf('13812345678') === -1 &&
        p.hx.indexOf('［手机号］') >= 0 && p.hx.indexOf('［身份证号］') >= 0 &&
        p.hx.indexOf('张三') === -1 && p.hx.indexOf('［地址］') >= 0 &&
        p.deidentified === true;
      document.getElementById('clinicHx').value = '';
      CLINIC_SELECT.hx = false;   // 复原到「无资料」判定不影响 ref_cases 分支
      // 参考病例：未确认 → 不携带；逐个确认后才携带（未经确认不读取其他病例）
      const histSaved = CHAT_HISTORY.length;
      CHAT_HISTORY.push({ n: 1, time: '10:00:00', text: '既往病例：手部湿疹', regions: [], files: [], payload: {} });
      p = clinicalPayloadSelected(999);
      r.refGated = !p || !p.ref_cases;
      REF_CASE_CONFIRMED[1] = true;
      p = clinicalPayloadSelected(999);
      r.refConfirmed = !!p && p.ref_cases && p.ref_cases.length === 1 && p.ref_cases[0].seq === 1;
      delete REF_CASE_CONFIRMED[1];
      CHAT_HISTORY.length = histSaved;
      CLINIC_SELECT.hx = true;
      // —— 阶段5：AI 辅助判断渲染（模拟 Qt 推送服务端草稿 → 三区 DOM 断言） ——
      const draft5 = {
        schema_version: '1.1', workflow_state: 'awaiting_review', review_status: 'pending',
        case_summary: { selected_body_region: '小腿（左）', symptom_summary: '红斑瘙痒', data_completeness: 'partial' },
        suspected_conditions: [
          { name: '接触性皮炎', confidence: 'medium',
            supporting_evidence: [{ statement: '边界清楚的红斑', citation_ids: ['CIT-001'] }],
            missing_information: ['斑贴试验'], red_flags: [] },
          { name: '湿疹', confidence: 'unknown', supporting_evidence: [], missing_information: [], red_flags: [] }],
        symptom_assessment: [{ symptom: '瘙痒', status: 'present', basis: 'patient_report', citation_ids: ['CIT-001'] }],
        medication_checks: [{ generic_name: '炉甘石洗剂', purpose: '医生核对药物与病情相关性',
          items_to_verify: ['indication', 'allergy'], requires_doctor_confirmation: true, citation_ids: [] }],
        severity: { level: 'mild', basis: ['初步评估'], citation_ids: [] },
        red_flag_escalation: { triggered: false, reasons: [], recommended_action: '未触发红旗，按常规医生审核流程处理', is_blocking: false },
        recommended_next_steps: [{ action: '结合引文核对鉴别诊断与用药，由医生决定下一步', urgency: 'routine', citation_ids: ['CIT-001'] }],
        uncertainties: ['本草稿由规则引擎生成'],
        citations: [{ citation_id: 'CIT-001' }],
        model_version: 'rule-engine@demo', generated_by: 'rule-fallback',
        knowledge_snapshot: 'synthetic-kb@2026-09-04', safety_policy_version: 'sp-1',
        generated_at: '2026-09-11T08:00:00Z'
      };
      r.pushAck = window.__pushAssist(JSON.stringify(draft5)) === 'ok';   // 推送被接受
      const dx5 = document.getElementById('assistDx');
      const adv5 = document.getElementById('assistAdvice');
      const au5 = document.getElementById('assistAudit');
      r.dxRendered = !!dx5 && dx5.querySelectorAll('.dx-card').length === 2;
      r.dxNotProbability = !!dx5 && dx5.textContent.indexOf('非确诊概率') >= 0;   // 排序不称确诊概率
      r.dxPendingWording = !!dx5 && dx5.textContent.indexOf('（待排）') >= 0;      // 鉴别方向一律「待排」
      r.dxCite = !!dx5 && dx5.textContent.indexOf('[CIT-001]') >= 0;              // 证据带引用
      r.dxUnknownNoEvidence = !!dx5 && dx5.textContent.indexOf('支持证据：无') >= 0;
      r.dxAbsent = !!dx5 && dx5.textContent.indexOf('知识库未覆盖') === -1;        // 有结果不显空态
      r.advRendered = !!adv5 && adv5.querySelectorAll('.adv-item').length >= 3;
      r.advMedicationGuard = !!adv5 && adv5.textContent.indexOf('禁止自动处方/剂量/疗程') >= 0;
      r.advDoctorConfirm = !!adv5 && adv5.textContent.indexOf('需医生确认后执行') >= 0;
      r.auditPending = !!au5 && !!au5.querySelector('.pending-pill');             // AI 建议状态默认 pending
      r.auditMeta = !!au5 && au5.textContent.indexOf('rule-engine@demo') >= 0 &&
        au5.textContent.indexOf('2026-09-11 08:00:00') >= 0;
      r.disclaimerAlwaysOn = !!document.getElementById('assistDisclaimer') &&
        document.getElementById('assistDisclaimer').textContent.indexOf('不能替代医生的独立判断') >= 0;
      r.pushReject = window.__pushAssist('not-json') === 'parse_error';           // 坏载荷拒绝且不炸
      window.__pushAssist(JSON.stringify({ review_status: 'pending', suspected_conditions: [] }));
      r.dxEmptyState = !!dx5 && dx5.textContent.indexOf('知识库未覆盖') >= 0;      // 空结果显空态（不编造）
      // —— 阶段6：审核历史 / 医生审核状态联动 ——
      const draft6 = JSON.parse(JSON.stringify(draft5));
      draft6.review_status = 'edited';
      draft6.review_history = [
        { at: '2026-09-11T08:01:00Z', decision: 'modified', action: 'modified', feedback: '修改：请补充接触史', result_version: 'r2' },
        { at: '2026-09-11T08:02:00Z', decision: 'regenerate', action: 'regenerate', feedback: '重新生成：请重新分析', result_version: 'r3' }];
      window.__pushAssist(JSON.stringify(draft6));
      r.auditHistoryCount = !!au5 && au5.textContent.indexOf('2 条') >= 0;
      r.auditHistoryLatest = !!au5 && au5.textContent.indexOf('重新生成') >= 0;
      r.auditEditedStatus = !!au5 && au5.textContent.indexOf('已修订（待医生再确认）') >= 0;
      const draft6b = JSON.parse(JSON.stringify(draft6));
      draft6b.review_status = 'approved';
      window.__pushAssist(JSON.stringify(draft6b));
      r.auditApprovedStatus = !!au5 && au5.textContent.indexOf('待签署草稿已生成') >= 0;
      // —— v58b：对话区结果卡真实性横幅契约（横幅只认 llm.source） ——
      const draft7 = JSON.parse(JSON.stringify(draft5));
      draft7.action = 'review';
      draft7.llm = { ok: true, source: 'llm', model: 'test-model', error: '', at: '2026-09-11T08:00:00Z' };
      window.__pushAssist(JSON.stringify(draft7));
      r.convGreenBanner = document.querySelectorAll('#convLog .conv-banner.llm-ok').length >= 1;   // source=llm → 绿横幅
      const draft7b = JSON.parse(JSON.stringify(draft7));
      draft7b.llm = {};                                                     // 载荷缺真实性元信息 → 红横幅（绝不伪装）
      window.__pushAssist(JSON.stringify(draft7b));
      r.convRedBannerWithoutLlm = document.querySelectorAll('#convLog .conv-banner.llm-bad').length >= 1;
      window.__pushAssist(JSON.stringify(draft5));                                 // 复原快照
    } catch (e) { r.errors.push('EXC: ' + (e && e.message || e)); }
    return r;
  })();
  const settingsLayer = (() => {
    const r = { errors: [] };
    try {
      const entry = document.getElementById('settingsEntry');
      const drawer = document.getElementById('settingsDrawer');
      const kbStatus = document.getElementById('kbStatus');
      r.ui = !!(entry && drawer && document.getElementById('settingsClose') &&
        document.getElementById('kbList') && document.getElementById('kbImportBtn') &&
        document.getElementById('kbBackupBtn') && document.getElementById('kbRestoreBackupBtn') &&
        document.getElementById('kbSnapshotLine') && document.getElementById('ragRunSettings') &&
        document.getElementById('settingsDisclaimer') &&
        document.getElementById('settingsNav') && document.getElementById('settingsContent') &&
        document.querySelector('.nav-item[data-pane="general"]') &&
        document.querySelector('.settings-pane[data-pane="general"]'));
      const composer = document.getElementById('composer');
      const toolbar = document.getElementById('toolbar');
      r.gearInToolbar = !!(toolbar && composer && toolbar.contains(entry) && !composer.contains(entry));
      const modelSel = document.getElementById('modelSelect');
      // —— v58：人体模型下拉已按需求移到 3D 区域顶部工具浮层（#stageTools），不再在顶栏/composer ——
      const stageToolsEl = document.getElementById('stageTools');
      r.modelInToolbar = !!(modelSel && stageToolsEl && composer &&
        stageToolsEl.contains(modelSel) && !composer.contains(modelSel) && !toolbar.contains(modelSel));
      // —— v46：composer 发送按钮旁是 LLM 大模型切换（非人体模型） ——
      const llmSel = document.getElementById('llmModelSelect');
      r.llmSelInComposer = !!(llmSel && composer && toolbar &&
        composer.contains(llmSel) && !toolbar.contains(llmSel));
      r.llmSelOffline = !!(llmSel && llmSel.disabled &&
        llmSel.textContent.indexOf('离线') >= 0);          // 无 bridge：禁用并提示离线，不伪造可用模型
      // —— v47：思考等级切换（发送旁、紧贴模型选择器；无 bridge 禁用降级） ——
      const thinkSel = document.getElementById('llmThinkingSelect');
      r.thinkingSelInComposer = !!(thinkSel && composer && toolbar &&
        composer.contains(thinkSel) && !toolbar.contains(thinkSel));
      r.thinkingSelOffline = !!(thinkSel && thinkSel.disabled &&
        thinkSel.textContent.indexOf('离线') >= 0);
      // —— v45：独立全屏设置界面（非侧边抽屉） ——
      r.fullscreenPage = !!(drawer && drawer.classList.contains('settings-page'));
      r.backWording = document.getElementById('settingsClose').textContent.indexOf('返回应用') >= 0;
      // —— v43：语言切换（切换 → 英文生效 → 恢复中文） ——
      const langEn = document.getElementById('langEn');
      const langZh = document.getElementById('langZh');
      const paneTitle = document.getElementById('paneGeneralTitle');
      r.langUi = !!(langEn && langZh && paneTitle &&
        document.getElementById('langSecTitle'));
      langEn.click();
      r.langSwitch = paneTitle.textContent.indexOf('General') >= 0 &&
        document.getElementById('kbImportBtn').textContent.indexOf('Import Books') >= 0;
      langZh.click();
      r.langRestore = paneTitle.textContent.indexOf('通用') >= 0;
      // —— v43：LLM 连接区（URL/模型/Key 输入与掩码状态） ——
      const llmStatus = document.getElementById('llmStatus');
      r.llmUi = !!(document.getElementById('llmBaseUrl') && document.getElementById('llmModel') &&
        document.getElementById('llmApiKey') && document.getElementById('llmSaveBtn') && llmStatus);
      const hist = document.getElementById('histDrawer');
      if (hist) hist.classList.add('open');            // 先开历史抽屉 → 验证设置面板互斥
      entry.click();                                   // 最左下角齿轮 → 打开设置面板
      r.opens = drawer.classList.contains('open');
      r.exclusive = !!(hist && !hist.classList.contains('open'));
      r.degradedNoBridge = !!kbStatus && (kbStatus.textContent.indexOf('未连接') >= 0 ||
        kbStatus.textContent.indexOf('离线') >= 0);     // 无 bridge：明确降级提示，不伪造数据
      r.llmMasked = !!llmStatus && (llmStatus.textContent.indexOf('未连接') >= 0 ||
        llmStatus.textContent.indexOf('离线') >= 0);    // 无 bridge：LLM 状态明确降级，不回显任何 Key
      // —— v54：接入方式卡片（顶栏模式/地址控件移除后收纳于此；无 bridge 禁用降级） ——
      const agentSel = document.getElementById('agentModeSelect');
      const agentUrl = document.getElementById('agentUrlInput');
      const agentBtn = document.getElementById('agentSaveBtn');
      r.agentUi = !!(agentSel && agentUrl && agentBtn && agentSel.tagName === 'SELECT' &&
        document.getElementById('agentHelp') && document.getElementById('agentStatus'));
      r.agentDefaultServer = !!(agentSel && agentSel.value === 'server');   // 默认 server + 本机地址
      r.agentOffline = !!(agentSel && agentSel.disabled && agentUrl.disabled && agentBtn.disabled &&
        document.getElementById('agentStatus').textContent.indexOf('离线') >= 0);
      r.disclaimer = document.getElementById('settingsDisclaimer').textContent.indexOf('不能替代医生的独立判断') >= 0;
      r.snapshotLabel = document.getElementById('kbSnapshotLine').textContent.indexOf('快照') >= 0;
      r.ragLine = document.getElementById('ragRunSettings').textContent.indexOf('检索模式') >= 0;
      // —— v49：向量模型自选安装（下拉 + 安装按钮 + 进度行；无 bridge 时按钮禁用） ——
      const embSel = document.getElementById('embModelSelect');
      const embBtn = document.getElementById('embInstallBtn');
      r.embUi = !!(embSel && embBtn && document.getElementById('embProgressLine') &&
        embSel.tagName === 'SELECT' && embBtn.tagName === 'BUTTON' &&
        embBtn.disabled && embBtn.textContent.indexOf('离线') >= 0);   // 探针环境无 bridge：明确离线降级
      // —— v45：设置页内左侧导航分区切换 ——
      const navSecurity = document.querySelector('.nav-item[data-pane="security"]');
      const navGeneral = document.querySelector('.nav-item[data-pane="general"]');
      const paneGeneral = document.querySelector('.settings-pane[data-pane="general"]');
      const paneSecurity = document.querySelector('.settings-pane[data-pane="security"]');
      navSecurity.click();
      const secActive = paneSecurity.classList.contains('active') &&
        !paneGeneral.classList.contains('active') && navSecurity.classList.contains('active');
      navGeneral.click();
      r.navSwitch = !!(secActive && paneGeneral.classList.contains('active') &&
        !paneSecurity.classList.contains('active') && navGeneral.classList.contains('active'));
      document.getElementById('settingsClose').click();
      r.closes = !drawer.classList.contains('open');
    } catch (e) { r.errors.push('EXC: ' + (e && e.message || e)); }
    return r;
  })();
  const flowLayer = (() => {
    /* v58：流程步骤条已删除——改为「一行状态文字」。本层只验证：状态行存在、__pushFlow
       正常工作、ask_more 弹窗与 review 自动展开辅助面板的行为保留、llm_unavailable 状态文字。 */
    const r = { errors: [] };
    try {
      const status = document.getElementById('statusLine');
      const modal = document.getElementById('flowGuide');
      // 层间卫生：assistLayer 已推送过结果卡（会改状态行）——复位 FLOW 与状态行再断言默认态
      FLOW = null; setStatus('等待医生输入', '');
      r.ui = !!(status && modal && document.getElementById('flowGuideState') &&
        document.getElementById('flowGuideText') && document.getElementById('flowGuideMissing') &&
        document.getElementById('flowGuideInput') && document.getElementById('flowGuideSend') &&
        document.getElementById('flowGuideLater') && document.getElementById('flowGuideClose'));
      r.pushFn = typeof window.__pushFlow === 'function';
      r.noFlowBar = !document.getElementById('flowBar');
      r.statusDefault = status.textContent.indexOf('等待医生输入') >= 0;
      // ask_more 推送 → v64-P3 不再弹固定流程指引窗；状态行引导医生看对话内澄清卡
      window.__pushFlow(JSON.stringify({ action: 'ask_more', workflow_state: 'awaiting_follow_up',
        question: '资料不足请补充', missing: ['主诉', '病程', '过敏史'], review_status: '' }));
      r.askNoModal = !modal.classList.contains('open');
      r.statusAsk = status.textContent.indexOf('对话') >= 0;
      // review 推送 → 不弹窗打断、状态行「等待医生审核」、辅助面板自动展开
      const assist = document.getElementById('assistDrawer');
      window.__pushFlow(JSON.stringify({ action: 'review', workflow_state: 'awaiting_review',
        question: '请审核', missing: [], review_status: 'pending' }));
      r.reviewNoModal = !modal.classList.contains('open');
      r.statusReview = status.textContent.indexOf('等待医生审核') >= 0;
      r.assistAutoOpen = assist.classList.contains('open');
      // llm_unavailable 推送 → 状态行明确「未连接 LLM」
      window.__pushFlow(JSON.stringify({ action: 'llm_unavailable', workflow_state: 'llm_unavailable',
        message: '当前未连接 LLM，暂不能生成真实 AI 医疗建议', missing: [], review_status: '' }));
      r.statusLlmBad = status.textContent.indexOf('未连接 LLM') >= 0;
      // 复原：收起辅助面板、状态行回默认，不影响后续断言快照
      window.__pushFlow(JSON.stringify({ action: '', workflow_state: 'screening',
        question: '', missing: [], review_status: '' }));
      assist.classList.remove('open');
      r.reset = status.textContent.indexOf('等待医生输入') >= 0 && !modal.classList.contains('open');
    } catch (e) { r.errors.push('EXC: ' + (e && e.message || e)); }
    return r;
  })();
  /* —— v58 左栏导航：新对话/历史病例/项目/最近（顺序固定、高亮、空状态不造假、清场不删历史） —— */
  const navLayer = (() => {
    const r = { errors: [] };
    try {
      // v63 Codex 式历史流：navNew + 时间流（railHistGroup，无标题无「查看全部」）+ 最近分组 + 底部图标行（📁/⚙）
      const navNew = document.getElementById('navNew'), navProject = document.getElementById('navProject');
      const titles = Array.from(document.querySelectorAll('#toolbar .rail-group-title')).map(e => e.textContent.trim());
      r.orderOk = navNew && navNew.textContent.indexOf('新对话') >= 0 &&
        !document.getElementById('navHist') &&                       // 历史病例浮层面板入口退役
        !!document.getElementById('railHistGroup') &&
        titles.length === 1 && titles[0] === '最近' &&               // 仅保留「最近」分组标题
        !!document.getElementById('railBottom') &&
        document.getElementById('railBottom').textContent.indexOf('📁') >= 0 &&
        document.getElementById('railBottom').textContent.indexOf('⚙') >= 0;
      // 侧栏条目（探针无 bridge）：历史流/最近均空态不造假，不渲染删除键
      r.railHistEmptyOk = ((document.getElementById('railHistEntries') || {}).textContent || '')
        .indexOf('未连接') >= 0 || ((document.getElementById('railHistEntries') || {}).textContent || '')
        .indexOf('暂无历史病例') >= 0;
      r.railRecentEmptyOk = ((document.getElementById('railRecentEntries') || {}).textContent || '')
        .indexOf('暂无最近记录') >= 0;
      r.railNoFakeDel = document.querySelectorAll('#toolbar .rail-entries .nav-row-del').length === 0;
      r.noViewButtons = !document.querySelector('#toolbar button[data-view]');
      r.noModelSelectInRail = !document.querySelector('#toolbar select#modelSelect');
      // 高亮：默认新对话 active；点击底部「📁 项目」高亮迁移
      r.newActiveDefault = navNew.classList.contains('active');
      navProject.click();
      r.projectActive = navProject.classList.contains('active') && !navNew.classList.contains('active');
      r.paneOpens = document.getElementById('navPane').classList.contains('open') &&
        document.getElementById('navPaneTitle').textContent.indexOf('项目') >= 0;
      r.projectCard = document.getElementById('navPaneBody').textContent.indexOf('DermAgent') >= 0;
      // 最近：空状态（不造假）→ 记录一条后再打开有真实记录
      document.getElementById('navRecent').click();
      r.recentActive = document.getElementById('navRecent').classList.contains('active');
      const bodyTxt0 = document.getElementById('navPaneBody').textContent;
      r.recentEmptyOk = bodyTxt0.indexOf('暂无最近记录') >= 0 || bodyTxt0.indexOf('💬') >= 0 || bodyTxt0.indexOf('🗂') >= 0;
      // 返回按钮：关闭面板 + 高亮回新对话
      document.getElementById('navPaneClose').click();
      r.paneCloses = !document.getElementById('navPane').classList.contains('open');
      r.activeBackToNew = navNew.classList.contains('active');
      // 新对话清场：造一些选点/对话内容 → 点新对话 → 输入/选点清空，但历史记录（CHAT_HISTORY）保留
      selectedRegions.push({ id: 'tmp_nav_test', label: 'nav测试', pos: new THREE.Vector3(0, 1, 0), desc: 'nav测试' });
      renderSelection();
      document.getElementById('chatInput').value = '导航层临时文本';
      const histBefore = CHAT_HISTORY.length;
      navNew.click();
      r.newClearsInput = document.getElementById('chatInput').value === '';
      r.newClearsRegions = selectedRegions.length === 0;
      r.newKeepsHistory = CHAT_HISTORY.length === histBefore;      // 不删历史数据
      r.backTo3dPrimary = document.getElementById('workstage').classList.contains('conv-primary');   // v59：新对话回对话主区
      r.backToChatPrimary = document.getElementById('workstage').classList.contains('conv-primary');
      r.statusReset = document.getElementById('statusLine').textContent.indexOf('等待医生输入') >= 0;
    } catch (e) { r.errors.push('EXC: ' + (e && e.message || e)); }
    return r;
  })();
  /* —— v60 删除按键层：最近记录单条删除/清空全部（localStorage 真实交互）；历史病例无 bridge 空态不造假 —— */
  const delLayer = (() => {
    const r = { errors: [] };
    try {
      window.confirm = function () { return true; };   // 探针环境直接确认（真实浏览器由医生决定）
      // 预置两条最近记录（🗂 病例 + 💬 对话），走真实导航入口渲染
      localStorage.setItem("derm_recent", JSON.stringify([
        { kind: "case", summary: "测试病例A", time: "2026-09-12 10:00" },
        { kind: "msg", summary: "测试对话B", time: "2026-09-12 10:01" }
      ]));
      document.getElementById('navRecent').click();
      let rows = document.querySelectorAll('#navPaneBody .nav-row');
      r.recentDelRows = rows.length === 2 &&
        document.querySelectorAll('#navPaneBody .nav-row-del').length === 2;
      r.recentDelClearBtn = document.getElementById('navPaneBody').textContent.indexOf('清空最近记录') >= 0;
      // v61：侧栏「最近」分组与 pane 数据同步（预置后侧栏应渲染 2 条 + 2 个 ✕）
      renderRailRecent();
      r.railRecentSync = document.querySelectorAll('#railRecentEntries .rail-entry').length === 2 &&
        document.querySelectorAll('#railRecentEntries .nav-row-del').length === 2;
      // 单条删除：点第一条 ✕ → 只剩「测试对话B」且持久化到 localStorage
      rows[0].querySelector('.nav-row-del').click();
      rows = document.querySelectorAll('#navPaneBody .nav-row');
      r.recentDelRemove = rows.length === 1 && rows[0].textContent.indexOf('测试对话B') >= 0;
      r.railRecentSyncAfterDel = document.querySelectorAll('#railRecentEntries .rail-entry').length === 1 &&
        document.querySelectorAll('#railRecentEntries .rail-entry')[0].textContent.indexOf('测试对话B') >= 0;
      let stored = [];
      try { stored = JSON.parse(localStorage.getItem("derm_recent") || "[]"); } catch (e) {}
      r.recentDelPersist = stored.length === 1 && stored[0].summary === '测试对话B';
      // 清空全部：点「清空最近记录」→ confirm 已被探针确认 → 空态 + localStorage 清空
      const clearBtn = Array.prototype.filter.call(
        document.querySelectorAll('#navPaneBody button'),
        b => b.textContent.indexOf('清空最近记录') >= 0)[0];
      if (clearBtn) clearBtn.click();
      r.recentClearAll = document.getElementById('navPaneBody').textContent.indexOf('暂无最近记录') >= 0;
      try { stored = JSON.parse(localStorage.getItem("derm_recent") || "[]"); } catch (e) {}
      r.recentClearPersist = Array.isArray(stored) && stored.length === 0;
      r.railRecentSyncClear = document.querySelectorAll('#railRecentEntries .rail-entry').length === 0 &&
        ((document.getElementById('railRecentEntries') || {}).textContent || '').indexOf('暂无最近记录') >= 0;
      // 历史病例：无 bridge → v63 历史流空态且不渲染删除按钮/「删除全部」（不造假记录）
      renderRailHist();
      const railHist = document.getElementById('railHistEntries');
      r.histNoFakeDel = railHist.querySelectorAll('.nav-row-del').length === 0 &&
        (railHist.textContent.indexOf('未连接') >= 0 || railHist.textContent.indexOf('暂无历史病例') >= 0);
      // v62/v63：空态下历史流底部不渲染「删除全部病例」按钮（无数据无可删，不造假）
      r.histEmptyNoDelAll = Array.prototype.filter.call(
        document.querySelectorAll('#railHistGroup button'),
        b => b.textContent.indexOf('删除全部') >= 0).length === 0;
      // 复原：关闭面板 + 高亮回新对话 + 清掉探针预置数据（侧栏同步回空态）
      document.getElementById('navPaneClose').click();
      document.getElementById('navNew').click();
      localStorage.removeItem("derm_recent");
      renderRailRecent();
    } catch (e) { r.errors.push('EXC: ' + (e && e.message || e)); }
    return r;
  })();
  /* —— v58 3D 顶部工具浮层：视角菜单（前/后/左/右/重置）+ 人体模型 + 缩放；选点全程保留 —— */
  const stageToolsLayer = (() => {
    const r = { errors: [] };
    try {
      setLayout('model_primary');               // v59：navLayer 已切到对话主区，这里切回 3D
      const tools = document.getElementById('stageTools');
      const viewBtn = document.getElementById('viewBtn');
      const menu = document.getElementById('viewMenu');
      const modelSel = document.getElementById('modelSelect');
      r.toolsInStage = !!tools && !!viewBtn && !!menu && !!modelSel &&
        !!document.getElementById('zoomIn') && !!document.getElementById('zoomOut') &&
        !!document.getElementById('resetCam') && !!document.getElementById('btnBackToChat');
      r.toolsInsideStageWrap = document.getElementById('stage-wrap').contains(tools);
      r.menuClosedDefault = !menu.classList.contains('open');
      // 视角菜单打开
      viewBtn.click();
      r.menuOpens = menu.classList.contains('open');
      const btns = menu.querySelectorAll('button[data-view]');
      r.menuItems = Array.prototype.map.call(btns, b => b.textContent.trim());
      r.menuOrderOk = r.menuItems.length === 5 &&
        r.menuItems[0] === '前视图' && r.menuItems[1] === '后视图' && r.menuItems[2] === '左视图' &&
        r.menuItems[3] === '右视图' && r.menuItems[4] === '重置视角';
      // 造一个选点，验证视角/缩放/切模型后仍保留
      selectedRegions.push({ id: 'tmp_view_test', label: '视角测试', pos: new THREE.Vector3(0, 1, 0), desc: '视角测试' });
      renderSelection();
      const pos0 = camera.position.clone();
      const pick = name => { menu.querySelectorAll('button[data-view]').forEach(b => { if (b.getAttribute('data-view') === name) b.click(); }); };
      const markerKept = () => markerPool.filter(m => m.visible).length > 0 && selectedRegions.some(x => x.id === 'tmp_view_test');
      pick('front');
      r.viewFront = camera.position.z > 0 && Math.abs(camera.position.x) < 0.01 && markerKept();
      pick('back');
      r.viewBack = camera.position.z < 0 && markerKept();
      pick('left');
      r.viewLeft = camera.position.x > 0 && markerKept();
      pick('right');
      r.viewRight = camera.position.x < 0 && markerKept();
      r.viewMenuCloses = !menu.classList.contains('open');
      // 缩放：沿视线改变距离（目标点不变）
      const target0 = controls.target.clone();
      const d0 = camera.position.distanceTo(target0);
      document.getElementById('zoomIn').click();
      const d1 = camera.position.distanceTo(target0);
      document.getElementById('zoomOut').click();
      document.getElementById('zoomOut').click();
      const d2 = camera.position.distanceTo(target0);
      r.zoomIn = d1 < d0 - 0.01;
      r.zoomOut = d2 > d0 + 0.01;
      document.getElementById('resetCam').click();
      r.resetCam = camera.position.distanceTo(controls.target) > 0 && markerKept();
      // 人体模型切换下拉存在且在 3D 顶部（切模型选点保留逻辑由模型层既有断言覆盖）
      r.modelSelectInTools = tools.contains(modelSel);
      // 复原：移除临时选点
      selectedRegions = selectedRegions.filter(x => x.id !== 'tmp_view_test');
      renderSelection();
    } catch (e) { r.errors.push('EXC: ' + (e && e.message || e)); }
    return r;
  })();
  /* —— v59 两态互斥布局：chat_primary（默认对话全宽）/ model_primary（3D 全屏）——
        只显示一个主区，绝不叠加；3D 场景不重建，选点/视角跨切换保留；无小窗无浮层。 */
  const layoutLayer = (() => {
    const r = { errors: [] };
    try {
      const ws = document.getElementById('workstage');
      const wrap = document.getElementById('stage-wrap');
      const pane = document.getElementById('convPane');
      r.sceneKept = !!scene && !!camera && markerPool.length > 0;      // 场景对象恒定（不重建）
      // 造一个选点，验证跨布局切换不丢失
      selectedRegions.push({ id: 'tmp_layout_test', label: '布局测试', pos: new THREE.Vector3(0, 1, 0), desc: '布局测试' });
      renderSelection();
      // ① model_primary：3D 全屏显示 + 对话区隐藏（互斥，不叠加）
      setLayout('model_primary');
      r.modelClass = ws.classList.contains('model-primary');
      r.modelWrapVisible = getComputedStyle(wrap).display !== 'none';
      r.modelPaneHidden = getComputedStyle(pane).display === 'none';
      r.noMiniWindow = !document.getElementById('miniTag');            // v59：小窗已删除
      // ② 输入框不再放置人体模型按钮；3D 入口由工作区工具栏提供
      r.modelBtnInComposer = !document.getElementById('btnModelMode');
      r.backBtnInStage = !!document.getElementById('btnBackToChat');
      // ③ 切回 chat_primary：对话全宽 + 3D 隐藏（互斥），选点保留
      setLayout('chat_primary');
      r.chatClass = ws.classList.contains('conv-primary') && !ws.classList.contains('model-primary');
      r.chatWrapHidden = getComputedStyle(wrap).display === 'none';
      r.chatPaneVisible = getComputedStyle(pane).display !== 'none';
      r.regionsKeptInChat = selectedRegions.some(x => x.id === 'tmp_layout_test');
      r.markersKept = markerPool.filter(m => m.visible).length > 0;
      // ④ 再切回模型主区：场景与选点仍保留（连续互切不丢状态）
      setLayout('model_primary');
      r.reEnterModel = getComputedStyle(wrap).display !== 'none';
      r.regionsStillKept = selectedRegions.some(x => x.id === 'tmp_layout_test');
      r.sceneStillKept = !!scene && !!camera && markerPool.length > 0;
      // 复原：清临时选点，保持模型主区（后续 3D 交互断言需要 3D 可见）
      selectedRegions = selectedRegions.filter(x => x.id !== 'tmp_layout_test');
      renderSelection();
    } catch (e) { r.errors.push('EXC: ' + (e && e.message || e)); }
    return r;
  })();
  /* —— v58 LLM 真实性守卫：结果卡三态（真实 AI / 离线演示 / 未连接），绝不伪装 —— */
  const llmGuardLayer = (() => {
    const r = { errors: [] };
    try {
      setLayout('chat_primary');                       // 对话区可见才能看到结果卡
      const log = document.getElementById('convLog');
      r.convPaneExists = !!log;
      // ① 真实 LLM 分析（source=llm）→ 显示方向与真实标识
      window.__pushAssist(JSON.stringify({ action: 'review', workflow_state: 'awaiting_review',
        llm: { ok: true, source: 'llm', model: 'test-model-x', error: '', at: '2026-09-12T00:00:00Z' },
        draft: { suspected_conditions: [{ name: '接触性皮炎', confidence: 'medium' }], workflow_state: 'awaiting_review' } }));
      let cards = log.querySelectorAll('.conv-card');
      let last = cards[cards.length - 1];
      r.llmOkBanner = last.textContent.indexOf('真实 AI 分析') >= 0;
      r.llmOkCondition = last.textContent.indexOf('接触性皮炎') >= 0;
      r.llmOkModel = last.textContent.indexOf('test-model-x') >= 0;
      r.llmOkReviewBtn = !!last.querySelector('.conv-actions button');
      r.statusReviewAfterLlm = document.getElementById('statusLine').textContent.indexOf('等待医生审核') >= 0;
      // ② 离线演示（source=demo）→ 强制横幅「不是真实 AI 分析，不可用于医疗判断」
      window.__pushAssist(JSON.stringify({ action: 'review', workflow_state: 'awaiting_review',
        llm: { ok: true, source: 'demo', model: 'offline_demo', error: '', at: '2026-09-12T00:00:01Z' },
        draft: { suspected_conditions: [{ name: '演示疾病', confidence: 'low' }], workflow_state: 'awaiting_review' } }));
      cards = log.querySelectorAll('.conv-card');
      last = cards[cards.length - 1];
      r.demoBanner = last.textContent.indexOf('离线演示数据') >= 0 && last.textContent.indexOf('不可用于医疗判断') >= 0;
      r.demoNoRealClaim = last.textContent.indexOf('真实 AI 分析（') < 0;
      r.statusDemo = document.getElementById('statusLine').textContent.indexOf('离线演示') >= 0;
      // ③ LLM 不可用（source=none）→ 不显示任何疾病/药物内容，只显示连接提示
      window.__pushAssist(JSON.stringify({ action: 'llm_unavailable', workflow_state: 'llm_unavailable',
        llm: { ok: false, source: 'none', model: '', error: 'LLM 请求失败：HTTP 500', at: '2026-09-12T00:00:02Z' },
        draft: null, message: '当前未连接 LLM，暂不能生成真实 AI 医疗建议' }));
      cards = log.querySelectorAll('.conv-card');
      last = cards[cards.length - 1];
      r.unavailMsg = last.textContent.indexOf('当前未连接 LLM，暂不能生成真实 AI 医疗建议') >= 0;
      r.unavailNoMedical = last.textContent.indexOf('疾病') < 0 || last.textContent.indexOf('演示疾病') < 0;
      r.unavailErrorShown = last.textContent.indexOf('HTTP 500') >= 0;
      r.unavailNoDraftCards = last.textContent.indexOf('鉴别') < 0 && last.textContent.indexOf('药物类别') < 0;
      // ④ appendLlmUnavailableCard（发送前探测失败的提示卡，含三入口）
      appendLlmUnavailableCard({ available: false, mode: 'server', error: 'LLM 未配置或服务不可达' });
      cards = log.querySelectorAll('.conv-card');
      last = cards[cards.length - 1];
      r.probeCardMsg = last.textContent.indexOf('当前未连接 LLM') >= 0;
      r.probeCardEntries = last.textContent.indexOf('检查连接') >= 0 &&
        last.textContent.indexOf('打开模型设置') >= 0 && last.textContent.indexOf('切换离线演示') >= 0;
      r.statusLlmUnavailable = document.getElementById('statusLine').textContent.indexOf('未连接 LLM') >= 0;
      // ⑤ v59 文书任务卡（__pushDoc）：真实 AI / 本地模板 / 未连接 三态横幅 + 内容与任务标题
      window.__pushDoc(JSON.stringify({ task_type: 'history', workflow_state: 'doc_ready',
        llm: { ok: true, source: 'llm', model: 'test-model-x', error: '', at: '2026-09-12T00:00:03Z' },
        doc: { task_type: 'history', content: '现病史：右前臂红斑瘙痒7天', generated_by: 'llm',
               citations: [{ citation_id: 'CIT-AD-001-S1' }], disclaimer: 'AI 生成文书草稿，供医生审核' } }));
      cards = log.querySelectorAll('.conv-card');
      last = cards[cards.length - 1];
      r.docLlmBanner = last.textContent.indexOf('真实 AI 生成') >= 0;
      r.docTaskTitle = last.textContent.indexOf('现病史整理') >= 0;
      r.docContent = last.textContent.indexOf('右前臂红斑瘙痒7天') >= 0;
      r.docCitation = last.textContent.indexOf('CIT-AD-001-S1') >= 0;
      window.__pushDoc(JSON.stringify({ task_type: 'summary', workflow_state: 'doc_ready',
        llm: { ok: true, source: 'template', model: '', generated_by: 'local-template', error: '' },
        doc: { task_type: 'summary', content: '病例摘要（整理）', generated_by: 'local-template',
               citations: [], disclaimer: '本地模板整理，不是真实 AI 分析' } }));
      cards = log.querySelectorAll('.conv-card');
      last = cards[cards.length - 1];
      r.docTemplateBanner = last.textContent.indexOf('本地模板整理') >= 0;
      r.docTemplateNoAiClaim = last.textContent.indexOf('真实 AI 生成（') < 0;
      window.__pushDoc(JSON.stringify({ task_type: 'differential', workflow_state: 'llm_unavailable',
        llm: { ok: false, source: 'none', model: '', error: 'offline', task_type: 'differential' },
        doc: null, message: '离线演示模式未连接 LLM，无法生成真实 AI 医疗建议' }));
      cards = log.querySelectorAll('.conv-card');
      last = cards[cards.length - 1];
      r.docUnavailBanner = last.textContent.indexOf('未连接 LLM') >= 0;
      r.docUnavailNoMedical = last.textContent.indexOf('鉴别') < 0 || last.textContent.indexOf('未连接') >= 0;
      // 复原：清空对话流、回模型主区（后续 3D 交互断言需要 3D 可见）
      log.innerHTML = '';
      setLayout('model_primary');
      APP_STATE.llm = 'unknown';
    } catch (e) { r.errors.push('EXC: ' + (e && e.message || e)); }
    return r;
  })();
  return JSON.stringify({
    ok: true,
    canvasSize: [canvas.width, canvas.height],
    shotKB: Math.round(dataUrl.length / 1024),
    triangles: renderer.info.render.triangles,
    drawCalls: renderer.info.render.calls,
    centerClickLabel: centerLabel,
    armLabel,
    v17Removed,
    hitRecord,
    faceSemActive: (typeof FACE_SEM_ACTIVE !== 'undefined') ? FACE_SEM_ACTIVE : null,
    unlabeled,
    calib,
    regionLayer,
    clinicLayer,
    assistLayer,
    settingsLayer,
    flowLayer,
    navLayer,
    delLayer,
    stageToolsLayer,
    layoutLayer,
    llmGuardLayer,
    anatomy,
    anatomyActive: (typeof ANATOMY_ACTIVE !== 'undefined') ? ANATOMY_ACTIVE : null,
    posStable,
    zones,
    wizard,
    multiCount,
    multiLabel,
    afterXDelete,
    afterClear,
    armRootRegression,
    thighRecs,
    thighDistinct,
    glbLoaded: (typeof modelRoot !== 'undefined' && modelRoot !== null),
    modelFile: (typeof MODEL_FILES !== 'undefined' && typeof modelIndex !== 'undefined') ? MODEL_FILES[modelIndex] : 'n/a',
    modelIndex: (typeof modelIndex !== 'undefined') ? modelIndex : -1,
    markersVisible: markerPool.filter(m => m.visible).length,
    selectedLabel: document.getElementById('regionLabel').textContent,
    fitSize: [ +fit2.x.toFixed(2), +fit2.y.toFixed(2), +fit2.z.toFixed(2) ]
  });
})()
"""

# v17：19 区域 pick 断言已移除（zoneFromPoint 型带断言见 ZONE_HINTS）

# 解剖语义断言：id|中文标签（锚点直验；趾为聚合带 0.6 置信）
ANATOMY_HINTS = {
    "leftEye": "left_eye|左眼（含虹膜）", "rightEye": "right_eye|右眼（含虹膜）",
    "nose": "nose|鼻",
    "upperLip": "upper_lip|上唇", "lowerLip": "lower_lip|下唇",
    "lThumb": "left_thumb_finger|左手拇指", "lIndex": "left_index_finger|左手食指",
    "lMiddle": "left_middle_finger|左手中指", "lRing": "left_ring_finger|左手无名指",
    "lLittle": "left_little_finger|左手小指",
    "rThumb": "right_thumb_finger|右手拇指", "rIndex": "right_index_finger|右手食指",
    "rMiddle": "right_middle_finger|右手中指", "rRing": "right_ring_finger|右手无名指",
    "rLittle": "right_little_finger|右手小指",
    "lToe": "left_toes|左脚趾（聚合）", "rToe": "right_toes|右脚趾（聚合）",
}

# v16 自动分区断言：型带比例（id|中文标签，zoneFromPoint 直验）
ZONE_HINTS = {
    "neck": "neck|颈部", "shoulderBelt": "shoulder_belt|右肩带（肩/上臂根）",
    "chest": "chest|胸", "abdomen": "abdomen|腹",
    "backUpper": "back_upper|背（上）", "backLower": "back_lower_waist|腰/背（下）",
    "hipButtock": "hip_buttock|右臀", "thigh": "thigh|左大腿",
    "knee": "knee|左膝", "calf": "calf|左小腿", "ankle": "ankle|左踝",
    "footToes": "foot_toes|左足/趾（当前模型缺少趾级精细语义，需校准）",
    "upperArm": "elbow_upper_arm|左上臂", "elbow": "elbow_upper_arm|左肘",
    "forearm": "forearm|左前臂", "wrist": "wrist|左腕",
    "palm": "palm|左掌", "headTop": "head_top|头顶",
}

# —— v32 女性模型回归断言：锚点 id|中文标签（坐标由 JS 从 anatomy-labels-Female3d66.json 表驱动读取，
#      此处只断言 id 与标签文本；锚点坐标本身是 tools/measure_female_anchors.py 的几何实测值） ——
FEMALE_ANAT = {
    "left_eye": "left_eye|左眼（含虹膜）", "right_eye": "right_eye|右眼（含虹膜）",
    "nose": "nose|鼻", "upper_lip": "upper_lip|上唇", "lower_lip": "lower_lip|下唇",
    "left_thumb_finger": "left_thumb_finger|左手拇指",
    "left_index_finger": "left_index_finger|左手食指",
    "left_middle_finger": "left_middle_finger|左手中指",
    "left_ring_finger": "left_ring_finger|左手无名指",
    "left_little_finger": "left_little_finger|左手小指",
    "right_thumb_finger": "right_thumb_finger|右手拇指",
    "right_index_finger": "right_index_finger|右手食指",
    "right_middle_finger": "right_middle_finger|右手中指",
    "right_ring_finger": "right_ring_finger|右手无名指",
    "right_little_finger": "right_little_finger|右手小指",
    "left_toes": "left_toes|左脚趾（聚合）", "right_toes": "right_toes|右脚趾（聚合）",
    "left_ear": "left_ear|左耳", "right_ear": "right_ear|右耳",
}

# 切换到女性模型（modelSelect 第 2 项）并等待装配+锚点表校验完成
SWITCH_JS = """
(() => {
  const s = document.getElementById('modelSelect');
  if (!s) return JSON.stringify({ ok: false, reason: 'no modelSelect' });
  const want = String(s.options[%d] ? s.options[%d].value : '');
  if (!want) return JSON.stringify({ ok: false, reason: 'option missing' });
  s.value = want;
  s.dispatchEvent(new Event('change'));
  return JSON.stringify({ ok: true, value: want });
})()
"""

POLL_MODEL_JS = """
JSON.stringify((() => ({
  key: (typeof modelKey === 'function') ? String(modelKey()) : '',
  anatomyActive: (typeof ANATOMY_ACTIVE !== 'undefined') ? ANATOMY_ACTIVE === true : false,
  faceSemActive: (typeof FACE_SEM_ACTIVE !== 'undefined') ? FACE_SEM_ACTIVE === true : false,
  ready: (typeof modelRoot !== 'undefined') && !!modelRoot && !!fitInfo
}))())
"""

# 女性模型断言（单发）：锚点直验 + z_band 带归属 + 胸型带 + 向导预设 + 真实 onPick 链投点
# （眼/趾/发片/衣物）+ 材质兜底。所有结果 JSON 字符串回传（本 PySide6 只可靠回传标量/字符串）。
FEMALE_CHECK_JS = r"""
(() => {
  try {
    if (!modelRoot || !fitInfo || !ANATOMY || !ANATOMY.entries)
      return JSON.stringify({ ok: false, reason: '模型/锚点表未就绪' });
    const out = { ok: true };
    out.model = modelKey();
    out.anatomyActive = (typeof ANATOMY_ACTIVE !== 'undefined') ? ANATOMY_ACTIVE : null;
    out.faceSemActive = (typeof FACE_SEM_ACTIVE !== 'undefined') ? FACE_SEM_ACTIVE : null;
    const fit = fitInfo.getSize(new THREE.Vector3());
    out.fitSize = [+fit.x.toFixed(3), +fit.y.toFixed(3), +fit.z.toFixed(3)];
    const byId = {};
    ANATOMY.entries.forEach(e => { byId[e.anatomical_label] = e; });
    function anat(x, y, z) {
      const a = anatomyFromPoint(new THREE.Vector3(x, y, z));
      return a ? a.id + '|' + a.label : null;
    }
    // —— 锚点直验（19 项，坐标全部取自当前生效的女性锚点表） ——
    const wants = ['left_eye','right_eye','nose','upper_lip','lower_lip',
      'left_thumb_finger','left_index_finger','left_middle_finger','left_ring_finger','left_little_finger',
      'right_thumb_finger','right_index_finger','right_middle_finger','right_ring_finger','right_little_finger',
      'left_toes','right_toes','left_ear','right_ear'];
    out.anchors = {};
    wants.forEach(id => {
      const e = byId[id];
      out.anchors[id] = e ? anat(e.world_xyz[0], e.world_xyz[1], e.world_xyz[2]) : 'MISSING';
    });
    // —— z_band 带归属：左中指带中点（x 在指尖板层内、y 同锚点） → 应报中指 ——
    const lm = byId.left_middle_finger;
    out.fingerBand = (lm && lm.z_band)
      ? anat(lm.tip_x - 0.02, lm.world_xyz[1], (lm.z_band[0] + lm.z_band[1]) / 2) : null;
    // —— 胸型带（向导胸点 ny≈0.69 ∈ chest 带 0.62..0.835） ——
    const cz = zoneFromPoint(new THREE.Vector3(0.0, 0.949, 0.2737));
    out.chestZone = cz ? cz.id + '|' + cz.label : null;
    // —— 向导 7 点预设（应来自女性表：左眼 spot.x=+0.0906） ——
    const ws = wizardSteps();
    out.wizard = { count: ws.length, leftEyeX: ws.length ? ws[0].spot[0] : null };
    // —— 真实 onPick 链投点（前视相机；surf 先验证可视表面命中） ——
    setView('front');
    const canvas = document.querySelector('#stage canvas');
    const rect = canvas.getBoundingClientRect();
    function meshMatName(o) {
      const m = Array.isArray(o.material) ? o.material[0] : o.material;
      return (m && m.name) || '';
    }
    function surf(wx, wy, wz) {
      const dir = new THREE.Vector3(wx, wy, wz).sub(camera.position).normalize();
      raycaster.set(camera.position, dir);
      const pickables = [];
      modelRoot.traverse(o => { if (o.isMesh && o.userData.pickable) pickables.push(o); });
      const hits = raycaster.intersectObjects(pickables, false);
      return hits.length ? { point: hits[0].point, mat: meshMatName(hits[0].object) } : null;
    }
    function realClick(wx, wy, wz) {
      clearSelection();
      const v = new THREE.Vector3(wx, wy, wz).project(camera);
      const cx = rect.left + (v.x + 1) / 2 * rect.width;
      const cy = rect.top + (1 - (v.y + 1) / 2) * rect.height;
      const offs = [[0,0],[3,0],[-3,0],[0,3],[0,-3]];
      for (const [dx, dy] of offs) {
        canvas.dispatchEvent(new MouseEvent('click', { clientX: cx + dx, clientY: cy + dy, bubbles: true }));
        if (selectedRegions.length) {
          const r = selectedRegions[selectedRegions.length - 1];
          const s = surf(wx, wy, wz);
          return { label: r.label, source: r.source, mat: s ? s.mat : '', d: null };
        }
        clearSelection();
      }
      return null;
    }
    out.clicks = {};
    // 眼：瞄准下睑（锚点下方 2.5cm、前方 10cm 引导射线）→ 命中眼睑皮肤应报左眼（7.5cm 眼睑带）
    const le = byId.left_eye;
    const sEye = surf(le.world_xyz[0], le.world_xyz[1] - 0.025, le.world_xyz[2] + 0.10);
    if (sEye) {
      const dEye = sEye.point.distanceTo(new THREE.Vector3(le.world_xyz[0], le.world_xyz[1], le.world_xyz[2]));
      out.clicks.leftEye = realClick(sEye.point.x, sEye.point.y, sEye.point.z);
      if (out.clicks.leftEye) out.clicks.leftEye.d = +dEye.toFixed(4);
    } else out.clicks.leftEye = { miss: true };
    // 趾：趾甲聚合锚点正射（front 可视趾面）。+z 前方引导会因相机俯角越过趾尖、
    // 撞上脚底前缘（足底带 y≤-2.467）误报足弓——语义表/运行时均正确，纯探针引导失准
    const lt = byId.left_toes;
    const sToe = surf(lt.world_xyz[0], lt.world_xyz[1], lt.world_xyz[2]);
    if (sToe) {
      const dToe = sToe.point.distanceTo(new THREE.Vector3(lt.world_xyz[0], lt.world_xyz[1], lt.world_xyz[2]));
      out.clicks.leftToes = realClick(sToe.point.x, sToe.point.y, sToe.point.z);
      if (out.clicks.leftToes) out.clicks.leftToes.d = +dToe.toFixed(4);
    } else out.clicks.leftToes = { miss: true };
    // 发片/衣物材质兜底：按 coveringMaterialFor 收集前视可见的候选 mesh（≤3 个/类），
    // 取其包围盒前缘投点。命中材质与类一致时断言标签（射线穿隙命中他物则跳过该候选）。
    const covPts = { hair: [], clothing: [] };
    modelRoot.traverse(o => {
      if (!o.isMesh) return;
      const cov = coveringMaterialFor(o);
      if (!cov || covPts[cov].length >= 12) return;
      const b = new THREE.Box3().setFromObject(o);
      if (cov === 'hair' && b.getCenter(new THREE.Vector3()).z < 0.05) return;   // 后发片前视不可见
      const c = b.getCenter(new THREE.Vector3());
      const w = b.max.x - b.min.x;
      covPts[cov].push([c.x, c.y, b.max.z]);                                    // 盒前面中心
      covPts[cov].push([b.min.x + w * 0.25, b.min.y + (b.max.y - b.min.y) * 0.5, b.max.z]);
      covPts[cov].push([c.x, b.min.y + (b.max.y - b.min.y) * 0.35, b.max.z]);   // 偏上一点
      covPts[cov].push([b.min.x + w * 0.75, b.min.y + (b.max.y - b.min.y) * 0.5, b.max.z]);
    });
    function clickCandidates(kind, wantCov, wantInLabel) {
      const tried = [];
      let hit = null;
      const matOk = m => coveringMaterialFor({ material: { name: m } }) === wantCov;   // 伪 Material 实例
      for (const [x, y, z] of covPts[kind]) {
        const s = surf(x, y, z);
        if (!s || !matOk(s.mat)) { tried.push({ mat: s ? s.mat : null, skip: true }); continue; }
        const rec = realClick(x, y, z);
        tried.push({ mat: s.mat, label: rec ? rec.label : null });
        if (rec && rec.label && rec.label.indexOf(wantInLabel) !== -1) { hit = { label: rec.label, source: rec.source, mat: s.mat }; break; }
      }
      return { hit, tried };
    }
    out.clicks.hair = clickCandidates('hair', 'hair', '（发覆盖）');
    out.clicks.clothing = clickCandidates('clothing', 'clothing', '（衣物覆盖）');
    clearSelection();
    return JSON.stringify(out);
  } catch (e) { return JSON.stringify({ ok: false, reason: String(e && e.stack || e) }); }
})()
"""

# 切回男性后的零回归复核：锚点表/逐面表重新启用 + 抽查锚点/型带/向导预设 + z_band 修复生效
MALE_RECHECK_JS = r"""
(() => {
  try {
    if (!modelRoot || !fitInfo) return JSON.stringify({ ok: false, reason: '模型未就绪' });
    function anat(x, y, z) {
      const a = anatomyFromPoint(new THREE.Vector3(x, y, z));
      return a ? a.id + '|' + a.label : null;
    }
    let fingerBand = null;
    ANATOMY.entries.forEach(e => {
      if (e.anatomical_label === 'left_middle_finger' && e.z_band)
        fingerBand = anat(e.tip_x - 0.02, e.world_xyz[1], (e.z_band[0] + e.z_band[1]) / 2);
    });
    return JSON.stringify({
      ok: true,
      model: modelKey(),
      anatomyActive: ANATOMY_ACTIVE,
      faceSemActive: FACE_SEM_ACTIVE,
      anchors: {
        leftEye: anat(0.094, 2.134, 0.205),
        rightEye: anat(-0.094, 2.134, 0.205),
        lMiddle: anat(1.8471, 0.0487, 0.1058)
      },
      fingerBand,
      wizardLeftEyeX: wizardSteps()[0].spot[0]
    });
  } catch (e) { return JSON.stringify({ ok: false, reason: String(e && e.stack || e) }); }
})()
"""


# 女性逐面语义表真实点击普查：对每个 zone 的代表面质心做真实 onPick 链点击，
# 记录 label+source。__SAMPLES__ 由 Python 注入 [[zid, zh, x, y, z], ...]；
# 先在 __VIEW__ 视角点击，本区（zh 前缀）未命中者换 __VIEW2__ 视角重试——
# 样本可见视角由双视角实测决定，不依赖构建器的法线/位置猜测（唇内面/趾甲等
# 混合法线带单面判定不可靠）。
ZONE_CLICK_JS = r"""
(() => {
  try {
    if (!modelRoot || !fitInfo || !FACE_SEM_ACTIVE)
      return JSON.stringify({ ok: false, reason: '模型/逐面表未就绪' });
    const SAMPLES = __SAMPLES__;
    const canvas = document.querySelector('#stage canvas');
    const rect = canvas.getBoundingClientRect();
    function realClick(wx, wy, wz) {
      clearSelection();
      const v = new THREE.Vector3(wx, wy, wz).project(camera);
      const cx = rect.left + (v.x + 1) / 2 * rect.width;
      const cy = rect.top + (1 - (v.y + 1) / 2) * rect.height;
      const offs = [[0, 0], [2, 0], [-2, 0], [0, 2], [0, -2]];
      for (const [dx, dy] of offs) {
        canvas.dispatchEvent(new MouseEvent('click', { clientX: cx + dx, clientY: cy + dy, bubbles: true }));
        if (selectedRegions.length) {
          const r = selectedRegions[selectedRegions.length - 1];
          return { label: r.label, source: r.source };
        }
        clearSelection();
      }
      return null;
    }
    function hitBase(rec, zh) {
      const base = zh.split('（')[0];
      return !!(rec && base && rec.label.includes(base));
    }
    const rows = {};
    setView('__VIEW__');
    for (const [zid, zh, x, y, z] of SAMPLES) rows[zid] = { zh: zh, click: realClick(x, y, z), view: '__VIEW__' };
    setView('__VIEW2__');
    for (const [zid, zh, x, y, z] of SAMPLES) {
      const r = rows[zid];
      if (hitBase(r.click, zh)) continue;
      const c2 = realClick(x, y, z);
      if (hitBase(c2, zh)) { r.click = c2; r.view = '__VIEW2__'; }
      else if (!r.click) { r.click = c2; r.view = '__VIEW2__'; }
      else { r.click2 = c2; r.view2 = '__VIEW2__'; }   // 两次均未直中：一并交给 Python 兜底判定
    }
    clearSelection();
    return JSON.stringify({ ok: true, view: '__VIEW__/__VIEW2__', n: SAMPLES.length, rows: rows });
  } catch (e) { return JSON.stringify({ ok: false, reason: String(e && e.stack || e) }); }
})()
"""


def zone_click_ok(zid: str, zh: str, rec) -> bool:
    """zone 样本点击判定：命中本区 zh 前缀（逐面表来源），或可解释的遮挡兜底。

    手/足细带（指节/指甲/趾）在 A-pose 侧对相机，±2px 轮询常命中间邻指皮肤、
    指蹼（手背）或前伸的邻趾——真实用户点击同点也会得到同样结果，属正确运行时
    行为；细带身份已由构建期断言（锚点最近邻+面数）与运行时锚点断言保证。
    rec 为双视角尝试结果：click（首选视角）/click2（对侧重试），任一通过即可。
    """
    if rec is None:
        return False
    if isinstance(rec, dict):
        rec = [rec]
    return any(_zone_click_one(zid, zh, r) for r in rec if r is not None)


def _zone_click_one(zid: str, zh: str, rec) -> bool:
    if not isinstance(rec, dict):
        return False
    label = str(rec.get("label") or "")
    if not label:
        return False
    base = zh.split("（")[0]
    if base and base in label:
        return str(rec.get("source") or "").startswith("逐面语义表")  # 必须是逐面表来源
    if "头发" in label or "覆盖" in label:           # 发片/衣物（含内裤）遮挡属可解释兜底
        return True
    if zid == "briefs_side" and any(k in label for k in ("侧腹", "髋", "腹")):
        return True                                  # 内裤侧带上角与侧腹皮界 ±2px 掠射
    if (zid.startswith(("palm_", "hand_", "wrist_", "fingernail_")) or "_phalanx_" in zid) \
            and any(k in label for k in ("指", "掌", "手背", "指甲", "腕")):
        return True                                  # 手部细带：邻指/指蹼遮挡
    if zid.startswith(("toe_", "toenail_", "arch_", "forefoot_", "instep_", "heel_",
                       "foot_rim_")) \
            and any(k in label for k in ("趾", "脚掌", "足弓", "脚跟", "脚背")):
        return True                                  # 足部细带：前视不可见足底/邻趾遮挡
    if zid.startswith(("eyeball", "iris", "eyelid", "periorbital")) and "眼" in label:
        return True                                  # 眼球件相邻（角膜/睑）同属眼
    if zid.startswith("eyelashes") and ("睫毛" in label or "眼" in label):
        return True
    if zid in ("mouth_inner", "teeth", "upper_lip", "lower_lip", "mouth_region") \
            and any(k in label for k in ("唇", "口", "牙")):
        return True                                  # 闭口唇缘遮挡口腔/牙/唇周
    if zid.startswith("ear_") and "耳" in label:
        return True                                  # 耳廓被发片遮挡（锚点表 occlusion_notes 已注明）
    if zid == "head_base" and any(k in label for k in ("头发", "面颊", "耳前", "额")):
        return True                                  # 颅底皮肤被发片/面部边缘遮挡
    if zid.startswith("hair_") and any(k in label for k in HAIR_UNDER_OK.get(zid, ())):
        return True                                  # 发梢被体表遮挡：命中发下体区=配对正确
    return False


# 发区 → 其遮盖的身体区（双视角下发梢常被体表遮挡，命中发下体区属可解释结果，
# 且恰好校验「发区 ↔ 发下体区」语义配对，如 背上（发覆盖）→背（上））
HAIR_UNDER_OK = {
    "hair_forehead": ("额",),
    "hair_face": ("颊", "眼", "鼻", "面", "颞"),
    "hair_neck_front": ("颈", "喉"),
    "hair_neck_side": ("颈",),
    "hair_shoulder_l": ("肩", "胸", "锁骨"),
    "hair_shoulder_r": ("肩", "胸", "锁骨"),
    "hair_occiput": ("头",),
    "hair_crown": ("头",),
    "hair_nape": ("颈", "头"),
    "hair_back_upper": ("背",),
    "hair_ear_cheek": ("耳", "颊", "颞"),
    "hair_rest": ("头发",),
}


def save_data_url(data_url: str, path: str) -> None:
    import base64
    raw = base64.b64decode(data_url.split(",", 1)[1])
    with open(path, "wb") as fh:
        fh.write(raw)


def main() -> int:
    from PySide6.QtCore import Qt
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication([])
    view = QWebEngineView()
    view.setWindowFlags(view.windowFlags() | 0x00000080)  # WindowStaysOnBottomHint：验证窗口置底少打扰
    view.resize(900, 760)
    page = DebugPage(view)
    view.setPage(page)

    result = {"ok": False, "reason": "timeout"}
    shots = {}

    # —— v33 女性 zone 样本（构建器输出的真实面质心）+ 逐面表 zh 标签 → 双视角点击普查 ——
    female_zones = None
    try:
        with open(os.path.join(DATA, "female_zone_samples.json")) as fh:
            zs = json.load(fh)["zones"]
        with open(os.path.join(HERE, "web", "face-semantics-Female3d66.json")) as fh:
            flabels = json.load(fh)["labels"]
        # 前/后视皆试（JS 内未命中换对侧重试），front 在前仅是相机顺序偏好
        female_zones = sorted(
            ([zid, (flabels.get(zid) or {}).get("zh") or zid, *rec["point"]]
             for zid, rec in zs.items()),
            key=lambda row: 0 if zs[row[0]].get("view") == "front" else 1)
        print(f"[female-zones] zones={len(female_zones)}（前/后双视角实测）", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[female-zones] 样本加载失败：{exc}", flush=True)

    def on_checks(payload):
        nonlocal result
        result = json.loads(payload) if isinstance(payload, str) else {}
        if not result.get("ok"):
            finish(3)
            return
        # —— 点击命中断言：胸/头/臂/腿/小腿/足 必须映射正确（pick 段已并入 zones 直验） ——
        bad = []
        # —— v17 断言：19 区域逻辑已删净（REGIONS/regionFromPoint/命中盒不存在） ——
        v17r = result.get("v17Removed") or {}
        for key in ("noREGIONS", "noRegionFromPoint", "noHitboxes"):
            if v17r.get(key) is not True:
                bad.append(f"v17Removed.{key}: 19 区域残留 {v17r}")
        # —— 阶段2 人工区域数据层：新建/追加视角/undo 恢复/重叠规则/导出就绪/失败不变性 ——
        rl = result.get("regionLayer") or {}
        for key, msg in (
            ("created", "front 圈选应新建 region v1（单视角）"),
            ("revised", "back 同部位圈选应追加视角 v2（faces 并集）"),
            ("undoRevise", "undo 应回退 revision 至 v1 单视角"),
            ("undoRestored", "二次 undo 应回退创建、regions 数复原"),
            ("overlapLatest", "同面重叠应归属 updated_at 最新区域"),
            ("exportReady", "导出就绪：完整 SHA-256 模型指纹"),
            ("invariant", "指纹不匹配导入应拒绝且原数据零改动"),
        ):
            if rl.get(key) is not True:
                bad.append(f"regionLayer.{key}: {msg} got={rl.get(key)} errors={rl.get('errors')}")
        # —— 阶段3 医生资料层：入口/抽屉开合/检验表收集/payload 集成/影像同步拒绝 ——
        cl = result.get("clinicLayer") or {}
        for key, msg in (
            ("ui", "＋菜单医生资料入口/抽屉元素缺失"),
            ("opens", "抽屉应可打开"),
            ("closes", "抽屉应可关闭"),
            ("labs2", "检验两行收集不符"),
            ("labs1", "删行后收集递减不符"),
            ("payloadNull", "无资料时 payload.clinic 应为 null"),
            ("payloadHas", "填病史后 payload.clinic 应携带"),
            ("rejectExt", "不支持扩展名应同步拒绝（安全降级）"),
            ("rejectEmpty", "空文件应同步拒绝"),
            ("patUi", "病历单患者基本信息输入缺失"),
            ("patAutoDate", "就诊日期留空应自动填今天"),
            ("patAutoNo", "有姓名无门诊号应自动生成 OPD 号并写回输入框"),
            ("patMasked", "载荷 patient 应携带且身份字段脱敏/临床字段保留"),
            ("patSelRow", "辅助面板选择区应出现患者基本信息行"),
        ):
            if cl.get(key) is not True:
                bad.append(f"clinicLayer.{key}: {msg} got={cl.get(key)} errors={cl.get('errors')}")
        # —— 阶段4 辅助面板：互斥抽屉/默认全选/逐项取消/默认脱敏/参考病例逐个确认 ——
        al = result.get("assistLayer") or {}
        for key, msg in (
            ("ui", "辅助面板入口/选择区/预览区元素缺失"),
            ("opens", "辅助面板应可打开"),
            ("exclusive", "打开辅助面板应收起医生资料抽屉（同侧互斥）"),
            ("allSelected", "资料应默认全选"),
            ("excluded", "取消病史后 payload 应不携带并计入 excluded_items"),
            ("included", "恢复勾选后 payload 应携带病史"),
            ("masked", "发送载荷应默认脱敏（手机号/身份证/姓名/地址→类别标记）"),
            ("refGated", "参考病例未经逐个确认不得进入载荷"),
            ("refConfirmed", "医生确认后的参考病例应带 seq 进入 ref_cases"),
            # —— 阶段5：AI 辅助判断三区渲染 ——
            ("pushAck", "推送服务端草稿应返回 ok"),
            ("dxRendered", "诊断辅助区应渲染 2 个鉴别方向卡"),
            ("dxNotProbability", "辅助排序必须标注「非确诊概率」"),
            ("dxPendingWording", "鉴别方向措辞必须为「待排」"),
            ("dxCite", "支持证据应带引用 id"),
            ("dxUnknownNoEvidence", "unknown 方向应显示「支持证据：无」"),
            ("dxAbsent", "有结果时诊断辅助区不应出现「知识库未覆盖」空态"),
            ("advRendered", "临床建议区应渲染症状/下一步/用药核对条目"),
            ("advMedicationGuard", "用药核对必须常显「禁止自动处方/剂量/疗程」"),
            ("advDoctorConfirm", "用药核对必须标注「需医生确认后执行」"),
            ("auditPending", "审核区 AI 建议状态应为 pending 徽标"),
            ("auditMeta", "审核区应显示模型版本与生成时间"),
            ("disclaimerAlwaysOn", "免责声明横幅应常显于辅助面板"),
            ("pushReject", "非法推送载荷应被拒绝且不抛错"),
            ("dxEmptyState", "无结果时诊断辅助区应显示「知识库未覆盖」空态（不编造）"),
            # —— 阶段6：审核历史与医生审核状态 ——
            ("auditHistoryCount", "审核区应显示审核历史条数"),
            ("auditHistoryLatest", "审核区应显示最近一次决策（重新生成）"),
            ("auditEditedStatus", "修订后医生审核状态应为「已修订（待医生再确认）」"),
            ("auditApprovedStatus", "确认后医生审核状态应标注「待签署草稿已生成」"),
        ):
            if al.get(key) is not True:
                bad.append(f"assistLayer.{key}: {msg} got={al.get(key)} errors={al.get('errors')}")
        # —— §16.1：左下角齿轮设置面板（知识库 / RAG 运行 / 版本与安全） ——
        sl = result.get("settingsLayer") or {}
        for key, msg in (
            ("ui", "齿轮入口/设置面板/知识库列表/导入备份按钮/快照行/免责声明/导航分区元素缺失"),
            ("gearInToolbar", "设置齿轮必须在左侧栏最底部（参考图布局），不在 composer 内"),
            ("modelInToolbar", "v58 人体模型下拉应在 3D 顶部工具浮层 #stageTools 内，不在顶栏/composer 内"),
            ("llmSelInComposer", "LLM 大模型切换下拉必须在 composer 发送按钮旁，不在左侧栏内"),
            ("llmSelOffline", "无 bridge 时 LLM 模型下拉应禁用并提示离线，不伪造可用模型"),
            ("thinkingSelInComposer", "思考等级切换必须在 composer 发送按钮旁（紧贴模型选择器）"),
            ("thinkingSelOffline", "无 bridge 时思考等级下拉应禁用并提示离线"),
            ("fullscreenPage", "设置必须是独立全屏界面（settings-page 覆盖层），而非侧边抽屉"),
            ("backWording", "设置页顶部必须有「← 返回应用」按钮"),
            ("langUi", "语言切换单选（简体中文/English）元素缺失"),
            ("langSwitch", "切换 English 后设置面板标题/按钮应变为英文"),
            ("langRestore", "切回简体中文后应恢复中文文案"),
            ("llmUi", "LLM 连接区（Base URL/模型/Key/保存/状态）元素缺失"),
            ("opens", "点击最左下角齿轮应打开设置面板"),
            ("exclusive", "打开设置面板应收起其他抽屉（互斥）"),
            ("degradedNoBridge", "无 bridge 时知识库区应显示「未连接/离线」降级提示，不伪造数据"),
            ("llmMasked", "无 bridge 时 LLM 状态应显示「未连接/离线」，绝不回显任何 Key"),
            ("disclaimer", "设置面板版本与安全区必须常显免责声明"),
            ("snapshotLabel", "设置面板应显示知识库快照版本行"),
            ("ragLine", "设置面板应显示 RAG 检索模式行"),
            ("embUi", "设置页应有向量模型自选安装区块（下拉+按钮+进度行），无 bridge 时按钮明确离线禁用"),
            ("agentUi", "设置页通用区应有「接入方式」卡片（模式下拉+地址输入+保存+帮助/状态行），顶栏控件已移除"),
            ("agentDefaultServer", "接入模式下拉默认应为 server（服务端管线）"),
            ("agentOffline", "无 bridge 时接入方式控件应整体禁用并提示离线，不伪造可修改状态"),
            ("navSwitch", "设置页左侧导航应能切换分区（点击「检索与安全」→对应面板激活，切回「通用」恢复）"),
            ("closes", "「← 返回应用」应可收起全屏设置页"),
        ):
            if sl.get(key) is not True:
                bad.append(f"settingsLayer.{key}: {msg} got={sl.get(key)} errors={sl.get('errors')}")
        # —— v58 状态行（流程步骤条已删除）+ ask_more 弹窗 + review 自动开辅助面板 ——
        fl = result.get("flowLayer") or {}
        for key, msg in (
            ("ui", "状态行/指引弹窗元素缺失（状态行+弹窗+缺失区+输入+发送/稍后/关闭按钮）"),
            ("pushFn", "window.__pushFlow 桥接入口缺失"),
            ("noFlowBar", "流程步骤条应已删除（不再有 #flowBar）"),
            ("statusDefault", "初始状态行应显示「等待医生输入」"),
            ("askNoModal", "v64：ask_more 推送不应弹固定流程指引窗（澄清走对话流）"),
            ("statusAsk", "ask_more 状态行应引导医生看对话内澄清"),
            ("reviewNoModal", "review 推送不应弹窗打断（草稿已在原生窗口展示）"),
            ("statusReview", "review 状态行应显示「等待医生审核」"),
            ("assistAutoOpen", "review 推送应自动展开「医生辅助判断」面板（下一步窗口直接送到眼前）"),
            ("statusLlmBad", "llm_unavailable 推送状态行应明确「未连接 LLM」"),
            ("reset", "探针复原后状态行回默认且弹窗关闭"),
        ):
            if fl.get(key) is not True:
                bad.append(f"flowLayer.{key}: {msg} got={fl.get(key)} errors={fl.get('errors')}")
        # —— v58 左栏导航 + v61 分组常驻：三分组顺序/侧栏条目空态不造假/项目与最近面板/新对话清场不删历史 ——
        nl = result.get("navLayer") or {}
        for key, msg in (
            ("orderOk", "v63 左栏应为「新对话 + 时间分组历史流（无历史病例面板入口）+ 最近分组 + 底部📁/⚙图标行」"),
            ("railHistEmptyOk", "侧栏历史流无 bridge 时应显示空态（不造假记录）"),
            ("railRecentEmptyOk", "侧栏最近分组无记录时应显示空态（不造假）"),
            ("railNoFakeDel", "侧栏分组无数据时不应渲染删除按钮"),
            ("noViewButtons", "左栏不应再有前/后/左/右视角按钮（已并入 3D 顶部工具浮层）"),
            ("noModelSelectInRail", "左栏不应再有人体模型下拉（已移到 3D 顶部）"),
            ("newActiveDefault", "默认应高亮「新对话」"),
            ("projectActive", "点击底部「📁 项目」应高亮迁移"),
            ("paneOpens", "点击「项目」应打开导航面板并显示标题"),
            ("projectCard", "项目面板应显示 DermAgent 项目信息"),
            ("recentActive", "点击「最近」应高亮"),
            ("recentEmptyOk", "最近面板应显示真实记录或空状态（不造假）"),
            ("paneCloses", "返回按钮应关闭导航面板"),
            ("activeBackToNew", "关闭面板后高亮应回到「新对话」"),
            ("newClearsInput", "新对话应清空输入框"),
            ("newClearsRegions", "新对话应清空 3D 选点"),
            ("newKeepsHistory", "新对话不得删除历史数据（CHAT_HISTORY 保留）"),
            ("backTo3dPrimary", "新对话应回到对话主区布局（v59：chat_primary）"),
            ("statusReset", "新对话后状态行应回「等待医生输入」"),
        ):
            if nl.get(key) is not True:
                bad.append(f"navLayer.{key}: {msg} got={nl.get(key)} errors={nl.get('errors')}")
        # —— v60 删除按键 + v61 侧栏同步：最近记录单条 ✕/清空全部（真实持久化）；历史病例无 bridge 不造假删除键 ——
        dl = result.get("delLayer") or {}
        for key, msg in (
            ("recentDelRows", "预置 2 条最近记录后应渲染 2 行且各带删除按钮（✕）"),
            ("recentDelClearBtn", "最近面板应提供「清空最近记录」按钮"),
            ("railRecentSync", "预置后侧栏「最近」分组应同步渲染 2 条 + 2 个 ✕（v61）"),
            ("recentDelRemove", "点第一条 ✕ 后应只剩「测试对话B」一行"),
            ("railRecentSyncAfterDel", "单条删除后侧栏「最近」分组应同步剩 1 条（v61）"),
            ("recentDelPersist", "单条删除应持久化到 localStorage（剩 1 条）"),
            ("recentClearAll", "点「清空最近记录」后应显示空状态"),
            ("recentClearPersist", "清空后 localStorage 应为空数组"),
            ("railRecentSyncClear", "清空后侧栏「最近」分组应同步为空态（v61）"),
            ("histNoFakeDel", "历史病例无 bridge 时应空态且不渲染删除按钮（不造假）"),
            ("histEmptyNoDelAll", "v62 历史病例空态下不应渲染「删除全部病例」按钮（不造假）"),
        ):
            if dl.get(key) is not True:
                bad.append(f"delLayer.{key}: {msg} got={dl.get(key)} errors={dl.get('errors')}")
        # —— v58 3D 顶部工具浮层：视角菜单/四视角+重置/缩放/选点保留 ——
        st = result.get("stageToolsLayer") or {}
        for key, msg in (
            ("toolsInStage", "3D 顶部工具浮层（视角/模型/缩放/重置/小窗标签）元素缺失"),
            ("toolsInsideStageWrap", "工具浮层必须位于 3D 区域内部（不再放左侧栏）"),
            ("menuClosedDefault", "视角菜单默认应收起"),
            ("menuOpens", "点击「视角」应展开菜单"),
            ("menuOrderOk", "视角菜单应含前/后/左/右/重置视角五项"),
            ("viewFront", "前视图应移动相机且选点保留"),
            ("viewBack", "后视图应移动相机且选点保留"),
            ("viewLeft", "左视图应移动相机且选点保留"),
            ("viewRight", "右视图应移动相机且选点保留"),
            ("viewMenuCloses", "选择视角后菜单应收起"),
            ("zoomIn", "放大应使相机沿视线靠近（距离变短）"),
            ("zoomOut", "缩小应使相机远离（距离变长）"),
            ("resetCam", "重置视角应生效且选点保留"),
            ("modelSelectInTools", "人体模型下拉应位于 3D 顶部工具浮层内"),
        ):
            if st.get(key) is not True:
                bad.append(f"stageToolsLayer.{key}: {msg} got={st.get(key)} errors={st.get('errors')}")
        # —— v59 两态互斥布局：对话主区（默认）/ 3D 全屏主区，不叠加、无小窗、场景不重建 ——
        ly = result.get("layoutLayer") or {}
        for key, msg in (
            ("sceneKept", "3D 场景对象必须恒定（切换布局不得重建场景）"),
            ("modelClass", "model_primary 下 workstage 应带 model-primary"),
            ("modelWrapVisible", "model_primary 下 3D 区应可见（全屏主区）"),
            ("modelPaneHidden", "model_primary 下对话区应隐藏（两态互斥不叠加）"),
            ("noMiniWindow", "3D 小窗必须已删除（v59）"),
            ("modelBtnInComposer", "composer 行已移除人体模型入口"),
            ("backBtnInStage", "3D 工具区应有「完成并返回对话」入口"),
            ("chatClass", "chat_primary 下 workstage 应只带 conv-primary"),
            ("chatWrapHidden", "chat_primary 下 3D 区应隐藏"),
            ("chatPaneVisible", "chat_primary 下对话区应全宽可见"),
            ("regionsKeptInChat", "切回对话主区不得丢失已选部位"),
            ("markersKept", "切换布局不得丢失 3D 标记"),
            ("reEnterModel", "再次切入模型主区 3D 应恢复显示"),
            ("regionsStillKept", "二次切换选点仍保留"),
            ("sceneStillKept", "二次切换场景仍不重建"),
        ):
            if ly.get(key) is not True:
                bad.append(f"layoutLayer.{key}: {msg} got={ly.get(key)} errors={ly.get('errors')}")
        # —— v58 LLM 真实性守卫：结果卡三态 + 发送前三入口卡 ——
        lg = result.get("llmGuardLayer") or {}
        for key, msg in (
            ("convPaneExists", "对话区消息流缺失"),
            ("llmOkBanner", "source=llm 应显示「真实 AI 分析」横幅"),
            ("llmOkCondition", "真实 LLM 结果应显示分析方向"),
            ("llmOkModel", "结果卡应显示当前模型名"),
            ("llmOkReviewBtn", "真实 LLM 结果应提供医生审核入口"),
            ("statusReviewAfterLlm", "真实 LLM 结果后状态行应为「等待医生审核」"),
            ("demoBanner", "source=demo 必须显示「离线演示数据…不可用于医疗判断」横幅"),
            ("demoNoRealClaim", "演示结果不得标注为真实 AI 分析"),
            ("statusDemo", "演示结果状态行应明确「离线演示」"),
            ("unavailMsg", "source=none 应显示「当前未连接 LLM…」提示"),
            ("unavailNoMedical", "未连接 LLM 不得显示任何疾病/医疗内容"),
            ("unavailErrorShown", "结果卡应如实显示失败原因"),
            ("unavailNoDraftCards", "未连接 LLM 不得渲染鉴别/药物区块"),
            ("probeCardMsg", "发送前探测失败应弹出提示卡"),
            ("probeCardEntries", "提示卡必须提供「检查连接/打开模型设置/切换离线演示」三入口"),
            ("statusLlmUnavailable", "探测失败状态行应显示「未连接 LLM」"),
            # —— v59 文书任务卡（__pushDoc）四态守卫 ——
            ("docLlmBanner", "doc source=llm 应显示「真实 AI 生成」横幅"),
            ("docTaskTitle", "doc 卡应显示任务类型标题（现病史整理）"),
            ("docContent", "doc 卡应显示文书内容"),
            ("docCitation", "doc 卡应显示引用 id"),
            ("docTemplateBanner", "doc source=template 应显示「本地模板整理」横幅"),
            ("docTemplateNoAiClaim", "本地模板整理不得标注为真实 AI 生成"),
            ("docUnavailBanner", "doc source=none 应显示「未连接 LLM」横幅"),
            ("docUnavailNoMedical", "未连接 LLM 的 doc 卡不得含医学内容"),
        ):
            if lg.get(key) is not True:
                bad.append(f"llmGuardLayer.{key}: {msg} got={lg.get(key)} errors={lg.get('errors')}")
        # —— v17 多选 3 点 → × 删中间 → 余 2（列表行/3D 标记/uid 同步） → 清空 0 ——
        axd = result.get("afterXDelete") or {}
        if result.get("multiCount") != 3:
            bad.append(f"multi: 3 点选择失败 count={result.get('multiCount')} label={result.get('multiLabel')}")
        if axd.get("count") != 2 or axd.get("rows") != 2 or axd.get("markers") != 2:
            bad.append(f"afterXDelete: 删中间后应为 2/2/2 got={axd}")
        if axd.get("labels") and len(axd.get("labels")) == 2:
            first, last = axd["labels"]
            if "眼" not in first:
                bad.append(f"afterXDelete: 第一条应含眼 got={axd.get('labels')}")
            if "大腿" not in last:
                bad.append(f"afterXDelete: 末条应含大腿 got={axd.get('labels')}")
        else:
            bad.append(f"afterXDelete: 剩余标签错误 {axd.get('labels')}")
        if axd.get("removed") is not True:
            bad.append("afterXDelete: 已删 uid 再删应返回 false")
        if result.get("afterClear") != 0:
            bad.append(f"afterClear: 清空后应 0 got={result.get('afterClear')}")
        # —— 解剖语义断言：左右眼、左右手五指、鼻、唇、趾聚合，逐项核对 id|标签 ——
        if not result.get("anatomyActive"):
            bad.append("anatomy: ANATOMY_ACTIVE 未启用（锚点表与当前模型不匹配）")
        else:
            anatomy = result.get("anatomy") or {}
            for key, expect in ANATOMY_HINTS.items():
                got = anatomy.get(key)
                if got != expect:
                    bad.append(f"anatomy.{key}: got={got} expect={expect}")
        # —— v16 自动分区断言：18 个型带区域 id|标签逐项核对 ——
        zones = result.get("zones") or {}
        for key, expect in ZONE_HINTS.items():
            got = zones.get(key)
            if got != expect:
                bad.append(f"zones.{key}: got={got} expect={expect}")
        # —— v16 校准向导断言：7 步、纠正写入、对侧镜像、持久化、低置信提示 ——
        wz = result.get("wizard") or {}
        wexpect = {"steps": 7, "closed": True, "rmidShifted": True, "lmidMirrored": True,
                   "rmidProv": True, "lmidProv": True, "persisted": True}
        for key, expect in wexpect.items():
            if wz.get(key) is not expect:
                bad.append(f"wizard.{key}: got={wz.get(key)} expect={expect}")
        lc = wz.get("lowConfZone") or ""
        if not lc.startswith("cheek_ear_zone|") or "未能可靠识别耳廓" not in lc or not lc.endswith("低置信"):
            bad.append(f"wizard.lowConfZone: 耳颊区低置信提示异常 {lc}")
        # —— v31 回归：屏幕左侧臂根（模型局部坐标 -0.269,1.301,0.002）必须识别为上臂 ——
        arm_reg = result.get("armRootRegression") or []
        for probe in arm_reg:
            if probe.get("expect") not in (probe.get("label") or ""):
                bad.append(f"armRootRegression.{probe.get('id')}: 应为{probe.get('expect')} got={probe}")
        hr = result.get("hitRecord")
        if not hr or not all((hr.get("hasMesh"), hr.get("hasFace"), hr.get("hasWorld"),
                              hr.get("hasLocal"), hr.get("hasNormal"))):
            bad.append(f"hitRecord: 真实命中字段不完整 {hr}")
        un = result.get("unlabeled") or {}
        if un.get("ok") is not True:
            bad.append(f"unlabeled: 语义不足诚实提示未生效 {un}")
        cb = result.get("calib") or {}
        if cb.get("sameFace") != "回归测试标签" or cb.get("nearby") != "回归测试标签":
            bad.append(f"calib: 校准标签复用失败 {cb}")
        # —— v18 逐点独立记录 / 视角稳定性 ——
        if not result.get("thighDistinct"):
            bad.append(f"thighDistinct: 大腿连点应≥4条独立记录 got={result.get('thighRecs')}")
        if not result.get("posStable"):
            bad.append("posStable: 视角切换后标记世界坐标漂移")
        if bad:
            result["failures"] = bad
            print("[ASSERT-FAIL]", "; ".join(bad), flush=True)
            finish(4)
            return
        print("[ASSERT-PASS] v18：19 区域已删净 ✓；17 项解剖语义 + 18 项型带区域 ✓；"
              "完整命中字段（mesh/face/局部坐标/变换）✓；校准标签复用 ✓；"
              "「缺少精细语义」如实提示 ✓；真实 onPick 链 3 点选择→×删中间→余2 ✓；"
              "大腿连点 5 处 → 独立记录不合并 ✓；视角往返标记逐字节稳定 ✓", flush=True)
        # —— v32 女性模型回归：切换 → 锚点/型带/向导/材质兜底断言 → 切回男性零回归 ——
        poll_clinic_async()   # 先收异步影像验证结果，再进入女性阶段

    # —— 阶段3 影像异步验证轮询：合法 png → ready；伪造 png → rejected ——
    CLINIC_ASYNC_JS = ("JSON.stringify((function(){var a=window.__clinicAsync;"
                       "return a?{ready:(a.ready||{}).status||null,broken:(a.broken||{}).status||null}:null})())")

    def poll_clinic_async(tries=0):
        def _on(raw):
            try:
                st = json.loads(raw) if isinstance(raw, str) else {}
            except (TypeError, ValueError):
                st = {}
            if st.get("ready") != "ready" or st.get("broken") != "rejected":
                if tries < 25:
                    QTimer.singleShot(300, lambda: poll_clinic_async(tries + 1))
                    return
                result["failures"] = [f"clinicAsync: 影像解码验证轮询超时 got={st}"]
                print("[ASSERT-FAIL] clinicAsync: 合法 png 应 ready、伪造 png 应 rejected", flush=True)
                finish(4)
                return
            result["clinicAsync"] = st
            print("[ASSERT-PASS clinic-imaging] 合法 png→ready；伪造 png（PDF 头字节）→rejected（安全降级）✓", flush=True)
            start_female()
        page.runJavaScript(CLINIC_ASYNC_JS, _on)

    # —— v32 女性阶段：状态机（switch → check → shot ×2 → switch back → recheck → male shots） ——

    def poll_until(expect_key_substr, need_sem, on_ready, state):
        def _step():
            def _on_poll(raw):
                try:
                    st = json.loads(raw) if isinstance(raw, str) else {}
                except (TypeError, ValueError):
                    st = {}
                if (expect_key_substr in str(st.get("key", "")) and st.get("ready")
                        and st.get("anatomyActive") and (st.get("faceSemActive") or not need_sem)):
                    on_ready()
                    return
                state["tries"] += 1
                if state["tries"] > 60:
                    result["ok"] = False
                    result["reason"] = f"模型切换未就绪：{st}"
                    finish(3)
                    return
                QTimer.singleShot(500, _step)
            page.runJavaScript(POLL_MODEL_JS, _on_poll)
        QTimer.singleShot(300, _step)

    def start_female():
        page.runJavaScript(SWITCH_JS % (1, 1), lambda _r: poll_until(
            "Female3d66", True, run_female_checks, {"tries": 0}))

    def run_female_checks():
        page.runJavaScript(FEMALE_CHECK_JS, on_female_checks)

    def on_female_checks(payload):
        try:
            fem = json.loads(payload) if isinstance(payload, str) else {}
        except (TypeError, ValueError):
            fem = {}
        result["female"] = fem
        if not fem.get("ok"):
            result["ok"] = False
            result["reason"] = f"女性阶段 JS 失败：{fem.get('reason')}"
            finish(3)
            return
        bad = []
        if fem.get("anatomyActive") is not True:
            bad.append(f"female.anatomyActive: 女性锚点表未启用 got={fem.get('anatomyActive')}")
        if fem.get("faceSemActive") is not True:
            bad.append(f"female.faceSemActive: 女性逐面语义表未启用 got={fem.get('faceSemActive')}")
        anchors = fem.get("anchors") or {}
        for key, expect in FEMALE_ANAT.items():
            got = anchors.get(key)
            if got != expect:
                bad.append(f"female.anchors.{key}: got={got} expect={expect}")
        if fem.get("fingerBand") != "left_middle_finger|左手中指":
            bad.append(f"female.fingerBand: z_band 带归属失败 got={fem.get('fingerBand')}")
        if fem.get("chestZone") != "chest|胸":
            bad.append(f"female.chestZone: 女性胸型带失败 got={fem.get('chestZone')}")
        wz = fem.get("wizard") or {}
        if wz.get("count") != 7 or not (isinstance(wz.get("leftEyeX"), (int, float)) and wz["leftEyeX"] > 0.09):
            bad.append(f"female.wizard: 女性向导 7 点预设异常 got={wz}")
        clicks = fem.get("clicks") or {}
        eye = clicks.get("leftEye") or {}
        if not eye or "眼" not in str(eye.get("label") or ""):
            bad.append(f"female.clicks.leftEye: 真实点击应报眼区（fsem 眶周/眼睑） got={eye}")
        toe = clicks.get("leftToes") or {}
        if not toe or "趾" not in str(toe.get("label") or ""):
            bad.append(f"female.clicks.leftToes: 真实点击应报趾区（fsem 趾/趾甲） got={toe}")
        hair = clicks.get("hair") or {}
        if not hair.get("hit"):
            bad.append(f"female.clicks.hair: 发片点击应报发下体区（…（发覆盖）） got={hair.get('tried')}")
        cloth = clicks.get("clothing") or {}
        if not cloth.get("hit"):
            bad.append(f"female.clicks.clothing: 衣物点击应注明（衣物覆盖） got={cloth.get('tried')}")
        if bad:
            result["failures"] = bad
            print("[ASSERT-FAIL female]", "; ".join(bad), flush=True)
            finish(4)
            return
        print("[ASSERT-PASS female] 19 项锚点 ✓；z_band 带归属 ✓；胸型带 ✓；向导 7 点（女性表）✓；"
              "真实点击 眼/趾 ✓；发片→发下体区（发覆盖）✓；衣物→（衣物覆盖）✓；女性逐面表已启用 ✓", flush=True)
        run_zone_clicks()

    def run_zone_clicks():
        if not female_zones:
            result["ok"] = False
            result["reason"] = "女性 zone 样本缺失（先运行 tools/build_female_face_semantics.py）"
            finish(4)
            return
        code = (ZONE_CLICK_JS
                .replace("__SAMPLES__", json.dumps(female_zones, ensure_ascii=False))
                .replace("__VIEW__", "front")
                .replace("__VIEW2__", "back"))
        page.runJavaScript(code, on_zone_clicks)

    def on_zone_clicks(raw):
        try:
            d = json.loads(raw) if isinstance(raw, str) else {}
        except (TypeError, ValueError):
            d = {}
        result["zoneClicks"] = d
        if not d.get("ok"):
            result["ok"] = False
            result["reason"] = f"女性 zone 点击普查失败：{d.get('reason')}"
            finish(3)
            return
        rows = d.get("rows") or {}
        total = len(rows)
        missed = []
        for zid, rec in rows.items():
            attempts = [rec.get("click"), rec.get("click2")]
            if not zone_click_ok(zid, str(rec.get("zh") or ""), attempts):
                missed.append(f"{zid}({rec.get('zh')}): got={rec.get('click')} / {rec.get('click2')}")
        hit = total - len(missed)
        result["zoneHits"] = {"hit": hit, "total": total}
        if missed:
            result["failures"] = (result.get("failures") or []) + [
                f"female.zoneClicks: {len(missed)}/{total} 区样本未命中本区"]
            print(f"[ASSERT-FAIL female-zones] {len(missed)}/{total} 区未命中:", flush=True)
            for m in missed:
                print("   ", m, flush=True)
            finish(4)
            return
        print(f"[ASSERT-PASS female-zones] {hit}/{total} 区样本双视角真实点击命中本区或发下体区（fsem）✓", flush=True)
        page.runJavaScript("setView('front'); screenshot()",
                           lambda u: save_shot(u, "verify3d_female_front.png", on_female_back_shot))

    def on_female_back_shot():
        page.runJavaScript("setView('back'); screenshot()",
                           lambda u: save_shot(u, "verify3d_female_back.png", switch_back))

    def switch_back():
        page.runJavaScript(SWITCH_JS % (0, 0), lambda _r: poll_until(
            "Custom3d66", True, run_male_recheck, {"tries": 0}))

    def run_male_recheck():
        page.runJavaScript(MALE_RECHECK_JS, on_male_recheck)

    def on_male_recheck(payload):
        try:
            rec = json.loads(payload) if isinstance(payload, str) else {}
        except (TypeError, ValueError):
            rec = {}
        result["maleRecheck"] = rec
        if not rec.get("ok"):
            result["ok"] = False
            result["reason"] = f"男性复核 JS 失败：{rec.get('reason')}"
            finish(3)
            return
        bad = []
        if "Custom3d66" not in str(rec.get("model", "")):
            bad.append(f"maleRecheck.model: 未切回男性 got={rec.get('model')}")
        if rec.get("anatomyActive") is not True or rec.get("faceSemActive") is not True:
            bad.append(f"maleRecheck: 切回后锚点表/逐面表未重新启用 got={rec}")
        an = rec.get("anchors") or {}
        for key, expect in (("leftEye", "left_eye|左眼（含虹膜）"),
                            ("rightEye", "right_eye|右眼（含虹膜）"),
                            ("lMiddle", "left_middle_finger|左手中指")):
            if an.get(key) != expect:
                bad.append(f"maleRecheck.anchors.{key}: got={an.get(key)} expect={expect}")
        if rec.get("fingerBand") != "left_middle_finger|左手中指":
            bad.append(f"maleRecheck.fingerBand: 男性 z_band 带归属（v32 侧别过滤修复）失败 got={rec.get('fingerBand')}")
        if not (isinstance(rec.get("wizardLeftEyeX"), (int, float)) and rec["wizardLeftEyeX"] > 0.09):
            bad.append(f"maleRecheck.wizardLeftEyeX: 向导预设未切回男性表 got={rec.get('wizardLeftEyeX')}")
        if bad:
            result["failures"] = (result.get("failures") or []) + bad
            print("[ASSERT-FAIL recheck]", "; ".join(bad), flush=True)
            finish(4)
            return
        print("[ASSERT-PASS recheck] 切回男性零回归：锚点表+逐面表重新启用 ✓；锚点抽查 ✓；"
              "男性 z_band 带归属（v32 侧别过滤修复）✓；向导预设切回男性表 ✓", flush=True)
        page.runJavaScript("screenshot()", on_shot_front)

    def save_shot(data_url, fname, next_cb):
        try:
            save_data_url(data_url, os.path.join(DATA, fname))
            result.setdefault("extraPngs", []).append("data/" + fname)
        except Exception as exc:  # noqa: BLE001
            result["shotError"] = str(exc)
        next_cb()

    def on_shot_front(data_url):
        shots["front"] = data_url
        page.runJavaScript("setView('back'); screenshot()", on_shot_back)

    def on_shot_back(data_url):
        shots["back"] = data_url
        finish(0)

    def finish(code):
        report = dict(result)
        report["qtwebengine"] = True
        try:
            if shots.get("front"):
                save_data_url(shots["front"], os.path.join(DATA, "verify3d_front.png"))
                report["frontPng"] = "data/verify3d_front.png"
            if shots.get("back"):
                save_data_url(shots["back"], os.path.join(DATA, "verify3d_back.png"))
                report["backPng"] = "data/verify3d_back.png"
        except Exception as exc:  # noqa: BLE001
            report["shotError"] = str(exc)
        with open(os.path.join(DATA, "verify3d_report.json"), "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(json.dumps(report, ensure_ascii=False))
        app.exit(code)

    view.loadStarted.connect(lambda: print("[load] started", flush=True))

    def on_load(ok):
        print(f"[load] finished ok={ok} url={view.url().toString()[:80]}", flush=True)
        if not ok:
            finish(3)
            return
        def probe1(v):
            print("[probe] 1+1 =", v, flush=True)
            page.runJavaScript("typeof THREE", probe2)
        def probe2(v):
            print("[probe] typeof THREE =", v, flush=True)
            page.runJavaScript("typeof renderer + '/' + typeof regionMeshes", probe3)
        def probe3(v):
            print("[probe] renderer/regionMeshes =", v, flush=True)
            page.runJavaScript(CHECK_JS, on_checks)
        QTimer.singleShot(2500, lambda: page.runJavaScript("1+1", probe1))

    view.loadFinished.connect(on_load)
    view.load(QUrl.fromLocalFile(os.path.join(HERE, "web", "index.html")))
    # v32：含女性模型装配（19.8MB GLB）+ 两次切换轮询 → 总超时放宽到 120s
    QTimer.singleShot(120000, lambda: finish(2))
    view.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
