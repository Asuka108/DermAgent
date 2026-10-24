
## 默认 3D 模型更换（2026-09-06）：3d66 人体模型

- 用户附件 `3d66.com_19298115.glb`（89.6MB，1,295,502 三角面，17 网格/17 PBR 材质，0 贴图纯材质色，无骨骼/动画，无 Draco 扩展）→ 复制为 `client/web/vendor/models/Custom3d66.glb`（sha256 1d65241f… 校验一致）；
- `main.js` MODEL_FILES 首位换成 Custom3d66（链：3d66 → Xbot → Michelle → CesiumMan → 程序化），attachEyes 移到 index===1（Xbot 专属）；缓存版本升至 v13；
- 材质修正（metalness≤0.05/roughness≤0.85）对其生效——模型原材质 roughness=0 会镜面反光，实测显色正常（肤色 T-pose 人台）；
- 来源：3d66.com（模型 ID 19298115），**授权待确认**——比赛使用前须在 docs/data-license-register.md 补登记许可；89.6MB 体积对 PyInstaller 打包和加载时间有影响，演示机需预留加载等待；
- 验证：QtWebEngine 真机 glbLoaded=true / triangles=1296342 / regionGroups=19 / 点击拾取「已选（1 处，再点可删除）：腹部」✓ / 正背截图显色正常；客户端重启零 JS 错误。

## v23（2026-09-07）：解剖位置语义映射逐点校准 + 来源/置信度透传

- **根因一（方位词系统性翻转）**：`surfaceAspect` 同时放入 front(v=nz) 与 back(v=-nz) 冗余反向项，二者 |v| 恒相等、sort 稳定性使 front 恒居首 → 一切背面点永远输出「前侧」（大腿后侧报「前外侧」、臀报「前侧」）。修复：每轴只保留有符号主导方向（`main.js` surfaceAspect）。
- **根因二（内法线）**：Custom3d66 主身体 mesh 顶点序使 `face.normal` 为几何内法线（python 解析 GLB 实测：下半身前面法线朝前率 0.00）。`recordHit` 按「可见面外法线与视线 dot≤0」数学必然性校正，并记录 `normal_corrected` 字段。
- **躯干/臀上中下份**：body/torso 类 zone（有 yband 且非 border/fold/margin/xiphoid 交界带）注入 yRel → 输出「上份/中份/下份」——臀部不同纵深处点击描述不再相同（用户需求第 3/5 条）。
- **来源/置信度透传**（用户需求第 8 条）：记录新增 `source`（人工校准/逐面语义表（真实网格三角面）/解剖锚点（几何实测）/几何型带（近似推断）/无法可靠识别），展开详情显示「位置识别来源 + 置信度」两行，`buildStructuredPayload().regions_full` 每条携带 `source + confidence`。
- **v23 逐点审计（QtWebEngine 真机，29 点全 PASS）**：臀上/中/下份×4 变体（上份/外侧/后侧/后下方各得其所）、臀褶、臀腿交界（语义不足 conf 0.55 如实标注）、大腿后上/中/下（后外侧近端/后侧近端偏中/后侧中段偏远）、大腿前上/中/下、大腿近髋/中段/近膝（近端/近端偏中/远端）、胸骨/胸前中带/胸下缘/剑突/上腹（全部前侧+yband 中份）、左右拇指各 4 节 8/8（掌骨区/近节/远节/指尖，方位各异）。
- 审计方法学：探针相机置于候选面外法线方向（模拟用户转到正对该表面点击），屏幕像素射线预验证命中面语义===目标 zone 才算通过——忠实模拟真实点击链（onPick 全链）。
- 「chest zone 无候选面」为审计脚本幽灵命名：v20 起表内已无裸 chest（被 sternum/breast/chest_midline 等细分取代），运行时不受影响。
- 缓存版本 v25（web/index.html + webui/index.html）。
- 回归：v23 审计 29/29 ✓ · verify_3d ✓（glbLoaded/面语义 22/22/多点独立/×删除三同步）· v22_final G1-G6 ✓ · v21_accept/v21_composer failCount=0 ✓ · client pytest 11 ✓ · server pytest 16 ✓ · vitest 412 ✓ · node --check ✓。

## v31（2026-09-11）：画布自适应布局变化——修复「极端情况选不到脚」

- 根因：`onResize` 只挂 window resize（web/main.js init），标签行增减改变的是 `#stage-wrap` 高度（flex 布局）不触发 window resize → 画布保持旧尺寸/旧纵横比，底部溢出被 `#stage-wrap`（overflow:hidden）裁掉 → 多标签 + 放大躯干时脚不可见。
- 修复：init 处 ResizeObserver 观察 `#stage` 兜底触发 onResize；`onResize` 加 w/h<2 过渡态守卫（防 0 尺寸产生非法相机纵横比）；提示文案（index.html 初始 + main.js 模型加载后）补「找不到部位时点『重置』复位视角」。
- 验证：一次性探针（QtWebEngine 真实页面 + 50 次真实 canvas click）——chips=50、标签行限高生效（scroll 523/client 106）、stage-wrap 746→640、画布绘制缓冲 1:1 跟随收缩（746→640）、画布矩形零溢出（cssBottom==wrapBottom）、camera.aspect 同步 1.7909→2.0875，4 断言全绿；verify_3d ok:true（男性 40 校准点全 PASS；唯一失败 female.leftToes 为上轮中断遗留，与本轮无关）；vitest 412 ✓；pytest 11 ✓；node --check ✓。
- 备注：renderer 未设 setPixelRatio（默认 1），绘制缓冲与 CSS 尺寸 1:1；探针首跑还实证 0 尺寸布局下新守卫正确拦截 setSize（canvas 保持 300×150，未产生非法 aspect）。

## 近期改动速览（2026-09-09~10，详情见 git log）

- v29~v30b 语义表分带校准（肩前带/前臂守卫/臀腿分带/臂根后带/后腋褶 z 界/上臂守卫放宽）；侧别从观察者镜像翻转为解剖学患侧（x<0=右）；浅色主题三件套（index.html/main.js/main.py）；标签区限高 3 行+滚动。

## 2026-09-11 · v46 语义澄清：LLM 模型切换 vs 人体模型（提交 e06a1ff）

- 用户澄清：v44 参考图里「把切换模型按钮放在发送的那里」指的是 **LLM 大模型**切换（Codex 截图输入框右下角「5.6 Sol 高」），不是人体 3D 模型；人体模型切换回老位置。
- **composer 发送旁 = LLM 大模型切换**（#llmModelSelect，胶囊样式 max-width 150px）：选项 = 预设常用模型（qwen-max/qwen-plus/gpt-4o/deepseek-chat/claude-sonnet-4）∪ 当前配置模型（不在预设则插入并选中）；切换走 bridge.llmConfigSet（base_url 取 curLlm 缓存、api_key 传空 = 保留已存 Key，只改 model）；成功后 refreshLlmStatus 回刷，失败回显实际配置；无 bridge → disabled +「LLM 离线」选项，不伪造可用模型。启动即初始化（initBridge 回调 + 底部序列双保险）。
- **人体模型 #modelSelect 回左侧栏老位置**（view-btn 组之后、hint 之前，v43 同款样式），refreshModelSelect 逻辑不变。
- 教训：用户说「切换模型」在聊天应用语境下默认指 LLM 模型，不是业务对象（人体 3D 模型）——布局需求要先确认对象语义再动手，v44 理解错了方向。
- verify_3d settingsLayer 19→21 断言：modelInComposer 改为 modelInToolbar（toolbar 含且 composer 不含），新增 llmSelInComposer/llmSelOffline（无 bridge 禁用+离线文案）。
- 验证：verify_3d exit 0（21 断言全 true/errors 空）、vitest 412、client pytest 11、tests-server 27。缓存号 v45→**v46**；客户端已重启（进程 10380）。
- 遗留：LLM 模型预设列表为前端硬编码（不含密钥，可后续改为从服务端 /v1/models 拉取）；§16b 其余项不变。

## 2026-09-11 · v47 LLM 思考等级切换（提交 d39f01a）

- 用户需求：在切换 LLM 模型那里（composer 发送旁）加思考等级切换（参考 Codex「高/中/低」推理力度）。
- **UI**：#llmThinkingSelect 紧贴 #llmModelSelect（胶囊 82px，弱化灰字），四档：思考：标准（不发参数，行为与旧版一致）/低/中/高；切换即写配置（llmConfigSet 保 Key/base_url/model 只改 thinking_level）；无 bridge → disabled+「思考：离线」。i18n 双语（Thinking: Std/Low/Med/High），状态行附「思考 等级」。
- **配置链**：llm_config.json 加 thinking_level 字段；客户端 read/write_llm_config + bridge llmConfigGet/llmConfigSet 白名单归一（low/medium/high 之外→""）；原生 SettingsDialog 同步加「思考等级」QComboBox（findData 回填）。
- **服务端映射**：_chat 加 reasoning_effort 参数（OpenAI 兼容字段）；非空时注入请求体；**失败不变性——网关返回 400/404/422（不认识该字段）自动去掉参数重试一次**，草稿照常生成；401/429 等不重试如实报错。空等级完全不传参（向后兼容旧网关）。
- **测试环境隔离（重要教训）**：医生本机已在 UI 保存真实 LLM 配置（gpt-5.6-terra）后，tests-server 的 health 断言挂（llm=server 而非 rule-fallback）且 demo 用例真发外部请求（整套 15s→82s）。修复：新增 autouse fixture _isolated_llm_config 把 LLM_CONFIG_FILE 统一指向 tmp_path 不存在文件——测试集恢复确定性，不再受本机配置影响。
- 教训：测试集不能依赖「用户没配置」这种环境状态；凡读用户可写落盘文件的路径，测试都要 autouse 隔离。
- verify_3d settingsLayer 21→23 断言（thinkingSelInComposer/thinkingSelOffline）。
- 验证：verify_3d exit 0（23 断言全 true）、vitest 412、client pytest 11、tests-server **29**（+思考等级流转/网关拒绝降级）。缓存号 v46→**v47**；客户端已重启（进程 13762）。
- 遗留：thinking 等级对不支持 reasoning_effort 的模型实际无效（服务端降级但草稿无感知提示）；§16b 其余项不变。

## v48（2026-09-11）本地 embedding 模型 BAAI/bge-base-zh-v1.5 安装与混合检索接通
- 用户新装模型：BAAI/bge-base-zh-v1.5（412MB，ModelScope 下载——本机 huggingface.co/hf-mirror 不通、modelscope.cn 可达），落盘 server/data/models/bge-base-zh-v1.5/（server/data/ 整体 gitignore）；依赖 sentence-transformers 经清华源装入 client/.venv
- 新增 server/rag_embed.py 隔离模块：ensure_loaded() 惰性加载（local_files_only=True、device=cpu、锁内幂等），manifest 落盘 server/data/rag/embedding_manifest.json（权重 sha256+字节，§16.4 哈希记录），STATUS 状态机 not_installed/ready/error；rank() 用 bge 检索指令前缀 + normalize_embeddings + 纯 Python 余弦
- rag.py search()：BM25 与语义召回 RRF（k=60）融合；向量通道任何异常降级纯 BM25，retrieval_mode/embedding_state 绝不伪造；EMBEDDING_STATUS 同步 detail
- UI 三态文案（client/main.py rag_line + main.js ragLine）：已加载 / 未安装 / 加载失败（带原因）；缓存 bump v48
- tests-server 30 例：新增 autouse _isolated_embedding（防本机真模型进测试）+ test_kb_hybrid_rrf_fuses_semantic_ranking（语义排名改变 BM25 排序、异常降级）
- 教训：test_api.py 修 fixture 时用 Edit 把 _isolated_llm_config 整块替换掉了而非追加，health 测试立刻失败——改 fixture 必须确认是追加不是替换
- 验证：verify3d exit0（settingsLayer 23 断言）+ vitest 412 + client pytest 11 + tests-server 30；真机冒烟 ensure_loaded ready + manifest 哈希落盘 + hybrid 检索真实返回（湿疹章 RRF 0.0328 登顶）

## v49（2026-09-11）设置页向量模型自选安装
- 用户需求：「给程序设置添加个按钮，embedding模型安装，可以自己选择embedding模型」
- rag_embed 重构为多模型目录：MODEL_CATALOG 三档（bge-small ~100MB / bge-base ~410MB 推荐 / bge-large ~1.3GB），统一 bge-zh 文件清单从 ModelScope 下载；manifest 扩展为 {active, models:{short:{sha256,bytes,installed_at}}}，兼容 v48 平铺格式自动迁移；install() 后台线程 + .part 原子落盘 + 新装自动激活；set_active() 已安装模型间切换；STATUS 增加 installing 进度字段（下载中检索仍按未安装处理，绝不半途启用半成品）
- 链路：WebBridge 新增 embeddingInfo/embeddingInstall/embeddingSetActive 三槽（审计 kb_embedding_*）；server 新增 /api/kb/embedding（info/install/activate）三端点供独立部署
- UI：设置页知识库 pane 新增「🧠 向量模型」区块（embModelSelect 下拉带使用中/已安装标注 + embInstallBtn 双语义按钮 + embProgressLine 进度/失败行）；下载中 1.2s 轮询；缓存 bump v49
- 语义修正：catalog 的 active 标注以 installed 为前提（active 指针指向未装模型时如实回落，不留"使用中但未安装"的矛盾态）
- 教训：改 rag_embed 常量结构时必须同步 _isolated_embedding fixture（MODEL_DIR→MODELS_ROOT+MANIFEST_FILE），monkeypatch.setattr 对已删除属性会 AttributeError 炸掉全部测试
- 验证：verify3d exit0（settingsLayer 24 断言含新 embUi）+ vitest 412 + client pytest 11 + tests-server 31；真机冒烟 manifest 迁移 + active 识别 + rank 语义合理

## v50（2026-09-11）修复向量模型状态行误显示「未安装」
- 用户截图反馈：设置页「RAG 运行设置」显示「本地向量模型：未安装」，但模型实际已装
- 根因：rag.EMBEDDING_STATUS 只在 rag.search() 执行时同步 rag_embed.ensure_loaded() 的真实状态；客户端启动后未检索过 → 状态停留在初始 not_installed。UI 状态行/检索模式都读这个字典，因此一直显示未安装（做一次检索才会"变对"）
- 修复：rag.py 新增 embedding_status()（惰性 ensure_loaded + 同步 EMBEDDING_STATUS + 返回 dict，异常如实降级）；kb_list_payload（Web 与原生共用）、server kb_books 端点、原生 SettingsDialog rag_line 统一改用；main() 启动加 daemon 预热线程（后台加载 400MB 权重，不阻塞界面）
- main.js 未改 → 缓存不 bump；verify3d 中途一次 settingsLayer.opens flake（与桌面客户端并行的偶发时序），重跑 exit 0 确认非代码回归
- 教训：重启客户端 pkill -f "client/main.py" 匹配不到实际 cmdline（…/MacOS/Python main.py），要用 pkill -f "MacOS/Python main.py" 或 pgrep -f main.py 定位，否则旧实例残留双开
- 验证：tests-server 31 + client pytest 11 + verify3d exit 0；冒烟：全新进程未检索调 embedding_status() 即返回 ready

## v51（2026-09-11）澄清顶栏「服务地址」语义防误填
- 用户疑问：顶栏「接入：server·服务端 + 服务地址」与设置页「LLM 与模型」是否功能重复（截图：把 LLM 网关 https://www.codex5.net/v1 填进服务地址 → SSL CERTIFICATE_VERIFY_FAILED 弹窗）
- 事实链路（不重复，是两段）：顶栏接入 = 客户端问谁（mock=本地规则管线；server=HTTP 连 DermAgent 服务端 server/app.py）；设置页 LLM 连接 = 服务端再问哪个大模型网关（base_url+Key+模型+思考等级）。服务地址填 LLM 网关是错配 → SSL 失败
- 修复（纯文案）：placeholder →「DermAgent 服务端地址（如 http://127.0.0.1:8765，非 LLM 网关）」；server 模式缺地址报错、服务不可用弹窗均注明「LLM 连接在设置页配置」
- 教训：单机用户对「服务地址」的默认联想是 AI 服务地址；凡是让用户填 URL 的地方必须写明填「谁」的地址
- 首次用 pkill -f "MacOS/Python main.py" 成功杀掉旧实例（v50 教训的修正用法）
- 验证：client pytest 11 + verify3d exit 0

## v52（2026-09-12）流程指引：步骤条 + 下一步自动弹窗
- 用户反馈：「流程太复杂了，一不知道下一步怎么做……添加流程指引，直接弹出下一步要进行的窗口」
- 实现：① composer 上方常显 5 步流程条（①选择部位→②提交病例→③补充资料→④查看AI分析→⑤医生审核确认，done/cur/todo 三态）；② window.__pushFlow 新桥接入口（main.py _push_flow_to_web，_show_result 每次服务端交互统一推送，mock/server 同契约）；③ ask_more → 自动弹出「🧭 下一步操作指引」弹窗：缺失字段 chips + 弹窗内直接输入补充（Enter/按钮发送，复用 sendChat 全链路）；弹窗提示明确「字段：值」只是示例不是强制、自由文本会并入症状、最多追问 2 轮（直接消化上一轮"为什么没有输出/格式"困惑）；④ review → 不弹窗打断，自动展开辅助判断面板（下一步窗口直接送到眼前）；⑤ report/closed → 步骤条终态 + 点击流程条随时可看指引
- agent_client：MockAgentClient ask_more 返回与 ServerAgentClient._render 均结构化透传 missing（此前只在 question 文本里）
- 教训：verify_3d 断言区 Edit 时 old_string 尾部（if sl.get...）被一并匹配，把 settingsLayer 检查行复制进了 flowLayer 循环（settingsLayer.pushFn 假失败）；数字型探针（barSteps=5）不能用 `is not True` 断言，要单独数值比较
- 验证：client pytest 13（+2：ask_more 携带 missing / _render 透传 missing）+ tests-server 31 + vitest 412 + verify3d exit 0（settingsLayer 24 断言 + flowLayer 17 断言全过，弹窗自动开/chips/步骤推进/i18n/复原）

## v53（2026-09-12）打开程序自动启动本机服务端
- 用户痛点：「每次都要启动那个server服务端吗，不能直接集成在程序里，打开程序自动启动吗」
- 实现（client/main.py）：_server_health_ok()（/api/health status=ok 判定）+ _ensure_local_server(url)——server 模式且地址为 127.0.0.1/localhost/::1（须带端口）时，无健康服务端就 Popen 拉起 server/app.py（PORT 取自服务地址、stdout 合并落 client/data/server.log、轮询 30s）；已有健康服务端复用绝不重复拉起；远程地址/无端口不托管；server/app.py 路径为模块常量 SERVER_APP_PATH（可测）
- MainWindow：server_ready Signal（后台线程→主线程状态栏，不阻塞 UI）；_autostart_local_server 在 _load_settings 后触发（mock 模式跳过）；closeEvent 只 terminate 自己 spawn 的子进程（kill -TERM 强杀不走 closeEvent，残留服务端下次启动被复用——自愈）
- 状态栏文案：「正在启动本地服务端…→✅ 已自动启动 / ✅ 检测到已在运行 / ⚠️ 启动失败（见 client/data/server.log）」；审计 server_autostart/server_autostart_failed/server_autostop
- 教训：Bash cwd 漂移再次坑验证——nohup .venv/bin/python main.py 在错误 cwd 下静默失败（nohup: No such file or directory），导致冒烟误判"没拉起"；启动类命令必须绝对路径或显式 cd
- 真机冒烟：A) 手动服务端在跑→客户端复用（无 autostart 记录、无重复进程）；B) 无服务端→客户端拉起子进程 pid 20187（audit server_autostart，health 200）；C) Cmd+Q 退出→子进程被收（audit server_autostop）
- 验证：client pytest 16（+3：远程/复用守卫、Popen 带 PORT env、子进程即退报 timeout）+ tests-server 31 + vitest 412 + verify3d exit 0；web 未改缓存不 bump

## v54（2026-09-12）顶栏接入控件收进设置页「通用」pane
- 用户确认：v53 自动托管后，顶栏「接入：server·服务端」下拉 + 服务地址输入框多余（截图询问能否删除）
- 方案 B（收纳不删功能）：mock 是断网/托管失败的离线退路、服务地址是跨端续办与自定义端口入口 → 顶栏删除两控件，入口移到 Web 设置页「通用」pane 新「🔗 接入方式」卡片（模式下拉 server（推荐）/mock + 地址输入 + 保存 + 状态行，无 bridge 整体禁用降级）
- main.py：新增模块级 _read_agent_settings()（唯一读取入口：无文件/坏 mode/空 url 回落默认 server + http://127.0.0.1:8765，坏值不伪造）；MainWindow 接入设置改数据字段 agent_mode/server_url_cfg（mode_combo/self.server_url 全删，7+ 引用点改写）；WebBridge 新增 agentSettingsGet/Set 槽（Set 保存 settings.json 后：切 server 立即 _autostart_local_server 探测/带起服务端，切 mock 更新状态栏）
- 默认值语义变化：无 settings.json 新装用户默认 server（v53 前默认 mock）——自动托管下打开即用，mock 明确是退路
- web：index.html 通用 pane 卡片 + 缓存 main.js?v=54；main.js refreshAgentSettings/toggleAgentUrlRow/saveAgentSettings（地址行随模式显隐；server 模式校验 http(s) 前缀；保存后提示"正在检查/启动本机服务端"），设置页打开与启动时均刷新；i18n 三字典补词条
- 提示语同步：服务端启动失败/服务不可用弹窗的"切回 mock"指引改为「设置 → 接入方式」
- 教训：改 UI 控件为数据字段时，先 grep 全部引用点再动手（本次 7 处一次改净，无残留 AttributeError）
- 验证：client pytest 17（+1：_read_agent_settings 默认/透传/坏值回落）+ tests-server 31 + vitest 412 + verify3d exit 0（settingsLayer 新增 agentUi/agentDefaultServer/agentOffline 断言全过）
- 冒烟补充：顶栏截图确认两控件已消失（历史/分析结果/截图/设置四钮）、状态栏「接入：server」（数据字段渲染）、服务端复用 health 200；osascript `click at` 需辅助访问权限（本机被拒 -25211），UI 点击自动化不可用时改用数据链路证据（settings.json + 状态栏 + verify_3d 实渲染）

## v55（2026-09-12）流程条点击步骤直达操作位置
- 用户反馈（附流程条截图）：「点击下面进度应该跳转到需要执行的地方」——v52 流程条整条点击只开说明弹窗，不跳转
- 实现（main.js）：renderFlowBar 给每个 pill 挂 dataset.step、已到达（done/cur 且非 closed）加 .goto 可点态；goToFlowStep(n)——①选择部位→清场（关医生资料/辅助/历史/设置抽屉）+ 开已选位置列表，3D 全视野；②提交病例/③补充资料→聚焦底部输入框（ask_more 中的 ③ 直接弹补充弹窗，弹窗内可发送）；④查看 AI 分析/⑤医生审核确认→展开辅助判断面板并 scrollIntoView 到诊断辅助/审核与审计分区。未到达步骤/已关闭 → 只弹指引弹窗，不伪造进度；点流程条空白/「不知道下一步」→ 仍开指引。CSS 加 .goto hover（cur 保持蓝底白字）
- 教训：verify_3d 探针里 window.__pushFlow 会触发 renderFlowBar 重建 DOM——探针缓存的 pills 立即 detached，后续 .click() 不再派发；pushFlow 后必须重新 querySelectorAll 取节点（本次 futureBlocked 过而 focusChat/jump4 假失败即此因）
- 验证：verify3d exit 0（flowLayer 新增 stepNav 4 断言：cur=1 点⑤/②只弹指引、cur=4 点②聚焦输入框、点④展开辅助面板）+ vitest 412 + client pytest 17 + tests-server 31；缓存 bump main.js?v=55

## v56（2026-09-12）规范门诊病历单（患者基本信息 + 病历结构重排）
- 用户反馈：「没有规范的病例窗口，病例窗口应该像实际病例单一样上面有的信息」——clinicDrawer 此前只有资料录入视角，缺真实病历单的抬头结构
- 实现（web）：clinicDrawer 重写为「📋 门诊病历单」——顶部「患者基本信息」patient-grid（姓名/性别 select/年龄/门诊号/就诊日期 5 控件，日期 readonly 自动填今天）；分区按真实门诊病历结构重排：主诉/现病史 → 既往史/过敏史/用药/当前治疗 → 结构化检验 → 报告文字导入 → 查体影像
- 门诊号自动生成：OPD_SEQ 会话计数器，ensurePatientAuto（collectClinical 开头调用）——日期空→填 today；门诊号空且姓名有值→生成 OPD-YYYYMMDD-NNN 写回输入框（可手改，下次收集不重复生成）
- 载荷与脱敏（main.js sanitizeClinic）：clinic payload 新增 patient 子对象 {name,sex,age,no,date}；**直接身份标识类别替换**——name→「［姓名］」、no→「［门诊号］」，性别/年龄/就诊日期是临床信息原样保留（本机表单保留原文，沿用既有脱敏原则）；辅助面板选择区自动出现「患者基本信息」行（CLINIC_SELECT/LABELS 加 patient，meta 显示 已填/空）；clinicalHas/clinicalSummary/clinicClear 同步
- 服务端（server/app.py）：CLINIC_ALLOWED_KEYS 加 patient + PATIENT_ALLOWED_KEYS 嵌套白名单（name/sex/age/no/date，只留声明键+字符串值）；语义统一——patient 缺失/非字典/子键全被滤 → **整键不出现**（不伪造 None/空对象，与其他键「缺失即不出现」一致，LLM 提示词不吃噪音）。教训：首版「总是写 patient: None」被旧回归测试 test_clinic_payload_enters_case_and_reaches_llm_context 当场抓住（clinic_payload 回读 == 原 clinic 断言失败）——白名单过滤器的键存在性语义必须键间一致
- 砍掉（评估后）：AI 结构化字段回填病历单只读区——患者信息属医生录入权威数据，AI 回填有覆盖医生输入风险，收益低
- 验证：client pytest 17 + tests-server 32（+1：patient 嵌套白名单——合法五字段保留/偷渡 id_card 与非字符串 height 丢弃/非字典与空子键整键不出现/缺失不出现）+ vitest 412 + verify3d exit 0 ×3（clinicLayer 新增 patUi/patAutoDate/patAutoNo/patMasked/patSelRow 5 断言全过）；缓存 bump main.js?v=56
- 教训：verify_3d 偶发 exit 139（QtWebEngine 退出阶段段错误，ok:true 已输出）——单次 139 不当失败，重跑 3 次稳定 exit 0 才算数

## v57（2026-09-12）安装 BAAI/bge-m3 向量模型（多模型文件清单 + 下载器 certifi 修复）
- 用户指令：「安装BAAI/bge-m3模型」——此前 catalog 只有 bge-zh 系列三款（BERT 架构统一文件清单，权重名/前缀/分词器硬编码）
- 实现（server/rag_embed.py）：① MODEL_CATALOG 加 bge-m3（XLM-RoBERTa 架构，1024 维，多语言/8192 长文本，约 2.3GB）——per-model "files" 清单覆盖默认（sentencepiece.bpe.model 替代 vocab.txt，其余 9 文件同名；权重仍叫 pytorch_model.bin，sha256/大小记录复用）；② query_prefix 随模型配置——bge-zh v1.5 官方检索指令前缀保留，bge-m3 官方明确无需指令 → 空串，rank() 按 _loaded_short 取前缀；③ 下载器 _download_file 加 _ssl_context()：macOS 框架 Python 缺系统 CA（CERTIFICATE_VERIFY_FAILED），certifi 可用时用其证书束，否则回退默认——首次安装从「必然失败」变为可用，Linux/自装 CA 不受影响
- 仓库校验：安装前逐文件 HEAD 校验 ModelScope BAAI/bge-m3 全部 200；发现 modules.json 引用 2_Normalize 模块但仓库无其 config.json——Normalize.load 不读文件（HF 官方同样缺省），10 文件清单即为完整
- 依赖：sentence-transformers 6.0.1 + torch 2.14.0（client/.venv，服务端实际运行解释器）；教训——ps cmdline 显示框架 Python 路径（venv symlink 目标），实际 site-packages 在 client/.venv；装依赖前先确认目标解释器，别信 cmdline 表象
- 验证：tests-server 33（+1：bge-m3 清单——sentencepiece 落盘/vocab.txt 不落盘/query_prefix 空/安装即激活；catalog ≥3 与 zh 系列前缀不变断言保留）；真机——2.27GB 下载完成（约 3 分钟，进度经 /api/kb/embedding.installing 可见），ensure_loaded state=ready（首查 8.1s 含权重加载），hybrid 检索排序语义正确（「遗传过敏体质+干燥瘙痒」→特应性皮炎第一；「免疫介导鳞屑性」→银屑病在前）；client pytest 17 + vitest 412 + verify3d exit 0 无回归；web 未改缓存不 bump

## v58（2026-09-12）LLM 真实性防线 + 3D 控件重组 + 左栏导航 + 双状态布局
- 用户四项要求：①LLM 未连接绝不伪装 AI 医疗建议；②3D 控件合并到 3D 顶部浮层；③左栏固定四入口（新对话/历史病例/项目/最近）；④删流程步骤条改一行状态文字 + Codex 风格 3D↔对话双状态布局
- 防伪装链路（核心）：`_make_draft_server` 返回 `(draft|None, err)`——自定义病例 LLM 未配置/失败/超时/非法返回 → `workflow_state="llm_unavailable"` + draft=None，**绝不 build_draft 冒充**；demo 病例例外（明确标注的离线演示，llm.source="demo"）。快照带 llm 元信息 {ok, source(llm/demo/none), model, generated_by, error, at}；服务端新增 GET /api/llm/status（?check=1 实测网关可达性，Key 只回布尔）；WebBridge 新增 llmProbe/casesList/caseResume 三槽；MockAgentClient 统一返回 llm demo 元信息（前端守卫只认 source，不特判接入模式）
- 前端守卫（main.js）：sendChat 发送前 probeLlm → unavailable 阻断 + 三入口卡（检查连接/打开模型设置/切换离线演示）；appendConvResultCard 三态——source=llm 绿横幅「真实 AI 分析（LLM 请求成功并通过结构化校验）」+ 方向列表 + 医生审核按钮；source=demo 橙横幅「离线演示数据，不是真实 AI 分析，不可用于医疗判断」；source=none 红横幅「当前未连接 LLM」+ 失败原因，零医疗内容；结果卡显示模型/返回时间/失败原因
- 3D 控件（index.html #stageTools 浮层）：视角按钮下拉（前/后/左/右/重置）+ 人体模型下拉 + 缩放±/重置；删左侧视角按钮列与旧位置模型下拉；zoomCam 沿视线缩放（target 不变）；`.conv-primary #stageTools` 自动隐藏
- 左栏导航（#toolbar 改 rail）：新对话（清输入/选点/对话区/分析结果，CHAT_HISTORY 不删）/历史病例（bridge.casesList，空态不造假）/项目（项目信息+知识库+LLM 设置入口）/最近（localStorage derm_recent 20 条）；navPane 复用面板 + active 高亮
- 双状态布局：#stage-wrap 恒 absolute，length-percentage 混合值 CSS transition（0.45s）——3D 主区(0,0,100%,100%) ↔ 小窗(14px,12px,320×240 圆角阴影)；#convPane 淡入淡出；first send → setLayout('conversation_primary')；点小窗恢复主区；ResizeObserver 跟随 renderer.setSize，场景/相机/REGIONS/选点全程恒定不重建
- 状态行替代步骤条：#statusLine 一行文字（正在检查 LLM 连接/正在分析病例/等待医生输入/等待医生审核/当前未连接 LLM）；__pushFlow 空 action 复位到 flowStateLine
- 测试：tests-server 36（+3：llm_unavailable 无医疗稿/llm_status 端点 Key 不落日志/真实请求失败不伪造；3 个旧用例适配 demo="custom" 适配口——demo 键传非内置值走文本解析+失败回退明确标注的规则稿）；client pytest 17；vitest 412；verify3d exit0 ×3（探针重写：flowLayer 缩简 14 + 新 navLayer 17 + stageToolsLayer 14 + layoutLayer 17 + llmGuardLayer 16）；缓存 bump v58
- 教训：①同步 IIFE 探针断言 CSS transition 终态必须临时 `style.transition='none'` 读终态再恢复（0.45s 内 computed 是插值中值）；②探针层间污染——前面层推送结果卡会改状态行，依赖「初始态」的断言要显式复位 FLOW/statusLine；③旧断言跟随需求演进（modelInToolbar 从「左侧栏老位置」改「3D 顶部浮层」），断言文案同步改

## v58b（2026-09-12）真实 LLM 全链路打通（三处网关兼容修复）+ 真机绿横幅证据
- 真机冒烟发现防伪装防线真实触发但根因在网关兼容：①SSL CERTIFICATE_VERIFY_FAILED（macOS 框架 Python 缺系统 CA，v58 首版只给 /api/llm/status 加了 certifi，**_chat 的实际 urlopen 漏加**）→ 补 `context=_http_context()`；②修复后转 Cloudflare 403 error 1010（浏览器签名封禁，Python-urllib 默认 UA 被拦）→ _chat 请求头加浏览器式 User-Agent（探测确认：默认 UA 403 / 浏览器 UA 200 / curl 200；Key 只进 Authorization 绝不进 UA）；③**main.py `_push_assist_to_web` 只推 draft 不带 result["llm"]**——前端 appendConvResultCard 守卫只认 d.llm.source，缺失即红横幅「当前未连接 LLM」，把真实成功结果误标为未连接（宁可不标也不伪装的设计正确，但 review 推送载荷必须带真实性元信息）→ 签名加 llm 参数，review/report/closed 三处调用传 result.get("llm")（ServerAgentClient._render 所有 action 统一带 llm，来源契约一致）
- 新测试：tests-server test_chat_sends_browser_user_agent（UA 含 Mozilla/5.0、不含 Python-urllib、Key 不泄进 UA）；verify_3d assistLayer 新增 convGreenBanner（llm.source=llm → #convLog 出现 .conv-banner.llm-ok）+ convRedBannerWithoutLlm（载荷缺 llm → .llm-bad 红横幅）——补上横幅颜色契约缺口（旧模拟载荷不带 llm 键，全部走了红横幅分支而未断言）
- 真机证据闭环：红横幅路径（SSL 失败→llm_unavailable→零医疗内容）此前已验；本轮绿横幅路径全通——「身体区域：小腿…」完整病例 → 新对话 → 发送 → 状态行/横幅「LLM 请求成功并通过结构化校验」+ 结果卡元信息「模型：gpt-5.6-terra · 返回时间 · workflow_state=awaiting_review」+ 辅助面板诊断辅助「湿疹样皮炎可能，待排（辅助排序：中——非确诊概率）」带依据与引用 [CIT-AD-001-S1]；API 交叉验证 llm={ok:true,source:"llm",generated_by:"llm"}、2 个待排方向
- 冒烟操作学：宿主（NuwaClaw）会抢前台导致点击/粘贴落错窗口——每步先 osascript set frontmost；enter 与粘贴必须同一条命令（跨命令焦点丢失）；pyautogui 键盘事件对 QtWebEngine 不可靠（typewrite/press 全被吞），osascript tell <pid> keystroke return 最稳；sips cropOffset 换算屏幕点 = (offsetY + y×缩放)/2，漏加 offset 会偏 33pt 点到空白；发送按钮点击不生效时可改输入框回车
- 验证：tests-server 37（+1）+ client pytest 17 + vitest 412 + verify3d exit 0 ×3（含 convGreenBanner/convRedBannerWithoutLlm 新断言）

## v59（2026-09-12）Agent 对话化核心：自然语言任务路由 + 3D 降为可选工具 + 两态布局
- 服务端任务路由（app.py）：`TASK_PATTERNS` 规则分类 12 类（case_edit→report_export→history→lesion_desc→differential→medication_review→treatment_ref→patient_edu→referral→followup→summary→case_write，顺序敏感、默认 case_write，纯规则离线可用）；`POST /api/cases/{id}/chat` 延续上下文（任务指令进 messages + task_text；case_write/general 且无 draft 时 merge_reply 并入病例；clinic 白名单增量并入）；_screen 只做路由分发不再强制追问——缺失字段由草稿 unknown 如实列出，缺失不阻断发送
- doc 产物契约：{task_type, content(markdown), generated_by(llm/llm-edited/local-template/local-export), citations, model_version, disclaimer, at}，workflow_state="doc_ready"；MEDICAL_DOC_TASKS（鉴别/治疗/用药/教育）LLM 不可用→llm_unavailable 零内容；TEMPLATE_DOC_TASKS（现病史/皮损/摘要/转诊/随访）→_template_doc 本地模板整理（source="template"，只重组已提供字段、缺失一律「未提供」绝不写「无/否认」、零引用零医学知识）——普通文字整理在明确标注下可离线继续，医学判断必须真 LLM；case_edit 三分支（LLM 重写 llm-edited / AI 草稿+掉线→llm_unavailable 绝不本地篡改 / 无草稿→模板重排）；report_export 本地组装既有内容（无内容给提示）；_doc_prompt 硬约束（待排措辞/未提供/禁处方/引用白名单）+ 12 类 task_spec + 最近对话 6 条；_sanitize_doc_citations 白名单外引用就地剔除
- 客户端（main.py/agent_client.py）：发送分支 case_id 有则 chat（3D 选点并入「身体区域：…」文本，没有照常发送绝不阻断）无则 start；ServerAgentClient.chat + _render 透传 doc/task_type；_show_result 新 action=="doc" → _push_doc_to_web（llm 元信息随载荷）；MockAgentClient 离线任务回退同构（文书→模板 doc、医学判断→llm_unavailable、导出→本地组装）；WebBridge.onNewSession 重置 case_id/draft
- 前端两态布局（main.js/index.html）：setLayout('chat_primary'|'model_primary') 互斥显示绝不叠加（#stage-wrap z5 display:none ↔ #convPane z4 display:none），3D 场景不重建 ResizeObserver 跟随；「🧍 人体模型」在 composer 行、「✅ 完成并返回对话」在 stageTools；发送后自动回 chat_primary；左栏纯导航（新对话/历史病例/项目/最近）、无流程条、删 3D 小窗/miniTag；appendDocCard 四态横幅（llm 绿/template 蓝「本地模板整理（非 AI）」/demo 橙/其余红）+ task_type 标题 + citations；__pushDoc 桥
- 真机冒烟暴露并修复三个真 bug：①_chat 在 except 块内去参重试二次抛 HTTPError 未捕获→500 冒泡弹窗——重构为 _once()+外层退避重试两层（400/404/422 去参保留；所有失败归一 ConnectionError 落 llm_unavailable 防线，绝不 500）；网关瞬时抖动（间歇 unknown provider/503）退避 1.5s 重试一次自愈；②WebBridge.onNewSession 只清 win.case_id 不清 client.case_id——发送分支看 win、请求由 client 发，失步打出 /api/cases/None/chat 404→同步清；③**_ensure_client 每次发送无条件重建 client**（case_id 句柄清零而 win.case_id 残留）→改「类型或地址变化才重建」，会话句柄跨发送保留（测试锁定）；顺带修连接错误弹窗过时文案（v54 顶栏控件已入设置页）
- 真机证据：默认对话主区全宽+无小窗+无流程条；🧍 人体模型→3D 全屏（视角/分组/重置工具条）→点左前臂选点（已选 1+坐标）→完成并返回对话→chips 并入对话；案例 1 无 3D 直接文字→红横幅期间零医疗内容（网关 503/unknown provider 防线真机验证）→网关恢复后同会话 chat 200+绿横幅「真实 AI 分析（LLM 请求成功并通过结构化校验）」+辅助判断面板自动展开（API 交叉验证 llm.source=llm/awaiting_review）；LLM 不可用时 POST 200 走防线而非 500
- 测试：client pytest 19（+2：mock 任务路由/渲染透传 + ensure_client 句柄保留）；tests-server 49（+12：任务分类 15 用例/doc 管线/六案例 fake-LLM 全链路/_chat 重试回归×2）；vitest 412；verify3d exit0（layoutLayer 两态 15 断言重写+llmGuardLayer doc 卡 8 断言）；缓存 bump v59
- 教训：①网关故障会伪装成多种形态（SSL/403 UA/503/400 unknown provider），失败路径必须在 _chat 单点归一成 ConnectionError，调用方统一落防线；②except 块内发起的重试调用不再受同一 try 保护——重构测试必须数清每个场景的真实调用次数（400 去参=每 _once 2 次、503=1 次）；③同一会话句柄存两处（win/client）必然失步——单点持有或同步清空必须成对；④真机 GUI 自动化在用户活跃使用机器上会与用户互相抢焦点——核心证据（API 快照+服务端日志）优先，截屏见缝插针

## v60（2026-09-12）删除按键：历史病例硬删 + 历史对话/最近记录删除与清空
- 用户指令：「增加可以删除历史病例，删除历史对话，项目的删除按键」
- 覆盖口径：①「历史病例」=左栏 pane（服务端 sessions.json 持久化）→ 每行「🗑 删除」+ DELETE 端点（服务端硬删，列表/续办立即失效，audit case_delete 落盘；删当前打开病例时 bridge 同步失效 win.case_id/client.case_id/draft）；②「历史对话」=web「最近」pane（localStorage derm_recent）单条「✕」+「🗑 清空最近记录」（confirm，只清本机记录不动服务端）+ Qt「🕘 历史」抽屉「清空历史记录」按钮（列表+详情清空）；③「项目」pane 为静态项目信息卡无可删数据——用户口径的「项目」由「最近」🗂 病例类条目删除键覆盖（recordRecent("case") 条目），汇报时已说明取舍
- 实现：server DELETE /api/cases/{id}（404 幂等语义：不存在报 404，_lock 保护 pop→写回）；agent_client.delete_case（DELETE 归一 ConnectionError）；WebBridge.caseDelete Slot（server 模式校验/句柄同步失效/audit case_delete_bridge）；main.js showHistCases 行尾 🗑（confirm 文案「病例会话与待审草稿一并删除，不可恢复」→ bridge.caseDelete → 成功重渲染/失败 alert）+ showRecentPane persist(next)（✕ 按 idx 过滤 + 清空全部 confirm「服务端已保存的病例不受影响」）；Qt _clear_history（history_list 清空 + 详情「（已清空）」+ audit history_cleared）；CSS .nav-row-del 红 hover；缓存 bump v60
- 测试：tests-server 50（+1 test_delete_case_removes_from_list_and_resume：创建→列表含→DELETE 200→列表不含→GET 404→重复 DELETE 404→chat 404；适配 test_chat_drops_reasoning_effort_when_gateway_rejects——v59 外层退避使 401 也重试 1 次后报错，calls 1→2 更新断言与 docstring）；verify_3d 新增 delLayer 7 断言（探针 stub window.confirm 直通；预置 localStorage derm_recent 两条→最近 pane 2 行 2 ✕+清空按钮→点首条 ✕ 剩「测试对话B」且持久化→清空→空态+localStorage 空数组；历史病例无 bridge 空态不渲染删除键）——探针无 QWebChannel，历史病例删除交互真机覆盖；client pytest 19 + vitest 412 无回归
- 真机证据（CGEvent 全链路）：历史病例列表每行 🗑 渲染→点首行→confirm 文案正确→OK→列表自动刷新（该行消失）→API 交叉验证 GET 404 + audit.jsonl case_delete 落盘（13:31:40 case_id/label/workflow_state）；「最近」🗂 续办病例条目 + ✕ + 清空按钮渲染→✕ 单条删除回空态→再续办产生记录→「清空最近记录」→confirm（文案含「服务端已保存的病例不受影响」）→空态；Qt 历史抽屉 4 条记录→「清空历史记录」→列表清空 + 详情「（已清空）」；四件套 client pytest 19 / tests-server 50 / vitest 412 / verify3d exit 0
- 教训：①真机点删测试病例后别急着下结论「API 未删」——先核对列表行时间戳与 case_id 对应关系（挂住的 POST 会在 curl 被 kill 后由服务端完成落盘，列表顺序与直觉不符），本机 curl 的「重复 DELETE」也可能误删第二只测试病例；②window.confirm 在探针环境会永久阻塞——delLayer 须先 stub confirm 直通，真实浏览器语义由真机冒烟覆盖；③2000+ 病例列表全量渲染，删除键真机验证务必用自建 case_id 定位行，绝不点真数据

## v63（2026-09-13）侧栏 Codex 式对话历史流：时间分组常驻 + 面板退役 + 测试数据隔离防线
- 用户方向纠正：「项目是像codex一样的对话历史管理，不是功能合集」——v61 三分组 pane 迭代为统一历史流；经 ask-question 表单确认：①侧栏=Codex 式统一历史流（「新对话」+病例会话按时间分组常驻滚动，条目点击续办、行尾🗑）；②本机「最近」（localStorage）保留为独立分组；③「查看全部」浮层面板退役（侧栏时间流即全量列表）；「删除全部病例」移到历史流底部小字
- 实现（main.js/index.html）：timeGroupOf(iso)→今天/昨天/7天内/更早（本地午夜差，Math.round 消 DST 抖动）；renderRailHist 重写——list.slice(0,200) 分组渲染 .rail-time-head + .rail-entry（entry-time 小字：今天 HH:MM、更早 M/D），流底 .rail-del-all「🗑 删除全部病例」（confirm 含条数→bridge.caseDeleteAll，空列表不渲染）；showHistCases 整体退役 + navHist 绑定删除；CSS .rail-stream flex:1 滚动区；「项目」「⚙设置」收 #railBottom 图标行；缓存 bump v63
- setNavActive 修复：只遍历 .rail-item 使底部图标按钮 📁 点击后高亮不迁移——改遍历 #toolbar [data-nav] + CSS #railBottom #navProject.active
- verify_3d：navLayer 重写 21 键（orderOk 断言单组标题「最近」+navHist 不存在+railBottom 含📁⚙；projectActive 点击底部📁高亮迁移）；delLayer 历史段改 railHist（无 bridge 空态、无伪造删除键、空列表无「删除全部」）；python 清单同步
- ⚠️ 事故与修复（tests-server 数据隔离防线）：跑全量 pytest 时 test_delete_all_cases_clears_list 经 in-process TestClient 直写真实 server/data/sessions.json，把医生当晚 23:59 刚创建的空壳病例「自定义病例」（4cc150280d47，存在约 3 分钟、case_started 后无任何内容保存事件）连同 3 条注入验证数据一并清空（audit 留痕 case_delete_all count=4；sessions/audit/rag/server.log 全盘检索无内容副本，无法找回）。修复三件：①app.py DATA 支持环境变量 DERM_DATA_DIR 覆盖；②新增 tests-server/conftest.py——conftest 顶层先于测试模块导入 server.app 执行，DERM_DATA_DIR 指 tempfile 临时目录，sessionfinish 清理；③test_audit_trail 硬编码真实 audit.jsonl 路径改跟随 server.app.AUDIT_FILE。验证：pytest 前后真实数据文件 stat 指纹（size+mtime）不变 + 51 全绿
- CDP 真机验证方法论（替代 CGEvent）：QTWEBENGINE_REMOTE_DEBUGGING=9333 → /json/list 取 webSocketDebuggerUrl → websocket-client Runtime.evaluate（必须 awaitPromise:True+returnByValue:True，否则 async IIFE 返回 {}）派发 el.click() 合成点击——零鼠标事件、零焦点竞争，用户占用电脑时也可验证；CGEvent 教训（activate 进程名非应用名/宿主抢前台点击落错窗口）就此退役
- 真机证据（V63_CDP PASS）：heads==[今天,昨天,更早] 顺序正确；entries=3；navHist 退役；底部📁⚙就位；流底「🗑 删除全部病例」存在（只断言存在未点击，全删链路证据由 v62 CDP 承载）；entry-time×3+行尾🗑×3；点首条续办→localStorage derm_recent 首条 case_id=aaa111v63today+侧栏「最近」分组同步出现条目；单删「分组验证·8天前」（case_id 定位+confirm stub）→行消失、侧栏 3→2、API 交叉验证 3→2；四件套 client pytest 19 / tests-server 51（+隔离验证）/ vitest 412 / verify3d exit 0
- 教训：①in-process TestClient 测试与真实服务共用模块级常量时，数据目录必须在「conftest 顶层」就隔离——等 fixture 挂上已经晚了（测试模块顶层 import server.app 时常量已固化）；②任何「全删/清空」类测试在数据隔离缺位时就是真实数据定时炸弹，先修隔离再加测试；③验证数据注入要按本地时区校准分组归属（午夜跨越时 2h 前已是「昨天」）
