/**
 * v70 导航语义反转 + 工作结果按钮区测试 —— 直接执行 client/web/main.js 里的
 * v69 导航模块 + v70 按钮区源码（沙箱切片：v69 起点至 v66 面板之间）。
 *
 * v70 核心行为：
 * 1. window.__nav 只「声明」建议工作区（NAV.suggested），绝不自动切换/弹窗/高亮；
 * 2. 工作窗口只能由医生点击回复卡「工作结果」按钮区的 window.openWork(action) 打开；
 * 3. 按钮清单只来自服务端 available_actions（没有产物就不渲染按钮，绝不造假）；
 * 4. 历史卡各取 MSG_ARTIFACTS[message_id] 自己的按钮；跨病例按钮被守卫拦截。
 */

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const MAIN_JS = resolve(__dirname, "../client/web/main.js");

function navSource(): string {
  const src = readFileSync(MAIN_JS, "utf-8");
  const start = src.indexOf("/* ================= v69：统一导航层");
  const end = src.indexOf("/* ================= v66：右侧门诊病历面板");
  expect(start).toBeGreaterThan(-1);
  expect(end).toBeGreaterThan(start);
  return src.slice(start, end);
}

type El = any;

function mkEl(id: string): El {
  const el: El = {
    id,
    textContent: "",
    title: "",
    style: {},
    disabled: false,
    scrollTop: 0,
    scrollHeight: 100,
    children: [] as El[],
    parentNode: null,
    _classes: new Set<string>(),
    _handlers: {} as Record<string, Function>,
    classList: {
      add: (c: string) => el._classes.add(c),
      remove: (c: string) => el._classes.delete(c),
      contains: (c: string) => el._classes.has(c),
      toggle: (c: string, on?: boolean) => { (on ?? !el._classes.has(c)) ? el._classes.add(c) : el._classes.delete(c); },
    },
    addEventListener: (ev: string, fn: Function) => { el._handlers[ev] = fn; },
    click: () => { (el._handlers["click"] || (() => {}))(); },
    appendChild: (child: El) => { el.children.push(child); child.parentNode = el; return child; },
    focus: () => { doc.activeElement = el; },
    scrollIntoView: () => {},
    querySelector: () => null,
    querySelectorAll: () => [] as El[],
  };
  return el;
}

let doc: any;
let inputEl: El;

function buildEnv() {
  const ids = ["wsTitle", "wsCaseId", "wsOpenCase", "wsOpen3d", "wsBackChat", "workstage",
    "convLog", "casePanel", "cpCitations", "cpEmpty", "cpBody", "chatInput", "cpExportBtn"];
  const map: Record<string, El> = {};
  ids.forEach((id) => { map[id] = mkEl(id); });
  inputEl = map["chatInput"];
  doc = {
    activeElement: null as El | null,
    getElementById: (id: string) => map[id] ?? null,
    createElement: (tag: string) => mkEl("<" + tag + ">"),
    querySelector: () => null,
    addEventListener: () => {},
  };
  return map;
}

function load() {
  buildEnv();
  const win: any = { localStorage: makeStore(), __nav: undefined };
  const calls: any = { layout: [], panel: [], regions: [], status: [], chat: [] };
  const bridge = {
    casePanelToggle: (a: string) => calls.panel.push(a),
    navLog: () => {},
    regionsCommitted: (payload: string, cb?: Function) => {
      calls.regions.push(JSON.parse(payload));
      if (cb) cb(JSON.stringify({ ok: true, record: {} }));
    },
    onChatSubmitted: (payload: string) => { calls.chat.push(payload); },
  };
  const APP_STATE = { layout: "chat_primary" };
  const setLayout = (m: string) => { APP_STATE.layout = m; calls.layout.push(m); };
  const t = (zh: string) => zh;
  const cpState = { data: null as any };
  const selectedRegions = [{ desc: "面部" }, { label: "颈部" }];

  const fn = new Function(
    "window", "document", "localStorage", "setTimeout", "t", "setLayout", "APP_STATE",
    "cpState", "bridge", "console", "selectedRegions", "setStatus", "autosizeInput", "convLogEl",
    navSource() + "\nreturn { NAV: NAV, nav: window.__nav, navLog: window.__navLog, " +
    "navState: window.__navState, navLeave3d: navLeave3d, navManual: navManual, " +
    "navToSources: window.navToSources, navBindChrome: navBindChrome, navRestore: navRestorePersisted, " +
    "openWork: window.openWork, setMsgArtifacts: window.__setMsgArtifacts, " +
    "msgArtifacts: function () { return MSG_ARTIFACTS; }, " +
    "renderWorkActions: renderWorkActions, applyPendingCase: navApplyPendingCase };",
  );
  const setStatus = (msg: string, kind: string) => { calls.status.push([msg, kind]); };
  const api = fn(win, doc, win.localStorage, setTimeout, t, setLayout, APP_STATE, cpState,
    bridge, console, selectedRegions, setStatus, () => {},
    () => doc.getElementById("convLog"));
  return { win, api, calls, bridge, cpState, APP_STATE, map: doc };
}

function makeStore() {
  const m = new Map<string, string>();
  return {
    getItem: (k: string) => (m.has(k) ? m.get(k)! : null),
    setItem: (k: string, v: string) => { m.set(k, v); },
    removeItem: (k: string) => { m.delete(k); },
    _m: m,
  };
}

const DEC = (o: Record<string, any> = {}) => Object.assign({
  nav_state: "chat_reply", next_action: "navigate_to_chat", workspace: "chat",
  case_id: "c1", session_id: "c1", workflow_state: "chat_reply", task_type: "chat",
  tool_calls: [], reason: "test", seq: 1,
}, o);

describe("v70 导航语义反转：只声明、绝不自动执行", () => {
  it("病例类决策到达：不切工作区、不开面板，只记录建议工作区", () => {
    const env = load();
    expect(env.api.nav(DEC({ nav_state: "case_draft", next_action: "navigate_to_case", workspace: "case", seq: 1 })))
      .toBe("ok");
    expect(env.api.NAV.workspace).toBe("chat");                 // 仍在对话工作区
    expect(env.api.NAV.suggested).toBe("case");                 // 只声明建议
    expect(env.map.getElementById("workstage").classList.contains("case-open")).toBe(false);
    expect(env.calls.panel).toEqual([]);                        // 病例面板未被拉起
    expect(env.calls.layout).toEqual([]);                       // 布局未被切换
    const log = JSON.parse(env.api.navLog());
    expect(log[log.length - 1].auto_switched).toBe(false);
    expect(log[log.length - 1].suggested_workspace).toBe("case");
    expect(log[log.length - 1].to_workspace).toBe("chat");
  });

  it("3D 决策同样只声明：布局保持对话全宽（3D 是可选辅助工具）", () => {
    const env = load();
    env.api.nav(DEC({ nav_state: "tool_3d_active", workspace: "3d", next_action: "navigate_to_3d", seq: 1 }));
    expect(env.api.NAV.workspace).toBe("chat");
    expect(env.APP_STATE.layout).toBe("chat_primary");
    expect(env.api.NAV.suggested).toBe("3d");
  });

  it("声明仍更新导航状态：case_id / nav_state / 持久化照常", () => {
    const env = load();
    env.api.nav(DEC({ case_id: "c42", nav_state: "case_draft", workspace: "case", seq: 1 }));
    expect(env.api.NAV.caseId).toBe("c42");
    expect(env.api.NAV.navState).toBe("case_draft");
    const saved = JSON.parse(env.win.localStorage.getItem("dermagent.nav.v1")!);
    expect(saved.caseId).toBe("c42");
    expect(saved.workspace).toBe("chat");
  });

  it("focus_input 仍执行：回复留在对话区，医生可直接继续输入", () => {
    const env = load();
    env.api.nav(DEC({ nav_state: "collecting_information", next_action: "focus_input",
      workspace: "chat", focus_input: true, workflow_state: "clarifying", seq: 1 }));
    expect(doc.activeElement).toBe(inputEl);
    const log = JSON.parse(env.api.navLog());
    expect(log[log.length - 1].focus_ok).toBe(true);
  });

  it("对话区滚动仍执行（不滚病例面板、不闪结果卡）", () => {
    const env = load();
    env.api.nav(DEC({ scroll_to_latest: true, seq: 1 }));
    expect(env.map.getElementById("convLog").scrollTop).toBe(100);
    const log = JSON.parse(env.api.navLog());
    expect(log[log.length - 1].scroll_ok).toBe(true);
  });

  it("乱序守卫保留：序号倒退的旧决策一律丢弃", () => {
    const env = load();
    env.api.nav(DEC({ case_id: "caseA", workspace: "chat", seq: 7 }));
    const res = env.api.nav(DEC({ case_id: "caseA", workspace: "chat", seq: 5 }));
    expect(res).toBe("out_of_order");
    const log = JSON.parse(env.api.navLog());
    expect(log[log.length - 1].skipped).toBe("out_of_order");
  });

  it("过期病例守卫保留：决策病例与当前病例不符 → 丢弃", () => {
    const env = load();
    env.api.nav(DEC({ case_id: "caseA", workspace: "chat", seq: 1 }));
    const res = env.api.nav(DEC({ case_id: "caseB", workspace: "case",
      next_action: "navigate_to_case", nav_state: "case_draft", seq: 2 }));
    expect(res).toBe("stale_case");
    expect(env.api.NAV.caseId).toBe("caseA");
    expect(env.api.NAV.workspace).toBe("chat");
  });

  it("源码级守卫：导航模块不再包含自动切换语句", () => {
    const src = navSource();
    expect(src).toContain("v70 语义反转");
    expect(src).not.toContain("NAV.lastKey = key");   // 决策驱动的自动切换/去重窗口已移除
    expect(src).toContain("auto_switched");
    expect(src).toContain("window.openWork");
  });
});

describe("v70 工作结果按钮区：openWork 是唯一打开入口", () => {
  it("open_case：医生点击后才开病例工作区与面板", () => {
    const env = load();
    expect(env.api.openWork("open_case", {})).toBe("ok");
    expect(env.api.NAV.workspace).toBe("case");
    expect(env.map.getElementById("workstage").classList.contains("case-open")).toBe(true);
    expect(env.calls.panel).toEqual(["open"]);
    const log = JSON.parse(env.api.navLog());
    expect(log[log.length - 1].next_action).toBe("open_case");
    expect(log[log.length - 1].trigger).toBe("work_open_case");
  });

  it("open_3d：医生点击后才进入 3D 布局", () => {
    const env = load();
    expect(env.api.openWork("open_3d", {})).toBe("ok");
    expect(env.api.NAV.workspace).toBe("3d");
    expect(env.APP_STATE.layout).toBe("model_primary");
  });

  it("跨病例守卫：点历史消息的按钮不会打开别的病例", () => {
    const env = load();
    env.api.nav(DEC({ case_id: "caseA", seq: 1 }));   // NAV.caseId = caseA
    const res = env.api.openWork("open_case", { caseId: "caseB" });
    expect(res).toBe("stale_case");
    expect(env.api.NAV.workspace).toBe("chat");
    expect(env.calls.panel).toEqual([]);
  });

  it("open_sources：面板数据未就绪时挂起，由快照渲染后消费", () => {
    const env = load();
    expect(env.api.openWork("open_sources", {})).toBe("ok");
    expect(env.api.NAV.pendingSources).toBe(true);
    env.api.applyPendingCase();
    expect(env.api.NAV.pendingSources).toBe(false);
  });

  it("open_medication / open_risk：定位对应字段高亮（挂起→消费）", () => {
    const env = load();
    expect(env.api.openWork("open_medication", {})).toBe("ok");
    expect(env.api.NAV.pendingHighlight).toEqual(["medication_check"]);
    env.api.applyPendingCase();
    expect(env.api.NAV.pendingHighlight).toEqual([]);
    expect(env.api.openWork("open_risk", {})).toBe("ok");
    expect(env.api.NAV.pendingHighlight).toEqual(["danger_signals"]);
  });

  it("export_case：不绕过确认门禁——未确认只提示，不伪造解锁", () => {
    const env = load();
    const eb = env.map.getElementById("cpExportBtn");
    eb.disabled = true;
    expect(env.api.openWork("export_case", {})).toBe("ok");
    expect(eb._classes.has("nav-flash")).toBe(true);   // 注意力引导
    expect(eb.disabled).toBe(true);                     // 门禁原样保留
  });

  it("retry：原样重发上一次 payload", () => {
    const env = load();
    expect(env.api.openWork("retry", { retryPayload: '{"text":"case"}' })).toBe("ok");
    expect(env.calls.chat).toEqual(['{"text":"case"}']);
  });

  it("未知动作拒绝执行", () => {
    const env = load();
    expect(env.api.openWork("open_unknown", {})).toBe("unknown_action");
  });
});

describe("v70 按钮区渲染：只按服务端清单渲染，绝不造假", () => {
  it("available_actions + result_summary → 摘要一行 + 按钮清单（open_case 主按钮）", () => {
    const env = load();
    const wrap = env.api.renderWorkActions(
      { available_actions: ["open_case", "open_3d"], result_summary: ["已生成病例草稿"],
        case_id: "c1", message_id: "m1" }, {});
    expect(wrap).not.toBeNull();
    expect(wrap.className).toBe("conv-work");
    expect(wrap.children[0].textContent).toContain("工作结果");
    expect(wrap.children[0].textContent).toContain("已生成病例草稿");
    const row = wrap.children[1];
    expect(row.children.length).toBe(2);
    expect(row.children[0].className).toBe("primary");
    expect(row.children[0].textContent).toContain("查看病例");
    expect(row.children[1].textContent).toContain("打开 3D 人体模型");
  });

  it("点击按钮 → window.openWork(action, ctx)（携带 case_id/message_id）", () => {
    const env = load();
    const seen: any[] = [];
    env.win.openWork = (a: string, ctx: any) => { seen.push([a, ctx]); return "ok"; };
    const wrap = env.api.renderWorkActions(
      { available_actions: ["open_case"], case_id: "c9", message_id: "m9" }, {});
    const btn = wrap.children[0].children[0];   // 无摘要时 children[0] 即按钮行
    btn.click();
    expect(seen).toEqual([["open_case",
      expect.objectContaining({ caseId: "c9", messageId: "m9" })]]);
  });

  it("无产物/无动作 → 不渲染任何按钮（纯闲聊回复零虚假按钮）", () => {
    const env = load();
    expect(env.api.renderWorkActions({}, {})).toBeNull();
    expect(env.api.renderWorkActions({ available_actions: [], result_summary: [] }, {})).toBeNull();
  });

  it("未知动作名被过滤（前后端清单不一致时不渲染乱码按钮）", () => {
    const env = load();
    expect(env.api.renderWorkActions({ available_actions: ["bogus_action"] }, {})).toBeNull();
  });
});

describe("v70 消息级产物存档：历史卡各取自己的按钮", () => {
  it("__setMsgArtifacts 注入后按 message_id 取回", () => {
    const env = load();
    expect(env.api.setMsgArtifacts(JSON.stringify({
      m1: { artifacts: { case_draft: true }, available_actions: ["open_case"],
            result_summary: ["已生成病例草稿"], authenticity: "real_ai" },
    }))).toBe("ok");
    const ma = env.api.msgArtifacts();
    expect(ma.m1.available_actions).toEqual(["open_case"]);
    expect(ma.m1.authenticity).toBe("real_ai");
    // 非对象载荷 → 拒绝并保持空表
    expect(env.api.setMsgArtifacts("[1,2]")).toBe("ok");
    expect(Object.keys(env.api.msgArtifacts()).length).toBe(0);
    expect(env.api.setMsgArtifacts("not json")).toBe("parse_error");
  });
});

describe("v70-C 3D 纯按需：选点写回病例并在对话中显示确认卡", () => {
  it("从 3D 返回对话：部位写入病例 + 对话流出现确认消息卡", () => {
    const env = load();
    // 进入 3D 也必须由医生手动（openWork），这里只验证写回链路
    expect(env.api.openWork("open_3d", {})).toBe("ok");
    env.api.navLeave3d();
    expect(env.api.NAV.workspace).toBe("chat");
    expect(env.calls.regions).toEqual([{ regions: ["面部", "颈部"] }]);
    expect(doc.activeElement).toBe(inputEl);
    const logEl = env.map.getElementById("convLog");
    expect(logEl.children.length).toBe(1);          // 对话确认消息卡
    expect(logEl.children[0].children[0].textContent).toContain("3D 选点写入病例");
    expect(env.calls.status.pop()![1]).toBe("ok");
  });

  it("新对话/发送后回到对话区：不存在任何自动进入 3D 的路径", () => {
    const env = load();
    // 沙箱内唯一进入 3D 的入口是 openWork("open_3d")（医生点击）；
    // 导航决策（workspace:"3d"）不再触发布局切换。
    env.api.nav(DEC({ nav_state: "tool_3d_active", workspace: "3d", next_action: "navigate_to_3d", seq: 1 }));
    expect(env.APP_STATE.layout).toBe("chat_primary");
  });
});
