# 肤智（DermAgent）四案例三端验收卡

用途：比赛评审前的人工回归卡（对齐实施计划 §8.1）。三端指 **Web/PWA、Android（PWA 安装形态）、Windows 客户端**；三端必须使用同一个 `case_id`，只允许渲染服务端返回的权威状态、结果版本、知识快照和引用。

对应自动化回归：`tests-server/test_api.py`（状态机/引用白名单/跨端快照/结果版本）+ `tests/flow.test.ts`（四案例 interrupt/resume）。人工卡验证的是三端 UI 行为，两者互补、不可互相替代。

## 通用断言（每张卡都执行）

- [ ] 三端打开同一 `case_id` 后，`workflow_state`、`review_status`、`result_version`、`knowledge_snapshot`、`safety_policy_version` 与 `draft` 完全一致（自动化断言：`test_three_terminals_same_snapshot`）。
- [ ] 客户端只渲染服务端已校验的 `draft`；不自行补全 `citation_id`，不直连模型供应商。
- [ ] 界面显示免责声明、模型版本、知识快照、规则版本、生成时间、审核状态和阻断原因。
- [ ] `unknown`/`not_asked` 显示为「未知/未询问」，不得显示为「无」；用药区只显示核对项，不出现自动处方、剂量或疗程。
- [ ] 3D 不可用时，可用文字搜索或分区列表完成同一 `body_region` 字段并继续主流程（3D 失效不阻断）。
- [ ] 任一端不暴露 API Key、后台地址或绕过服务端安全校验。

## 案例 1：资料充分的常见病例

**初始输入**：任一端选择「案例1」，或提交 `demo=1`（合成双手/肘窝红斑瘙痒资料）。

**预期状态迁移**：`screening → awaiting_review → approved`。

**证据与安全断言**：
- 草稿 `data_completeness=complete`；`severity.level=routine`；`red_flag_escalation.is_blocking=false`。
- 至少一条当前知识快照引用；每条鉴别依据的 `citation_ids` 均在 `draft.citations` 白名单内。

**医生动作**：先「确认」草稿，再导出辅助报告；**确认前不得出现任何报告**（自动化断言：确认前 `report` 为空）。

**三端断言**：Web 创建 → Android 续办 → Windows 再次打开；三端 `result_version`、引用集合与报告内容一致。

## 案例 2：红旗病例

**初始输入**：任一端选择「案例2」，或提交 `demo=2`（单侧簇集水疱伴神经痛）。

**预期状态迁移**：`screening → escalated`，普通分析不可绕过；医生修改后仍保持红旗信息，确认/拒绝继续闭环。

**证据与安全断言**：
- `red_flag_escalation.triggered=true`、`is_blocking=true`；`blocking_reason` 含 `RF-HZ-001`。
- 显示升级动作（如眼科会诊/面诊建议），不输出自助诊疗结论。

**医生动作**：可「修改：…」后再审，或「拒绝：…」并确认不生成报告。

**三端断言**：任一端都不能把 `escalated` 改成 `awaiting_review`（红旗由服务端规则派生，客户端无覆写入口）；三端显示同一阻断原因与升级动作。

## 案例 3：信息不足病例

**初始输入**：任一端选择「案例3」，或提交 `demo=3`（缺失病程、诱因、病史、过敏史、用药等）。

**预期状态迁移**：`screening → awaiting_follow_up →（补充后）awaiting_review`。

**证据与安全断言**：
- 首轮返回 `missing` 列表与阻断级 `follow_up_questions`；未知字段不被推断为阴性。
- 补齐前不检索、不生成草稿；补齐后才进入 `awaiting_review`。

**医生动作**：按「字段：值」补充资料（如「病程：5天；过敏史：否认」），再审核草稿；缺失未补齐时不得生成报告。

**三端断言**：可在 A 端创建、B 端补充同一 `case_id`；三端看到相同缺失字段、问题列表与后续 `result_version`。

## 案例 4：无可靠证据病例

**初始输入**：任一端选择「案例4」，或提交 `demo=4`（甲床纵行黑线，知识库未覆盖）。

**预期状态迁移**：`screening → awaiting_review`（安全降级形态，非正常分析）。

**证据与安全断言**：
- `citations=[]`、`suspected_conditions=[]`；明确「知识库未覆盖/不足以判断」。
- 不编造引用或鉴别诊断；建议面诊/医生进一步评估（自动化断言：`test_case4_no_evidence_refusal`）。

**医生动作**：可拒绝草稿或补充资料；无可靠证据时不生成带诊疗结论的报告。

**三端断言**：三端一致显示空引用集合与不确定性说明及同一 `knowledge_snapshot`；客户端不能添加引用。

## 回归记录

| 日期 | 版本/提交 | Web | Android | Windows | 备注 |
|---|---|---|---|---|---|
| TODO（评审前填写） | TODO | TODO | TODO | TODO | 每案例打勾并记录截图编号 |
