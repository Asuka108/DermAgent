# docs 索引

本目录是 **本项目规格书**：能力面、节点选型、factory API、图规则、编排范式与排错。  
读本目录即可理解「框架有什么、怎么拼图」；实现细节以源码为准。

## 怎么读

| 目的 | 从哪开始 |
|------|----------|
| 术语 | [glossary.md](glossary.md) |
| 节点选型 | [node-catalog.md](node-catalog.md) → [node-kit.md](node-kit.md) |
| 写 / 改图 | [flow-graph-rules.md](flow-graph-rules.md) + [flow-orchestration.md](flow-orchestration.md) |
| 扩展范式（RAG / HITL 等骨架） | [examples.md](examples.md) · [flow-patterns.md](flow-patterns.md) |
| 能力从哪来 | [capabilities.md](capabilities.md) |
| 排错 | [troubleshooting.md](troubleshooting.md) |

## 文档地图

| 文件 | 用途 |
|------|------|
| [glossary.md](glossary.md) | 术语权威 |
| [flow-graph-rules.md](flow-graph-rules.md) | 图编排硬规则 R-G001+ |
| [node-catalog.md](node-catalog.md) | 节点选型（先看这个） |
| [node-kit.md](node-kit.md) | factory API 详表 |
| [flow-orchestration.md](flow-orchestration.md) | 编排速查（默认 ReAct / HITL / 坑） |
| [flow-patterns.md](flow-patterns.md) | Send / interrupt / subgraph 等进阶模式 |
| [examples.md](examples.md) | 扩展范式 + 可复制骨架；**交付底座**（多轮会话 / 压缩 / checkpoint）优先于编排口味 |
| [capabilities.md](capabilities.md) | 运行时能力面（含 `platformToolRefs`） |
| [troubleshooting.md](troubleshooting.md) | 排错 |
