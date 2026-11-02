# 本地书籍知识库（KB）单管线

> 状态（2026-10-30）：代码与离线 fake 回归已完成；MySQL / Redis / Milvus 已联调，
> MinerU 支持云端 Precision API（token 待配置）与本地 SDK 两种模式；embedding/rerank 模型
> 未安装前按表降级并如实标注，绝不伪造解析或检索结果。

## 管线

```
上传原件 → MinerU 解析（云端 Precision API / 本地 SDK）→ 标准清洗
→ 切分（按章节 / 段落 / 句号（。）/ 按书；overlap 0–300 可配，默认 60）
→ bge-base-zh-v1.5（768 维）→ Milvus derm_kb_chunk_v1
查询 → bge 查询向量（带检索前缀）→ Milvus top-k → bge-reranker-base 重排
→ 分数断崖取 top-n → 上下文注入（每条 ≤350 字 / 总计 ≤2600 字）→ LLM
```

- **上传与索引分离**：`POST /api/kb/books` 只落盘登记（`uploaded`），医生再点「开始索引」
  （`POST /api/kb/books/{id}/index`）。解析/切分/向量化由服务端 worker 单并发推进。
- **切分模式**（`/api/rag/prefs` 的 `chunk_mode`，任务执行时读取）：
  - `section`（默认）：空行段落打包，超长段落再按句末标点切；
  - `paragraph`：每个空行段落一块，不跨段合并；
  - `sentence`：按 `。！？；!?;` 切句后打包，段落间保留换行，超长句硬切；
  - `book`：忽略章节边界连续打包，目标长度取硬上限 600。
- **切分参数**：目标 450 字（book 模式 600）、硬上限 600；overlap 默认 60，
  可配 0–300 且须小于目标块长。overlap 为同章节内复制上一块尾部，
  叠加后按剩余空间缩短以保证不超过硬上限；`book` 模式允许跨章节。
  表格在所有模式下原子保留（超长按行拆并重复表头）。
  `chunk_id = {book_id}-g{gen}-{index:05d}-{hash16}`；引用 ID 用
  `RB-{book_id}-{anchor_hash8}`，正文不变时重切保持稳定。
- **参数生效**：`DERM_KB_CHUNK_*` 只是默认值；医生保存的 `chunk_mode` / `chunk_overlap`
  写入 `server/data/rag/prefs.json`，对**之后的导入与重建**生效。生效值
  （`chunk_mode`、`chunk_target`、`chunk_overlap`、`chunk_max`）写入 book meta 便于审计。
  重叠文字会进入 `content`，因此参与向量化、BM25、token 统计与引用摘录。
- **generation**：重建索引期间旧 generation 继续可检索；新 generation 计数校验后原子切换，
  再删旧 generation。
- **k**：MySQL active artifact → Redis 缓存 → SQLite 镜像 → 默认 20；查询向量与簇质心
  cosine ≥0.65 且样本 ≥3 用簇 k，否则全局 k（`k_source=cluster|global|default`）。
- **n**：重排分降序后找最大相邻落差（相对 ≥30% 或绝对 ≥0.15），截断处为 n，限制 3≤n≤8。

## 存储

| 组件 | 内容 |
|---|---|
| MySQL `dermagent` | `kb_books`（书目/状态/路径）、`kb_k_spec`、`kb_query_clusters` |
| Redis `derm:kb:v1:*` | 队列、job hash、进度、租约、延迟重试、k-spec 缓存 |
| Milvus `derm_kb_chunk_v1` | chunk 元数据 + 768 维向量（COSINE + HNSW） |
| SQLite `data/kb/mirror.sqlite3` | 只读镜像：书目 + 当前 generation chunk，供 BM25 降级 |
| 磁盘 `data/kb/` | `uploads/{book_id}/original.*`、`mineru/{book_id}/g{n}/`、`cleaned/{book_id}/g{n}.md`、`exports/` |

旧 `data/rag/books.json` 只读归档，不再写入；`kb-backup-*.json` 为含第三方文献摘录的私有备份，不随仓库分发。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/kb/books` | 书目 + `embedding_state/rerank_state/mineru_state/k_spec_version` |
| POST | `/api/kb/books` | base64 JSON 上传；`/upload` 为 multipart（需 python-multipart） |
| POST | `/api/kb/books/{id}/index`、`/reindex` | 入队；重复入队幂等 |
| GET | `/api/kb/books/{id}/status`、`/api/kb/jobs/{job_id}` | 轮询阶段/进度 |
| DELETE | `/api/kb/books/{id}`、`/hard` | 可恢复暂停（quarantined）/ 物理删除 |
| POST | `/api/kb/books/{id}/restore` | 复用清洗文本重嵌入（异步） |
| GET | `/api/kb/search` | `retrieval_mode/k_used/n_used/k_source/rerank_state` + 引用 |
| GET | `/api/kb/health` | MySQL/Redis/Milvus/embedder/reranker/MinerU 逐项状态 |
| GET/POST | `/api/kb/backup`、`/restore` | v2：书目 + 清洗文本，不含向量；恢复后自动重建 |
| POST | `/api/kb/rebuild`、`/import-folder` | 按当前参数重切 / 扫描 `book/` 目录（兼容入口） |

引用字段：`citation_id`（`RB-*`）、`book_id`、`title`、`section_path`、`page/page_hint`、
`quoted_text`、`vector_score`、`rerank_score`、`chunk_hash`。书籍引用与合成引用
（`CIT/DOC/KB`）使用独立白名单，`draft.book_citations` / `doc.book_citations` 分开返回。

## 生命周期与降级

```
uploaded → queued → parsing → cleaning → chunking → embedding → indexing → ready
异常 → failed(error_code)；ready/failed → quarantined；restore → queued；* → deleting
```

| 缺失项 | 行为 |
|---|---|
| Milvus / embedder | `retrieval_mode=bm25`，读 SQLite 镜像；`rerank_state=skipped` |
| reranker | `retrieval_mode=milvus`，按向量分排序 + 同一 n 规则 |
| MinerU | 云端缺 token → `mineru_unavailable(token_missing)`；本地缺包 → `mineru_unavailable(not_installed)`；均不使用 pypdf 伪装 |
| MinerU 云端网络/超时 | `mineru_service_unavailable` / `mineru_timeout`，可重试 3 次 |
| MySQL | 目录读镜像；上传/入队等写操作 503 |
| Redis | 上传可落 MySQL；入队 503 `queue_unavailable` |
| 镜像也不可用 | 空引用 + `degraded_reason`，不伪造 |

## k 离线评估

```bash
# 1) 30 条查询（默认调用 server/data/llm_config.json 的 LLM；也可 --from-file 导入）
python tools/kb_eval/gen_queries.py --count 30

# 2) 候选池（向量 top-50 + BM25 top-50；需 Milvus + 模型）
python tools/kb_eval/pool_candidates.py

# 3) 医生标注：复制 labels.template.csv，grade 0/1/2（按 candidates 的 chunk_anchor 对齐）
cp tools/kb_eval/labels.template.csv web/backend/server/data/kb/eval/labels.csv

# 4) 评估：NDCG@k 饱和（相对 NDCG@50 的 97%，边际增益 <0.005）+ 4 簇查询聚类
python tools/kb_eval/run_eval.py

# 5) 应用（写 MySQL active=1 + SQLite 镜像 + Redis 缓存）；--dry-run 只校验
python tools/kb_eval/apply_spec.py web/backend/server/data/kb/exports/kspec-*.json
```

无标注时 `run_eval.py --dry-run` 输出 `labels_present=false / global_k=20`，运行时
`k_source=default`，不把默认值伪装成评估结果。NDCG 的 IDCG 固定为该 query 全部相关标注的
理想排序（避免 top-1 高相关导致 NDCG@1=1、k 饱和点永远偏小）。

## 环境变量（`DERM_KB_*`）

`DERM_KB_ENABLED`、`DERM_KB_DISABLED`、`DERM_KB_BACKEND`（auto/fake/sqlite）、
`DERM_KB_AUTOSTART_WORKER`、`DERM_KB_COLLECTION`、`DERM_KB_MILVUS_ALIAS`、
`DERM_KB_EMBED_MODEL`、`DERM_KB_QUERY_PREFIX`、`DERM_KB_RERANK_MODEL`、
`DERM_KB_CHUNK_TARGET/CHUNK_OVERLAP/CHUNK_MAX`（仅为默认值，界面保存的偏好优先）、`DERM_KB_K_DEFAULT/K_MAX`、
`DERM_KB_N_MIN/N_MAX`、`DERM_KB_GAP_REL/GAP_ABS`、`DERM_KB_CONTEXT_CHARS`、
`DERM_KB_CITATION_CHARS`、`DERM_KB_TIMEOUT`。连接凭据复用
`DERM_MYSQL_*` / `DERM_REDIS_URL` / `DERM_MILVUS_*`（Milvus 开启鉴权时另设
`DERM_MILVUS_TOKEN`，例如 `root:Milvus`），不落日志。

MinerU 解析：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DERM_KB_MINERU_MODE` | `auto` | `auto`（有 token 走云端，否则本地 SDK）/ `cloud` / `local` |
| `DERM_MINERU_TOKEN`（或 `MINERU_TOKEN`） | 空 | 云端 Precision API token；缺失时 `cloud` 模式直接失败，不静默转本地 |
| `DERM_MINERU_BASE_URL` | `https://mineru.net/api/v4` | 云端 API 前缀 |
| `DERM_MINERU_MODEL_VERSION` | `vlm` | `pipeline` / `vlm` / `MinerU-HTML` |
| `DERM_MINERU_LANGUAGE` | `ch` | OCR 语言 |
| `DERM_MINERU_POLL` / `DERM_MINERU_TIMEOUT` | `5` / `1800` | 轮询间隔与总超时（秒） |

云端流程：`POST /file-urls/batch` 申请签名上传链接 → `PUT` 原件 → 系统自动建任务 →
`GET /extract-results/batch/{batch_id}` 轮询到 `state=done` → 下载 `full_zip_url`，
解出 `full.md`、`content_list.json` 与 `images/`（落 `data/kb/mineru/{book_id}/g{gen}/`）。
单文件上限 200MB / 200 页；书页会上传到 mineru.net，需确认版权与隐私边界。

## 三端

三端共用同一套本机服务端接口；设置页的「检索测试」只是 `GET /api/kb/search` 的调用方，
不另建检索、重排或截断逻辑。

- **Web 与桌面内嵌 WebView（同一页面）**：设置 → 医学书籍知识库为仪表盘式布局。
  顶部组件健康显示八个 chip（MySQL / 镜像 / Redis / Milvus / 向量模型 / 重排 / MinerU /
  Worker），摘要如实区分「向量+重排 / 向量 / 仅 BM25 / 不可用」与文档解析能力；中部书籍表
  展示状态、片段数、阶段进度与更新时间，导入后自动排队索引，可单本开始/重建/暂停/恢复/
  彻底删除；底部检索测试直接调用 `GET /api/kb/search`，原样展示 `retrieval_mode`、
  `k_used`/`n_used`、`k_source`、`latency_ms`、`kb_snapshot` 与 `RB-*` 引用，并可复制引用
  列表。备份恢复、RAG 参数、全库重建与 `book/` 文件夹导入收在「高级」折叠区。
- **桌面内嵌 WebView**：页面与浏览器一致；上传/删除走 HTTP（QWebChannel 单消息 >1MB 会
  静默丢数据），桥只做小数据降级。桌面自动拉起服务端时按 `DERM_SERVER_PYTHON` →
  `sys.executable` → 仓库 `.venv-tmp` / `.venv` / `web/backend/.venv` 顺序挑选装齐
  fastapi/pymysql/redis 的解释器；缺依赖时在状态栏明示，不静默降级。
- **原生 PySide6 设置对话框**：四个页签（知识库 / 组件与模型 / LLM 与语言 / 高级），所有
  KB 调用进 `QThreadPool`，避免上传与索引轮询冻结界面；单一 `QTimer` 轮询（活跃 2s、
  稳定 10s、错误退避 5/10/20/30s），关闭对话框即停止；检索结果与服务端同序，复制经
  `QApplication.clipboard()`；备份写出真实文件路径并可打开所在目录；服务端不可达时进入
  只读镜像模式，上传/索引/检索/安装均禁用并显示横幅。
- 桌面 `agent_client._render` 透传 `book_citations`，Web/桌面分别渲染书籍引用。

### 设置页验收要点

1. 健康栏：MySQL / 镜像 / Redis / Milvus / Worker 为绿，向量与重排未加载时显示 warn，
   MinerU 云端为 ready；摘要不得在书目接口成功时谎称「知识库就绪」。
2. 导入 1–2 本 `.txt/.md` 后自动排队，进度推进至 `ready` 且 `chunks > 0`。
3. 检索测试输入「湿疹 瘙痒」，Top 5 返回 `RB-*` 引用，模式为 `milvus_rerank` 或
   `milvus`，并显示 n/k、耗时与快照；引用按服务端顺序原样展示。
4. 暂停后检索不再命中，恢复后重新 ready，彻底删除后列表消失；行内操作立即触发刷新。
5. 关闭设置、切换页签不残留定时器；服务端停止后进入只读状态，不显示假数据。

## 依赖与许可证

- 新增可选依赖见 `web/backend/requirements.txt` 末尾注释：`python-multipart`、`mineru[core]`。
  云端模式不需要安装 `mineru`；本地模式才需要。
- MinerU 上游 **AGPL-3.0**，本地分发前需法务确认；云端模式受 mineru.net 服务条款约束，
  且会把书页上传到第三方。任一路径不可用时仅索引失败，不影响其他功能。
- 模型权重（bge-base-zh-v1.5 / bge-reranker-base）不随仓库分发，安装到本机模型目录；
  许可证登记见 `docs/data-license-register.md`。
