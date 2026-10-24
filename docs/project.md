# project.md — 项目记忆（肤智（DermAgent））

> 敏感值只记变量名，不记明文。

## 项目定位

肤智（DermAgent） — 皮肤科智能问诊与循证辅助智能体（医生决策支持演示原型）。
不面向患者诊疗；仅合成/脱敏数据；禁止自动处方/剂量/疗程；草稿须经医生确认/修改/拒绝。

## 交互形态与图

- interaction: **approval**（HITL durable stateful flow，暴露 hasStarted；首条 query 开题、之后 resume）
- 图：`src/app/graph.ts`（intake → screen →(ask_more)→ retrieve → analyze → doctor_review →(revise|finalize_report|case_closed)）
- 纯函数逻辑：`src/app/logic.ts`（computeMissing / detectRedFlags / retrieveCitations / sanitizeDraftCitations / classifyReview / 路由 / 兜底渲染）
- 合成知识库：`src/app/knowledge/kb.ts`；三合成案例：`src/app/knowledge/demo-cases.ts`（案例1 湿疹样 / 案例2 带状疱疹红旗 / 案例3 资料不足）

## 平台配置

- 平台 Agent #33296164；`agent.name: fuzhi3d-derm`；`systemPromptPath: prompts/derm-flow.md`
- 平台 systemPrompt / openingChatMsg：经 dev-engineer-toolkit 同步（源文件 `prompts/derm-flow.md` / `prompts/derm-opening.md`）

## 平台能力门禁记录（RAG 知识库）

搜索关键词与结果（`search-apis.sh`，2026-09-02）：
- 「皮肤|医学|知识库」→ 2 条：飞书知识库 Plugin（飞书 Wiki 搜索，非医学）、聪姜蒜同学 Workflow（通用知识库问答，非已审核医学库）——均不适用
- 「皮肤」「湿疹」「带状疱疹」「诊断」→ 0 条
- 「检索」→ 7 条：航班检索器、飞书多维表格、时间工具、古诗词、医美报告、职业规划——均不适用
- **结论**：平台无已审核授权医学知识库 → RAG 走本地合成演示知识库（app 层纯函数，无 fetch 包装、不替代真实 Knowledge 登记）；真实部署时在平台登记 Knowledge 后替换 retrieve 节点数据源。

## 环境备注

- 本机 Python 3.12（python.org 版）缺 CA 证书 → 调 dev-engineer-toolkit 脚本需 `export SSL_CERT_FILE=<certifi>/cacert.pem`（certifi 已装）。
- pnpm 装依赖；迭代期不 `pnpm build`；静态三连 = `pnpm typecheck && pnpm test && pnpm graph`。

## 验证方式

- 静态三连 ✅：`pnpm typecheck` + `pnpm test`（424 passed，含 derm-logic 24 例纯逻辑 + flow 5 例四案例 interrupt/resume 全链路）+ `pnpm graph`
- **§6.1 Schema 升级（2026-09-03）**：ConsultDraft 对齐参赛计划 §6.1（schemaVersion/dataCompleteness/confidence/symptomAssessment(status=basis)/medicationChecks/severity.level/redFlagEscalation/recommendedNextSteps/modelVersion/knowledgeSnapshot/generatedAt）；引用白名单覆盖结论内 citationIds；unknown/not_asked 语义保护；非法 JSON 受限修复一次（bespoke nodes/analyze.ts）→ 失败标记「未通过系统校验」；检索否定词感知（无疼痛/否认过敏史不误报）；案例4 无答案拒答内置。
- 平台真实链路（flow-debugger，2026-09-02/03）：
  - 案例1（新会话）：草稿（LLM 生成 + 引用 + 免责声明）→「确认」→ 正式报告 ✅ SSE PASS；日志 `[结论] 日志正常`
  - 案例2（新会话）：草稿含 RF-HZ-001/升级人工审核 ✅ →「修改：疼痛评估与眼科会诊」修订纳入 ✅ →「确认」→ 流式正式报告 ✅ SSE PASS；日志 `[结论] 日志正常`（tokenChunks=1895 真流式）
  - 案例3（新会话）：「补充：…」合并必填 → 草稿 ✅ SSE PASS；「拒绝」→ `flowStatus=done`，输出「❌ 医生已拒绝…不生成报告」（日志核验 ✅）
  - **§6.1 升级后回归（2026-09-03）**：案例1 草稿含 schemaVersion/symptomAssessment/redFlagEscalation/requiresDoctorConfirmation ✅ SSE PASS + 日志正常（questionChars=5653）；案例4 无答案拒答：草稿标注「知识库未覆盖/不足以判断」、不编造鉴别诊断 ✅ SSE PASS + 日志正常（questionChars=3395）
  - **超时调优**：§6.1 大 Schema 下平台慢模型 analyze 可能超 180s 默认长超时 → `config.model.settings.invokeLongTimeoutMs: 300000`；超时重试耗尽仍会优雅降级为规则稿（实测 873a 会话）
- **已知平台侧现象**（非本图缺陷）：无 LLM 的瞬时轮次（22–24ms 完成，如案例3 首轮追问、拒绝轮）SSE 侧 FINAL_RESULT 偶发丢失（debug.sh 收流挂起至超时）；runtime 日志证明该轮次行为完全正确。含 LLM 的慢轮次不受影响。演示时在预览面板可见消息历史。

## 交付物

- `src/app/state.ts` · `src/app/graph.ts` · `src/app/logic.ts` · `src/app/nodes/derm.ts` · `src/app/nodes/analyze.ts` · `src/app/knowledge/{kb,demo-cases}.ts`
- `prompts/derm-flow.md` · `prompts/derm-opening.md`（已同步平台并回读，无污染）
- `docs/derm-product-design.md`（15 章比赛模板）· `docs/data-license-register.md`（数据授权台账）
- `tests/derm-logic.test.ts` · `tests/flow.test.ts`（重写）· `tests/topology.test.ts` / `tests/flow-registry.test.ts` / `tests/flow-selection.test.ts`（更新）

## 阶段 C：Windows 客户端 MVP（2026-09-03 完成）

- `client/main.py`：PySide6 三栏工作台（九要素表单/脱敏照片上传 EXIF 清理/案例一键导入｜Three.js 3D 定位区域选择回填+截图｜§6.1 草稿卡片/确认·修改·拒绝/报告导出）+ 审计 JSONL + 断网降级
- `client/agent_client.py` 三模式：**mock（离线演示）** / **llm（直连大模型：URL/API Key/模型名三输入框，OpenAI 兼容 chat/completions，temperature=0；本地保留红旗/检索/引用白名单强制；非法 JSON 修复一次→llm-invalid 降级；网络失败→rule-fallback 降级）** / http（服务端桥接）
- `client/derm_logic.py`：TS 逻辑同构移植 + `normalize_llm_draft`（LLM 输出枚举收拢、引用白名单、severity/completeness 本地权威派生）
- `client/web/`：Three.js r128（vendored，MIT）程序化参数化人体（13 区域）+ OrbitControls + QWebChannel 桥
- 密钥管理：仅存本机 `client/data/settings.json`（gitignored，勿截图/勿提交）；不入库不入客户端安装包
- 验证：pytest **26 例**全过（含 LLM 直连四路径：白名单剔除+枚举收拢 / 修复一次 / llm-invalid 标记 / 断网降级）+ 离屏 UI 烟雾 + llm 模式 UI 走查（设置持久化）
- **3D 真机渲染验证（client/tools/verify_3d.py，QtWebEngine 实渲染）**：**真实人体模型 Cesium Man（CC BY 4.0，内嵌 data URL 加载，file:// 零 CORS）**：glbLoaded=true / triangles=5576 / 13 区域点击拾取 + 选中标记 ✓；正/背面截图 `client/data/verify3d_front.png`、`verify3d_back.png`；GLB 失败自动回落程序化人台。坑：①file:// 下 ES Module 与 XHR 均被 Chromium CORS 阻止 → vendor 用 r147 UMD + GLB 内嵌 data.js；②经典 GLTFLoader 挂在 THREE.GLTFLoader 命名空间
- 待办：Windows 机 PyInstaller 打包（不可交叉打包）；3D 面板在演示机上的手感调优

## 新计划转向（2026-09-04）：肤智（DermAgent）三端一致智能体产品

- **契约升级 v1.1（§6.3）**：`workflow_state` 状态机（awaiting_follow_up/escalated/awaiting_review/approved/rejected/blocked）、`blocking_reason`、`follow_up_questions`、`red_flag_escalation.is_blocking`、`safety_policy_version(sp-1.0)`、`review_status`；全链 snake_case。
- **单一权威实现 `shared/derm_core.py`**：server 与 Windows 客户端共用；TS `src/app/logic.ts` 同构（v1.1）。
- **可信服务层 `server/app.py`（FastAPI, :8765）**：case 会话状态机持久化（sessions.json）、规则管线 + 服务端 LLM（LLM_BASE_URL/LLM_API_KEY/LLM_MODEL env，Key 不出服务端）、引用白名单、审计 JSONL、静态托管 webui/ 与 3D 面板 /web。
- **Web/PWA 主评审入口 `webui/`**：响应式 + manifest + SW（Android 可安装 = Android 端）；区域三形态（文字搜索/分区列表/3D postMessage）；跨端续办（case_id 列表续办）。
- **Windows 客户端**：modes = mock（离线演示资产）+ server（可信服务层）；**llm 直连模式已按新计划移除**；渲染 v1.1 snake_case。
- **测试**：TS 412/412 + Python 20/20（tests-server 覆盖四案例状态机、引用白名单、跨端续办、三端快照一致）。
- **平台**：systemPrompt 已更名「肤智（DermAgent）」并同步 v1.1 契约（回读 ✓）。
- 修复：server `_lock` 非重入死锁（review/supplement 嵌套 _store_sess）→ RLock；报告头 workflow_state 未随确认更新的缺陷。
- 待办：Android 原生体验增强（相机/图库）、四案例三端验收卡、视频、发布包、女娲作品链接冻结。

## 三端 UI 统一重构（2026-09-04）：左历史对话 / 中 3D 模型 / 右症状显示

- **Web（webui/）**：三栏布局——左「历史对话时间线」（user/agent/system 条目 + 可展开详情/引用点击溯源 + 跨端续办下拉），中「3D 真实人体」（iframe 内嵌 /web 面板，postMessage 回填区域），右「症状九要素表单 + 医生审核 + 导出」；零 JS 错误，浏览器实测案例2 → 确认 → 辅助报告全链路 ✓
- **Windows（client/main.py）**：同款三栏——左 QListWidget 历史记录 + 详情 QTextBrowser，中 QtWebEngine 3D，右症状表单 + 审核/导出；Fusion 深色统一主题
- app.js 过期元素引用（btnStart/btn3d/quotePanel 等）导致监听挂载中断的缺陷已清扫（浏览器 console 验证零错误）

## 交互流改版（2026-09-04）：对话式三栏

- 中间 = 3D 模型 + **下方输入框**（案例1–4 快捷键填入触发词「案例N」+ 照片上传 + 提交按钮）；
- 右侧 = **症状显示区域**：空态提示，仅在提交后渲染智能体分析结果（v1.1 草稿/红旗/引用/审核按钮/报告）；
- 左侧 = 历史对话记录：仅提交后产生条目（user/agent 时间线 + 可展开详情 + 引用点击溯源）。
- 修复：Web 端 app.js 顶层绑定渲染后元素导致的初始化中断、案例快捷键填入值错误（"2"→"案例2"）、esc 函数缺失；浏览器端到端回归（空态→提交→历史/结果出现→确认→报告）全过。

## 多区域症状支持（2026-09-04）：一人多处痛

- 3D 点击已选区域 = 取消、未选 = 加入（多选），标记池每处一枚红球，标签「已选（N 处）：A、B」；
- 上报契约改为全量列表（Qt 桥 "multi|A、B" / postMessage {regions:[…]}），Web 中间输入框与 Windows 身体区域字段均回填合并值；
- 服务端/离线管线按病例全文拼接做红旗筛查与知识检索（、分隔多区域天然支持），pytest 用例 `test_multi_region_body_pain` 覆盖。

## 3D 主展示区升级（2026-09-04）：Michelle 真实人体 + 19 细分区域

- 默认主模型：**Michelle.glb**（three.js 官方示例角色，3.2MB，写实人形）；加载链 Michelle → CesiumMan(CC BY 4.0) → 程序化人台；
- 可选区域 13 → **19**：头面部/颈部/胸部/腹部/上背/腰部（下背）/会阴（隐私）/左右上臂/左右前臂/左右手/左右大腿/左右小腿/左右足；命中盒按包围盒分数带自动布局；
- 多区域多选保持（点击切换 + 标记池 + 全量列表上报）；Web 布局中栏 3D 为主展示区；
- 验证：QtWebEngine 真机 glbLoaded=true / triangles=28946 / 19 组命中盒 / 多选 2 处标记 ✓；Web 端实测回填「身体区域：腹部、大腿（左）」。

## 3D 严重 bug 修复（2026-09-04）：Michelle 黑屏/不可见

- 症状：Michelle 载入后画面全黑（toDataURL 全背景），用户看到空场景。
- 根因（py-spy 式逐层诊断）：①Mixamo→glTF 骨架自带巨大缩放偏移，蒙皮顶点实际渲染在 z≈-308（geometry Box3 是假象，真实位置须按骨骼实测）；②模型躺姿存储（需扶正）；③材质 metalness=0.5/roughness=1 且无环境贴图 → 全黑。
- 修复：boneBox() 按 65 根骨骼实测世界位置做「躺姿扶正 → 身高 5.0 缩放 → 水平居中 → 脚底落地」；材质强制 metalness≤0.05/roughness≤0.85。
- 验证：骨骼包围盒 x±2.3 / y-2.5..2.5 站立居中；画面 nonBg 7.5%、肤色像素 4.6%；多选 2 处标记与回填 ✓。

## Xbot 紧身衣模型 + 可删除多选（2026-09-04）

- 按用户反馈：①已选疼痛部位可删除（再点同部位 toggle 移除；3D 工具栏新增「清空选择」）；②换**紧身衣**模型便于看清部位——默认主模型改为 **Xbot.glb**（three.js 官方示例角色，深色紧身连体，49952 三角面），链：Xbot → Michelle → CesiumMan → 程序化人台；
- 缓存治理：webui iframe src 与 main.js 加版本号（?v=9/?v=8），main.js 顶层绑定加空值防护（元素缺失优雅跳过）；
- 已知环境现象：MCP 调试 Chrome 今晚页面频繁崩溃（连 iframe 文档都不加载），与产品无关；QtWebEngine 真机验证全绿（Xbot triangles=49952、19 区域、多选 2 处、toggle 删除 ✓、截图 120KB）。

## 3D 黑屏二次修复（2026-09-04 深夜）

- 根因：一次补丁把 `client/web/index.html` 误写为**空文件**（s[:0] no-op 写入），客户端 3D 面板整个页面为空 → THREE 未定义、模型/区域全无。
- 修复：重建 index.html（工具栏含「清空选择」、UMD 脚本链含 GLTFLoader/CesiumMan.data、main.js?v=9 破缓存）。
- 验证：QtWebEngine 真机 glbLoaded=true（Xbot 49952 tris）、19 区域、多选/删除标记 ✓、截图 120KB。
- 教训：批量补丁脚本必须断言写入后的文件内容；本次已对所有后续补丁启用 assert 校验。

## 比赛材料收口（2026-09-05）

- **git 纪律**：按用户指令回滚上一轮全部未提交改动后重新实现；`.gitignore` 补 `server/data/`、`__pycache__/`、`.venv-tmp/`（运行时脏数据不再入库）。
- **命名统一**：全部用户可见命名改为「肤智（DermAgent）」（README/client README/project.md/设计报告/flow 注册 userLabel/config description/derm-opening/webui 标题与 h1/Windows 窗口标题/3D 面板标题），grep 残留=0；平台 openingChatMsg 经 dev-engineer-toolkit 重传并回读 ✓（systemPrompt 非空、无旧名、无 Skill 污染段）。
- **新增文档**：`docs/cross-platform-acceptance.md`（四案例三端验收卡，映射自动化断言）· `docs/demo-assets-checklist.md`（发布/离线资产清单，视频/发布包/女娲链接冻结均为 TODO）。
- **服务端**：`_snapshot` 补 result_version/model_version/knowledge_snapshot/safety_policy_version；`_bump_result_version` 在 screening 产出/确认/拒绝/修订四处递增（r0→r1…）。
- **测试**：tests-server 9→16 例（新增：结果版本递增与三端元数据一致、红旗不可绕过（supplement/modify 均不解除）、unknown/not_asked 不转 absent、确认前不出报告、引用属于当前快照、用药无剂量疗程）。注意：`normalize_llm_draft` 的 citations 块设计为「恒等于本次检索快照」，结论内引用才做白名单剔除——测试按此契约断言。
- **验证**：pytest 16/16（client/.venv）· vitest 412/412 · typecheck ✓ · pnpm graph 12 节点 15 边 7 条件边 ✓ · QtWebEngine 3D 真机 Xbot triangles=50568、regionGroups=19、点击拾取「已选（1 处，再点可删除）：腹部」✓ · HTTP smoke（r1→r2 递增/红旗保留/快照一致/命名生效）✓ · 文字兜底（无 3D 完成 body_region→检索→草稿）✓。
- **遗留 TODO**：Windows PyInstaller 打包、4:30–5:30 演示视频（正式+无网备份）、女娲作品链接冻结、四案例三端人工回归记录（见验收卡回归表）。


## 3D 点击命中错位修复（2026-09-06）

**根因**：旧实现用 19 个固定透明命中盒（按 Xbot 垂直手臂假设摆放），3d66 模型是 A-pose（手臂下斜约 20°），点击躯干侧缘（|x|>0.26 归一化）会误中垂直放置的上臂圆柱命中盒 → 「点胸显示手臂」。

**方案**（`client/web/main.js`）：
1. `registerHuman` 给全部可见网格标 `userData.pickable`，模型装配后记录实时包围盒 `fitInfo`（Box3）。
2. `onPick` 改为 raycast 可见网格取最近交点，命中点经 `regionFromPoint(point)` 映射：以 fitInfo 归一化（ny 0..1 足→头，nx/nz -1..1），按人体比例分段到 19 区域；固定命中盒降级为兜底。
3. 臂部阈值按真实网格实测校准（上臂 |x| 0.62–0.75/ny 0.58–0.66，前臂 0.78–0.90/ny 0.56–0.62，手 0.92+/ny 0.52–0.56），适配任意姿态模型，不依赖 Xbot 固定坐标。
4. 19 区域聚合不变（一个区域含多个 mesh），多选/再点删除/清空、手动导入、文字列表兜底均保留。

**回归**（`client/tools/verify_3d.py`，QtWebEngine 真机）：胸/头/左上臂/右前臂/左手/右大腿/左小腿/右足 8 点射线断言全过；多选2→删除1→清空0 过；截图 `client/data/verify3d_front.png`/`back.png` 正常。浏览器端到端（127.0.0.1:8765）：点胸→「胸部」，输入框联动「身体区域：胸部、上臂（左）；」。`webui/index.html` iframe 缓存参数同步 v=13。

## 真实人体解剖语义映射（2026-09-06）

**目标**：3D 点击从 19 粗区域升级为真实解剖语义命中（左右眼、五指逐指、鼻、唇、趾等），19 区域只作聚合兜底。

**GLB 语义盘点**（`vendor/models/Custom3d66.glb`，sha256 前缀 1d65241f）：17 个 mesh 均为流水号命名（Obj3d66-19298115-N），**无骨骼、无动画、无解剖语义名称**；可用的语义信息仅有：左右眼各 3 个独立 mesh（眼球/虹膜/高光）、鼻独立 mesh、眼镜框 2 个 mesh、头发 4 层、四肢+躯干混在 2 个大 mesh（-6-141_4 腿足、_5 臂手）→ 五指/唇/趾只能靠几何分析。

**交付物**：
1. `client/web/anatomy-labels.json`（新增）：22 个解剖条目（anatomical_label/中文显示名/mesh_names/世界坐标/左右侧/置信度/provenance 来源标注）+ 2 条不可靠声明（耳被头发覆盖、第 2–5 趾网格分辨率不足）+ 镜框遮挡说明 + 匹配策略说明。五指锚点由指尖 z-gap 聚类实测得出；z_band 区间按相邻指间隙中点划分；锚点表通过「模型文件名 + fitInfo 尺寸 2% 容差」双校验后才启用。
2. `client/web/main.js`（v14）：`loadAnatomyConfig`/`validateAnatomy`/`anatomyFromPoint`/`glassesFallback`；`onPick` 解剖层优先（锚点 ≤3.5cm 直接命中；指间过渡两点距离差 <5mm 报「无法可靠区分」置信 0.3，不伪造单指）；19 区域降级为回退。`registerHuman` 重载模型时清空旧选区（世界坐标标记随模型失效）。
3. `client/tools/verify_3d.py`：新增 17 项解剖断言（左右眼/鼻/上下唇/左右手五指/左右趾聚合）+ 精细标记 toggle/多选/视角稳定性断言。

**调试中修复的两个真 bug**：① 单候选分支缺失——只落进一条 z_band 而无锚点命中时 pick 为 null；② tip_x 符号存反——左手指尖 x 存成正值，导致侧别过滤把左手样本判给右手条目（左右镜像误标）。

**回归结果**：指尖核心区 50/50 样本全部命中正确手指；QtWebEngine 17 项解剖断言 + 8 项 19 区域断言 + 标记行为断言全过（`client/data/verify3d_report.json` anatomyActive=true, failures=null）；pytest client 11/11、server 16/16；浏览器端到端（127.0.0.1:8765）：整数像素真实点击左眼/左手食指/鼻三处共存（markers=3），视角切换前后标记世界坐标逐字节一致，再点食指单点删除、清空归零。正视图中指/无名指部分像素被相邻指遮挡属几何事实（换左右视角即可命中，已验证）。

**诚实降级**：耳（头发遮挡无独立几何）、第 2–5 趾（单趾顶点 <15）无法可靠识别，UI 报置信度 0.5–0.6 的聚合标签或归入不可靠清单，不伪造精细结果。

## 真实命中 + 可校准标注（2026-09-06 第二轮）

**需求**：点击输出真实 Raycaster 命中数据；GLB 无语义部位时用人工标注校准；无依据部位显示「未标注」，禁止猜测为手部/19 区域。

**实现**（`client/web/main.js` v15 + `web/anatomy-calibrations.json` + `client/main.py` 桥接）：
1. `recordHit`：每次点击记录 `{mesh_name, node_name, face_index, world_xyz, local_xyz, uv, normal, distance}`（本 GLB UV 返回 null，如实保留）。
2. `anatomy-calibrations.json` + localStorage 双存储：键 = `mesh名:faceIndex`，含 local_xyz 邻域 2cm 复用；Qt 桥 `onCalibrationSaved` 落盘 `client/data/anatomy-calibrations.saved.json`，`promptLabel` 用 QInputDialog（QtWebEngine 的 window.prompt 不可靠）。
3. 命中分级改为：标注模式录入 → 人工校准标签 → 锚点表语义 → **「未标注（mesh 面#N）」**；19 区域不再作为点击兜底（`regionFromPoint` 仅保留给程序化人台命中盒路径）。
4. 眼锚点容差 5.5→7.5cm：真实点击落在眼睑表面（脸 mesh）时距眼球锚点可达 5.5cm+，旧容差漏判为未标注；7.5cm 覆盖眼睑带且不误吞全头（对侧眼 d=0.19 不命中）。

**回归**（`tools/verify_3d.py` 新增断言）：真实命中字段完整性（mesh/face/world/local/normal）、未标注语义（躯干点报「未标注（…面#1380）」而非 19 区域名）、校准标签同面+邻域复用、原 17 项解剖语义 + 8 项区域 + 标记行为全过。pytest 27/27。浏览器端到端：左眼命中 mesh -7-343 报「左眼（含虹膜）」、躯干报「未标注」、标注「腹部观察点」后同点复用 ✓、左眼+左手食指多点共存、视角切换标记稳定 ✓。

**语义盘点结论**：GLB 无骨骼/无解剖命名，眼球有独立 mesh（-7-343 等 6 个）→ 眼/鼻可自动识别；五指靠几何锚点（置信 0.85）自动识别；第 2–5 趾、耳无法自动识别 → 报「未标注」或聚合标签，可用标注模式人工校准。

## 自动解剖分区 + 少量抽查校准（2026-09-06 第三轮）

**需求**：GLB 无骨骼/无语义节点时不得退回粗略 19 区域，也不得要求用户逐部位手工标注——按姿态/包围盒/左右对称/几何拓扑自动生成细粒度解剖区域，再加 4–8 点抽查校准向导。

**实现**（`client/web/main.js` v16）：
1. 正式语义检查 `client/tools/inspect_glb_semantics.py`：读取 skins/bones/nodes/meshes/materials，检测解剖词命名 → 结论 `has_skeleton: False, semantic_node_names: None`（17 mesh 全流水号）→ 走几何路线（骨骼识别路线按需求保留，有骨骼模型时优先）。
2. `zoneFromPoint` 自动分区引擎：fitInfo 归一化（ny 0=脚底→1=头顶、nx ±1、nz ±1）+ A-pose 臂比 r=(|nx|-0.62)/0.38。30+ 细粒度区域：头顶/后脑/额/面部/颞/耳颊区、颈、肩、肩带、上臂/肘、前臂/腕/掌、指、胸、腹、腰背、背（上）、臀/髋前/会阴、大腿/膝/膝外侧带/小腿、踝、足/趾。每项输出 confidence（0.35–0.8）+ provenance（ny/ax/nz 证据）；置信 <0.5 显示「未能可靠识别（…）」。
3. 校准向导 `startWizard/wizardNext/applyWizardResult`：7 步抽查（左眼/右眼/左中指/右中指/左脚/右脚/胸，符合 4–8 点要求）→ 落点距预设 <6cm 确认、否则记录纠偏 → 对侧按 x 镜像自动推得（只纠一侧也能整对生效）→ 写入 ANATOMY 运行时锚点副本 + localStorage `derm_wizard_applied` + Qt 桥落盘。未落在模型上的点击给出「请重新点击」提示。
4. onPick 命中分级：标注模式 → 人工校准 → 向导锚点/出厂锚点 → 镜框回退 → 自动分区（低置信加「未能可靠识别」前缀）→ 「未标注（mesh 面#N）」。

**回归**：
- `tools/verify_3d.py` 新增 v16 断言：18 项型带区域（颈/肩带/胸/腹/背上/腰背/臀/大腿/膝/小腿/踝/足趾/上臂/肘/前臂/腕/掌/头顶 id|标签逐项核对）+ 向导 7 步（右中指纠正 +0.08x 写入、对侧左中指镜像 −1.9271、provenance「校准向导纠正/对称修正」、localStorage 持久化、耳颊区低置信「未能可靠识别」提示）。全过，`data/verify3d_report.json` failures=null。
- 22 点真实点击回归（QtWebEngine，surface-probe + ±3px 像素网格）：左右眼/鼻/唇/左右手指级/左右足/颈/胸/腹/大腿/上臂/前臂/肩带/背上/腰背/左臀/小腿/踝/膝 22/22 命中。
- 标记行为：多点共存 2、视角切换世界坐标逐字节稳定、同点再点删除、清空归零。
- pytest client 11/11 + server 16/16。

**测试点修正记录（非引擎 bug）**：thighR/shoulderL/臀/小腿外侧 4 处初测未命中均为探测坐标悬空（双腿间隙空气、肩峰实际 nx≈−0.35 而非 −0.55、臀区真实表面 ny 0.36–0.44），射线扇扫描定位真实表面后全部命中。足跟后视点落在小腿/足趾聚合带（几何正确）。

## 移除 19 区域 + 列表 × 删除 + 全表面精细部位（2026-09-06 第四轮 v17）

**需求**：删除所有 19 身体区域的点击命中/映射/限制/兜底；GLB 全部可见表面可 Raycaster 点击；输出细粒度部位标签（左眼球/右手中指/掌/前臂/胸等），3D 模型仅是位置选择器；已选列表每行独立 × 删除（不再点模型同点删除），仅清空全部需确认；证据不足明确报「该位置无法从当前模型可靠识别」。

**实现**（`client/web/main.js` v17 + `web/index.html` + `webui/app.js`）：
1. **删净 19 区域**：`REGIONS` 表、`regionFromPoint` 映射、19 块隐形命中盒（buildHitboxes/rebuildHitboxes）、onPick 命中盒兜底分支、`highlightRegion` 的 REGIONS 依赖，全部移除。GLB 可见表面直接 Raycaster 命中（pickable 网格 22 个，全部三角面可选）。
2. **命中分级（无 19 区域参与）**：人工校准标签 → 解剖锚点（真实交点+三角面+mesh 名+左右侧 → 左右眼/五指逐指/趾聚合/唇/鼻）→ 几何型带分区（30+ 区域，confidence+provenance）→ 置信 <0.5 或无证据 → **「该位置无法从当前模型可靠识别（疑似…，置信 …）」**，禁止猜测/错误映射。
3. **列表 × 删除**：`selectedRegions` 加递增 uid；`renderSelection` 渲染 `#selList`（每行 label + 独立 × 按钮）；`removeSelected(uid)` 立即删列表项+3D 标记+上报数据，不影响其他条目；同点再点模型仍为 toggle 删除（防误累积）；「清空全部标记」`clearAllConfirmed` 带 confirm，取消则保留——这是唯一需要确认的删除操作。空列表也上报（WebUI 同步清空输入框前缀）。WebUI 输入前缀改「皮损位置：」并兼容清洗旧「身体区域：」。
4. Qt 桥 `onRegionSelected("multi|…")` 契约不变（全量列表上报）。

**回归**（`tools/verify_3d.py` v17 断言全过，`data/verify3d_report.json` failures=null）：
- 19 区域删净：`typeof REGIONS === 'undefined'`、`typeof regionFromPoint === 'undefined'`、命中盒为空；
- 17 项解剖语义（左右眼/五指/鼻/唇/趾）+ 18 项型带区域 + 向导 7 步镜像 + 真实命中字段 + 校准复用全过；
- **3 点选→列表 × 删中间→余 2**：列表行/可见标记/uid 三者同步为 2，剩余标签「左眼（含虹膜）」「右手中指」，已删 uid 再删返回 false，两端 pos 逐字节不变；
- 清空确认：confirm 文案「确认清空全部 N 个已选位置标记？」，取消保留、确认清零；
- 「无法可靠识别」：耳颊点击显示「该位置无法从当前模型可靠识别（疑似左耳颊区…置信 0.35）」。

**浏览器 E2E**（Chrome 真实页面 127.0.0.1:8877）：左右眼点击 ✓；左视五指逐指全对（前视拇指遮挡食指为几何事实，surf 探测证实）；掌区表面点均落在指根 3.5cm 内 → 报更精细的指级标签（zone=palm 由型带兜底，非错误）；三点选择→× 删中间→余 2（列表/标记/坐标三者同步）✓；视角切换+缩放往返标记 pos 逐字节稳定 ✓；清空确认/取消分支 ✓；列表 UI 截图核验（每行 × 在位）。pytest client 11/11 + server 16/16。

**诚实降级**：A-pose 掌面几乎被指根覆盖，掌区点击倾向报指级标签（更精细，非错误）；纯「掌」标签可经标注模式或校准向导补充。

## 连续精确位置选择器 v18（2026-09-06 第五轮）

**需求**：3D 模型必须是连续、精确的位置选择器——递归 Raycaster 命中真实渲染网格（禁大区域/碰撞盒/屏幕判断）；每次点击保存完整真实命中数据；输出「解剖区域+表面方位+相对位置+局部坐标」；同区连点必须独立记录绝不合并；缺少语义时如实提示；列表可展开/备注/× 删除；调试模式开关。

**实现**（`client/web/main.js` v18 + `web/index.html` + `webui/index.html`）：
1. **递归命中**：`raycaster.intersectObject(modelRoot, true)`，过滤 `userData.noPick`（标记/辅助物），取距相机最近交点（视觉最前方）。19 区域命中盒 v17 已删，无任何代理/兜底。
2. **完整命中记录**（recordHit v18）：mesh_name / node_name / node_chain（Mesh→根祖先链）/ bone+skin+weights（本 GLB 无骨骼→如实 null）/ instanceId（非 InstancedMesh→null）/ face_index / world_xyz / local_xyz（Mesh 系）/ **model_local_xyz（modelRoot 系，重载恢复用）** / uv（该模型无 UV→null）/ 世界法线 / distance / model_transform（旋转/缩放/平移）/ object_ref。
3. **描述引擎**：`surfaceAspect`（法线五向分解：前/后/内-外/上/下，次分量 ≥0.55 → 组合方位「前外侧」，反向互斥杜绝「后前侧」）+ `limbRelPos`（zoneFromPoint 附带 relT：近端/中段/远端+偏向）+ `boxRelPos`（区域包围盒上中下/偏内偏外偏前偏后）→ `describeHit` 拼装「左大腿，前外侧，中段偏远，局部坐标 (-0.14, 0.43, 0.00)」。
4. **逐点独立**：`addRegionSelection` 每次点击 push 新记录（uid 递增），无 toggle/合并/去重；同点连击=多条记录（toggle 语义已删）。
5. **列表 UI**：每行「完整描述 + ▸ 展开 + ✎ 备注 + × 删除」；展开显示精确坐标/命中 Mesh/Node 链/faceIndex/instanceId/uv/法线/骨骼/模型变换/相机距离/命中等级；备注经 prompt（≤200 字）实时刷新；× 按 uid 立即删除；「清空全部」唯一需确认。
6. **调试模式**：`toggleDebug` 按钮，开启后 regionLabel 附命中 Mesh/faceIndex/交点，展开含命中等级（校准/锚点/型带/不可识别）；正式模式隐藏。
7. **重载恢复**：`restoreMarkersAfterReload`——同模型重载按 model_local_xyz 贴回（world_xyz 刷新为新摆放）；非同模型记录移除（局部坐标无意义，不伪造）。
8. **语义不足**：文案升级为「当前模型缺少该部位的精细语义（疑似…，置信…）」，仍保存精确坐标与方位。
9. WebUI 同步不变（regions 紧凑 + regions_full 完整上报）。

**回归**（`tools/verify_3d.py` v18 全过 + `data/verify3d_report.json` failures=null）：
- 完整命中字段 ✓（mesh/face/world/local/normal 全在，uv=null 如实）；校准复用 ✓；
- 真实 onPick 链 3 点（左眼/胸/右大腿）→ × 删中间 → 余 2（行/标记/uid 同步）✓；
- 左大腿 5 连点 → ≥4 条独立记录、uid 互异、描述各异（外侧近端偏中/前外侧中段偏远/左膝前侧…）✓；
- 「缺少精细语义」耳颊点如实提示 ✓；17 项解剖语义 + 18 项型带 ✓；向导 7 步 ✓；
- 视角往返（前/左/后）标记 pos 逐字节稳定 ✓。
- **左视五指**（真实点击 ±5px 网格）：拇指/食指/中指/无名指/小指全对，含方位描述（外前侧/前外侧/外侧/外上方）；掌指交界→指级；腕前臂交界→「右前臂，上外方，近端」。
- **浏览器 E2E**（Chrome 8878）：大腿 5 连点独立 uid、× 删第 2 条余 [1,3,4]、备注「左大腿外侧皮损」写入、展开详情含「模型局部坐标」、旋转+缩放 pos 稳定、调试开关生效；截图 `e2e_v18.png` 列表三行 + 标记贴体表。
- **重载恢复**：模拟重载位移 +1.5 → 恢复后 ml 全同、pos.y 精确 +1.5、mesh/face 保留、标记 2 可见；非同模型恢复清空 ✓。
- pytest client 11/11 + server 16/16。

**诚实说明**：正面视角 A-pose 五指互相遮挡（拇指 z 最前），逐指独立点击需左/右视——几何事实非缺陷；该 GLB 无骨骼/Skin/UV，bone/skin/weights/uv 字段恒 null（如实记录，不伪造）；大腿 7 点中 1 点因射线掠过腿间隙 miss（测试瞄准问题，实际 UI 用户点击可见表面必然命中）。

## 连续精确位置选择器 v20（2026-09-07 第七轮）

**需求**：修复三问题——①指关节误识别 ②胸部误识别为腹部 ③单次点击命中范围过大。约束：不恢复 19 区域限制、不用粗略包围盒/屏幕坐标/大范围容差；只有点击标记位置、精细标签与实际可见表面一致才算修复。

**根因定位**：臂轴投影 at 失真——`AX_S=(0.92,1.40,0.05)→AX_F=(1.85,0.04,0.10)` 的线性投影在手部严重弯折（at=0.88 对应世界 |x|≈1.74），旧 forearm(at<0.88)/wrist(0.88-0.93)/palm(0.93-0.975)/finger(≥0.975) 分带把掌块（x 1.52-1.65）和指根整体吞进 forearm/wrist——这是"点指根报前臂/腕"的数学根因。胸腹侧旧表 y=0.62 单直线边界把中腹上部归胸。

**实现**（`client/tools/build_face_semantics.py` v20 重生成 `web/face-semantics.json`；`client/web/main.js` 仅注释/版本号微调）：
1. **指轴归属替代 z_band 硬切**：每指定义 xz 平面轴线 MCP→TIP，点到轴距离 ≤30mm（FINGER_HALF_TOL，半指厚+指缝）归属该指，轴向参数 t（0=MCP 1=tip）三分指节；指根随扇形展开偏离锚点带的问题由轴模型解决。实测分界：腕 |x| 1.44-1.52 / 掌块 1.52-1.65 / 指部 ≥1.65；拇指短、MCP 在 x≈1.70（比四指更靠远端），独立 THUMB_MCP_X 分界。手部分类**必须置于 at 分带前**（否则掌块指根被 forearm/wrist 抢走——代码注释含证明）。
2. **每侧 22 个新 zone**：mcp/pip/dip/ip（拇指无 pip/dip 有 ip）+ proximal/middle/distal_phalanx ×5 指；标签如「右食指掌指关节（指根）」「左小指远端指关节」。
3. **胸腹细分 14 zone**：胸骨(y 0.92-1.24,|x|<0.075 微凸)/胸乳区(0.84-1.24)/左右胸外侧(|x|>0.42)/胸下缘(0.72-0.78)/左右肋缘(0.78-0.96 外高内低斜带)/剑突(0.84-0.92)/上腹(0.42-0.78)/胸腹交界带(0.78-0.84)——交界带 conf 0.4 + note「胸/腹解剖分界（膈）不可见于体表，交界带内无法从网格可靠区分」，诚实不猜测。
4. **单面精确命中不变**：onPick 优先级链 人工校准 → fsem 逐面表（finger_zone_* conf0.6 让位手指锚点）→ 锚点 → 型带 → 不可识别诚实提示；无容差/无吸附/无多面投票。
5. 构建输出：22 mesh、18496 segments、180 labels、fallback=0、全表无空洞（gaps=0）。

**验收**（`/tmp/v20_accept.py`，QtWebEngine 真实 onPick 链，failCount=0）：
- **56/56 指节关节精确命中**：左右手 ×5 指 ×(mcp/pip/dip/ip) + 近/中/远节，id 精确匹配 `fsem:zone`（如「左拇指掌指关节（指根）」「右食指近端指关节」「左小指远端指关节」「右中指中节」）；探针+±3px 像素网格重试（细小指面 1 像素投影偏差会落邻面，属测试瞄准非产品容差）。
- **7/7 胸腹点**：胸骨/左胸/右胸/剑突/右肋缘/上腹全中；胸腹交界带点输出「胸腹交界（边界位置，当前模型语义不足）」含诚实字样。
- **连点独立**：左大腿 4 连点 + 右胸 3 连点（≥2.5cm 间距），uid 互异、坐标全不同、无合并扩散。
- **× 删除**：7 条连点删中间 1 条 → 记录数-1、该 uid 消失、可见标记数同步（marker 池复用机制：多余标记置 visible=false）。
- **既有回归 `tools/verify_3d.py` failures=null**（对齐 v20 表的两处断言更新：①afterXDelete 眼点在 v20 表下先命中「眼镜框」——含「眼」字，符合真实遮挡关系；②原「耳颊未标注点」v20 表被 head_side conf0.6 诚实覆盖，语义诚实断言改验胸腹交界带「语义不足」提示；另 realClickWorld 网格先试 (0,0) 再试邻域、报告补 faceSemActive 字段）。
- **pytest 27/27**（client 11 + tests-server 16）。
- **浏览器 E2E**（Chrome 127.0.0.1:8765?v=22）：右食指掌指关节/胸骨/上腹部真实点击 3/3 命中 fsem id；列表 × 删除后「未选择区域」。

**诚实说明**：face-semantics 表为**几何聚类推断**（顶点位置+法线+命名语义），非 GLB 原生标注——该 Custom3d66.glb 无骨骼/Skin/动画/UV，Bone/Skin 权重字段恒 null（如实记录）；A-pose 四指互相贴靠，指缝处点击可能命中邻指对应半指厚的轴归属边界（30mm 容差是几何必然，非吸附）；拇指指间关节与掌指关节的区分依赖 THUMB_MCP_X=1.70 实测拐点。

## v21：拇指五区细分 + 背面下半身 + 臀腿边界 + Codex 风格统一提交 UI（2026-09-07 第八轮）

**需求**（六部分）：①大拇指左右手区分+掌骨/近节/远节/指间关节/指尖五区；②背面下半身（臀/臀沟/臀腿交界/大腿后侧/膝窝/小腿后/脚跟）按真实可见表面命中；③臀腿边界重定义（大腿不再误归臀）+「左右侧+区域+方位+近端中段远端+局部坐标」五元组输出+交界带语义不足诚实标注；④单次点击=最近可见三角面（无容差/吸附/扩散）；⑤点击→底部输入框位置标签，与文字/文件合并为一条结构化请求提交；⑥调试模式+实测验收。

**实现**：
1. **拇指五区**（`build_face_semantics.py`）：每侧 5 zone（metacarpal/proximal_phalanx/distal_phalanx/ip/tip），THUMB_MCP_X=1.70 分掌骨/指骨、THUMB_IP_FRAC=0.55 分近节/远节（拇指单关节）；26/48/125/42/133 面/侧。
2. **臂部五区**：upper_arm/forearm/elbow/elbow_tip/cubital 分带（at 轴向参数）+ `force_over` 机制从早先躯干侧带夺回被抢面 + **距臂轴守卫**（柱面 dist<0.25，侧腰 dist≈0.39 高置信分离）。axn 口径勘误：axn=|x|/1.8491（half=1.849），v20 的 axn>0.55 实为 |x|>1.017——v20 的 shoulder_up mask 恒空（幽灵标签），改用 |x|+y+法线组合带。
3. **幽灵标签清零**：v20 表 forearm/shoulder_up/heel_back/elbow/chest/shoulder/finger_zone 均 0 面 → v21 全部修复/删除，174 labels 全部有面、18788 segments、1295502 面全覆盖 0 空洞。
4. **heel_back**：`put(foot & sg & nzf<-0.6)`（跟腱-跟骨后突弧面，法线朝后与 sole/dorsum 互斥）。
5. **relT 修复**（`main.js` fsem 命中注入）：表 yband 是构建器归一化系（身高5，y∈[-2.5,2.5]），与 `model_local_xyz`（GLB node 0.01 缩放原始系）**不同坐标系**——初版误用 model_local 导致 relT 恒 clamp 0 全报「近端」；改为 world_xyz（registerHuman 归一化后世界系=表系）。cat==="limb" 守卫防躯干污染。limbRelPos 五段：近端/近端偏中/中段/中段偏远/远端。
6. **臀腿交界**：gluteal_fold y -0.942..-0.842 背面 z<-0.06（唯一可见标志）；thigh_hip_border 76 面/侧 conf 0.55 + note「体表不可精确判定，交界带内不强行二分」；thigh yband 下界从 -0.742 收到 -0.943（大腿不再吞臀）。
7. **Codex 风格 UI**（`index.html` 重写 + `main.js` 增 composer 框架）：顶部紧凑工具栏（品牌+视角六钮+已选/历史抽屉钮）；中央 3D 工作区；左「历史对话」/右「已选位置」抽屉；底部统一输入框（＋菜单 9 项/多行自动增高 ≤160px/Enter 发送/Shift+Enter 换行/发送按钮状态/拖拽图片附件）。**verify_3d 契约保留**：selList/regionLabel/hint/按钮 ID 与「已选（N 处…）」文本格式不变。
8. **统一提交框架**：点击→chips 位置标签（desc+Mesh/Node/faceIndex/坐标 tooltip，× 删除三同步：chip+记录+3D 标记）；sendChat 合并「全部已选位置(regions 紧凑+regions_full 完整含 mesh/node_chain/face_index/model_local_xyz/world_xyz/normal/uv/bone/skin/weights/confidence)+文字+图片附件」为一条 `{v:21, type:"derm consult request"}` 结构化载荷（console+bridge.onChatSubmitted 上报，本地回执不接诊断 API）；历史卡（时间/文字/位置/附件/可展开载荷）；无文字有位置可发、只有文字可发、空输入禁发；提交后文字附件清空、位置保留。图片附件白名单 png/jpg/jpeg/webp ≤10MB（与 Python 端同口径）。

**验收**（QtWebEngine 真实链，两脚本均 failCount=0）：
- `/tmp/v21_accept.py`：拇指五区×左右手 10/10 id 精确匹配（掌骨区/近节/远节/指间关节/指尖）；背面下半身 11/11（左右臀/臀沟/臀腿交界/左右大腿/左右膝窝/小腿/左右脚跟后侧）；thigh_hip_border 含「语义不足」；大腿 yband 五等分定向连点 5/5（relT 0.09→0.91 全带覆盖，词与实际命中坐标自洽：近端/近端偏中/中段/中段偏远/远端）；× 删除同步。
- `/tmp/v21_composer_accept.py`：F1 新 UI 元素 20 项全在；F2 点击→chips 同步+计数；F3 标签×删除三同步；F4 结构化合并发送（载荷 text/regions/regions_full/mesh/face_index/坐标全量、无文字可发、纯文字可发、空输入禁发）；F5 提交后清空+历史完整+Enter 发送+Shift+Enter 换行；F6 历史卡渲染+抽屉开关+＋菜单外点关闭。
- 回归：verify_3d ASSERT-PASS（19 区域删净/17 锚点/完整命中字段/连点独立/视角稳定）；client 11 passed；tests-server 16 passed。
- 探针方法学沉淀：验收脚本相机必须**正对命中面法线**（旋转/径向偏移相机掠射角下投影中心偏移→假 yOff）；点击前用产品同款 `setFromCamera` 做像素射线预验证；keep=true 连点需 stale 检测（记录数+uid 判断本次点击是否真的新增）+失败记录删除回滚。

**诚实说明**：face-semantics 仍是几何聚类推断（顶点+法线+命名语义），非 GLB 原生标注；Custom3d66.glb 无骨骼/Skin/UV → bone/skin/weights/uv 恒 null；thigh_hip_border 交界带 conf 0.55 语义不足是解剖事实（臀沟体表标志仅背面可见，前面无可靠分界）；背面 zone 的探针相机为自动生成的多角度偏移，真实用户背面点击一致性由 Raycaster 最近可见面+逐面查表保证（无吸附无容差）。发送为本地结构化回执（console+bridge），未接服务端分析 API——待需求方确认后接入。

## v22：点击零弹层 + 桌面端 Codex 工作台重构（2026-09-07 第九轮）

**需求**（五部分）：①禁止每次点击自动弹出第二层界面（点击只在交点加标记+输入框新增位置标签；「已选位置」面板只能用户主动打开；打开不复制模型/不加遮罩；关闭回同一模型实例）；②简化为 Codex 风格工作台（中央只留 3D + 底部输入框；病例/分析/历史默认隐藏为抽屉）；③3D 点击识别精度保持（无容差/吸附/扩散，最近可见三角面）；④拇指/背面下半身易错区域保持；⑤验收标准七条。

**实现**：
1. **web 端零弹层修复**（`client/web/main.js`）：删除 renderSelection 内「选点自动打开 selDrawer」的一行（v21 引入的 bug 根源）——点击选点现在只更新标记+chips+计数徽标，抽屉只能由「已选」按钮/＋菜单打开。
2. **单实例保证**（实测验证）：initScene 仅启动时调用一次，无遮罩层/第二 canvas/modelRoot 复制；10 连点跨 5 个身体区 canvas 恒 1、renderer.domElement 同一实例、渲染 mesh 集合恒 22。
3. **桌面端 Codex 工作台**（`client/main.py` 重构）：`__init__` 去三栏 QSplitter → 中央 3D 视图（`_build_3d_panel` 仅 web 视图）+ 左「🕘 历史/跨端续办」右「📋 分析结果/医生审核」两个默认隐藏 QDockWidget（仅 Closable）+ 顶部 QToolBar（历史/分析结果 checkable 开关、截图、mock/server 模式与服务地址收纳）；`_toggle_dock` 只 setVisible 取反，不复制模型不加遮罩。
4. **统一提交桥接**：WebBridge 新增 `onChatSubmitted(payload)` slot——web sendChat 的 `{v:21}` 结构化载荷（文字+全部位置+附件）→ Qt `on_chat_submitted`：拼摘要入历史抽屉 + `_ensure_client()` + `client.start(pipeline_text)`（mock 离线确定性管线 / server）→ `_show_result`；ask_more 分支补写 result_view（此前只入历史）。`on_region_selected` 不再改写输入框文字（chips 在 web 内完成），且过滤空/multi 初始化上报（notifyRegion 加 labels.length 守卫）。
5. **遗留 bug 清理**：esc() 未定义（NameError）→ `from html import escape as esc`；`_server_url()` 被调用未定义 → 新增（环境变量优先，其次工具栏输入框）；`_load_settings` 从未被调用（接入模式/服务地址不恢复）→ `__init__` 补调；on_pick_photo 引用已不存在的 photo_preview/case_input + 重复 audit → 重写为独立脱敏副本工具（附件主路径走 web ＋菜单）；on_load_demo 依赖已删的 case_input → 改走 `on_start_with_text`；imports 清理（移除未用 QImage/QPixmap/QKeySequence/QMenu/QToolButton/QSplitter/QTextEdit，误删的 QPushButton 回补）。
6. **发送按钮联动**（回归中发现）：renderSelection/clearSelection 末尾补 `updateSendBtn()`——选点/删除现在正确启用/禁用发送按钮（v21_composer F1.sendBtnDisabled 假失败根因：清空后按钮未禁用）。

**验收**（QtWebEngine 真实链）：
- `/tmp/v22_final.py`（G1-G6 全 PASS）：G1 十连点（左右大腿/臀/小腿/脚跟后/拇指掌骨/膝窝/足缘）全部命中且 selDrawer/histDrawer/plusMenu 全程 false、无遮罩节点、canvas 恒 1；G2 renderer.domElement 同一实例；G3 渲染 mesh 集合恒 22 未复制；G4 抽屉仅用户路径开关（按钮开→关、＋菜单开、× 关全正常）；G5 抽屉开着继续点击→rows 10→11、抽屉保持开、关闭后同一 renderer 继续可选点；G6 × 删除三同步（12→11→0，chips/记录/标记一致）。
- 桌面端离屏截图：默认中央 3D+底部输入框，左右抽屉隐藏；工具栏 act 触发后抽屉可见、再触发隐藏。
- 端到端链路：web 统一提交（文字+2 位置+1 附件）→ bridge.onChatSubmitted → 历史 2 条（用户+智能体）→ mock 管线 ask_more → result_view 显示「资料不足+缺失项+补充引导」→ 状态栏「待补充资料」。
- 回归全绿：verify_3d ASSERT-PASS；v21_accept failCount=0（拇指 10/背面 11/relT 0.1-0.9/×删除）；v21_composer failCount=0（F1-F6，含修复后的 sendBtn 联动）；client 11 passed；tests-server 16 passed。

**诚实说明**：桌面端「统一提交」的智能体应答仍走 mock 离线确定性管线（server 模式需用户填服务地址）；web 端 sendChat 的回执也是本地结构化载荷（console+bridge），未直接接服务端分析 API；QtWebEngine 下 window.prompt 不可靠的问题沿用 bridge.promptLabel 方案未变。

## v24（2026-09-08）：3D 部位识别全量审计 + 拇指掌指关节修复

- 用户问题：3D 人体部位识别「某些地方依然不准确」。
- 新工具 `client/tools/audit_all_zones.py`：176 个语义分区全量探针审计（真实 onPick 链）。
  - 方法学三大修正（踩坑实录，勿重蹈）：
    1. MouseEvent clientX/clientY 整数截断——探针预验证必须用整数像素；
    2. 点击成功后 regionLabel 文字变长 → toolbar 换行长高 ~16.5px → canvas rect.top
       下移 → 探针与 click 读到不同 rect → 射线偏 16.5px 命中邻面（假阴性）。
       修法：attempt 开头先 clearSelection 复位布局，探针/点击间零 DOM 变动；
    3. ±2px 网格重试 + 外法线/轴向 7 方向相机（恢复 v23 方法学）。
- 真 bug 修复：`mcp_l_thumb`/`mcp_r_thumb` 分类器有产出但 labels 未注册 →
  faceSemFor 返回 null 落兜底。`build_face_semantics.py` 补注册（左右各 14 面），重建表。
- 终局判定：PASS 166/176；其余 10 区为「外层网格遮挡的解剖现实」
  （头发盖颅顶/枕/侧头、内裤盖腰后带、眼睑/眼周/眼球嵌套内壳），用户点到的是
  可视外层 → 报外层标签即正确行为，不是缺陷。
- 回归：verify_3d 29 点全过（main.js?v=26）；根 vitest 412/412；client pytest 11/11。

## v25（2026-09-08）：胸上区/颈根误标 abdomen 修复（用户实测发现）

- 用户报：点击斜方肌/颈根处（落点 y≈1.53）报「腹，前上方，上份」。
- 根因（v20 起即存在，非 v24 引入）：躯干前面分支只覆盖到 y<1.24（sternum/breast 上界），
  y 1.24-1.78 前侧中带全部落到 abdomen 兜底 → 840 面误标（含锁骨下/胸上/颈根）。
  背面 back_upper 同样顶到 1.778，项部/斜方肌上缘被算进「背（上）」。
- 修复（build_face_semantics.py，实测前中线轮廓定界：y 1.60 起前缘 z 回升=颈圆柱，
  y 1.80 骤降=下颌）：新增 chest_upper（192 面）、supraclavicular_l/r（各 98 面）；
  neck 前段下探 y 1.45（前中 |x|<0.20）、背面 y>=1.62 项部归 neck；abdomen 上界 y<1.24。
- audit_all_zones.py 候选采样改「各段轮转」（原被主段吃满，腰背高位段无代表面）。
- 终局审计 PASS 172/179，0 异常；7 UNREACH 全为已知遮挡（眼球/眼睑/头发）。
- 回归：verify_3d ASSERT-PASS（main.js?v=27）、vitest 412/412、pytest 11/11。commit 5cb95a4。

## v26（2026-09-08）：下身语义带整体下移修复（用户实测发现）

- 用户报：点裆上水平报「髋前·上份」、点大腿视觉中上段报「近端偏中」——下半身识别「整体向下移动」。
- 排查结论：命中链无偏移（4 个点击最近面归属与记录一致、探针投影像素与截图吻合），
  错在语义带定界本身——旧 thigh band 只从世界 y −0.942 起，视觉大腿根（裆底/股会合）
  在 y≈−0.33、内裤下缘 −0.20，即大腿上 58% 被划给「髋前/侧腹/臀腿交界」，
  relT 分母被截断 → 描述系统性偏低。
- 修复（build_face_semantics.py）：
  1. 大腿带重定界 y −1.441..−0.19（前外侧全部裸露皮肤，含股根）；thigh 556→832 面/侧；
  2. hip_front 逐面表删除（前侧腹股沟区被内裤覆盖，可见皮肤即大腿根；标签仅为
     zoneFromPoint 几何兜底保留）；
  3. thigh_hip_border_l/r 幽灵标签删除（前侧不再设交界带，背面臀褶 gluteal_fold 保留）；
  4. labels 179→177。
- 用户 4 点击复现（真实 onPick 链）：裆水平两点 →「左/右大腿，前侧，近端」（视觉=大腿根 ✓）；
  大腿 67% 两点 →「中段偏远」（relT 0.703 与视觉 67% 吻合 ✓）。
- 回归：audit PASS 169/176（7 UNREACH 全为已知遮挡）、0 异常；verify_3d ASSERT-PASS；
  vitest 412/412；pytest 11/11。表 cache:no-cache 无需 bump。
- 方法学再确认：复现脚本同踩 v24 的「toolbar 换行 rect 下移」坑（连续点击 regionLabel
  变长 → 布局位移），attempt 前 clearSelection 复位即可。

## v27（2026-09-08）：膝带重定界修复（小腿上部报膝、膝报大腿）

- 用户报（截图）：点左小腿上部（local −0.161,0.300,−0.015 → world y=−1.685）报「左膝·远端」，
  且「膝盖识别成大腿」。
- 根因：旧 knee band [−1.6817,−1.4413] 以「y 台阶」定界而非解剖标志。实测腿宽剖面（左腿柱，
  0.04 层）：髁最宽 y −1.50~−1.44（宽 0.319 峰），−1.56 以下骤减（−1.66 处 0.284→−1.70 处
  0.255），髌骨前凸 zmax 起于 y −1.38（+0.009→+0.027@−1.30→+0.0749@−1.22）。旧带下缘 −1.682
  把髁下方约 0.24 高的小腿上端算成膝（用户点 −1.685 恰压带外 0.0036 跨带），上缘 −1.441 又
  把髁上方 −1.44~−1.56 股髁上区算进小腿/大腿 → 双向错位。
- 修复（build_face_semantics.py）：新膝带 [−1.56,−1.36]（上=髁上缘宽度台阶、下=胫骨粗隆/
  髌韧带下缘，高 0.20 占腿 6.8%）；calf 上缘 −1.682→−1.56；popliteal 背面（z<−0.20）同步
  [−1.56,−1.36]；膝上 gap 带 [−1.36,−0.942) 全方位归 thigh（put 为 first-wins，须在臀/
  会阴分支前只填该 gap，避免抢走 [−0.942,−0.19) 背面臀区）。
- 面数核对：thigh 832→773/侧（0.20 高划给膝）、knee 77/侧、calf 306/侧、popliteal 45/侧、
  segments 18767→18766、labels 177 不变。
- 端到端复现 6/6（真实 onPick 链）：用户点 →「左小腿，前外侧，近端偏中」✓（不再报膝）；
  对称右小腿 ✓；膝两点 uid10/uid9 →「左膝」✓；大腿远端 uid8 →「左大腿」✓；髋前L v26 回归 ✓。
- 回归：audit PASS 169/176（与 v26 基线一致，7 UNREACH 全为已知遮挡）、verify_3d ASSERT-PASS、
  vitest 412/412、pytest 11/11。表 cache:no-cache 无需 bump。
- 待办（可选一致性）：main.js zoneFromPoint 几何兜底分带（ny 0.155/0.205 膝小腿界）与新表
  不同步——仅表 miss 时生效，暂不动。

## v28（2026-09-09）：膝带以用户实测双锚定界（v27 仍把膝点报成大腿）

- 用户报（截图）：两个髌骨点击 local(−0.142,0.527,0.052)/(0.128,0.537,0.053) → world
  y −1.065/−1.037，标记正落在髌骨前凸面上，报「大腿，中段偏远」。v27 修复无效。
- 根因（v25-v27 三版共病）：一直用「几何代理」定膝带而不用用户实测锚。v27 把腿宽
  峰 y −1.50~−1.44（宽 0.319）当成「股骨髁」——实测对照前凸剖面后确认那是**腓肠肌
  最宽点**（膝关节线下方，属小腿），膝带 [−1.56,−1.36] 因此整体低了 0.3~0.5。
- 决定性证据：
  1. 用户三轮实测膝点 world −1.037/−1.065/−1.070（z +0.144/+0.141，正落前凸峰）；
  2. 膝关节线人体测量学：膝中心≈身高 28% → y=−2.44+0.28×5≈−1.04，与实测吻合；
  3. 前凸剖面（x −0.50..−0.25，0.02 层）：zmax 自 −1.30（+0.027，髌韧带）升至
     −1.00（+0.160，髌骨体顶），髌骨中心≈−1.05。
- 修复（build_face_semantics.py）：新膝带 knee=[−1.44,−0.942)（用户膝点距边界
  ≥0.10）；calf 上缘 −1.56→−1.44（腓肠肌宽峰归小腿）；popliteal z<−0.20 同步；
  verify_3d selThigh 大腿探针归一化 0.245→0.38（旧点 world −1.28 现属膝带）。
- 面数：knee 473/侧、calf 371、thigh 276、popliteal 81；segments 18812、labels 177。
- 端到端复现 6/6：两髌骨点+上轮 −1.070 →「左/右膝」✓；小腿 −1.685 → calf ✓；
  大腿中/髋前回归 ✓。
- 回归：audit 169/176（7 UNREACH 全为已知遮挡）、verify_3d ASSERT-PASS、
  vitest 412/412、pytest 11/11。commit 见 git log。
- 方法学教训：语义带定界必须以「用户实测点击」为锚、几何测量为辅；纯几何代理
  （宽度峰/台阶）已连续三版产生同类错位。

## v29 腋窝过宽/前臂漏面/臀腿背面错带（2026-09-09）

- 用户报（6 截图 10 点）：①肩前/三角肌前缘两点报「腋窝」；②臂根内下报「腋窝·远端」
  （实为腋窝凹陷臂侧缘，标签对但需核对）；③前臂前/背面两点报「侧腹」「腰/背(下)」；
  ④臀褶下/臀外上两点报「右臀·上份」「腰/背(下)·下份」；⑤大腿后中段两点报「臀」。
- 根因三处（build_face_semantics.py）：
  1. 腋窝 amp mask `nyf<0.35 & y<1.44` 太松——肩前三角肌区（nyf −0.18..+0.47 朝前）
     被吞。真腋窝与肩前的区分特征是法线：腋窝凹陷底 nyf ≤−0.53（强朝下），肩前朝前。
  2. 前臂 armdist<0.25 守卫太紧——臂柱边缘面（armdist 0.30-0.35，u2 点 0.310/u6 点
     0.323）漏给 flank/back_lower_waist 兜底。
  3. 臀带下界 −0.942 太深——把大腿后侧整段吞成臀。**剔除内裤 mesh 后重测发现宽度
     剖面 y −1.00..−0.12 无台阶，旧「臀沟≈−0.60」系内裤面混入的假象**；改用 u4 右点
     （y −0.247 视觉正落臀褶）+ 裆底 −0.33 双锚定臀沟≈−0.34。
- 修复：amp 改 `y [1.0,1.32) & nyf<−0.20 & z<−0.02`；新增肩前带 shfront
  （|x| 0.42-1.03, y 1.12-1.45, nyf>−0.25, z>−0.10 → shoulder_up）；前臂 armdist
  0.25→0.36；背面重定界：大腿背面=[−0.942,−0.36)、臀=[−0.36,−0.13)、臀褶=[−0.36,−0.22)
  z<−0.06、cleft 随臀带。u3 点期望修正为 armpit（实测 nyf −0.53..−0.65 属腋窝臂侧缘）。
- 面数变化：thigh 276→502/侧（+背面 226）、forearm 250→341、armpit 141、shoulder_up 191、
  buttock 52、gluteal_fold 55；labels 177 不变、segments 18686。
- 端到端复现 13/13（10 用户点+3 v28 回归点，背面点用背面相机）：u1 两点→「肩·前侧」、
  u2/u6→「前臂」、u3→「腋窝」、u4R→「臀褶」、u4L→「臀·上份」、u5 两点→「大腿·后侧·
  中段」、髌骨两点→膝、小腿/大腿前回归 ✓。
- 回归：audit 169/176（基线一致）、verify_3d ASSERT-PASS、vitest 412/412、pytest 16/16。
- 方法学补遗：测量几何时必须剔除覆盖件 mesh（内裤 -13-483），否则宽度剖面/台阶检测
  会被覆盖层污染产生假边界（本轮「臀沟−0.60」假象即由此来）。

## v30 臂根后带（2026-09-09）

- 用户报（截图 13:21）：背面视角点击臂根后侧（三角肌后缘）报「背（上）·后侧·中份」，
  应为大臂。local (−0.271,1.365,−0.118) → world (−0.741,+1.226,−0.324)。
- 根因：背面臂根无带可归——v29 肩前带 shfront 只管 z>−0.10、肩带 shband 要求
  y≥1.30 & nyf>0.25、旧 upper_arm 要求 at>0.10（该面 at≈0.02）→ 被 back_upper
  （y 0.62-1.62 & z<−0.15）兜底吞掉。
- 剖面证据：背面 y 1.00-1.50 各层在 |x| 0.5-0.6 处近空（0 面），躯干背（|x|<0.5）
  与臂根后（|x|≥0.6）天然分离；u7 邻域 |x| 0.71-0.79、法线朝后上（nyf +0.04..+0.49）。
- 修复（build_face_semantics.py）：新增臂根后带 |x| 0.60-1.03 & y 1.02-1.38 &
  z≤−0.10 & nyf>−0.5 & armdist<0.48 → upper_arm（force_over=flank/hip_front）。
- 面数：upper_arm 82→204/侧（臂根后 +120）、back_upper 1256→1118；labels 177 不变。
- 端到端复现 14/14（u7→「左上臂，后侧，近端偏中」+ v29 十三点全回归）✓；
  audit 169/176、verify_3d ASSERT-PASS、vitest 412/412、pytest 16/16 全绿。

## v30a 后腋褶归位（2026-09-09）

- 用户报（截图 13:59）：与 u7 几乎同位的臂柱后缘点击报「左腋窝，后侧，中段偏远」，
  应为大臂。world (−0.763,+1.120,−0.323)，z −0.32 过后。
- 根因：v29 腋窝 amp mask 只设了 z 上界（<−0.02）没设下界——后腋褶下缘法线同样朝下
  （nyf<−0.20），被 amp 先抢；v30 臂根后带的 force_over 不含 armpit 夺不回。
- 修复：amp 加 z 下界 −0.20（真腋窝底实测 z −0.02..−0.16，z<−0.20 的后腋褶归上臂）。
- 面数：armpit 141→83/侧（后腋褶 58 面让给 upper_arm）、upper_arm 206/侧。
- 端到端复现 15/15（u8→「左上臂，后侧，中段」+ u7 + v29 十三点全回归）✓；
  audit 169/176、verify_3d ASSERT-PASS、vitest 412/412、pytest 16/16 全绿。

## v30b 上臂守卫放宽（2026-09-09）

- 用户报（截图 15:44）：正面点击左臂上端前外侧报「胸外侧，前侧，上份」，应为上臂。
  world (−0.850,+1.051,+0.007)；命中面 axw 0.839 / at 0.147 / armdist 0.268。
- 根因：上臂带三重旧阈值卡死臂根柱面——|x|≥0.85（命中面 0.839 差 0.011）、
  armdist<0.25（0.268 超 0.018）；v29 只放宽了前臂的同款守卫，上臂漏改。
- 修复（build_face_semantics.py）：上臂 |x| 下界 0.85→0.80、armdist 0.25→0.36
  （与前臂一致）；at>0.10 守卫仍隔离躯干侧壁（侧壁沿轴投影 <0.08）。
- 面数：upper_arm 206→264/侧、chest_side 57→52/侧。
- 端到端复现 16/16（u9→「左上臂，前侧，中段」+ u7/u8 + v29 十三点全回归）✓；
  audit 169/176、verify_3d ASSERT-PASS、vitest 412/412、pytest 16/16 全绿。
- 腋窝-上臂-肩带交界区已按用户 9 轮实测全部校准（u1-u9）。

## v31（2026-09-10）浅色主题 UI 改造（用户截图锚定）

- 需求：用户提供聊天应用截图，要求「程序界面变成这种样式」。
- 改动：web/index.html 全量样式重写（深→浅 tokens；顶栏改左侧浅色导航栏；
  底部输入区圆角大卡+圆形深色发送键；抽屉/气泡/菜单浅色圆角；缓存 v=30）；
  web/main.js 6 处颜色（场景背景+雾 0x0d1118→0xf3f4f6、半球光地色、阴影
  0.32→0.22、标注/向导/调试/备注 4 处内联 toggle 色）；main.py
  _apply_dark_theme→_apply_light_theme（QPalette+Qt 样式表全浅色，内联 HTML
  旧深色 #ffd479/#7f93b5/#9ab0cd 同步替换为可读色）。
- 验证：vitest 412/412、pytest 11/11、verify_3d 男性模型 ASSERT-PASS
  （40 个用户校准点全对，浅色场景拾取链完好）；客户端真机运行截图确认
  web+Qt 全浅色。所有 ID/class 契约未动（verify_3d 依赖的选择器原样保留）。
- 遗留（非本轮）：①上轮中断的侧别翻转（x<0=解剖学右）+u10 窄带已实施未
  提交未回归；②verify_3d 女性模型 leftToes 探针报足弓不报趾区——女性语义表
  会话未完收尾，与样式无关。
- 教训：QtWebEngine 启动后约前 10 秒模型/相机仍在加载瞬态，截屏验收须等
  setView 完成后再截，勿把瞬态当 bug。
- 追加（同日）：用户反馈「部位点多了会遮挡模型」——同点连击产生 N 条相同
  位置标签把 3D 视口顶没。修复：#chipsRow 限高 96px + 内部滚动（细滚动条），
  renderChips 后 scrollTop=scrollHeight 保持最新可见；缓存 v=31。
  另：pkill 进程名区分大小写（"Python main.py"），曾误判已杀导致双实例，
  清理时用 pgrep 全列核对。

## v32（2026-09-11）：女性模型白模化置纯白 → 曝白失去立体感（浅色主题暴露）

- 现象：用户截图对比——男性模型有明暗梯度看得清，女性模型整体死白糊成一片。
- 根因：v29 的女性白模化（`applyFemaleWhiteFinish`，去写实贴图对齐男性观感）把皮肤/衣物材质置**纯白 (1,1,1)**，而男性 glb 底色是 **0.5 中灰**——深色主题下纯白对比强看不出问题，浅色化后纯白 + 白背景 + ACES 曝光 1.1 → 全部逼近饱和白。
- 修复：白模化目标色 1.0 → **0.5 中灰**（与男性 glb baseColorFactor 完全一致，「和男性一样的颜色」这次真正对齐）；运行时材质归一化的金属度/粗糙度压限逻辑不动。
- 验证：运行时材质探针（QtWebEngine 实测）——女性 Torso/Face/Arms/Legs/shirt 全部 [0.5,0.5,0.5]、头发材质恢复 0.906×发丝贴图；**躯干区灰度梯度量化对比：男 std 12.96 / 女 std 12.90、mean 230.5 / 231.8——渲染特性完全对齐**；verify_3d ok:true（男性全 PASS，唯一失败仍为女性 leftToes 遗留）；vitest 412 ✓；pytest 11 ✓；客户端重启确认。
- 备注：排查中曾试「通用近白材质压灰」规则，因白模化在其后异步执行会盖回而无效，已撤销——修复落在白模化源头一处。

## 执行计划阶段 0–8 + §16a（2026-09-11）：AI 转医疗 Agent《AI转医疗Agent执行计划.md》执行完毕

按 971 行执行计划顺序完成阶段 0→8 与 §16 第一步（§16a），提交链：ca7720(1)→4bc676(2)→841b5d(3)→29defc(4)→426098f(5)→40613e2(6)→9f115d2(7)→§16a。

- **阶段1** 女性左脚趾回归根因=探针引导失准（front 相机俯角越过趾尖撞足底前缘 y≤-2.467），非语义表问题；探针改锚点正射。
- **阶段2** 人工区域严格数据层：多视角 durable face set（views 追加/revise/history）、显式重叠规则（updated_at 升序后写覆盖）、`onManualRegionsExported` Qt 文件对话框原子导出。
- **阶段3** 🩺 医生资料抽屉：病史/检验行编辑/报告/用药史/影像（扩展名+大小+空文件同步拒绝、低分辨率注记），payload 携带+审计计数。
- **阶段4** 🧠 辅助面板（§3.1 资料预览）：逐项勾选 CLINIC_SELECT、参考病例确认门禁 REF_CASE_CONFIRMED、`sanitizeText` **正则顺序=身份证(长)先于手机号(短)**（否则 11010119900307889X 中段 19900307889 被手机模式吃掉）、脱敏预览、与资料抽屉同侧互斥。
- **阶段5** AI 辅助判断（§3.2/3.3/3.4）：web 辅助面板三区渲染（诊断辅助/临床建议/审核审计），数据流 `_show_result → _push_assist_to_web(ensure_ascii 双层序列化) → window.__pushAssist`（仅会话内存、textContent 防注入、pending 常显、免责声明横幅常驻）；「非确诊概率/待排/禁处方剂量」文案硬编码于渲染层并 verify_3d 断言；clinic 载荷接入 server（StartBody.clinic → 自定义病例 case.clinic_payload → LLM prompt；demo 不并入）。
- **阶段6** 整份审核：⟳重新生成（ReviewBody.action=regenerate→ask_count=0 重走分流回 pending，**history.append 必须在 _screen 前**否则快照少一条）、review_history 逐条 + doctor_final 与 AI draft 分开存、render_report 尾部 SIGNATURE_NOTE（待签署草稿/禁自动写入病历医嘱处方治疗）、web 审核区历史+状态中文映射。
- **阶段7** 安全闭环：`/api/anatomy/refine`（几何摘要 only，screenshot/image/glb/model_data/bytes/base64→400，与影像通道分离）、`/api/imaging/adapters` 注册表（local_upload ai_enabled=False）、clinic_payload 白名单 `_filter_clinic_payload`（未声明键丢弃+审计）。
- **阶段8** E2E（demo3 带资料→追问→补充→确认→待签署报告）、失败不变性（**monkeypatch sa._chat 后 finally 必须恢复**——曾写出 `sa._chat = None or orig_chat if False else sa._chat` 自污染表达式）、审计链断言；docs/doctor_pilot.md 医生试点材料。
- **§16a** server/rag.py 本地书籍 RAG 核心：生命周期 queued→parsing→chunking→indexing→ready/failed/quarantined、SHA-256 去重、解析器注册表（pdf/docx/epub 未注册→failed 明确不伪造）、章节/段落+600 字符切分（chunk_id=book-idx-hash，过短节内合并）、Okapi BM25 中文 bigram、引用契约（只回 ready；citation_id/书名/版本/章节/页码/摘录/chunk_hash/kb_snapshot）、软删即停检索+恢复、备份往返、EMBEDDING_STATUS 如实 not_installed；`_screen` 自动全库检索→draft.book_citations（与合成 KB 引用分开，try/except 不阻断）。KB 测试用 autouse fixture 把 BOOKS_FILE 指 tmp_path（跨运行残留会让 duplicate_sha256 拒绝二次导入）。
- **测试基线**：verify_3d exit 0（阶段5 15 项+阶段6 4 项断言入 assistLayer）、vitest 412/412、client pytest 11、tests-server pytest **26**（+10：clinic 接入/审核历史/安全边界/E2E/失败不变性/审计链/KB×4）。缓存号 v39→v40（阶段5）→v41（阶段6）。
- **遗留 §16b**：齿轮设置面板（web 左下角+PySide6 原生共享数据）、批量导入任务管理（暂停/取消/断点续传）、PDF/扫描 OCR/DOCX/EPUB 真实解析器（rag.PARSERS 注册位已留）、本地 embedding 模型管理、web 面板书籍引用展开 UI。

## 2026-09-11 · §16.1 双齿轮设置入口（v42，提交 aea58fa）

- 用户反馈「没有找到左下角齿轮设置按钮」→ 实现 §16.1 双入口：
  - **Web 侧**：#stage-wrap 左下角浮动 #settingsEntry 齿轮（z-index 7）→ #settingsDrawer（挂 composer 内，与 clinic/assist 同侧互斥、并收 hist/sel 抽屉）；三分区——📚 知识库管理（书籍列表状态 pill/批量导入 file input→readAsDataURL→base64→bridge.kbImport/可恢复删除与恢复/备份与恢复最近备份）、🔎 RAG 运行设置（检索模式+向量模型状态如实标注）、🛡 版本与安全（kb_snapshot 行+数据位置说明+免责声明常显）。
  - **PySide6 原生**：工具栏「⚙ 设置」→ SettingsDialog(QDialog)（QTableWidget 5 列+批量导入 QFileDialog multi-select/停止恢复检索/备份恢复）。两处入口共用模块级助手 `load_rag_module()/kb_list_payload()/kb_backup_to_disk()/kb_restore_latest_backup()`——sys.path 注入 ../server 后直接 `import rag` 操作同一落盘（server/data/rag/books.json），不经 HTTP，mock/server 模式均可用。
  - **bridge 新增 6 槽**：kbList/kbImport/kbDelete/kbRestore（result=str，QWebChannel 回调式 `bridge.kbList(function(s){})`）/kbBackup/kbRestoreBackup；main.js renderKbBooks 全 textContent 渲染；**无 bridge（浏览器/探针）→「未连接桌面端本地知识库（离线模式）」明确降级，不伪造数据**。
- 教训：①verify_3d on_checks 里插入新 for 循环时，原 `if al.get(key)` 会错误归到新循环——必须同步改成 `sl.get(key)`；②Edit 工具把方法体插入另一方法中间会打断函数（_open_settings 曾插进 _build_toolbar 中段），改完必须 ast.parse+目视核对方法边界。
- 验证：verify_3d exit 0（新增 settingsLayer 8 断言全 true/errors 空）、vitest 412、client pytest 11、tests-server pytest 26 全绿；缓存号 v41→**v42**；桌面客户端已重启加载 v42（进程 7403）。
- **遗留 §16b（更新）**：批量导入任务管理（暂停/取消/断点续传/失败重试）、PDF/扫描 OCR/DOCX/EPUB 真实解析器（rag.PARSERS 注册位已留；扫描 PDF 本地 OCR 不得发外部服务）、本地 embedding 模型管理（首次允许联网下载固定版本+哈希记录+失败降级）、书籍引用展开 UI（点击展开页码/原文/文件位置）、恶意文件与压缩炸弹防护（已有 200MB 上限）。

## 2026-09-11 · v43 齿轮移位 + 语言切换 + LLM 连接设置（提交 fc6bca5）

- 用户反馈三点：①齿轮应在程序最左下角而非 3D 区；②设置加语言切换（简体中文/English）；③设置加 LLM 连接（URL/API Key）。
- **齿轮移位**：#settingsEntry 从 stage-wrap 浮动改为 composer 行首（程序最左下角），verify_3d 新增 gearInComposer 断言（在 composer 内且不在 stage-wrap 内）。
- **语言切换**：main.js I18N_TEXT/I18N_PH/I18N_TITLE 三字典（~60 项静态文案：菜单/抽屉标题/临床资料分区/辅助面板分区标题/设置面板/占位符/tooltip）+ t() 接入设置面板全部 JS 动态文案；setLanguage/applyLanguage/__applyLanguage（原生回推钩子）；偏好双齿轮共享：bridge.uiPrefsGet/Set → client/data/ui-prefs.json，无 bridge 回退 localStorage；原生侧 NATIVE_I18N + set_app_language/_apply_native_language（工具栏 4 动作+窗口标题重译）。**边界：AI 生成内容（服务端草稿/报告）与合规免责声明保持中文不翻译**（§7 免责文本必须原文常显）。
- **LLM 连接**：设置面板新增 Base URL/模型名/API Key(password) 输入与保存；桥接 llmConfigGet（掩码摘要：configured/base_url/model/has_key/key_tail 4 位，绝不回 Key 本体）/llmConfigSet（http(s) 校验，Key 空则保留已存 Key，保存后明文输入框即清空）；落盘 server/data/llm_config.json，服务端 load_llm_config() 逐字段优先于 LLM_BASE_URL/LLM_API_KEY/LLM_MODEL env，health 端点同步；审计只记 base_url/model/has_key 不记 Key。**验收18保持：客户端代码无任何硬编码密钥**。
- 教训：测试 fake LLM 输出里 severity 给字符串会炸归一化（derm_core 期望 {level,basis} dict）——fake 契约要对齐 normalize_llm_draft。
- 验证：verify_3d exit 0（settingsLayer 15 断言含 langSwitch/langRestore/llmMasked）、vitest 412、client pytest 11、tests-server **27**（+LLM 配置优先级）。缓存号 v42→**v43**；客户端已重启（进程 8128）。
- **遗留**：辅助面板内层动态标签与 chips 的 i18n（本批只做静态文案+设置面板）；导入任务管理/OCR 等解析器/embedding 模型管理（§16b 其余）。

## 2026-09-11 · v44 参考图布局调整（齿轮→侧栏底部，模型切换→发送行）

- 用户给参考截图（聊天应用界面）：①切换模型按钮放发送那里；②设置齿轮放左下角（侧栏最底部）。
- **齿轮移位**：#settingsEntry 从 composer 行首移至左侧 #toolbar 最底部（regionLabel 之后，margin-top:8px），样式 32px 圆角图标钮。
- **模型切换移位**：#modelSelect 从左侧栏移至 composer 发送按钮左侧（max-width 130px、32px 高圆角胶囊）。JS 改造：选项填充抽成 refreshModelSelect()（applyLanguage 时按 LANG 重刷并保持选中值），MODELS 加 en 字段（男·3d66/Male·3d66、女·3d66/Female·3d66），切换逻辑/verify 探针按 id 取元素不受 DOM 位置影响。
- 教训：zsh 下 `ls dist/*.exe` 无匹配会整条命令中断（no matches found）——glob 前先拆分或 2>/dev/null；git 提交路径以仓库根为准（cwd 在 client/ 时前缀 client/ 会双重拼接报 pathspec 错误）。
- 验证：verify_3d exit 0（settingsLayer 16 断言：gearInToolbar/modelInComposer 新增全过）、vitest 412、client pytest 11、tests-server 27。缓存号 v43→**v44**；客户端已重启（进程 9164）。

## 2026-09-11 · v45 独立全屏设置界面（提交 c68f88d）

- 用户给第二张参考截图（ChatGPT/Codex 桌面端设置页）：进入设置应是独立设置界面，非侧边抽屉。
- **结构重写**：#settingsDrawer 从 400px 侧边抽屉改为 `.settings-page` 全屏覆盖层（position:fixed inset:0 z-60，flex 行布局，浅灰底）；左侧 #settingsNav（「← 返回应用」按钮 + 4 个导航分区：通用/LLM 与模型/医学书籍知识库/检索与安全）+ 右侧 #settingsContent（4 个 .settings-pane 卡片式内容区，pane 大标题 22px）。原 5 个 .settings-sec 重组：general=语言；llm=LLM 连接；kb=书籍管理+RAG 运行设置；security=版本与安全（快照行/数据位置/免责声明）。
- **契约保持**：全部功能元素 id 原样保留（kbList/kbStatus/kbImportBtn/kbBackupBtn/kbRestoreBackupBtn/kbFile/kbSnapshotLine/ragRunSettings/settingsDisclaimer/langZh/langEn/llmBaseUrl/llmModel/llmApiKey/llmSaveBtn/llmStatus），open 类开关机制不变——initSettingsUI 只加导航切换（showPane：nav-item/pane 双向 active 切换 + curPane 会话内记忆，重开设置回到上次分区）。原 settingsTitle/settingsBody/settings-sec 元素删除，main.js 同步清理。
- **i18n**：I18N_TEXT 新增 settingsClose（← 返回应用/Back to App）/navGroupLabel/nav*/pane* 标题/langHelp/dataLocationLine/ragSecTitle/secSecTitle；I18N_TITLE 加 settingsClose；语言切换 sentinel 从 settingsTitle 改为 paneGeneralTitle（General/通用）。
- **verify_3d settingsLayer 16→19 断言**：新增 fullscreenPage（drawer 含 settings-page 类）/backWording（settingsClose 文案含「返回应用」）/navSwitch（点「检索与安全」→对应 pane+nav 双 active，切回「通用」恢复）。
- 验证：verify_3d exit 0（settingsLayer 19 断言全 true/errors 空）、vitest 412、client pytest 11、tests-server 27。缓存号 v44→**v45**；客户端已重启（进程 9638）。
- 遗留：设置分区记忆不跨会话持久化（仅会话内）；导航无图标自定义（用 emoji）；§16b 其余项不变。

## v61（2026-09-12）侧栏三分组常驻改版（参考 NuwaClaw 侧栏）
- 用户指令：「我想要的左侧任务栏是这样的」+ 参考截图；确认方案：三分组（项目 + 历史病例 + 最近）/ 每组常驻 5 条 / 分组底部「查看全部 ›」开现有浮层面板
- 实现：#toolbar 176→208px，navNew 之下三个 .rail-group（.rail-group-title + .rail-entries + .rail-more）；navProject=「📁 肤智 DermAgent」开项目 pane；renderRailHist() 经 bridge.casesList 取前 5（railHistSeq 序号守卫防过期响应，无 bridge/空列表空态不造假）+ renderRailRecent()（localStorage derm_recent 前 5）；railCaseRow 行点击 recordRecent("case","续办病例",case_id)+bridge.caseResume，行尾 🗑（stopPropagation+confirm+bridge.caseDelete→renderRailHist+面板同步）；recordRecent 加第三参 case_id；bridge 就绪回调与 initNavUI 双处首渲染；删 navHint 说明行
- 测试：verify_3d navLayer 重写 21 键（三分组标题顺序/查看全部/空态/无造假删除键）+ delLayer +3（railRecentSync 预置同步/单删后同步/清空后空态）；client pytest 19 + tests-server 50 + vitest 412
- 真机（CDP，v62 收尾轮）：三分组结构/标题断言、侧栏条目点击续办（最近分组同步出现 🗂 条目带 case_id）、侧栏行 🗑 删除（侧栏剩 1 条 + 服务端 2→1）、「查看全部 ›」开历史面板、「📁 肤智 DermAgent」开项目 pane
- 教训：①宿主/用户抢前台使 CGEvent 真机点击不可靠（两次误点其他应用）——改 QTWEBENGINE_REMOTE_DEBUGGING + CDP Runtime.evaluate 派发合成点击（awaitPromise: True），零鼠标事件零焦点竞争；②async 断言忘 awaitPromise 会得到 {}（Promise 不可枚举）还误以为 skip

## v62（2026-09-12）历史病例面板「删除全部病例」按钮 + 侧栏启动空态修复
- 用户指令：「给管理历史病例的窗口最底部增加一个删除全部病例按钮」
- 实现：server DELETE /api/cases（_lock 保护整表清空，逐条 case_delete 审计 + 汇总 case_delete_all，返回 deleted 数）；agent_client.delete_all_cases；WebBridge.caseDeleteAll Slot（server 模式校验/全删后 win.case_id/draft/client.case_id 句柄失效/audit case_delete_all_bridge）；main.js showHistCases 列表尾部 .nav-del-all（confirm 文案含条数与不可恢复警告 → bridge.caseDeleteAll → 成功 renderRailHist+showHistCases 双刷新/失败 alert；空态与无 bridge 不渲染按钮）+ CSS 红色边框按钮；缓存 bump v62
- 测试：tests-server 51（+1 test_delete_all_cases_clears_list：建 2 条→全删 200 ok→deleted>=2→列表空→GET 404→重复空删 deleted==0 幂等）；verify_3d delLayer +histEmptyNoDelAll（空态无全删按钮）；client pytest 19 + vitest 412
- 真机（CDP）：面板 1 行病例 + 「🗑 删除全部病例」按钮渲染→confirm stub→点击→面板空态 + 侧栏「历史病例」同步「暂无历史病例」+ 空态按钮消失 + 服务端 GET /api/cases == [] + audit case_delete_all 落盘；全删前已备份 sessions.json→/tmp/sessions.backup-v62.json（服务端累计 2485 条历史测试病例一并清理——UI 截断只显示前 30，全删正是清理场景）
- 修复 v61 真机 bug：MainWindow 新增 server_online Signal——bridge 就绪回调的首次 renderRailHist 可能早于本机 server 可用（started 路径拉起耗时），失败后侧栏停在空态无重试；server 就绪（started/already_running 两分支）后主线程 runJavaScript 重拉 renderRailHist（幂等）。CDP 验证 started 场景：客户端先起、server 后就绪，侧栏最终出现 2 条
- 教训：①tests-server 无 conftest 隔离、pytest 直写 server/data/sessions.json（多轮积累 2485 条）——涉及全量数据的端点真机验证前先备份；②Bridge Slot 异步回调在 Runtime.evaluate 返回后才完成，断言必须 awaitPromise + 轮询等待

## v64（2026-09-12/13）Codex 式对话 Agent 化改版（P0–P4 五阶段）
- 用户八条核心需求：①修复 LLM 无回复（正在思考/错误+重试/零伪装，未配置只显示「当前未连接 LLM」）②Agent 运行逻辑（闲聊不调工具；工作先理解+动作，确认后执行）③连续对话上下文（会话状态对象/恢复/隔离）④按需调用工具+agent_state 九态⑤3D 降为可选工具⑥主界面 Codex 式+删固定步骤条⑦病例写作澄清→补充→草稿+措辞纪律⑧8 类对话验收案例
- P0：_push_thinking（发送即「正在思考」占位）/__pushChatError 错误卡（retry_payload 原样重发；unavailable 无重试）/llm_unavailable 零医疗内容防线/客户端 60s 超时归一错误
- P1：服务端会话状态对象（conversation_id/messages/current_task/task_status/case_state/selected_body_regions/attachments/tool_history）+ GET /api/cases/{id}/restore 恢复端点 + __restoreConversation 历史卡渲染（_on_restore_done 先推历史再 _show_result 顺序修复）
- P2：计划确认协议——_plan_gate（工作类任务首轮出「我理解到的任务+准备动作」计划卡，workflow_state=planning；_is_confirm 精确匹配「确认」短语；补充≠确认并入病例字段更新理解再出计划）；_AGENT_STATES 九态映射进快照 agent_state；_screen 开头 pop 上一轮 reply 防 stale 遮蔽 draft；clarify 优先于 plan gate；demo 豁免
- P3：确认删固定流程步骤条（v58 遗留已清）；ask_more 澄清改走对话流不弹窗（verify_3d flowLayer 断言改 askNoModal）；图片按钮提为一级；用户卡右对齐 85% 蓝气泡；缓存 v66
- P4：8 类案例真机全过（闲聊真实回复/澄清/补充→计划/确认→真实草稿/门诊病历 doc/随访记录 doc/3D 切换保持对话/重启恢复）；同会话追问「刚才这位患者」正确出鉴别计划卡
- **关键 bug（doc 卡静默不渲染）**：_push_doc_to_web 传对象字面量，而 main.js __pushDoc(jsonStr) 契约是 JSON 字符串（内部 JSON.parse）→ JSON.parse("[object Object]") 被 catch 静默吞。修复：与 _push_assist_to_web 同模式先 dumps 成字符串再 dumps 一层做 JS 字符串字面量（client/main.py:1580）。教训：桥函数契约逐个审计——__pushFlow/__pushAgentReply/__restoreConversation 收对象，__pushAssist/__pushDoc 收字符串，两种并存极易踩
- 测试四件套：tests-server 60（+1 计划确认协议流/13 个非 demo 测试补确认轮）+ client pytest 21（+2）+ verify_3d exit 0 + vitest 412
- 教训：①wait 轮询匹配产物卡关键词可能命中用户卡文本（「现病史」）——用横幅文本（「真实 AI 生成/分析」）；②CDP 端口被旧客户端占住新客户端 bind 失败、eval 落旧页面——先 lsof kill 再单开；③zsh 中 echo === 报 == not found

## v64-P5（2026-09-13）对话序号随新对话重置 + 用户气泡自适应宽度
- 用户截图报障：①新对话仍显示 #7（序号跨会话累计）②两字气泡占满整行
- 修复：①navNewSession 清空对话流处同步 chatSeq=0（main.js:4104，此前 chatSeq 是页面级全局只在加载时归零）；__restoreConversation 恢复时 chatSeq 同步为历史 user 消息条数 + 恢复 user 卡 meta 补 #序号——延续会话编号连续、新对话才从头计数；②.conv-user 加 width:fit-content（index.html:112，此前块级默认占满 max-width:85%）
- 真机（CDP）：恢复卡 #1 → 延续消息 #2/#3 连续；点新对话后首条消息 #1；「你好」气泡宽 51px（25px 文本+24 padding+2 border，修复前 ~1090px 占满）；右对齐 right≈log 宽
- 缓存 v66→v67；verify3d exit0 + vitest 412 + pytest 81（client21+server60）全绿

## v64-P6（2026-09-13）图片按钮收敛进＋菜单 + 3D 模型精简为男女两款
- 用户指令：①删底部一级图片按钮（与➕添加附件重复）②人体模型只留男女两款
- 实现：①addImage 从 composer 一级行移入 plusMenu 首位（id 不变→绑定/imgFile/i18n 键全兼容，功能实测完好）；②MODELS 删 Xbot/Michelle/CesiumMan 三项（main.js），删 attachEyes/eyeGroup 死代码（唯一 eyes:true 模型已去），index.html 删 CesiumMan.data.js script，git rm 4 个模型文件（约 4.4MB）；SWITCH_JS 索引 0=男/1=女不受影响
- 真机（CDP）：composer 一级行无图片按钮 ✓ plusMenu 首项=图片附件 ✓ 点击触发文件选择+关菜单 ✓ modelSelect 仅[男·3d66,女·3d66] ✓ 男性默认装配 ✓ 切女性 Female3d66+锚点装配 ✓
- 缓存 v67→v68；verify3d exit0 + vitest 412 + pytest 81 全绿

## v64-P7（2026-09-13）新对话后旧会话进入侧栏「最近」
- 用户报障：点新对话后，之前的对话没出现在「最近历史对话」中
- 根因：recordRecent 只写 localStorage 不刷侧栏（「最近」仅初始化渲染一次）；且每条消息 unshift 一条会刷屏
- 修复（main.js）：①recentSessionToken 会话级条目——同一会话（token 相同）只保留一条「最近」，摘要随最新消息更新；navNewSession 时 token+1，旧会话条目固化、新会话另起一条；②recordRecent 写后即时 renderRailRecent()
- 真机（CDP）：A1 实时出现「💬 你好」→ A2 同条目更新为「💬 皮肤瘙痒怎么办」→ 点新对话旧条目固化 → 新会话条目置顶、旧条目第 2 位
- 缓存 v68→v69；verify3d exit0 + vitest 412 + pytest 81 全绿

## v65（2026-09-13）独立病例窗口——真实医院门诊病历单（S1 服务端 + S2 客户端窗口 + S3 联动 + S4 医疗安全）
- 需求：对话 Agent 决定打开独立「皮肤科门诊病历」窗口；17 字段 查看/编辑；三态来源（AI 草稿/医生修改/医生确认）；六按钮；医生确认才可导出正式报告；对话↔病例共享 conversation/case_id；无 3D 不阻塞；LLM 未连接仍可手动填写（零伪造）
- S1 服务端（server/app.py）：RECORD_FIELDS 17 字段 + _DOC_SECTION_MAP（doc 标题→字段归并）+ _sync_record（4 处产出路径同步：doc 任务/病例分析/case_edit LLM/template；AI 只覆盖非 doctor_confirmed 字段）+ start_case 创建即初始化空病历单；5 端点 GET/PUT/confirm/regen/export /api/cases/{id}/record*；regen LLM 失败返回「当前未连接 LLM…您仍可手动填写」；export md 含编号/创建/更新/审核状态/17 字段（空=未提供，body_regions 空=未选择 3D 部位）/引用/免责声明
- S2 客户端（client/）：CaseBridge(QObject)（caseReady/saveDraft/confirmAll/regenField/exportMd/backToChat 六 Slot）+ CaseWindow(QDialog 非模态 parent=None 独立窗口) 内嵌 QWebEngineView 加载 web/case_window.html（qrc:///qtwebchannel/qwebchannel.js——本地不存在该文件，第一版引相对路径踩坑）+ case_window.js（17 卡片渲染/编辑模式/脱敏开关/来源徽章/横幅）；Python→JS __loadCase 双层 dumps 字符串契约；所有 HTTP/LLM 操作 threading.Thread + case_done/case_export Signal 回主线程；导出 QFileDialog 保存；MainWindow 顶栏「📋 病例窗口」按钮（i18n zh/en）
- S3 联动：server _wants_case_window（「新建一份病例/打开病例窗口/写一份门诊病历」…关键词）→ 响应 open_case=True → _render 透传（初版 _render 白名单丢字段导致自动开窗失效，真机抓出）→ _on_chat_done 自动 open_case_window；chat done/恢复/resume/新对话 四处 push_case_to_window（窗口开着才推；新对话推空模板引导）；doc 卡新增「📋 在病例窗口打开」按钮（main.js → bridge.openCaseWindow）；窗口关闭仅 hide
- S4：3D 选点由 server 注入 record.body_regions（无则「未选择 3D 部位」不阻塞）；脱敏开关（姓名首字+＊、门诊号留尾 4 位）；诊断措辞「考虑/需要排除/建议医生核对」+ citation_id；regen 失败红横幅不伪造
- 真机修复三 bug：①qwebchannel.js 404→QWebChannel undefined（换 qrc 路径）②saveDraft 全量 PUT 用编辑快照覆盖 AI 新内容（改 dirty 字段集——只提交医生实际改过的字段）③页面重载后 _pending_case 已消费不重推（caseReady 二次到来时重新拉取）
- 真机（CDP 双 target index.html+case_window.html）：10 步验收全过——自动开窗/17 字段渲染/编辑保存医生修改徽章/regen 真实 LLM（含引用与措辞纪律）/医生确认→最终审核版本/导出 md 头尾齐全/脱敏开关/新对话窗口隔离/关闭重开内容保留
- 缓存 main.js v69→v70、case_window.js v1→v2；tests-server 65（+test_chat_open_case_flag）+ client pytest 21 + vitest 412 + verify3d exit0 全绿

## v66（2026-09-13）门诊病历窗口 → web 右侧面板（对话核心 + 病例辅助文档）
- **布局**：删除 QDialog 独立窗口（case_window.html/js 已 git rm），改为 `#casePanel` 绝对定位右侧（内容区宽 38%、min-width 340px、z-index 9）；`#workstage.case-open` 时 `#convPane/#stage-wrap right:max(38%,340px)` 同步让位——三栏（左导航/中对话/右病例单），对话与输入框永不被遮挡；@media 900px 断点 46%/300px。
- **服务端**（server/app.py）：来源细分 6 种（user_input/ai_draft/doctor_modified/doctor_confirmed/model_3d/kb_ref）；状态派生 `_record_status`（rejected > confirmed > doctor_modified > ai_draft > pending_review）；PUT 保存写 history（field/before/after/at/by，留 50 条）；新端点 POST record/reject；export 403 门禁（review_status 必须 confirmed）；医生再次修改自动把 review_status 重置 draft（未审核修改不得以已确认身份导出）；`_DOC_TASK_FIELD`（history/lesion_desc/differential/treatment_ref/medication_review/patient_edu/followup 正文直接落对应字段——分节解析不到时兜底）；**body_regions 不受医生确认锁**（confirm 端点与 _sync_record 双处例外，确认后 3D 新选点仍可写入）。
- **web 面板**（main.js v73）：17 字段紧凑病历单；core 7 字段（患者信息/主诉/现病史/皮损/身体部位/初步判断/医生审核）默认展开，其余进「更多病历字段」组（`moreOpen=null` 初始按内容自动展开、之后尊重手动收起）；来源徽章六色；状态标签五态；更多菜单（重新生成/历史版本/导出/拒绝）；底部固定（返回对话/保存草稿/医生确认）；保存只提交 dirty 字段（不覆盖 AI 新内容）；保存后对话流联动提示卡「医生已修改"X"，是否重新整理？」（确认才执行、不自动重写）；LLM 未连横幅（cpLlmOff 双路径判定：顶层 llm_connected 或 record.llm_meta）。
- **桥契约教训**：QWebChannel 带 `result=str` 的 slot 在 JS 端返回 **Promise**——必须回调风格 `bridge.caseRecordSave(payload, function(s){ JSON.parse(s) })`（与 embeddingInfo 同模式），同步 JSON.parse 会得到 "[object Promise]"。
- **验收**：CDP 真机 18 项全过（38%/对话可见/composer 不挡/默认展开/折叠交互/三态/对话创建 d410099ee409/对话只改现病史/无 3D 不阻塞/3D 选点 model_3d 写入/关闭重开不丢/新对话 no_case 隔离/LLM 未连横幅/确认前 403+disabled/确认后解锁/小窗 892px min-width 生效）；测试 87+412+verify3d exit0。

## v68（2026-09-13）工具扩展：15 工具统一协议 + 图片分析/病例对比 + 分层上下文管理
- **工具注册表**（server/tool_registry.py 新建）：15 工具六要素统一协议（name/label/description/input_schema/output_schema/requires_confirmation/source_required/risk_level/route）；医学判断类（鉴别/用药核对）risk=high+source_required+requires_confirmation 三门禁；TASK_TOOLS 任务映射 + tool_for_task/spec；未知工具中性缺省。app.py `_tool_traced` 升级：12 字段记录（label/风险/来源要求/确认/goal/输入摘要/输出摘要/RAG 引用/case_version/at）+ audit("tool_call")；route 名（case_writer 等）反查注册表中文标签。
- **图片分析工具**（image_analyzer，零 LLM 零编造）：上传照片先询问是否分析（不默认执行）；「分析图片」→ 质量提示（<20KB 分辨率不足/无拍摄时间）+ 15 项可见特征勾选卡（reply_card.type=image_analysis，前端 checkbox 网格+提交按钮）；提交「皮损特征：…」追加到 symptoms（不覆盖医生已录描述；例外于 draft 条件）；免责「不等于诊断」。带附件 chat 自动追加「回复『分析图片』可进行质量检查与特征采集」。
- **病例对比工具**（case_comparator，零 LLM）：「对比病例/对比 <ID>」字段级 diff（变化/新增/缺失）→ doc 卡 markdown（generated_by=local）；无 ID 多候选出选择卡；确认轮 text=「确认」时回退 prev_task_text 取 ID（plan gate 不再吞医生原始指令）；成功时清 plan gate 残留 reply（防 reply 优先级压住 doc 卡）。
- **分层上下文**（计划 §十一）：_build_context 五层（病例字段→安全层[过敏史/危险信号/医生修改永不裁剪]→当前任务→病例摘要→最近对话+工具记录+RAG 引用，CTX_BUDGET_CHARS=6000 从最新往旧装）；_maybe_summarize（30 条消息或 9000 字符触发，规则式七段 JSON confirmed_facts/…/generated_by=local-summary，原始对话保留，audit("context_summarized")）；chat 前置触发 + 快照透传 context_summary/context_compressed；main.py 压缩提示卡「🧹 上下文已压缩…」；_doc_prompt 改用分层上下文。
- **前端**（main.js v78/index.html）：toolTraceEl 共用渲染（appendDocCard/appendAgentReply/appendConvResultCard 三卡接线——doc/评估/回复产物全部展示工具执行记录）；工具条目可读化「· 图片分析 · 风险:medium · 已确认 · goal → 产出 · 引用 · r3」；main.py _push_doc_to_web/_push_assist_to_web 均携带 tool_history。
- **测试**：tests-server 77（+6：注册表协议/追踪元数据/图片分析/病例对比/上下文摘要与分层/route 反查回归）+ client 22 + vitest 412 + verify3d exit0。
- **真机 CDP**（v78）：评估确认后 review 卡工具记录（病例整理/风险/引用 RB-…/r4）；「分析图片」无附件提示→特征勾选卡（15 项+质量提示）→勾选红斑/鳞屑/边界清楚提交→症状追加「…；红斑、鳞屑、边界清楚」；「对比 204dd670f69b」确认→病例对比 doc 卡（变化/新增/缺失+免责）+4 条工具记录可追踪；10 轮零 LLM 消息触发自动摘要→「🧹 上下文已压缩」提示卡渲染、青霉素过敏保留于摘要。

## v67（2026-09-13）「进入诊断」重构为对话驱动医疗辅助 Agent（11 段需求闭环）
- **核心流程**：描述病情/进入诊断 → Agent 理解任务+初步 RAG → 确认卡（已识别/还缺少/最低信息要求/是否开始）→ 医生确认 → 病例草稿（18 字段病历单）→ 动态追问（只问缺失项）→ 资料齐 → LLM 医疗辅助评估（9 项：鉴别排序+依据/建议检查/治疗参考/恢复周期/风险提示等）→ 待医生审核 → 医生修改/确认 → 导出（门禁）。3D 选点全程可选（来源 model_3d vs 医生输入）。
- **server/app.py**：①_AGENT_COMMANDS 五类指令路由（skip_3d/add_image/save_case/reask/undo_last，零 LLM，generated_by=agent-cmd）；②_plan_gate 确认卡五要素+rag_prelim 初步 RAG 缓存（rag_task_key 去重）；③min gate：确认时缺最低三要素（症状/身体区域/病程）→ clarify_min_gate 精准追问一轮后放行；④chat merge 防污染：确认短语/指令文本/纯指令不并入病例字段；⑤doctor_modified 保护：AI 不得覆盖医生手动字段，case_edit「重新整理」一次性授权（allow_ai_override 消费制）；⑥danger_signals 第 18 字段+分节映射；⑦两阶段 RAG：初步（确认卡 top_k=3）+最终（doc 任务按症状+主题检索真书，citation_id 去重）；_stamp_citations 全链路 retrieved_at；⑧9 项评估输出映射（_sections_from_draft 排序+依据+恢复周期合并 follow_up）+ _analyze_prompt schema 扩展；⑨chat/supplement 前 case_prev 快照（undo_last 回退）。
- **shared/derm_core.py**：normalize_llm_draft 解析 recommended_exams/treatment_reference/recovery_period（含 camelCase 兼容、citation 白名单）。
- **client/web**（main.js v75/index.html）：①CP_FIELDS+danger_signals；②引用详情卡（[id] 资料名/作者/出版社/章节/页/片段 140 字/检索时间，textContent 防 XSS）+CSS；③probeLlm 已配置但探测失败 600ms 自动重试一次；④appendDocCard source=agent 分支（「✦ Agent 指令响应（本地执行，未调用 AI）」横幅+中性状态，不污染 APP_STATE.llm 全局连接态）。
- **client/agent_client.py**：_render 优先级修复——reply（本轮 Agent 响应）提到残留 doc 之前（缺陷：指令回合快照残留旧 doc → 误渲染「未连接 LLM」+旧文书卡）；Mock 字段同步 danger_signals。
- **真机（CDP，LLM 网关 gpt-5.6-terra）11 项端到端**：直接对话建病例确认卡五要素+初步 RAG 真书 3 条；确认后真实 LLM 分析 awaiting_review；面板 18 字段/鉴别排序+依据/引用详情（[CIT-AD-001-S1] 名称+版本+片段+检索时间）；跳过3D 指令零 LLM；3D 选点 model_3d 上行；min gate 追问闭环（缺「身体区域、病程」→补充→awaiting_review）；differential 最终 RAG 真书引用 RB-TEST-02；面板编辑→医生修改徽章；确认→已确认→确认态修改→医生已修改/draft 回退；导出门禁（draft 403/confirmed 200/回退后 403）；probeLlm 失败一次自动重试（probeCalls=2）。
- **测试**：pytest 93（87 既有+5 v67 专项+1 _render 优先级回归）+ vitest 412 + verify3d exit0；缓存 v73→v75。
- **LLM 配置**：网关/模型/Key 只存 server/data/llm_config.json（服务端管理；UI 仅显示尾号 4 位）；环境变量 LLM_BASE_URL/LLM_API_KEY/LLM_MODEL 可覆盖；未连接时零伪造（llm_unavailable 卡+手动填写通道）。

## v70（2026-09-13）方向反转：对话优先 · 结果按需打开（v69 自动导航降级为只声明）
- **需求**：LLM 回复后不自动跳转任何窗口/不弹病例/3D/分析页；每条回复下方按实际产物渲染「工作结果」按钮区；所有工作窗口由医生点击打开；回复与按钮分离（按钮只来自服务端返回的 artifacts/available_actions，前端不做文字猜测）；失败只显示真实错误+重试。
- **server/artifacts.py（新建派生层）**：ARTIFACT_KEYS 六产物 + ACTION_LABELS 八动作中文标签 + _ACTIONS 有序映射；build(sess,snap)：失败态（llm_error/llm_unavailable）→只 retry(+open_case/export_case)；否则 artifacts = 本轮差异（turn_products 指纹）∪ 结果卡承载（carries=nav.action∈report/doc/closed）；model_3d_context 纯 presence；全 False 只留 open_3d；_confirmed（sess.review_status=approved 或 record.review_status=confirmed）解锁 export_case；summary 中文摘要；GET /api/artifacts/map 只读导出（前端按钮标签一致性测试锚点）。
- **server/app.py**：_turn_fingerprint（值必须 JSON 原生 [count,md5] 列表——tuple 落盘读回变 list 恒不等，曾致闲聊轮虚报产物）；_stamp_message 在 _screen 之后盖章（否则指纹看不到本轮产出）；msg_seq 消息级 message_id；_rebuild_snapshot 回注一次性 nav_intent（_screen 内部 pop 掉后重建快照会丢）；_persist_msg_artifacts（message_id→artifacts/actions/summary/authenticity，近 12 条）随快照透传；_snapshot 增 message_id/turn_products/msg_artifacts；_apply_nav 末尾派生 artifacts（失败 setdefault 兜底不 500）；nav.py resolve() 增 "auto": False 语义声明。
- **client/agent_client.py**：_render 透传 message_id/artifacts/available_actions/result_summary/authenticity/msg_artifacts。
- **client/main.py**：_v70_fields 统一搬运；reply 分支 __pushAgentReply、_push_doc_to_web、_push_assist_to_web（+result 参数，review/report/closed 三调用点）全部携带按钮区数据；_on_restore_done 先 __setMsgArtifacts 再 __restoreConversation。
- **client/web/main.js**：①window.__nav 语义反转——决策只写入 NAV.suggested+日志（suggested_workspace/auto_switched:false），绝不 navSetWorkspace/高亮/闪卡/定位引用；保留乱序/过期病例守卫、focus_input、对话区滚动、case_id 持久化；②新增 v70 模块：WORK_ACTION_META 八动作中文标签、MSG_ARTIFACTS 存档、window.openWork 唯一打开入口（open_case→面板；open_analysis→卡片滚动+闪烁；open_sources→面板+引用定位（未就绪挂起 pendingSources）；open_medication/open_risk→字段高亮；open_3d→model_primary；export_case→不绕门禁只提示；retry→原样重发；跨病例 wantCase≠NAV.caseId→stale_case 拦截）；renderWorkActions（无产物不渲染按钮；未知动作过滤）；③__pushAgentReply/appendDocCard/appendConvResultCard 三卡接线；__restoreConversation 历史卡按 message_id 各取 MSG_ARTIFACTS 自己的按钮；④navToCase/navToSources 改走 openWork（医生手动）；⑤navLeave3d 写回结果以确认卡落对话流（不只状态行）。
- **测试**：tests-server +8（v70 派生层/导航不执行/唯一 message_id/闲聊零虚假按钮/失败只 retry/确认解锁导出/分析轮产物）；vitest derm-nav.test.ts 重写 23 用例（只声明不执行/openWork 全动作/跨病例守卫/按钮区渲染零造假/消息级存档/3D 写回确认卡）；pytest 114 + vitest 435 全绿 + verify3d exit0。
- **真机 CDP 五场景+重启恢复**：闲聊轮真实 LLM 回复停留对话区（auto_switched=false，按钮=服务端派生查看病例+3D）；planning 确认轮聚焦输入框无产物按钮；确认执行→真实 RAG 鉴别分析（RB 引用）→结果卡摘要「已生成病例草稿·已完成辅助分析·已找到 3 条知识库引用」+四按钮+不跳转；点查看病例→case-open；点查看 RAG 引用→引用区高亮滚动定位；3D 手动打开 model_primary→选点→返回→写回身体部位+对话确认卡；重启恢复 7 张历史卡 4 张各带自己的按钮且工作区保持 chat。
- **注意**：kill app 父进程不带走 8765 server 子进程（孤儿占端口会让新 app 连旧代码 server）——重启验证前先 pkill server。

## v71 知识库管理修复（2026-09-14）

**用户症状**：①批量导入医学书籍后没有显示进度、新书籍不出现；②不能管理知识库、不能删除现有书籍。

**真机复现定位的三个根因**：
1. **QWebChannel 大消息丢数据**：15.7MB 书 base64 经 bridge 传输后 Python 侧收到 **0 字节**（books.json 实证 BOOK-0007 size=0 → empty_text failed）——大书经 kbImport slot 导入必失败；0.67MB 内可过（Chromium IPC 消息上限）。用户此前导入的书实际部分成功入库，但 UI 无反馈。
2. **GUI 主线程冻结**：kbImport slot 同步执行 import_book（解析+切分持 _lock），大书期间 Qt 主线程阻塞，界面无响应、进度不可见。
3. **删除语义缺失**：只有软删 soft_delete_book（quarantine=「⏸ 停止检索（可恢复）」，书仍留在列表），无物理删除——用户视角即「不能删除」。

**修复**（server/app.py + server/rag.py + client/main.py + client/web/main.js + index.html v81）：
- **导入走 HTTP 直连**：前端 importKbFiles 重写为 fetch POST /api/kb/books（FastAPI def 端点在线程池跑，不冻 GUI；无消息大小上限）。启用链路：server 加 CORSMiddleware（allow_origin_regex ".*"，file:// null origin）+ 自定义 PNA 预检中间件（Chromium 对 file://→localhost 强制 Access-Control-Request-Private-Network 预检，旧版 CORSMiddleware 打 400 → 短路返回 204+PNA 放行头）+ QWebEngineSettings.LocalContentCanAccessRemoteUrls=True（main.py）+ WebBridge.serverBase slot 提供基址。bridge.kbImport 保留为离线降级。
- **导入进度**：逐本顺序导入 + kbStatusMsg 逐本刷新（「导入 i/N：文件名 → 已入库（片段 N）/失败（原因）」）+ 导入互斥防重入（kbImportBusy + 按钮禁用）+ 完成自动 refreshKbBooks。
- **物理删除**：rag.delete_book（books.json 移除，不可恢复）+ DELETE /api/kb/books/{id}/hard 端点 + WebBridge.kbDeleteHard + UI 每行「🗑 彻底删除」按钮（window.confirm 二次确认含书名与不可恢复警示，红色警示样式）。「⏸ 停止检索（可恢复）」软删保留互补。
- tests-server 新增 test_kb_hard_delete_and_cors（物理删除/重复删 not_found/软删恢复不变/CORS null origin 放行）。

**真机 CDP 验收**（v81）：15.7MB 大书 HTTP 导入成功（8934 chunks，导入期间 UI 计数持续=不冻结）；生产链路 importKbFiles（File→FileReader）完成态「导入完成：成功 1 本，失败 0 本」+ 列表即时刷新；批量 2 本「成功 2 本」；🗑 彻底删除 confirm 文案+列表 9→8+books.json 实证移除；HTTP DELETE 清理测试书。测试书全部清理（保留 5 本内置演示书）。

**教训**：QWebChannel 传参方式（回调风格）没问题，但**大 payload 会静默丢数据**（>~1MB 风险区，15MB 实证归零）——大文件传输一律走 HTTP 直连或分块；file:// 页面 fetch localhost 需同时满足 LocalContentCanAccessRemoteUrls + CORS + PNA 预检三件套。CDP 探测跨调用共享 window 变量（读错变量名会误判挂起）。

## v73：安卓手机版（PWA 复用 Windows 端界面，2026-09-16）

- **方案**：不新做 App，给 server 托管的 `client/web`（Windows 端同款对话式界面）补 PWA 能力——手机浏览器访问后「添加到主屏幕」即成安卓版，复用全部现有对话/病例/3D/RAG 链路。
- **新增**：`client/web/manifest.webmanifest`（standalone/竖屏/theme #10151c/icon）、`client/web/sw.js`（壳缓存 v1；`/api/` 一律直连不缓存）、`client/web/icon.svg`（蓝底 #1683f7 白十字，512 viewBox，any+maskable）；`client/web/index.html` head 加 manifest/theme-color/icon 引用 + SW 注册（insecure/file:// 下静默跳过）。
- **验证**：静态托管即时生效（无需重启 server）——4 资源 200 且 Content-Type 正确（manifest+json）；Chrome 真实浏览器 SW 注册成功（scope /）、manifest link 生效。
- **限制**：PWA 完整安装需 secure context（https/localhost）；手机经局域网 http 访问时「添加到主屏幕」为快捷方式形态（Chrome flags 或后续上 https 可解锁完整 PWA）。

## v74：安卓手机端 APK（WebView 壳真 App，2026-09-16）

- **形态**：用户确认要 APK 安装包。零 Gradle 路线（aapt2+javac+d8+zipalign+apksigner 手动构建），产物仅 16K，debug 自签（10000 天）。
- **工程**：`client/android/`——AndroidManifest（INTERNET/CAMERA/RECORD_AUDIO、明文 http、竖屏、label 肤智）、`MainActivity.java`（WebView 加载电脑端 server 同款界面；地址 SharedPreferences 持久化+默认 172.20.10.2:8765；连接失败弹改地址对话框；ACTION_GET_CONTENT 文件上传桥+多选；摄像头/麦克风运行时授权后 reload；外链交系统浏览器；back 键走 WebView 历史）、`res/drawable/ic_launcher.xml`（蓝底白十字 vector）、`build_apk.sh`（6 步构建脚本，可重复）。
- **工具链**：`~/derm-build-tools/`（沙盒 HOME 重映射路径）——Temurin JDK 17.0.20.1 arm64（清华 Adoptium 镜像）、build-tools 34.0.0 + platform android-34-ext7_r03 + cmdline-tools（腾讯 Nexus 镜像；dl.google.com 直连极慢 ~2.5MB/min 不堪用）。**platform-34 的 zip 实名 platform-34-ext7_r03.zip（非 platform-34_r02.zip）**。
- **交付路径**：APK 复制为 `client/web/dermagent.apk`，server 静态托管 → 手机浏览器 `http://<电脑IP>:8765/dermagent.apk` 直接下载安装。已验证：apksigner verify ✓、aapt2 badging（minSdk24/target34/label 肤智）✓、HTTP 200 且字节一致 ✓。
- **限制**：手机须与电脑同网；server 关闭则 App 内页面不可用（壳可改地址重连）；debug 证书安装时会有未知来源提示；IP 变化时 App 内改地址即可，无需重装。
