/* 肤智（DermAgent）— 3D 人体位置选择面板（v19：逐三角面语义表 + 底视 + 诚实缺语义）
 *
 * v19 核心：离线逐三角面语义表（face-semantics.json，tools/build_face_semantics.py 生成）
 *   命中解析链：人工校准 → 逐面语义表（mesh+faceIndex 精确查表）→ 解剖锚点（眼/五指）
 *   → 型带分区 → 「当前 GLB 缺少该部位的可靠语义」。
 *   表按「mesh 运行时名 + faceIndex 区段」索引，覆盖全部 22 个渲染 mesh / 129.55 万三角面；
 *   被遮挡/未建模部位（耳廓、裸露会阴）如实标注遮挡来源，不伪造。
 * 功能：真实人体模型（3d66 男/女两款，v64-P6 精简；加载失败降级程序化人台）·
 *       递归 Raycaster（intersectObject(modelRoot, true)）命中真实渲染网格最近的可见三角面，
 *       标记/辅助物/地面/外壳（userData.noPick）不参与命中 ·
 *       每次点击保存完整真实命中数据：object/faceIndex/世界+局部+模型局部坐标/uv/normal/
 *       instanceId/Mesh-Node 链/Bone-Skin（无则如实记 null）/模型当前变换 ·
 *       输出格式「解剖区域 + 表面方位 + 相对位置 + 局部坐标」，如「左大腿，前侧，中段偏内，局部坐标 (…)」·
 *       语义不足时明确报「当前 GLB 缺少该部位的可靠语义」（仍保存坐标与方位，不伪造解剖名）·
 *       每次点击 = 独立记录 + 独立标记，绝不合并/覆盖/去重；删除仅经列表 ×（清空全部需确认）·
 *       列表行可展开（精确坐标/命中信息），支持逐点备注；调试模式显示命中 Mesh/faceIndex/表项 ·
 *       视角预设（前/后/左/右/底） · 截图入报告 · 导入自定义 GLB · 双通道上报。
 * 定位：3D 模型仅是位置选择器，输出是精细部位标签，不是诊断结论。
 * 契约：与 Python/Qt 桥及 WebUI 均按「全量已选列表」上报（regions 紧凑 + regions_full 完整）。
 */
/* global THREE, OrbitControls */

var SKIN = 0xc68863;
var HIGHLIGHT = 0xff4d3d;
/* 模型描述表（男女切换）：file=GLB 路径，name=界面显示名。
 * v64-P6：只保留 3d66 男/女两款，Xbot/Michelle/CesiumMan 已删（含其 GLB 文件）。
 * MODEL_FILES 为派生数组（兼容锚点/逐面表的 model 字段校验）。modelIndex=0 男性为默认。 */
var MODELS = [
  { file: "vendor/models/Custom3d66.glb",  name: "3d66 男性模型", zh: "男 · 3d66",  en: "Male · 3d66" },
  { file: "vendor/models/Female3d66.glb",  name: "3d66 女性模型", zh: "女 · 3d66",  en: "Female · 3d66" }
];
var MODEL_FILES = MODELS.map(function (m) { return m.file; });
var modelIndex = 0;
var ACTIVE_MODEL_KEY = "";
var modelRoot = null;
var fitInfo = null;         // 当前模型实测包围盒（适配后世界坐标），区域映射按此归一化，不再用固定偏移
var modelRest = null;       // 模型静止基准变换（v18：装配完成后固化，报告模型当前旋转/缩放/平移）
/* 人工区域必须绑定到完整 GLB 指纹和可复现的几何适配版本。
 * sha256 为空时只允许显示模型，不激活已保存的人工真值。 */
var ANNOTATION_SCHEMA_VERSION = 4;
var TOPOLOGY_VERSION = "three-face-index-v1";
var FIT_VERSION = "normalized-height-5-v2";
var MODEL_IDENTITY = { model_key: "unknown", file_name: "", sha256: "", topology_version: TOPOLOGY_VERSION,
  fit_version: FIT_VERSION, mesh_faces: {}, ready: false };
var PENDING_MANUAL_DOCUMENT = null;
var markerPool = [];
var regionDrawGroup = null;
var selectedRegions = [];   // 多选：[{uid, id, label, pos, confidence, note, desc, hit}]（uid 供列表 × 精确删除）
var selSeq = 0;               // 递增 uid
var DEBUG_MODE = false;     // 调试模式：列表展开与顶部标签显示命中 Mesh/faceIndex/坐标
var regionMeshes = {};      // 程序化人台部件名（仅 buildProceduralBody 用，非点击命中依据）
var scene, camera, renderer, controls, raycaster, stageEl;
var bridge = (!window.qt && window.__browserBridge) ? window.__browserBridge() : null;
/* v75：浏览器模式 → REST 桥（browser-bridge.js）；桌面端仍为 null，QWebChannel 就绪后赋值 */
/* 解剖语义映射表（config/anatomy-labels.json 同步副本）：眼/五指/趾/唇/鼻等精细部位。
 * 来源声明：本 GLB 无骨骼、mesh 名为 3ds Max 自动编号，锚点由顶点几何离线聚类生成（见各 provenance）。
 * 换模型或重新导出 GLB 后必须重新生成该表，否则精细命中退回型带/未识别（不伪造结果）。 */
var ANATOMY = null;           // {entries:[{anatomical_label, display_zh, world_xyz, confidence, aggregate_of?}], ...}
var ANATOMY_ACTIVE = false;   // 锚点表是否与当前模型匹配（按模型文件名+fitInfo 尺寸校验）
var ANATOMY_TOL = 0.055;      // 锚点匹配容忍半径（世界单位；手指定 0.045，躯干级忽略）
/* 校准标注层：用户人工标注的真实命中映射（"真实命中 + 可校准标注"方案）。
 * calibrations[modelFile][key] = {label, mesh_name, face_index, local_xyz, world_xyz, uv, normal, created_at}
 * key = mesh名:faceIndex（同 mesh 同面必同区域）；查询时同 mesh 内按局部坐标最近邻（<2cm）复用。 */
var CALIB = { byModel: {}, mode: false };
var MANUAL_REGIONS = { schema_version: 3, model: {}, regions: {}, history: [], llm_subregions: {} };
var MANUAL_FACE_OWNER = {};
var currentViewName = "front";
var REGION_DRAW = { active: false, view: "front", points: [], hits: [], previewFaces: [], preview: false };
var LLM_SUBREGIONS = {};
var REGION_LABELS = { head_face: "头面部", neck: "颈部", chest: "胸部", abdomen: "腹部", back_upper: "上背", back_lower: "下背", shoulder_l: "左肩", shoulder_r: "右肩", upper_arm_l: "左上臂", upper_arm_r: "右上臂", elbow_l: "左肘", elbow_r: "右肘", forearm_l: "左前臂", forearm_r: "右前臂", wrist_l: "左腕", wrist_r: "右腕", hand_l: "左手", hand_r: "右手", thigh_l: "左大腿", thigh_r: "右大腿", knee_l: "左膝", knee_r: "右膝", calf_l: "左小腿", calf_r: "右小腿", foot_l: "左足", foot_r: "右足" };
/* 实测校正点按模型隔离：坐标在各模型归一化局部系（身高 5，y −2.5..2.5），换模型互不适用。
 * 键 = 模型文件名（modelKey() 取末段）；当前模型无条目 → 返回空表（不把 A 模型的点套到 B 模型）。 */
var MEASURED_CORRECTIONS_BY_MODEL = {
  "Custom3d66.glb": [
    { point: [0.454, 1.156, 0.025], standard_id: "forearm_l", label: "左前臂" },
    { point: [0.472, 1.147, 0.027], standard_id: "forearm_l", label: "左前臂" },
    { point: [0.481, 1.133, 0.029], standard_id: "forearm_l", label: "左前臂" },
    { point: [0.504, 1.117, 0.035], standard_id: "forearm_l", label: "左前臂" },
    { point: [0.346, 1.245, -0.009], standard_id: "cubital_fossa_l", label: "左肘窝" },
    { point: [0.370, 1.227, -0.093], standard_id: "elbow_l", label: "左肘" },
    { point: [0.258, 1.315, -0.116], standard_id: "upper_arm_l", label: "左上臂" },
    { point: [0.233, 1.300, -0.068], standard_id: "upper_arm_l", label: "左上臂" },
    { point: [0.281, 1.258, -0.078], standard_id: "upper_arm_l", label: "左上臂" },
    { point: [0.223, 1.374, -0.116], standard_id: "upper_arm_l", label: "左上臂" },
    { point: [0.218, 1.345, -0.095], standard_id: "upper_arm_l", label: "左上臂" },
    { point: [0.316, 1.222, -0.055], standard_id: "elbow_l", label: "左肘" },
    { point: [0.200, 1.421, 0.019], standard_id: "shoulder_l", label: "左肩" },
    { point: [0.577, 0.997, 0.104], standard_id: "left_thumb_finger", label: "左手拇指", tol: 0.028 },
    { point: [-0.573, 0.987, 0.117], standard_id: "right_thumb_finger", label: "右手拇指", tol: 0.028, noMirror: true }
  ],
  "Female3d66.glb": []      // 女性模型校正点：tools/probe_female.py 实测后填入
};
/* 右侧 = 左侧的完全镜像（模型左右对称，用户确认）：加载时自动生成 x<0 镜像校正点。
 * 左侧后续再修正时右侧自动跟随；个别不对称处可在条目上加 noMirror:true 排除镜像。 */
Object.keys(MEASURED_CORRECTIONS_BY_MODEL).forEach(function (mk) {
  var list = MEASURED_CORRECTIONS_BY_MODEL[mk];
  list.slice().forEach(function (c) {
    var isL = c.standard_id.slice(-2) === "_l";
    if (c.noMirror || (!isL && !c.mirror)) return;
    list.push({
      point: [-c.point[0], c.point[1], c.point[2]],
      standard_id: isL ? c.standard_id.slice(0, -2) + "_r" : c.standard_id.replace(/^left_/, "right_"),
      label: c.label.replace("左", "右"),
      tol: c.tol
    });
  });
});
var MEASURED_ARM_CORRECTIONS = MEASURED_CORRECTIONS_BY_MODEL["Custom3d66.glb"];
var MEASURED_FACE_CORRECTIONS = {
  "Obj3d66-19298115-6-141_5:1610": { standard_id: "elbow_l", label: "左肘" }
};
var LAST_HIT
/* v19 逐三角面语义表：{model, meshes:{meshName:{faces,segments:[[s,e,zoneId]…]}}, labels:{zoneId:{zh,conf,cat,note?}}}
 * 离线生成（tools/build_face_semantics.py），按「命中 mesh 运行时名 + faceIndex」精确查表。 */
var FACE_SEM = null;
var FACE_SEM_ACTIVE = false;  // 表与当前模型匹配（文件名 + mesh 名集合 + 面数校验）；点击语义唯一来源
/* v24 注记：Anatomy Atlas（tools/build_anatomy_atlas.py 产物，~600MB）未接线，
 * 运行时不加载；如未来接线必须注意其预计算 surface 方位基于几何法线，
 * 与本文件 recordHit 的可见性校正口径不一致，不能直接替代。 */
var ATLAS = null;
var ATLAS_ACTIVE = false;
var ATLAS_BY_MESH_NAME = {};
var ATLAS_MIN_CONFIDENCE = 0.70;

/* ================= 场景 ================= */

function initScene() {
  stageEl = document.getElementById("stage");
  scene = new THREE.Scene();
  /* 浅色主题：背景与雾同色（原深色 0x0d1118） */
  scene.background = new THREE.Color(0xf3f4f6);
  scene.fog = new THREE.Fog(0xf3f4f6, 14, 30);
  camera = new THREE.PerspectiveCamera(38, 1, 0.1, 100);
  camera.position.set(0, 0.35, 9);
  renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
  renderer.outputEncoding = THREE.sRGBEncoding;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.1;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  stageEl.appendChild(renderer.domElement);
  controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.minDistance = 3;
  controls.maxDistance = 18;

  scene.add(new THREE.HemisphereLight(0xbfd7ff, 0xb9afa4, 0.9));
  var key = new THREE.DirectionalLight(0xfff1e0, 2.1);
  key.position.set(4.5, 7, 6.5);
  key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  key.shadow.camera.left = -5; key.shadow.camera.right = 5;
  key.shadow.camera.top = 6; key.shadow.camera.bottom = -4;
  key.shadow.bias = -0.0004;
  key.shadow.radius = 6;
  scene.add(key);
  var fill = new THREE.DirectionalLight(0x9db8e8, 0.5);
  fill.position.set(-6, 2, 3);
  scene.add(fill);
  var rim = new THREE.DirectionalLight(0x88aaff, 1.1);
  rim.position.set(-2, 4, -7);
  scene.add(rim);

  var ground = new THREE.Mesh(new THREE.CircleGeometry(6, 48), new THREE.ShadowMaterial({ opacity: 0.22 }));
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -2.62;
  ground.receiveShadow = true;
  scene.add(ground);

  raycaster = new THREE.Raycaster();
  loadCalibrations();
  loadCalibrationsLocal();
  loadManualRegions();
  loadFaceSemantics();    // v19：逐面语义表（先于或后于模型加载均可，validateFaceSemantics 双向对齐）
  onResize();
  window.addEventListener("resize", onResize);
  /* 标签行增减改变的是 stage-wrap 高度（flex 布局），不触发 window resize —— ResizeObserver 兜底，
     否则画布保持旧尺寸、底部被 stage-wrap 裁掉（放大躯干后脚不可见） */
  if (window.ResizeObserver) new ResizeObserver(onResize).observe(stageEl);
  renderer.domElement.addEventListener("click", onPick);
  renderer.domElement.addEventListener("pointerdown", function (e) {
    window.__pickDown = [e.clientX, e.clientY];   // v74-fix：区分拖拽旋转与点击（拖拽后松开不误删标记）
    lassoDown(e);                                 // v74：圈选模式
  });
  renderer.domElement.addEventListener("pointermove", lassoMove);
  renderer.domElement.addEventListener("pointerup", lassoUp);
  var _lassoBtn = document.getElementById("lassoBtn");
  if (_lassoBtn) _lassoBtn.addEventListener("click", function () { setLasso(!LASSO.active); });
  loadAnatomyConfig();   // 解剖锚点表（先于或后于模型加载均可，validateAnatomy 会双向对齐）
  loadHumanModel();
  animate();
}

/* ================= 模型加载链与装配 ================= */

function boneBox() {
  if (!modelRoot) return null;
  modelRoot.updateWorldMatrix(true, true);
  var bb = new THREE.Box3();
  var n = 0;
  modelRoot.traverse(function (o) {
    if (o.isBone) { n++; bb.expandByPoint(o.getWorldPosition(new THREE.Vector3())); }
  });
  return n >= 2 ? bb : null;
}

/* v18：模型重载后标记恢复。同一模型 → 按模型局部坐标（worldToLocal 对 modelRoot）重新贴回
 * 真实表面（局部坐标在模型系恒定，重载只改世界摆放）；不同模型 → 局部坐标无意义，移除该记录。
 * 恢复后 pos 为新世界坐标，渲染标记与上报数据同步刷新。 */
function restoreMarkersAfterReload(prevModel) {
  if (!modelRoot) return;
  var same = prevModel === modelKey();
  var kept = [];
  selectedRegions.forEach(function (x) {
    if (!same || !x.hit || !x.hit.model_local_xyz) return;   // 非同模型/无局部坐标 → 丢弃
    var lp = new THREE.Vector3(x.hit.model_local_xyz[0], x.hit.model_local_xyz[1], x.hit.model_local_xyz[2]);
    var wp = modelRoot.localToWorld(lp);
    var p = wp.clone();                                       // 世界落点
    // 贴回真实表面：从上方/原命中方向微调到最近表面（局部坐标精度内直接使用）
    x.pos = p;
    x.hit.world_xyz = [+p.x.toFixed(5), +p.y.toFixed(5), +p.z.toFixed(5)];
    kept.push(x);
  });
  selectedRegions = kept;
  renderSelection();
}

function computeSha256Hex(buffer) {
  if (!buffer || !window.crypto || !window.crypto.subtle) return Promise.resolve("");
  return window.crypto.subtle.digest("SHA-256", buffer).then(function (digest) {
    return Array.prototype.map.call(new Uint8Array(digest), function (b) {
      return b.toString(16).padStart(2, "0");
    }).join("");
  }).catch(function () { return ""; });
}

function setModelIdentity(sourceName, sha256) {
  var meshes = {};
  if (modelRoot) modelRoot.traverse(function (o) {
    if (!o.isMesh || !o.geometry) return;
    meshes[o.name] = (o.geometry.index ? o.geometry.index.count :
      (o.geometry.attributes.position ? o.geometry.attributes.position.count : 0)) / 3;
  });
  MODEL_IDENTITY = {
    model_key: modelKey(),
    file_name: sourceName || modelKey().split("/").pop(),
    sha256: sha256 || "",
    topology_version: TOPOLOGY_VERSION,
    fit_version: FIT_VERSION,
    mesh_faces: meshes,
    ready: !!sha256
  };
  MANUAL_REGIONS.model = Object.assign({}, MODEL_IDENTITY);
  rebuildManualFaceIndex();
  if (!MODEL_IDENTITY.ready) console.warn("[manual-regions] 缺少完整模型 SHA-256，人工区域保持停用");
}

function activatePendingManualDocument() {
  if (!PENDING_MANUAL_DOCUMENT || !MODEL_IDENTITY.ready) return;
  var doc = PENDING_MANUAL_DOCUMENT;
  PENDING_MANUAL_DOCUMENT = null;
  if (doc.model && doc.model.sha256 === MODEL_IDENTITY.sha256 &&
      doc.model.topology_version === TOPOLOGY_VERSION && doc.model.fit_version === FIT_VERSION) {
    MANUAL_REGIONS = doc;
    MANUAL_REGIONS.regions = MANUAL_REGIONS.regions || MANUAL_REGIONS.manual_regions || {};
    LLM_SUBREGIONS = MANUAL_REGIONS.llm_subregions || {};
    rebuildManualFaceIndex();
  } else {
    console.warn("[manual-regions] 模型指纹/拓扑/拟合版本不匹配，拒绝激活导入数据");
  }
}

function registerHuman(gltf, sourceName, sha256, modelKeyOverride) {
  var prevModel = modelKey();
  ACTIVE_MODEL_KEY = modelKeyOverride || ((typeof MODEL_FILES !== "undefined" && MODEL_FILES[modelIndex]) ? MODEL_FILES[modelIndex] : "unknown");
  if (modelRoot) { scene.remove(modelRoot); modelRoot = null; }
  // v18：重载前模型名（同模型 → 标记按局部坐标贴回）
  modelRoot = gltf.scene;
  // 骨骼实测适配（Mixamo 骨架常带巨大缩放偏移，geometry Box3 是假象）
  var box = boneBox() || new THREE.Box3().setFromObject(modelRoot);
  var size = box.getSize(new THREE.Vector3());
  if (Math.max(size.x, size.z) > size.y * 1.5) {          // 躺姿扶正
    modelRoot.rotation.x = -Math.PI / 2;
    box = boneBox() || box;
    size = box.getSize(new THREE.Vector3());
  }
  var scale = 5.0 / size.y;                                // 统一身高 5.0
  modelRoot.scale.setScalar(scale);
  box = boneBox() || box;
  var center = box.getCenter(new THREE.Vector3());
  /* v28 修复：box 是缩放前的原始坐标，而 position 在 scale 之后生效（world = s·local + pos），
   * 平移量必须乘 scale，否则中线偏移被放大 (s−1)·c（男性原始中心≈0 一直未暴露；
   * 女性模型原始中心 0.009 → 中线偏 0.25、脚底悬空 15mm）。 */
  modelRoot.position.x -= center.x * scale;
  modelRoot.position.z -= center.z * scale;
  modelRoot.position.y += (-2.5 - box.min.y * scale);      // 骨骼实测落地（脚底 y=−2.5）
  modelRoot.traverse(function (o) {
    if (o.isMesh) {
      o.castShadow = true;
      o.frustumCulled = false;
      o.userData.pickable = true;   // 可见网格全部可点（最近交点 → 空间带映射区域）
      // 高金属度材质无环境贴图会渲染全黑 → 压低金属度让贴图漫反射显色
      var mats = Array.isArray(o.material) ? o.material : [o.material];
      mats.forEach(function (mt) {
        if (!mt) return;
        if (mt.metalness !== undefined) mt.metalness = Math.min(mt.metalness, 0.05);
        if (mt.roughness !== undefined) mt.roughness = Math.min(mt.roughness, 0.85);
      });
    }
  });
  fitInfo = new THREE.Box3().setFromObject(modelRoot);   // 适配后实测包围盒（世界坐标）
  setModelIdentity(sourceName, sha256);
  activatePendingManualDocument();
  modelRest = {                                           // v18：固化静止基准变换（命中报告用）
    rotation: modelRoot.rotation.toArray().slice(0, 3),
    scale: modelRoot.scale.toArray().slice(0, 3),
    position: modelRoot.position.toArray().slice(0, 3)
  };
  scene.add(modelRoot);
  restoreMarkersAfterReload(prevModel);   // v18：同模型重载后标记按模型局部坐标贴回原表面（非同模型记录移除）
  clearSelection();   // 模型重载后旧世界坐标标记失效：清空选区（含标记与文本）
  validateAnatomy(sourceName);
  validateFaceSemantics(sourceName);   // v19：逐面语义表匹配性校验
  setView("front");
  var hint = document.getElementById("hint");
  if (hint) hint.textContent = "人体模型：" + sourceName + " · 拖拽旋转 · 点击选择位置（每次点击=独立记录，列表可展开/备注/× 删除）；找不到部位时点「重置」复位视角";
}

/* —— 解剖语义层：加载锚点表，并在模型装配后校验其是否适用于当前模型 ——
 * 多模型：优先取「anatomy-labels-<模型名>.json」专用表（如 Female3d66），404 回落通用
 * anatomy-labels.json（男性 Custom3d66 出厂表）。切换模型时重新拉取，validateAnatomy 双保险。 */
function semanticsFileFor(prefix) {
  var base = (MODEL_FILES[modelIndex] || "").split("/").pop().replace(/\.glb$/i, "");
  return base && base !== "Custom3d66" ? prefix + "-" + base + ".json" : prefix + ".json";
}

function fetchFirstJson(urls) {
  return new Promise(function (resolve) {
    var i = 0;
    function tryNext() {
      if (i >= urls.length) { resolve(null); return; }
      fetch(urls[i], { cache: "no-cache" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) { resolve(j || (i++, tryNext())); })
        .catch(function () { i += 1; tryNext(); });
    }
    tryNext();
  });
}

function loadAnatomyConfig() {
  var f = semanticsFileFor("anatomy-labels");
  var urls = f === "anatomy-labels.json" ? [f] : [f, "anatomy-labels.json"];
  fetchFirstJson(urls).then(function (j) { ANATOMY = j; if (ANATOMY) validateAnatomy(null); });
}

/* ================= v19：逐三角面语义表（face-semantics.json） =================
 * 加载 → 模型装配后校验（模型文件名 + 逐 mesh 面数全匹配才启用）→ 命中时精确查表。
 * 表由 tools/build_face_semantics.py 离线生成（几何聚类，provenance 见文件 honesty_note）。
 * 多模型：优先「face-semantics-<模型名>.json」，404 回落通用表（不匹配会自动停用）。 */
function loadFaceSemantics() {
  var f = semanticsFileFor("face-semantics");
  var urls = f === "face-semantics.json" ? [f] : [f, "face-semantics.json"];
  fetchFirstJson(urls).then(function (j) { FACE_SEM = j; if (FACE_SEM) validateFaceSemantics(null); });
}

/* 表与当前模型匹配性校验：模型文件名匹配 + 每个渲染 mesh 的实际面数与表一致。 */
function validateFaceSemantics(sourceName) {
  FACE_SEM_ACTIVE = false;
  if (!FACE_SEM || !FACE_SEM.meshes || !modelRoot) return;
  if (sourceName && FACE_SEM.model && sourceName.indexOf(FACE_SEM.model.split(".").shift().slice(0, 8)) === -1 &&
      MODEL_FILES[modelIndex].indexOf(FACE_SEM.model) === -1) return;
  var okMeshes = 0, total = 0;
  modelRoot.traverse(function (o) {
    if (!o.isMesh) return;
    total++;
    var ent = FACE_SEM.meshes[o.name];
    if (!ent) return;
    var n = (o.geometry.index ? o.geometry.index.count : o.geometry.attributes.position.count) / 3;
    if (Math.abs(n - ent.faces) < 0.5) okMeshes++;
  });
  // 全部渲染 mesh 都在表内且面数一致才启用（防错表错模型）
  FACE_SEM_ACTIVE = total > 0 && okMeshes === total;
  console.log("[face-semantics] 校验 " + okMeshes + "/" + total + " mesh 匹配 → " +
              (FACE_SEM_ACTIVE ? "启用" : "未启用（不伪造，退回型带/缺语义提示）"));
  return FACE_SEM_ACTIVE;
}

/* 命中 → 逐面语义表查询。hit: recordHit 结果（mesh_name + face_index）。
 * 返回 {id, label, confidence, provenance, note?} 或 null（表未启用/未收录）。 */
function faceSemFor(hit) {  if (!FACE_SEM_ACTIVE || !FACE_SEM || !hit) return null;
  var ent = FACE_SEM.meshes[hit.mesh_name];
  if (!ent || !ent.segments || !ent.segments.length) return null;
  var fi = hit.face_index;
  if (fi == null || fi < 0 || fi >= ent.faces) return null;
  // 区段查找：segments 有序 → 二分
  var lo = 0, hi = ent.segments.length - 1, seg = null;
  while (lo <= hi) {
    var mid = (lo + hi) >> 1;
    var s = ent.segments[mid];
    if (fi < s[0]) hi = mid - 1;
    else if (fi >= s[1]) lo = mid + 1;
    else { seg = s; break; }
  }
  if (!seg) return null;
  var lab = FACE_SEM.labels[seg[2]];
  if (!lab) return null;
  return { id: seg[2], label: lab.zh, confidence: lab.conf,
           provenance: "逐面语义表[" + hit.mesh_name + " #" + fi + " → " + seg[2] + "]",
           note: lab.note || null };
}

/* v28 多模型：命名材质兜底（女性 3d66 模型的发片/衣物是命名材质）。
 * 发片不是皮肤——垂到胸前的发束按型带会误报「胸」，按材质名如实报「头发」；
 * 衣物覆盖处区域仍按体表位置分区，但结果注明覆盖。仅当逐面表未命中时走到
 * （男性模型 fsem 全覆盖，永不会触发此层，行为字节级不变）。 */
var NAMED_COVER_MATERIALS = {
  hair: ["Back", "BackUnder", "Band", "Bangs1", "Bangs2", "Cap", "Front1", "Front2", "Side1", "Side2"],
  clothing: ["shirt", "trims"]
};
function coveringMaterialFor(obj) {
  if (!obj) return null;
  var mats = Array.isArray(obj.material) ? obj.material : [obj.material];
  for (var i = 0; i < mats.length; i++) {
    var mn = mats[i] && mats[i].name;
    if (!mn) continue;
    if (NAMED_COVER_MATERIALS.hair.indexOf(mn) !== -1) return "hair";
    if (NAMED_COVER_MATERIALS.clothing.indexOf(mn) !== -1) return "clothing";
  }
  return null;
}

/* 锚点表与当前模型匹配性校验：模型文件 + fitInfo 尺寸双校验（误差 >2% 判不适用）。
 * 校验通过才启用锚点级精细命中；否则点击走型带分区或如实报「无法可靠识别」，不伪造精细结果。 */
function validateAnatomy(sourceName) {
  ANATOMY_ACTIVE = false;
  if (!ANATOMY || !fitInfo) return;
  if (sourceName && ANATOMY.model && MODEL_FILES[modelIndex].indexOf(ANATOMY.model.split("/").pop()) === -1) return;
  var size = fitInfo.getSize(new THREE.Vector3());
  var ref = ANATOMY.fitInfo;
  if (!ref) return;
  var rx = ref.max[0] - ref.min[0], ry = ref.max[1] - ref.min[1], rz = ref.max[2] - ref.min[2];
  if (Math.abs(size.x - rx) / rx > 0.02 || Math.abs(size.y - ry) / ry > 0.02 || Math.abs(size.z - rz) / rz > 0.02) return;
  ANATOMY_ACTIVE = true;
}

/* 世界坐标 → 解剖标签。手指：锚点距离(≤3.5cm)优先；锚点间落点按 z_band 全带归属；
 * 两锚点距离差 <5mm 且都 <12mm → 指间过渡区，报「无法可靠区分」（不伪造单指）。
 * 返回 {id, label, confidence, provenance, ambiguous?} 或 null。 */
function anatomyFromPoint(p) {
  if (!ANATOMY_ACTIVE || !ANATOMY || !ANATOMY.entries) return null;
  // 锚点表为模型装配后的世界坐标（与 fitInfo 同系）；本应用旋转/缩放作用于相机，
  // 模型根节点静止 → 世界坐标匹配在任意视角下恒成立，无需局部空间换算
  var best = null, bestD = Infinity;
  var calibHit = LAST_HIT ? calibrationFor(LAST_HIT) : null;   // 人工校准优先于几何锚点
  var fingerCandidates = [];           // z_band 命中的手指候选（含锚点距离，供歧义判定）
  for (var i = 0; i < ANATOMY.entries.length; i++) {
    var e = ANATOMY.entries[i];
    var dx = p.x - e.world_xyz[0], dy = p.y - e.world_xyz[1], dz = p.z - e.world_xyz[2];
    var d = Math.sqrt(dx * dx + dy * dy + dz * dz);
    var isFinger = e.anatomical_label.indexOf("_finger") !== -1;
    // 眼锚点在眼球中心，真实点击落在眼睑/眼眶表面（脸 mesh），表面到锚点可达 5.5cm+；
    // 7.5cm 覆盖眼睑带，超出即如实报「未标注」（不把整个头都算眼）
    var isEye = e.anatomical_label.indexOf("_eye") !== -1;
    var tol = isFinger ? 0.035 : (isEye ? 0.075 : ANATOMY_TOL);
    if (d < tol && d < bestD) { best = e; bestD = d; }
    if (isFinger && p.z >= e.z_band[0] - 0.008 && p.z <= e.z_band[1] + 0.008 &&
        Math.abs(p.y - e.world_xyz[1]) < 0.09) {
      // 侧别过滤：left 手 x>0 / right 手 x<0（x>0=解剖学左，与锚点表 world_xyz 一致）；
      // 且落点需在指尖板层（|x| 距尖端 7.5cm 内），排除躯干/大腿等恰落同一 z 带的远端点。
      // tip_x 为带符号指尖 x（同 world_xyz[0]）。
      var sgn = e.side === "left" ? 1 : -1;
      if (p.x * sgn > 0 && Math.abs(p.x) >= Math.abs(e.tip_x) - 0.075) fingerCandidates.push({ e: e, d: d });
    }
  }
  var pick = best, ambiguous = null;
  if (fingerCandidates.length) {
    fingerCandidates.sort(function (a, b) { return a.d - b.d; });
    var f0 = fingerCandidates[0], f1 = fingerCandidates[1];
    // 指间过渡：两点带内且锚距都 <30mm 且更近者与次近者差 <12mm → 无法可靠区分（不伪造单指结论）。
    // 阈值依据：相邻指锚点间距实测 12–36mm（食/中 16mm），z_band 边界带内落点到两侧锚点
    // 各为「间距一半 ± 深度差」，锚距通常 8–25mm——旧阈值（12mm/5mm）在此几何下不可达（死代码）。
    if (f1 && f0.d < 0.030 && (f1.d - f0.d) < 0.012) {
      ambiguous = f0.e.display_zh + " / " + f1.e.display_zh;
      pick = f0.e;
    } else if (!best || bestD > f0.d || best.anatomical_label.indexOf("_finger") === -1) {
      // 带归属优先：除更近的手指锚点外，手指带命中不被其他部位（含手部聚合）覆盖
      pick = f0.e;
    }
  }
  if (!pick) return calibHit ? { id: calibHit.id, label: calibHit.label, confidence: calibHit.confidence,
                                provenance: calibHit.provenance, meshNames: [LAST_HIT.mesh_name] } : null;
  if (calibHit) {   // 校准标签优先：几何锚点只作兜底（人工标注是可信依据）
    return { id: calibHit.id, label: calibHit.label, confidence: calibHit.confidence,
             provenance: calibHit.provenance, meshNames: [LAST_HIT.mesh_name] };
  }
  return {
    id: pick.anatomical_label,
    label: ambiguous ? "指间（无法可靠区分：" + ambiguous + "）" : pick.display_zh,
    confidence: ambiguous ? 0.3 : pick.confidence,
    provenance: pick.provenance,
    meshNames: pick.mesh_names,
    ambiguous: !!ambiguous
  };
}

/* ================= 自动解剖分区引擎（几何路线，无骨骼模型） =================
 * GLB 无骨骼/无语义命名（tools/inspect_glb_semantics.py 结论）→ 按姿态+包围盒+左右对称
 * 自动生成细粒度区域。分级：锚点（几何实测）> 型带（比例区间，基于 fitInfo 归一化）。
 * 型带区间为人体测量学比例（A-pose 适配），全部输出 provenance，证据不足返回 null。 */

/* A-pose 臂部比例：r=(|nx|-0.62)/0.38（肩根 0 → 指尖 1），型带分区同标定 */
function zoneFromPoint(p) {
  if (!fitInfo) return null;
  var size = fitInfo.getSize(new THREE.Vector3());
  var center = fitInfo.getCenter(new THREE.Vector3());
  var ny = (p.y - fitInfo.min.y) / size.y;          // 0=脚底 1=头顶
  var nx = (p.x - center.x) / (size.x / 2);         // -1..1（右..左，与锚点表一致：x>0=解剖学左）
  var nz = (p.z - center.z) / (size.z / 2);         // -1..1（背..胸腹前）
  var ax = Math.abs(nx), side = nx > 0 ? "l" : "r", sideZh = nx > 0 ? "左" : "右";
  // v18：附带相对位置依据 —— relT=区域内纵向比例（近端0→远端1），bb=区域包围盒（上中下/偏内偏外）
  function zone(id, zh, conf, opts) {
    opts = opts || {};
    var out = { id: id, label: zh, confidence: conf,
             provenance: "自动分区（型带比例：ny=" + ny.toFixed(2) + " ax=" + ax.toFixed(2) + " nz=" + nz.toFixed(2) + "）" };
    if (opts.relT !== undefined) out.relT = +opts.relT.toFixed(3);
    if (opts.bb) out.bb = opts.bb;
    return out;
  }
  function bbBand(y0, y1, x0, x1) {   // 区域包围盒（fitInfo 归一化带 → 世界盒，供 boxRelPos）
    return { min: { x: center.x + x0 * size.x / 2, y: fitInfo.min.y + y0 * size.y, z: fitInfo.min.z },
             max: { x: center.x + x1 * size.x / 2, y: fitInfo.min.y + y1 * size.y, z: fitInfo.max.z } };
  }
  /* —— 头部（ny>0.875）：按 nz/nx 分面。眼锚点已在上级处理，此处只报无锚点区 —— */
  if (ny > 0.875) {
    if (ny > 0.965) return zone("head_top", "头顶", 0.6);
    if (nz < -0.45) return zone("head_back", "后脑", 0.65);
    if (nz > 0.35) {                                  // 面部前带
      if (nz > 0.72 && ny < 0.955) return ny > 0.925 ? zone("forehead", "额", 0.6) : zone("face_mid", "面部（颊区）", 0.55);
      return zone("face_front", "面部", 0.55);
    }
    return ny > 0.93 ? zone("temple", sideZh + "颞", 0.5) : zone("cheek_ear_zone", sideZh + "耳颊区（耳被头发覆盖，未能可靠识别耳廓）", 0.35);
  }
  /* —— 颈 —— */
  if (ny > 0.835) {
    if (ax < 0.30) return zone("neck", "颈部", 0.75);
    return zone("shoulder", sideZh + "肩", 0.7);       // 835–875 带外侧是肩
  }
  /* —— 手臂带（|x|>0.62）：肩→肘→前臂→腕→掌→指，A-pose 比例 —— */
  if (ax > 0.62) {
    var r = (ax - 0.62) / 0.38;                        // 0=肩根 1=指尖
    if (r > 0.93) {
      return r > 0.97 ? zone("fingers", sideZh + "手指（当前模型缺少指级精细语义，需锚点或校准）", 0.4, { relT: (r - 0.93) / 0.07 })
                      : zone("palm", sideZh + "掌", 0.6, { relT: (r - 0.93) / 0.07 });
    }
    if (r > 0.845) return zone("wrist", sideZh + "腕", 0.7, { relT: (r - 0.845) / 0.155 });
    if (r > 0.40) return zone("forearm", sideZh + "前臂", 0.8, { relT: (r - 0.40) / 0.445, bb: bbBand(ny - 0.06, ny + 0.06, side === "l" ? 0.62 : -1, side === "l" ? 1 : -0.62) });
    if (r > 0.12) return zone("elbow_upper_arm", ny < 0.60 ? sideZh + "肘" : sideZh + "上臂", 0.65, { relT: (r - 0.12) / 0.28, bb: bbBand(ny - 0.06, ny + 0.06, side === "l" ? 0.62 : -1, side === "l" ? 1 : -0.62) });
    return zone("shoulder_upper", sideZh + "肩", 0.7);
  }
  /* —— 腿带（ny<=0.30）—— */
  if (ny <= 0.30) {
    /* v19：足部细分（法线+纵向，仅当无逐面表时的兜底）：脚底=法线朝下 → 跟/弓/前掌 */
    var nrm = LAST_HIT && LAST_HIT.normal;
    var soleDown = nrm && nrm[1] < -0.5;
    if (ny <= 0.045 || (ny <= 0.10 && ax > 0.55)) {
      if (soleDown && ax > 0.20) {
        if (nz < -0.30) return zone("heel", sideZh + "脚跟", 0.85);
        if (nz < 0.15) return zone("arch", sideZh + "足弓", 0.8);
        return zone("forefoot", sideZh + "前脚掌", 0.8);
      }
      return zone("foot_toes", sideZh + "足/趾（当前模型缺少趾级精细语义，需校准）", 0.5);
    }
    if (ny <= 0.075) return zone("ankle", sideZh + "踝", 0.6, { relT: (0.075 - ny) / 0.03 });
    if (ny <= 0.155) return zone("calf", sideZh + "小腿", 0.8, { relT: (0.155 - ny) / 0.08, bb: bbBand(ny - 0.05, ny + 0.05, side === "l" ? 0.02 : -0.5, side === "l" ? 0.5 : -0.02) });
    if (ax > 0.62) return zone("knee_outer", sideZh + "膝（外侧带）", 0.5);
    if (ny <= 0.205) {
      if (nz < -0.45) return zone("popliteal", sideZh + "膝窝（腘）", 0.8);   // v19：背面凹陷带
      return zone("knee", sideZh + "膝", 0.7, { relT: (0.205 - ny) / 0.05 });
    }
    return zone("thigh", sideZh + "大腿", 0.8, { relT: (0.30 - ny) / 0.095, bb: bbBand(ny - 0.06, ny + 0.06, side === "l" ? 0.02 : -0.5, side === "l" ? 0.5 : -0.02) });
  }
  /* —— 躯干（0.30<ny<=0.835）—— */
  if (ax > 0.30 && ny > 0.62) return zone("shoulder_belt", sideZh + "肩带（肩/上臂根）", 0.5);
  if (nz < -0.15) {
    if (ny > 0.62) return zone("back_upper", "背（上）", 0.75, { bb: bbBand(0.62, 0.835, -0.45, 0.45) });
    if (ny > 0.44) return zone("back_lower_waist", "腰/背（下）", 0.7);
    /* v19：臀部细分 —— 沟壁（|x|<0.07 窄带法线朝中线）/普通臀 */
    var nrmH = LAST_HIT && LAST_HIT.normal;
    var medial = nrmH && (nrmH[0] * (p.x - center.x)) < 0;
    if (Math.abs(nx) < 0.045 && medial && nrmH && nrmH[2] < 0) return zone("cleft", sideZh + "臀沟（臀裂壁）", 0.7);
    return zone("hip_buttock", sideZh + "臀", 0.7);
  }
  if (ny > 0.62) return zone("chest", "胸", 0.8, { bb: bbBand(0.62, 0.835, -0.45, 0.45) });
  if (ny > 0.44) return zone("abdomen", "腹", 0.8, { bb: bbBand(0.44, 0.62, -0.45, 0.45) });
  return ax < 0.14 ? zone("groin", "会阴（隐私区）", 0.6) : zone("hip_front", sideZh + "髋前", 0.6);
}

/* 镜框遮挡回退：命中镜框 mesh 时按落点 x 判左右眼（镜框环内）。 */
function glassesFallback(point) {
  if (!ANATOMY_ACTIVE) return null;
  var side = point.x > 0 ? "left_eye" : "right_eye";   // 与锚点表一致：x>0=解剖学左
  var zh = point.x > 0 ? "左眼（含虹膜）" : "右眼（含虹膜）";
  return { id: side, label: zh, confidence: 0.85, provenance: "镜框环内回退（眼镜 mesh 命中）" };
}

/* ================= v18：表面方位 + 相对位置（连续精确位置描述） =================
 * 在解剖区域（锚点/型带）基础上，依据命中点法线与区域包围盒局部坐标生成：
 * 「解剖区域 + 表面方位（前/后/内/外/上/下组合）+ 相对位置（上中下/近端中段远端 + 偏向）+ 局部坐标」。
 * 全部由真实命中数据（normal、model_local_xyz）推导，无屏幕位置判断、无碰撞盒。 */

/* 世界法线（模型根静止 = 模型系方向）→ 表面方位词。
 * 五向分解：前(+z)/后(-z)/内-外(x 符号 vs 命中点侧别)/上(+y)/下(-y)。
 * 主分量 = |投影| 最大者；次分量占比 ≥0.55 且主分量 >0.15 → 组合方位
 * （前外侧/后内侧/前上方…）。反向分量互相排除，绝不出现「后前侧」。 */
function surfaceAspect(normal, px) {
  if (!normal || normal.length < 3) return null;
  var nx = normal[0], ny = normal[1], nz = normal[2];
  var outward = (nx * (px || 0)) >= 0 ? "外" : "内";   // 法线离体 = 外；指向中线 = 内
  // v23 修复：旧版同时放入 front(v=nz) 与 back(v=-nz)，二者 |v| 恒相等，sort 稳定性使 front
  // 恒居首 → 所有背面点永远输出「前侧」。改为每轴只保留有符号的主导方向（无冗余反向项）。
  var comps = [
    { k: nz >= 0 ? "front" : "back", v: Math.abs(nz), w: nz >= 0 ? "前" : "后", opp: null },
    { k: "out", v: Math.abs(nx), w: outward, opp: null },
    { k: ny >= 0 ? "up" : "down", v: Math.abs(ny), w: ny >= 0 ? "上" : "下", opp: null }
  ];
  comps.sort(function (a, b) { return b.v - a.v; });
  var c0 = comps[0];
  var horiz = { front: 1, back: 1, out: 1 };
  function wordOf(c) { return c.k === "up" || c.k === "down" ? c.w + "方" : c.w + "侧"; }
  var zh = wordOf(c0);
  var c1 = comps.find(function (c) { return c !== c0 && c.k !== c0.opp; });
  if (c1 && c1.v >= 0.55 * c0.v && c0.v > 0.15 && c1.w !== c0.w) {
    zh = c0.w + c1.w + (horiz[c0.k] && horiz[c1.k] ? "侧" : "方");   // 前外侧 / 前上方
  }
  return { key: c0.k, zh: zh, dominance: +c0.v.toFixed(3) };
}

/* 肢体（腿/臂）纵轴相对位置：0=近端（髋/肩）1=远端（膝踝/腕手），配合前后内外偏向 */
function limbRelPos(t) {
  if (t < 0.22) return "近端";
  if (t < 0.45) return "近端偏中";
  if (t < 0.62) return "中段";
  if (t < 0.80) return "中段偏远";
  return "远端";
}

/* 解剖区域包围盒内的相对位置：上/中/下段 + 横向偏内/偏外 + 纵深偏前/偏后 */
function boxRelPos(ml, bb) {
  var ny = (ml.y - bb.min.y) / Math.max(bb.max.y - bb.min.y, 1e-6);   // 0..1
  var nx = (ml.x - bb.min.x) / Math.max(bb.max.x - bb.min.x, 1e-6);
  var nz = (ml.z - bb.min.z) / Math.max(bb.max.z - bb.min.z, 1e-6);
  var v = ny > 0.68 ? "上段" : (ny < 0.32 ? "下段" : "中段");
  var h = nx > 0.66 ? "偏外" : (nx < 0.34 ? "偏内" : "");
  var d = nz > 0.66 ? "偏前" : (nz < 0.34 ? "偏后" : "");
  var bits = [v];
  if (h) bits.push(h);
  if (d) bits.push(d);
  return { text: bits.join(""), ny: +ny.toFixed(3), nx: +nx.toFixed(3), nz: +nz.toFixed(3) };
}

/* 主入口：真实命中 + 解剖区域 → 完整位置描述。
 * region：{id, label, source:"anchor"|"zone"|"unreliable", confidence, relT?}
 * relT：肢体纵向比例（zoneFromPoint 已算臂 r / 腿 ny 带内段），供近端/中段/远端。
 * 返回 {main, parts:{area, aspect, rel, coords}, coordsText} */
function describeHit(hitData, region) {
  var area = region.label;
  var parts = { area: area, aspect: null, rel: null, coords: null };
  var ml = hitData && hitData.model_local_xyz;
  // 1) 表面方位：真实命中法线（模型系）+ 命中点横向位置（判内/外）
  var asp = surfaceAspect(hitData && hitData.normal, ml ? ml.x : (hitData && hitData.world_xyz ? hitData.world_xyz[0] : 0));
  if (asp && asp.dominance >= 0.28) parts.aspect = asp.zh;
  // 2) 相对位置
  var rel = null;
  if (region.relT !== undefined && region.relT !== null) {
    // 肢体区：纵向比例 → 近端/中段/远端，附加横向偏内/外
    var lateral = ml ? (Math.abs(ml.x) > 0.02 ? (ml.x < 0 ? "偏外" : "偏内") : "") : "";
    var sagg = asp && (asp.zh.indexOf("前") === 0 || asp.zh.indexOf("前") === 1) ? ""
      : (ml ? (ml.z > 0.02 ? "（偏前）" : ml.z < -0.02 ? "（偏后）" : "") : "");
    rel = limbRelPos(region.relT) + (lateral && region.relT > 0.05 ? lateral : "") + sagg;
  } else if (region.yRel !== undefined && region.yRel !== null) {
    // v23 躯干/臀区：yband 内纵向比例 → 上份/中份/下份（臀部等非肢体大区按用户要求
    // 也必须区分不同纵深处点击；肢体「近端/远端」口径不适用于臀/躯干，用上中下份）
    rel = (region.yRel > 0.66 ? "上份" : region.yRel < 0.34 ? "下份" : "中份");
    // 臀区附加内外份（避开中线→外侧的横向变化）与前后（臀后表面恒为后，法线已给方位）
    if (region.ybandRef && region.ybandRef[1] > region.ybandRef[0] && ml) {
      // 无额外横向信息——臀部左右由标签承担，此处不再叠「偏内偏外」防重复
    }
  } else if (ml && region.bb) {
    rel = boxRelPos(ml, region.bb).text;
  }
  if (rel) parts.rel = rel;
  // 3) 精确坐标：模型局部坐标（模型系恒定，旋转/缩放视角不变）
  if (ml) {
    parts.coords = "(" + ml.map(function (v) { return v.toFixed(3); }).join(", ") + ")";
    parts.coordsWorld = "(" + (hitData.world_xyz || []).map(function (v) { return v.toFixed(3); }).join(", ") + ")";
  }
  var segs = [area];
  if (parts.aspect) segs.push(parts.aspect);
  if (parts.rel) segs.push(parts.rel);
  if (parts.coords) segs.push("局部坐标 " + parts.coords);
  parts.main = segs.join("，");
  if (region.fsemNote) parts.fsemNote = region.fsemNote;   // v19：遮挡/缺语义说明透传
  return parts;
}

/* ================= 校准标注层（真实命中 + 人工标签） ================= */

function modelKey() {
  return ACTIVE_MODEL_KEY || ((typeof MODEL_FILES !== "undefined" && MODEL_FILES[modelIndex]) ? MODEL_FILES[modelIndex] : "unknown");
}

function annotationModelKey() {
  return MODEL_IDENTITY && MODEL_IDENTITY.sha256
    ? "sha256:" + MODEL_IDENTITY.sha256
    : modelKey();
}

function measuredCorrectionsFor() {
  var base = modelKey().split("/").pop();
  return MEASURED_CORRECTIONS_BY_MODEL[base] || [];
}

function nearestMeasuredCorrection(hit) {
  if (!hit || !hit.model_local_xyz) return null;
  var best = null, bestD = Infinity;
  measuredCorrectionsFor().forEach(function (item) {
    var p = item.point, q = hit.model_local_xyz;
    var d = Math.sqrt(Math.pow(q[0] - p[0], 2) + Math.pow(q[1] - p[1], 2) + Math.pow(q[2] - p[2], 2));
    var tol = item.tol || 0.055;   // 细小部位（手指等）可用更紧的 tol，避免吞掉邻区
    if (d < tol && d < bestD) { bestD = d; best = item; }
  });
  return best;
}
function manualRegionFaceKey(mesh, face) {
  return String(mesh || "") + ":" + String(face);
}

function manualFaceIndexKey(mesh, face) {
  return annotationModelKey() + "|" + manualRegionFaceKey(mesh, face);
}

/* 阶段2 重叠规则（显式）：同一面被多个区域圈选时，归属 updated_at 最新的区域。
 * 按 updated_at 升序遍历、后写覆盖——保证索引构建确定性，与遍历顺序无关。 */
function rebuildManualFaceIndex() {
  MANUAL_FACE_OWNER = {};
  if (!MODEL_IDENTITY.ready) return;
  Object.keys(MANUAL_REGIONS.regions || {}).map(function (id) { return MANUAL_REGIONS.regions[id]; })
    .filter(function (r) { return r && r.status !== "deleted"; })
    .sort(function (a, b) {
      return String(a.updated_at || a.created_at || "").localeCompare(String(b.updated_at || b.created_at || ""));
    })
    .forEach(function (region) {
      (region.faces || []).forEach(function (face) {
        MANUAL_FACE_OWNER[manualFaceIndexKey(face.mesh_name, face.face_index)] = region;
      });
    });
}

function manualRegionFor(hit) {
  if (!hit || hit.face_index == null || !MODEL_IDENTITY.ready) return null;
  return MANUAL_FACE_OWNER[manualFaceIndexKey(hit.mesh_name, hit.face_index)] || null;
}

function measuredFaceCorrection(hit) {
  if (!hit) return null;
  return MEASURED_FACE_CORRECTIONS[manualRegionFaceKey(hit.mesh_name, hit.face_index)] || null;
}

function loadManualRegions() {
  try {
    var raw = localStorage.getItem("derm_manual_regions");
    if (!raw) return;
    var data = JSON.parse(raw);
    if (!data || typeof data !== "object") throw new Error("文档不是对象");
    if (data.schema_version != null && data.schema_version > ANNOTATION_SCHEMA_VERSION) {
      throw new Error("文档 schema 版本过新");
    }
    PENDING_MANUAL_DOCUMENT = data;
    activatePendingManualDocument();
  } catch (e) { console.warn("人工区域本地数据读取失败", e); }
}

function persistManualRegions() {
  var doc = manualRegionsDocument();
  try { localStorage.setItem("derm_manual_regions", JSON.stringify(doc)); } catch (e) { console.warn("人工区域本地保存失败", e); }
  if (bridge && bridge.onManualRegionsSaved) bridge.onManualRegionsSaved(JSON.stringify(doc));
  rebuildManualFaceIndex();
}

function pointInPolygon(x, y, polygon) {
  var inside = false;
  for (var i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    var a = polygon[i], b = polygon[j];
    if (((a[1] > y) !== (b[1] > y)) &&
        x < (b[0] - a[0]) * (y - a[1]) / ((b[1] - a[1]) || 1e-9) + a[0]) inside = !inside;
  }
  return inside;
}

function projectScreen(worldPoint) {
  var rect = renderer.domElement.getBoundingClientRect();
  var p = worldPoint.clone().project(camera);
  return [rect.left + (p.x + 1) * rect.width / 2, rect.top + (1 - p.y) * rect.height / 2];
}

function clearRegionDrawVisuals() {
  if (regionDrawGroup && scene) scene.remove(regionDrawGroup);
  regionDrawGroup = null;
}

function renderRegionDrawVisuals() {
  clearRegionDrawVisuals();
  if (!scene || (!REGION_DRAW.active && !REGION_DRAW.preview)) return;
  regionDrawGroup = new THREE.Group();
  regionDrawGroup.userData.noPick = true;
  var pointMat = new THREE.MeshBasicMaterial({ color: 0xff1744, depthTest: false });
  REGION_DRAW.hits.forEach(function (hit) {
    if (!hit || !hit.world_xyz) return;
    var marker = new THREE.Mesh(new THREE.SphereGeometry(0.07, 16, 12), pointMat);
    marker.position.set(hit.world_xyz[0], hit.world_xyz[1], hit.world_xyz[2]);
    marker.userData.noPick = true;
    regionDrawGroup.add(marker);
  });
  if (REGION_DRAW.points.length > 1) {
    var linePoints = REGION_DRAW.points.map(function (point) {
      var rect = renderer.domElement.getBoundingClientRect();
      return new THREE.Vector3(((point[0] - rect.left) / rect.width) * 2 - 1,
        -((point[1] - rect.top) / rect.height) * 2 + 1, 0.5).unproject(camera);
    });
    var line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(linePoints),
      new THREE.LineBasicMaterial({ color: 0xff1744, depthTest: false, linewidth: 3 }));
    line.userData.noPick = true;
    regionDrawGroup.add(line);
  }
  scene.add(regionDrawGroup);
}

function updateRegionDrawUI() {
  var box = document.getElementById("regionAnnotator");
  var count = document.getElementById("raCount");
  var status = document.getElementById("raStatus");
  if (box) box.classList.toggle("open", REGION_DRAW.active || REGION_DRAW.preview);
  if (count) count.textContent = REGION_DRAW.points.length + " 点 · " + REGION_DRAW.previewFaces.length + " 面";
  if (status) status.textContent = REGION_DRAW.preview ? "预览人工区域：选择标准部位后完成保存" :
    "人工区域（" + REGION_DRAW.view + "）：依次点击边界顶点（红色点）";
  renderRegionDrawVisuals();
}

function enterRegionDraw() {
  if (!modelRoot) { alert("模型尚未加载"); return; }
  exitLabelMode();
  clearRegionDrawVisuals();
  REGION_DRAW = { active: true, view: currentViewName, points: [], hits: [], previewFaces: [], preview: false };
  if (controls) controls.enabled = false;
  updateRegionDrawUI();
  var hint = document.getElementById("hint");
  if (hint) hint.textContent = "区域标注：点击模型表面依次添加边界点，至少 3 点后点击完成";
}

function exitRegionDraw() {
  REGION_DRAW.active = false;
  REGION_DRAW.preview = false;
  REGION_DRAW.points = [];
  REGION_DRAW.hits = [];
  REGION_DRAW.previewFaces = [];
  if (controls) controls.enabled = true;
  clearRegionDrawVisuals();
  updateRegionDrawUI();
}

function regionDrawClick(hit, event) {
  REGION_DRAW.points.push([event.clientX, event.clientY]);
  REGION_DRAW.hits.push(hit);
  updateRegionDrawUI();
}

function collectProjectedFaces(polygon) {
  var result = [], seen = {};
  if (!modelRoot) return result;
  modelRoot.updateWorldMatrix(true, true);
  modelRoot.traverse(function (mesh) {
    if (!mesh.isMesh || mesh.userData.noPick || !mesh.geometry || !mesh.geometry.attributes.position) return;
    var pos = mesh.geometry.attributes.position, idx = mesh.geometry.index;
    var faceCount = idx ? idx.count / 3 : pos.count / 3;
    for (var face = 0; face < faceCount; face++) {
      var ia = idx ? idx.getX(face * 3) : face * 3;
      var ib = idx ? idx.getX(face * 3 + 1) : face * 3 + 1;
      var ic = idx ? idx.getX(face * 3 + 2) : face * 3 + 2;
      var a = new THREE.Vector3().fromBufferAttribute(pos, ia).applyMatrix4(mesh.matrixWorld);
      var b = new THREE.Vector3().fromBufferAttribute(pos, ib).applyMatrix4(mesh.matrixWorld);
      var c = new THREE.Vector3().fromBufferAttribute(pos, ic).applyMatrix4(mesh.matrixWorld);
      var pa = projectScreen(a), pb = projectScreen(b), pc = projectScreen(c);
      var sample = [(pa[0] + pb[0] + pc[0]) / 3, (pa[1] + pb[1] + pc[1]) / 3];
      if (!pointInPolygon(sample[0], sample[1], polygon)) continue;
      var rect = renderer.domElement.getBoundingClientRect();
      raycaster.setFromCamera(new THREE.Vector2((sample[0] - rect.left) / rect.width * 2 - 1,
        -(sample[1] - rect.top) / rect.height * 2 + 1), camera);
      var visible = raycaster.intersectObject(modelRoot, true).filter(function (h) {
        return h.object && h.object.isMesh && !h.object.userData.noPick;
      })[0];
      if (!visible || visible.object !== mesh || visible.faceIndex !== face) continue;
      var key = manualRegionFaceKey(mesh.name, face);
      if (seen[key]) continue;
      seen[key] = true;
      var center = a.clone().add(b).add(c).multiplyScalar(1 / 3);
      var modelLocal = modelRoot.worldToLocal(center.clone());
      result.push({ mesh_name: mesh.name, face_index: face,
        model_local_xyz: [+modelLocal.x.toFixed(5), +modelLocal.y.toFixed(5), +modelLocal.z.toFixed(5)] });
    }
  });
  return result;
}

function finishRegionDraw() {
  if (!REGION_DRAW.active || REGION_DRAW.points.length < 3) { alert("请至少点击 3 个边界点"); return; }
  REGION_DRAW.previewFaces = collectProjectedFaces(REGION_DRAW.points);
  if (!REGION_DRAW.previewFaces.length) { alert("当前边界没有选中可见三角面"); return; }
  REGION_DRAW.active = false;
  REGION_DRAW.preview = true;
  if (controls) controls.enabled = true;
  updateRegionDrawUI();
}

/* 阶段2 修订：同标准部位的最新 confirmed 区域（追加视角目标）。 */
function latestRegionByStandard(standard) {
  var found = null;
  Object.keys(MANUAL_REGIONS.regions || {}).forEach(function (id) {
    var r = MANUAL_REGIONS.regions[id];
    if (!r || r.status === "deleted" || r.standard_id !== standard) return;
    if (!found || String(r.updated_at || "") > String(found.updated_at || "")) found = r;
  });
  return found;
}

function commitRegionDraw() {
  var selector = document.getElementById("raLabel");
  var standard = selector && selector.value;
  if (!standard || !REGION_DRAW.previewFaces.length) { alert("请选择标准人体部位"); return; }
  var now = new Date().toISOString();
  var boundary = REGION_DRAW.hits.map(function (hit, i) {
    return { screen: REGION_DRAW.points[i], mesh_name: hit.mesh_name, face_index: hit.face_index,
      model_local_xyz: hit.model_local_xyz };
  });
  var appendEl = document.getElementById("raAppend");
  var existing = (appendEl && appendEl.checked) ? latestRegionByStandard(standard) : null;
  var id, region, label, note;
  if (existing) {
    // 阶段2 revision：同标准部位追加新视角 → views 追加 + faces 并集去重 + version+1
    id = existing.id;
    region = existing;
    MANUAL_REGIONS.history.push({ at: now, action: "region_revise", region_id: id,
      regions_json: JSON.stringify(MANUAL_REGIONS.regions), llm_json: JSON.stringify(LLM_SUBREGIONS) });
    var before = region.faces.length;
    var seen = {};
    region.faces.forEach(function (f) { seen[manualRegionFaceKey(f.mesh_name, f.face_index)] = true; });
    REGION_DRAW.previewFaces.forEach(function (f) {
      var k = manualRegionFaceKey(f.mesh_name, f.face_index);
      if (!seen[k]) { seen[k] = true; region.faces.push(f); }
    });
    region.views.push({ view: REGION_DRAW.view, boundary_points: boundary });
    region.version = (region.version || 1) + 1;
    region.updated_at = now;
    note = "已追加视角到「" + region.label + "」（" + before + " → " + region.faces.length + " 面，v" + region.version + "）";
    label = region.label;
  } else {
    id = "manual-region-" + Date.now().toString(36);
    region = { id: id, standard_id: standard, label: REGION_LABELS[standard] || standard,
      views: [{ view: REGION_DRAW.view, boundary_points: boundary }],
      faces: REGION_DRAW.previewFaces, version: 1, status: "confirmed", source: "manual",
      created_at: now, updated_at: now };
    MANUAL_REGIONS.history.push({ at: now, action: "region_save", region_id: id,
      regions_json: JSON.stringify(MANUAL_REGIONS.regions), llm_json: JSON.stringify(LLM_SUBREGIONS) });
    MANUAL_REGIONS.regions[id] = region;
    note = null;
    label = region.label;
  }
  persistManualRegions();
  var labelEl = document.getElementById("regionLabel");
  if (labelEl) labelEl.textContent = note || ("已人工标注：" + region.label + "（" + region.faces.length + " 面）");
  exitRegionDraw();
}

function undoManualRegion() {
  var history = MANUAL_REGIONS.history || [];
  if (!history.length) return false;
  var snapshot = history.pop();
  MANUAL_REGIONS.regions = JSON.parse(snapshot.regions_json || "{}");
  LLM_SUBREGIONS = JSON.parse(snapshot.llm_json || "{}");
  persistManualRegions();
  return true;
}

/* 阶段2 严格导出：整份人工区域文档（含模型指纹/schema/history）交 Qt 落盘到医生自选路径。
 * 导出是纯序列化——不改动任何运行状态，失败也不影响已有数据。 */
function exportManualRegions() {
  if (!MODEL_IDENTITY.ready) { alert("模型指纹未就绪（缺少完整 SHA-256），无法导出人工区域"); return; }
  var payload = JSON.stringify(manualRegionsDocument());
  if (bridge && bridge.onManualRegionsExported) {
    bridge.onManualRegionsExported(payload);
    var label = document.getElementById("regionLabel");
    if (label) label.textContent = "已请求导出人工区域文档（" + Object.keys(MANUAL_REGIONS.regions || {}).length + " 区域）";
  } else {
    alert("当前环境未接入桌面桥接，无法导出文件");
  }
}

/* 导出/持久化共用：返回当前人工区域文档（确保 schema 兼容字段齐全）。 */
function manualRegionsDocument() {
  MANUAL_REGIONS.manual_regions = MANUAL_REGIONS.regions;
  MANUAL_REGIONS.llm_subregions = LLM_SUBREGIONS;
  MANUAL_REGIONS.saved_at = new Date().toISOString();
  return MANUAL_REGIONS;
}

function renderLLMReview() {
  var list = document.getElementById("llmReviewList");
  if (!list) return;
  var all = [];
  Object.keys(LLM_SUBREGIONS || {}).forEach(function (key) { all = all.concat(LLM_SUBREGIONS[key] || []); });
  if (!all.length) { list.innerHTML = '<div class="empty">尚无生成结果</div>'; return; }
  list.innerHTML = all.map(function (item) {
    return '<div class="llm-card ' + (item.review_status || "pending") + '"><b>' + esc(item.label || item.standard_id || "未命名") +
      '</b><div class="llm-meta">状态：' + (item.review_status || "pending") + " · " + (item.faces || []).length +
      ' 面 · 置信度：' + (item.confidence == null ? "—" : item.confidence) + '</div>' +
      (item.review_status === "pending" ? '<button data-llm="accept" data-id="' + item.id + '">接受</button><button data-llm="reject" data-id="' + item.id + '">拒绝</button>' : '') + '</div>';
  }).join("");
  list.querySelectorAll("button[data-llm]").forEach(function (button) {
    button.addEventListener("click", function () { reviewLLM(button.getAttribute("data-id"), button.getAttribute("data-llm")); });
  });
}

function requestLLMBatch() {
  var regions = Object.keys(MANUAL_REGIONS.regions || {}).map(function (key) { return MANUAL_REGIONS.regions[key]; })
    .filter(function (region) { return region && region.status === "confirmed" && (region.faces || []).length; });
  if (!regions.length) { alert("请先完成至少一个人工区域标注"); return; }
  var payload = { schema_version: 1, model_key: modelKey(), regions: regions.map(function (region) {
    return { region_id: region.id, revision: region.version, standard_id: region.standard_id, label: region.label,
      views: (region.views || []).map(function (view) { return view.view; }), face_count: region.faces.length,
      faces: region.faces.map(function (face) { return { mesh_name: face.mesh_name, face_index: face.face_index,
        model_local_xyz: (face.model_local_xyz || []).map(function (value) { return +(+value).toFixed(4); }) }; }) };
  }), instruction: "仅使用 parent face set 内的面，结果逐项待审核" };
  if (bridge && bridge.onLLMRefinementRequested) {
    bridge.onLLMRefinementRequested(JSON.stringify(payload));
    toggleLLMReview(true);
    return;
  }
  LLM_SUBREGIONS = {};
  regions.forEach(function (region) { LLM_SUBREGIONS[region.id] = [{ id: "draft-" + region.id, parent_region_id: region.id,
    parent_version: region.version, standard_id: region.standard_id, label: region.label + "（待审核）",
    faces: region.faces.slice(0, Math.max(1, Math.floor(region.faces.length / 2))), rationale: "离线演示草稿：父区域面集子集",
    confidence: 0.5, review_status: "pending", generator_version: "mock-region-v1" }]; });
  persistManualRegions(); renderLLMReview(); toggleLLMReview(true);
}

function receiveLLMRefinement(raw) {
  try {
    var data = typeof raw === "string" ? JSON.parse(raw) : raw;
    var regions = data && data.regions ? data.regions : data && data.llm_subregions;
    if (!regions || typeof regions !== "object") throw new Error("无效的 LLM 结果");
    var accepted = {};
    Object.keys(regions).forEach(function (key) {
      var parent = MANUAL_REGIONS.regions[key];
      if (!parent) return;
      var allowed = {};
      (parent.faces || []).forEach(function (face) { allowed[manualRegionFaceKey(face.mesh_name, face.face_index)] = true; });
      accepted[key] = (regions[key] || []).filter(function (item) {
        return item && item.parent_region_id === key && item.parent_version === parent.version &&
          item.faces && item.faces.length && item.faces.every(function (face) {
            return allowed[manualRegionFaceKey(face.mesh_name, face.face_index)];
          });
      }).map(function (item) { item.review_status = "pending"; return item; });
    });
    LLM_SUBREGIONS = accepted;
    persistManualRegions(); renderLLMReview(); toggleLLMReview(true);
  } catch (e) { alert("LLM 结果校验失败：" + e.message); }
}

function reviewLLM(id, action) {
  Object.keys(LLM_SUBREGIONS || {}).forEach(function (key) { (LLM_SUBREGIONS[key] || []).forEach(function (item) {
    if (item.id === id) { item.review_status = action === "accept" ? "accepted" : "rejected"; item.reviewed_at = new Date().toISOString(); }
  }); });
  persistManualRegions(); renderLLMReview();
}

function toggleLLMReview(force) {
  var drawer = document.getElementById("llmReviewDrawer");
  if (drawer) drawer.classList.toggle("open", force === undefined ? !drawer.classList.contains("open") : force);
}

function loadCalibrations() {
  fetch("anatomy-calibrations.json", { cache: "no-cache" })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (j) { if (j && j.calibrations) CALIB.byModel = j.calibrations; })
    .catch(function () { /* 无校准文件 = 全部未标注，正常起点 */ });
}

function saveCalibrations() {
  try {
    localStorage.setItem("derm_anatomy_calibrations", JSON.stringify({
      version: "1.0", saved_at: new Date().toISOString(), calibrations: CALIB.byModel
    }));
  } catch (e) { console.warn("校准标签本地保存失败：", e); }
  // Qt 客户端/服务端部署时经桥接上报（本地 file:// 无后端，localStorage 为主存储）
  if (bridge && bridge.onCalibrationSaved) bridge.onCalibrationSaved(JSON.stringify(CALIB.byModel));
}

function loadCalibrationsLocal() {
  try {
    var raw = localStorage.getItem("derm_anatomy_calibrations");
    if (raw) {
      var j = JSON.parse(raw);
      if (j && j.calibrations) {
        // 服务端文件与本地存储合并：本地后写优先
        for (var mk in j.calibrations) {
          CALIB.byModel[mk] = Object.assign({}, CALIB.byModel[mk] || {}, j.calibrations[mk]);
        }
      }
    }
  } catch (e) { /* localStorage 不可用（file:// 部分环境）→ 仅用服务端文件 */ }
}

/* 真实 Raycaster 命中 → 完整原始数据（v18：不做任何部位猜测，字段缺什么如实记 null）。
 * 记录：object 引用 / faceIndex / 世界坐标 + Mesh 局部坐标 + 模型局部坐标（modelRoot 系）/
 * uv / 世界法线 / instanceId / Mesh-Node 祖先链 / Bone-Skin 权重（本 GLB 无 → null 并注明）/
 * 模型当前变换。 */
function recordHit(hit) {
  var o = hit.object, p = hit.point;
  var meshLocal = o.worldToLocal(p.clone());
  var modelLocal = modelRoot ? modelRoot.worldToLocal(p.clone()) : null;
  // v23：本 GLB 顶点序导致 face.normal 为几何内法线（tools 实测主身体 mesh 前面法线朝后率 0.00）。
  // 校正依据（零假设、数学必然）：Raycaster 命中的是相机可见面，其真实外法线与命中视线方向夹角必 >90°
  // （dot ≤ 0）。按此把法线翻转为「背离相机来向」的朝向，即真实朝外法线。
  var normalWorld = null;
  var nDotVCaptured = 0;
  if (hit.face) {
    normalWorld = hit.face.normal.clone().transformDirection(o.matrixWorld);
    if (hit.face.normal && raycaster && raycaster.ray && raycaster.ray.direction) {
      var viewDir = raycaster.ray.direction;                      // 相机→命中点方向
      nDotVCaptured = normalWorld.x * viewDir.x + normalWorld.y * viewDir.y + normalWorld.z * viewDir.z;
      if (nDotVCaptured > 0) normalWorld.negate();                // 法线朝相机来向 → 内法线，翻转
    }
  }
  var uv = hit.uv ? { u: +hit.uv.x.toFixed(5), v: +hit.uv.y.toFixed(5) } : null;
  // Mesh→根 的节点祖先链（v18：Node 语义取证用）
  var nodeChain = [];
  for (var n = o; n; n = n.parent) {
    if (n.name || n.isMesh || n.isBone) nodeChain.push(n.name || "(未命名)");
    if (n === modelRoot) break;
  }
  // 骨骼/Skin 权重：仅 SkinnedMesh 有；本 GLB（Custom3d66）无 skins → 如实 null
  var bone = null, skin = null, weights = null;
  if (o.isSkinnedMesh) {
    bone = (o.skeleton && o.skeleton.bones && getBoneNear(o, p)) || null;
    skin = "skinned";
    weights = getSkinWeights(o, hit.faceIndex);
  }
  var tf = modelRoot ? {
    rotation: [+modelRoot.rotation.x.toFixed(5), +modelRoot.rotation.y.toFixed(5), +modelRoot.rotation.z.toFixed(5)],
    scale: [+modelRoot.scale.x.toFixed(5), +modelRoot.scale.y.toFixed(5), +modelRoot.scale.z.toFixed(5)],
    position: [+modelRoot.position.x.toFixed(5), +modelRoot.position.y.toFixed(5), +modelRoot.position.z.toFixed(5)]
  } : null;
  LAST_HIT = {
    mesh_name: o.name || "(未命名 mesh)",
    node_name: o.userData.node_name || o.name || "(未命名 node)",
    node_chain: nodeChain,
    bone: bone, skin: skin, weights: weights,        // 无骨骼模型恒为 null（不伪造）
    instanceId: hit.instanceId != null ? hit.instanceId : null,
    face_index: hit.faceIndex != null ? hit.faceIndex : null,
    world_xyz: [+p.x.toFixed(5), +p.y.toFixed(5), +p.z.toFixed(5)],
    local_xyz: [+meshLocal.x.toFixed(5), +meshLocal.y.toFixed(5), +meshLocal.z.toFixed(5)],
    model_local_xyz: modelLocal ? [+modelLocal.x.toFixed(5), +modelLocal.y.toFixed(5), +modelLocal.z.toFixed(5)] : null,
    uv: uv,
    normal: normalWorld ? [+normalWorld.x.toFixed(4), +normalWorld.y.toFixed(4), +normalWorld.z.toFixed(4)] : null,
    normal_corrected: normalWorld ? (nDotVCaptured > 0) : null,   // v23：true=face.normal 为内法线已被翻转
    distance: hit.distance != null ? +hit.distance.toFixed(4) : null,
    model_transform: tf,
    object_ref: o
  };
  return LAST_HIT;
}

/* SkinnedMesh 命中点最近骨骼名（v18 语义取证；无骨骼模型不会走到） */
function getBoneNear(skinned, p) {
  var best = null, bestD = Infinity;
  (skinned.skeleton ? skinned.skeleton.bones : []).forEach(function (b) {
    var wp = b.getWorldPosition(new THREE.Vector3());
    var d = wp.distanceTo(p);
    if (d < bestD) { bestD = d; best = b.name || "(未命名 bone)"; }
  });
  return best ? { name: best, distance: +bestD.toFixed(4) } : null;
}

/* 命中三角面三顶点的 skinIndex/skinWeight（v18；无骨骼模型返回 null） */
function getSkinWeights(skinned, faceIndex) {
  try {
    var g = skinned.geometry, idx = faceIndex * 3;
    var si = g.attributes.skinIndex, sw = g.attributes.skinWeight;
    if (!si || !sw) return null;
    var vi = g.index ? g.index.getX(idx) : idx;
    return { vertex: vi,
             skinIndex: [si.getX(vi), si.getY ? si.getY(vi) : null].filter(function (v) { return v !== null; }),
             skinWeight: [sw.getX(vi), sw.getY ? sw.getY(vi) : null].filter(function (v) { return v !== null; }) };
  } catch (e) { return null; }
}

/* 已校准标签查询：精确 faceKey 命中优先；否则同 mesh 局部坐标最近邻（<2cm，三角面邻域复用）。
 * 返回 {label, provenance:"人工校准", calibration_key} 或 null。 */
function calibrationFor(lastHit) {
  if (!lastHit) return null;
  var byModel = CALIB.byModel[modelKey()] || {};
  var faceKey = lastHit.mesh_name + ":" + lastHit.face_index;
  if (byModel[faceKey]) {
    var c = byModel[faceKey];
    return { label: c.label, id: "calib:" + faceKey, confidence: 1.0,
             provenance: "人工校准（" + faceKey + "）", calibration_key: faceKey };
  }
  var bestKey = null, bestD = 0.02;   // 2cm 邻域
  for (var k in byModel) {
    if (k.indexOf(lastHit.mesh_name + ":") !== 0) continue;
    var lc = byModel[k].local_xyz;
    if (!lc) continue;
    var d = Math.sqrt(Math.pow(lastHit.local_xyz[0] - lc[0], 2) +
                      Math.pow(lastHit.local_xyz[1] - lc[1], 2) +
                      Math.pow(lastHit.local_xyz[2] - lc[2], 2));
    if (d < bestD) { bestD = d; bestKey = k; }
  }
  if (bestKey) {
    return { label: byModel[bestKey].label, id: "calib:" + bestKey, confidence: 0.95,
             provenance: "人工校准邻域（" + bestKey + "，" + (bestD * 100).toFixed(1) + "mm）",
             calibration_key: bestKey };
  }
  return null;
}

/* 标注模式：进入后下一次点击弹出标签输入，保存真实命中映射 */
function enterLabelMode() {
  CALIB.mode = true;
  var hint = document.getElementById("hint");
  if (hint) hint.textContent = "标注模式：点击模型表面 → 输入部位名称（如 左眼 / 左手中指）；再点「标注模式」按钮退出";
  var btn = document.getElementById("labelMode");
  if (btn) btn.style.background = "#f7e5c1";
}

function exitLabelMode() {
  CALIB.mode = false;
  var hint = document.getElementById("hint");
  if (hint) hint.textContent = "拖拽旋转 · 滚轮缩放 · 点击选择/再点取消（可多选）";
  var btn = document.getElementById("labelMode");
  if (btn) btn.style.background = "";
}

function promptCalibration(hit) {
  var text = null;
  if (bridge && bridge.promptLabel) {
    text = bridge.promptLabel("标注部位（真实命中 " + hit.mesh_name + " 面#" + hit.face_index + "）");
  } else {
    text = window.prompt("标注部位名称（如：左眼 / 左手中指 / 右脚拇趾）：\n命中 mesh=" + hit.mesh_name +
                         " 面#" + hit.face_index + " 局部=" + hit.local_xyz.join(","), "");
  }
  if (!text) return false;   // 用户取消 → 退出标注模式，不当次标注
  var key = hit.mesh_name + ":" + hit.face_index;
  var mk = modelKey();
  if (!CALIB.byModel[mk]) CALIB.byModel[mk] = {};
  CALIB.byModel[mk][key] = {
    label: text.trim(),
    mesh_name: hit.mesh_name,
    face_index: hit.face_index,
    local_xyz: hit.local_xyz,
    world_xyz: hit.world_xyz,
    uv: hit.uv,
    normal: hit.normal,
    created_at: new Date().toISOString(),
    source: "manual"
  };
  saveCalibrations();
  return true;
}

/* ================= 首次校准向导（抽查 7 点 + 对称修正） =================
 * 只抽查关键点：左眼/右眼/左中指/右中指/左足/右足/胸。用户逐点「确认或纠正」；
 * 纠正值经 x 镜像自动推得对侧（左右对称），与确认值一起写入锚点偏置表（向导级校准，
 * 修改 ANATOMY 锚点运行时副本，不覆盖出厂 anatomy-labels.json），整个模型映射即时生效。 */
var WIZARD = { active: false, steps: [], idx: 0, applied: null };

/* 7 点向导预设抽查点按模型隔离（各模型归一化局部系实测；男性为出厂表，女性由探针实测填入） */
var WIZARD_SPOTS_BY_MODEL = {
  "Custom3d66.glb": [
    { id: "left_eye",   zh: "左眼",   spot: [0.094, 2.134, 0.205], mirrorOf: "right_eye" },
    { id: "right_eye",  zh: "右眼",   spot: [-0.094, 2.134, 0.205],  mirrorOf: "left_eye" },
    { id: "left_middle_finger",  zh: "左中指", spot: [1.8471, 0.0487, 0.1058], mirrorOf: "right_middle_finger" },
    { id: "right_middle_finger", zh: "右中指", spot: [-1.8471, 0.0487, 0.1058],  mirrorOf: "left_middle_finger" },
    { id: "left_toes",  zh: "左脚",   spot: [0.6726, -2.4341, 0.3903], mirrorOf: "right_toes" },
    { id: "right_toes", zh: "右脚",   spot: [-0.6726, -2.4339, 0.3905],  mirrorOf: "left_toes" },
    { id: "chest",      zh: "胸",     spot: [0.0202, 0.2523, 0.2759], mirrorOf: null }
  ],
  "Female3d66.glb": [       // 女性模型 7 点预设：命名材质几何质心实测（tools/measure_female_anchors.py）
    { id: "left_eye",   zh: "左眼",   spot: [0.0906, 2.1341, 0.1593], mirrorOf: "right_eye" },
    { id: "right_eye",  zh: "右眼",   spot: [-0.0906, 2.1341, 0.1593],  mirrorOf: "left_eye" },
    { id: "left_middle_finger",  zh: "左中指", spot: [1.7358, 0.1454, 0.0671], mirrorOf: "right_middle_finger" },
    { id: "right_middle_finger", zh: "右中指", spot: [-1.7358, 0.1454, 0.0671],  mirrorOf: "left_middle_finger" },
    { id: "left_toes",  zh: "左脚",   spot: [0.7199, -2.4519, 0.2588], mirrorOf: "right_toes" },
    { id: "right_toes", zh: "右脚",   spot: [-0.7199, -2.4519, 0.2588],  mirrorOf: "left_toes" },
    { id: "chest",      zh: "胸",     spot: [-0.0, 0.949, 0.2737], mirrorOf: null }
  ]
};

function wizardSteps() {
  var spots = WIZARD_SPOTS_BY_MODEL[modelKey().split("/").pop()] || WIZARD_SPOTS_BY_MODEL["Custom3d66.glb"];
  return spots.map(function (s) { return Object.assign({}, s); });
}

function startWizard() {
  WIZARD.active = true;
  WIZARD.steps = wizardSteps();
  WIZARD.idx = 0;
  WIZARD.applied = { shifts: {} };   // 记录每锚点的纠偏向量（对侧镜像复用）
  var hint = document.getElementById("hint");
  if (hint) hint.textContent = "校准向导 1/" + WIZARD.steps.length + "：请点击「" + WIZARD.steps[0].zh + "」——将落点与系统识别对比后确认或纠正";
  var btn = document.getElementById("wizard");
  if (btn) btn.style.background = "#d9e9f8";
}

function wizardNext(spotWorld, userConfirmed) {
  var step = WIZARD.steps[WIZARD.idx];
  if (spotWorld && !userConfirmed) {
    // 纠正：用户点击的新位置即为该部位真实锚点；记录偏置（对侧镜像 x 取反）
    WIZARD.applied.shifts[step.id] = [spotWorld.x, spotWorld.y, spotWorld.z];
  }
  WIZARD.idx += 1;
  if (WIZARD.idx >= WIZARD.steps.length) { finishWizard(); return; }
  var hint = document.getElementById("hint");
  if (hint) hint.textContent = "校准向导 " + (WIZARD.idx + 1) + "/" + WIZARD.steps.length + "：请点击「" + WIZARD.steps[WIZARD.idx].zh + "」";
}

/* 应用校准结果：把纠偏写入 ANATOMY 锚点运行时副本（对侧自动镜像），整表生效 */
function applyWizardResult() {
  if (!ANATOMY || !ANATOMY.entries || !WIZARD.applied) return;
  var shifts = WIZARD.applied.shifts;
  var byId = {};
  ANATOMY.entries.forEach(function (e) { byId[e.anatomical_label] = e; });
  Object.keys(shifts).forEach(function (id) {
    var e = byId[id];
    if (!e) return;
    e.world_xyz = shifts[id];                        // 本侧直接采用用户纠正点
    e.provenance = "校准向导纠正（" + shifts[id].map(function (v) { return v.toFixed(3); }).join(",") + "）";
  });
  // 对称修正：只纠正了一侧时，另一侧按 x 镜像推得（人体左右对称）
  ["left_eye", "right_eye", "left_middle_finger", "right_middle_finger", "left_toes", "right_toes"].forEach(function (id) {
    var pair = id.indexOf("left_") === 0 ? id.replace("left_", "right_") : id.replace("right_", "left_");
    if (shifts[id] && !shifts[pair] && byId[pair]) {
      byId[pair].world_xyz = [-shifts[id][0], shifts[id][1], shifts[id][2]];
      byId[pair].provenance = "校准向导对称修正（镜像自 " + id + "）";
    }
  });
  // 清空 z_band 缓存依赖的手指锚点若被移动则带随更新（本版仅眼/足/中指锚点平移，z_band 以新 y 为准重排）
  localStorage.setItem("derm_wizard_applied", JSON.stringify({ model: modelKey(), shifts: shifts, at: new Date().toISOString() }));
  if (bridge && bridge.onCalibrationSaved) bridge.onCalibrationSaved(JSON.stringify({ wizard: { model: modelKey(), shifts: shifts } }));
}

function finishWizard() {
  applyWizardResult();
  WIZARD.active = false;
  var btn = document.getElementById("wizard");
  if (btn) btn.style.background = "";
  var hint = document.getElementById("hint");
  var n = Object.keys(WIZARD.applied.shifts).length;
  if (hint) hint.textContent = "校准完成：确认 7 点中纠正 " + n + " 点（对侧已自动镜像），模型映射已更新";
}

/* v29 女性模型外观对齐：用户要求「女性人体变为和男性一样的颜色」。男性 3d66 是无贴图
 * 白模（底色 0.5 中灰）；女性自带写实肤色/衣物贴图 → 对皮肤/衣物/指甲族材质去贴图并
 * 置同款 0.5 中灰。v31b 注意：不能置纯白——0.5 灰才有明暗梯度空间，纯白在浅色主题下
 * 曝白失去立体感（男性看得清、女性糊成一片的根因）。
 * 保留：发片（棕，非人体）、虹膜/瞳孔/角膜（眼球有色层）、睫毛。法线贴图保留（表面起伏细节）。 */
var FEMALE_WHITE_MATERIALS = ["Face", "Torso", "Arms", "Legs", "Ears", "Lips", "Fingernails", "Toenails",
  "EyeSocket", "EyeMoisture", "adskMatEyeMoisture", "Mouth", "Teeth", "shirt", "trims", "Mat3d66_853441_4_1820"];
function applyFemaleWhiteFinish(root) {
  var n = 0;
  root.traverse(function (o) {
    if (!o.isMesh || !o.material) return;
    var mats = Array.isArray(o.material) ? o.material : [o.material];
    mats.forEach(function (m) {
      if (!m || FEMALE_WHITE_MATERIALS.indexOf(m.name) === -1) return;
      m.map = null;
      m.color.setRGB(0.5, 0.5, 0.5);   // 与男性 glb baseColorFactor 0.5 一致（纯白会曝白）
      m.needsUpdate = true;
      n++;
    });
  });
  return n;
}

/* 模型加载：idx 省略 = 启动自动链（失败顺延下一个模型，全失败回落程序化人台）；
 * manual=true = 用户显式切换（失败只报错，不静默跳到别的模型）。切换成功后按新模型
 * 重拉锚点/逐面语义表（loadAnatomyConfig/loadFaceSemantics 按 modelIndex 取文件）。 */
function loadHumanModel(idx, manual) {
  if (idx === undefined) idx = modelIndex;
  modelIndex = idx;
  var loader = new THREE.GLTFLoader();
  fetch(MODELS[idx].file, { cache: "no-cache" }).then(function (response) {
    if (!response.ok) throw new Error("HTTP " + response.status);
    return response.arrayBuffer();
  }).then(function (arrayBuffer) {
    return computeSha256Hex(arrayBuffer).then(function (sha256) {
      loader.parse(arrayBuffer, "", function (gltf) {
        registerHuman(gltf, MODELS[idx].name, sha256, MODELS[idx].file);
        if (MODELS[idx].file.indexOf("Female3d66.glb") !== -1) {
          var nW = applyFemaleWhiteFinish(modelRoot);   // v29：女性人体去贴图置白（对齐男性白模）
          console.log("[female-finish] 白模化材质 " + nW + " 个");
        }
        loadAnatomyConfig();                  // 切模型后重取该模型的锚点/逐面语义表
        loadFaceSemantics();
      }, undefined, function (err) { throw err; });
    });
  }).catch(function (err) {
    console.warn("人体模型加载失败（" + idx + "）：", err);
    if (manual) { alert("模型加载失败：" + MODELS[idx].file); return; }
    modelIndex += 1;
    if (modelIndex < MODEL_FILES.length) loadHumanModel();
    else { console.warn("全部模型失败，回落程序化人台"); buildProceduralBody(); }
  });
}

/* 导入自定义 GLB（图生 3D 服务产物一键接入；Blob URL 规避 file:// CORS） */
function importCustomGLB(arrayBuffer, fileName) {
  var blobUrl = URL.createObjectURL(new Blob([arrayBuffer], { type: "model/gltf-binary" }));
  var loader = new THREE.GLTFLoader();
  computeSha256Hex(arrayBuffer).then(function (sha256) {
    loader.load(blobUrl, function (gltf) {
      URL.revokeObjectURL(blobUrl);
      var customKey = "custom:" + sha256;
      registerHuman(gltf, fileName || "自定义 GLB", sha256, customKey);
    }, undefined, function (err) {
      URL.revokeObjectURL(blobUrl);
      console.error("自定义 GLB 解析失败：", err);
      alert("GLB 解析失败，请确认文件为有效 glTF-Binary（.glb）");
    });
  });
}

/* ================= 程序化人台（全部模型失败时的兜底） ================= */

function skinMat() {
  return new THREE.MeshPhysicalMaterial({ color: SKIN, roughness: 0.55, metalness: 0.02, sheen: 0.5 });
}
function capsule(r, len) { return new THREE.CapsuleGeometry(r, len, 8, 24); }
function ball(r) { return new THREE.SphereGeometry(r, 32, 24); }

function buildProceduralBody() {
  function addPart(regionId, geometry, x, y, z) {
    var mesh = new THREE.Mesh(geometry, skinMat());
    mesh.position.set(x, y, z);
    mesh.castShadow = true;
    scene.add(mesh);
    (regionMeshes[regionId] = regionMeshes[regionId] || []).push(mesh);
    return mesh;
  }
  var skull = addPart("head_face", ball(0.42), 0, 2.56, 0); skull.scale.set(0.92, 1.16, 1.0);
  var jaw = addPart("head_face", ball(0.30), 0, 2.30, 0.06); jaw.scale.set(0.82, 0.9, 0.92);
  [-1, 1].forEach(function (s) { var e = addPart("head_face", ball(0.085), s * 0.40, 2.44, -0.02); e.scale.set(0.45, 1.0, 0.75); });
  addPart("neck", capsule(0.145, 0.34), 0, 2.06, -0.01);
  var profile = [[0.235, 2.02], [0.34, 1.88], [0.505, 1.72], [0.545, 1.42], [0.50, 1.05], [0.475, 0.72], [0.52, 0.42], [0.55, 0.18]]
    .map(function (p) { return new THREE.Vector2(p[1], p[0]); });
  addPart("trunk", new THREE.LatheGeometry(profile, 40), 0, 0, 0).scale.z = 0.62;
  [-1, 1].forEach(function (s) {
    var side = s < 0 ? "_l" : "_r";
    var d = addPart("arm" + side, ball(0.20), s * 0.60, 1.72, 0); d.scale.set(1.0, 1.15, 0.95);
    addPart("arm" + side, capsule(0.125, 0.62), s * 0.70, 1.30, 0);
    addPart("arm" + side, capsule(0.105, 0.58), s * 0.82, 0.55, 0.03);
    var palm = addPart("hand" + side, ball(0.115), s * 0.85, 0.12, 0.05); palm.scale.set(0.85, 1.55, 0.55);
    addPart("leg" + side, capsule(0.185, 0.85), s * 0.30, -0.72, 0);
    addPart("leg" + side, capsule(0.13, 0.78), s * 0.30, -1.85, 0.01);
    var foot = addPart("foot" + side, ball(0.13), s * 0.30, -2.50, 0.14); foot.scale.set(0.95, 0.6, 2.1);
  });
  var hips = addPart("groin", ball(0.5), 0, 0.08, 0); hips.scale.set(0.92, 0.72, 0.60);
  setView("front");
}

/* ================= 选择（逐点独立记录）与标记 =================
 * v18：每次点击 = 独立三维表面位置记录 + 独立标记。
 * 即使同一条左大腿连点不同位置，也生成不同记录/标记（绝不合并、覆盖、去重）。
 * 删除仅经列表 ×（按 uid）；「清空全部」是唯一需确认的删除操作。 */

function addRegionSelection(entry) {
  if (!scene) return;                       // 页面重载竞态防护
  selectedRegions.push({
    uid: ++selSeq,
    id: entry.id,
    label: entry.label,
    pos: entry.pos ? entry.pos.clone() : new THREE.Vector3(),
    confidence: entry.confidence === undefined ? null : entry.confidence,
    source: entry.source || null,            // v23：位置识别来源（人工校准/逐面语义表/锚点/型带/无法可靠识别）
    note: "",                                // 逐点备注
    desc: entry.desc || null,                // 「解剖区域+方位+相对位置+坐标」主描述
    parts: entry.parts || null,              // 描述分项
    hit: entry.hit || null                   // 完整真实命中数据（含 mesh/faceIndex/坐标/变换）
  });
  renderSelection();
}

/* 列表 × 删除：按 uid 精确移除一条（列表项 + 3D 标记 + 上报数据同步更新） */
function removeSelected(uid) {
  var idx = selectedRegions.findIndex(function (x) { return x.uid === uid; });
  if (idx === -1) return false;
  selectedRegions.splice(idx, 1);
  renderSelection();
  return true;
}

/* 逐点备注：不改变记录与标记，只更新 note 并刷新列表 */
function setRegionNote(uid, note) {
  var x = selectedRegions.find(function (s) { return s.uid === uid; });
  if (!x) return false;
  x.note = String(note == null ? "" : note).slice(0, 200);
  renderSelection();
  return true;
}

function clearSelection() {
  selectedRegions.length = 0;
  renderSelection();
  if (typeof updateSendBtn === "function") updateSendBtn();   // v22：清空后同步禁用发送
}

/* 清空全部（唯一需要确认的删除操作） */
function clearAllConfirmed() {
  if (!selectedRegions.length) return;
  if (!window.confirm("确认清空全部 " + selectedRegions.length + " 个已选位置标记？")) return;
  clearSelection();
}

function renderSelection() {
  if (!scene) return;
  while (markerPool.length < selectedRegions.length) {
    var m = new THREE.Mesh(new THREE.SphereGeometry(0.07, 20, 16),
      new THREE.MeshBasicMaterial({ color: HIGHLIGHT }));
    m.userData.noPick = true;                // v18：标记不参与 Raycaster 命中
    scene.add(m);
    markerPool.push(m);
  }
  markerPool.forEach(function (m, i) {
    if (i < selectedRegions.length) {
      m.position.copy(selectedRegions[i].pos);   // 世界坐标：模型根静止，旋转/缩放视角后标记恒在体表
      m.userData.regionUid = selectedRegions[i].uid;   // v74-fix：标记 ←→ 记录关联（点标记可删除）
      m.visible = true;
    } else {
      m.visible = false;
    }
  });
  // —— 已选位置列表：每行「主描述 + 展开(▸) + 备注(✎) + 独立 ×」 ——
  var listEl = document.getElementById("selList");
  if (listEl) {
    listEl.innerHTML = "";
    if (!selectedRegions.length) {
      var empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "点击 3D 模型表面选择位置，每次点击 = 独立记录；点已有标记可删除";
      listEl.appendChild(empty);
    }
    selectedRegions.forEach(function (x) {
      var row = document.createElement("div");
      row.className = "sel-row" + (DEBUG_MODE ? " dbg" : "");
      var txt = document.createElement("span");
      txt.className = "sel-label";
      txt.textContent = x.desc || x.label;
      txt.title = x.desc || x.label;
      var ops = document.createElement("span");
      ops.className = "sel-ops";
      // ▸ 展开：精确 3D 坐标 / 命中 Mesh/Node / faceIndex / uv / 法线 / 模型变换
      var btnEx = document.createElement("button");
      btnEx.className = "sel-ex";
      btnEx.textContent = "▸";
      btnEx.title = "展开命中详情";
      var detail = document.createElement("pre");
      detail.className = "sel-detail";
      detail.style.display = "none";
      btnEx.addEventListener("click", function () {
        var open = detail.style.display === "none";
        detail.style.display = open ? "block" : "none";
        btnEx.textContent = open ? "▾" : "▸";
      });
      // ✎ 备注：prompt 逐点备注
      var btnNote = document.createElement("button");
      btnNote.className = "sel-note";
      btnNote.textContent = "✎";
      btnNote.title = "备注";
      if (x.note) btnNote.style.background = "#d8efdd";
      btnNote.addEventListener("click", function () {
        var t = window.prompt("备注（可空）：", x.note || "");
        if (t !== null) setRegionNote(x.uid, t);
      });
      // × 删除：立即删除该条（列表项 + 3D 标记 + 上报数据），不影响其他点
      var btn = document.createElement("button");
      btn.className = "sel-del";
      btn.textContent = "×";
      btn.title = "删除：" + (x.desc || x.label);
      btn.addEventListener("click", function () { removeSelected(x.uid); });
      ops.appendChild(btnEx); ops.appendChild(btnNote); ops.appendChild(btn);
      row.appendChild(txt); row.appendChild(ops);
      detail.textContent = detailText(x);
      row.appendChild(detail);
      listEl.appendChild(row);
    });
    listEl.style.display = selectedRegions.length ? "block" : "block";
    // v21：顶部计数徽标。v22 修复：点击选点绝不自动打开抽屉/弹窗——「已选位置」
    // 面板只能由用户主动点「已选」按钮或 ＋ 菜单打开（用户明确要求）。
    var cnt = document.getElementById("selCount");
    if (cnt) cnt.textContent = String(selectedRegions.length);
  }
  var labels = selectedRegions.map(function (x) { return x.desc || x.label; });
  var labelEl = document.getElementById("regionLabel");
  if (labelEl) labelEl.textContent =
    labels.length ? "已选（" + labels.length + " 处，列表 × 可删除）：" + labels.join(" ｜ ") : "未选择区域";
  if (typeof updateSendBtn === "function") updateSendBtn();   // v22：选点/删除联动发送按钮可用性
  if (typeof updateMiniTag === "function") updateMiniTag();   // v58：3D 小窗简短部位标签同步
  notifyRegion(labels);
}

/* 展开详情：精确坐标（模型局部+世界）、命中 Mesh/Node、faceIndex、uv、法线、变换、调试信息 */
function detailText(x) {
  var L = [];
  L.push("记录 #" + x.uid + (x.note ? "（备注：" + x.note + "）" : ""));
  L.push("位置描述：" + (x.desc || x.label));
  var h = x.hit;
  if (h) {
    L.push("模型局部坐标：" + JSON.stringify(h.model_local_xyz));
    L.push("世界坐标：" + JSON.stringify(h.world_xyz));
    L.push("命中 Mesh：" + h.mesh_name + "　Node 链：" + (h.node_chain || []).join(" ← "));
    L.push("faceIndex：" + h.face_index + "　instanceId：" + (h.instanceId === null ? "null（非 InstancedMesh）" : h.instanceId));
    L.push("uv：" + (h.uv ? JSON.stringify(h.uv) : "null（该模型无 UV 通道）"));
    L.push("法线（世界）：" + JSON.stringify(h.normal) + (h.normal_corrected ? "（已按可见性校正为外法线）" : ""));
    L.push("Bone/Skin：" + (h.bone ? h.bone.name : "null（该模型无骨骼）") + " / " + (h.skin || "null"));
    if (h.model_transform) L.push("模型变换（旋转/缩放/平移）：" + JSON.stringify(h.model_transform));
    L.push("相机距离：" + h.distance);
    L.push("位置识别来源：" + (x.source || "未知"));
    L.push("置信度：" + x.confidence);
    if (x.parts && x.parts.fsemNote) L.push("说明：" + x.parts.fsemNote);
  } else {
    L.push("（无原始命中数据：内部构造点）");
  }
  return L.join("\n");
}

function notifyRegion(labels) {
  if (labels.length && bridge && bridge.onRegionSelected) bridge.onRegionSelected("multi|" + labels.join("、"));
  try {
    var full = selectedRegions.map(function (x) {
      return { uid: x.uid, label: x.desc || x.label, note: x.note,
               model_local_xyz: x.hit ? x.hit.model_local_xyz : null,
               world_xyz: x.hit ? x.hit.world_xyz : null,
               mesh_name: x.hit ? x.hit.mesh_name : null, face_index: x.hit ? x.hit.face_index : null };
    });
    var msg = JSON.stringify({ type: "derm_region", regions: labels, regions_full: full });
    if (window.parent && window.parent !== window) window.parent.postMessage(msg, "*");
    if (window.opener) window.opener.postMessage(msg, "*");
  } catch (e) { /* 跨源等场景忽略 */ }
  // v21 统一提交框架：每次选点变化 → 同步输入框位置标签（chips）
  renderChips();
}

/* ================= v21 统一提交框架（Codex 风格底部输入框） =================
 * 点击 3D 模型 → 底部输入框生成位置标签（chips）；文字与位置标签共存同一输入区；
 * 发送时合并「全部已选位置 + 文字 + 图片附件」为一条结构化请求（regions 紧凑 + regions_full 完整）；
 * 标签 × 单独删除 → 同步删除 3D 标记与记录；历史消息卡完整留存。
 * 发送为本地回执（结构化载荷 console + bridge 上报），不接真实诊断 API。 */
var CHAT_FILES = [];           // 附件：[{name, size, dataUrl}]
var CHAT_HISTORY = [];         // 结构化消息记录：[{n, time, text, regions, files, payload}]
var committedRegionKeys = {};  // 已即时写入病历单的 3D 部位；不再重复显示/提交到输入框
var chatSeq = 0;
var recentSessionToken = 0;   // v64-P7：会话级「最近」条目 token——点新对话换 token，旧会话条目固化

function esc(s) {
  return String(s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}

/* 选点/附件 → 输入框 chips：已写入病历单的部位不重复显示；附件仍正常显示。 */
function pendingSelectedRegions() {
  return selectedRegions.filter(function (x) {
    return !committedRegionKeys[String(x.desc || x.label || "")];
  });
}
function renderChips() {
  var row = document.getElementById("chipsRow");
  if (!row) return;
  row.innerHTML = "";
  pendingSelectedRegions().forEach(function (x) {
    var chip = document.createElement("span");
    chip.className = "chip";
    var dot = document.createElement("span"); dot.className = "chip-dot";
    var b = document.createElement("b");
    b.textContent = x.desc || x.label;
    b.title = (x.desc || x.label) + (x.hit ?
      "\nMesh: " + x.hit.mesh_name + " ← " + (x.hit.node_chain || []).join(" ← ") +
      "\nfaceIndex: " + x.hit.face_index +
      "\n局部坐标: " + JSON.stringify(x.hit.model_local_xyz) +
      "\n世界坐标: " + JSON.stringify(x.hit.world_xyz) : "");
    var xn = document.createElement("button");
    xn.className = "x"; xn.textContent = "×"; xn.title = "删除该位置标签（同步删除 3D 标记与记录）";
    xn.addEventListener("click", function () { removeSelected(x.uid); });
    chip.appendChild(dot); chip.appendChild(b); chip.appendChild(xn);
    row.appendChild(chip);
  });
  CHAT_FILES.forEach(function (f, i) {
    var chip = document.createElement("span");
    chip.className = "chip chip-file";
    var b = document.createElement("b");
    b.textContent = "📎 " + f.name + "（" + Math.round(f.size / 1024) + " KB）";
    var xn = document.createElement("button");
    xn.className = "x"; xn.textContent = "×"; xn.title = "移除附件";
    xn.addEventListener("click", function () { CHAT_FILES.splice(i, 1); renderChips(); updateSendBtn(); });
    chip.appendChild(b); chip.appendChild(xn);
    row.appendChild(chip);
  });
  var pending = pendingSelectedRegions();
  row.style.display = (pending.length || CHAT_FILES.length) ? "flex" : "none";
  row.scrollTop = row.scrollHeight;   /* 标签区限高滚动：新增后始终滚到最新一条 */
  updateSendBtn();
}

function updateSendBtn() {
  var btn = document.getElementById("sendBtn");
  var input = document.getElementById("chatInput");
  if (!btn || !input) return;
  var hasText = input.value.trim().length > 0;
  var hasContent = hasText || pendingSelectedRegions().length > 0 || CHAT_FILES.length > 0;
  if (window.__chatBusy) { btn.disabled = false; btn.title = "中止生成"; return; }   // v74：生成中=停止按钮，恒可点
  btn.disabled = !hasContent;
  btn.title = hasContent ? "发送（Enter）" : "输入文字或点击 3D 模型选择位置后可发送";
}

/* 结构化载荷：位置（紧凑+完整）+ 文字 + 文件（dataUrl 不入 payload 防爆栈，仅本地留存） */
function buildStructuredPayload(text, clinicOverride) {
  return {
    v: 21,
    type: "derm consult request",
    text: text || "",
    regions: pendingSelectedRegions().map(function (x) { return x.desc || x.label; }),
    regions_full: pendingSelectedRegions().map(function (x) {
      return {
        uid: x.uid, label: x.label, desc: x.desc, note: x.note || null,
        source: x.source || null,
        confidence: x.confidence != null ? x.confidence : null,
        mesh: x.hit ? x.hit.mesh_name : null,
        node_chain: x.hit ? x.hit.node_chain : null,
        face_index: x.hit ? x.hit.face_index : null,
        model_local_xyz: x.hit ? x.hit.model_local_xyz : null,
        world_xyz: x.hit ? x.hit.world_xyz : null,
        normal: x.hit ? x.hit.normal : null,
        uv: x.hit ? x.hit.uv : null,
        bone: x.hit ? x.hit.bone : null,
        skin: x.hit ? x.hit.skin : null,
        weights: x.hit ? x.hit.weights : null
      };
    }),
    files: CHAT_FILES.map(function (f) { return { name: f.name, size: f.size }; }),
    clinic: (clinicOverride !== undefined) ? clinicOverride : clinicalPayload()
  };
}

/* ================= 阶段3：医生资料（＋菜单唯一入口 → 抽屉录入 → 随消息提交） ================= */
/* 影像安全口径：可解码验证的位图格式（png/jpg/webp/bmp/gif）才收；浏览器无法解码验证的
 * （tiff/dicom/pdf…）一律拒绝并注明原因——不伪造「已导入」。载荷只带元数据，不带文件本体。 */
var CLINIC_IMG_EXTS = ["png", "jpg", "jpeg", "webp", "bmp", "gif"];
var CLINIC_IMG_MAX = 10 * 1024 * 1024;   // 与图片附件同一 10MB 上限
var CLINIC_REPORT_MAX = 2 * 1024 * 1024;

function clinicalEmpty() {
  return { patient: null, hx: "", labs: [], report: "", meds: "", allergy: "", past: "", tx: "", imaging: [] };
}
function clinicalHas(c) {
  return !!(c && (c.patient || c.hx || c.report || c.meds || c.allergy || c.past || c.tx ||
    (c.labs && c.labs.length) || (c.imaging && c.imaging.length)));
}

/* v56 门诊病历单：就诊日期自动填今天；门诊号留空且已填姓名时自动生成一次（写回输入框，医生可改） */
var OPD_SEQ = 0;
function todayLabel() {
  var d = new Date();
  function p(n) { return (n < 10 ? "0" : "") + n; }
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
}
function ensurePatientAuto() {
  var dateEl = document.getElementById("clinicPatDate");
  if (dateEl && !dateEl.value) dateEl.value = todayLabel();
  var noEl = document.getElementById("clinicPatNo");
  var nameEl = document.getElementById("clinicPatName");
  if (noEl && !noEl.value && nameEl && nameEl.value) {
    OPD_SEQ += 1;
    var seq = String(OPD_SEQ); while (seq.length < 3) seq = "0" + seq;
    noEl.value = "OPD-" + todayLabel().replace(/-/g, "") + "-" + seq;
  }
}

/* 从抽屉表单收集当前资料（影像列表单独维护，不重读 DOM）。 */
function collectClinical() {
  var c = clinicalEmpty();
  var val = function (id) { var el = document.getElementById(id); return el ? el.value.trim() : ""; };
  ensurePatientAuto();
  var pat = { name: val("clinicPatName"), sex: (document.getElementById("clinicPatSex") || {}).value || "",
              age: val("clinicPatAge"), no: val("clinicPatNo") };
  var dateEl = document.getElementById("clinicPatDate");
  if (pat.name || pat.sex || pat.age || pat.no) {
    pat.date = dateEl ? dateEl.value : "";
    c.patient = pat;
  }
  c.hx = val("clinicHx"); c.report = val("clinicReport");
  c.meds = val("clinicMeds"); c.allergy = val("clinicAllergy");
  c.past = val("clinicPast"); c.tx = val("clinicTx");
  Array.prototype.slice.call(document.querySelectorAll("#labRows .lab-row")).forEach(function (row) {
    var inputs = row.querySelectorAll("input");
    var name = inputs[0] ? inputs[0].value.trim() : "";
    var v = inputs[1] ? inputs[1].value.trim() : "";
    var unit = inputs[2] ? inputs[2].value.trim() : "";
    if (name || v) c.labs.push({ name: name || "（未命名）", value: v, unit: unit });
  });
  c.imaging = CLINIC_IMAGING.map(function (m) {
    return { name: m.name, size: m.size, mime: m.mime, status: m.status, reason: m.reason || null,
             width: m.width || null, height: m.height || null, quality_note: m.quality_note || null };
  });
  return c;
}

/* 载荷集成：无资料时返回 null（字段缺省即「未提供」，不伪造空对象）。 */
function clinicalPayload() {
  var c = collectClinical();
  return clinicalHas(c) ? c : null;
}

/* 历史卡/审计用摘要（不含正文内容，只报条目构成）。 */
function clinicalSummary(c) {
  if (!clinicalHas(c)) return null;
  var parts = [];
  if (c.patient) parts.push("患者信息");
  if (c.hx) parts.push("病史");
  if (c.labs && c.labs.length) parts.push("检验" + c.labs.length + "项");   // v73-fix：脱敏载荷可能无 labs/imaging 键
  if (c.report) parts.push("报告");
  if (c.meds) parts.push("用药");
  if (c.allergy) parts.push("过敏史");
  if (c.past) parts.push("既往史");
  if (c.tx) parts.push("治疗");
  if (c.imaging && c.imaging.length) parts.push("影像" + c.imaging.filter(function (m) { return m.status === "ready"; }).length + "张");
  return parts.join("/");
}

var CLINIC_IMAGING = [];   // [{name, size, mime, status: ready|rejected, reason, width, height, quality_note}]

/* ================= 阶段4：医生辅助面板（逐项选择/默认脱敏/参考病例逐个确认） ================= */
/* 资料选择状态：默认全选；取消项不进发送载荷（表单原文保留）。影像按条目名逐张选择。 */
var CLINIC_SELECT = { patient: true, hx: true, labs: true, report: true, meds: true, allergy: true, past: true, tx: true };
var CLINIC_SELECT_IMAGING = {};   // name -> true/false（默认跟随 ready 状态全选）
var REF_CASE_CONFIRMED = {};      // 历史消息 n -> true：医生逐个确认后才可作为参考病例

var CLINIC_SELECT_LABELS = {
  patient: "患者基本信息", hx: "病史 / 观察", labs: "结构化检验", report: "报告文字", meds: "用药",
  allergy: "过敏史", past: "既往史", tx: "当前治疗"
};

/* 默认脱敏：直接身份信息以类别标记替换。只用于发送载荷；表单与历史卡保留原文。
 * 替换顺序：长模式（身份证）先于短模式（手机/座机）——否则身份证中段会被手机模式吃掉。 */
function sanitizeText(s) {
  var t = String(s == null ? "" : s);
  t = t.replace(/\d{6}(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[\dXx]/g, "［身份证号］");
  t = t.replace(/1[3-9]\d{9}/g, "［手机号］");
  t = t.replace(/0\d{2,3}-?\d{7,8}/g, "［电话］");
  t = t.replace(/(患者|姓名)\s*[:：]\s*[一-龥·A-Za-z]{1,6}/g, "$1：［姓名］");
  t = t.replace(/(住址|地址|现住)\s*[:：]\s*[^\n，。；,;]{2,40}(路|街|巷|道|村|号楼|号院|栋|单元)[^\n，。；,;]*/g, "$1：［地址］");
  return t;
}
function sanitizeClinic(c) {
  if (!c) return c;
  var out = {};
  Object.keys(c).forEach(function (k) {
    if (k === "patient") {   // v56：直接身份标识（姓名/门诊号）按现有原则类别替换；性别/年龄/日期是临床信息保留
      if (c.patient && (c.patient.name || c.patient.sex || c.patient.age || c.patient.no)) {
        out.patient = { name: c.patient.name ? "［姓名］" : "", sex: c.patient.sex || "",
                        age: c.patient.age || "", no: c.patient.no ? "［门诊号］" : "",
                        date: c.patient.date || "" };
      }
    }
    else if (k === "labs") {
      if (c.labs && c.labs.length) out.labs = c.labs.map(function (l) {
        return { name: sanitizeText(l.name), value: sanitizeText(l.value), unit: sanitizeText(l.unit) };
      });
    }
    else if (k === "imaging") { if (c.imaging && c.imaging.length) out.imaging = c.imaging; }   // 影像条目只含文件元数据，无身份文本
    else if (typeof c[k] === "string") { if (c[k]) out[k] = sanitizeText(c[k]); }   // 空/未选字段不出现（缺省即未提供）
    else out[k] = c[k];
  });
  return out;
}

/* 候选参考病例：本会话此前提交过的结构化消息（未经确认不参与分析）。 */
function candidateRefCases(currentSeq) {
  return CHAT_HISTORY.filter(function (m) {
    return m.n !== currentSeq && (m.text || (m.regions && m.regions.length) || m.clinicSummary);
  }).map(function (m) {
    return { n: m.n, time: m.time, text: (m.text || "").slice(0, 60),
      regions: (m.regions || []).length, clinic: m.clinicSummary || null };
  });
}

/* 资料选择 + 参考病例应用后的载荷（阶段4 语义：取消项不发送）。 */
function clinicalPayloadSelected(currentSeq) {
  var c = collectClinical();
  var selected = [], excluded = [];
  var picked = clinicalEmpty();
  Object.keys(CLINIC_SELECT_LABELS).forEach(function (k) {
    if (CLINIC_SELECT[k]) { picked[k] = c[k]; selected.push(CLINIC_SELECT_LABELS[k]); }
    else if (c[k]) excluded.push(CLINIC_SELECT_LABELS[k]);
  });
  var readyImaging = c.imaging.filter(function (m) { return m.status === "ready"; });
  picked.imaging = readyImaging.filter(function (m) {
    if (CLINIC_SELECT_IMAGING[m.name] === false) { excluded.push("影像：" + m.name); return false; }
    return true;
  });
  var refs = candidateRefCases(currentSeq).filter(function (r) { return REF_CASE_CONFIRMED[r.n]; });
  // 无资料且无已确认参考病例且无明确排除 → 不携带；有明确排除项时仍返回载荷，
  // 让接收端看到医生的选择（excluded_items），不把「被取消」伪装成「未提供」。
  if (!clinicalHas(picked) && !refs.length && !excluded.length) return null;
  var payload = sanitizeClinic(picked);
  if (refs.length) payload.ref_cases = refs.map(function (r) {
    return { seq: r.n, time: r.time, text: sanitizeText(r.text), regions: r.regions, clinic: r.clinic };
  });
  payload.deidentified = true;
  payload.selected_items = selected;
  if (excluded.length) payload.excluded_items = excluded;
  return payload;
}

function renderClinicImaging() {
  var el = document.getElementById("clinicImaging");
  if (!el) return;
  el.innerHTML = "";
  if (!CLINIC_IMAGING.length) {
    var d = document.createElement("div"); d.className = "empty"; d.textContent = "尚无影像";
    el.appendChild(d); return;
  }
  CLINIC_IMAGING.forEach(function (m, i) {
    var row = document.createElement("div");
    row.className = "img-item " + (m.status === "ready" ? "ready" : "rejected");
    var txt = (m.status === "ready" ? "✔ " : "✘ ") + m.name + "（" + Math.round(m.size / 1024) + " KB）";
    if (m.status === "ready") txt += " · " + m.width + "×" + m.height + (m.quality_note ? " · " + m.quality_note : "");
    else txt += " · 已安全拒绝：" + (m.reason || "不支持的格式");
    row.textContent = txt;
    row.title = "点击移除";
    row.addEventListener("click", function () { CLINIC_IMAGING.splice(i, 1); renderClinicImaging(); });
    el.appendChild(row);
  });
}

/* 影像检查链：扩展名白名单 → 大小上限 → FileReader 读入 → Image 解码验证 → 分辨率质量注记。
 * 任一步失败：状态 rejected + 原因（安全降级——文件不进任何载荷与存储）。 */
function addClinicImageFile(file) {
  var ext = (file.name.split(".").pop() || "").toLowerCase();
  var entry = { name: file.name, size: file.size, mime: file.type || "", status: "rejected",
    reason: null, width: null, height: null, quality_note: null };
  CLINIC_IMAGING.push(entry);
  var fail = function (reason) { entry.status = "rejected"; entry.reason = reason; renderClinicImaging(); };
  if (CLINIC_IMG_EXTS.indexOf(ext) === -1) { fail("不支持的格式 ." + ext + "（仅 png/jpg/webp/bmp/gif）"); return; }
  if (file.size > CLINIC_IMG_MAX) { fail("超过 10MB 上限"); return; }
  if (file.size <= 0) { fail("空文件"); return; }
  var reader = new FileReader();
  reader.onload = function () {
    var img = new Image();
    img.onload = function () {
      entry.status = "ready";
      entry.width = img.naturalWidth; entry.height = img.naturalHeight;
      if (img.naturalWidth < 200 || img.naturalHeight < 200) entry.quality_note = "低分辨率";
      renderClinicImaging();
    };
    img.onerror = function () { fail("文件无法解码验证（疑似损坏或扩展名不符）"); };
    img.src = String(reader.result);
  };
  reader.onerror = function () { fail("文件读取失败"); };
  reader.readAsDataURL(file);
  renderClinicImaging();
}

/* 检验表行构造（initClinicUI 与验证脚本共用）。 */
function addClinicLabRow(name, v, unit) {
  var labRows = document.getElementById("labRows");
  if (!labRows) return null;
  var row = document.createElement("div");
  row.className = "lab-row";
  var i1 = document.createElement("input"); i1.type = "text"; i1.placeholder = "项目"; i1.value = name || "";
  var i2 = document.createElement("input"); i2.type = "text"; i2.placeholder = "结果"; i2.value = v || "";
  var i3 = document.createElement("input"); i3.type = "text"; i3.placeholder = "单位"; i3.value = unit || "";
  var del = document.createElement("button"); del.className = "lab-del"; del.textContent = "×"; del.title = "删除该行";
  del.addEventListener("click", function () { row.remove(); });
  row.appendChild(i1); row.appendChild(i2); row.appendChild(i3); row.appendChild(del);
  labRows.appendChild(row);
  return row;
}

function initClinicUI() {
  var entry = document.getElementById("clinicEntry");
  var drawer = document.getElementById("clinicDrawer");
  var close = document.getElementById("clinicClose");
  var labRows = document.getElementById("labRows");
  if (!entry || !drawer) return;
  /* v73：病历单内 3D 选点入口——完成选点后 navLeave3d 自动写回「身体部位」字段 */
  var d3btn = document.getElementById("clinic3dBtn");
  if (d3btn) d3btn.addEventListener("click", function (ev) {
    ev.stopPropagation();
    NAV.clinic3dPick = true;
    NAV.returnFrom3d = "chat";
    navSetWorkspace("3d");
  });
  /* v73：标题栏面板开关（图标按钮）——复用病历单开合逻辑（同侧互斥/渲染） */
  var pbtn = document.getElementById("wsPanelToggle");
  if (pbtn) pbtn.addEventListener("click", function (ev) {
    ev.stopPropagation();
    entry.click();
  });
  function status(msg) { var el = document.getElementById("clinicStatus"); if (el) el.textContent = msg || ""; }
  /* v73：保存确认·进入下一步——病历单内容随消息提交给智能体（相当于保存进病例并推进流程） */
  var saveNext = document.getElementById("clinicSaveNext");
  if (saveNext) saveNext.addEventListener("click", function (ev) {
    ev.stopPropagation();
    if (!clinicalPayload()) { status("病历单为空：请先填写至少一项内容再保存。"); return; }
    drawer.classList.remove("open");
    status("");
    // v73-fix：不写入输入框（用户会看到「只输入未发送」的假象），直接作为消息文本发送
    sendChat(t("病历单已填写完成，请进入下一步。", "Record form completed; please proceed to the next step."));
  });
  entry.addEventListener("click", function (ev) {
    ev.stopPropagation();
    drawer.classList.toggle("open");
    document.getElementById("plusMenu").classList.remove("open");
    var assistDrawer = document.getElementById("assistDrawer");
    if (drawer.classList.contains("open") && assistDrawer) assistDrawer.classList.remove("open");   // 同侧互斥
  });
  close.addEventListener("click", function () { drawer.classList.remove("open"); });
  document.getElementById("labAddRow").addEventListener("click", function () { addClinicLabRow(); status(""); });
  document.getElementById("clinicImgAdd").addEventListener("click", function () {
    document.getElementById("clinicImgFile").click();
  });
  document.getElementById("clinicImgFile").addEventListener("change", function () {
    Array.prototype.slice.call(this.files || []).forEach(function (f) { addClinicImageFile(f); });
    this.value = "";
  });
  document.getElementById("clinicReportImport").addEventListener("click", function () {
    document.getElementById("reportFile").click();
  });
  document.getElementById("reportFile").addEventListener("change", function () {
    var f = (this.files || [])[0]; this.value = "";
    if (!f) return;
    if (f.size > CLINIC_REPORT_MAX) { status("报告文件超过 2MB 上限，已拒绝：" + f.name); return; }
    var reader = new FileReader();
    reader.onload = function () {
      var el = document.getElementById("clinicReport");
      var text = String(reader.result || "");
      // .html 报告：剥掉标签取纯文本（不渲染、不执行任何内嵌内容）
      if (/\.html?$/i.test(f.name)) {
        var div = document.createElement("div");
        div.textContent = text;                       // 先按纯文本转义，杜绝脚本注入
        text = (div.textContent || "").replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
      }
      el.value = el.value ? el.value + "\n" + text : text;
      status("已导入报告：" + f.name + "（" + Math.round(f.size / 1024) + " KB）");
    };
    reader.onerror = function () { status("报告文件读取失败：" + f.name); };
    reader.readAsText(f, "utf-8");
  });
  document.getElementById("clinicClear").addEventListener("click", function () {
    if (!confirm("确定清空病历单全部资料？")) return;
    ["clinicHx", "clinicReport", "clinicMeds", "clinicAllergy", "clinicPast", "clinicTx",
     "clinicPatName", "clinicPatAge", "clinicPatNo", "clinicPatDate"].forEach(function (id) {
      var el = document.getElementById(id); if (el) el.value = "";
    });
    var sexEl = document.getElementById("clinicPatSex"); if (sexEl) sexEl.value = "";
    labRows.innerHTML = "";
    CLINIC_IMAGING = [];
    CLINIC_SELECT_IMAGING = {};
    renderClinicImaging();
    renderAssistPanel();
    status("已清空");
  });
}

/* 面板渲染：资料逐项选择（默认全选）/参考病例逐个确认/脱敏发送预览。 */
/* —— 阶段5：AI 辅助判断渲染（诊断辅助/临床建议/审核与审计） ——
   数据源：Qt 侧 _show_result 把服务端待审核草稿经 window.__pushAssist 推入。
   只存会话内存（不落 localStorage）；全部文本用 textContent 渲染，服务端内容不进 HTML。
   所有条目状态默认 pending（review_status 由服务端保证，前端不做任何"确认"改写）。 */
var ASSIST_SNAPSHOT = null;   // 最近一次服务端草稿（含 workflow_state/review_status）
var ASSIST_CONF_LABELS = { low: "低", medium: "中", high: "高", unknown: "未知" };
var ASSIST_STATUS_LABELS = { present: "存在", absent: "无", unknown: "未知（未询问 ≠ 没有）", not_asked: "未询问" };
var ASSIST_URGENCY_LABELS = { routine: "常规", soon: "尽快", urgent: "紧急", emergency: "需医生即刻评估" };
var ASSIST_BASIS_LABELS = { patient_report: "患者自述", doctor_observation: "医生观察",
  image_description: "影像描述", test_result: "检验结果", inference: "推断（需核实）" };
var ASSIST_REVIEW_LABELS = { pending: "待审核（医生未处理）", edited: "已修订（待医生再确认）",
  approved: "已确认——待签署草稿已生成（最终签署须在授权目标系统完成）", rejected: "已拒绝（不生成报告）" };
var ASSIST_REVIEW_DECISION_LABELS = { confirmed: "接受", modified: "修改", rejected: "拒绝",
  regenerate: "重新生成" };

function assistCiteIds(ids) {
  return (ids || []).filter(function (x) { return x; }).map(function (x) { return "[" + x + "]"; }).join(" ");
}

/* 诊断辅助区（§3.2）：鉴别方向 + 辅助排序（非确诊概率）+ 证据 + 缺失 + 风险 */
function renderAssistDx(draft, host) {
  host.textContent = "";
  var red = draft.red_flag_escalation || {};
  if (red.triggered && (red.reasons || []).length) {
    var rf = document.createElement("div"); rf.className = "rf-banner";
    rf.textContent = "🚩 风险提示（命中红旗，须医生人工审核" + (red.is_blocking ? "，普通分析已被阻断" : "") +
      "）：" + red.reasons.join("；") + "。处置建议：" + (red.recommended_action || "由主诊医生复核");
    host.appendChild(rf);
  }
  var conds = draft.suspected_conditions || [];
  if (!conds.length) {
    var e0 = document.createElement("div"); e0.className = "empty";
    e0.textContent = "知识库未覆盖本病例主题——不给鉴别诊断（不足以判断，不编造）";
    host.appendChild(e0);
  }
  conds.forEach(function (c, idx) {
    var card = document.createElement("div"); card.className = "dx-card";
    var head = document.createElement("div"); head.className = "dx-head";
    var nm = document.createElement("span"); nm.className = "dx-name";
    nm.textContent = (c.rank || idx + 1) + "、" + (c.name || "待排") + "（待排）";   // 措辞约束：鉴别方向一律「待排」，不是结论
    var conf = document.createElement("span");
    conf.className = "dx-conf" + (c.confidence === "unknown" ? " unknown" : "");
    conf.textContent = "辅助排序：" + (ASSIST_CONF_LABELS[c.confidence] || "未知") + "——非确诊概率";
    head.appendChild(nm); head.appendChild(conf); card.appendChild(head);
    (c.supporting_evidence || []).forEach(function (ev) {
      var row = document.createElement("div"); row.className = "dx-ev";
      row.textContent = "• " + (ev.statement || "");
      if ((ev.citation_ids || []).length) {
        var cite = document.createElement("span"); cite.className = "cite-id";
        cite.textContent = assistCiteIds(ev.citation_ids);
        row.appendChild(cite);
      }
      card.appendChild(row);
    });
    if (!(c.supporting_evidence || []).length) {
      var ne = document.createElement("div"); ne.className = "dx-missing";
      ne.textContent = "• 支持证据：无（不足以判断）";
      card.appendChild(ne);
    }
    (c.missing_information || []).forEach(function (mi) {
      var row = document.createElement("div"); row.className = "dx-missing";
      row.textContent = "⚠ 缺失资料：" + mi;
      card.appendChild(row);
    });
    (c.red_flags || []).forEach(function (fl) {
      var row = document.createElement("div"); row.className = "dx-sub";
      row.textContent = "🚩 " + fl;
      card.appendChild(row);
    });
    host.appendChild(card);
  });
  var unc = draft.uncertainties || [];
  if (unc.length) {
    var ub = document.createElement("div"); ub.className = "dx-missing"; ub.style.marginTop = "6px";
    ub.textContent = "不确定性：" + unc.join("；");
    host.appendChild(ub);
  }
}

/* 临床建议区（§3.3）：症状评估/检查与治疗方向/用药安全提醒——禁止自动处方剂量 */
function renderAssistAdvice(draft, host) {
  host.textContent = "";
  var items = [];
  (draft.symptom_assessment || []).forEach(function (a) {
    items.push({ main: "症状评估：" + (a.symptom || "") + "｜状态：" + (ASSIST_STATUS_LABELS[a.status] || a.status),
                 meta: "依据：" + (ASSIST_BASIS_LABELS[a.basis] || a.basis || "—") + " " + assistCiteIds(a.citation_ids) });
  });
  (draft.recommended_next_steps || []).forEach(function (n) {
    items.push({ main: "下一步：" + (n.action || ""),
                 meta: "紧迫度：" + (ASSIST_URGENCY_LABELS[n.urgency] || n.urgency || "常规") + "（由医生决定执行）" +
                       " " + assistCiteIds(n.citation_ids) });
  });
  (draft.treatment_reference || []).forEach(function (t) {
    var extra = [t.indication_or_goal ? "目标：" + t.indication_or_goal : "",
      t.basis ? "依据：" + t.basis : "", t.when_to_refer ? "转诊：" + t.when_to_refer : ""].filter(Boolean).join("｜");
    items.push({ main: "治疗方向：" + (t.approach || "待核实"),
                 meta: (extra || "待医生核对") + " " + assistCiteIds(t.citation_ids),
                 flag: "非处方，仅供医生参考与核定" });
  });
  (draft.medication_recommendations || []).forEach(function (m) {
    var checks = (m.contraindications_or_checks || []).join("、") || "过敏、禁忌、相互作用、肝肾功能";
    items.push({ main: "用药参考：" + (m.generic_name || "药物名称待核实") + "｜用途：" + (m.purpose || "待核实"),
                 meta: "参考使用时长：" + (m.reference_duration || "待核实") + "｜核对：" + checks +
                       " " + assistCiteIds(m.citation_ids),
                 flag: "不得直接执行；须医生确认后决定，禁止自动处方/剂量/频次/起停医嘱" });
  });
  (draft.medication_checks || []).forEach(function (m) {
    items.push({ main: "用药核对：" + (m.generic_name || "") + (m.purpose ? "｜" + m.purpose : ""),
                 meta: "需医生核实：" + ((m.items_to_verify || []).join("、") || "indication/allergy/contraindication/interaction") +
                       "｜" + (m.requires_doctor_confirmation ? "需医生确认后执行" : ""),
                 flag: "禁止自动处方/剂量/疗程 " + assistCiteIds(m.citation_ids) });
  });
  var sev = draft.severity || {};
  if (sev.level) {
    items.push({ main: "严重度提示：" + sev.level + "（仅供医生参考，不自动分流）",
                 meta: ((sev.basis || []).join("；")) + " " + assistCiteIds(sev.citation_ids) });
  }
  if (!items.length) {
    var e0 = document.createElement("div"); e0.className = "empty";
    e0.textContent = "尚无 AI 分析结果";
    host.appendChild(e0);
    return;
  }
  items.forEach(function (it) {
    var row = document.createElement("div"); row.className = "adv-item";
    var m1 = document.createElement("div"); m1.textContent = it.main; row.appendChild(m1);
    if (it.meta) {
      var m2 = document.createElement("div"); m2.className = "adv-meta"; m2.textContent = it.meta; row.appendChild(m2);
    }
    if (it.flag) {
      var m3 = document.createElement("div"); m3.className = "adv-flag"; m3.textContent = it.flag; row.appendChild(m3);
    }
    host.appendChild(row);
  });
}

/* 审核与审计区（§3.4 骨架）：使用资料/生成信息/AI建议状态/医生审核状态；完整审核历史在阶段6 */
function renderAssistAudit(draft, host) {
  host.textContent = "";
  var cs = draft.case_summary || {};
  var red = draft.red_flag_escalation || {};
  var lastRec = (typeof CHAT_HISTORY !== "undefined" && CHAT_HISTORY.length) ? CHAT_HISTORY[CHAT_HISTORY.length - 1] : null;
  var usedMaterials = (draft.clinic_payload && clinicalSummary(draft.clinic_payload)) ||
    (lastRec && lastRec.clinicSummary) || "（本次未携带病历单资料；3D 选点/附件见发送预览区）";
  var rows = [
    ["使用资料", usedMaterials],
    ["脱敏处理", "发送载荷 deidentified=true（姓名/身份证号/电话/地址已替换）"],
    ["部位定位", cs.selected_body_region || "（未携带）"],
    ["生成时间", draft.generated_at ? String(draft.generated_at).replace("T", " ").slice(0, 19) + " UTC" : "—"],
    ["生成方式", draft.generated_by || "—"],
    ["模型版本", draft.model_version || "—"],
    ["知识快照", draft.knowledge_snapshot || "—"],
    ["安全策略", draft.safety_policy_version || "—"],
    ["红旗状态", red.triggered ? ("已触发（" + (red.reasons || []).length + " 条）") : "未触发"],
  ];
  rows.forEach(function (r) {
    var row = document.createElement("div"); row.className = "audit-row";
    var k = document.createElement("span"); k.className = "k"; k.textContent = r[0];
    var v = document.createElement("span"); v.textContent = r[1];
    row.appendChild(k); row.appendChild(v); host.appendChild(row);
  });
  var stRow = document.createElement("div"); stRow.className = "audit-row";
  var k1 = document.createElement("span"); k1.className = "k"; k1.textContent = "AI 建议状态";
  var v1 = document.createElement("span");
  var pill = document.createElement("span"); pill.className = "pending-pill";
  pill.textContent = (draft.review_status === "pending" ? "pending" : String(draft.review_status)) +
    (draft.review_status === "pending" ? "（待医生整份审核）" : "");
  v1.appendChild(pill); stRow.appendChild(k1); stRow.appendChild(v1); host.appendChild(stRow);
  var stRow2 = document.createElement("div"); stRow2.className = "audit-row";
  var k2 = document.createElement("span"); k2.className = "k"; k2.textContent = "医生审核状态";
  var v2 = document.createElement("span");
  v2.textContent = ASSIST_REVIEW_LABELS[draft.review_status] || String(draft.review_status || "—");
  stRow2.appendChild(k2); stRow2.appendChild(v2); host.appendChild(stRow2);
  // 阶段6：完整审核历史（接受/修改/拒绝/重新生成，逐条时间+决策+意见摘要）
  var hist = draft.review_history || [];
  if (hist.length) {
    var hRow = document.createElement("div"); hRow.className = "audit-row";
    var hk = document.createElement("span"); hk.className = "k"; hk.textContent = "审核历史";
    var hv = document.createElement("span");
    hv.textContent = hist.length + " 条；最近：" +
      (ASSIST_REVIEW_DECISION_LABELS[hist[hist.length - 1].decision] || hist[hist.length - 1].decision) +
      ((hist[hist.length - 1].feedback || "") ? "（" + hist[hist.length - 1].feedback.slice(0, 24) + "…）" : "");
    hRow.appendChild(hk); hRow.appendChild(hv); host.appendChild(hRow);
    hist.forEach(function (h, i) {
      var row = document.createElement("div"); row.className = "audit-row";
      var k = document.createElement("span"); k.className = "k";
      k.textContent = "  " + (i + 1) + ".";
      var v = document.createElement("span"); v.className = "adv-meta";
      v.textContent = String(h.at || "").replace("T", " ").slice(0, 19) + " · " +
        (ASSIST_REVIEW_DECISION_LABELS[h.decision] || h.decision) +
        ((h.feedback || "") ? "：" + h.feedback.slice(0, 40) : "");
      row.appendChild(k); row.appendChild(v); host.appendChild(row);
    });
  }
}

function renderAssistSnapshot() {
  if (!ASSIST_SNAPSHOT) return;
  var dx = document.getElementById("assistDx");
  var adv = document.getElementById("assistAdvice");
  var au = document.getElementById("assistAudit");
  if (dx) renderAssistDx(ASSIST_SNAPSHOT, dx);
  if (adv) renderAssistAdvice(ASSIST_SNAPSHOT, adv);
  if (au) renderAssistAudit(ASSIST_SNAPSHOT, au);
}

/* Qt 桥接入口：main.py _push_assist_to_web → JSON 字符串（ensure_ascii 全转义） */
window.__pushAssist = function (jsonStr) {
  try {
    var d = JSON.parse(jsonStr);
    if (!d || typeof d !== "object") return "rejected";
    ASSIST_SNAPSHOT = d;
    renderAssistSnapshot();
    appendConvResultCard(d);   // v58：对话区结果卡（含真实性元信息守卫，绝不伪装 AI 分析）
    return "ok";
  } catch (e) { return "parse_error"; }
};

function renderAssistPanel() {
  var selEl = document.getElementById("assistSel");
  var refEl = document.getElementById("assistRefs");
  var prevEl = document.getElementById("assistPreview");
  if (!selEl) return;
  var c = collectClinical();
  selEl.innerHTML = "";
  function checkRow(parent, checked, labelText, meta, onChange) {
    var lab = document.createElement("label");
    var cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = !!checked;
    cb.addEventListener("change", function () { onChange(cb.checked); renderAssistPanel(); });
    var box = document.createElement("span");
    box.textContent = labelText;
    var metaEl = document.createElement("span"); metaEl.className = "sel-meta"; metaEl.textContent = meta || "";
    lab.appendChild(cb); lab.appendChild(box); lab.appendChild(metaEl);
    parent.appendChild(lab);
    return lab;
  }
  var hasAny = false;
  Object.keys(CLINIC_SELECT_LABELS).forEach(function (k) {
    var v = c[k];
    var meta = k === "labs" ? (v.length ? v.length + " 项" : "（空）")
      : k === "patient" ? (v ? "已填" : "（空）")
      : (v ? v.length + " 字" : "（空）");
    if (v) hasAny = true;
    checkRow(selEl, CLINIC_SELECT[k], CLINIC_SELECT_LABELS[k], meta, function (on) { CLINIC_SELECT[k] = on; });
  });
  var readyImaging = c.imaging.filter(function (m) { return m.status === "ready"; });
  readyImaging.forEach(function (m) {
    if (CLINIC_SELECT_IMAGING[m.name] === undefined) CLINIC_SELECT_IMAGING[m.name] = true;
    hasAny = true;
    checkRow(selEl, CLINIC_SELECT_IMAGING[m.name], "影像：" + m.name,
      m.width + "×" + m.height + (m.quality_note ? " · " + m.quality_note : ""),
      function (on) { CLINIC_SELECT_IMAGING[m.name] = on; });
  });
  if (!hasAny) {
    var none = document.createElement("div"); none.className = "empty";
    none.textContent = "尚无资料（在「📋 病历单」中录入）";
    selEl.appendChild(none);
  }
  // 参考病例：默认不确认；逐个勾选后才参与分析
  refEl.innerHTML = "";
  var refs = candidateRefCases(chatSeq + 1);
  if (!refs.length) {
    var nr = document.createElement("div"); nr.className = "empty";
    nr.textContent = "暂无候选病例（历史提交后出现）";
    refEl.appendChild(nr);
  } else {
    refs.forEach(function (r) {
      var meta = " #" + r.n + " · " + r.time + (r.regions ? " · " + r.regions + " 部位" : "") + (r.clinic ? " · " + r.clinic : "");
      checkRow(refEl, REF_CASE_CONFIRMED[r.n], "确认参考：" + (r.text || "（无文字，仅 3D 选点）"), meta,
        function (on) { REF_CASE_CONFIRMED[r.n] = on; });
    });
  }
  // 脱敏发送预览
  var pv = clinicalPayloadSelected(chatSeq + 1);
  prevEl.textContent = pv ? JSON.stringify(pv, null, 2)
    : "（当前无资料可发送——录入资料或在历史中确认参考病例）";
  // 阶段5：服务端草稿已推送时同步刷新诊断辅助/临床建议/审核审计三区
  renderAssistSnapshot();
}

function initAssistUI() {
  var entry = document.getElementById("assistEntry");
  var drawer = document.getElementById("assistDrawer");
  if (!entry || !drawer) return;
  entry.addEventListener("click", function (ev) {
    ev.stopPropagation();
    var opening = !drawer.classList.contains("open");
    drawer.classList.toggle("open");
    document.getElementById("plusMenu").classList.remove("open");
    var clinicDrawer = document.getElementById("clinicDrawer");
    if (opening && clinicDrawer) clinicDrawer.classList.remove("open");   // 同侧互斥
    if (opening) renderAssistPanel();
  });
  document.getElementById("assistClose").addEventListener("click", function () { drawer.classList.remove("open"); });
}

/* 发送：合并为一条结构化请求 → 历史卡 + bridge/console 上报（本地回执，不接诊断 API） */
/* v58：发送前置 LLM 探测——未连接时阻断发送并给出「检查连接/打开模型设置/切换离线演示」
   三入口；绝不在 LLM 不可用时把任何内容送出去伪装成 AI 分析。 */
function probeLlm(cb) {
  // v67：已配置但探测失败（网关瞬时抖动/本机服务端刚拉起）→ 自动重试一次；未配置不重试。
  var done = false, tries = 0;
  var finish = function (r) { if (!done) { done = true; cb(r); } };
  var attempt = function () {
    if (bridge && bridge.llmProbe) {
      try {
        bridge.llmProbe(function (s) {
          var r = null;
          try { r = JSON.parse(s); } catch (e) {}
          r = r || { available: false, mode: "server", error: t("探测返回无效", "Invalid probe response") };
          if (!r.available && r.configured && tries === 0) {
            tries += 1;
            setTimeout(attempt, 600);
            return;
          }
          finish(r);
        });
        return;
      } catch (e) { /* bridge 异常 → 按未连接处理 */ }
      finish({ available: false, mode: "server", error: t("连接探测异常", "Probe error") });
      return;
    }
    finish({ available: true, mode: "offline", note: "offline_demo" });   // 无桌面桥：演示兜底（结果卡如实标注）
  };
  attempt();
}

function sendChat(overrideText) {
  if (window.__chatBusy) {   // v74：生成中点发送/回车 = 中止本轮生成
    if (bridge && bridge.stopChat) bridge.stopChat();
    return;
  }
  var input = document.getElementById("chatInput");
  var text = (typeof overrideText === "string" && overrideText) ? overrideText : (input ? input.value.trim() : "");
  var clinicRaw = clinicalPayload();
  if (!text && !selectedRegions.length && !CHAT_FILES.length && !clinicRaw) return;   // 无任何内容不可发送
  setStatus(t("正在检查 LLM 连接…", "Checking LLM connection…"), "busy");
  window.__pushThinking("check");   // v64：探测期对话流立即给占位反馈（成功→刷新为「正在思考」；失败→错误卡）
  probeLlm(function (probe) {
    if (!probe.available) {
      appendLlmUnavailableCard(probe);
      setStatus(t("当前未连接 LLM，暂不能生成真实 AI 医疗建议",
        "LLM not connected; real AI medical advice unavailable"), "warn");
      return;
    }
    sendChatNow(overrideText);
  });
}

function sendChatNow(overrideText) {
  var input = document.getElementById("chatInput");
  var text = (typeof overrideText === "string" && overrideText) ? overrideText : (input ? input.value.trim() : "");
  chatSeq += 1;   // 先定序号：参考病例确认按消息序号引用，本条排除自身
  var clinicRaw = clinicalPayload();          // 是否有实际资料内容（不受逐项选择影响）
  var clinic = clinicalPayloadSelected(chatSeq);   // 按医生选择过滤后的载荷
  if (!text && !selectedRegions.length && !CHAT_FILES.length && !clinicRaw) { chatSeq -= 1; return; }   // 无任何内容不可发送
  var payload = buildStructuredPayload(text, clinic);
  var rec = {
    n: chatSeq,
    time: new Date().toLocaleTimeString("zh-CN", { hour12: false }),
    text: text,
    regions: payload.regions.slice(),
    files: CHAT_FILES.map(function (f) { return { name: f.name, size: f.size }; }),
    clinicSummary: clinicalSummary(clinic),
    payload: payload
  };
  CHAT_HISTORY.push(rec);
  renderHistory();
  if (bridge && bridge.onChatSubmitted) bridge.onChatSubmitted(JSON.stringify(payload));
  try { console.log("[derm] structured request #" + chatSeq, JSON.stringify(payload)); } catch (e) {}
  // 提交后：文字与附件清空；已选位置保留（下一条消息可继续携带或 × 删除）
  if (input) input.value = "";
  CHAT_FILES = [];
  renderChips();
  autosizeInput();
  if (document.getElementById("assistDrawer") && document.getElementById("assistDrawer").classList.contains("open")) {
    renderAssistPanel();   // 发送后刷新预览与参考病例候选（新历史出现）
  }
  // v59：发送后自动回到对话主区（模型模式下也能直接发送；3D 选点/视角全保留，可随时切回）
  // v69：走导航层，保持 NAV.workspace 与实际布局一致（否则 3D→对话的手动切换不入导航状态）
  navSetWorkspace("chat");
  appendConvUserCard(rec);
  recordRecent("session", (text || "（3D 选点/附件，无文字）") + (rec.regions.length ? " · " + rec.regions.length + " 个部位" : ""));
  window.__setChatBusy(true);   // v74：发送按钮 → 停止按钮（输入框上方不再显示「正在分析」文字，占位/状态栏已足够）
}

/* v74：中止/生成状态——true 时发送按钮变「停止」样式（深色圆 + 白色方点），可点击中止 */
window.__setChatBusy = function (busy) {
  var btn = document.getElementById("sendBtn");
  if (!btn) return;
  window.__chatBusy = !!busy;
  btn.classList.toggle("stopping", !!busy);
  btn.innerHTML = busy ? '<span class="stop-sq"></span>' : "↑";
  if (typeof updateSendBtn === "function") updateSendBtn();
};

/* 历史消息卡：时间 + 文字 + 位置 + 附件 + 完整结构化载荷（可展开核对） */
function renderHistory() {
  var el = document.getElementById("histList");
  if (!el) return;
  el.innerHTML = "";
  if (!CHAT_HISTORY.length) {
    var d = document.createElement("div");
    d.className = "empty";
    d.textContent = "提交后，这里显示本次会话的结构化消息记录";
    el.appendChild(d);
    return;
  }
  CHAT_HISTORY.forEach(function (m) {
    var card = document.createElement("div");
    card.className = "msg-card";
    var t = document.createElement("div"); t.className = "msg-time";
    t.textContent = "#" + m.n + " · " + m.time;
    card.appendChild(t);
    if (m.text) {
      var tx = document.createElement("div"); tx.className = "msg-text"; tx.textContent = m.text;
      card.appendChild(tx);
    }
    if (m.regions.length) {
      var rg = document.createElement("div"); rg.className = "msg-region";
      rg.textContent = "📍 " + m.regions.join("；");
      card.appendChild(rg);
    }
    m.files.forEach(function (f) {
      var fl = document.createElement("div"); fl.className = "msg-file";
      fl.textContent = "📎 " + f.name + "（" + Math.round(f.size / 1024) + " KB）";
      card.appendChild(fl);
    });
    if (m.clinicSummary) {
      var cl = document.createElement("div"); cl.className = "msg-region";
      cl.textContent = "🩺 资料：" + m.clinicSummary;
      card.appendChild(cl);
    }
    var det = document.createElement("details");
    var sm = document.createElement("summary"); sm.textContent = "结构化载荷";
    var pre = document.createElement("pre");
    pre.textContent = JSON.stringify(m.payload, null, 2);
    det.appendChild(sm); det.appendChild(pre);
    card.appendChild(det);
    el.appendChild(card);
  });
  el.scrollTop = el.scrollHeight;
}

/* 输入框自动增高（随内容，封顶 160px） */
function autosizeInput() {
  var input = document.getElementById("chatInput");
  if (!input) return;
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
}

function toggleDrawer(id, force) {
  var el = document.getElementById(id);
  if (!el) return;
  var open = force !== undefined ? force : !el.classList.contains("open");
  ["selDrawer", "histDrawer"].forEach(function (d) {
    var e = document.getElementById(d);
    if (e) e.classList.toggle("open", d === id ? open : false);
  });
  var btns = { selDrawer: ["openSelDrawer", "menuSelDrawer"], histDrawer: ["openHistDrawer", "menuHistDrawer"] };
  (btns[id] || []).forEach(function (bid) {
    var b = document.getElementById(bid);
    if (b) b.classList.toggle("toggled", open);
  });
}

/* 图片附件：白名单 + ≤10MB（与 Python 端同一安全口径），FileReader 转 dataUrl */
function addChatFile(f) {
  if (!f) return;
  var okExt = [".png", ".jpg", ".jpeg", ".webp"];
  var ext = (f.name ? f.name.slice(f.name.lastIndexOf(".")) : "").toLowerCase();
  if (okExt.indexOf(ext) === -1) { alert("仅支持 png/jpg/jpeg/webp：" + f.name); return; }
  if (f.size > 10 * 1024 * 1024) { alert("图片超过 10MB：" + f.name); return; }
  var reader = new FileReader();
  reader.onload = function () {
    CHAT_FILES.push({ name: f.name, size: f.size, dataUrl: reader.result });
    renderChips();
  };
  reader.readAsDataURL(f);
}

function initComposer() {
  var input = document.getElementById("chatInput");
  var send = document.getElementById("sendBtn");
  var plus = document.getElementById("plusBtn");
  var menu = document.getElementById("plusMenu");
  if (!input || !send) return;
  input.addEventListener("input", function () { autosizeInput(); updateSendBtn(); });
  input.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); sendChat(); }
  });
  send.addEventListener("click", sendChat);
  plus.addEventListener("click", function (ev) {
    ev.stopPropagation();
    menu.classList.toggle("open");
  });
  document.addEventListener("click", function (ev) {
    if (menu.classList.contains("open") && !menu.contains(ev.target) && ev.target !== plus) {
      menu.classList.remove("open");
    }
  });
  document.getElementById("imgFile").addEventListener("change", function () {
    Array.prototype.slice.call(this.files || []).forEach(function (f) { addChatFile(f); });
    this.value = "";
  });
  document.getElementById("addImage").addEventListener("click", function () {
    document.getElementById("imgFile").click();
    menu.classList.remove("open");
  });
  var composer = document.getElementById("composer");
  ["dragenter", "dragover"].forEach(function (en) {
    composer.addEventListener(en, function (ev) { ev.preventDefault(); composer.classList.add("drag"); });
  });
  ["dragleave", "drop"].forEach(function (en) {
    composer.addEventListener(en, function (ev) { ev.preventDefault(); composer.classList.remove("drag"); });
  });
  composer.addEventListener("drop", function (ev) {
    Array.prototype.slice.call((ev.dataTransfer && ev.dataTransfer.files) || [])
      .forEach(function (f) { addChatFile(f); });
  });
  [["openSelDrawer", "selDrawer"], ["menuSelDrawer", "selDrawer"]].forEach(function (pair) {
    var btn = document.getElementById(pair[0]);
    if (btn) btn.addEventListener("click", function () { toggleDrawer(pair[1]); });
  });
  [["selDrawerClose", "selDrawer"], ["histDrawerClose", "histDrawer"]].forEach(function (pair) {
    var btn = document.getElementById(pair[0]);
    if (btn) btn.addEventListener("click", function () { toggleDrawer(pair[1], false); });
  });
  updateSendBtn();
}

/* ================= 拾取 / 视角 / 截图 / 桥 ================= */
/* v18：递归 Raycaster（intersectObject(modelRoot, true)）——只取距相机最近的、
 * 真实可见三角面交点。标记/辅助物/地面/外壳（userData.noPick）不参与命中；
 * 重叠网格以视觉最前方（距离最小）交点为准。每次点击 = 独立记录 + 独立标记。
 * 输出格式：「解剖区域 + 表面方位 + 相对位置 + 局部坐标」。 */

function pickablesRoot() { return modelRoot; }   // v18：递归命中真实渲染网格，无代理/命中盒

/* ================= v74：自选区域画笔涂抹（brush，原圈选改涂）=================
 * 涂抹模式下禁用视角旋转：按住拖拽即画笔涂抹（每个采样点射线取体表命中），
 * 松开后自动识别位置——质心=主区域；纵向=最低涂抹点→最高涂抹点，
 * 横向=最左涂抹点→最右涂抹点（解剖名按对应三维点解析），生成一条
 * 「涂抹区域」选中记录（与单点选点同一数据流：标记 / 已选列表 / 发送载荷）。
 * 涂错了：直接点标记删除（v74-fix），或重新涂抹。 */
var LASSO = { active: false, drawing: false, pts: [], world: [] };
var __lassoSvg = null, __lassoLine = null;

function setLasso(on) {
  LASSO.active = !!on;
  if (controls) controls.enabled = !LASSO.active;   // 圈选与视角旋转互斥
  var b = document.getElementById("lassoBtn");
  if (b) { b.style.background = on ? "#1a1a1a" : ""; b.style.color = on ? "#fff" : ""; }
  if (!on) lassoClearSvg();
}

function lassoSvg() {
  if (__lassoSvg) return __lassoSvg;
  var wrap = renderer.domElement.parentNode || document.getElementById("stage-wrap");   // 与画布同原点，坐标零偏移
  __lassoSvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  __lassoSvg.setAttribute("style", "position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:6;display:none;");
  __lassoLine = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  __lassoLine.setAttribute("fill", "none");
  __lassoLine.setAttribute("stroke", "rgba(37,99,235,.55)");
  __lassoLine.setAttribute("stroke-width", "12");
  __lassoLine.setAttribute("stroke-linecap", "round");
  __lassoLine.setAttribute("stroke-linejoin", "round");
  __lassoSvg.appendChild(__lassoLine);
  wrap.appendChild(__lassoSvg);
  return __lassoSvg;
}

function lassoClearSvg() {
  LASSO.drawing = false; LASSO.pts = [];
  if (__lassoSvg) __lassoSvg.style.display = "none";
}

function lassoDrawPts() {   // 记录用 client 坐标，绘制转画布局部坐标（v74-fix：修覆盖层偏移）
  var r = renderer.domElement.getBoundingClientRect();
  __lassoLine.setAttribute("points", LASSO.pts.map(function (p) {
    return (p[0] - r.left) + "," + (p[1] - r.top);
  }).join(" "));
}

function lassoDown(e) {
  if (!LASSO.active || e.button !== 0) return;
  LASSO.drawing = true; LASSO.pts = []; LASSO.world = [];
  lassoSvg().style.display = "block";
  lassoDrawPts();
}

function lassoMove(e) {
  if (!LASSO.drawing) return;
  // 画笔涂抹：每个采样点射线取模型表面命中；只记录真实落在体表上的点
  var rect = renderer.domElement.getBoundingClientRect();
  raycaster.setFromCamera(new THREE.Vector2(
    (e.clientX - rect.left) / rect.width * 2 - 1,
    -((e.clientY - rect.top) / rect.height) * 2 + 1), camera);
  var hit = modelRoot ? raycaster.intersectObject(modelRoot, true).filter(function (h) {
    return h.object && h.object.isMesh && !h.object.userData.noPick;
  })[0] : null;
  if (!hit) return;   // 划出体表的段不入记录（笔画视觉仍连续）
  LASSO.pts.push([e.clientX, e.clientY]);
  LASSO.world.push(hit.point.clone());
  lassoDrawPts();
}

function lassoUp() {
  if (!LASSO.drawing) return;
  var pts = LASSO.pts.slice(), world = (LASSO.world || []).slice();
  lassoClearSvg();
  if (world.length < 5) { setStatus(t("请在模型表面涂抹", "Brush on the model surface")); return; }
  // 位置识别（与单点选点完全同口径）：质心=主区域；纵向=最低涂抹点→最高涂抹点，横向=最左→最右，
  // 各极值点在松开时重新射线命中并走 recognizePoint 全识别链（人工校准→逐面语义表→锚点→型带）
  var rect = renderer.domElement.getBoundingClientRect();
  function pickAt(p) {
    raycaster.setFromCamera(new THREE.Vector2(
      (p[0] - rect.left) / rect.width * 2 - 1,
      -((p[1] - rect.top) / rect.height) * 2 + 1), camera);
    return raycaster.intersectObject(modelRoot, true).filter(function (h) {
      return h.object && h.object.isMesh && !h.object.userData.noPick;
    })[0] || null;
  }
  function rec(p) {
    var h = pickAt(p);
    if (!h) return null;
    var hd = recordHit(h);
    return { r: recognizePoint(hd, h.point, h.object), h: hd };
  }
  function ext(idx, cmp) {
    var best = 0;
    for (var i = 1; i < pts.length; i++) if (cmp(pts[i][idx], pts[best][idx])) best = i;
    return pts[best];
  }
  var rTop = rec(ext(1, function (a, b) { return a < b; }));    // 屏幕 y 最小 = 最高
  var rBot = rec(ext(1, function (a, b) { return a > b; }));    // 屏幕 y 最大 = 最低
  var rL = rec(ext(0, function (a, b) { return a < b; }));
  var rR = rec(ext(0, function (a, b) { return a > b; }));
  var rMid = rec(pts[Math.floor(pts.length / 2)]);
  if (!rMid) { setStatus(t("请在模型表面涂抹", "Brush on the model surface")); return; }
  function lbl(r) { return r ? r.r.region.label : "未识别位置"; }
  var base = rMid.r.region.label;
  var c = new THREE.Vector3(0, 0, 0);
  world.forEach(function (w) { c.add(w); });
  c.multiplyScalar(1 / world.length);
  addRegionSelection({
    id: "brush:" + Date.now(),
    label: base + "（涂抹）",
    pos: c,
    confidence: rMid.r.region.confidence,
    source: rMid.r.region.provenance,
    parts: rMid.r.parts,
    hit: rMid.h,
    desc: base + " · 涂抹区域：纵向自「" + lbl(rBot) + "」至「" + lbl(rTop) +
          "」，横向自「" + lbl(rL) + "」至「" + lbl(rR) + "」"
  });
  setStatus(t("已涂抹识别：" + base, "Brushed region: " + base));
}

/* v74：单点识别管线（onPick 原链抽离）：hitData + 交点 + 命中对象 → {region, parts}。
 * 单点选点与画笔涂抹共用，保证涂抹的位置识别与单点点击完全同口径。 */
function recognizePoint(hitData, p0, o0) {
    // 1) 解剖语义层：人工校准 → v19/v20 逐面语义表 → 锚点表（眼/五指兜底）→ 镜框回退
    var manual = manualRegionFor(hitData);
    var measuredCorrection = nearestMeasuredCorrection(hitData);
    var measuredFace = measuredFaceCorrection(hitData);
    var anat = anatomyFromPoint(p0);
    var isGlasses = o0.name && o0.name.indexOf("-1-966") !== -1;
    if (isGlasses && (!anat || anat.confidence < 0.9)) anat = glassesFallback(p0);
    var region, descParts;
    // v20：逐面语义表优先于锚点（表按 mesh+faceIndex 精确命中，含 v20 指节/关节/胸腹细分）。
    // 例外：① 人工校准（calib:）是用户权威标注，仍最优先；
    // ② finger_zone_* 是表内声明的「指级由锚点细分」低置信带（conf 0.6），让位给手指锚点。
    var fsem = faceSemFor(hitData);
    var anatIsCalib = anat && anat.id.indexOf("calib:") === 0;
    var fsemYieldsToFinger = fsem && fsem.id.indexOf("finger_zone_") === 0 && anat &&
                             anat.id.indexOf("_finger") !== -1;
    if (manual) {
      region = { id: "manual:" + manual.id, label: manual.label, confidence: 1.0,
                 source: "manual_region", provenance: "人工区域（" + manual.id + " v" + manual.version + "）" };
    } else if (measuredFace) {
      region = { id: "measured_face:" + hitData.mesh_name + ":" + hitData.face_index,
                 label: measuredFace.label, confidence: 1.0, source: "manual_measurement",
                 provenance: "用户实测面校正" };
    } else if (measuredCorrection) {
      region = { id: "measured:" + measuredCorrection.standard_id, label: measuredCorrection.label,
                 confidence: 1.0, source: "manual_measurement", provenance: "用户截图实测校正" };
    } else if (fsem && !anatIsCalib && !fsemYieldsToFinger) {
      region = { id: "fsem:" + fsem.id, label: fsem.label, confidence: fsem.confidence,
                 source: "fsem", fsemNote: fsem.note };
      // v21：肢体纵向带（大腿/小腿/上臂/前臂/踝-小腿交界/臀腿交界）补 relT —— 表的 yband 给出该
      // zone 的实测 y 范围（构建器归一化系：身高 5、y∈[-2.5,2.5]，与运行时世界系同口径，
      // 因为 registerHuman 归一化后世界系即构建器系）。命中点 y 在带内归一化（0=近端 1=远端），
      // describeHit 走 limbRelPos 输出「近端/中段/远端」。
      // 注意不能用 model_local_xyz —— 那是 modelRoot 原始系（GLB node 自带 0.01 缩放，y≈0.4 级别），
      // 与表系差一个归一化变换，直接用会恒 clamp 到 0（全部「近端」）。
      // 仅肢体/交界 zone 应用（cat=limb）——躯干 yband 的「上/下」语义与肢体近端/远端不同，不套用。
      var lab = FACE_SEM.labels[fsem.id];
      if (lab && lab.cat === "limb" && lab.yband && hitData.world_xyz) {
        var _y = hitData.world_xyz[1], _y0 = lab.yband[0], _y1 = lab.yband[1];
        if (_y1 > _y0) {
          // 肢体近端在上方（y 大，靠髋/肩）→ relT = (y1-y)/(y1-y0)，0=近端 1=远端
          region.relT = Math.min(1, Math.max(0, (_y1 - _y) / (_y1 - _y0)));
        }
      }
      // v23：躯干/臀等 body/torso 区同样注入纵向比例（yRel，0=下份 1=上份），使同区不同
      // 纵深处的点击输出「上份/中份/下份」——用户要求臀部不同位置必须产生不同描述。
      // 肢体不用此口径（relT 已给近端/远端），交界带（臀褶/胸腹交界）语义上是「带」也无须再分。
      if (lab && (lab.cat === "body" || lab.cat === "torso") && lab.yband && hitData.world_xyz &&
          fsem.id.indexOf("border") === -1 && fsem.id.indexOf("fold") === -1 &&
          fsem.id.indexOf("margin") === -1 && fsem.id.indexOf("xiphoid") === -1) {
        var _by = hitData.world_xyz[1], _b0 = lab.yband[0], _b1 = lab.yband[1];
        if (_b1 > _b0) {
          region.yRel = Math.min(1, Math.max(0, (_by - _b0) / (_b1 - _b0)));
          region.ybandRef = lab.yband;
        }
      }
    } else if (coveringMaterialFor(o0) === "hair") {
      // v28：发片兜底（女性模型发束垂在脸侧/胸前，型带会误报「脸/胸」；按材质名如实报头发）
      region = { id: "hair", label: "头发（发丝，非皮肤）", confidence: 0.9, source: "material" };
    } else if (anat) {
      region = { id: "anchor:" + anat.id, label: anat.label, confidence: anat.confidence, source: "anchor" };
    } else {
      // 2) 几何型带分区（含 relT/bb 供相对位置）；置信 <0.5 = 证据不足
      var zn = zoneFromPoint(p0);
      if (zn && zn.confidence >= 0.5) {
        region = { id: "zone:" + zn.id, label: zn.label, confidence: zn.confidence,
                   source: "zone", relT: zn.relT, bb: zn.bb };
      } else {
        // 3) 证据不足 → 「当前 GLB 缺少该部位的可靠语义」（仍保存坐标与方位，不伪造解剖名）
        var lackLabel = zn
          ? "当前 GLB 缺少该部位的可靠语义（疑似" + zn.label + "，置信 " + zn.confidence + "）"
          : "当前 GLB 缺少该部位的可靠语义（" + hitData.mesh_name + " 面#" + hitData.face_index + "，可在标注模式中命名）";
        region = { id: "unreliable:" + hitData.mesh_name + ":" + hitData.face_index,
                   label: lackLabel, confidence: zn ? zn.confidence : 0, source: "unreliable" };
      }
    }
    // v28：衣物覆盖注记——衣物下的皮肤仍是原解剖区域，只在标签上注明覆盖（不改变归属）
    if (region.source !== "material" && coveringMaterialFor(o0) === "clothing") {
      region.label += "（衣物覆盖）";
    }
    // 4) 完整描述：「解剖区域 + 表面方位 + 相对位置 + 局部坐标」
    descParts = describeHit(hitData, region);
    // v23：位置识别来源（provenance 主等级）随记录透传——展示与提交共用同一数据
    region.provenance = region.source === "manual_region" ? "人工区域"
      : region.source === "manual_measurement" ? "用户实测校正"
      : region.id.indexOf("calib:") === 0 ? "人工校准"
      : region.source === "fsem" ? "逐面语义表（真实网格三角面）"
      : region.source === "anchor" ? "解剖锚点（几何实测）"
      : region.source === "zone" ? "几何型带（近似推断）"
      : region.source === "material" ? "材质语义（命名材质兜底）"
      : "无法可靠识别";
    descParts.provenance = region.provenance;
    descParts.confidence = region.confidence;
  return { region: region, parts: descParts };
}

function onPick(ev) {
  if (LASSO.active || LASSO.drawing) return;   // v74：圈选模式不产生单点选点
  var rect = renderer.domElement.getBoundingClientRect();
  var pointer = new THREE.Vector2(
    ((ev.clientX - rect.left) / rect.width) * 2 - 1,
    -((ev.clientY - rect.top) / rect.height) * 2 + 1
  );
  raycaster.setFromCamera(pointer, camera);
  // v74-fix：点中已有标记 = 删除该标记（选错位置直接点掉即可）。拖拽旋转松开（位移>6px）不误删。
  var _pd = window.__pickDown || [0, 0];
  var _moved = Math.hypot(ev.clientX - _pd[0], ev.clientY - _pd[1]);
  var _mhits = raycaster.intersectObjects(markerPool.filter(function (m) { return m.visible; }), false);
  if (_moved <= 6 && _mhits.length && _mhits[0].object.userData.regionUid) {
    removeSelected(_mhits[0].object.userData.regionUid);
    return;
  }
  // 递归检测真实渲染网格（标记/辅助物 userData.noPick 不在 modelRoot 下或被排除）
  var hits = modelRoot ? raycaster.intersectObject(modelRoot, true).filter(function (h) {
    return h.object && h.object.isMesh && !h.object.userData.noPick;
  }) : [];
  if (hits.length) {
    var p0 = hits[0].point;                       // 距相机最近 = 视觉最前方可见交点
    var o0 = hits[0].object;
    var hitData = recordHit(hits[0]);             // 完整真实命中数据（v18 字段全量）
    if (REGION_DRAW.active) { regionDrawClick(hitData, ev); return; }
    // 0a) 标注模式：点击 → 输入标签 → 保存真实命中映射，不选区
    if (CALIB.mode) {
      var okLabeled = promptCalibration(hitData);
      if (okLabeled) {
        var lbEl = document.getElementById("regionLabel");
        if (lbEl) lbEl.textContent = "已标注：" + CALIB.byModel[modelKey()][hitData.mesh_name + ":" + hitData.face_index].label +
          "（" + hitData.mesh_name + " 面#" + hitData.face_index + "）";
      }
      exitLabelMode();
      return;
    }
    // 0b) 校准向导：当前步骤点确认（命中即视为「系统识别正确」）或纠正（点击别处为新锚点）
    if (WIZARD.active) {
      var step = WIZARD.steps[WIZARD.idx];
      var v = new THREE.Vector3(step.spot[0], step.spot[1], step.spot[2]);
      var confirmed = p0.distanceTo(v) < 0.06;   // 落点在预置点 6cm 内 = 确认系统识别
      wizardNext(p0, confirmed);
      var lbWiz = document.getElementById("regionLabel");
      if (lbWiz) lbWiz.textContent = confirmed
        ? "校准：「" + step.zh + "」确认 ✓（系统识别正确）"
        : "校准：「" + step.zh + "」已纠正为你点击的位置（对侧自动镜像）";
      return;
    }
    // v74：识别链抽为 recognizePoint（单点选点与画笔涂抹共用同一识别管线）
    var __rec = recognizePoint(hitData, p0, o0);
    var region = __rec.region, descParts = __rec.parts;
    addRegionSelection({ id: region.id, label: region.label, pos: p0,
      confidence: region.confidence, desc: descParts.main, parts: descParts, hit: hitData,
      source: region.provenance });
    if (DEBUG_MODE) {
      var lbDbg = document.getElementById("regionLabel");
      if (lbDbg) lbDbg.setAttribute("data-debug",
        "[调试] " + hitData.mesh_name + " 面#" + hitData.face_index + " 交点 " +
        hitData.world_xyz.map(function (v) { return v.toFixed(2); }).join(",") + " → " + region.label);
    }
    return;
  }
  // 未命中任何网格：向导中提示重试；其余静默
  if (WIZARD.active) {
    var hintEl = document.getElementById("hint");
    if (hintEl) hintEl.textContent = "校准向导：该点击未落在模型上，请重新点击「" + WIZARD.steps[WIZARD.idx].zh + "」附近";
  }
}

/* 调试模式开关：开启后顶部标签附命中 Mesh/faceIndex/交点，列表展开含命中等级 */
function toggleDebug() {
  DEBUG_MODE = !DEBUG_MODE;
  var btn = document.getElementById("dbgMode");
  if (btn) btn.style.background = DEBUG_MODE ? "#d9e9f8" : "";
  var lb = document.getElementById("regionLabel");
  if (lb && !DEBUG_MODE) lb.removeAttribute("data-debug");
  renderSelection();
  return DEBUG_MODE;
}

var VIEWS = {
  front: { pos: [0, 0.35, 9], target: [0, 0.1, 0] },
  back: { pos: [0, 0.35, -9], target: [0, 0.1, 0] },
  left: { pos: [9, 0.35, 0], target: [0, 0.1, 0] },
  right: { pos: [-9, 0.35, 0], target: [0, 0.1, 0] },
  bottom: { pos: [0, -6.5, 0.9], target: [0, -1.6, 0] },   // v19：底视（查看脚底/趾尖）
  reset: { pos: [0, 0.35, 9], target: [0, 0.1, 0] }
};

function setView(name) {
  currentViewName = VIEWS[name] ? name : "reset";
  var v = VIEWS[name] || VIEWS.reset;
  camera.position.set(v.pos[0], v.pos[1], v.pos[2]);
  controls.target.set(v.target[0], v.target[1], v.target[2]);
  controls.update();
  camera.updateMatrixWorld(true);   // 立即刷新矩阵（含 matrixWorldInverse）：切换视角后同帧内的拾取/投影即用新视角
}

function highlightRegion(regionId, label) {
  if (!regionId) return;
  addRegionSelection({ id: regionId, label: label || regionId });   // v18：独立记录（无命中数据的内部构造点）
}

function screenshot() {
  renderer.render(scene, camera);
  var data = renderer.domElement.toDataURL("image/png");
  if (bridge && bridge.onScreenshot) bridge.onScreenshot(data);
  return data;
}

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}

function initBridge() {
  if (!window.qt && window.__browserBridge) {   /* v75：浏览器端 REST 桥已就绪——与桌面回调同口径补刷新 */
    if (typeof renderRailHist === "function") renderRailHist();
    if (typeof fetchKbServerBase === "function") fetchKbServerBase(function () {
      if (typeof refreshKbBooks === "function") refreshKbBooks();
    });
    return;
  }
  if (!window.qt || !window.qt.webChannelTransport) return;
  var s = document.createElement("script");
  s.src = "qrc:///qtwebchannel/qwebchannel.js";
  s.onload = function () {
    /* eslint-disable no-undef */
    new QWebChannel(qt.webChannelTransport, function (channel) {
      bridge = channel.objects.bridge;
      if (typeof refreshLlmStatus === "function") refreshLlmStatus();   // v46：启动即填充 composer 旁 LLM 模型选择
      renderRailHist();   // v61：bridge 就绪后填充侧栏「历史病例」分组
      if (typeof fetchKbServerBase === "function") fetchKbServerBase(function () {
        /* bridge 异步就绪后补做一次刷新，覆盖初始化时的“正在连接”占位状态。 */
        if (typeof refreshKbBooks === "function") refreshKbBooks();
      });
    });
  };
  document.head.appendChild(s);
}

function onResize() {
  var w = stageEl.clientWidth, h = stageEl.clientHeight;
  if (w < 2 || h < 2) return;   /* 布局过渡态防 0 尺寸产生非法相机纵横比 */
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h);
}

/* ================= 启动 ================= */

/* v58：3D 顶部工具浮层——视角下拉菜单（前/后/左/右/重置）+ 缩放 + 重置。
   左侧旧视角按钮已删除；切换视角/缩放不触碰已选部位与病例状态（只动相机）。 */
document.querySelectorAll("#viewMenu button[data-view]").forEach(function (btn) {
  btn.addEventListener("click", function () {
    setView(btn.getAttribute("data-view"));
    var menu = document.getElementById("viewMenu");
    if (menu) menu.classList.remove("open");
  });
});
(function () {
  var viewBtn = document.getElementById("viewBtn");
  var menu = document.getElementById("viewMenu");
  if (viewBtn && menu) {
    viewBtn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      menu.classList.toggle("open");
    });
    document.addEventListener("click", function (ev) {
      if (!menu.contains(ev.target) && ev.target !== viewBtn) menu.classList.remove("open");
    });
  }
})();

/* v58：缩放沿视线 dolly（目标点不变 → 不改变视角方向，只改距离；选点/病例状态不受影响） */
function zoomCam(factor) {
  var off = camera.position.clone().sub(controls.target).multiplyScalar(factor);
  camera.position.copy(controls.target.clone().add(off));
  controls.update();
  camera.updateMatrixWorld(true);
}
[["zoomIn", function () { zoomCam(0.85); }], ["zoomOut", function () { zoomCam(1.18); }],
 ["resetCam", function () { setView("reset"); }]].forEach(function (pair) {
  var el = document.getElementById(pair[0]);
  if (el) el.addEventListener("click", pair[1]);
});
[["shot", screenshot], ["clearSel", clearAllConfirmed], ["dbgMode", toggleDebug], ["labelMode", function () {
  if (REGION_DRAW.active || REGION_DRAW.preview) exitRegionDraw(); else if (CALIB.mode) exitLabelMode(); else enterRegionDraw();
}], ["ctxCompressBtn", function () {
  document.getElementById("plusMenu").classList.remove("open");
  sendChat(t("压缩上下文", "Compress context"));   // v73：与输入框指令同一条链路
}], ["wizard", startWizard]].forEach(function (pair) {
  var el = document.getElementById(pair[0]);
  if (el) el.addEventListener("click", pair[1]);  // 元素缺失时优雅跳过（版本/缓存差异防护）
});
(function () {
  var btn = document.getElementById("importGlb");
  var file = document.getElementById("glbFile");
  if (!btn || !file) return;
  btn.addEventListener("click", function () { file.click(); });
  file.addEventListener("change", function () {
    var f = file.files[0];
    if (!f) return;
    var reader = new FileReader();
    reader.onload = function () { importCustomGLB(reader.result, f.name); };
    reader.readAsArrayBuffer(f);
  });
})();

/* 男女（多）模型切换：下拉选中即显式加载该模型（失败只报错不回落到其他模型）；
 * 与导入 GLB 互斥——自定义 GLB 不在列表内，选择列表项即回到内置模型。 */
function refreshModelSelect() {
  var sel = document.getElementById("modelSelect");
  if (!sel) return;
  var cur = sel.value;
  sel.innerHTML = "";
  MODELS.forEach(function (m, i) {
    var opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = (LANG === "en" && m.en) ? m.en : m.zh;
    sel.appendChild(opt);
  });
  sel.value = cur || String(modelIndex);
}
(function () {
  var sel = document.getElementById("modelSelect");
  if (!sel) return;
  refreshModelSelect();
  sel.value = String(modelIndex);
  sel.addEventListener("change", function () {
    var i = parseInt(sel.value, 10);
    if (i === modelIndex || isNaN(i) || !MODELS[i]) return;
    loadHumanModel(i, true);
  });
})();

[ ["raUndo", function () { if (REGION_DRAW.points.length) { REGION_DRAW.points.pop(); REGION_DRAW.hits.pop(); updateRegionDrawUI(); } }],
  ["raFinish", finishRegionDraw], ["raCancel", exitRegionDraw], ["raExport", exportManualRegions],
  ["llmBatch", requestLLMBatch],
  ["llmReview", function () { toggleLLMReview(); }], ["llmReviewClose", function () { toggleLLMReview(false); }] ].forEach(function (pair) {
  var element = document.getElementById(pair[0]);
  if (element) element.addEventListener("click", pair[1]);
});
var raLabel = document.getElementById("raLabel");
if (raLabel) raLabel.addEventListener("change", function () { if (REGION_DRAW.preview) commitRegionDraw(); });

/* —— v43 i18n：界面语言切换（简体中文/English）。静态界面文案双语言；
   AI 生成内容（服务端草稿/报告）与医嘱类文案保持中文（合规文本不翻译）。
   偏好双端共享：bridge.uiPrefsGet/Set（ui-prefs.json）优先，无 bridge 回退 localStorage。 */
var LANG = "zh";
var I18N_TEXT = {
  settingsClose: { zh: "← 返回应用", en: "← Back to App" },
  navGroupLabel: { zh: "设置", en: "Settings" },
  navGeneral: { zh: "通用", en: "General" },
  navLlm: { zh: "LLM 与模型", en: "LLM & Model" },
  navKb: { zh: "医学书籍知识库", en: "Medical Book Library" },
  navSecurity: { zh: "检索与安全", en: "Retrieval & Safety" },
  paneGeneralTitle: { zh: "通用", en: "General" },
  paneLlmTitle: { zh: "LLM 与模型", en: "LLM & Model" },
  paneKbTitle: { zh: "医学书籍知识库", en: "Medical Book Library" },
  paneSecurityTitle: { zh: "检索与安全", en: "Retrieval & Safety" },
  langHelp: { zh: "界面语言（与原生窗口「⚙ 设置」共享，切换即全端生效）",
              en: "UI language (shared with the native window; applies everywhere instantly)" },
  langSecTitle: { zh: "🌐 语言 / Language", en: "🌐 Language / 语言" },
  agentSecTitle: { zh: "🔗 接入方式", en: "🔗 Connection" },
  agentHelp: { zh: "数据来源：server 为服务端管线（打开程序自动启动本机服务端，支持跨端续办）；mock 为离线演示（断网或服务端不可用时的退路）。服务地址是 DermAgent 服务端，不是 LLM 网关（LLM 在「LLM 与模型」里配置）。",
               en: "Data source: server = server pipeline (auto-starts the local server, supports cross-device resume); mock = offline demo (fallback when offline or server unavailable). The URL is the DermAgent server, NOT the LLM gateway (configure LLM under \"LLM & Model\")." },
  agentSaveBtn: { zh: "💾 保存接入设置", en: "💾 Save Connection Settings" },
  llmSecTitle: { zh: "🔌 LLM 连接", en: "🔌 LLM Connection" },
  llmSaveBtn: { zh: "💾 保存 LLM 连接", en: "💾 Save LLM Connection" },
  kbSecTitle: { zh: "📚 书籍管理（本地 RAG）", en: "📚 Book Management (Local RAG)" },
  ragSecTitle: { zh: "🔎 RAG 运行设置", en: "🔎 RAG Runtime" },
  embSecTitle: { zh: "🧠 向量模型（本地语义检索）", en: "🧠 Embedding Model (Local Semantic Retrieval)" },
  embHintLine: { zh: "首次安装需联网下载模型文件；下载完成后语义检索全程本地运行，书籍原文绝不发送到任何外部服务。",
                 en: "First install downloads model files online; afterwards semantic retrieval runs fully locally and book text never leaves this machine." },
  secSecTitle: { zh: "🛡 版本与安全", en: "🛡 Version & Safety" },
  dataLocationLine: { zh: "数据位置：本地 server/data/rag/（书籍、切分文本、索引、元数据与审计全部落盘本地，不依赖云端服务）。本设置界面与原生窗口「⚙ 设置」共享同一套数据。",
                      en: "Data location: local server/data/rag/ (books, chunks, index, metadata and audit are all stored locally, no cloud dependency). This settings page shares the same data as the native ⚙ Settings dialog." },
  kbImportBtn: { zh: "📂 批量导入书籍", en: "📂 Import Books" },
  kbDelHardBtn: { zh: "🗑 彻底删除", en: "🗑 Delete permanently" },
  kbDelHardConfirm: { zh: "将彻底删除《", en: "Permanently delete \"" },
  kbDelHardConfirm2: { zh: "》及其全部索引，不可恢复。确定删除？", en: "\" and all of its index? This cannot be undone." },
  kbDelHardDone: { zh: "已彻底删除：", en: "Deleted permanently: " },
  kbImportViaHttp: { zh: "（经本机服务端直连，支持大文件）", en: "(direct to local server, large files supported)" },
  kbImportViaBridge: { zh: "（经桌面桥，仅建议小文件）", en: "(via desktop bridge; small files only)" },
  kbImportBusy: { zh: "已有导入在进行，请稍候。", en: "An import is already running; please wait." },
  /* v72：导入进度 UI——进度条/阶段/耗时/逐本结果（用户要求可见的导入反馈） */
  kbPhaseReading: { zh: "读取文件中…", en: "Reading file…" },
  kbPhaseUploading: { zh: "上传中…", en: "Uploading…" },
  kbPhaseFallback: { zh: "直连异常，改经桌面桥重试…", en: "Direct connection failed; retrying via desktop bridge…" },
  kbBackupBtn: { zh: "💾 备份知识库", en: "💾 Backup Library" },
  kbRestoreBackupBtn: { zh: "↩ 恢复最近备份", en: "↩ Restore Last Backup" },
  histTitle: { zh: "🕘 历史对话", en: "🕘 History" },
  selTitle: { zh: "📋 已选位置", en: "📋 Selected Regions" },
  llmReviewTitle: { zh: "🧠 LLM 精细结果审核", en: "🧠 LLM Refinement Review" },
  assistTitle: { zh: "🧠 医生辅助判断", en: "🧠 Physician Assist" },
  clinicEntry: { zh: "📋 病历单", en: "📋 Record Form" },
  assistEntry: { zh: "🧠 医生辅助判断", en: "🧠 Physician Assist" },
  addImage: { zh: "📎 添加图片附件", en: "📎 Add Image Attachment" },
  labelMode: { zh: "🏷 标注模式", en: "🏷 Label Mode" },
  wizard: { zh: "🧭 校准向导", en: "🧭 Calibration Wizard" },
  importGlb: { zh: "📁 导入 GLB 模型", en: "📁 Import GLB Model" },
  shot: { zh: "📷 截图入报告", en: "📷 Screenshot to Report" },
  clearSel: { zh: "🗑 清空全部标记", en: "🗑 Clear All Markers" },
  dbgMode: { zh: "🐞 调试模式", en: "🐞 Debug Mode" },
  menuSelDrawer: { zh: "📋 已选位置列表", en: "📋 Selected Regions" },
  menuHistDrawer: { zh: "🕘 历史对话", en: "🕘 History" },
  clinicTitle: { zh: "📋 门诊病历单（随下一条消息提交）", en: "📋 Outpatient Record (sent with next message)" },
  clinicSecPatient: { zh: "患者基本信息", en: "Patient Information" },
  clinicSecHx: { zh: "主诉 / 现病史", en: "Chief Complaint / HPI" },
  clinicSecLab: { zh: "辅助检查 · 结构化检验", en: "Tests · Structured Lab Results" },
  clinicSecReport: { zh: "辅助检查 · 报告文字导入", en: "Tests · Report Text Import" },
  clinicSecMeds: { zh: "既往史 / 过敏史 / 用药 / 当前治疗", en: "Past History / Allergies / Medications / Current Treatment" },
  clinicSecImg: { zh: "查体影像（png/jpg/webp/bmp/gif，≤10MB，逐张验证完整性）", en: "Exam Images (png/jpg/webp/bmp/gif, ≤10MB, verified one by one)" },
  clinicReportImport: { zh: "导入报告文件", en: "Import Report File" },
  labAddRow: { zh: "＋ 加一行", en: "＋ Add Row" },
  clinicImgAdd: { zh: "添加影像", en: "Add Image" },
  clinicClear: { zh: "🗑 清空资料", en: "🗑 Clear Clinical Info" },
  imgEmpty: { zh: "尚无影像", en: "No images yet" },
  assistSecBoundary: { zh: "AI 辅助建议边界（常显）", en: "AI Assist Boundary (always shown)" },
  assistSecSel: { zh: "本次分析使用的资料（默认全选，可逐项取消）", en: "Materials used this analysis (all selected by default)" },
  assistSecRefs: { zh: "参考病例（未经逐个确认不参与分析）", en: "Reference cases (excluded until confirmed one by one)" },
  assistSecPreview: { zh: "发送预览（默认脱敏：姓名/身份证号/电话/地址）", en: "Send Preview (masked by default: name/ID/phone/address)" },
  assistSecDx: { zh: "诊断辅助（辅助排序，非确诊概率）", en: "Diagnostic Assist (ranking, NOT diagnosis probability)" },
  assistSecAdvice: { zh: "临床建议（检查/治疗方向/用药核对）", en: "Clinical Suggestions (tests/treatment direction/medication check)" },
  assistSecAudit: { zh: "审核与审计", en: "Review & Audit" },
  flowGuideTitle: { zh: "🧭 下一步操作指引", en: "🧭 Next Step Guide" },
  flowGuideSend: { zh: "发送补充", en: "Send Supplement" },
  flowGuideLater: { zh: "稍后自己填", en: "Fill In Later" },
  flowGuideOpenAssist: { zh: "打开审核面板", en: "Open Review Panel" }
};
var I18N_PH = {
  chatInput: { zh: "描述症状或问题（可同时附加 3D 选点与图片）… Enter 发送 · Shift+Enter 换行",
               en: "Describe symptoms (3D selections and images can be attached)… Enter to send · Shift+Enter for newline" },
  clinicHx: { zh: "主诉、现病史、皮损观察记录…", en: "Chief complaint, HPI, lesion observation…" },
  clinicPatName: { zh: "姓名", en: "Name" },
  clinicPatAge: { zh: "年龄（如 34）", en: "Age (e.g. 34)" },
  clinicPatNo: { zh: "门诊号（留空自动生成）", en: "Outpatient No. (auto if blank)" },
  clinicReport: { zh: "粘贴检验/病理/影像报告文字，或从 .txt/.md/.html 导入", en: "Paste lab/pathology/imaging report text, or import from .txt/.md/.html" },
  clinicMeds: { zh: "当前与近期用药（名称/剂量/频次）…", en: "Current and recent medications (name/dose/frequency)…" },
  clinicAllergy: { zh: "药物/食物/接触过敏史…", en: "Drug/food/contact allergy history…" },
  clinicPast: { zh: "既往史、家族史…", en: "Past medical history, family history…" },
  clinicTx: { zh: "当前治疗（外用/系统治疗/光疗等）…", en: "Current treatment (topical/systemic/phototherapy…)…" },
  llmBaseUrl: { zh: "Base URL（如 https://api.example.com/v1）", en: "Base URL (e.g. https://api.example.com/v1)" },
  llmModel: { zh: "模型名（如 gpt-4o / qwen-max）", en: "Model name (e.g. gpt-4o / qwen-max)" },
  llmApiKey: { zh: "API Key（留空 = 保持已保存的 Key 不变）", en: "API Key (leave blank to keep the saved key)" },
  agentUrlInput: { zh: "服务端地址（如 http://127.0.0.1:8765，非 LLM 网关）",
                   en: "Server URL (e.g. http://127.0.0.1:8765, NOT the LLM gateway)" }
};
var I18N_TITLE = {
  settingsClose: { zh: "返回主界面", en: "Back to the app" },
  settingsEntry: { zh: "⚙ 设置：语言 / LLM 连接 / 知识库 / RAG 运行 / 版本与安全（左侧栏最底部，与原生窗口共享同一套数据）",
                   en: "⚙ Settings at the bottom of the sidebar: language / LLM / library / RAG / security (shared with the native window)" },
  modelSelect: { zh: "切换人体模型（男/女）", en: "Switch body model (male/female)" },
  plusBtn: { zh: "更多功能", en: "More" },
  sendBtn: { zh: "发送（Enter）", en: "Send (Enter)" },
  kbImportBtn: { zh: "批量导入医学书籍（txt/md/html 内置解析；pdf/docx/epub 解析器未装时明确失败，不伪造解析结果）",
                 en: "Import medical books in batch (txt/md/html built-in; pdf/docx/epub fail explicitly until parsers installed)" },
  kbBackupBtn: { zh: "备份书籍、切分文本、索引与元数据到本地备份目录", en: "Backup books, chunks, index and metadata locally" },
  embModelSelect: { zh: "选择要安装或启用的本地向量模型；检索与书籍原文全程在本机运行，原文不出本机",
                    en: "Choose a local embedding model to install or enable; retrieval and book text stay on this machine" },
  embInstallBtn: { zh: "下载并安装所选向量模型（首次安装需联网下载模型文件；之后检索全部本地运行，书籍原文不出本机）",
                   en: "Download and install the selected embedding model (network needed once; retrieval then runs fully locally)" },
  flowGuideClose: { zh: "关闭", en: "Close" },
  llmSaveBtn: { zh: "保存到本机服务端配置（server/data/llm_config.json）；密钥不进入客户端代码与日志",
                en: "Save to local server config (server/data/llm_config.json); the key never enters client code or logs" },
  agentModeSelect: { zh: "选择接入模式：server（服务端管线，自动启动本机服务端）或 mock（离线演示）",
                     en: "Choose connection mode: server (server pipeline, auto-starts the local server) or mock (offline demo)" },
  agentSaveBtn: { zh: "保存接入设置；server 模式保存后立即检查/自动启动本机服务端",
                  en: "Save connection settings; in server mode the local server is checked/started right after saving" }
};

function applyLanguage() {
  function setProp(map, prop) {
    Object.keys(map).forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el[prop] = map[id][LANG];
    });
  }
  setProp(I18N_TEXT, "textContent");
  setProp(I18N_PH, "placeholder");
  setProp(I18N_TITLE, "title");
  if (typeof refreshModelSelect === "function") refreshModelSelect();
  if (typeof refreshLlmModelSelect === "function") refreshLlmModelSelect();
  if (typeof refreshLlmThinkingSelect === "function") refreshLlmThinkingSelect();
  if (typeof refreshEmbedding === "function") refreshEmbedding();
  if (typeof updateMiniTag === "function") updateMiniTag();       // v58：小窗标签随语言重渲染
  if (typeof renderFlowGuide === "function") renderFlowGuide();
  if (typeof renderRailHist === "function") renderRailHist();     // v73-fix：侧栏流是动态渲染（时间分组/删除按钮），切换语言后必须重建
  var zh = document.getElementById("langZh"), en = document.getElementById("langEn");
  if (zh) zh.checked = LANG === "zh";
  if (en) en.checked = LANG === "en";
}

function t(zh, en) { return LANG === "en" ? en : zh; }

function setLanguage(lang, persist) {
  LANG = (lang === "en") ? "en" : "zh";
  applyLanguage();
  if (persist !== false) {
    try { localStorage.setItem("derm_lang", LANG); } catch (e) {}
    if (bridge && bridge.uiPrefsSet) bridge.uiPrefsSet(JSON.stringify({ language: LANG }));
  }
}

/* 供原生设置对话框回推（PySide6 runJavaScript 调用） */
window.__applyLanguage = function (lang) { setLanguage(lang, false); return "ok"; };

function loadLanguagePref() {
  if (bridge && bridge.uiPrefsGet) {
    bridge.uiPrefsGet(function (s) {
      var p = parseKbJson(s);
      if (p && p.language && p.language !== LANG) setLanguage(p.language, false);
    });
  } else {
    try {
      var saved = localStorage.getItem("derm_lang");
      if (saved && saved !== LANG) setLanguage(saved, false);
    } catch (e) {}
  }
}

/* —— v72：LLM 多连接管理。Key 仅随保存动作短暂进入 bridge，列表永不回传 Key。 —— */
var LLM_THINKING_LEVELS = [
  { v: "", zh: "思考：标准", en: "Thinking: Std" },
  { v: "low", zh: "思考：低", en: "Thinking: Low" },
  { v: "medium", zh: "思考：中", en: "Thinking: Med" },
  { v: "high", zh: "思考：高", en: "Thinking: High" }
];
var llmStore = { active_id: "", connections: [], providers: {} };
var curLlm = { id: "", base_url: "", model: "", has_key: false, thinking: "" };

function activeLlmConnection() {
  for (var i = 0; i < llmStore.connections.length; i++) if (llmStore.connections[i].id === llmStore.active_id) return llmStore.connections[i];
  return null;
}
function refreshLlmModelSelect() {
  var sel = document.getElementById("llmModelSelect"); if (!sel) return;
  sel.textContent = "";
  if (!bridge || !bridge.llmConnectionActivate) {
    sel.disabled = true; var off = document.createElement("option"); off.textContent = t("LLM 离线", "LLM offline"); sel.appendChild(off); return;
  }
  sel.disabled = !llmStore.connections.length;
  llmStore.connections.forEach(function(c) {
    var o = document.createElement("option"); o.value = c.id;
    o.textContent = (c.name || c.provider || "LLM") + " · " + (c.model || t("未设模型", "No model")); sel.appendChild(o);
  });
  if (llmStore.active_id) sel.value = llmStore.active_id;
  sel.title = t("切换活动 LLM 连接", "Switch active LLM connection");
}
function switchLlmModel(connectionId) {
  if (!bridge || !bridge.llmConnectionActivate || !connectionId) return;
  bridge.llmConnectionActivate(connectionId, function(s) { var r = parseKbJson(s); if (r && r.ok) refreshLlmStatus(); });
}
function refreshLlmThinkingSelect() {
  var sel = document.getElementById("llmThinkingSelect"); if (!sel) return;
  sel.textContent = ""; sel.disabled = !curLlm.id;
  LLM_THINKING_LEVELS.forEach(function(lv) { var o=document.createElement("option"); o.value=lv.v; o.textContent=t(lv.zh,lv.en); sel.appendChild(o); });
  sel.value = curLlm.thinking || "";
}
function switchLlmThinking(level) {
  var c = activeLlmConnection(); if (!c || !bridge || !bridge.llmConnectionSave) return;
  var payload = { id:c.id, name:c.name, provider:c.provider, base_url:c.base_url, model:c.model, thinking_level:level || "" };
  bridge.llmConnectionSave(JSON.stringify(payload), function(s) { var r=parseKbJson(s); if (r&&r.ok) refreshLlmStatus(); });
}
function llmIcon(c) { return c.provider === "deepseek" ? "🐋" : (c.provider === "zhipu" ? "✣" : ((c.name || "L").charAt(0).toUpperCase())); }
function renderLlmConnections() {
  var box=document.getElementById("llmConnections"); if (!box) return; box.textContent="";
  if (!llmStore.connections.length) { var e=document.createElement("div"); e.className="llm-connection-empty"; e.textContent=t("尚无模型连接，点击“新增连接”开始配置。","No model connections. Add one to begin."); box.appendChild(e); return; }
  llmStore.connections.forEach(function(c) {
    var card=document.createElement("div"); card.className="llm-connection-card"+(c.id===llmStore.active_id?" active":"");
    var icon=document.createElement("div"); icon.className="llm-provider-icon "+c.provider; icon.textContent=llmIcon(c); card.appendChild(icon);
    var info=document.createElement("div"); info.className="llm-connection-info";
    var name=document.createElement("div"); name.className="llm-connection-name"; name.textContent=c.name || c.model || "LLM"; info.appendChild(name);
    var url=document.createElement("div"); url.className="llm-connection-url"; url.textContent=c.base_url || "—"; info.appendChild(url);
    var meta=document.createElement("div"); meta.className="llm-connection-meta";
    meta.textContent=(c.model||t("未设置模型","No model"))+" · "+(c.has_key?t("Key 尾号 ","Key …")+(c.key_tail||""):t("未设置 Key","No key")); info.appendChild(meta); card.appendChild(info);
    var acts=document.createElement("div"); acts.className="llm-connection-actions";
    if(c.id===llmStore.active_id){var pill=document.createElement("span");pill.className="llm-active-pill";pill.textContent=t("使用中","Active");acts.appendChild(pill);}
    else {var use=document.createElement("button");use.className="use";use.textContent=t("▷ 启用","▷ Use");use.onclick=function(){switchLlmModel(c.id);};acts.appendChild(use);}
    var test=document.createElement("button");test.textContent=t("↻ 测试","↻ Test");test.onclick=function(){testLlmConnection(c);};acts.appendChild(test);
    var edit=document.createElement("button");edit.textContent=t("✎ 编辑","✎ Edit");edit.onclick=function(){openLlmEditor(c);};acts.appendChild(edit);
    var del=document.createElement("button");del.textContent="⌫";del.title=t("删除连接","Delete connection");del.onclick=function(){deleteLlmConnection(c);};acts.appendChild(del);
    card.appendChild(acts); box.appendChild(card);
  });
}
function openLlmEditor(c) {
  c=c||{}; var ed=document.getElementById("llmEditor"); ed.style.display="";
  document.getElementById("llmEditorTitle").textContent=c.id?t("编辑 LLM 连接","Edit LLM connection"):t("新增 LLM 连接","Add LLM connection");
  document.getElementById("llmConnectionId").value=c.id||""; document.getElementById("llmProvider").value=c.provider||"custom";
  document.getElementById("llmName").value=c.name||""; document.getElementById("llmBaseUrl").value=c.base_url||"";
  document.getElementById("llmModel").value=c.model||""; document.getElementById("llmApiKey").value=""; document.getElementById("llmClearKey").checked=false;
}
function closeLlmEditor(){document.getElementById("llmEditor").style.display="none";document.getElementById("llmApiKey").value="";}
function applyLlmProviderPreset(){
  var p=document.getElementById("llmProvider").value, preset=llmStore.providers[p]||{};
  if(!document.getElementById("llmName").value) document.getElementById("llmName").value=preset.name||"";
  if(preset.base_url) document.getElementById("llmBaseUrl").value=preset.base_url;
  if((preset.models||[]).length) document.getElementById("llmModel").value=preset.models[0];
}
function refreshLlmStatus() {
  var el=document.getElementById("llmStatus");
  if(!bridge || !bridge.llmConnectionsList){if(el)el.textContent=t("未连接桌面端（离线模式）。","Desktop bridge offline.");refreshLlmModelSelect();return;}
  bridge.llmConnectionsList(function(s){var d=parseKbJson(s);if(!d||!d.ok){if(el)el.textContent=t("LLM 配置读取失败。","Failed to read LLM settings.");return;}
    llmStore=d; var c=activeLlmConnection(); curLlm={id:c?c.id:"",base_url:c?c.base_url:"",model:c?c.model:"",has_key:!!(c&&c.has_key),thinking:c?c.thinking_level:""};
    if(el)el.textContent=c?t("当前活动：","Active: ")+(c.name||c.model)+" · "+(c.model||"—"):t("尚未配置活动模型连接。","No active model connection.");
    renderLlmConnections();refreshLlmModelSelect();refreshLlmThinkingSelect();
  });
}
function saveLlmConfig(){
  var id=document.getElementById("llmConnectionId").value, key=document.getElementById("llmApiKey").value;
  var data={id:id,name:document.getElementById("llmName").value.trim(),provider:document.getElementById("llmProvider").value,
    base_url:document.getElementById("llmBaseUrl").value.trim(),model:document.getElementById("llmModel").value.trim(),api_key:key,
    clear_key:document.getElementById("llmClearKey").checked,thinking_level:(id===llmStore.active_id?curLlm.thinking:"")};
  if(!/^https?:\/\//i.test(data.base_url)){document.getElementById("llmStatus").textContent=t("Base URL 须以 http:// 或 https:// 开头。","Base URL must start with http:// or https://.");return;}
  if(!bridge||!bridge.llmConnectionSave)return;
  bridge.llmConnectionSave(JSON.stringify(data),function(s){document.getElementById("llmApiKey").value="";var r=parseKbJson(s);if(r&&r.ok){closeLlmEditor();refreshLlmStatus();}else document.getElementById("llmStatus").textContent=t("保存失败：","Save failed: ")+((r&&r.reason)||"unknown");});
}
function deleteLlmConnection(c){if(!bridge||!bridge.llmConnectionDelete||!confirm(t("确定删除连接“","Delete connection \"")+(c.name||c.model)+"\"?"))return;bridge.llmConnectionDelete(c.id,function(){refreshLlmStatus();});}
function testLlmConnection(c){var el=document.getElementById("llmStatus");if(el)el.textContent=t("正在测试 ","Testing ")+(c.name||c.model)+"…";
  fetch(KB_SERVER+"/api/llm/connections/"+encodeURIComponent(c.id)+"/test",{cache:"no-store"}).then(function(r){return r.json();}).then(function(d){if(el)el.textContent=d.reachable?t("连接可达：","Reachable: ")+(c.name||c.model)+(d.error_code?" · "+d.message:""):t("连接失败：","Connection failed: ")+(d.message||"unknown");}).catch(function(e){if(el)el.textContent=t("连接失败：","Connection failed: ")+e;});}

/* —— v54：接入方式（顶栏控件移除后收纳于此）。bridge.agentSettingsGet/Set 读写同一
   client/data/settings.json；切 server 保存后原生侧立即探测/带起本机服务端。无 bridge 降级禁用。 */
function toggleAgentUrlRow() {
  var sel = document.getElementById("agentModeSelect");
  var url = document.getElementById("agentUrlInput");
  if (sel && url) url.style.display = (sel.value === "server") ? "" : "none";
}

function refreshAgentSettings() {
  var sel = document.getElementById("agentModeSelect");
  var url = document.getElementById("agentUrlInput");
  var status = document.getElementById("agentStatus");
  if (!sel || !url) return;
  if (!bridge || !bridge.agentSettingsGet) {
    sel.disabled = true; url.disabled = true;
    var btn = document.getElementById("agentSaveBtn");
    if (btn) btn.disabled = true;
    sel.title = t("未连接桌面端（离线模式），无法修改接入方式。",
                  "Desktop bridge not connected (offline); cannot change connection mode.");
    if (status) status.textContent = t("未连接桌面端（离线模式）。", "Desktop bridge not connected (offline).");
    return;
  }
  bridge.agentSettingsGet(function (s) {
    var cfg = parseKbJson(s);
    if (!cfg) { if (status) status.textContent = t("接入设置读取失败。", "Failed to read connection settings."); return; }
    sel.value = (cfg.mode === "mock") ? "mock" : "server";
    url.value = cfg.server_url || "";
    toggleAgentUrlRow();
  });
}

function saveAgentSettings() {
  var status = document.getElementById("agentStatus");
  function say(m) { if (status) status.textContent = m; }
  if (!bridge || !bridge.agentSettingsSet) {
    say(t("未连接桌面端（离线模式），无法保存。", "Desktop bridge not connected (offline); cannot save."));
    return;
  }
  var mode = document.getElementById("agentModeSelect").value;
  var url = (document.getElementById("agentUrlInput").value || "").trim();
  if (mode === "server" && !/^https?:\/\//i.test(url)) {
    say(t("服务地址须以 http:// 或 https:// 开头。", "Server URL must start with http:// or https://."));
    return;
  }
  bridge.agentSettingsSet(JSON.stringify({ mode: mode, server_url: url }), function (s) {
    var r = parseKbJson(s);
    if (r && r.ok) {
      say(mode === "server"
        ? t("已保存：server 模式（正在检查/启动本机服务端…）。", "Saved: server mode (checking/starting the local server…).")
        : t("已保存：mock 离线演示模式。", "Saved: mock offline demo mode."));
    } else say(t("保存失败：", "Save failed: ") + ((r && r.reason) || "unknown"));
  });
}

/* —— §16.1 设置面板（左下角齿轮）：知识库管理 / RAG 运行设置 / 版本与安全。
   与 PySide6 原生「⚙ 设置」对话框共享同一套本地 rag 落盘（server/data/rag/）。
   全部渲染走 textContent（服务端文本不进 HTML）；bridge 缺失时明确降级提示，不伪造数据。 */
function parseKbJson(s) { try { return JSON.parse(s); } catch (e) { return null; } }

function kbStatusMsg(msg) {
  var el = document.getElementById("kbStatus");
  if (el) el.textContent = msg || "";
}

function renderKbBooks(res) {
  var list = document.getElementById("kbList");
  if (!list) return;
  list.textContent = "";
  var books = (res && res.books) || [];
  kbStatusMsg(t("知识库已连接 · 共 ", "Knowledge base connected · ") + books.length + t(" 本书", " books"));
  if (!books.length) {
    var empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = t("尚无书籍。批量导入 txt/md/html 开始建立本地知识库（解析失败的书会明确标注原因，不进入检索）。",
      "No books yet. Import txt/md/html files to build the local library (failures are labeled explicitly and never enter retrieval).");
    list.appendChild(empty);
  }
  books.forEach(function (b) {
    var row = document.createElement("div");
    row.className = "kb-item" + (b.quarantined ? " kb-del" : "");
    var head = document.createElement("div"); head.className = "kb-head";
    var name = document.createElement("span"); name.className = "kb-name";
    name.textContent = (b.title || b.filename || b.book_id) + (b.format_incomplete ? "（元数据不完整）" : "");
    var pill = document.createElement("span"); pill.className = "kb-pill " + b.status;
    pill.textContent = b.status_label || b.status;
    head.appendChild(name); head.appendChild(pill);
    var meta = document.createElement("div"); meta.className = "kb-meta";
    var parts = [];
    if (b.version_label) parts.push(b.version_label);
    if (b.section_count != null) parts.push("片段 " + b.section_count);
    if (b.updated_label) parts.push(b.updated_label);
    if (b.error) parts.push("失败原因：" + b.error);
    meta.textContent = parts.join(" · ");
    var actions = document.createElement("div"); actions.className = "kb-actions";
    var del = document.createElement("button");
    del.textContent = b.quarantined ? t("↩ 恢复检索", "↩ Resume retrieval") : t("⏸ 停止检索（可恢复）", "⏸ Pause retrieval (resumable)");
    del.addEventListener("click", function () {
      if (!bridge || !bridge.kbDelete) { kbStatusMsg(t("未连接桌面端本地知识库（离线模式）。", "Desktop library not connected (offline).")); return; }
      var fn = b.quarantined ? bridge.kbRestore : bridge.kbDelete;
      fn.call(bridge, b.book_id, function (s) {
        var r2 = parseKbJson(s);
        if (r2 && r2.ok) {
          kbStatusMsg((b.quarantined ? t("已恢复检索：", "Retrieval resumed: ") : t("已停止检索（数据保留，可随时恢复）：", "Retrieval paused (data kept, resumable): ")) + b.book_id);
          refreshKbBooks();
        } else kbStatusMsg(t("操作失败：", "Operation failed: ") + ((r2 && r2.reason) || "unknown"));
      });
    });
    actions.appendChild(del);
    /* v71：物理删除——「停止检索」只是软删（书仍在列表，检索暂停），医生需要真正的删除。 */
    var hard = document.createElement("button");
    hard.textContent = t("🗑 彻底删除", "🗑 Delete permanently");
    hard.addEventListener("click", function () {
      var label = b.title || b.filename || b.book_id;
      if (!window.confirm(t("将彻底删除《", "Permanently delete \"") + label +
                          t("》及其全部索引，不可恢复。确定删除？", "\" and all of its index? This cannot be undone."))) return;
      function fin(r) {
        if (r && r.ok) { kbStatusMsg(t("已彻底删除：", "Deleted permanently: ") + label); refreshKbBooks(); }
        else kbStatusMsg(t("删除失败：", "Delete failed: ") + ((r && r.reason) || "unknown"));
      }
      if (KB_SERVER && window.fetch) {
        fetch(KB_SERVER + "/api/kb/books/" + encodeURIComponent(b.book_id) + "/hard", { method: "DELETE" })
          .then(function (resp) { return resp.json().catch(function () { return { ok: false, reason: "http_" + resp.status }; }); })
          .then(fin)
          .catch(function () {
            if (bridge && bridge.kbDeleteHard) bridge.kbDeleteHard(b.book_id, function (s) { fin(parseKbJson(s)); });
            else fin({ ok: false, reason: "offline" });
          });
      } else if (bridge && bridge.kbDeleteHard) {
        bridge.kbDeleteHard(b.book_id, function (s) { fin(parseKbJson(s)); });
      } else {
        kbStatusMsg(t("未连接桌面端本地知识库（离线模式）。", "Desktop library not connected (offline)."));
      }
    });
    actions.appendChild(hard);
    row.appendChild(head); row.appendChild(meta); row.appendChild(actions);
    list.appendChild(row);
  });
  var snap = document.getElementById("kbSnapshotLine");
  if (snap && res && res.kb_snapshot) snap.textContent = t("知识库快照版本：", "Library snapshot version: ") + res.kb_snapshot;
  var rag = document.getElementById("ragRunSettings");
  if (rag && res && res.embedding_state) {
    var embTxt = res.embedding_state === "ready" ? t("已加载", "loaded")
      : res.embedding_state === "error" ? t("加载失败（检索仅在关键词模式下运行）", "load failed (keyword-only retrieval)")
      : t("未安装（检索仅在关键词模式下运行，引用中如实标注）", "not installed (keyword-only retrieval, labeled honestly in citations)");
    rag.textContent = t("检索模式：", "Retrieval mode: ") + (res.retrieval_mode === "hybrid" ? t("混合（关键词+本地向量）", "Hybrid (keyword + local vector)") : t("关键词（BM25）", "Keyword (BM25)")) +
      " · " + t("本地向量模型：", "Local embedding model: ") + embTxt;
  }
}

// —— v49：本地向量模型自选安装（设置页·知识库 pane） ——

var embPollTimer = null;   // 安装进度轮询
var embCache = null;       // 最近一次 catalog 响应（语言切换重渲染）

function embBtnLabelFor(m) {
  if (m.active) return t("使用中", "In use");
  return m.installed ? t("设为当前", "Set as current") : t("⬇ 安装模型", "⬇ Install model");
}

/* v73：RAG 偏好（检索强度/分块方式）——经本机服务端 HTTP 直连读写 */
function refreshRagPrefs() {
  var st = document.getElementById("ragStrengthSelect");
  var ck = document.getElementById("ragChunkSelect");
  if (!st || !ck || !KB_SERVER) return;
  fetch(KB_SERVER + "/api/rag/prefs").then(function (r) { return r.json(); }).then(function (p) {
    if (p && p.strength) st.value = p.strength;
    if (p && p.chunk_mode) ck.value = p.chunk_mode;
  }).catch(function () {});
}

function saveRagPrefs() {
  var st = document.getElementById("ragStrengthSelect");
  var ck = document.getElementById("ragChunkSelect");
  var msg = document.getElementById("ragPrefsStatus");
  if (!st || !ck || !KB_SERVER) {
    if (msg) msg.textContent = t("未连接本机服务端，无法保存。", "Local server unavailable.");
    return;
  }
  fetch(KB_SERVER + "/api/rag/prefs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ strength: st.value, chunk_mode: ck.value })
  }).then(function (r) { return r.json(); }).then(function (p) {
    if (msg) msg.textContent = (p && p.ok)
      ? t("已保存：检索强度即时生效；切分方式只影响之后导入的书。", "Saved. Strength applies now; chunk mode affects future imports.")
      : t("保存失败。", "Save failed.");
  }).catch(function () {
    if (msg) msg.textContent = t("保存失败：本机服务端不可达。", "Save failed: local server unreachable.");
  });
}

function refreshEmbedding() {
  var sel = document.getElementById("embModelSelect");
  var btn = document.getElementById("embInstallBtn");
  var prog = document.getElementById("embProgressLine");
  if (!sel || !btn) return;
  if (!bridge || !bridge.embeddingInfo) {
    sel.disabled = true; sel.innerHTML = "";
    var o = document.createElement("option");
    o.textContent = t("向量模型：离线", "Embedding: offline");
    sel.appendChild(o);
    btn.disabled = true; btn.textContent = t("离线", "Offline");
    if (prog) prog.style.display = "none";
    return;
  }
  bridge.embeddingInfo(function (s) {
    var res; try { res = JSON.parse(s); } catch (e) { return; }
    if (!res || !res.ok) { sel.disabled = true; btn.disabled = true; return; }
    embCache = res;
    var installing = res.status && res.status.installing;
    sel.disabled = !!installing;
    sel.innerHTML = "";
    res.catalog.forEach(function (m) {
      var o = document.createElement("option");
      o.value = m.short;
      var tag = m.active ? t("（使用中）", " (in use)") : (m.installed ? t("（已安装）", " (installed)") : "");
      o.textContent = m.label + tag;
      sel.appendChild(o);
    });
    sel.value = res.active || (res.catalog[0] && res.catalog[0].short) || "";
    var chosen = res.catalog.filter(function (m) { return m.short === sel.value; })[0];
    btn.disabled = !!installing || !chosen || chosen.active;
    btn.textContent = installing ? t("下载中…", "Downloading…") : (chosen ? embBtnLabelFor(chosen) : "");
    if (prog) {
      if (installing) {
        var mb = function (n) { return (n / 1048576).toFixed(0); };
        prog.textContent = t("⬇ 下载 ", "⬇ Downloading ") + installing.file + " " + installing.pct + "%" +
          (installing.total ? " (" + mb(installing.downloaded) + "/" + mb(installing.total) + " MB)" : "") +
          t(" —— 首次安装需联网，之后检索全程本地运行。", " — network needed once; retrieval then runs fully locally.");
        prog.style.display = "";
      } else if (res.last_error) {
        prog.textContent = t("上次安装失败：", "Last install failed: ") + res.last_error +
          t("（已保留关键词检索）", " (keyword retrieval kept)");
        prog.style.display = "";
      } else {
        prog.style.display = "none";
      }
    }
    if (installing && !embPollTimer) {
      embPollTimer = setInterval(function () { refreshEmbedding(); }, 1200);
    } else if (!installing && embPollTimer) {
      clearInterval(embPollTimer); embPollTimer = null;
      refreshKbBooks();             // 安装结束（成功或失败）：同步 RAG 运行状态行
    }
  });
}

function embModelAction() {
  var sel = document.getElementById("embModelSelect");
  if (!bridge || !bridge.embeddingInstall || !sel || !sel.value) return;
  var chosen = ((embCache && embCache.catalog) || []).filter(function (m) { return m.short === sel.value; })[0];
  if (!chosen || chosen.active) return;
  if (chosen.installed && bridge.embeddingSetActive) {
    bridge.embeddingSetActive(sel.value, function () { refreshEmbedding(); refreshKbBooks(); });
    return;
  }
  bridge.embeddingInstall(sel.value, function () { refreshEmbedding(); });
}

function refreshKbBooks() {
  function viaBridge() {
    if (bridge && bridge.kbList) {
    bridge.kbList(function (s) {
      var r = parseKbJson(s);
      if (r && r.ok) renderKbBooks(r);
      else kbStatusMsg("本地知识库不可用：" + ((r && r.reason) || "未知原因"));
    });
    } else {
    kbStatusMsg(t("未连接桌面端本地知识库（离线模式）：请在原生窗口「⚙ 设置」中管理书籍。",
      "Desktop library not connected (offline): manage books in the native ⚙ Settings."));
    }
  }
  /* 导入走 HTTP 时必须用同一数据源刷新，否则服务端已成功但界面仍显示旧列表。 */
  if (KB_SERVER && window.fetch) {
    fetch(KB_SERVER + "/api/kb/books", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (r) { if (r && Array.isArray(r.books)) renderKbBooks({ ok: true, books: r.books, embedding_state: r.embedding_state, kb_snapshot: r.kb_snapshot }); else viaBridge(); })
      .catch(viaBridge);
  } else {
    viaBridge();
  }
}

/* —— v71：书籍导入走 HTTP 直连本机服务端。根因：QWebChannel 单消息超过约 1MB 会
   丢数据（实测 15MB 书 base64 到达后 size=0）且同步解析冻结 Qt 主线程（界面卡死、
   进度不可见）。serverBase slot 提供基址；不可用时降级桌面桥（仅小文件）。
   v72：serverBase 回填移入 bridge 就绪回调（fetchKbServerBase）——顶层同步执行时
   bridge 尚未就绪导致回填被静默跳过、HTTP 路径从未启用；fetch 加超时防挂起锁死互斥。 —— */
var KB_SERVER = "";            // 本机服务端 HTTP 基址（serverBase 回填）
var kbImportBusy = false;      // 导入互斥：防重复点击导致进度错乱

function fetchKbServerBase(onReady) {
  if (!bridge || !bridge.serverBase) return;
  bridge.serverBase(function (s) {
    var r = parseKbJson(s);
    if (r && r.ok && r.base) {
      KB_SERVER = String(r.base).replace(/\/+$/, "");
      if (typeof onReady === "function") onReady(KB_SERVER);
    } else if (typeof onReady === "function") onReady("");
  });
}

function kbServerImport(item, onDone, timeoutMs) {
  /* v72：AbortController 超时——此前 fetch 无超时，链路挂起时回调永不触发，
     kbImportBusy 永久为 true，后续导入全部被互斥拦截（用户看到"没有反应"）。 */
  var ctl = (typeof AbortController !== "undefined") ? new AbortController() : null;
  var timer = setTimeout(function () {
    try { if (ctl) ctl.abort(); } catch (e) { /* 忽略 */ }
  }, timeoutMs || 60000);
  fetch(KB_SERVER + "/api/kb/books", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename: item.filename, content_base64: item.b64, meta_note: item.meta_note }),
    signal: ctl ? ctl.signal : undefined
  }).then(function (r) {
    return r.json().catch(function () { return { ok: false, reason: "http_" + r.status }; });
  }).then(function (res) {
    clearTimeout(timer);
    onDone(res && res.ok ? res : { ok: false, reason: (res && res.reason) || "http_failed" });
  }).catch(function () {
    clearTimeout(timer);
    onDone(null);   // 通道异常（超时/网络）→ 与服务端明确失败区分，由调用方决定是否降级
  });
}

/* —— v72：导入进度 UI——进度条 + 阶段（读取/上传）+ 已耗时 + 逐本持久结果行。
   用户诉求：看得见"正在导入"、看得到进度条、明确成功/失败、知道耗时多久。 —— */
var kbT0 = 0, kbTimerInt = null;

function kbProgressStart(total) {
  var box = document.getElementById("kbProgress");
  var fill = document.getElementById("kbProgressFill");
  if (box) box.style.display = "";
  if (fill) { fill.className = "kb-progress-fill"; fill.style.width = "0%"; }
  kbT0 = Date.now();
  if (kbTimerInt) clearInterval(kbTimerInt);
  function tick() {
    var el = document.getElementById("kbProgressTimer");
    if (el) el.textContent = t("已耗时 ", "Elapsed ") + ((Date.now() - kbT0) / 1000).toFixed(1) + t(" 秒", "s");
  }
  tick();
  kbTimerInt = setInterval(tick, 200);
  kbResultShow(null, []);          // 清上一轮逐本结果
}

function kbProgressStep(doneN, total, name, phase, indet) {
  var fill = document.getElementById("kbProgressFill");
  if (fill) {
    if (indet) { fill.classList.add("indeterminate"); }
    else {
      fill.classList.remove("indeterminate");
      fill.style.width = (total ? Math.round(doneN / total * 100) : 0) + "%";
    }
  }
  var ph = document.getElementById("kbProgressPhase");
  if (ph) ph.textContent = t("正在导入 ", "Importing ") + doneN + "/" + total +
    (name ? " · " + name : "") + (phase ? " · " + phase : "");
}

function kbProgressFinish() {
  if (kbTimerInt) { clearInterval(kbTimerInt); kbTimerInt = null; }
  var box = document.getElementById("kbProgress");
  if (box) box.style.display = "none";
}

function kbResultShow(head, lines) {
  var box = document.getElementById("kbResult");
  if (!box) return;
  box.textContent = "";
  box.style.display = (head || (lines && lines.length)) ? "" : "none";
  if (head) {
    var h = document.createElement("div");
    h.className = "kb-result-head " + (head.indexOf("✅") === 0 ? "ok" : "bad");
    h.textContent = head;
    box.appendChild(h);
  }
  (lines || []).forEach(function (line) {
    var d = document.createElement("div");
    d.className = "kb-result-line " + (line.indexOf("✅") === 0 ? "ok" : line.indexOf("⏭") === 0 ? "skip" : "bad");
    d.textContent = line;          // 全部 textContent：书名/原因不进 HTML
    box.appendChild(d);
  });
}

function importKbFiles(files) {
  var arr = Array.prototype.slice.call(files || []);
  if (!arr.length) return;
  if (kbImportBusy) { kbStatusMsg(t("已有导入在进行，请稍候。", "An import is already running; please wait.")); return; }
  var useHttp = !!(KB_SERVER && window.fetch);
  if (!useHttp && !(bridge && bridge.kbImport)) {
    kbStatusMsg(t("未连接桌面端本地知识库（离线模式），无法导入。", "Desktop library not connected (offline); cannot import."));
    return;
  }
  kbImportBusy = true;
  var impBtn = document.getElementById("kbImportBtn");
  if (impBtn) impBtn.disabled = true;
  var okN = 0, skipN = 0, i = 0;
  var results = [];                // v72：逐本结果行（进行中即渲染，完成后持久展示）
  kbProgressStart(arr.length);
  kbStatusMsg(t("正在导入 ", "Importing ") + "0/" + arr.length + " …" +
    (useHttp ? t("（经本机服务端直连，支持大文件）", "(direct to local server, large files supported)")
             : t("（经桌面桥，仅建议小文件）", "(via desktop bridge; small files only)")));
  function finish() {
    kbProgressFinish();
    kbImportBusy = false;
    if (impBtn) impBtn.disabled = false;
    var elapsed = ((Date.now() - kbT0) / 1000).toFixed(1);
    var head;
    if (okN === 0 && skipN === 0) {
      head = t("❌ 导入失败：所选书籍均未成功入库（总耗时 ", "❌ Import failed: no book imported (took ") + elapsed + t(" 秒）", "s)");
    } else {
      head = t("✅ 导入完成：成功 ", "✅ Import finished: ") + okN + t(" 本，失败 ", " succeeded, ") +
        (arr.length - okN - skipN) + t(" 本，跳过 ", " failed, ") + skipN +
        t(" 本（总耗时 ", " skipped; took ") + elapsed + t(" 秒）", "s)");
    }
    kbStatusMsg(head);
    kbResultShow(head, results);   // v72：结果持久展示（不随导入结束消失）
    refreshKbBooks();
  }
  function next() {
    if (i >= arr.length) { finish(); return; }
    var f = arr[i];
    var settled = false, watchdog = null;
    function settle(line, kind) {
      if (settled) return;         // 防单本双回调（watchdog 与真实回调竞态）
      settled = true;
      if (watchdog) { clearTimeout(watchdog); watchdog = null; }
      if (kind === "ok") okN++;
      else if (kind === "skip") skipN++;
      results.push(line);
      kbResultShow(null, results);
      i++;
      kbProgressStep(i, arr.length, "", "", false);
      next();
    }
    kbProgressStep(i, arr.length, f.name, t("读取文件中…", "Reading file…"), false);
    if (f.size > 200 * 1024 * 1024) {
      settle(t("⏭ ", "⏭ ") + f.name + t(" — 已跳过（超过 200MB 上限）", " — skipped (over the 200MB limit)"), "skip");
      return;
    }
    /* v72：单本 120s watchdog——任何通道挂起都保证释放互斥、明确失败、继续下一本 */
    watchdog = setTimeout(function () {
      settle(t("❌ ", "❌ ") + f.name + t(" — 失败：", " — failed: ") + "timeout_120s", "fail");
    }, 120000);
    var reader = new FileReader();
    reader.onload = function () {
      var b64 = String(reader.result || "").split(",")[1] || "";
      kbProgressStep(i, arr.length, f.name, t("上传中…", "Uploading…"), true);
      var onRes = function (r) {
        if (r && r.ok) settle(t("✅ ", "✅ ") + f.name + t(" — 已入库，片段 ", " — indexed, chunks ") + r.chunks, "ok");
        else settle(t("❌ ", "❌ ") + f.name + t(" — 失败：", " — failed: ") + ((r && r.reason) || "unexpected result"), "fail");
      };
      if (useHttp) {
        kbServerImport({ filename: f.name, b64: b64, meta_note: "web-gear" }, function (r) {
          if (!r && f.size <= 1024 * 1024 && bridge && bridge.kbImport) {
            /* v72：HTTP 通道异常（超时/网络）且文件 ≤1MB → 降级桌面桥重试一次；
               服务端明确失败（r.ok=false，如重复 SHA/格式不支持）不降级。 */
            kbProgressStep(i, arr.length, f.name, t("直连异常，改经桌面桥重试…", "Direct connection failed; retrying via desktop bridge…"), true);
            bridge.kbImport(JSON.stringify({ filename: f.name, data_b64: b64, meta_note: "web-gear" }), function (s) { onRes(parseKbJson(s)); });
          } else {
            onRes(r);
          }
        });
      } else {
        bridge.kbImport(JSON.stringify({ filename: f.name, data_b64: b64, meta_note: "web-gear" }), function (s) { onRes(parseKbJson(s)); });
      }
    };
    reader.onerror = function () {
      settle(t("❌ ", "❌ ") + f.name + t(" — 文件读取失败", " — failed to read file"), "fail");
    };
    reader.readAsDataURL(f);
  }
  next();
}

function initSettingsUI() {
  var entry = document.getElementById("settingsEntry");
  var drawer = document.getElementById("settingsDrawer");
  if (!entry || !drawer) return;
  var curPane = "general";                           // v45：记住上次所在分区（本次会话内）
  function showPane(name) {
    curPane = name || "general";
    document.querySelectorAll(".nav-item[data-pane]").forEach(function (n) {
      n.classList.toggle("active", n.getAttribute("data-pane") === curPane);
    });
    document.querySelectorAll(".settings-pane[data-pane]").forEach(function (p) {
      p.classList.toggle("active", p.getAttribute("data-pane") === curPane);
    });
  }
  document.querySelectorAll(".nav-item[data-pane]").forEach(function (n) {
    n.addEventListener("click", function () { showPane(this.getAttribute("data-pane")); });
  });
  entry.addEventListener("click", function (ev) {
    ev.stopPropagation();
    var opening = !drawer.classList.contains("open");
    drawer.classList.toggle("open");
    ["clinicDrawer", "assistDrawer", "histDrawer", "selDrawer"].forEach(function (id) {
      var d = document.getElementById(id);
      if (opening && d) d.classList.remove("open");    // 与其他抽屉互斥
    });
    document.getElementById("plusMenu").classList.remove("open");
    if (opening) { showPane(curPane); refreshKbBooks(); refreshLlmStatus(); refreshEmbedding(); refreshAgentSettings(); }
  });
  /* v58：全局入口（左栏「项目」/LLM 不可用提示卡直达指定分区） */
  window.openSettingsPane = function (pane) {
    drawer.classList.add("open");
    if (pane) showPane(pane);
    refreshKbBooks(); refreshLlmStatus(); refreshEmbedding(); refreshAgentSettings();
    fetchKbServerBase(refreshRagPrefs);   // v73：RAG 偏好回填（检索强度/切分方式）
  };
  document.getElementById("settingsClose").addEventListener("click", function () { drawer.classList.remove("open"); });
  document.getElementById("kbImportBtn").addEventListener("click", function () {
    document.getElementById("kbFile").click();
  });
  document.getElementById("kbFile").addEventListener("change", function () {
    var fs = this.files; this.value = ""; importKbFiles(fs);
  });
  var ragSave = document.getElementById("ragPrefsSave");
  if (ragSave) ragSave.addEventListener("click", saveRagPrefs);
  var kbFolder = document.getElementById("kbFolderBtn");
  if (kbFolder) kbFolder.addEventListener("click", function () {
    if (!KB_SERVER) { kbStatusMsg(t("未连接本机服务端，无法导入。", "Local server unavailable.")); return; }
    kbFolder.disabled = true;
    kbStatusMsg(t("正在扫描 book 文件夹…", "Scanning book folder…"));
    fetch(KB_SERVER + "/api/kb/import-folder", { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        kbFolder.disabled = false;
        if (res && res.ok) {
          kbStatusMsg(t("文件夹导入完成：新入库 ", "Folder import done: added ") + res.imported +
            t(" 本，跳过已存在 ", ", skipped existing ") + res.skipped + t(" 本", " book(s)") +
            (res.failed && res.failed.length ? t("；失败：" + res.failed.join("；"), "; failed: " + res.failed.join("; ")) : ""));
          refreshKbBooks();
        } else {
          kbStatusMsg(t("导入失败：" + ((res && res.reason) || "未知原因"), "Import failed: " + ((res && res.reason) || "unknown")));
        }
      })
      .catch(function () {
        kbFolder.disabled = false;
        kbStatusMsg(t("导入失败：本机服务端不可达。", "Import failed: local server unreachable."));
      });
  });
  var kbRebuild = document.getElementById("kbRebuildBtn");
  if (kbRebuild) kbRebuild.addEventListener("click", function () {
    if (!KB_SERVER) { kbStatusMsg(t("未连接本机服务端，无法重建。", "Local server unavailable.")); return; }
    kbRebuild.disabled = true;
    kbStatusMsg(t("正在按当前切分方式重建索引…", "Rebuilding index with current chunk mode…"));
    fetch(KB_SERVER + "/api/kb/rebuild", { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        kbRebuild.disabled = false;
        if (res && res.ok) {
          var modeText = res.mode === "paragraph" ? t("按段落", "paragraph")
            : res.mode === "book" ? t("按书", "book") : t("按章节", "section");
          kbStatusMsg(t("重建完成：", "Rebuilt: ") + res.rebuilt + t(" 本（", " book(s) (") + modeText + t("切分）", ")")
            + (res.skipped ? t("；跳过 " + res.skipped + " 本（旧版导入无缓存原文，需重新导入一次）",
                              "; skipped " + res.skipped + " (no cached text, re-import once)") : ""));
          refreshKbBooks();
        } else {
          kbStatusMsg(t("重建失败。", "Rebuild failed."));
        }
      })
      .catch(function () {
        kbRebuild.disabled = false;
        kbStatusMsg(t("重建失败：本机服务端不可达。", "Rebuild failed: local server unreachable."));
      });
  });
  document.getElementById("kbBackupBtn").addEventListener("click", function () {
    if (!bridge || !bridge.kbBackup) { kbStatusMsg(t("未连接桌面端本地知识库（离线模式），无法备份。", "Desktop library not connected (offline); cannot back up.")); return; }
    bridge.kbBackup(function (s) {
      var r = parseKbJson(s);
      kbStatusMsg(r && r.ok ? t("已备份：", "Backed up: ") + r.path : t("备份失败：", "Backup failed: ") + ((r && r.reason) || "unknown"));
    });
  });
  document.querySelectorAll('input[name="langRadio"]').forEach(function (radio) {
    radio.addEventListener("change", function () { if (this.checked) setLanguage(this.value, true); });
  });
  document.getElementById("llmSaveBtn").addEventListener("click", saveLlmConfig);
  var llmAdd = document.getElementById("llmAddBtn");
  if (llmAdd) llmAdd.addEventListener("click", function () { openLlmEditor(null); });
  var llmCancel = document.getElementById("llmCancelBtn");
  if (llmCancel) llmCancel.addEventListener("click", closeLlmEditor);
  var llmProvider = document.getElementById("llmProvider");
  if (llmProvider) llmProvider.addEventListener("change", applyLlmProviderPreset);
  var agentSel = document.getElementById("agentModeSelect");      // v54：接入方式（顶栏控件移入设置页）
  if (agentSel) agentSel.addEventListener("change", toggleAgentUrlRow);
  var agentBtn = document.getElementById("agentSaveBtn");
  if (agentBtn) agentBtn.addEventListener("click", saveAgentSettings);
  var llmSel = document.getElementById("llmModelSelect");     // v46：composer 旁 LLM 大模型切换
  if (llmSel) llmSel.addEventListener("change", function () { switchLlmModel(this.value); });
  var thinkSel = document.getElementById("llmThinkingSelect");  // v47：思考等级切换
  if (thinkSel) thinkSel.addEventListener("change", function () { switchLlmThinking(this.value); });
  var embBtn = document.getElementById("embInstallBtn");        // v49：向量模型安装/切换
  if (embBtn) embBtn.addEventListener("click", embModelAction);
  var embSel = document.getElementById("embModelSelect");
  if (embSel) embSel.addEventListener("change", refreshEmbedding);
  document.getElementById("kbRestoreBackupBtn").addEventListener("click", function () {
    if (!bridge || !bridge.kbRestoreBackup) { kbStatusMsg(t("未连接桌面端本地知识库（离线模式），无法恢复。", "Desktop library not connected (offline); cannot restore.")); return; }
    bridge.kbRestoreBackup(function (s) {
      var r = parseKbJson(s);
      if (r && r.ok) { kbStatusMsg(t("已从最近备份恢复（", "Restored from last backup (") + r.books + t(" 本书）", " books)")); refreshKbBooks(); }
      else kbStatusMsg("恢复失败：" + ((r && r.reason) || "未知原因"));
    });
  });
  /* v71/v72：获取本机服务端基址——大书导入/彻底删除走 HTTP 直连。bridge 就绪回调
     （initBridge）也会调用一次；此处覆盖 bridge 已先就绪的时序，幂等。 */
  fetchKbServerBase();
}

/* —— v59：界面状态管理 + 两态互斥布局（Agent 对话为主） ——
   状态管理（用户验收要求）：chat_primary（默认，对话全宽主区）/ model_primary（3D 全屏主区）；
   两态互斥显示，绝不叠加——3D 只是可选的身体部位选择工具，不是必填步骤。
   llm_connected / llm_unavailable / offline_demo（LLM 真实性三态）；waiting_review（等待医生审核）。
   原则：3D 场景从不重建（scene/camera/REGIONS 恒定），切回模型模式时 ResizeObserver
   重新跟随 stage 尺寸，选点与视角全保留。 */
var FLOW = null;                     // 最近一次工作流状态推送
var APP_STATE = { layout: "chat_primary", llm: "unknown", demo: false, waitingReview: false };

function setLayout(mode) {
  if (mode !== "chat_primary" && mode !== "model_primary") return;
  APP_STATE.layout = mode;
  var ws = document.getElementById("workstage");
  if (ws) {
    ws.classList.toggle("model-primary", mode === "model_primary");
    ws.classList.toggle("conv-primary", mode === "chat_primary");
  }
  if (mode === "chat_primary") {
    setNavActive("");                 // 布局切换不属于导航选中态
    if (APP_STATE.waitingReview) setStatus(t("等待医生审核", "Waiting for doctor review"), "ok");
  } else {
    var np = document.getElementById("navPane");
    if (np) np.classList.remove("open");
  }
}

/* 一行简洁状态文字（取代流程步骤条） */
function setStatus(msg, kind) {
  var el = document.getElementById("statusLine");
  if (!el) return;
  el.textContent = msg || "";
  el.className = kind || "";
}

/* 3D 小窗简短部位标签（小窗态显示；不复制完整选点数据，仅摘要） */
function updateMiniTag() {
  var el = document.getElementById("miniTag");
  if (!el) return;
  var label = t("小窗 · ", "Mini · ");
  if (selectedRegions.length) {
    var names = selectedRegions.map(function (x) { return x.label || x.id || ""; }).filter(Boolean);
    label += (names.slice(0, 2).join("、") || t("已选 ", "picked ")) + (names.length > 2 ? t(" 等 " + names.length + " 处", " +" + (names.length - 2)) : "");
  } else {
    label = t("小窗", "Mini");   // v73：未选部位不再显示区域占位文字
  }
  el.textContent = label;
}

/* —— 对话区消息流 —— */
function convLogEl() { return document.getElementById("convLog"); }

function appendConvUserCard(rec) {
  var log = convLogEl();
  if (!log) return;
  var card = document.createElement("div");
  card.className = "conv-card";
  var meta = document.createElement("div");
  meta.className = "conv-meta";
  meta.textContent = "#" + rec.n + " · " + rec.time;
  var body = document.createElement("div");
  body.className = "conv-user";
  body.textContent = rec.text || t("（无文字：3D 选点 / 附件 / 资料）", "(no text: regions / files / data)");
  card.appendChild(meta);
  card.appendChild(body);
  if (rec.regions && rec.regions.length) {
    var rg = document.createElement("div");
    rg.className = "conv-meta";
    rg.textContent = "📍 " + rec.regions.join("；");
    card.appendChild(rg);
  }
  (rec.files || []).forEach(function (f) {
    var fl = document.createElement("div");
    fl.className = "conv-meta";
    fl.textContent = "📎 " + f.name;
    card.appendChild(fl);
  });
  if (rec.clinicSummary) {
    var cl = document.createElement("div");
    cl.className = "conv-meta";
    cl.textContent = "🩺 " + rec.clinicSummary;
    card.appendChild(cl);
  }
  log.appendChild(card);
  log.scrollTop = log.scrollHeight;
}

/* 真实性元信息条：LLM 状态 / 模型 / 是否成功 / 返回时间 / 失败原因——只如实呈现 */
function convLlmMetaText(llm) {
  if (!llm) return "";
  var parts = [];
  if (llm.source === "llm") parts.push(t("来源：真实 LLM 分析", "Source: real LLM"));
  else if (llm.source === "template") parts.push(t("来源：本地模板整理（非 AI）", "Source: local template (not AI)"));
  else if (llm.source === "demo") parts.push(t("来源：离线演示数据", "Source: offline demo"));
  else parts.push(t("来源：无（未生成）", "Source: none"));
  if (llm.model && llm.source !== "demo") parts.push(t("模型：", "Model: ") + llm.model);
  if (llm.ok === false && llm.error) parts.push(t("失败原因：", "Failure: ") + llm.error);
  if (llm.at) parts.push(t("返回时间：", "At: ") + String(llm.at).replace("T", " ").slice(0, 19));
  return parts.join(" · ");
}

/* —— v59：文书/医学判断任务卡（doc_ready 产物）——
   渲染守卫与 v58 同纪律：来源只认 llm.source（llm=真实 AI / template=本地模板整理 /
   demo=离线演示 / 其余=未连接），缺失一律按未连接处理——宁可少显示，不伪装真实性。 */
var DOC_TASK_LABELS = {
  case_write: "病例写作", case_edit: "病例编辑", history: "现病史整理",
  lesion_desc: "皮损描述", differential: "鉴别分析", treatment_ref: "治疗参考",
  medication_review: "用药核对", patient_edu: "患者健康教育", referral: "转诊摘要",
  followup: "随访记录", summary: "病例摘要", report_export: "报告导出",
  case_compare: "病例对比", image_analyze: "图片分析"
};

/* v68：工具执行记录折叠块（统一协议元数据可读化）——appendDocCard / appendAgentReply 共用。 */
function toolTraceEl(tools) {
  var det = document.createElement("details");
  det.className = "conv-tool-trace";
  var sm = document.createElement("summary");
  sm.textContent = t("🔧 本次使用的工具（" + tools.length + "）",
    "🔧 Tools used (" + tools.length + ")");
  det.appendChild(sm);
  tools.forEach(function (tool) {
    var line = document.createElement("div");
    line.className = "conv-meta";
    if (tool && typeof tool === "object" && tool.label) {
      // v68：统一协议元数据可读化（风险/确认状态/产出摘要/来源）
      var bits = ["· " + tool.label];
      if (tool.risk_level && tool.risk_level !== "low") bits.push("风险:" + tool.risk_level);
      if (tool.requires_confirmation) bits.push(tool.confirmed ? "已确认" : "未确认");
      if (tool.goal) bits.push(String(tool.goal).slice(0, 30));
      if (tool.output_digest) bits.push("→ " + String(tool.output_digest).slice(0, 50));
      if (tool.sources && tool.sources.length) bits.push("引用:" + tool.sources.join(","));
      if (tool.case_version) bits.push(tool.case_version);
      line.textContent = bits.join(" · ");
    } else {
      line.textContent = "· " + (typeof tool === "string" ? tool : JSON.stringify(tool));
    }
    det.appendChild(line);
  });
  return det;
}

function appendDocCard(d) {
  var log = convLogEl();
  if (!log) return;
  var llm = d.llm || {};
  var doc = d.doc || null;
  var task = d.task_type || (doc && doc.task_type) || "case_write";
  // v67：source=agent（本地指令响应，零 LLM）不改变全局 LLM 连接状态——误标 llm_unavailable 会挂错横幅。
  APP_STATE.llm = llm.source === "llm" ? "llm_connected" : (llm.source === "demo" ? "offline_demo"
    : (llm.source === "template" || llm.source === "agent" ? APP_STATE.llm : "llm_unavailable"));
  var card = document.createElement("div");
  card.className = "conv-card";
  var body = document.createElement("div");
  body.className = "conv-result";
  var banner = document.createElement("div");
  if (llm.source === "llm") {
    banner.className = "conv-banner llm-ok";
    banner.textContent = t("✅ 真实 AI 生成（LLM 请求成功并通过校验）",
      "✅ Real AI output (LLM succeeded and passed validation)");
  } else if (llm.source === "template") {
    banner.className = "conv-banner template";
    banner.textContent = t("📄 本地模板整理（非 AI 分析；只重组已提供信息，缺失标注「未提供」）",
      "📄 Local template (not AI; only reorganizes provided info; missing → not provided)");
  } else if (llm.source === "demo") {
    banner.className = "conv-banner demo";
    banner.textContent = t("⚠️ 离线演示数据，不是真实 AI 分析，不可用于医疗判断",
      "⚠️ Offline demo data — NOT a real AI analysis; not for medical judgment");
  } else if (llm.source === "agent") {
    // v67：Agent 本地指令响应（跳过3D/撤销/保存等）——如实标注非 AI 生成，不得误报「未连接」。
    banner.className = "conv-banner template";
    banner.textContent = t("✦ Agent 指令响应（本地执行，未调用 AI，不含医疗建议）",
      "✦ Agent command response (local, no AI involved; no medical advice)");
  } else {
    banner.className = "conv-banner llm-bad";
    banner.textContent = t("当前未连接 LLM，无法生成真实 AI 医疗建议",
      "LLM not connected; real AI medical advice unavailable");
  }
  body.appendChild(banner);
  var h4 = document.createElement("h4");
  h4.textContent = "📝 " + (DOC_TASK_LABELS[task] || task);
  body.appendChild(h4);
  if (doc && doc.content) {
    var pre = document.createElement("pre");
    pre.className = "conv-doc-content";
    pre.textContent = doc.content;
    body.appendChild(pre);
    var cites = doc.citations || [];
    if (cites.length) {
      var cl = document.createElement("div");
      cl.className = "conv-meta";
      cl.textContent = t("引用：", "Citations: ") + cites.map(function (c) { return c.citation_id || c; }).join("、");
      body.appendChild(cl);
      var cacts = document.createElement("div");   // v69：结果留在对话区，详情用入口按钮进入
      cacts.className = "conv-actions";
      var cb = document.createElement("button");
      cb.textContent = t("📚 查看引用详情（" + cites.length + "）", "📚 View citations (" + cites.length + ")");
      cb.addEventListener("click", function () { window.navToSources(); });
      cacts.appendChild(cb);
      body.appendChild(cacts);
    }
  }
  var docTools = d.tool_history || [];
  if (docTools.length) {
    body.appendChild(toolTraceEl(docTools));   // v68：doc 产物同样展示工具执行记录
  }
  if (d.message) {
    var msg = document.createElement("div");
    msg.className = "conv-meta";
    msg.textContent = d.message;
    body.appendChild(msg);
  }
  var meta = document.createElement("div");
  meta.className = "conv-llm-meta";
  meta.textContent = convLlmMetaText(llm) + (doc && doc.disclaimer ? " · " + doc.disclaimer : "");
  // v65：Agent 产物直接进病例窗口（独立门诊病历单，不遮挡对话；数据与对话共享同一 case_id）
  var openCaseBtn = document.createElement("button");
  openCaseBtn.className = "conv-retry-btn";
  openCaseBtn.style.marginTop = "6px";
  openCaseBtn.textContent = t("📋 在病例窗口打开 / 更新门诊病历", "📋 Open in Case Record window");
  openCaseBtn.addEventListener("click", function () {
    if (typeof bridge !== "undefined" && bridge && bridge.openCaseWindow) bridge.openCaseWindow();
  });
  body.appendChild(openCaseBtn);
  card.appendChild(body);
  card.appendChild(meta);
  // v70：doc 卡「工作结果」按钮区（服务端派生清单；含导出/3D 等，按需打开）
  var dwa = renderWorkActions(d, { cardEl: card });
  if (dwa) card.appendChild(dwa);
  log.appendChild(card);
  log.scrollTop = log.scrollHeight;
  if (llm.source === "llm") setStatus(t((DOC_TASK_LABELS[task] || task) + "已生成（AI，待医生审核）", "Doc ready (AI, pending review)"), "ok");
  else if (llm.source === "template") setStatus(t((DOC_TASK_LABELS[task] || task) + "已生成（本地模板整理）", "Doc ready (local template)"), "");
  else if (llm.source === "demo") setStatus(t("离线演示结果（不可用于医疗判断）", "Offline demo result (not for medical use)"), "");
  else if (llm.source === "agent") setStatus(t("已执行您的指令（本地响应）", "Command executed (local response)"), "");
  else setStatus(t("当前未连接 LLM", "LLM not connected"), "warn");
}

window.__pushDoc = function (jsonStr) {
  try {
    appendDocCard(JSON.parse(jsonStr));
  } catch (e) {
    try { console.log("[derm] __pushDoc parse error", e); } catch (e2) {}
  }
};

/* —— v64：Agent 对话桥（main.py 线程化后经 chat_done 推送）——
   __pushThinking：发送即出现「正在思考」占位，结果/错误到达即移除，绝不静默等待。
   removeConvThinking：移除占位（结果/错误卡替换它）。
   __pushChatError：请求失败/超时错误卡——带 retry_payload 时给「重试」按钮原样重发；
     unavailable=true（未配置/离线）只说明无法回复，不给重试（须先在设置页配置连接）。
   __pushAgentReply：Agent 自然语言回复卡（闲聊回复/澄清清单）——来源如实标注，
     tool_history 折叠为「过程」消息，不当回复正文展示。 */

var CONV_THINKING_ID = "convThinkingRow";

/* 轻量 Markdown 渲染（离线可用，无外部依赖）：先整体转义再生成受限标签，
   支持 标题/列表/加粗/行内代码/引用/换行——足够医生端回复排版，且无注入面。 */
function renderAgentMd(raw) {
  var src = String(raw || "");
  var lines = esc(src).split(/\r?\n/);
  var html = [];
  var inUl = false;
  function closeUl() { if (inUl) { html.push("</ul>"); inUl = false; } }
  function inline(s) {
    return s
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  }
  lines.forEach(function (ln) {
    var line = ln.trim();
    if (/^[-*•]\s+/.test(line)) {
      if (!inUl) { html.push("<ul>"); inUl = true; }
      html.push("<li>" + inline(line.replace(/^[-*•]\s+/, "")) + "</li>");
      return;
    }
    closeUl();
    var h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { html.push("<strong class='md-h'>" + inline(h[2]) + "</strong>"); return; }
    if (/^&gt;\s?/.test(line)) { html.push("<div class='md-quote'>" + inline(line.replace(/^&gt;\s?/, "")) + "</div>"); return; }
    if (!line) { html.push("<div class='md-gap'></div>"); return; }
    html.push("<div class='md-p'>" + inline(line) + "</div>");
  });
  closeUl();
  return html.join("");
}

window.__pushThinking = function (kind) {
  try {
    removeConvThinking();
    var log = convLogEl();
    if (!log) return;
    var card = document.createElement("div");
    card.id = CONV_THINKING_ID;
    card.className = "conv-card conv-thinking";
    var who = document.createElement("div");
    who.className = "conv-agent-who";
    who.textContent = "✦ DermAgent";
    var body = document.createElement("div");
    body.className = "conv-thinking-body";
    body.textContent = kind === "check"
      ? t("正在连接 LLM…（检查连接后即开始生成）", "Connecting to LLM… (will generate once connected)")
      : t("正在思考…（LLM 生成中，可能需要数十秒）",
          "Thinking… (LLM generating; may take tens of seconds)");
    card.appendChild(who);
    card.appendChild(body);
    log.appendChild(card);
    log.scrollTop = log.scrollHeight;
  } catch (e) {
    try { console.log("[derm] __pushThinking error", e); } catch (e2) {}
  }
};

window.removeConvThinking = function () {
  try {
    var el = document.getElementById(CONV_THINKING_ID);
    if (el && el.parentNode) el.parentNode.removeChild(el);
  } catch (e) { /* 占位不存在属正常 */ }
};

window.__pushChatError = function (d) {
  try {
    removeConvThinking();
    var log = convLogEl();
    if (!log) return;
    d = d || {};
    var card = document.createElement("div");
    card.className = "conv-card conv-error";
    var banner = document.createElement("div");
    banner.className = "conv-banner " + (d.unavailable ? "template" : "llm-bad");
    banner.textContent = d.unavailable
      ? t("当前未连接 LLM，无法回复", "LLM not connected; cannot reply")
      : t("⚠️ 请求失败，本次未收到回复", "⚠️ Request failed; no reply received");
    card.appendChild(banner);
    if (d.message) {
      var msg = document.createElement("div");
      msg.className = "conv-error-msg";
      msg.textContent = String(d.message).slice(0, 400);
      card.appendChild(msg);
    }
    var retryPayload = d.retry_payload;
    if (retryPayload && !d.unavailable) {
      var actions = document.createElement("div");
      actions.className = "conv-actions";
      var btn = document.createElement("button");
      btn.className = "primary";
      btn.textContent = t("🔄 重试", "🔄 Retry");
      btn.addEventListener("click", function () {
        // 原样重发 payload（不重复追加用户消息卡；「正在思考」占位由 Qt 端推送）
        if (card.parentNode) card.parentNode.removeChild(card);
        setStatus(t("正在重新发送…", "Resending…"), "busy");
        try {
          if (bridge && bridge.onChatSubmitted) {
            bridge.onChatSubmitted(typeof retryPayload === "string"
              ? retryPayload : JSON.stringify(retryPayload));
            window.__setChatBusy(true);   // v74：重发后进入生成中状态（按钮变停止）
          } else {
            setStatus(t("重试失败：桌面桥不可用", "Retry failed: bridge unavailable"), "warn");
          }
        } catch (e) {
          setStatus(t("重试失败：", "Retry failed: ") + e, "warn");
        }
      });
      actions.appendChild(btn);
      card.appendChild(actions);
    } else if (!d.unavailable) {
      var hint = document.createElement("div");
      hint.className = "conv-meta";
      hint.textContent = t("可在设置页检查 LLM 连接后重试。", "Check the LLM connection in Settings, then retry.");
      card.appendChild(hint);
    }
    log.appendChild(card);
    log.scrollTop = log.scrollHeight;
    setStatus(d.unavailable ? t("当前未连接 LLM", "LLM not connected")
      : t("请求失败，可点击对话中的「重试」", "Request failed; retry in the conversation"), "warn");
  } catch (e) {
    try { console.log("[derm] __pushChatError error", e); } catch (e2) {}
  }
};

window.__pushAgentReply = function (d) {
  try {
    /* v73：服务端设置动作——Agent 改界面类设置（如语言）随回复执行，先于卡片渲染 */
    if (d && d.ui_action && d.ui_action.type === "set_language" && typeof setLanguage === "function") {
      try { setLanguage(String(d.ui_action.value || "zh"), true); } catch (eLang) {}
    }
    removeConvThinking();
    var log = convLogEl();
    if (!log) return;
    d = d || {};
    var llm = d.llm || {};
    var card = document.createElement("div");
    card.className = "conv-card conv-agent";
    var who = document.createElement("div");
    who.className = "conv-agent-who";
    who.textContent = "✦ DermAgent" + (llm.source === "llm" && llm.model ? " · " + llm.model : "");
    card.appendChild(who);
    var body = document.createElement("div");
    body.className = "conv-agent-reply";
    body.innerHTML = renderAgentMd(d.content || "");
    card.appendChild(body);
    // v68（计划 §三.3/§五.2）：图片分析特征勾选卡——医生勾选可见特征后提交，
    // 以「皮损特征：…」文本并入病例（来源=医生输入）；AI 不代看照片，零编造。
    var rdCard = d.reply_card || d.card || null;
    if (rdCard && rdCard.type === "image_analysis") {
      var iwrap = document.createElement("div");
      iwrap.className = "conv-image-card";
      var qh = rdCard.quality_hints || [];
      if (qh.length) {
        var qEl = document.createElement("div");
        qEl.className = "conv-meta";
        qEl.textContent = "🔍 质量提示：" + qh.join("；");
        iwrap.appendChild(qEl);
      }
      var grid = document.createElement("div");
      grid.className = "conv-feat-grid";
      (rdCard.features || []).forEach(function (f) {
        var lab = document.createElement("label");
        lab.className = "conv-feat";
        var cb = document.createElement("input");
        cb.type = "checkbox";
        cb.value = f;
        lab.appendChild(cb);
        lab.appendChild(document.createTextNode(f));
        grid.appendChild(lab);
      });
      iwrap.appendChild(grid);
      var iacts = document.createElement("div");
      iacts.className = "conv-actions";
      var isub = document.createElement("button");
      isub.className = "primary";
      isub.textContent = t("提交勾选特征", "Submit selected features");
      isub.addEventListener("click", function () {
        var picked = Array.from(grid.querySelectorAll("input:checked")).map(function (c) { return c.value; });
        var input = document.getElementById("chatInput");
        if (!input) return;
        input.value = picked.length
          ? t("皮损特征：" + picked.join("、"), "Lesion features: " + picked.join(", "))
          : t("皮损特征：未见上述特征", "Lesion features: none of the listed");
        sendChat();
      });
      iacts.appendChild(isub);
      iwrap.appendChild(iacts);
      var idis = document.createElement("div");
      idis.className = "conv-meta";
      idis.textContent = t("图片分析仅供辅助，不等于诊断；特征以医生勾选为准。",
        "Image analysis is assistive only, not a diagnosis; features are doctor-confirmed.");
      iwrap.appendChild(idis);
      card.appendChild(iwrap);
    }
    var meta = document.createElement("div");
    meta.className = "conv-llm-meta";
    if (llm.source === "llm") {
      meta.textContent = t("来源：真实 LLM 回复", "Source: real LLM reply")
        + (llm.model ? " · " + llm.model : "");
    } else if (d.workflow_state === "planning") {
      meta.textContent = t("来源：本地计划确认（非 AI 生成）", "Source: local plan confirmation (not AI)");
    } else {
      // generated_by=agent（本地澄清清单）——如实标注非 AI 生成，不冒充模型回复
      meta.textContent = t("来源：本地澄清清单（非 AI 生成）", "Source: local clarify checklist (not AI)");
    }
    card.appendChild(meta);
    if (d.workflow_state === "planning") {
      // v64-P2：计划确认卡——一键「确认执行」走正常对话发送（服务端识别确认短语后执行）
      var acts = document.createElement("div");
      acts.className = "conv-actions";
      var go = document.createElement("button");
      go.className = "primary";
      go.textContent = t("✅ 确认执行", "✅ Confirm & run");
      go.addEventListener("click", function () {
        // 走 sendChat 全路径：自动补用户卡「确认执行」+「正在思考」占位 + bridge 发送
        var input = document.getElementById("chatInput");
        if (input) {
          input.value = t("确认执行", "Confirm");
          sendChat();
        } else if (bridge && bridge.onChatSubmitted) {
          bridge.onChatSubmitted(JSON.stringify({text: "确认执行"}));
        }
      });
      acts.appendChild(go);
      card.appendChild(acts);
    }
    var tools = d.tool_history || [];
    if (tools.length) {
      card.appendChild(toolTraceEl(tools));
    }
    // v70：回复卡底部「工作结果」按钮区——只按服务端 available_actions 渲染，按需打开
    var wa = renderWorkActions(d, { cardEl: card });
    if (wa) card.appendChild(wa);
    log.appendChild(card);
    log.scrollTop = log.scrollHeight;
  } catch (e) {
    try { console.log("[derm] __pushAgentReply error", e); } catch (e2) {}
  }
};

/* v64-P1：重启恢复最近会话——服务端对话历史渲染进对话流（用户/Agent 卡同款样式）。
   恢复只标注「恢复的历史」，不伪装成本次会话生成；当前病例上下文由 Qt 端 resume 恢复。 */
window.__restoreConversation = function (msgs) {
  try {
    var log = convLogEl();
    if (!log) return;
    var list = Array.isArray(msgs) ? msgs : [];
    log.innerHTML = "";
    // v64-P5：恢复会话时序号接着历史继续——chatSeq 同步为历史 user 消息条数，
    // 恢复卡与后续新消息编号连续（新对话才从头计数）。
    var userCount = 0;
    list.forEach(function (m) { if (m && m.role === "user" && String(m.content || "").trim()) userCount += 1; });
    chatSeq = userCount;
    var seq = 0;
    list.forEach(function (m) {
      m = m || {};
      var content = String(m.content || "").trim();
      if (!content) return;
      if (m.role === "user") {
        seq += 1;
        var ucard = document.createElement("div");
        ucard.className = "conv-card";
        var umeta = document.createElement("div");
        umeta.className = "conv-meta";
        umeta.textContent = "#" + seq + (m.at ? " · " + String(m.at).replace("T", " ").slice(5, 16) : "");
        var ubody = document.createElement("div");
        ubody.className = "conv-user";
        ubody.textContent = content;
        ucard.appendChild(umeta);
        ucard.appendChild(ubody);
        log.appendChild(ucard);
      } else {
        var acard = document.createElement("div");
        acard.className = "conv-card conv-agent";
        var who = document.createElement("div");
        who.className = "conv-agent-who";
        who.textContent = "✦ DermAgent";
        var abody = document.createElement("div");
        abody.className = "conv-agent-reply";
        abody.innerHTML = renderAgentMd(content);
        var ameta = document.createElement("div");
        ameta.className = "conv-llm-meta";
        ameta.textContent = t("（恢复的历史消息）", "(restored history)");
        acard.appendChild(who);
        acard.appendChild(abody);
        acard.appendChild(ameta);
        // v70：历史卡各自渲染自己的「工作结果」按钮——点旧消息只打开旧消息的结果
        var hma = MSG_ARTIFACTS[String(m.message_id || "")];
        if (hma && ((hma.available_actions || []).length || (hma.result_summary || []).length)) {
          var hwa = renderWorkActions(hma, { caseId: m.case_id || "", messageId: m.message_id, cardEl: acard });
          if (hwa) acard.appendChild(hwa);
        }
        log.appendChild(acard);
      }
    });
    log.scrollTop = log.scrollHeight;
  } catch (e) {
    try { console.log("[derm] __restoreConversation error", e); } catch (e2) {}
  }
};

/* 对话区结果卡：LLM 守卫——只有 source=llm（真实请求成功+结构化校验通过）才显示分析方向；
   source=demo 一律加「离线演示数据，不可用于医疗判断」横幅；其他（无元信息）不显示医疗内容。 */
function appendConvResultCard(d) {
  var log = convLogEl();
  if (!log) return;
  var llm = d.llm || {};
  APP_STATE.llm = llm.source === "llm" ? "llm_connected" : (llm.source === "demo" ? "offline_demo" : "llm_unavailable");
  APP_STATE.demo = llm.source === "demo";
  APP_STATE.waitingReview = d.action === "review";
  var card = document.createElement("div");
  card.className = "conv-card";
  var body = document.createElement("div");
  body.className = "conv-result";
  var banner = document.createElement("div");
  if (llm.source === "demo") {
    banner.className = "conv-banner demo";
    banner.textContent = t("⚠️ 离线演示数据，不是真实 AI 分析，不可用于医疗判断",
      "⚠️ Offline demo data — NOT a real AI analysis; not for medical judgment");
  } else if (llm.source === "llm") {
    banner.className = "conv-banner llm-ok";
    banner.textContent = t("✅ 真实 AI 分析（LLM 请求成功并通过结构化校验）",
      "✅ Real AI analysis (LLM succeeded and passed structural validation)");
  } else {
    banner.className = "conv-banner llm-bad";
    banner.textContent = t("当前未连接 LLM，暂不能生成真实 AI 医疗建议",
      "LLM not connected; real AI medical advice unavailable");
  }
  body.appendChild(banner);
  if (llm.source === "llm" && d.draft) {
    var h4 = document.createElement("h4");
    h4.textContent = t("AI 分析方向（待医生审核）", "AI analysis directions (pending doctor review)");
    body.appendChild(h4);
    var conds = d.draft.suspected_conditions || [];
    if (conds.length) {
      conds.slice(0, 6).forEach(function (c, idx) {
        var line = document.createElement("div");
        line.className = "conv-meta";
        line.textContent = "• " + (c.rank || idx + 1) + "、" + (c.name || "—") + "（待排）" +
          (c.confidence && c.confidence !== "unknown" ? "｜辅助排序：" + c.confidence : "");
        body.appendChild(line);
      });
      var tr = d.draft.treatment_reference || [];
      var mr = d.draft.medication_recommendations || [];
      if (tr.length || mr.length) {
        var summary = document.createElement("div"); summary.className = "conv-meta";
        summary.textContent = "治疗方向 " + tr.length + " 条 · 用药参考 " + mr.length + " 条（待医生核对，非处方）";
        body.appendChild(summary);
      }
    } else {
      var none = document.createElement("div");
      none.className = "conv-meta";
      none.textContent = t("模型未给出明确方向，详见辅助判断面板。", "No explicit direction; see assist panel.");
      body.appendChild(none);
    }
  } else if (llm.source === "demo" && d.draft) {
    var h4d = document.createElement("h4");
    h4d.textContent = t("演示草稿已生成（规则管线，非 AI）", "Demo draft generated (rule pipeline, not AI)");
    body.appendChild(h4d);
  }
  var revTools = d.tool_history || [];
  if (revTools.length) {
    body.appendChild(toolTraceEl(revTools));   // v68：评估结果卡展示工具执行记录
  }
  var actions = document.createElement("div");
  actions.className = "conv-actions";
  function mkBtn(text, primary, fn) {
    var b = document.createElement("button");
    if (primary) b.className = "primary";
    b.textContent = text;
    b.addEventListener("click", fn);
    return b;
  }
  if (d.action === "review" && d.draft) {
    actions.appendChild(mkBtn(t("🧠 展开辅助判断 / 医生审核", "Open assist / review"), true, function () {
      var ad = document.getElementById("assistDrawer");
      if (ad) { ad.classList.add("open"); renderAssistPanel(); }
    }));
  }
  if (d.action === "report" && d.report) {
    actions.appendChild(mkBtn(t("📄 查看报告（原生窗口）", "View report (native window)"), false, function () {
      setStatus(t("报告已在原生窗口「分析结果」页生成，可导出 Markdown。", "Report ready in the native window; export as Markdown."), "ok");
    }));
  }
  var convCites = (d.doc && d.doc.citations) || d.citations || (d.report && d.report.citations) || [];
  if (convCites.length) {   // v69：引用入口常驻结果卡（不强制跳离对话）
    actions.appendChild(mkBtn(t("📚 查看引用详情（" + convCites.length + "）",
      "📚 View citations (" + convCites.length + ")"), false, function () { window.navToSources(); }));
  }
  if (actions.children.length) body.appendChild(actions);
  var meta = document.createElement("div");
  meta.className = "conv-llm-meta";
  meta.textContent = convLlmMetaText(llm) + (d.workflow_state ? " · workflow_state=" + d.workflow_state : "");
  card.appendChild(body);
  card.appendChild(meta);
  // v70：结果卡「工作结果」按钮区（服务端派生清单，按需打开，绝不自动跳转）
  var rwa = renderWorkActions(d, { cardEl: card });
  if (rwa) card.appendChild(rwa);
  log.appendChild(card);
  log.scrollTop = log.scrollHeight;
  if (llm.source === "llm") setStatus(t("等待医生审核", "Waiting for doctor review"), "ok");
  else if (llm.source === "demo") setStatus(t("离线演示结果（不可用于医疗判断）", "Offline demo result (not for medical use)"), "");
  else setStatus(t("当前未连接 LLM", "LLM not connected"), "warn");
}

/* LLM 不可用提示卡（发送前探测失败时）：三入口，不生成任何医疗内容 */
function appendLlmUnavailableCard(probe) {
  removeConvThinking();   // v64：探测期占位在此收尾，换错误卡
  var log = convLogEl();
  if (!log) return;
  APP_STATE.llm = "llm_unavailable";
  var card = document.createElement("div");
  card.className = "conv-card";
  var body = document.createElement("div");
  body.className = "conv-result";
  var banner = document.createElement("div");
  banner.className = "conv-banner llm-bad";
  banner.textContent = t("当前未连接 LLM，暂不能生成真实 AI 医疗建议",
    "LLM not connected; real AI medical advice unavailable");
  body.appendChild(banner);
  var why = document.createElement("div");
  why.className = "conv-meta";
  why.textContent = (probe && probe.error) ? t("原因：", "Reason: ") + probe.error
    : t("原因：LLM 未配置或服务不可达", "LLM not configured or unreachable");
  body.appendChild(why);
  var actions = document.createElement("div");
  actions.className = "conv-actions";
  function mkBtn(text, fn) {
    var b = document.createElement("button");
    b.textContent = text;
    b.addEventListener("click", fn);
    return b;
  }
  actions.appendChild(mkBtn(t("🔄 检查连接", "Check connection"), function () {
    setStatus(t("正在检查 LLM 连接…", "Checking LLM connection…"), "busy");
    probeLlm(function (p) {
      if (p.available) {
        APP_STATE.llm = "llm_connected";
        setStatus(t("LLM 已连接（", "LLM connected (") + (p.model || t("模型未知", "unknown model")) + "）", "ok");
      } else {
        setStatus(t("当前未连接 LLM", "LLM not connected"), "warn");
        appendLlmUnavailableCard(p);
      }
    });
  }));
  actions.appendChild(mkBtn(t("⚙ 打开模型设置", "Open model settings"), function () {
    if (typeof openSettingsPane === "function") openSettingsPane("llm");
    else { var se = document.getElementById("settingsEntry"); if (se) se.click(); }
  }));
  actions.appendChild(mkBtn(t("▶ 切换离线演示", "Switch to offline demo"), function () {
    var inp = document.getElementById("chatInput");
    if (inp) { inp.value = t("演示：湿疹样皮疹", "Demo: eczema-like rash"); inp.focus(); }
    setStatus(t("已填入离线演示病例——发送后结果将明确标注为演示数据", "Demo case filled — result will be clearly marked as demo"), "");
  }));
  body.appendChild(actions);
  var meta = document.createElement("div");
  meta.className = "conv-llm-meta";
  meta.textContent = t("离线演示只用明确标注的演示数据，不会伪装成真实 AI 分析；恢复连接后即可生成真实 AI 建议。",
    "Offline demo uses clearly marked demo data only; real AI advice returns once LLM is connected.");
  card.appendChild(body);
  card.appendChild(meta);
  log.appendChild(card);
  log.scrollTop = log.scrollHeight;
}

/* —— 左栏导航（新对话 / 历史病例 / 项目 / 最近）—— */
function setNavActive(id) {
  document.querySelectorAll("#toolbar [data-nav]").forEach(function (b) {   // v63：底部图标入口（📁）也参与高亮迁移
    b.classList.toggle("active", b.id === id);
  });
}

/* 新对话：清空当前输入 / 3D 选点 / 当前分析结果，回 3D 主区；绝不删除历史数据 */
function navNewSession() {
  var inp = document.getElementById("chatInput");
  if (inp) inp.value = "";
  CHAT_FILES = [];
  committedRegionKeys = {};
  selectedRegions = [];
  renderChips();
  // v69：新对话 = 新病例上下文；清空导航态与持久化记录，绝不把上一条对话的病例带进来
  NAV.caseId = ""; NAV.sessionId = ""; NAV.navState = "idle";
  NAV.lastSeq = 0; NAV.lastKey = ""; NAV.lastAt = 0; NAV.pendingWorkspace = "";
  NAV.pendingScrollCase = false; NAV.pendingHighlight = [];
  navUpdateChrome(); navPersist();
  renderSelection();
  updateMiniTag();
  var log = convLogEl();
  if (log) log.innerHTML = "";
  chatSeq = 0;   // v64-P5：消息序号随新对话从头计数（此前跨会话累计，新对话仍显示 #7）
  recentSessionToken += 1;   // v64-P7：换会话 token——此前的对话条目固化进「最近」
  ASSIST_SNAPSHOT = null;
  renderAssistSnapshot();
  FLOW = null;
  APP_STATE.llm = "unknown";
  APP_STATE.demo = false;
  APP_STATE.waitingReview = false;
  var ad = document.getElementById("assistDrawer");
  if (ad) ad.classList.remove("open");
  var sd = document.getElementById("selDrawer");
  if (sd) sd.classList.remove("open");
  closeNavPane();
  // v59：新对话默认进入 Agent 对话主区；同步重置 Qt 端病例句柄（下一条消息开新病例）
  if (bridge && bridge.onNewSession) bridge.onNewSession();
  setLayout("chat_primary");
  setNavActive("navNew");
  setStatus(t("等待医生输入", "Waiting for doctor input"), "");
}

/* 最近打开记录（localStorage；仅本机界面记录，不造假）。v61：case 类条目带 case_id（侧栏「最近」可点击续办） */
function recordRecent(kind, summary, caseId) {
  try {
    var list = JSON.parse(localStorage.getItem("derm_recent") || "[]");
    var entry = { kind: kind, summary: String(summary || "").slice(0, 80),
                  time: new Date().toLocaleString("zh-CN", { hour12: false }),
                  case_id: String(caseId || "") };
    // v64-P7：会话级条目——同一会话（token 相同）只保留一条「最近」，摘要随最新消息更新；
    // 点「新对话」后 token 变化 → 旧会话条目固化在「最近」，新会话另起一条。
    if (kind === "session" && list.length && list[0].kind === "session" &&
        list[0].token === recentSessionToken) {
      entry.token = recentSessionToken;
      list[0] = entry;
    } else {
      if (kind === "session") entry.token = recentSessionToken;
      list.unshift(entry);
    }
    localStorage.setItem("derm_recent", JSON.stringify(list.slice(0, 20)));
    renderRailRecent();   // v64-P7：写后即时刷新侧栏（此前只在初始化渲染，新对话后看不到旧会话）
  } catch (e) {}
}

/* —— v61 分组常驻侧栏：项目 / 历史病例 / 最近 三组条目直列（前 5 条），全量列表走「查看全部」浮层面板 —— */
var RAIL_PREVIEW_N = 5;
var railHistSeq = 0;

function navPaneShowing(titleZh, titleEn) {
  var np = document.getElementById("navPane");
  if (!np || !np.classList.contains("open")) return false;
  var tt = (document.getElementById("navPaneTitle") || {}).textContent || "";
  return tt.indexOf(titleZh) >= 0 || tt.indexOf(titleEn) >= 0;
}

/* 侧栏「历史病例」条目行：点击续办；行尾 🗑 删除（confirm 后服务端硬删，侧栏同步刷新） */
function railCaseRow(c) {
  var row = document.createElement("button");
  row.className = "rail-item rail-entry";
  row.title = t("打开该病例（续办）", "Resume this case");
  var main = document.createElement("span");
  main.className = "entry-main";
  var title = String(c.initial_text || c.label || c.case_id || "").replace(/\s+/g, " ").trim();
  if (title.length > 42) title = title.slice(0, 42) + "…";
  main.textContent = "🗂 " + (title || "新病例");
  row.appendChild(main);
  var tm = document.createElement("span");          // v63：条目右侧时间小字（今天显 HH:MM，更早显 M/D）
  tm.className = "entry-time";
  var d = new Date(String(c.updated_at || "").replace(" ", "T"));
  if (!isNaN(d)) {
    var now = new Date();
    tm.textContent = (d.toDateString() === now.toDateString())
      ? String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0")
      : (d.getMonth() + 1) + "/" + d.getDate();
  }
  row.appendChild(tm);
  var del = document.createElement("button");
  del.className = "nav-row-del";
  del.textContent = "🗑";
  del.title = t("删除该病例（含会话与草稿，不可恢复）", "Delete this case (conversation and draft included; irreversible)");
  del.addEventListener("click", function (ev) {
    ev.stopPropagation();
    if (!window.confirm(t("删除病例「" + (c.label || c.case_id) + "」？\n病例会话与待审草稿一并删除，不可恢复。",
                          "Delete case \"" + (c.label || c.case_id) + "\"?\nIts conversation and draft will be removed permanently."))) return;
    if (!bridge || !bridge.caseDelete) { alert(t("未连接桌面端服务，无法删除。", "Desktop bridge unavailable.")); return; }
    bridge.caseDelete(c.case_id || "", function (s) {
      var r = null;
      try { r = JSON.parse(s); } catch (e) {}
      if (r && r.ok) {
        renderRailHist();
      } else {
        alert(t("删除失败：", "Delete failed: ") + ((r && r.error) || ""));
      }
    });
  });
  row.appendChild(del);
  row.addEventListener("click", function () {
    recordRecent("case", "续办病例 " + (c.case_id || ""), c.case_id);
    renderRailRecent();
    if (bridge && bridge.caseResume) bridge.caseResume(c.case_id || "", function () {});
  });
  return row;
}

/* v63：时间分组名（今天/昨天/7 天内/更早）——服务端列表已按 updated_at 降序 */
function timeGroupOf(iso) {
  var d = new Date(String(iso || "").replace(" ", "T"));
  if (isNaN(d)) return t("更早", "Earlier");
  var now = new Date();
  var today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  var that = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  var diff = Math.round((today - that) / 86400000);
  if (diff <= 0) return t("最近", "Recent");
  if (diff === 1) return t("昨天", "Yesterday");
  if (diff <= 7) return t("7 天内", "Previous 7 days");
  return t("更早", "Earlier");
}

/* v63：Codex 式历史流——服务端病例全量按时间分组常驻滚动；流底部「删除全部病例」小字（空列表不渲染） */
function renderRailHist() {
  var box = document.getElementById("railHistEntries");
  if (!box) return;
  var seq = ++railHistSeq;
  box.innerHTML = "";
  if (!bridge || !bridge.casesList) {
    var d0 = document.createElement("div");
    d0.className = "rail-empty";
    d0.textContent = t("未连接桌面端服务", "Desktop bridge unavailable");
    box.appendChild(d0);
    return;
  }
  bridge.casesList(function (s) {
    if (seq !== railHistSeq) return;   // 过期响应丢弃（删除/续办后可能已发起新渲染）
    var r = null;
    try { r = JSON.parse(s); } catch (e) {}
    var list = (r && r.cases) || [];
    if (!list.length) {
      var d = document.createElement("div");
      d.className = "rail-empty";
      d.textContent = t("暂无历史病例", "No saved cases yet");
      box.appendChild(d);
      return;
    }
    var shown = list.slice(0, 200);
    var groups = {}, order = [];
    shown.forEach(function (c) {
      var g = timeGroupOf(c.updated_at);
      if (!groups[g]) { groups[g] = []; order.push(g); }
      groups[g].push(c);
    });
    order.forEach(function (g) {
      var h = document.createElement("div");
      h.className = "rail-time-head";
      h.textContent = g;
      box.appendChild(h);
      groups[g].forEach(function (c) { box.appendChild(railCaseRow(c)); });
    });
    if (list.length > shown.length) {
      var more = document.createElement("div");
      more.className = "rail-empty";
      more.textContent = t("仅显示最近 200 条", "Showing the latest 200 only");
      box.appendChild(more);
    }
    /* v63：历史流底部「删除全部病例」——confirm 后服务端批量硬删，流与最近分组同步刷新 */
    var delAll = document.createElement("button");
    delAll.className = "rail-del-all";
    delAll.textContent = t("🗑 删除全部对话", "🗑 Delete all conversations");
    delAll.title = t("删除全部历史对话（含会话与草稿，不可恢复）", "Delete ALL saved conversations (drafts included; irreversible)");
    delAll.addEventListener("click", function () {
      if (!window.confirm(t("删除全部 " + list.length + " 条历史对话？\n所有会话与待审草稿一并删除，不可恢复。",
                            "Delete all " + list.length + " conversations?\nAll conversations and drafts will be removed permanently."))) return;
      if (!bridge || !bridge.caseDeleteAll) { alert(t("未连接桌面端服务，无法删除。", "Desktop bridge unavailable.")); return; }
      bridge.caseDeleteAll(function (s) {
        var r2 = null;
        try { r2 = JSON.parse(s); } catch (e) {}
        if (r2 && r2.ok) { renderRailHist(); }
        else { alert(t("删除失败：", "Delete failed: ") + ((r2 && r2.error) || "")); }
      });
    });
    box.appendChild(delAll);
  });
}

function renderRailRecent() {
  var box = document.getElementById("railRecentEntries");
  if (!box) return;
  box.innerHTML = "";
  var list = [];
  try { list = JSON.parse(localStorage.getItem("derm_recent") || "[]"); } catch (e) {}
  if (!list.length) {
    var d = document.createElement("div");
    d.className = "rail-empty";
    d.textContent = t("暂无最近记录", "No recent items yet");
    box.appendChild(d);
    return;
  }
  list.slice(0, RAIL_PREVIEW_N).forEach(function (it, idx) {
    var row = document.createElement("button");
    row.className = "rail-item rail-entry";
    var main = document.createElement("span");
    main.className = "entry-main";
    main.textContent = (it.kind === "case" ? "🗂 " : "💬 ") + (it.summary || "");
    row.appendChild(main);
    var del = document.createElement("button");
    del.className = "nav-row-del";
    del.textContent = "✕";
    del.title = t("删除该条最近记录（不影响病例数据）", "Remove this recent entry (case data is not affected)");
    del.addEventListener("click", function (ev) {
      ev.stopPropagation();
      var cur = [];
      try { cur = JSON.parse(localStorage.getItem("derm_recent") || "[]"); } catch (e) {}
      cur.splice(idx, 1);
      localStorage.setItem("derm_recent", JSON.stringify(cur.slice(0, 20)));
      renderRailRecent();
      if (navPaneShowing("最近", "Recent")) showRecentPane();
    });
    row.appendChild(del);
    if (it.kind === "case" && it.case_id) {
      row.title = t("打开该病例（续办）", "Resume this case");
      row.addEventListener("click", function () {
        if (bridge && bridge.caseResume) bridge.caseResume(it.case_id, function () {});
      });
    } else {
      row.title = it.summary || "";
    }
    box.appendChild(row);
  });
}

/* v73 病历库：全部已产生病历（对话/诊断/手动填写）；点击续办该病例并打开门诊病历面板 */
function showRecordLibPane() {
  setNavActive("");
  showNavPane(t("📚 历史病历", "📚 Medical Records"), function (body) {
    var card = document.createElement("div");
    card.className = "nav-card";
    if (!bridge || !bridge.recordList) {
      var nb = document.createElement("div");
      nb.className = "empty";
      nb.textContent = t("未连接桌面端服务，无法读取病历库。", "Desktop bridge unavailable.");
      card.appendChild(nb);
      body.appendChild(card);
      return;
    }
    bridge.recordList(function (s) {
      var r = null;
      try { r = JSON.parse(s); } catch (e) {}
      var list = (r && r.records) || [];
      if (!list.length) {
        var d = document.createElement("div");
        d.className = "empty";
        d.textContent = t("暂无病历——对话、诊断或手动填写病历单保存后自动入庫。",
                          "No records yet — saved automatically after chat, diagnosis, or form entry.");
        card.appendChild(d);
        body.appendChild(card);
        return;
      }
      list.forEach(function (rec) {
        var row = document.createElement("div");
        row.className = "nav-row";
        var col = document.createElement("div");
        col.className = "row-main";
        col.style.display = "flex";
        col.style.flexDirection = "column";
        col.style.gap = "2px";
        var main = document.createElement("div");
        var d = new Date(String(rec.updated_at || "").replace(" ", "T"));
        var day = isNaN(d) ? "" : ((d.getMonth() + 1) + "/" + d.getDate() + " " +
          String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"));
        main.style.cssText = "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
        main.textContent = "📄 " + (rec.title || "新病例") + "　· " + (day || "") +
          "　· " + (rec.review_status === "confirmed" ? t("已确认", "confirmed")
            : (!rec.filled_count ? t("待填写", "to fill") : t("待审核", "pending")));
        col.appendChild(main);
        if (rec.summary) {   /* v75：第二行 = 病历内容关键信息（患者·主诉/现病史·初步判断） */
          var subEl = document.createElement("div");
          subEl.style.cssText = "color:var(--muted);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
          subEl.textContent = rec.summary;
          col.appendChild(subEl);
        }
        row.title = t("打开该病历（续办病例并显示门诊病历单）", "Open this record (resume case)");
        /* v75-fix：删除按钮——历史病历此前只能看不能管 */
        var del = document.createElement("button");
        del.className = "nav-row-del";
        del.textContent = t("删除", "Delete");
        del.title = t("删除该病历（服务端硬删，不可恢复）", "Delete this record (hard delete)");
        del.addEventListener("click", function (ev) {
          ev.stopPropagation();
          if (!bridge || !bridge.caseDelete) return;
          if (!confirm(t("确定删除该病历？删除后不可恢复。", "Delete this record? This cannot be undone."))) return;
          bridge.caseDelete(rec.case_id || "", function () { showRecordLibPane(); });
        });
        row.appendChild(del);
        row.addEventListener("click", function () {
          closeNavPane();
          navSetWorkspace("case");   // 续办后直接显示门诊病历单面板
          if (bridge && bridge.caseResume) {
            bridge.caseResume(rec.case_id || "", function () {
              /* v75-fix：resume 是异步的——面板先开时拉到的还是旧病例快照（「点新病历显示老病历」的根因），
                 续办完成后再推一次最新 record，面板必定显示被续办的那份 */
              if (bridge && bridge.casePanelToggle) bridge.casePanelToggle("open");
            });
          }
        });
        row.appendChild(col);   // v75-fix：漏了这行——行被创建但文字从未挂上，历史病历显示为空白虚线
        card.appendChild(row);
      });
      body.appendChild(card);
    });
  });
}

function closeNavPane() {
  var np = document.getElementById("navPane");
  if (np) np.classList.remove("open");
}

function showNavPane(title, builder) {
  var np = document.getElementById("navPane");
  var body = document.getElementById("navPaneBody");
  var tEl = document.getElementById("navPaneTitle");
  if (!np || !body) return;
  tEl.textContent = title;
  body.innerHTML = "";
  builder(body);
  np.classList.add("open");
  // v59：导航面板为主区全覆盖 overlay，对话/模型两态下都直接浮层展示，不再切换布局
}


/* 「项目」：项目信息 + 知识库/配置入口 */
function showProjectPane() {
  setNavActive("navProject");
  showNavPane(t("项目", "Project"), function (body) {
    var card = document.createElement("div");
    card.className = "nav-card";
    var h = document.createElement("h4");
    h.textContent = "肤智 DermAgent";
    card.appendChild(h);
    var p = document.createElement("div");
    p.className = "conv-meta";
    p.textContent = t("皮肤科医生端 3D 人体部位标注与 AI 辅助分析工作台：3D 选点 → 结构化病例 → 服务端 LLM/RAG 分析 → 医生审核 → 报告导出。所有 AI 结果默认待医生确认，不做自动诊断。",
      "Dermatologist workbench: 3D region picking → structured case → server LLM/RAG analysis → doctor review → report export. AI results are always pending doctor confirmation.");
    card.appendChild(p);
    body.appendChild(card);
    var acts = document.createElement("div");
    acts.className = "nav-card";
    var h2 = document.createElement("h4");
    h2.textContent = t("入口", "Entries");
    acts.appendChild(h2);
    function row(text, fn) {
      var r = document.createElement("div");
      r.className = "nav-row";
      var b = document.createElement("button");
      b.textContent = text;
      b.addEventListener("click", fn);
      r.appendChild(b);
      acts.appendChild(r);
    }
    row(t("📚 知识库（书籍库 / 向量模型）", "📚 Knowledge base"), function () {
      if (typeof openSettingsPane === "function") openSettingsPane("kb");
    });
    row(t("⚙ LLM 与模型 / 接入方式设置", "⚙ LLM & connection settings"), function () {
      if (typeof openSettingsPane === "function") openSettingsPane("llm");
    });
    row(t("🧠 医生辅助判断面板", "🧠 Assist panel"), function () {
      var ad = document.getElementById("assistDrawer");
      if (ad) { ad.classList.add("open"); renderAssistPanel(); }
    });
    body.appendChild(acts);
  });
}

/* 「最近」：最近打开的病例 / 报告 / 会话（localStorage 记录）；v60 每条可删、可一键清空 */
function showRecentPane() {
  setNavActive("navRecent");
  showNavPane(t("最近", "Recent"), function (body) {
    var card = document.createElement("div");
    card.className = "nav-card";
    var list = [];
    try { list = JSON.parse(localStorage.getItem("derm_recent") || "[]"); } catch (e) {}
    if (!list.length) {
      var d = document.createElement("div");
      d.className = "empty";
      d.textContent = t("暂无最近记录（打开病例 / 提交会话后显示）。", "No recent items yet.");
      card.appendChild(d);
    }
    function persist(next) {
      localStorage.setItem("derm_recent", JSON.stringify(next.slice(0, 20)));
      renderRailRecent();        // v61：侧栏「最近」分组同步
      showRecentPane();          // 重渲染本 pane
    }
    list.forEach(function (it, idx) {
      var row = document.createElement("div");
      row.className = "nav-row";
      var main = document.createElement("div");
      main.className = "row-main";
      main.textContent = (it.kind === "case" ? "🗂 " : "💬 ") + (it.summary || "");
      var time = document.createElement("div");
      time.className = "row-time";
      time.textContent = it.time || "";
      /* v60：删除单条最近记录（🗂 病例条目 / 💬 对话条目同样可删；只清本机记录，不动服务端病例） */
      var del = document.createElement("button");
      del.className = "nav-row-del";
      del.textContent = "✕";
      del.title = t("删除该条最近记录（不影响病例数据）", "Remove this recent entry (case data is not affected)");
      del.addEventListener("click", function () {
        var next = list.filter(function (_it, i) { return i !== idx; });
        persist(next);
      });
      row.appendChild(main); row.appendChild(time); row.appendChild(del);
      card.appendChild(row);
    });
    body.appendChild(card);
    if (list.length) {
      var clearCard = document.createElement("div");
      clearCard.className = "nav-card";
      var clear = document.createElement("button");
      clear.textContent = t("🗑 清空最近记录", "🗑 Clear recent entries");
      clear.title = t("清空本机最近记录（服务端病例数据不受影响）", "Clear local recent entries (server cases are not affected)");
      clear.addEventListener("click", function () {
        if (!window.confirm(t("清空全部最近记录？服务端已保存的病例不受影响。", "Clear all recent entries? Saved cases on the server are not affected."))) return;
        persist([]);
      });
      clearCard.appendChild(clear);
      body.appendChild(clearCard);
    }
  });
}

function initNavUI() {
  var bind = function (id, fn) {
    var el = document.getElementById(id);
    if (el) el.addEventListener("click", fn);
  };
  bind("navNew", navNewSession);
  bind("navProject", showProjectPane);
  bind("openRecordLib", showRecordLibPane);   // v73 病历库
  bind("openHistDrawer", showRecordLibPane);  // v73：历史病历 = 病历库（同一视图）
  bind("navRecent", showRecentPane);     // v61：「查看全部」→ 最近全量浮层面板
  bind("navPaneClose", function () { closeNavPane(); setNavActive("navNew"); });
  renderRailHist();                      // v61：分组常驻条目首渲染（bridge 异步填充）
  renderRailRecent();
  /* v59：composer 行「人体模型」按钮 → 切入 3D 全屏模式（选点后「完成并返回对话」带回上下文） */
  bind("btnModelMode", function () { navManual("3d", "btn_model_mode"); });
  bind("btnBackToChat", function () { navLeave3d(); });
}

function flowGuidanceText() {
  var a = FLOW ? FLOW.action : "";
  if (a === "ask_more" || a === "review") {
    return String((FLOW && FLOW.question) || "").replace(/\*\*/g, "").trim() ||
      t("服务端正在处理，请稍候。", "The server is processing; please wait.");
  }
  if (a === "report") {
    return t("✅ 待签署草稿已生成——在原生窗口「分析结果 / 医生审核」点「导出报告」可导出 Markdown（最终签署须在授权目标系统完成）。",
      "✅ Draft approved — export the Markdown report from the native window (final signing happens in the authorized system).");
  }
  if (a === "closed") {
    return String((FLOW && FLOW.message) || "").trim() ||
      t("本病例已按医生决定关闭，不生成报告。可以重新选部位开始新病例。",
        "This case was closed by the doctor's decision; no report is generated. Pick a region to start a new case.");
  }
  return t("当前在第 1 步：在 3D 模型上点击选择皮损部位（可多选，每次点击=独立记录），再在下方输入框描述症状后点 ↑ 发送；也可以只提交部位，系统会逐步引导你补全资料。",
    "Step 1: click the lesion regions on the 3D model (multiple clicks allowed), describe symptoms below and press ↑ to submit; you can also submit regions only — the system will guide you through the rest.");
}

function flowStateLine() {
  var a = FLOW ? FLOW.action : "";
  if (a === "closed") return t("当前状态：病例已关闭（医生拒绝，不生成报告）", "State: case closed (rejected; no report)");
  if (a === "ask_more") return t("当前状态：等待补充资料", "State: waiting for supplement");
  if (a === "review") return t("当前状态：等待医生审核", "State: waiting for doctor review");
  if (a === "report") return t("当前状态：报告已生成（待签署导出）", "State: report ready (pending export)");
  return t("当前状态：等待医生输入", "State: waiting for doctor input");
}

function renderFlowGuide() {
  var stateEl = document.getElementById("flowGuideState");
  if (!stateEl) return;
  var a = FLOW ? FLOW.action : "";
  var isAsk = a === "ask_more";
  var isReview = a === "review";
  stateEl.textContent = flowStateLine();
  document.getElementById("flowGuideText").textContent = flowGuidanceText();
  var missEl = document.getElementById("flowGuideMissing");
  var missing = (FLOW && FLOW.missing) || [];
  missEl.textContent = "";
  missEl.style.display = (isAsk && missing.length) ? "flex" : "none";
  missing.forEach(function (m) {
    var chip = document.createElement("span");
    chip.className = "miss-chip";
    chip.textContent = t("缺：", "Missing: ") + m;
    missEl.appendChild(chip);
  });
  var input = document.getElementById("flowGuideInput");
  var actions = document.getElementById("flowGuideActions");
  input.style.display = isAsk ? "block" : "none";
  input.placeholder = t("示例：主诉：胸部红斑伴瘙痒；病程：3天；诱因：无明显诱因；既往史：体健；过敏史：否认；当前用药：未使用（也可直接用大白话描述）",
    "Example: Chief complaint: red chest rash itchy; Duration: 3 days; Triggers: none; History: healthy; Allergies: none; Medications: none (plain words work too)");
  actions.style.display = isAsk ? "flex" : "none";
  document.getElementById("flowGuideOpenAssist").style.display = isReview ? "inline-block" : "none";
  document.getElementById("flowGuideHint").textContent = isAsk
    ? t("「字段： 值」只是示例格式、不是强制——直接用大白话写也可以（如「胸部红斑3天，微痒，无诱因，既往体健，无过敏，目前未用药」），系统会自动解析；没匹配上字段的内容会并入症状描述，不会丢。最多追问 2 轮，之后会带着现有资料继续分析。",
        "The field format is an example, not required — plain words work too; unparsed text is kept as symptom notes. At most 2 follow-up rounds, then analysis proceeds with what is available.")
    : t("流程条常显在输入框上方，随时点击可查看当前步骤与下一步操作。",
        "The flow bar above the input always shows the current step; click it anytime for guidance.");
}

function openFlowGuide() {
  renderFlowGuide();
  var modal = document.getElementById("flowGuide");
  if (modal) modal.classList.add("open");
  var inp = document.getElementById("flowGuideInput");
  if (inp && inp.style.display !== "none") inp.focus();
}

function closeFlowGuide() {
  var modal = document.getElementById("flowGuide");
  if (modal) modal.classList.remove("open");
}

function closeDrawerIds(ids) {
  ids.forEach(function (id) {
    var d = document.getElementById(id);
    if (d) d.classList.remove("open");
  });
}

/* Qt 桥接入口：main.py _push_flow_to_web → JSON 字符串（v58：状态行 + 弹窗，不再有步骤条） */
window.__pushFlow = function (jsonStr) {
  try {
    var d = JSON.parse(jsonStr);
    if (!d || typeof d !== "object") return "rejected";
    FLOW = d;
    if (d.mode) { /* agent_mode 透传（mock=演示语义，结果卡由 __pushAssist 标注） */ }
    if (d.llm && d.llm.source) APP_STATE.llm = d.llm.source === "llm" ? "llm_connected"
      : (d.llm.source === "demo" ? "offline_demo" : "llm_unavailable");
    if (d.action === "ask_more") {
      // v64-P3：Agent 化后缺信息由对话流澄清卡承载（_route_clarify），不再弹固定流程指引窗
      setStatus(t("Agent 已在对话中列出需要的信息，请直接回复", "Agent listed the needed info in the conversation; reply directly"), "");
    } else if (d.action === "review") {
      closeFlowGuide();                      // 下一步=医生审核：自动展开辅助判断面板
      setStatus(t("等待医生审核", "Waiting for doctor review"), "ok");
      var ad = document.getElementById("assistDrawer");
      if (ad && !ad.classList.contains("open")) {
        ad.classList.add("open");
        renderAssistPanel();
      }
    } else if (d.action === "report") {
      setStatus(t("报告已生成，可在原生窗口导出", "Report ready; export in native window"), "ok");
    } else if (d.action === "closed") {
      setStatus(String(d.message || t("病例已关闭", "Case closed")).slice(0, 80), "warn");
    } else if (d.action === "llm_unavailable") {
      setStatus(t("当前未连接 LLM，暂不能生成真实 AI 医疗建议",
        "LLM not connected; real AI medical advice unavailable"), "warn");
    } else if (d.action === "doc") {
      // v59：文书/医学判断任务——结果卡由 __pushDoc 渲染，这里只清指引弹窗不抢状态行
      closeFlowGuide();
    } else {
      setStatus(flowStateLine(), "");        // 空/未知 action：复位到当前流状态行（默认「等待医生输入」）
    }
    return "ok";
  } catch (e) { return "parse_error"; }
};

/* 弹窗内补充发送：写入底部输入框后复用 sendChat 全链路（结构化载荷/医生资料/3D 选点不变） */
function sendFlowSupplement() {
  var inp = document.getElementById("flowGuideInput");
  var chat = document.getElementById("chatInput");
  var text = inp ? inp.value.trim() : "";
  if (!text) return;                          // 空内容不发送
  if (chat) chat.value = text;
  if (inp) inp.value = "";
  closeFlowGuide();
  sendChat();
}

function initFlowUI() {
  document.getElementById("flowGuideClose").addEventListener("click", closeFlowGuide);
  document.getElementById("flowGuideLater").addEventListener("click", closeFlowGuide);
  document.getElementById("flowGuideSend").addEventListener("click", sendFlowSupplement);
  var inp = document.getElementById("flowGuideInput");
  inp.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); sendFlowSupplement(); }
  });
  document.getElementById("flowGuideOpenAssist").addEventListener("click", function () {
    closeFlowGuide();
    var ad = document.getElementById("assistDrawer");
    if (ad) {
      ad.classList.add("open");
      var cd = document.getElementById("clinicDrawer");
      if (cd) cd.classList.remove("open");
      renderAssistPanel();
    }
  });
  document.getElementById("flowGuide").addEventListener("click", function (ev) {
    if (ev.target === this) closeFlowGuide();   // 点遮罩关闭
  });
}

initClinicUI();   // 阶段3：医生资料抽屉（＋菜单入口）
initAssistUI();   // 阶段4：医生辅助面板（资料选择/脱敏预览）
initSettingsUI(); // §16.1：设置面板（左下角齿轮，语言/LLM/知识库/RAG/版本与安全）
initFlowUI();     // v52：下一步指引弹窗（弹窗内可直接补充发送；v58 起不再有步骤条）
initNavUI();      // v58：左栏导航（新对话/历史病例/项目/最近）+ 双状态布局
refreshLlmStatus();   // v46：启动即初始化 composer 旁 LLM 模型选择（无 bridge → 离线禁用）
refreshAgentSettings();   // v54：启动即初始化设置页接入方式（无 bridge → 离线禁用）
loadLanguagePref();  // v43：加载语言偏好（bridge 共享偏好优先，回退 localStorage）

initScene();
initComposer();
initBridge();

/* ================= v69：统一导航层（对话驱动的工作区切换） =================
   根因：v68 之前前端没有「工作区」概念——APP_STATE 只有 layout（对话/3D 两态互斥），
   病例面板开合全靠零散的 __openCasePanel / cpClose 手动调用，服务端也只有 open_case
   一个布尔标志。于是「Agent 已经回复，但界面没有自动跳转到对应窗口」：只有「新建病例」
   一种情况会开面板，doc / 待审核 / 分析 / 引用结果全部停在原地。

   本层把导航收敛成唯一入口 window.__nav(decision)：
   - decision 由服务端 nav 模块给出（workflow_state/action/task_type → 动作 → 工作区），
     前端只执行不推断（离线/演示模式下用 NAV_FALLBACK 兜底）；
   - 八类事件：navigate_to_chat / navigate_to_case / navigate_to_3d / navigate_to_sources /
     navigate_to_analysis / focus_input / scroll_to_latest / highlight_case_fields；
   - 防闪烁：目标工作区与当前一致时不重复切换；同一决策窗口内重复到达直接跳过；
   - 过期守卫：seq 倒退（异步返回顺序错乱）或 case_id 与当前病例不符的病例类决策一律丢弃；
   - 导航日志：session_id / case_id / workflow_state / task_type / next_action / 工具调用 /
     触发事件 / 导航前工作区 / 导航后工作区 / 聚焦结果 / 滚动结果 / nav_state / 说明。

   v70 语义反转（对话优先 · 结果按需打开）：__nav 降级为「只声明、不执行」——服务端决策
   只写入 NAV.suggested 供日志与测试核对，前端绝不自动切换工作区/弹窗/高亮；所有工作窗口
   由医生点击回复卡「工作结果」按钮区的 window.openWork(action) 主动打开。 */
var NAV_WORKSPACE_LABELS = { chat: "对话工作区", "case": "病例工作区", "3d": "3D 人体模型" };
var NAV_WORKSPACE_ICONS = { chat: "💬", "case": "📋", "3d": "🧍" };
/* 兜底映射（服务端决策缺失时使用；权威映射以服务端 /api/nav/map 为准，
   两侧一致性由测试守卫）。null = 留在当前工作区。 */
var NAV_FALLBACK = {
  idle: null, awaiting_confirmation: "chat", collecting_information: "chat",
  awaiting_user_input: "chat", chat_reply: "chat", rag_searching: "chat",
  rag_completed: "chat", analysis_ready: "chat", case_draft: "case",
  awaiting_review: "case", doctor_modified: "case", confirmed: "case",
  rejected: "case", tool_3d_active: "3d", error: null
};
var NAV_STORE_KEY = "dermagent.nav.v1";
var NAV_DEDUPE_MS = 900;
var NAV = {
  workspace: "chat",        // 当前工作区（chat | case | 3d）
  caseId: "",               // 当前病例：所有导航与数据都绑定它
  sessionId: "",
  navState: "idle",
  lastSeq: 0,               // 已执行决策的序号（丢弃乱序到达的旧决策）
  lastKey: "", lastAt: 0,   // 去重窗口
  pendingWorkspace: "",     // 刷新恢复：待病例数据到达后再落地的工作区
  pendingScrollCase: false, // 病例面板待滚动（等快照渲染完再滚，避免滚到旧内容）
  pendingHighlight: [],     // 待高亮字段键
  pendingSources: false,    // v70：待定位引用区（医生点「查看 RAG 引用」时面板数据未就绪）
  returnFrom3d: "chat",     // 从病历单打开 3D 时，完成选点后回到病历单
  suggested: "",            // v70：服务端决策建议的工作区（只声明，绝不自动切换）
  log: []                   // 导航日志（最近 60 条）
};

function navEl(id) { return document.getElementById(id); }

function navLog(entry) {
  try {
    entry.at = new Date().toISOString();
    NAV.log.push(entry);
    if (NAV.log.length > 60) NAV.log.shift();
    try { console.log("[nav]", entry.trigger, entry.from_workspace, "→", entry.to_workspace,
                      entry.next_action, entry.skipped || ""); } catch (e0) {}
    if (bridge && bridge.navLog) bridge.navLog(JSON.stringify(entry));
  } catch (e) {}
}

window.__navLog = function () { return JSON.stringify(NAV.log.slice(-60)); };
window.__navState = function () {
  return JSON.stringify({ workspace: NAV.workspace, caseId: NAV.caseId,
                          sessionId: NAV.sessionId, navState: NAV.navState });
};

function navUpdateChrome() {
  var tEl = navEl("wsTitle"), cEl = navEl("wsCaseId");
  if (tEl) tEl.textContent = (NAV_WORKSPACE_ICONS[NAV.workspace] || "") + " " +
    t(NAV_WORKSPACE_LABELS[NAV.workspace] || NAV_WORKSPACE_LABELS.chat,
      NAV.workspace === "case" ? "Case workspace" : (NAV.workspace === "3d" ? "3D model" : "Chat workspace"));
  if (cEl) {
    cEl.textContent = NAV.caseId ? t("病例 ", "case ") + NAV.caseId : t("未关联病例", "no case yet");
    cEl.title = NAV.caseId
      ? t("当前病例 ID（导航与数据均绑定该病例）", "Current case id (navigation & data bound to it)") + "：" + NAV.caseId
      : t("尚未创建病例：在对话中描述病情即可生成门诊病历单", "No case yet: describe the case in chat");
  }
  var oc = navEl("wsOpenCase"), o3 = navEl("wsOpen3d"), bc = navEl("wsBackChat");
  if (oc) oc.classList.toggle("toggled", NAV.workspace === "case");
  if (o3) o3.classList.toggle("toggled", NAV.workspace === "3d");
  // 验收要求 14：「返回对话」始终可见（已在对话工作区时置灰而不是隐藏，避免按钮位置跳动）
  if (bc) { bc.disabled = NAV.workspace === "chat"; bc.title = NAV.workspace === "chat"
    ? t("当前已在对话工作区", "Already in chat workspace")
    : t("返回对话工作区（对话始终是主工作区）", "Back to chat workspace"); }
}

function navPersist() {
  try {
    if (!window.localStorage) return;
    localStorage.setItem(NAV_STORE_KEY, JSON.stringify(
      { workspace: NAV.workspace, caseId: NAV.caseId }));
  } catch (e) {}
}

function navRestorePersisted() {
  try {
    var raw = window.localStorage && localStorage.getItem(NAV_STORE_KEY);
    if (!raw) return;
    var s = JSON.parse(raw) || {};
    if (s.caseId) NAV.caseId = String(s.caseId);
    // 病例工作区需等病例数据到达后再落地（避免刷新时先弹出空面板）
    if (s.workspace === "case" || s.workspace === "3d") NAV.pendingWorkspace = s.workspace;
    navUpdateChrome();
  } catch (e) {}
}

/* 切换工作区（幂等：目标与当前一致则什么都不做，防重复响应导致窗口反复闪烁） */
function navSetWorkspace(ws) {
  if (ws !== "chat" && ws !== "case" && ws !== "3d") return false;
  var stage = navEl("workstage");
  if (!stage) return false;
  var before = NAV.workspace;
  if (ws === "3d") {
    if (typeof setLayout === "function" && APP_STATE.layout !== "model_primary") setLayout("model_primary");
    if (typeof onResize === "function") setTimeout(onResize, 0);   // 场景不重建，仅重新跟随尺寸
    /* v73：3D 是独立全屏工作区——进入时收起遮挡右侧的病历单/辅助抽屉（选点完成由 navLeave3d 按需重开） */
    ["clinicDrawer", "assistDrawer", "selDrawer"].forEach(function (id) {
      var d = document.getElementById(id);
      if (d) d.classList.remove("open");
    });
  } else {
    if (typeof setLayout === "function" && APP_STATE.layout !== "chat_primary") setLayout("chat_primary");
  }
  if (ws === "case") {
    var wasOpen = stage.classList.contains("case-open");
    stage.classList.add("case-open");
    // 面板未开才通知 Python 拉最新快照（已在显示则不重复拉取）
    if (!wasOpen && bridge && bridge.casePanelToggle) bridge.casePanelToggle("open");
  }
  // ws === "chat"：不强制收起病例面板——对话与病例同屏三栏，收起反而丢失医生上下文；
  // 只把「焦点与滚动」交回对话区。3D 是互斥布局，返回对话即恢复对话全宽。
  NAV.workspace = ws;
  navUpdateChrome();
  navPersist();
  return before !== ws;
}

function navFocusInput() {
  var input = navEl("chatInput");
  if (!input) return false;
  try { input.focus(); } catch (e) { return false; }
  try { if (typeof autosizeInput === "function") autosizeInput(); } catch (e2) {}
  return document.activeElement === input;
}

function navScrollLatest(ws) {
  if (ws === "case") {
    var body = navEl("cpBody");
    if (body) { body.scrollTop = body.scrollHeight; return true; }
  }
  var log = navEl("convLog");
  if (!log) return false;
  log.scrollTop = log.scrollHeight;
  return true;
}

/* 最新结果卡闪烁提示（分析结果就绪时告诉医生「就是这一条」） */
function navFlashLatestCard() {
  var log = navEl("convLog");
  if (!log) return false;
  var cards = log.querySelectorAll(".conv-card");
  var last = cards.length ? cards[cards.length - 1] : null;
  if (!last) return false;
  last.classList.add("nav-flash");
  setTimeout(function () { last.classList.remove("nav-flash"); }, 2600);
  return true;
}

/* 高亮待医生确认/已修改字段（只做视觉提示，绝不改动字段内容） */
function navHighlightFields(keys) {
  var panel = navEl("casePanel");
  if (!panel || !keys || !keys.length) return 0;
  var n = 0;
  keys.forEach(function (k) {
    var el = panel.querySelector('[data-field="' + String(k).replace(/[^A-Za-z0-9_]/g, "") + '"]');
    if (!el) return;
    n += 1;
    el.classList.add("nav-flash");
    setTimeout(function () { el.classList.remove("nav-flash"); }, 2600);
  });
  if (n && typeof cpSetBanner === "function") {
    cpSetBanner(t("已高亮 " + n + " 个待确认字段，请核对后确认或修改。",
      n + " field(s) highlighted for review."), "");
  }
  return n;
}

/* 病例面板的滚动/高亮/引用定位需要等快照渲染完成——先挂起，由 __pushCaseRecord 消费。
   v70：这些挂起项只由医生点击「工作结果」按钮产生，导航决策不再自动设置。 */
function navApplyPendingCase() {
  var out = { scrolled: null, highlighted: null, sources: null };
  if (NAV.pendingScrollCase) { out.scrolled = navScrollLatest("case"); NAV.pendingScrollCase = false; }
  if (NAV.pendingHighlight && NAV.pendingHighlight.length) {
    out.highlighted = navHighlightFields(NAV.pendingHighlight);
    NAV.pendingHighlight = [];
  }
  if (NAV.pendingSources) { out.sources = navRevealCitations(); NAV.pendingSources = false; }
  return out;
}

/* 打开并定位到引用资料区（RAG 完成后「查看引用」入口，不强制跳离对话） */
function navRevealCitations() {
  var panel = navEl("casePanel");
  if (!panel) return false;
  var cl = navEl("cpCitations");
  if (!cl) return false;
  var empty = navEl("cpEmpty");
  if (empty && empty.style.display !== "none") return false;   // 无病例：病例面板显示空态，无引用可定位
  if (cl.style.display === "none" || !cl.children.length) {
    if (typeof cpStatusFlash === "function")
      cpStatusFlash(t("本次结果暂无知识库引用（未覆盖时如实标注，不编造引用）",
        "No KB citations for this result (never fabricated)"));
    return false;
  }
  try { cl.scrollIntoView({ block: "center" }); } catch (e) { cl.scrollIntoView(); }
  cl.classList.add("nav-flash");
  setTimeout(function () { cl.classList.remove("nav-flash"); }, 2600);
  return true;
}

function navWorkspaceOf(dec) {
  var ws = dec.workspace;
  if (ws === undefined || ws === null || ws === "") ws = NAV_FALLBACK[dec.nav_state];
  if (ws === undefined) ws = null;
  return ws;
}

/* 该决策是否与病例绑定（用于过期守卫：病例类跳转绝不允许打到别的病例上） */
function navTouchesCase(dec) {
  return dec.workspace === "case" || dec.next_action === "navigate_to_case" ||
    dec.next_action === "highlight_case_fields" || dec.next_action === "navigate_to_sources";
}

/* 医生手动切换工作区（按钮/面板关闭）——与对话驱动导航同一状态与日志 */
function navManual(ws, trigger) {
  if (!navSetWorkspace(ws)) return;
  navLog({ session_id: NAV.sessionId, case_id: NAV.caseId, workflow_state: "",
           task_type: "", nav_state: "idle", next_action: "manual",
           tool_calls: [], trigger: trigger || "manual", from_workspace: "",
           to_workspace: NAV.workspace, focus_ok: null, scroll_ok: null, skipped: "",
           reason: "医生手动切换工作区" });
}

/* 3D 选点完成：先把已选部位写入当前病例草稿，再回对话并聚焦输入框。
   未建立病例时不隐式新建（返回 no_case，部位仍随下一条消息作为上下文提交）。
   v70-C：写回结果以确认消息卡落进对话流（不只状态行），医生在对话里能看到写回与来源。 */
function navLeave3d() {
  var regions = [];
  try {
    regions = (typeof selectedRegions !== "undefined" ? selectedRegions : [])
      .map(function (x) { return x.desc || x.label; }).filter(Boolean);
  } catch (e) {}
  var returnWorkspace = NAV.returnFrom3d === "case" ? "case" : "chat";
  NAV.returnFrom3d = "chat";
  navSetWorkspace(returnWorkspace);
  if (returnWorkspace === "chat") navFocusInput();
  if (NAV.clinic3dPick) {              // v73：从病历单打开 3D → 选点自动填入「身体部位」并回到病历单
    NAV.clinic3dPick = false;
    var cr = document.getElementById("clinicRegions");
    if (cr && regions.length) cr.value = regions.join("、");
    var cdForm = document.getElementById("clinicDrawer");
    if (cdForm) cdForm.classList.add("open");
  }
  if (!regions.length) return;
  if (!bridge || !bridge.regionsCommitted) return;
  bridge.regionsCommitted(JSON.stringify({ regions: regions }), function (res) {
    var r = {};
    try { r = JSON.parse(res) || {}; } catch (e2) {}
    var okMsg = t("已把 " + regions.length + " 处 3D 选点写入病例「身体部位」（来源：3D 模型选择）",
      regions.length + " 3D region(s) written to the case record");
    var noCaseMsg = t("当前对话尚未创建病例，已选 " + regions.length + " 处部位会随下一条消息提交",
      "No case yet; " + regions.length + " selected region(s) will be sent with your next message");
    var failMsg = t("3D 部位写入病例失败：" + (r.error || "未知原因"),
      "Failed to write 3D regions: " + (r.error || "unknown"));
    var log = convLogEl();
    if (log) {
      var card = document.createElement("div");
      card.className = "conv-card";
      var meta = document.createElement("div");
      meta.className = "conv-meta";
      meta.textContent = "📍 " + (r.ok ? okMsg : (r.no_case ? noCaseMsg : failMsg));
      card.appendChild(meta);
      log.appendChild(card);
      log.scrollTop = log.scrollHeight;
    }
    if (r.ok) {
      regions.forEach(function (label) { committedRegionKeys[String(label)] = true; });
      renderChips();
      setStatus(okMsg, "ok");
    } else {
      setStatus(r.no_case ? noCaseMsg : failMsg, "warn");
    }
  });
}

/* —— 统一导航入口：服务端决策 → 工作区/聚焦/滚动/高亮 —— */
window.__nav = function (payload) {
  var dec;
  try { dec = typeof payload === "string" ? JSON.parse(payload) : (payload || {}); }
  catch (e) { return "parse_error"; }
  var entry = {
    session_id: dec.session_id || "", case_id: dec.case_id || "",
    workflow_state: dec.workflow_state || "", task_type: dec.task_type || "",
    nav_state: dec.nav_state || "", next_action: dec.next_action || "stay",
    tool_calls: dec.tool_calls || [], trigger: dec.trigger || "unknown",
    from_workspace: NAV.workspace, to_workspace: NAV.workspace,
    focus_ok: null, scroll_ok: null, skipped: "", reason: dec.reason || ""
  };

  // ① 乱序守卫：异步返回顺序错乱时，序号倒退的旧决策一律丢弃（否则会跳回旧窗口/旧病例）
  if (typeof dec.seq === "number" && dec.seq > 0 && dec.seq < NAV.lastSeq) {
    entry.skipped = "out_of_order";
    entry.reason = "决策序号 " + dec.seq + " < 已执行 " + NAV.lastSeq + "：丢弃乱序旧决策";
    navLog(entry);
    return "out_of_order";
  }
  // ② 过期病例守卫：病例类决策携带的 case_id 与当前病例不符 → 丢弃（绝不打开错误病例）
  if (dec.case_id && NAV.caseId && dec.case_id !== NAV.caseId && navTouchesCase(dec)) {
    entry.skipped = "stale_case";
    entry.reason = "决策病例 " + dec.case_id + " ≠ 当前病例 " + NAV.caseId + "：丢弃过期导航";
    navLog(entry);
    return "stale_case";
  }
  if (typeof dec.seq === "number" && dec.seq > 0) NAV.lastSeq = dec.seq;
  if (dec.session_id) NAV.sessionId = String(dec.session_id);
  if (dec.case_id) {
    NAV.caseId = String(dec.case_id);
    renderRailHist();   // v73-fix：新病例建立/切换后刷新侧栏「最近」，否则新对话无法从侧栏返回
  }
  NAV.navState = dec.nav_state || NAV.navState;

  var ws = navWorkspaceOf(dec);
  // ③ v70 语义反转：决策只「声明」建议工作区，绝不自动切换——LLM 回复永远留在对话区，
  //    病例/3D/引用/导出等工作窗口一律由医生点击回复卡下的「工作结果」按钮打开（window.openWork）。
  NAV.suggested = ws || "";
  entry.suggested_workspace = NAV.suggested;
  entry.auto_switched = false;
  if (ws && ws !== NAV.workspace) {
    entry.reason = (dec.reason || "") +
      "（建议进入" + (NAV_WORKSPACE_LABELS[ws] || ws) + "，按需手动打开，不自动跳转）";
  }
  if (!ws) entry.skipped = dec.nav_state === "error" ? "" : "stay";

  // ④ 聚焦输入框（回复已在对话区呈现，医生可直接继续输入——这不是工作区跳转）
  if (dec.focus_input || dec.next_action === "focus_input") entry.focus_ok = navFocusInput();
  // ⑤ 对话区滚到最新回复（保持对话可见性；不滚病例面板、不闪结果卡、不弹任何窗口）
  if (dec.scroll_to_latest || dec.next_action === "scroll_to_latest" ||
      dec.next_action === "navigate_to_analysis") {
    entry.scroll_ok = navScrollLatest("chat");
  }

  navUpdateChrome();
  navPersist();   // v70：不再切工作区，但仍把最新 case_id 持久化（刷新恢复绑定同一病例）
  navLog(entry);
  return "ok";
};

/* v70：顶部按钮改为手动打开入口——直接走 openWork（医生主动行为），不再借道导航决策 */
window.navToSources = function () {
  window.openWork("open_sources", { caseId: NAV.caseId, trigger: "btn_sources" });
};
window.navToCase = function () {
  window.openWork("open_case", { caseId: NAV.caseId, trigger: "btn_case" });
};
window.navToChat = function () { navManual("chat", "btn_back_chat"); };
window.navToModel = function () { navManual("3d", "btn_model"); };

function navBindChrome() {
  var oc = navEl("wsOpenCase");
  if (oc) oc.addEventListener("click", function () {
    if (NAV.workspace === "case") cpClose();   // cpClose 内部已回写导航态为 chat
    else window.navToCase();
  });
  var o3 = navEl("wsOpen3d");
  if (o3) o3.addEventListener("click", function () {
    if (NAV.workspace === "3d") window.navToChat(); else window.navToModel();
  });
  var bc = navEl("wsBackChat");
  if (bc) bc.addEventListener("click", function () {
    if (NAV.workspace === "3d") navLeave3d(); else window.navToChat();
  });
}

/* ================= v70：工作结果按钮区（对话优先 · 结果按需打开） =================
   每条回复卡底部按服务端 available_actions/result_summary 渲染「工作结果」区：
   - 按钮清单是服务端 artifacts 派生层的权威输出，前端不做任何文字猜测；
   - 没有产物/动作就不渲染按钮（纯闲聊回复只有 3D 工具入口，绝无虚假按钮）；
   - 医生点击 window.openWork(action) 才打开对应工作窗口——这是全应用唯一打开入口；
   - 历史卡各自从 MSG_ARTIFACTS[message_id] 取自己的按钮，点旧消息只打开旧消息的结果。 */
var WORK_ACTION_META = {
  open_case:       { icon: "📋", zh: "查看病例",       en: "View case record" },
  open_analysis:   { icon: "🧠", zh: "查看完整分析",   en: "View full analysis" },
  open_sources:    { icon: "📚", zh: "查看 RAG 引用",  en: "View RAG citations" },
  open_medication: { icon: "💊", zh: "查看用药核对",   en: "View medication check" },
  open_risk:       { icon: "🚩", zh: "查看危险信号",   en: "View danger signals" },
  open_3d:         { icon: "🧍", zh: "打开 3D 人体模型", en: "Open 3D body model" },
  export_case:     { icon: "📤", zh: "导出病例",       en: "Export case" },
  retry:           { icon: "🔄", zh: "重试",           en: "Retry" }
};
var MSG_ARTIFACTS = {};   // message_id → {artifacts, available_actions, result_summary, authenticity}

/* 恢复会话前由 main.py 注入：各历史消息的产物/按钮存档（服务端仅保留近 12 条） */
window.__setMsgArtifacts = function (jsonStr) {
  try {
    var m = JSON.parse(jsonStr);
    MSG_ARTIFACTS = (m && typeof m === "object" && !Array.isArray(m)) ? m : {};
    return "ok";
  } catch (e) { return "parse_error"; }
};

/* 唯一打开入口：ctx = { caseId, messageId, cardEl, retryPayload, trigger }。
   病例绑定守卫：点历史消息的按钮只打开该消息对应的病例，绝不串到别的病例。 */
window.openWork = function (action, ctx) {
  ctx = ctx || {};
  var meta = WORK_ACTION_META[action];
  if (!meta) return "unknown_action";
  var wantCase = String(ctx.caseId || "");
  if (wantCase && NAV.caseId && wantCase !== NAV.caseId) {
    setStatus(t("该结果属于另一病例（" + wantCase + "），未打开；请先切换到对应会话",
      "This result belongs to another case (" + wantCase + "); switch to that session first"), "warn");
    return "stale_case";
  }
  navLog({ session_id: NAV.sessionId, case_id: wantCase || NAV.caseId,
           workflow_state: "", task_type: "", nav_state: "work_action",
           next_action: action, tool_calls: [], trigger: ctx.trigger || ("work_" + action),
           from_workspace: NAV.workspace, to_workspace: "", suggested_workspace: "",
           focus_ok: null, scroll_ok: null, skipped: "", auto_switched: false,
           reason: "医生点击「工作结果」按钮（按需打开，非自动跳转）" });
  switch (action) {
    case "open_case":
      if (typeof window.__openClinicForm === "function") window.__openClinicForm();   // v73：打开门诊病历单抽屉
      navScrollLatest("case");
      break;
    case "open_analysis": {
      var target = ctx.cardEl || null;
      if (target && target.parentNode) {
        try { target.scrollIntoView({ block: "center" }); } catch (e0) { target.scrollIntoView(); }
        target.classList.add("nav-flash");
        setTimeout(function () { target.classList.remove("nav-flash"); }, 2600);
      } else {
        navFlashLatestCard();
      }
      break;
    }
    case "open_sources":
      navSetWorkspace("case");
      // 面板数据未就绪（首次打开）→ 挂起，等 __pushCaseRecord 渲染后定位引用区
      if (!navRevealCitations()) NAV.pendingSources = true;
      break;
    case "open_medication":
    case "open_risk":
      navSetWorkspace("case");
      NAV.pendingHighlight = [action === "open_risk" ? "danger_signals" : "medication_check"];
      if (NAV.workspace === "case" && cpState && cpState.data) navApplyPendingCase();
      break;
    case "open_3d":
      navSetWorkspace("3d");   // 3D 是可选辅助工具：完全由医生手动打开
      break;
    case "export_case":
      navSetWorkspace("case");
      var eb = navEl("cpExportBtn");
      if (eb) {
        try { eb.scrollIntoView({ block: "center" }); } catch (e1) { eb.scrollIntoView(); }
        eb.classList.add("nav-flash");
        setTimeout(function () { eb.classList.remove("nav-flash"); }, 2600);
        if (eb.disabled) {   // 门禁不被绕过：未确认只提示，不伪造解锁
          if (typeof cpStatusFlash === "function")
            cpStatusFlash(t("病例尚未经医生确认：确认后「导出」自动解锁",
              "Case not confirmed yet; export unlocks after confirmation"));
        }
      }
      break;
    case "retry":
      var rp = ctx.retryPayload;
      if (rp && bridge && bridge.onChatSubmitted) {
        try { bridge.onChatSubmitted(typeof rp === "string" ? rp : JSON.stringify(rp)); }
        catch (e2) { setStatus(t("重试失败：", "Retry failed: ") + e2, "warn"); }
      } else {
        setStatus(t("重试失败：桌面桥不可用", "Retry failed: bridge unavailable"), "warn");
      }
      break;
  }
  return "ok";
};

/* 服务端权威清单 → 「工作结果」按钮区。没有产物/动作就不渲染（绝不造假按钮）。 */
function renderWorkActions(d, ctx) {
  d = d || {};
  var actions = (d.available_actions || []).filter(function (a) { return !!WORK_ACTION_META[a]; });
  var summary = (d.result_summary || []).filter(Boolean);
  if (!actions.length && !summary.length) return null;
  var wrap = document.createElement("div");
  wrap.className = "conv-work";
  if (summary.length) {
    var sEl = document.createElement("div");
    sEl.className = "conv-work-summary";
    sEl.textContent = t("工作结果：", "Work results: ") + summary.join(" · ");
    wrap.appendChild(sEl);
  }
  if (actions.length) {
    var row = document.createElement("div");
    row.className = "conv-actions conv-work-actions";
    var base = { caseId: d.case_id || (ctx && ctx.caseId) || "",
                 messageId: d.message_id || (ctx && ctx.messageId) || "",
                 cardEl: (ctx && ctx.cardEl) || null,
                 retryPayload: (ctx && ctx.retryPayload) || null };
    actions.forEach(function (a) {
      var m = WORK_ACTION_META[a];
      var b = document.createElement("button");
      if (a === "open_case") b.className = "primary";
      b.textContent = m.icon + " " + t(m.zh, m.en);
      b.addEventListener("click", function () { window.openWork(a, base); });
      row.appendChild(b);
    });
    /* v73：导出问诊单——内容即本条回复卡的输出文本（分析结果），存为 Markdown */
    var replyEl = base.cardEl ? base.cardEl.querySelector(".conv-agent-reply") : null;
    var replyText = replyEl ? String(replyEl.innerText || replyEl.textContent || "").trim() : "";
    if (replyText) {
      var eb = document.createElement("button");
      eb.textContent = "📄 " + t("导出问诊单", "Export consult note");
      eb.title = t("把本条输出内容导出为问诊单（Markdown 文件）", "Export this output as a consult note (Markdown)");
      eb.addEventListener("click", function () {
        if (bridge && bridge.exportConsultNote) bridge.exportConsultNote(replyText);
        else setStatus(t("桌面桥不可用，无法导出", "Bridge unavailable; export failed"), "warn");
      });
      row.appendChild(eb);
    }
    wrap.appendChild(row);
  }
  return wrap;
}

/* ================= v66：右侧门诊病历面板（紧凑病历单） =================
   三栏布局：左导航 + 中间对话（始终可见）+ 右侧病历单（宽 38%）；面板开合由
   #workstage.case-open 类控制 CSS，对话/3D 同步让位不遮挡。数据与对话共享同一
   case_id；快操作走 bridge 同步 JSON，regen/export 走 Python 后台线程推回。 */
var CP_FIELDS = [
  { k: "patient", label: "患者基本信息", core: true },
  { k: "chief_complaint", label: "主诉", core: true },
  { k: "present_illness", label: "现病史", core: true },
  { k: "past_history", label: "既往史" },
  { k: "personal_history", label: "个人史" },
  { k: "allergy_history", label: "过敏史" },
  { k: "medication_history", label: "用药史" },
  { k: "physical_exam", label: "体格检查" },
  { k: "skin_lesions", label: "皮损描述", core: true },
  { k: "body_regions", label: "身体部位", core: true },
  { k: "auxiliary_exam", label: "辅助检查" },
  { k: "impression", label: "初步判断/鉴别方向", core: true },
  { k: "treatment_ref", label: "治疗参考" },
  { k: "medication_check", label: "用药核对" },
  { k: "follow_up", label: "随访与恢复情况" },
  { k: "danger_signals", label: "风险提示" },
  { k: "doctor_review", label: "医生审核意见", core: true },
  { k: "notes", label: "备注" }
];
var CP_FIELD_MAP = {};
CP_FIELDS.forEach(function (f) { CP_FIELD_MAP[f.k] = f; });
var CP_SOURCE_LABELS = {
  ai_draft: "AI 草稿", doctor_modified: "医生修改", doctor_confirmed: "医生确认",
  user_input: "医生录入", model_3d: "来源：3D 人体模型", kb_ref: "知识库引用"
};
var CP_STATUS_LABELS = {
  ai_draft: "AI 草稿", pending_review: "待医生审核", doctor_modified: "医生已修改",
  confirmed: "已确认", rejected: "已拒绝"
};
var cpState = { data: null, mode: "view", editValues: {}, dirty: new Set(),
  curField: "impression", moreOpen: null, regenBusy: false };   // moreOpen: null=未手动操作（按内容自动展开）
function cpEl(id) { return document.getElementById(id); }
function cpLabel(k) { return (CP_FIELD_MAP[k] || {}).label || k; }
function cpStatusText(rec) {
  if (!rec) return "—";
  return rec.status_label || CP_STATUS_LABELS[rec.status] || CP_STATUS_LABELS.pending_review;
}
function cpTime(v) { return v ? String(v).replace("T", " ").slice(0, 19) : "—"; }

window.__openCasePanel = function () {
  // v69：与对话驱动导航同一入口——保证「手动打开」也写入 NAV 状态与导航日志
  window.__nav({ nav_state: "case_draft", next_action: "navigate_to_case", workspace: "case",
                 trigger: "open_case_panel", case_id: NAV.caseId });
};
/* v73：对话/Agent 触发的「打开病历单」统一改为打开门诊病历单抽屉（clinicDrawer），
   不再打开旧病例面板；数据随下一条消息提交，填写逻辑与 initClinicUI 完全一致。 */
window.__openClinicForm = function () {
  var cd = document.getElementById("clinicDrawer");
  if (!cd) return;
  cd.classList.add("open");
  var ad = document.getElementById("assistDrawer");
  if (ad) ad.classList.remove("open");   // 同侧互斥（与 clinicEntry 行为一致）
};
function cpClose() {
  var ws = document.getElementById("workstage");
  if (ws) ws.classList.remove("case-open");
  cpExitEdit(false);
  if (bridge && bridge.casePanelToggle) bridge.casePanelToggle("close");
  navManual("chat", "cp_close");   // v69：收起病例面板 = 回到对话工作区（同步标题与状态）
}
window.__pushCaseRecord = function (payloadStr) {
  var pl;
  try { pl = typeof payloadStr === "string" ? JSON.parse(payloadStr) : payloadStr; }
  catch (e) { return; }
  if (!pl) return;
  if (pl.no_case) {
    cpState.data = null; cpState.mode = "view"; cpRenderNoCase(pl.message || "");
    NAV.caseId = ""; NAV.pendingWorkspace = ""; navUpdateChrome();   // v69：确实无病例，撤销待恢复工作区
    return;
  }
  cpState.data = pl;
  // v69：病例面板永远显示「当前会话的病例」——以服务端快照为准修正导航态里的 case_id
  var sid = pl.case_id || (pl.record && pl.record.case_id) || "";
  if (sid) NAV.caseId = String(sid);
  navUpdateChrome();
  if (NAV.workspace === "case") {
    var pn = navApplyPendingCase();
    if (pn.scrolled || pn.highlighted) navLog({ session_id: NAV.sessionId, case_id: NAV.caseId,
      workflow_state: "", task_type: "", nav_state: NAV.navState, next_action: "apply_case_pending",
      tool_calls: [], trigger: "case_snapshot_rendered", from_workspace: NAV.workspace,
      to_workspace: NAV.workspace, focus_ok: null, scroll_ok: pn.scrolled,
      highlighted: pn.highlighted, skipped: "", reason: "快照渲染后滚动/高亮待确认字段" });
  }
  if (NAV.pendingWorkspace) {          // 刷新/重启恢复：数据到位后才落地工作区，避免先弹空面板
    var pw = NAV.pendingWorkspace; NAV.pendingWorkspace = "";
    navSetWorkspace(pw);
    navLog({ session_id: NAV.sessionId, case_id: NAV.caseId, workflow_state: "", task_type: "",
             nav_state: "idle", next_action: "restore_workspace", tool_calls: [],
             trigger: "restore_after_reload", from_workspace: "chat", to_workspace: NAV.workspace,
             focus_ok: null, scroll_ok: navScrollLatest(pw), skipped: "",
             reason: "刷新后恢复上次工作区（等病例数据到达再落地）" });
  }
  if (cpState.mode === "edit") cpFillEditValues();   // 对话更新时同步编辑框底层值，医生未改的字段不被覆盖
  cpRender();
};

function cpRenderNoCase(msg) {
  var empty = cpEl("cpEmpty"); if (!empty) return;
  cpEl("cpFields").innerHTML = "";
  cpEl("cpMoreFields").innerHTML = "";
  cpEl("cpMoreGroupHead").style.display = "none";
  cpEl("cpCitations").style.display = "none";
  cpEl("cpDisclaimer").textContent = "";
  cpSetBanner("", "");
  cpEl("cpCaseId").textContent = "—";
  cpEl("cpCreated").textContent = "—"; cpEl("cpUpdated").textContent = "—";
  var tag = cpEl("cpStatusTag"); tag.textContent = "未创建"; tag.setAttribute("data-state", "pending_review");
  empty.style.display = "block";
  empty.textContent = msg || t("当前对话尚未创建病例。在对话中描述病情即可自动生成门诊病历单。",
    "No case record yet. Describe the patient in chat to generate one.");
  cpEl("cpEditBtn").disabled = cpEl("cpSaveBtn").disabled = true;
  cpEl("cpConfirmBtn").disabled = cpEl("cpExportBtn").disabled = true;
  cpEl("cpRegenBtn").disabled = cpEl("cpHistoryBtn").disabled = cpEl("cpRejectBtn").disabled = true;
}
function cpSetBanner(text, kind) {
  var b = cpEl("cpBanner");
  if (!text) { b.style.display = "none"; b.textContent = ""; return; }
  b.style.display = "block"; b.textContent = text;
  b.className = kind ? kind : "";
}
function cpHasContent(v) { return !!(v && String(v).trim()); }
/* LLM 未连接判定：Python 推回路径带顶层 llm_connected；同步 get 路径从 record.llm_meta 派生
   （llm_meta.ok === false 且 source === "none"），两路径统一。 */
function cpLlmOff(d) {
  if (!d) return false;
  if (d.llm_connected === false) return true;
  var m = ((d.record || {}).llm_meta) || {};
  return m.ok === false && m.source === "none";
}

function cpRender() {
  var rec = (cpState.data || {}).record;
  if (!rec) { cpRenderNoCase((cpState.data || {}).message); return; }
  var empty = cpEl("cpEmpty"); empty.style.display = "none";
  var fields = rec.fields || {}, meta = rec.meta || {};
  cpEl("cpCaseId").textContent = rec.case_id || "—";
  cpEl("cpCreated").textContent = cpTime(rec.created_at);
  cpEl("cpUpdated").textContent = cpTime(rec.updated_at);
  var tag = cpEl("cpStatusTag");
  tag.textContent = cpStatusText(rec);
  tag.setAttribute("data-state", rec.status || "pending_review");
  cpEl("cpEditBtn").disabled = cpEl("cpSaveBtn").disabled = false;
  cpEl("cpConfirmBtn").disabled = false;
  if (cpEl("cpConfirmBtn2")) cpEl("cpConfirmBtn2").disabled = false;
  cpEl("cpExportBtn").disabled = rec.review_status !== "confirmed";   // 导出必须先医生确认
  cpEl("cpRegenBtn").disabled = cpLlmOff(cpState.data);
  cpEl("cpHistoryBtn").disabled = false;
  cpEl("cpRejectBtn").disabled = false;
  cpEl("cpConfirmBtn").disabled = rec.review_status === "confirmed";
  if (cpEl("cpConfirmBtn2")) cpEl("cpConfirmBtn2").disabled = rec.review_status === "confirmed";
  var intakeMode = (cpState.data || {}).workflow_state === "case_intake";
  if (intakeMode) {
    cpEl("cpConfirmBtn").textContent = t("下一步：情况补充", "Next: supplementation");
    if (cpEl("cpConfirmBtn2")) cpEl("cpConfirmBtn2").textContent = t("下一步：情况补充", "Next: supplementation");
  } else {
    cpEl("cpConfirmBtn").textContent = t("✅ 医生确认", "Physician confirm");
    if (cpEl("cpConfirmBtn2")) cpEl("cpConfirmBtn2").textContent = t("医生确认", "Physician confirm");
  }
  // LLM 未连接横幅：如实说明，不伪造 AI 内容
  if (cpLlmOff(cpState.data)) {
    cpSetBanner(t("当前未连接 LLM，AI 内容无法生成。可手动填写或保存本地模板整理结果；恢复连接后如需 AI 重新整理，请在对话中说明。",
      "LLM not connected. Fill in manually or keep template text; ask in chat after reconnecting."), "llm-off");
  } else if (cpState.regenBusy) {
    cpSetBanner(t("正在重新生成「" + cpLabel(cpState.curField) + "」（AI 生成约需数十秒，完成后自动刷新）…",
      "Regenerating \"" + cpLabel(cpState.curField) + "\"…"), "");
  } else {
    cpSetBanner("", "");
  }
  // 字段渲染：core 默认展开；非 core 进「更多病历字段」（任一有内容则自动展开）
  var hasMore = false;
  CP_FIELDS.forEach(function (f) { if (!f.core && cpHasContent(fields[f.k])) hasMore = true; });
  if (cpState.moreOpen === null) cpState.moreOpen = hasMore;   // 仅初始按内容自动展开，之后尊重医生手动收起/展开
  cpRenderFieldGroup(cpEl("cpFields"), CP_FIELDS.filter(function (f) { return f.core; }), fields, meta);
  cpRenderFieldGroup(cpEl("cpMoreFields"), CP_FIELDS.filter(function (f) { return !f.core; }), fields, meta);
  var gh = cpEl("cpMoreGroupHead");
  gh.style.display = "block";
  gh.textContent = (cpState.moreOpen ? "更多病历字段 ▾" : "更多病历字段 ▸") +
    (hasMore ? "" : t("（暂无内容）", " (empty)"));
  cpEl("cpMoreFields").style.display = cpState.moreOpen ? "block" : "none";
  // 引用（v67：详情行——[id] 资料名 · 作者/出版社/版本 · 章节 · 页提示 + 引用片段 + 检索时间）
  var cites = (rec.citations || []).filter(function (c) { return c && typeof c === "object"; });
  var cl = cpEl("cpCitations");
  cl.innerHTML = "";
  if (cites.length) {
    cl.style.display = "block";
    var ch = document.createElement("div"); ch.className = "cp-cites-h";
    ch.textContent = t("📚 引用资料（" + cites.length + "）", "📚 Citations (" + cites.length + ")");
    cl.appendChild(ch);
    cites.forEach(function (c) {
      var item = document.createElement("div"); item.className = "cp-cite";
      var l1 = document.createElement("div"); l1.className = "cp-cite-t";
      var name = c.book_title || c.document_title || c.title || "";
      l1.textContent = "[" + (c.citation_id || "-") + "] " + (name || t("（未命名资料）", "(untitled)"));
      item.appendChild(l1);
      var bits = [c.author, c.publisher, c.version ? "v" + c.version : "",
                  c.section, c.page_hint ? t("页 ", "p. ") + c.page_hint : ""].filter(Boolean);
      if (bits.length) {
        var l2 = document.createElement("div"); l2.className = "cp-cite-s";
        l2.textContent = bits.join(" · ");
        item.appendChild(l2);
      }
      var q = String(c.quoted_text || "").trim();
      if (q) {
        var l3 = document.createElement("div"); l3.className = "cp-cite-q";
        l3.textContent = "「" + (q.length > 140 ? q.slice(0, 140) + "…" : q) + "」";
        item.appendChild(l3);
      }
      var at = String(c.retrieved_at || "").replace("T", " ").slice(0, 16);
      if (at) {
        var l4 = document.createElement("div"); l4.className = "cp-cite-a";
        l4.textContent = t("检索时间 ", "Retrieved ") + at;
        item.appendChild(l4);
      }
      cl.appendChild(item);
    });
  } else cl.style.display = "none";
  cpEl("cpDisclaimer").textContent = rec.disclaimer ||
    t("本病历单为 AI 辅助整理草稿，仅供医生参考；所有内容需经医生审核确认后方可用于正式诊疗与导出。",
      "AI-assisted draft for physician review only.");
}
function cpRenderFieldGroup(box, defs, fields, meta) {
  box.innerHTML = "";
  defs.forEach(function (f) {
    var val = fields[f.k] || "", src = (meta[f.k] || {}).source || "";
    var row = document.createElement("div");
    row.className = "cp-field" + (f.core ? " core" : "");
    row.setAttribute("data-field", f.k);
    var head = document.createElement("div"); head.className = "cp-fhead";
    var name = document.createElement("span"); name.className = "cp-fname"; name.textContent = f.label;
    var arrow = document.createElement("span"); arrow.className = "cp-farrow";
    var srcTag = document.createElement("span"); srcTag.className = "cp-src";
    if (f.k === "body_regions") {
      // 3D 部位专用文案：选点时「来源：3D 人体模型」，未用不阻塞
      srcTag.textContent = cpHasContent(val) ? CP_SOURCE_LABELS.model_3d : t("未使用 3D 部位选择", "No 3D regions");
      srcTag.setAttribute("data-src", cpHasContent(val) ? "model_3d" : "none");
    } else if (src && CP_SOURCE_LABELS[src]) {
      srcTag.textContent = CP_SOURCE_LABELS[src];
      srcTag.setAttribute("data-src", src);
    } else { srcTag.textContent = t("待补充", "Pending"); srcTag.setAttribute("data-src", "none"); }
    var body = document.createElement("div"); body.className = "cp-fbody";
    if (cpState.mode === "edit" && !f.readonly) {
      arrow.textContent = "✏️";
      body.classList.add("editing");
      var ta = document.createElement("textarea");
      ta.value = cpState.editValues[f.k] !== undefined ? cpState.editValues[f.k] : val;
      ta.placeholder = t("（可手动填写；留空保存即清空该字段）", "(fill manually; save empty to clear)");
      ta.addEventListener("input", function () {
        cpState.editValues[f.k] = ta.value;
        cpState.dirty.add(f.k);
        cpState.curField = f.k;
      });
      ta.addEventListener("focus", function () { cpState.curField = f.k; });
      body.appendChild(ta);
    } else {
      arrow.textContent = "";
      if (cpHasContent(val)) {
        body.textContent = val;
      } else {
        body.classList.add("empty");
        body.textContent = f.k === "body_regions"
          ? t("未使用 3D 部位选择（不影响创建/保存/分析/导出）", "No 3D regions (not blocking)")
          : t("待补充", "Pending");
      }
    }
    head.appendChild(name); head.appendChild(arrow); head.appendChild(srcTag);
    row.appendChild(head); row.appendChild(body);
    box.appendChild(row);
  });
}
function cpFillEditValues() {
  var rec = (cpState.data || {}).record || {};
  CP_FIELDS.forEach(function (f) {
    if (!cpState.dirty.has(f.k)) cpState.editValues[f.k] = (rec.fields || {})[f.k] || "";
  });
}

/* —— 编辑 / 保存（只提交医生改过的字段，不覆盖 AI 新内容）—— */
function cpEnterEdit() {
  if (!cpState.data || !(cpState.data.record)) return;
  cpState.mode = "edit";
  cpState.editValues = {}; cpState.dirty = new Set();
  var rec = cpState.data.record;
  CP_FIELDS.forEach(function (f) { cpState.editValues[f.k] = (rec.fields || {})[f.k] || ""; });
  cpRender();
}
function cpExitEdit(rerender) {
  cpState.mode = "view"; cpState.editValues = {}; cpState.dirty = new Set();
  if (rerender) cpRender();
}
function cpSave(after) {
  if (!cpState.data || !cpState.data.record) return;

  if (cpState.mode !== "edit") { cpStatusFlash(t("没有修改需要保存（先点「编辑」修改字段）", "Nothing to save")); return; }
  if (!cpState.dirty.size) { cpStatusFlash(t("没有修改需要保存", "No edits to save")); cpExitEdit(true); return; }
  var fields = {};
  cpState.dirty.forEach(function (k) { fields[k] = cpState.editValues[k]; });
  var changed = Object.keys(fields);
  if (!(bridge && bridge.caseRecordSave)) { cpStatusFlash(t("桥接未就绪，保存失败", "Bridge not ready")); return; }
  bridge.caseRecordSave(JSON.stringify({ fields: fields }), function (s) {
    var res; try { res = JSON.parse(s); } catch (e) { res = null; }
    if (!res || res.ok === false) {
      cpStatusFlash(t("保存失败：" + ((res && res.error) || "未知错误"), "Save failed"));
      return;
    }
    cpState.data = res; cpExitEdit(false);
    cpRender();
    cpStatusFlash(t("已保存草稿", "Draft saved"));
    appendCaseLinkCard(changed);   // 对话联动：医生已修改提示卡（确认后才重新整理，不自动重写）
    if (typeof after === "function") after();
  });
}
function cpConfirm() {
  if (!cpState.data || !cpState.data.record) return;
  if (cpState.mode === "edit") { cpSave(function () { cpConfirm(); }); return; }   // 先保存再确认
  var intakeMode = (cpState.data || {}).workflow_state === "case_intake";
  if (!confirm(intakeMode
    ? t("确认病历基础信息填写完成，并进入情况补充？", "Finish intake and continue to supplementation?")
    : t("医生确认后本份病历单即为确认版本，导出正式病例将解锁。确认？",
        "Confirm this record as the physician-confirmed version?"))) return;
  bridge.caseRecordConfirm(JSON.stringify({ all: true }), function (s) {
    var res; try { res = JSON.parse(s); } catch (e) { res = null; }
    if (!res || res.ok === false) { cpStatusFlash(t("确认失败：" + ((res && res.error) || "未知错误"), "Confirm failed")); return; }
    cpState.data = res;
    cpRender();
    /* 确认后再次读取按钮状态，避免服务端异步推送覆盖旧病例快照。 */
    setTimeout(function () { cpRender(); }, 120);
    if (res.intake_completed) {
      cpStatusFlash(t("病历填写已完成，请返回对话补充情况", "Intake completed; continue in chat"));
      if (typeof window.navToChat === "function") window.navToChat();
    } else {
      cpStatusFlash(t("已确认为医生版本，可导出正式病历单", "Confirmed; export unlocked"));
    }
  });
}
function cpReject() {
  if (!cpState.data || !cpState.data.record) return;
  if (!confirm(t("标记本份病历为「已拒绝」？对话与 3D 选点不受影响。", "Mark this record as rejected?"))) return;
  bridge.caseRecordReject(function (s) {
    var res; try { res = JSON.parse(s); } catch (e) { res = null; }
    if (!res || res.ok === false) { cpStatusFlash(t("操作失败：" + ((res && res.error) || "未知错误"), "Failed")); return; }
    cpState.data = res; cpRender();
    cpStatusFlash(t("已标记为已拒绝", "Rejected"));
  });
}
function cpRegen() {
  if (cpState.mode === "edit") { cpStatusFlash(t("请先保存或退出编辑，再重新生成", "Save or exit editing first")); return; }
  if (!cpState.data || !cpState.data.record) return;
  if (cpLlmOff(cpState.data)) { cpStatusFlash(t("当前未连接 LLM，无法重新生成", "LLM not connected")); return; }
  var f = cpState.curField || "impression";
  cpState.regenBusy = true; cpRender();
  if (bridge && bridge.caseRecordRegen) bridge.caseRecordRegen(f);
}
function cpShowHistory() {
  var list = cpEl("cphList"); list.innerHTML = "";
  var renderHist = function (res) {
  var items = (res && res.history) || [];
  if (!items.length) {
    list.innerHTML = '<div class="cph-item">' + t("暂无修改记录。医生编辑保存与 AI 重新生成都会在这里留下前后版本。",
      "No edits yet.") + "</div>";
  } else {
    items.slice().reverse().forEach(function (h) {
      var d = document.createElement("div"); d.className = "cph-item";
      var m = document.createElement("div"); m.className = "cph-meta";
      m.textContent = cpTime(h.at) + " · " + (h.by === "doctor" ? "医生修改" : "AI 重新生成") + " · " + cpLabel(h.field);
      var before = document.createElement("div"); before.className = "cph-diff-before";
      before.textContent = "改前：" + (cpHasContent(h.before) ? String(h.before).slice(0, 120) : "（空）");
      var after = document.createElement("div"); after.className = "cph-diff-after";
      after.textContent = "改后：" + (cpHasContent(h.after) ? String(h.after).slice(0, 120) : "（空）");
      d.appendChild(m); d.appendChild(before); d.appendChild(after);
      list.appendChild(d);
    });
  }
  cpEl("cpHistoryLayer").classList.add("open");
  };
  if (!(bridge && bridge.caseHistoryGet)) { renderHist(null); return; }
  bridge.caseHistoryGet(function (s) {
    var res; try { res = JSON.parse(s); } catch (e) { res = null; }
    renderHist(res);
  });
}
function cpStatusFlash(msg) {
  var el = document.getElementById("statusLine");
  if (el) { setStatus(msg, "ok"); }
}

/* —— 对话联动提示卡：医生在面板修改后，对话流提示是否重新整理（确认才执行，不自动重写）—— */
function appendCaseLinkCard(fieldKeys) {
  var log = convLogEl();
  if (!log || !fieldKeys || !fieldKeys.length) return;
  var names = fieldKeys.map(cpLabel).join("、");
  var card = document.createElement("div");
  card.className = "conv-card";
  var meta = document.createElement("div"); meta.className = "conv-meta";
  meta.textContent = t("📋 病例面板联动", "📋 Case panel sync");
  var body = document.createElement("div"); body.className = "conv-user";
  body.textContent = t("医生已修改「" + names + "」，是否根据修改内容重新整理病例？（不会自动重写其他字段）",
    "Physician updated \"" + names + "\". Re-organize the record based on it? (no auto rewrite)");
  var row = document.createElement("div"); row.style.marginTop = "6px"; row.style.display = "flex"; row.style.gap = "8px";
  var yes = document.createElement("button"); yes.className = "conv-retry-btn";
  yes.textContent = t("是，根据修改重新整理", "Yes, re-organize");
  yes.addEventListener("click", function () {
    row.remove();
    var input = document.getElementById("chatInput");
    if (input) input.value = t(
      "医生已在病例面板修改了「" + names + "」。请仅根据修改后的内容重新整理本份病例的对应字段，保持其他字段不变，不要覆盖医生已修改的内容。",
      "The physician updated \"" + names + "\" in the case panel. Re-organize only the affected fields; keep others unchanged.");
    sendChat();
  });
  var no = document.createElement("button"); no.className = "conv-retry-btn";
  no.textContent = t("暂不", "Not now");
  no.addEventListener("click", function () { row.remove(); body.textContent += t("（已选择暂不重新整理）", " (skipped)"); });
  row.appendChild(yes); row.appendChild(no);
  card.appendChild(meta); card.appendChild(body); card.appendChild(row);
  log.appendChild(card);
  log.scrollTop = log.scrollHeight;
}

/* —— 事件绑定（一次）—— */
(function cpBind() {
  if (window.__cpInited) return; window.__cpInited = true;
  navBindChrome();            // v69：工作区标题栏按钮（病历单 / 人体模型 / 返回对话）
  navRestorePersisted();      // v69：刷新或重启后恢复上次工作区与当前病例标识
  var $ = function (id) { return document.getElementById(id); };
  $("cpClose").addEventListener("click", cpClose);
  $("cpBackBtn").addEventListener("click", cpClose);
  $("cpEditBtn").addEventListener("click", cpEnterEdit);
  $("cpSaveBtn").addEventListener("click", function () { cpSave(); });
  $("cpSaveBtn2").addEventListener("click", function () { cpSave(); });
  $("cpModelBtn").addEventListener("click", function () {
    NAV.returnFrom3d = "case";
    navSetWorkspace("3d");
  });
  $("cpConfirmBtn").addEventListener("click", cpConfirm);
  $("cpConfirmBtn2").addEventListener("click", cpConfirm);
  $("cpMoreBtn").addEventListener("click", function (ev) { ev.stopPropagation(); $("cpMoreMenu").classList.toggle("open"); });
  document.addEventListener("click", function () { $("cpMoreMenu").classList.remove("open"); });
  $("cpMoreMenu").addEventListener("click", function (ev) { ev.stopPropagation(); });
  $("cpRegenBtn").addEventListener("click", function () { $("cpMoreMenu").classList.remove("open"); cpRegen(); });
  $("cpHistoryBtn").addEventListener("click", function () { $("cpMoreMenu").classList.remove("open"); cpShowHistory(); });
  $("cpExportBtn").addEventListener("click", function () {
    $("cpMoreMenu").classList.remove("open");
    if (cpState.data && cpState.data.record && cpState.data.record.review_status !== "confirmed") {
      cpStatusFlash(t("导出正式病例前需医生确认：先点击「医生确认」", "Confirm by physician before export")); return;
    }
    if (bridge && bridge.caseRecordExport) bridge.caseRecordExport();   // Python 弹保存对话框
  });
  $("cpRejectBtn").addEventListener("click", function () { $("cpMoreMenu").classList.remove("open"); cpReject(); });
  $("cpMoreGroupHead").addEventListener("click", function () { cpState.moreOpen = (cpState.moreOpen === null ? true : !cpState.moreOpen); cpRender(); });
  $("cphClose").addEventListener("click", function () { $("cpHistoryLayer").classList.remove("open"); });
  $("cpHistoryLayer").addEventListener("click", function (ev) {
    if (ev.target === this) this.classList.remove("open");
  });
  // 字段行点击：非编辑模式记录当前字段（供「重新生成当前字段」），编辑模式聚焦
  document.getElementById("cpBody").addEventListener("click", function (ev) {
    var row = ev.target.closest(".cp-field");
    if (!row) return;
    var key = row.getAttribute("data-field") || "";
    cpState.curField = key || cpState.curField;
    /* 空字段可直接点击填写：自动进入编辑态并聚焦对应输入框。 */
    if (cpState.mode !== "edit" && row.querySelector(".cp-fbody.empty")) {
      cpEnterEdit();
      var input = null;
      document.querySelectorAll("#casePanel .cp-field").forEach(function (r) {
        if (r.getAttribute("data-field") === key) input = r.querySelector("textarea");
      });
      if (input) { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }
    }
  });
})();
