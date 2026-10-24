# 肤智（DermAgent）比赛发布与离线演示资产清单

只使用授权、脱敏或合成数据；不存在的资产明确标记 **TODO**，不用占位链接冒充已发布内容（对齐实施计划 §7.4 / §8.4 / 阶段 E）。

## 必备资产

- [x] Web/PWA 主评审入口：本地验证 `http://127.0.0.1:8765/`（`server/app.py` 静态托管 `webui/`）；正式女娲作品链接：**TODO（冻结后填写）**
- [x] Windows 客户端源码与运行说明：`client/README.md`；Windows PyInstaller 安装包：**TODO（必须在 Windows 机打包，不可交叉打包）**
- [x] Android 端：可安装 PWA（manifest + SW）；原生 APK / 应用商店：**TODO（P1 可选，非评审阻塞项）**
- [x] 四类固定案例：`shared/derm_core.py::DEMO_CASES`（案例1 正常 / 案例2 红旗 / 案例3 追问 / 案例4 拒答，全部合成数据）
- [x] 跨端验收卡：`docs/cross-platform-acceptance.md`（人工回归卡 + 自动化断言映射）
- [x] 数据授权台账：`docs/data-license-register.md`（3D 模型 Xbot/Michelle/CesiumMan 已登记）
- [x] 3D 真机验证资产：`client/tools/verify_3d.py` + `client/data/verify3d_front.png`、`verify3d_back.png`、`verify3d_report.json`
- [ ] 正式提交视频（4:30–5:30，按下方分镜）：**TODO**
- [ ] 无网络旁白录屏备份（决赛现场用）：**TODO**
- [ ] 现场演示硬件与投屏检查表：**TODO**

## 视频分镜（目标 4:30–5:30）

1. `0:00–0:30` 现实痛点、肤智（DermAgent）定位、女娲入口与免责声明
2. `0:30–1:10` Web/Android/Windows 打开同一案例：同一 `case_id`、同一状态
3. `1:10–1:50` 九要素采集、智能体追问、文字/分区列表/3D 身体区域输入
4. `1:50–2:40` 检索→安全→分析链路；展示授权证据片段与引用快照
5. `2:40–3:30` 结构化结果、用药核对、不确定性、模型/规则/知识版本
6. `3:30–4:15` 红旗升级与无答案拒答——状态不可绕过
7. `4:15–5:00` 医生确认、审计、辅助报告导出、跨端续办；片尾远期路线

视频统一口径：不是自动诊断、不自动开处方；每条结论受引用与结构化规则约束；医生始终在环；仅授权/脱敏/合成数据。

## 发布前回归命令

```bash
cd /Users/a1234/Desktop/aiyl/computer-project-workspace/1788336671/1687701
pytest -q tests-server
pnpm typecheck && pnpm test -- --run
python client/tools/verify_3d.py
```

## 断网演示韧性

- 停网或未配置 LLM 环境变量时，服务端自动走规则降级稿（`generated_by=rule-fallback`），界面明确提示，**不伪造模型建议**。
- 恢复后以同一 `case_id` 重试；结果以服务端 `result_version` 递增为准，防重复提交。
- 兜底证据：`tests-server/test_api.py` 全程以规则管线跑通四案例。

## 冻结检查（阶段 E，提交前完成）

- [ ] 女娲入口名称统一为「肤智（DermAgent）」，记录作品链接、访问权限与有效期：**TODO**
- [ ] 锁定 Prompt、Schema（1.1）、模型、规则、知识快照（`synthetic-kb@2026-09-04`）与案例包版本：**TODO**
- [ ] 核对 API 配额、素材许可证、视频隐私检查（无真实患者信息、无密钥画面）：**TODO**
