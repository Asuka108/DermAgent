/* 肤智（DermAgent） Web/PWA —— 左：历史对话（提交后产生）/ 中：3D+输入框 / 右：症状显示区域（提交后运行出来）。
 * 与 Windows/Android 同一 case_id 续办、同一 §6.3 v1.1 契约渲染。 */
/* global fetch */

const REGIONS = ["头面部", "颈部", "躯干（胸腹）", "背部", "生殖/隐私部位", "上肢（左）", "上肢（右）",
  "手部（左）", "手部（右）", "下肢（左）", "下肢（右）", "足部（左）", "足部（右）"];
const $ = (id) => document.getElementById(id);
let caseId = null;
let lastSnapshot = null;
let hasHistory = false;

function esc(s) { return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;"); }

async function api(path, body) {
  const res = await fetch(path, body ? {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  } : undefined);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

function setStatus(text) { $("status").textContent = text; }

// —— 左：历史对话（提交后产生；空态提示随首条历史消失） ——
function pushTimeline(who, title, detailHtml = "") {
  const box = $("timeline");
  const empty = box.querySelector(".empty");
  if (empty) empty.remove();
  hasHistory = true;
  const ts = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  const el = document.createElement("div");
  el.className = `msg ${who}`;
  el.innerHTML = `
    <span class="who">${({ user: "🧑‍⚕️ 医生", agent: "🤖 肤智", system: "⚙️ 系统" })[who] || who}</span>
    <span class="ts">${ts}</span>
    <div><b>${esc(title)}</b></div>
    ${detailHtml ? `<details open><summary>详情 / 引用</summary>${detailHtml}</details>` : ""}`;
  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
}

// —— 状态条 ——
function stateBar(snap) {
  const map = { awaiting_follow_up: "待补充", screening: "分析中", escalated: "红旗升级",
                awaiting_review: "待医生审核", approved: "已确认", rejected: "已拒绝", closed: "已关闭" };
  const ws = map[snap.workflow_state] || snap.workflow_state || "-";
  $("stateBar").innerHTML =
    `<span class="pill">workflow: ${ws}</span><span class="pill">review: ${snap.review_status || "pending"}</span>` +
    (snap.draft ? `<span class="pill">schema ${snap.draft.schema_version}</span><span class="pill">快照 ${snap.draft.knowledge_snapshot}</span>` : "") +
    `<div>case_id：<b>${esc(snap.case_id)}</b>（任意端续办）</div>`;
}

// —— 区域三形态（3D 点击 → 追加进中间输入框） ——
(function initRegions() {
  window.addEventListener("message", (ev) => {
    try {
      const msg = typeof ev.data === "string" ? JSON.parse(ev.data) : ev.data;
      if (msg && msg.type === "derm_region") {
        // 多区域：regions 为完整已选列表（点选切换后全量上报）
        const regions = msg.regions || (msg.label ? [msg.label] : []);
        const joined = regions.join("、");
        $("selectedRegion").textContent = regions.length ? `已选（${regions.length} 处）：${joined}` : "未选择区域";
        const input = $("caseInput");
        input.value = input.value.replace(/(?:身体区域|皮损位置)：[^；]*；?/, "").replace(/^；/, "");
        if (regions.length) input.value = `皮损位置：${joined}；` + input.value;
      }
    } catch (e) { /* 忽略无关消息 */ }
  });
})();

// —— 提交 / 续办 / 补充 / 审核 ——
document.querySelectorAll("[data-demo]").forEach(b => b.addEventListener("click", async () => {
  $("caseInput").value = "案例" + b.dataset.demo;
  await submit();
}));
$("btnSubmit").addEventListener("click", submit);
async function submit() {
  const text = $("caseInput").value.trim();
  if (!text) { setStatus("请先输入病例描述或选择案例"); return; }
  pushTimeline("user", "提交病例资料", esc(text));
  try {
    const snap = caseId
      ? await api(`/api/cases/${caseId}/supplement`, { reply: text })
      : await api("/api/cases", { text });
    render(snap);
  } catch (e) { setStatus("⚠️ " + e.message); }
}
$("btnResume").addEventListener("click", async () => {
  if (!$("caseList").value) return;
  pushTimeline("user", `跨端续办病例 ${$("caseList").value}`);
  try { render(await api("/api/cases/" + $("caseList").value)); }
  catch (e) { setStatus("⚠️ " + e.message); }
});
async function review(feedback) {
  if (!caseId) return;
  pushTimeline("user", "医生审核：" + feedback.slice(0, 30));
  try { render(await api(`/api/cases/${caseId}/review`, { feedback })); }
  catch (e) { setStatus("⚠️ " + e.message); }
}

// —— 右：症状显示区域（提交后运行出来） ——

function render(snap) {
  lastSnapshot = snap;
  caseId = snap.case_id;
  stateBar(snap);
  const panel = $("resultPanel");
  panel.innerHTML = ""; // 每次以最新分析版本覆盖（同一结果版本三端一致）

  if (snap.workflow_state === "awaiting_follow_up") {
    panel.innerHTML = `
      <p class="flag">📋 资料不足（workflow_state=awaiting_follow_up）</p>
      <ul class="plain">${(snap.missing || []).map(m => `<li>${esc(m)}</li>`).join("")}</ul>
      <p class="meta">请在中间输入框按「字段：值」补充后再次提交</p>`;
    setStatus("待补充资料"); refreshCaseList(); return;
  }
  if (snap.report) {
    panel.innerHTML = `
      <p style="color:var(--ok)"><b>✅ 辅助报告（医生已确认）</b>
      <span class="pill">review: approved</span></p>
      <pre class="report">${esc(snap.report)}</pre>`;
    setStatus("✅ 报告已生成，可导出"); refreshCaseList(); return;
  }
  if (snap.workflow_state === "rejected" || snap.phase === "closed") {
    panel.innerHTML = `<p class="flag">❌ 医生已拒绝，按流程不生成报告。</p><p class="meta">${esc(snap.message || "")}</p>`;
    setStatus("病例已关闭"); refreshCaseList(); return;
  }
  if (snap.draft) renderDraft(snap);
  refreshCaseList();
}

function renderDraft(snap) {
  const d = snap.draft;
  const red = d.red_flag_escalation || {};
  const covered = { complete: "完整", partial: "部分缺失", insufficient: "不足" }[d.case_summary.data_completeness] || "-";
  const flags = (red.reasons || []).map(r => `<p class="flag">🚩 ${esc(r)}</p>`).join("");
  const blocked = red.is_blocking ? `<p class="flag">⛔ workflow_state=${esc(d.workflow_state)}｜${esc(d.blocking_reason || "")}</p>` : "";
  const noEvidence = (d.citations || []).length === 0 ? `<p class="flag">ℹ️ 知识库未覆盖：不给出鉴别诊断（不足以判断），不编造引用</p>` : "";
  const dxRows = (d.suspected_conditions || []).map(c =>
    `<tr><td>${esc(c.name)}</td><td>${esc(c.confidence)}</td><td>${esc((c.supporting_evidence || []).map(e => e.statement).join("；"))} ${citeLinks((c.supporting_evidence || []).flatMap(e => e.citation_ids))}</td></tr>`
  ).join("") || `<tr><td colspan="3">知识库未覆盖，不足以判断（不编造）</td></tr>`;
  const statusMap = { present: "有", absent: "无（医生确认）", unknown: "未知（≠没有）", not_asked: "未询问（≠没有）" };
  const assess = (d.symptom_assessment || []).map(s =>
    `<li>${esc(s.symptom)}｜状态：${statusMap[s.status]}｜依据：${esc(s.basis)} ${citeLinks(s.citation_ids)}</li>`).join("");
  const meds = (d.medication_checks || []).map(m =>
    `<li>${esc(m.generic_name)}：核对【${esc((m.items_to_verify || []).join("、"))}】（需医生确认）</li>`).join("") || "<li>无用药信息</li>";
  const steps = (d.recommended_next_steps || []).map(n => `<li>${esc(n.action)}（${esc(n.urgency)}）</li>`).join("");
  const cites = (d.citations || []).map(c =>
    `<li>${citeLinks([c.citation_id])}｜${esc(c.document_title)}（${esc(c.document_version)}，${esc(c.source_type)}）§${esc(c.page_or_section)}<blockquote>「${esc(c.quoted_text)}」</blockquote></li>`).join("") || "<li>知识库未覆盖</li>";

  pushTimeline("agent", `📄 待审核草稿（完备度 ${covered}｜severity ${d.severity.level}）`,
    `<p><b>病例摘要</b>：${esc(d.case_summary.symptom_summary)}</p>${flags}${blocked}${noEvidence}`);

  const panel = $("resultPanel");
  panel.innerHTML = `
    <p><b>📄 待审核草稿</b><span class="pill">完备度：${covered}</span>
    <span class="pill">workflow: ${esc(d.workflow_state)}</span>
    <span class="pill">review: ${esc(d.review_status)}</span>
    <span class="pill">severity: ${esc(d.severity.level)}</span></p>
    ${flags}${blocked}${noEvidence}
    <p><b>病例摘要</b>：${esc(d.case_summary.symptom_summary)}</p>
    <p><b>鉴别诊断（仅供医生核对）</b></p>
    <table><tr><th>名称</th><th>置信度</th><th>依据</th></tr>${dxRows}</table>
    <p><b>症状评估（unknown/未询问 ≠ 没有）</b></p><ul class="plain">${assess}</ul>
    <p><b>用药核对（禁止自动处方/剂量/疗程）</b></p><ul class="plain">${meds}</ul>
    <p><b>建议下一步（由医生决定）</b></p><ul class="plain">${steps}</ul>
    <p><b>引用（点击看原文）</b></p><ul class="plain">${cites}</ul>
    <p class="meta">模型 ${esc(d.model_version)}｜生成方式 ${esc(d.generated_by)}｜安全策略 ${esc(d.safety_policy_version)}｜${esc(d.generated_at)}</p>
    <div class="row" style="margin-top:8px">
      <button id="btnConfirm">✔ 确认生成辅助报告</button>
      <button id="btnModify">✎ 修订</button>
      <button class="danger" id="btnReject">✖ 拒绝</button>
      <button id="btnExport">⬇ 导出报告</button>
    </div>
    <input id="reviewInput" placeholder="修改意见 / 拒绝理由" style="margin-top:6px">`;
  $("btnConfirm").addEventListener("click", () => review("确认"));
  $("btnModify").addEventListener("click", () => review("修改：" + ($("reviewInput").value.trim() || "请综合完善")));
  $("btnReject").addEventListener("click", () => review("拒绝：" + ($("reviewInput").value.trim() || "医生拒绝")));
  $("btnExport").addEventListener("click", () => {
    const blob = new Blob([renderReportText(d)], { type: "text/markdown" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `dermagent_report_${caseId}.md`; a.click();
  });
  setStatus(red.is_blocking ? "🚩 红旗已升级（escalated），待医生审核" : "草稿待医生审核");
}

// 报告导出：由草稿渲染（与服务端 render_report 同构的精简版）
function renderReportText(d) {
  const cites = (d.citations || []).map(c => `- ${c.citation_id}｜${c.document_title}（${c.document_version}，${c.source_type}）§${c.page_or_section}：「${c.quoted_text}」`).join("\n") || "- 知识库未覆盖";
  return ["# 肤智（DermAgent）辅助报告（医生已确认）",
    `> Schema ${d.schema_version}｜工作流状态 ${d.workflow_state}｜模型 ${d.model_version}｜知识库快照 ${d.knowledge_snapshot}｜安全策略 ${d.safety_policy_version}`,
    "## 一、病例摘要", `${d.case_summary.selected_body_region}：${d.case_summary.symptom_summary}（完备度：${d.case_summary.data_completeness}）`,
    "## 二、鉴别诊断（仅供医生核对）",
    (d.suspected_conditions || []).map(c => `- ${c.name}（${c.confidence}）：${(c.supporting_evidence || []).map(e => e.statement).join("；")}`).join("\n") || "- 知识库未覆盖，不足以判断",
    "## 三、症状依据", (d.symptom_assessment || []).map(s => `- ${s.symptom}（${s.status}）`).join("\n"),
    "## 四、用药核对（禁止自动处方/剂量/疗程）", (d.medication_checks || []).map(m => `- ${m.generic_name}：核对【${(m.items_to_verify || []).join("、")}】`).join("\n") || "- 无",
    `## 五、严重程度（${d.severity.level}）`, (d.severity.basis || []).map(b => `- ${b}`).join("\n"),
    "## 六、红旗与升级", d.red_flag_escalation.triggered ? d.red_flag_escalation.reasons.map(r => `- ${r}`).join("\n") + `\n- ${d.red_flag_escalation.recommended_action}` : "- 未触发红旗",
    "## 九、不确定性", (d.uncertainties || []).map(u => `- ${u}`).join("\n"),
    "## 十、引用", cites, "---", "## 免责声明", d.disclaimer].join("\n\n");
}

function citeLinks(ids) {
  return (ids || []).map(id => `<cite data-cid="${esc(id)}">${esc(id)}</cite>`).join("、") || "—";
}
document.addEventListener("click", (ev) => {
  const cid = ev.target && ev.target.dataset && ev.target.dataset.cid;
  if (!cid || !lastSnapshot || !lastSnapshot.draft) return;
  const c = (lastSnapshot.draft.citations || []).find(x => x.citation_id === cid);
  if (!c) return;
  pushTimeline("agent", `引用详情：${cid}`,
    `${esc(c.document_title)}（${esc(c.document_version)}，${esc(c.source_type)}）§${esc(c.page_or_section)}<blockquote>「${esc(c.quoted_text)}」</blockquote>`);
});

async function refreshCaseList() {
  try {
    const list = await api("/api/cases");
    const sel = $("caseList");
    sel.innerHTML = '<option value="">— 跨端续办：选择病例 —</option>' +
      list.filter(c => c.case_id !== caseId)
          .map(c => `<option value="${esc(c.case_id)}">${esc(c.case_id)}（${esc(c.label || "")}｜${esc(c.workflow_state)}）</option>`).join("");
  } catch (e) { /* 忽略 */ }
}

(async function init() {
  try {
    const h = await api("/api/health");
    setStatus(`服务正常 · schema ${h.schema_version} · 模型：${h.llm} · 快照 ${h.knowledge_snapshot}`);
  } catch (e) { setStatus("⚠️ 服务不可达（断网降级：AI/RAG 不可用）"); }
  refreshCaseList();
})();
