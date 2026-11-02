# 三层记忆系统（Redis / MySQL / Milvus）

> 面向部署、运维与隐私审查。设计细节见实施计划；本文只写「怎么起、怎么查、怎么删、什么不能外发」。

## 1. 构成与权威边界

| 层 | 存储 | 内容 | 丢失后果 |
|---|---|---|---|
| 短期 | Redis | 最近 20 轮对话窗口 + 规则摘要副本，TTL 7 天 | 回退 `sessions.json`，仅热窗口变短 |
| 画像 | MySQL | 患者实体 + 版本化事实（过敏/既往史/用药/家族史/诊断史/检验） | 不注入患者档案；病例与文书不受影响 |
| 情景 | Milvus | 每轮问答切片（病例级）+ 去标识话题（医生级） | 不做情景召回；病例与文书不受影响 |

`sessions.json` 仍是病例、文书、审核状态的唯一权威。记忆层只做提示词增强，任何一层失败都不得改变 `phase / workflow_state / draft / review`，也不得让 chat 返回 5xx。

规格单一真源：`web/backend/shared/memory_spec.json`（类别白名单、阈值、预算、TTL）。TS 端副本 `src/app/memory/spec.json` 由测试断言哈希一致。

## 2. 本地部署

```sh
# 1) 依赖（缺哪个就降级哪层，不影响启动）
pip install -r web/backend/requirements.txt     # redis / pymysql / pymilvus 为可选

# 2) Redis（Homebrew 示例）
brew services start redis

# 3) MySQL：库表由 memory.profile 幂等创建，凭据读 ~/.my.cnf
#    也可用环境变量覆盖，见 §3
scripts/memory-dev-up.sh

# 4) Milvus Standalone（需 Docker 守护进程）
cd deploy/milvus && docker compose up -d
docker compose ps                               # 等待 milvus 健康
```

`scripts/memory-dev-up.sh` 依次检查 Redis → 启动/检查 MySQL 并建库建表 → `docker compose up -d` Milvus。任一项未就绪只打印 warn，不视为失败。

服务启动后核对：

```sh
curl -s http://127.0.0.1:8765/api/memory/health          # 三层 + worker 状态
python web/backend/tools/memory_check.py                 # 同上的 CLI 版本
python web/backend/tools/memory_check.py --case <case_id>
```

健康接口只返回 `{ok, latency_ms, detail}`，不含凭据、表内容或对话正文。

## 3. 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `DERM_MEMORY_ENABLED` | `1` | `0` 关闭整层（等价于旧行为） |
| `DERM_MEMORY_DISABLED` | 空 | 测试用，`1` 时不连任何外部服务 |
| `DERM_REDIS_URL` | `redis://127.0.0.1:6379/0` | 短期窗口 |
| `DERM_MYSQL_HOST/PORT/USER/PASSWORD/DB` | 空 / `3306` / 空 / 空 / `dermagent` | 全空时读 `~/.my.cnf` |
| `DERM_MILVUS_URI` | `http://127.0.0.1:19530` | Milvus SDK 地址 |
| `DERM_MILVUS_TOKEN` | 空 | 启用鉴权时填写 |
| `DERM_MEMORY_EMBED_MODEL` | `bge-base-zh-v1.5` | 固定 768 维，与知识库检索模型解耦 |
| `DERM_MEMORY_HTTP` | 空 | TS 端走服务端端点时填写 |

Redis key 前缀 `derm:mem:v1:`，`case:{case_id}:msgs|summary|seq`；写入即 `LTRIM` 到 40 条并刷新 TTL 604800 秒（三个 key 都会续期）。

新病例 key 尚未建立、或 key 自然过期时，窗口回退 `sessions.json` 属正常读路径，**不**标 `degraded.short_term`；只有 Redis 连接层不可用才置 true。

## 4. 工具

```sh
# 情景补建：BGE/Milvus 装好后，把 sessions.json 的已关联病例补成切片（幂等）
python web/backend/tools/memory_backfill.py --dry-run
python web/backend/tools/memory_backfill.py
python web/backend/tools/memory_backfill.py --case <case_id>

# 自检与清理（只输出计数/ID/状态）
python web/backend/tools/memory_check.py --json
python web/backend/tools/memory_check.py --purge-case <case_id> --yes
python web/backend/tools/memory_check.py --purge-patient <patient_id> --yes
```

补建只处理**已关联患者**的病例：没有患者档案就拿不到姓名清单，无法保证去标识，工具会如实跳过而不是写入可疑切片。

## 5. 隐私边界

- 姓名/电话/身份证/门诊号只允许存在于 MySQL `patients` 与病例窗口展示。
- 进入提示词、Milvus `text/title`、审计、错误日志、外部 LLM 的文本一律先过 `memory/redact.py`（TS 端为 `memory/redact.ts`）。
- 患者档案注入只含临床事实（类别·字段·值·来源），不含身份字段。
- 抽取 LLM 的输入先替换已知姓名与证件/电话/门诊号；返回事实再校验一次，`identity` 类目不存在于白名单。
- 已知限制：未关联患者或患者档案不可用时，工具拒绝写情景切片——自由文本里的姓名无法在没有姓名清单时可靠识别。

## 6. 删除矩阵

| 操作 | Redis | MySQL | Milvus |
|---|---|---|---|
| 删除病例 | 删 `case:{id}:*` | 删 `case_patients`、`case_memory_cursor`、未入队任务 | 删 `case_id == id` 的病例级切片 |
| 删除患者 | 删其全部病例窗口 | 事实置 `deleted`、解除关联、患者置 `deleted` | 删该 `patient_id` 的病例级与医生级切片 |
| 合并患者 | 保留源病例窗口 | active 事实与关联迁到目标，源患者置 `merged` | 按目标 `patient_id` 重建切片 |

删除为软删除 + Milvus 物理删除；审计只记 ID 与计数，不记内容。

## 7. 降级矩阵

| Redis | MySQL | Milvus/BGE | 行为 |
|---|---|---|---|
| 正常 | 正常 | 正常 | 三层全开 |
| 挂 | 正常 | 正常 | 窗口回退 `sessions.json`，`degraded.short_term=true` |
| 正常 | 挂 | 正常 | 不读/不写患者档案，`degraded.profile=true`，任务留队 |
| 正常 | 正常 | 挂 | 跳过情景写入与召回，`degraded.episodic=true` |
| 挂 | 挂 | 挂 | 等价旧行为：窗口来自 JSON，无档案/情景 |
| LLM 不可用 | — | — | 抽取任务保持 queued，回复路径照旧 |

所有降级写入快照 `memory.degraded` 与健康接口；不伪造记忆内容。

## 8. 审计事件

每条只含 ID、类别、计数、耗时与错误摘要：

| 事件 | 触发 |
|---|---|
| `memory_recall` | 每轮装配（窗口条数/档案条数/召回条数/耗时/降级） |
| `memory_profile_write` | 抽取或医生手改写入事实 |
| `memory_episodic_write` | 情景切片写入 |
| `memory_episodic_skipped` | Milvus/BGE 不可用或切片构造失败 |
| `memory_extract_failed` | 抽取失败（含 LLM/校验/写库） |
| `patient_delete` / `patient_merge` | 患者删除/合并 |
| `case_memory_purge` | 病例记忆清理（三处计数） |

辅助事件：`memory_enqueued`、`memory_extract_done`、`memory_extract_skipped`、`memory_queue_full`、`memory_worker_error`、`memory_profile_unavailable`、`memory_episodic_unavailable`、`memory_enqueue_failed`、`memory_after_turn_failed`、`memory_profile_read_failed`、`memory_profile_redacted`、`memory_episodic_recall_failed`、`memory_purge_failed`、`memory_episodic_delete_failed`、`patient_create/update/link/unlink/fact_delete`。

## 9. 备份与迁移

- `~/mysql-data`、`deploy/milvus/volumes/` 属**敏感数据**：含患者身份与对话切片，不入仓库、不随知识库备份（`kb-backup`）导出。
- 知识库备份/恢复不包含记忆层；换机时若要带走记忆，需单独加密搬运上述两个目录，并在目标机重新核对 `memory_spec.json` 版本与向量维度。
- Milvus collection 带 `_v1` 版本；换嵌入模型时必须新建 collection 并用 `memory_backfill.py` 重建，不得混写。

## 10. 当前状态

服务端、桌面 mock、TS 适配器与假实现回归已完成。真实 MySQL/Milvus 端到端验收（安装后执行的 8 步）尚未进行；`GET /api/memory/health` 是唯一可信的「是否真的可用」来源。
