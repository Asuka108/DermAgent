/* ================= v75：浏览器桥（无 Qt 壳时，web 端 = Windows 桌面端同一体验） =================
   桌面端：client/main.py 经 QWebChannel 提供 bridge，编排逻辑（思考占位/结果卡渲染/导航决策/
   病历面板推送）在 Python，经 window.__push* 系列函数下发页面。
   浏览器端（http://127.0.0.1:8765/）：没有 Qt 壳，本文件用同一批 REST API + 同一批
   window.__push* 渲染函数实现同一套编排——对话流、中止、重试、doc/审核卡、导航决策、
   病历单面板、历史续办/删除、LLM 设置、知识库管理全部同口径。
   仅在 !window.qt 时由 main.js 激活（桌面端走 QWebChannel，本文件不生效）。 */
(function () {
  if (window.qt) return;

  var S = { caseId: null, busy: false, abort: null, seq: 0, lastPayload: null };

  /* —— 基础设施 —— */

  function _req(path, body, method) {
    var opt = { method: method || (body !== undefined ? "POST" : "GET"), headers: {} };
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json; charset=utf-8";
      opt.body = JSON.stringify(body);
    }
    return fetch(path, opt).then(function (r) {
      return r.text().then(function (txt) {
        if (!r.ok) throw _httpError(r.status, txt);
        try { return JSON.parse(txt); } catch (e) { return txt; }
      });
    });
  }

  /* v77：错误体优先读 error.message（新契约），回退旧 detail / 原始文本 */
  function _bodyMessage(txt) {
    try {
      var b = JSON.parse(txt) || {};
      var e = b.error || {};
      if (e.message) return String(e.message);
      if (typeof b.detail === "string" && b.detail) return b.detail;
      if (b.detail && typeof b.detail === "object" && b.detail.message) return String(b.detail.message);
    } catch (e2) { /* 非 JSON，按原文 */ }
    return String(txt || "").slice(0, 200);
  }

  function _httpError(status, txt) {
    var err = new Error("HTTP " + status + "：" + _bodyMessage(txt));
    err.status = status;
    try {
      var b = JSON.parse(txt) || {};
      err.code = (b.error && b.error.code) || (b.detail && b.detail.code) || "";
    } catch (e) { /* 保留 HTTP 文本 */ }
    return err;
  }

  function _cb(cb, val) {
    /* 桥回调契约：回调收 JSON 字符串（与 QWebChannel result=str 槽一致，调用方 parseKbJson 解析） */
    if (typeof cb !== "function") return;
    setTimeout(function () { cb(typeof val === "string" ? val : JSON.stringify(val)); }, 0);
  }

  function _err(e, n) { return String((e && e.message) || e || "未知错误").slice(0, n || 160); }

  function _download(blobOrUrl, name) {
    var a = document.createElement("a");
    a.href = blobOrUrl instanceof Blob ? URL.createObjectURL(blobOrUrl) : blobOrUrl;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () {
      if (blobOrUrl instanceof Blob) URL.revokeObjectURL(a.href);
      a.remove();
    }, 800);
  }

  /* —— 快照 → 结果渲染（client/agent_client.py ServerAgentClient._render 同口径） —— */

  function _render(snap) {
    snap = snap || {};
    var action = "review";
    if (snap.report) action = "report";
    else if (snap.workflow_state === "rejected" || snap.phase === "closed") action = "closed";
    else if (snap.workflow_state === "llm_unavailable") action = "llm_unavailable";
    else if (snap.workflow_state === "llm_error") action = "llm_error";
    else if (snap.pending_intent) action = "intent_confirm";   /* v75：三层路由待医生点选，未执行 handler */
    else if (snap.reply) action = "reply";
    else if (snap.doc) action = "doc";
    return { action: action,
      workflow_state: snap.workflow_state, agent_state: snap.agent_state || "",
      review_status: snap.review_status || "pending", draft: snap.draft,
      doc: snap.doc, task_type: snap.task_type || "case_write",
      review_history: snap.review_history || [], doctor_final: snap.doctor_final,
      llm: snap.llm || {}, missing: snap.missing || [], reply: snap.reply,
      tool_history: snap.tool_history || [], selected_body_regions: snap.selected_body_regions || [],
      messages: snap.messages || [], context_compressed: !!snap.context_compressed,
      question: snap.question || "", report: snap.report || "", message: snap.message || "",
      case_id: snap.case_id, open_case: snap.open_case, nav: snap.nav || {},
      next_action: snap.next_action || "", message_id: snap.message_id || "",
      artifacts: snap.artifacts || {}, available_actions: snap.available_actions || [],
      result_summary: snap.result_summary || [], authenticity: snap.authenticity || "",
      ui_action: snap.ui_action, msg_artifacts: snap.msg_artifacts || {},
      plan: snap.plan || null,
      verification: snap.verification || null,
      intent: snap.intent || null, pending_intent: snap.pending_intent || null };
  }

  function _v70(r) {   /* 结果卡「工作结果」按钮区字段（client/main.py _v70_fields 同口径） */
    return { case_id: r.case_id || S.caseId || "", message_id: r.message_id || "",
      available_actions: r.available_actions || [], result_summary: r.result_summary || [],
      artifacts: r.artifacts || {}, authenticity: r.authenticity || "",
      msg_artifacts: r.msg_artifacts || {} };
  }

  /* —— 页面推送（payload 契约与 client/main.py 逐一对应：
     __pushFlow/__pushAgentReply/__pushChatError/__nav 收对象；
     __pushDoc/__pushAssist/__pushCaseRecord/__setMsgArtifacts 收 JSON 字符串） —— */

  function _pushFlow(r) {
    if (window.__pushFlow) window.__pushFlow({ action: r.action || "",
      workflow_state: r.workflow_state || "", question: r.question || "",
      message: r.message || "", missing: r.missing || [],
      review_status: r.review_status || "", llm: r.llm || {}, mode: "server" });
  }

  function _nav(r, trigger, isError) {
    var src = (r && r.nav && typeof r.nav === "object") ? r.nav : null;
    var nav = src ? Object.assign({}, src) :
      { nav_state: isError ? "error" : "idle", next_action: "stay", workspace: null,
        reason: "无服务端导航决策：保持当前工作区（不跳转）" };
    nav.trigger = trigger;
    nav.seq = ++S.seq;   /* 单调序号：web 侧丢弃乱序旧决策（与桌面端 _navigate_to_web 同契约） */
    if (!nav.case_id) nav.case_id = S.caseId || "";
    if (window.__nav) window.__nav(nav);
  }

  function _pushDoc(r) {
    if (!window.__pushDoc) return;
    var b = { task_type: r.task_type || "case_write", workflow_state: r.workflow_state || "",
      doc: r.doc, llm: (r.llm && typeof r.llm === "object") ? r.llm : {},
      message: r.message || "", missing: r.missing || [], tool_history: r.tool_history || [] };
    Object.assign(b, _v70(r));
    window.__pushDoc(JSON.stringify(JSON.stringify(b)));
  }

  function _pushAssist(r) {
    if (!window.__pushAssist) return;
    var b = Object.assign({}, r.draft || {});
    b.review_history = r.review_history || [];
    b.llm = (r.llm && typeof r.llm === "object") ? r.llm : {};
    b.tool_history = r.tool_history || [];
    Object.assign(b, _v70(r));
    window.__pushAssist(JSON.stringify(JSON.stringify(b)));
  }

  function _pushCase(p) {
    if (window.__pushCaseRecord) window.__pushCaseRecord(JSON.stringify(JSON.stringify(p)));
  }

  function _llmOff(rec) {
    var lm = (rec || {}).llm_meta || {};
    return lm.ok === false && lm.source === "none";
  }

  function _pushCaseDone(res, op) {
    res = res || {};
    _pushCase({ ok: res.ok !== false, op: op || "get", record: res.record || {},
      workflow_state: res.workflow_state || "",
      llm_connected: !(_llmOff(res.record) || res.llm_connected === false),
      message: res.message || res.error || "" });
    if ((op === "save" || op === "confirm" || op === "reject") && res.nav && typeof res.nav === "object") {
      _nav({ nav: res.nav }, "case_panel_" + op);
    }
  }

  function pushCaseRecord() {   /* client/main.py push_case_to_window 同口径 */
    if (!S.caseId) {
      _pushCase({ no_case: true,
        message: "当前对话尚未创建病例。在对话中描述病情（如「患者男，35岁，右前臂红斑伴瘙痒7天」）即可生成病历单。" });
      return;
    }
    _req("/api/cases/" + encodeURIComponent(S.caseId) + "/record")
      .then(function (rec) { _pushCaseDone(rec, "get"); })
      .catch(function () {});
  }

  /* —— 渲染 + 导航一体（client/main.py _render_result + _navigate_to_web 同口径） —— */

  function _renderResult(r, trigger, isError, skipReply) {
    _pushFlow(r);
    if (r.context_compressed) {
      if (window.__pushAgentReply) window.__pushAgentReply({
        content: "🧹 上下文已压缩：已生成本病例结构化摘要（含已确认事实、待确认项、过敏史与危险信号、医生修改记录——这些内容永不裁剪），较早的普通对话将按摘要参与后续分析。",
        generated_by: "agent-cmd", llm: { source: "agent", model: "" },
        workflow_state: r.workflow_state || "", tool_history: [] });
    }
    var a = r.action;
    /* v79：非回复分支不保留流式临时气泡，权威卡（确认卡/文书/审核/错误）单独渲染 */
    if (a !== "reply" && window.__turnDiscardPreview) window.__turnDiscardPreview();
    if (a === "intent_confirm") {
      /* v75：待医生点选——只渲染候选卡，不执行任何 handler（与桌面端 _render_result 同口径） */
      if (window.__pushAgentReply) window.__pushAgentReply({
        content: r.message || "这句话有几种理解，请选择要执行的一项（点选前不会执行任何操作）：",
        pending_intent: r.pending_intent,
        llm: { source: "agent", model: "" },
        workflow_state: r.workflow_state || "", intent: r.intent, tool_history: [] });
    } else if (a === "reply") {
      if (skipReply) return _nav(r, trigger, isError);   /* 恢复态消息已渲染，避免重复回复卡 */
      var b = { content: String((r.reply && r.reply.content) || ""),
        generated_by: (r.reply && r.reply.generated_by) || "",
        llm: { source: r.llm && r.llm.source, model: r.llm && r.llm.model },
        workflow_state: r.workflow_state || "", reply_card: r.reply && r.reply.card,
        plan: r.plan || null,
        intent: r.intent, tool_history: r.tool_history || [],
        verification: r.verification || (r.reply && r.reply.verification) || null };
      Object.assign(b, _v70(r));
      if (r.ui_action) b.ui_action = r.ui_action;
      if (window.__pushAgentReply) window.__pushAgentReply(b);
    } else if (a === "llm_error") {
      if (window.__pushChatError) window.__pushChatError({ message: r.message || "LLM 请求失败，请重试",
        unavailable: false, retry_payload: S.lastPayload });
    } else if (a === "llm_unavailable") {
      if (window.__pushChatError) window.__pushChatError({ message: r.message || "当前未连接 LLM，无法回复",
        unavailable: true, retry_payload: null });
    } else if (a === "doc") {
      if (r.doc || !r.message) _pushDoc(r);   /* 空 doc 且有 message → 仅状态提示，不渲染卡 */
    } else if (a === "review") {
      if (r.draft) _pushAssist(r);            /* 无草稿 → 如实不渲染（与桌面端一致） */
    }
    _nav(r, trigger, isError);
  }

  /* —— 对话（client/main.py on_chat_submitted + _chat_work + _on_chat_done 同口径） —— */

  function _summary(text, regions, files, clinic) {
    var parts = [];
    if (text) parts.push(text);
    if (regions && regions.length) parts.push("部位：" + regions.join("；"));
    if (files && files.length) parts.push("附件：" + files.map(function (f) { return (f && f.name) || "?"; }).join("、"));
    if (clinic) {
      var imaging = clinic.imaging || [], ps = [];
      if (clinic.hx) ps.push("病史");
      if (clinic.labs) ps.push("检验" + clinic.labs.length + "项");
      if (clinic.report) ps.push("报告");
      if (clinic.meds || clinic.allergy || clinic.past || clinic.tx) ps.push("用药/史");
      if (imaging.length) {
        var ready = 0;
        imaging.forEach(function (m) { if (m && m.status === "ready") ready++; });
        ps.push("影像" + ready + "张");
      }
      if (ps.length) parts.push("资料：" + ps.join("/"));
    }
    return parts.join("；") || "（空请求）";
  }

  function _chatFinish() {
    S.busy = false; S.abort = null;
    if (window.__setChatBusy) window.__setChatBusy(false);
  }

  /* —— v76 流式：SSE 进度事件 + 最后一次完整快照；失败自动回退阻塞接口 —— */

  function _parseSseBlock(block) {
    var ev = { type: "message", data: null, id: 0 }, dataLines = [];
    block.split("\n").forEach(function (line) {
      if (line.indexOf("event:") === 0) ev.type = line.slice(6).trim();
      else if (line.indexOf("data:") === 0) dataLines.push(line.slice(5).trim());
      else if (line.indexOf("id:") === 0) ev.id = parseInt(line.slice(3).trim(), 10) || 0;
    });
    if (!dataLines.length) return null;
    try { ev.data = JSON.parse(dataLines.join("\n")); } catch (e) { return null; }
    return ev;
  }

  function _readSse(resp, onEvent) {
    var reader = resp.body.getReader(), decoder = new TextDecoder("utf-8"), buf = "";
    function pump() {
      return reader.read().then(function (chunk) {
        if (chunk.done) return;
        buf += decoder.decode(chunk.value, { stream: true });
        var idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          var ev = _parseSseBlock(buf.slice(0, idx));
          buf = buf.slice(idx + 2);
          if (ev) onEvent(ev);
        }
        return pump();
      });
    }
    return pump();
  }

  function _streamError(msg, fallback) {
    var err = new Error(msg);
    err.fallback = !!fallback;
    return err;
  }

  function _chatTryStream(path, body) {
    var clientTurnId = "ct-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8);
    var streamBody = {};
    Object.keys(body).forEach(function (k) { streamBody[k] = body[k]; });
    streamBody.client_turn_id = clientTurnId;
    S.clientTurnId = clientTurnId;
    return fetch(path + "/stream", {
      method: "POST", headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify(streamBody), signal: S.abort ? S.abort.signal : undefined
    }).then(function (r) {
      /* 404/405/501 = 服务端未启用流式 → 回退阻塞接口（重发同一条消息） */
      if (r.status === 404 || r.status === 405 || r.status === 501) {
        throw _streamError("stream_unsupported", true);
      }
      if (!r.ok) {
        return r.text().then(function (txt) {
          var e = _httpError(r.status, txt);
          var se = _streamError(e.message, r.status !== 409);
          se.status = e.status;
          se.code = e.code;
          throw se;
        });
      }
      if (!r.body || typeof r.body.getReader !== "function") {
        throw _streamError("no_stream", true);
      }
      var turnId = r.headers.get("X-Turn-Id") || "";
      var caseId = r.headers.get("X-Case-Id") || "";
      if (caseId) S.caseId = caseId;   /* 首条消息建案：不等 done 即可续办/轮询 */
      var started = false, snap = null, failure = null;
      return _readSse(r, function (ev) {
        if (ev.type === "turn_started") {
          started = true;
          if (ev.data && ev.data.turn_id) turnId = ev.data.turn_id;
          if (ev.data && ev.data.case_id) S.caseId = ev.data.case_id;
          S.turnId = turnId;
        } else if (ev.type === "done") {
          snap = ev.data && ev.data.snapshot;
        } else if (ev.type === "error") {
          failure = ev.data || {};
        } else if (ev.type !== "heartbeat") {
          var fn = window.__turnEvent || window.__turnProgress;
          if (fn) fn(ev.type, ev.data || {}, ev.id);
        }
      }).then(function () {
        if (failure) {
          var e = _streamError(failure.message || "对话失败", false);
          e.turnId = turnId;
          e.code = failure.code || "";
          throw e;
        }
        if (snap) return snap;
        var e2 = _streamError("stream_no_done", !started);
        e2.turnId = turnId;
        throw e2;
      }, function (exc) {
        /* 已拿到 X-Turn-Id＝服务端已受理并启动 turn：读流失败只轮询该 turn，
           绝不再发阻塞请求（否则会在服务端重复执行同一轮问诊）。 */
        if (!started && !turnId) {
          var e3 = _streamError("stream_read_failed", true);
          throw e3;
        }
        exc = exc || new Error("stream_read_failed");
        exc.turnId = exc.turnId || turnId;
        exc.fallback = false;
        throw exc;
      });
    });
  }

  /* 断线重连：SSE 断开不取消 turn，轮询已注册事件直到 done */
  function _pollTurn(turnId) {
    var seq = 0;
    return new Promise(function (resolve, reject) {
      function tick() {
        if (!S.busy) { reject(new Error("已中止")); return; }
        fetch("/api/cases/" + encodeURIComponent(S.caseId) + "/turns/" + encodeURIComponent(turnId)
              + "?after_seq=" + seq)
          .then(function (r) {
            if (r.status === 404) {
              var gone = new Error("turn 已过期");
              gone.fallback = true;
              throw gone;
            }
            if (!r.ok) throw new Error("HTTP " + r.status);
            return r.json();
          })
          .then(function (d) {
            (d.events || []).forEach(function (ev) {
              seq = Math.max(seq, ev.seq || 0);
              if (ev.type !== "heartbeat" && ev.type !== "done") {
                var fn = window.__turnEvent || window.__turnProgress;
                if (fn) fn(ev.type, ev.payload || {}, ev.seq);
              }
            });
            if (d.done && d.snapshot) { resolve(d.snapshot); return; }
            if (d.done && d.error) { reject(new Error(d.error.message || "对话失败")); return; }
            setTimeout(tick, 600);
          })
          .catch(reject);
      }
      tick();
    });
  }

  function _chatBlocking(path, body) {
    return fetch(path, { method: "POST", headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify(body), signal: S.abort ? S.abort.signal : undefined })
      .then(function (r) {
        return r.text().then(function (txt) {
          if (!r.ok) throw new Error("HTTP " + r.status + "：" + txt.slice(0, 200));
          return JSON.parse(txt);
        });
      });
  }

  function _chatSubmit(payloadStr) {
    var data;
    try { data = JSON.parse(payloadStr) || {}; } catch (e) { return; }
    var text = String(data.text || ""), regions = Array.isArray(data.regions) ? data.regions : [];
    var files = Array.isArray(data.files) ? data.files : [];
    var clinic = (data.clinic && typeof data.clinic === "object") ? data.clinic : null;
    if (S.busy) {
      if (window.__pushChatError) window.__pushChatError({ message: "上一条消息仍在处理中，请稍候再发送", retry_payload: null });
      return;
    }
    S.busy = true; S.lastPayload = data;
    if (window.__pushThinking) window.__pushThinking();
    var chatText = text;
    if (regions.length) chatText = (chatText + "\n身体区域：" + regions.join("；")).trim();
    if (!chatText && clinic) chatText = _summary(text, regions, files, clinic);
    var body = { text: chatText };
    if (clinic) body.clinic = clinic;
    if (regions.length) body.regions = regions;
    if (files.length) body.attachments = files;
    S.abort = new AbortController();
    var path = S.caseId ? "/api/cases/" + encodeURIComponent(S.caseId) + "/chat" : "/api/cases";
    var done = function (snap) {
      if (!S.busy) return;   /* 已中止 → 结果作废丢弃（与桌面端 epoch 同语义） */
      _chatFinish();
      S.caseId = snap.case_id || S.caseId;
      if (window.removeConvThinking) window.removeConvThinking();
      var turn = (snap && snap.turn) || {};
      if (window.__turnDone) window.__turnDone({
        status: turn.status || "", elapsed_ms: (turn.budget || {}).elapsed_ms });
      var r = _render(snap);
      _renderResult(r, "chat_done");
      pushCaseRecord();
      if (r.workflow_state === "case_intake" || r.open_case) {
        if (window.__openClinicForm) window.__openClinicForm();
      }
    };
    var fail = function (exc) {
      if (!S.busy) return;
      _chatFinish();
      if (window.__turnError) window.__turnError({ message: _err(exc, 200) });
      if (window.__pushChatError) window.__pushChatError({ message: _err(exc, 300), retry_payload: S.lastPayload });
      _nav({}, "chat_error", true);
    };
    /* v79：首条消息也走 /api/cases/stream（X-Case-Id 提前落定）；不支持时回退阻塞接口 */
    _chatTryStream(path, body).then(done).catch(function (exc) {
      if (!S.busy) return;
      if (exc && exc.turnId && !exc.fallback) {
        _pollTurn(exc.turnId).then(done).catch(function (exc2) {
          if (!S.busy) return;
          if (exc2 && exc2.fallback) _chatBlocking(path, body).then(done).catch(fail);
          else fail(exc2);
        });
        return;
      }
      if (exc && exc.fallback) {
        _chatBlocking(path, body).then(done).catch(fail);
      } else {
        fail(exc);
      }
    });
  }

  function _stop() {   /* v74 中止同口径：作废在途结果，界面立即可继续提问 */
    if (!S.busy) return;
    S.busy = false;
    if (S.abort) { try { S.abort.abort(); } catch (e) {} S.abort = null; }
    if (window.__setChatBusy) window.__setChatBusy(false);
    if (window.__turnError) window.__turnError({ message: "已中止生成" });
    if (window.removeConvThinking) window.removeConvThinking();
    if (typeof setStatus === "function") setStatus("已中止生成，可继续提问");
  }

  function _intentChoose(payloadStr) {
    /* v75：确认卡点选——只执行所选意图，且只执行一次（服务端校验 pending_id 与候选）。 */
    var data;
    try { data = JSON.parse(payloadStr) || {}; } catch (e) { return; }
    if (S.busy) return;
    if (!S.caseId) {
      if (window.__pushChatError) window.__pushChatError({ message: "确认卡已失效，请重新输入您的需求", retry_payload: null });
      if (window.__setChatBusy) window.__setChatBusy(false);
      return;
    }
    S.busy = true;
    if (window.__pushThinking) window.__pushThinking();
    _req("/api/cases/" + encodeURIComponent(S.caseId) + "/intent/choose",
      { intent: String(data.intent || ""), pending_id: String(data.pending_id || "") })
      .then(function (snap) {
        if (!S.busy) return;
        _chatFinish();
        S.caseId = snap.case_id || S.caseId;
        if (window.removeConvThinking) window.removeConvThinking();
        var r = _render(snap);
        _renderResult(r, "chat_done");
        pushCaseRecord();
        if (r.workflow_state === "case_intake" || r.open_case) {
          if (window.__openClinicForm) window.__openClinicForm();
        }
      })
      .catch(function (exc) {
        if (!S.busy) return;
        _chatFinish();
        if (window.__pushChatError) window.__pushChatError({ message: _err(exc, 300), retry_payload: null });
        _nav({}, "chat_error", true);
      });
  }

  /* —— 启动恢复（client/main.py _restore_work + _on_restore_done 同口径） —— */

  function _bootstrap() {
    if (S.caseId || S.busy) return;
    _req("/api/cases").then(function (cases) {
      if (!Array.isArray(cases) || !cases.length || S.caseId || S.busy) return;
      var latest = cases[0] && cases[0].case_id;
      if (!latest) return;
      _req("/api/cases/" + encodeURIComponent(latest)).then(function (snap) {
        if (S.caseId || S.busy) return;   /* 用户已抢先开始新对话则不打断 */
        S.caseId = snap.case_id || latest;
        var r = _render(snap);
        var restored = !!(r.messages && r.messages.length);
        if (restored && window.__restoreConversation) {
          if (window.__setMsgArtifacts) window.__setMsgArtifacts(JSON.stringify(JSON.stringify(r.msg_artifacts || {})));
          window.__restoreConversation(JSON.stringify(r.messages));
        }
        _renderResult(r, "restore", false, restored);
        pushCaseRecord();
        if (typeof setStatus === "function") setStatus("已恢复最近会话（输入即延续该病例上下文；「新对话」可另起）");
      }).catch(function () {});
    }).catch(function () {});
  }

  /* —— 浏览器桥本体（方法签名与 QWebChannel 槽一致：末位参数 = 回调） —— */

  var B = {
    /* 对话 */
    onChatSubmitted: function (payload) { _chatSubmit(payload); },
    intentChoose: function (payload) { _intentChoose(payload); },
    stopChat: function () { _stop(); },
    onNewSession: function () {
      if (S.busy) _stop();   /* 新对话 → 在途结果作废（与桌面端 epoch 同语义） */
      S.caseId = null;
      pushCaseRecord();
    },
    onRegionSelected: function () {},          /* 桌面端仅审计，浏览器无需 */
    navLog: function (payload) { try { console.log("[nav]", JSON.parse(payload)); } catch (e) {} },

    /* 病例列表 / 续办 / 删除 */
    casesList: function (cb) {
      _req("/api/cases").then(function (d) { _cb(cb, { ok: true, cases: d }); },
        function (e) { _cb(cb, { ok: false, cases: [], error: _err(e, 120) }); });
    },
    recordList: function (cb) {
      _req("/api/records").then(function (d) { _cb(cb, { ok: true, records: d }); },
        function (e) { _cb(cb, { ok: false, records: [], error: _err(e, 120) }); });
    },
    caseResume: function (caseId, cb) {
      _req("/api/cases/" + encodeURIComponent(caseId)).then(function (snap) {
        S.caseId = snap.case_id || caseId;
        _renderResult(_render(snap), "resume");
        _cb(cb, { ok: true });
      }, function (e) { _cb(cb, { ok: false, error: _err(e) }); });
    },
    caseDelete: function (caseId, cb) {
      _req("/api/cases/" + encodeURIComponent(caseId), undefined, "DELETE").then(function () {
        if (S.caseId === caseId) S.caseId = null;   /* 删的是当前病例 → 句柄同步失效 */
        _cb(cb, { ok: true });
      }, function (e) { _cb(cb, { ok: false, error: _err(e) }); });
    },
    caseDeleteAll: function (cb) {
      _req("/api/cases", undefined, "DELETE").then(function (d) {
        S.caseId = null;
        _cb(cb, { ok: true, deleted: (d && d.deleted) || 0 });
      }, function (e) { _cb(cb, { ok: false, error: _err(e) }); });
    },

    /* 病例单面板 */
    casePanelToggle: function (action) { if (action === "open") pushCaseRecord(); },
    openCaseWindow: function () { if (window.__openClinicForm) window.__openClinicForm(); },
    caseRecordGet: function (cb) {
      if (!S.caseId) { _cb(cb, { ok: false, no_case: true, history: [],
        message: "当前对话尚未创建病例。在对话中描述病情（如「患者男，35岁，右前臂红斑伴瘙痒7天」）即可生成病历单。" }); return; }
      _req("/api/cases/" + encodeURIComponent(S.caseId) + "/record").then(function (d) { _cb(cb, d); },
        function (e) { _cb(cb, { ok: false, error: _err(e, 200) }); });
    },
    caseRecordSave: function (payload, cb) {
      var f;
      try { f = (JSON.parse(payload) || {}).fields || {}; } catch (e) { _cb(cb, { ok: false, error: "保存数据解析失败" }); return; }
      _req("/api/cases/" + encodeURIComponent(S.caseId) + "/record", { fields: f }, "PUT")
        .then(function (d) { _cb(cb, d); }, function (e) { _cb(cb, { ok: false, error: _err(e, 200) }); });
    },
    caseRecordConfirm: function (payload, cb) {
      var b;
      try { b = JSON.parse(payload) || {}; } catch (e) { b = {}; }
      _req("/api/cases/" + encodeURIComponent(S.caseId) + "/record/confirm",
        { all: !!b.all, fields: b.fields || [] }).then(function (d) { _cb(cb, d); },
        function (e) { _cb(cb, { ok: false, error: _err(e, 200) }); });
    },
    caseRecordReject: function (cb) {
      _req("/api/cases/" + encodeURIComponent(S.caseId) + "/record/reject", {})
        .then(function (d) { _cb(cb, d); }, function (e) { _cb(cb, { ok: false, error: _err(e, 200) }); });
    },
    caseRecordRegen: function (field) {   /* LLM 调用 → 完成后推面板（桌面端 case_done 链路同口径） */
      if (!field || !S.caseId) return;
      _req("/api/cases/" + encodeURIComponent(S.caseId) + "/record/regen", { field: field })
        .then(function (d) { _pushCaseDone(d, "regen"); })
        .catch(function (e) { _pushCase({ ok: false, op: "regen", record: {}, message: _err(e, 200) }); });
    },
    caseHistoryGet: function (cb) {
      if (!S.caseId) { _cb(cb, { ok: false, no_case: true, history: [] }); return; }
      _req("/api/cases/" + encodeURIComponent(S.caseId) + "/record").then(function (rec) {
        _cb(cb, { ok: true, history: ((rec || {}).record || {}).history || [] });
      }, function (e) { _cb(cb, { ok: false, error: _err(e, 200), history: [] }); });
    },
    caseRecordExport: function () {   /* 导出正式病历单（服务端确认门禁 403 时如实报错） */
      if (!S.caseId) return;
      fetch("/api/cases/" + encodeURIComponent(S.caseId) + "/record/export").then(function (r) {
        return r.text().then(function (txt) {
          if (!r.ok) throw new Error("HTTP " + r.status + "：" + txt.slice(0, 120));
          _download(new Blob([txt], { type: "text/markdown" }),
            "dermagent_record_" + (S.caseId || "draft") + ".md");
          if (typeof setStatus === "function") setStatus("✅ 病历单已导出（浏览器下载）");
        });
      }).catch(function (e) {
        if (typeof setStatus === "function") setStatus("⚠️ 无法导出：" + _err(e, 120) + "（需医生确认后导出）");
      });
    },
    exportConsultNote: function (text) {
      _download(new Blob([String(text || "")], { type: "text/markdown" }), "问诊单.md");
    },

    /* 患者档案 / 记忆层（三层记忆 §7：医生手动建档/关联；身份字段仅本机） */
    patientList: function (query, cb) {
      _req("/api/patients?query=" + encodeURIComponent(String(query || ""))).then(function (d) {
        _cb(cb, { ok: true, patients: (d && d.patients) || [] });
      }, function (e) {
        _cb(cb, { ok: false, patients: [], error: _err(e, 160) });
      });
    },
    patientCreate: function (payload, cb) {
      var d;
      try { d = JSON.parse(payload) || {}; } catch (e) { _cb(cb, { ok: false, error: "新建患者数据解析失败" }); return; }
      _req("/api/patients", d).then(function (r) { _cb(cb, { ok: true, patient: r.patient }); },
        function (e) { _cb(cb, { ok: false, error: _err(e, 160) }); });
    },
    patientFacts: function (patientId, cb) {
      _req("/api/patients/" + encodeURIComponent(patientId) + "/facts").then(function (d) {
        _cb(cb, { ok: true, patient: (d && d.patient) || null, facts: (d && d.facts) || [] });
      }, function (e) { _cb(cb, { ok: false, error: _err(e, 160), facts: [] }); });
    },
    patientFactAdd: function (payload, cb) {
      var d;
      try { d = JSON.parse(payload) || {}; } catch (e) { _cb(cb, { ok: false, error: "事实数据解析失败" }); return; }
      _req("/api/patients/" + encodeURIComponent(d.patient_id || "") + "/facts",
        { category: d.category, field: d.field, value: d.value, evidence: d.evidence || "" })
        .then(function (r) { _cb(cb, r); }, function (e) { _cb(cb, { ok: false, error: _err(e, 160) }); });
    },
    patientFactDelete: function (payload, cb) {
      var d;
      try { d = JSON.parse(payload) || {}; } catch (e) { _cb(cb, { ok: false, error: "数据解析失败" }); return; }
      _req("/api/patients/" + encodeURIComponent(d.patient_id || "") + "/facts/" +
        encodeURIComponent(String(d.fact_id)), undefined, "DELETE")
        .then(function (r) { _cb(cb, r); }, function (e) { _cb(cb, { ok: false, error: _err(e, 160) }); });
    },
    patientSave: function (payload, cb) {
      var d;
      try { d = JSON.parse(payload) || {}; } catch (e) { _cb(cb, { ok: false, error: "数据解析失败" }); return; }
      var pid = d.patient_id || "";
      var fields = d.fields || {};
      _req("/api/patients/" + encodeURIComponent(pid), fields, "PATCH")
        .then(function (r) { _cb(cb, r); }, function (e) { _cb(cb, { ok: false, error: _err(e, 160) }); });
    },
    patientLink: function (payload, cb) {
      var d;
      try { d = JSON.parse(payload) || {}; } catch (e) { _cb(cb, { ok: false, error: "数据解析失败" }); return; }
      _req("/api/cases/" + encodeURIComponent(d.case_id || S.caseId || "") + "/patient",
        { patient_id: d.patient_id || "" })
        .then(function (r) { _cb(cb, r); }, function (e) { _cb(cb, { ok: false, error: _err(e, 160) }); });
    },
    patientDelete: function (patientId, cb) {
      _req("/api/patients/" + encodeURIComponent(patientId), undefined, "DELETE")
        .then(function (r) { _cb(cb, r); }, function (e) { _cb(cb, { ok: false, error: _err(e, 160) }); });
    },
    patientMerge: function (payload, cb) {
      var d;
      try { d = JSON.parse(payload) || {}; } catch (e) { _cb(cb, { ok: false, error: "数据解析失败" }); return; }
      _req("/api/patients/" + encodeURIComponent(d.patient_id || "") + "/merge",
        { target_id: d.target_id || "" })
        .then(function (r) { _cb(cb, r); }, function (e) { _cb(cb, { ok: false, error: _err(e, 160) }); });
    },
    caseMemoryGet: function (cb) {
      if (!S.caseId) { _cb(cb, { ok: false, no_case: true }); return; }
      _req("/api/cases/" + encodeURIComponent(S.caseId) + "/memory").then(function (d) { _cb(cb, d); },
        function (e) { _cb(cb, { ok: false, error: _err(e, 160) }); });
    },
    memoryHealth: function (cb) {
      _req("/api/memory/health").then(function (d) { _cb(cb, d); },
        function (e) { _cb(cb, { ok: false, enabled: false, error: _err(e, 160) }); });
    },

    /* 3D：区域提交 / 标注 / 截图 / 校准 / LLM 精细化 */
    regionsCommitted: function (payload, cb) {
      var regions;
      try { regions = (JSON.parse(payload) || {}).regions || []; } catch (e) { regions = []; }
      if (!regions.length) { _cb(cb, { ok: false, error: "未选择部位" }); return; }
      if (!S.caseId) {
        _cb(cb, { ok: false, no_case: true,
          message: "当前对话尚未创建病例，已选部位会随下一条消息一并提交。" });
        return;
      }
      _req("/api/cases/" + encodeURIComponent(S.caseId) + "/regions", { regions: regions })
        .then(function (res) {
          if (res && res.record) _pushCaseDone({ ok: true, record: res.record }, "get");
          if (res && res.nav) _nav({ nav: res.nav }, "regions_committed");
          _cb(cb, { ok: true, record: (res && res.record) || {} });
        }, function (e) { _cb(cb, { ok: false, error: _err(e, 200) }); });
    },
    promptLabel: function (q) { return window.prompt(q, "") || ""; },
    onScreenshot: function (dataUrl) {
      _download(dataUrl, "dermagent-3d-" + Date.now() + ".png");
      if (typeof setStatus === "function") setStatus("3D 视图截图已下载（浏览器端）");
    },
    onManualRegionsSaved: function (payload) {
      try { localStorage.setItem("dermagent.manual-regions", payload); } catch (e) {}
    },
    onManualRegionSaved: function () {},
    onManualRegionsExported: function (payload) {
      _download(new Blob([String(payload || "{}")], { type: "application/json" }),
        "manual-regions-" + Date.now() + ".json");
    },
    onCalibrationSaved: function (payload) {
      try { localStorage.setItem("dermagent.calibrations", payload); } catch (e) {}
    },
    onLLMRefinementRequested: function (payload) {
      try {
        var data = JSON.parse(payload);
        if (!data || !("regions" in data)) throw new Error("无效的精细化请求");
        ["screenshot", "image", "glb", "model_data"].forEach(function (k) {
          if (k in data) throw new Error("refinement request may contain geometry summary only");
        });
        _req("/api/anatomy/refine", data).then(function (res) {
          if (window.receiveLLMRefinement) window.receiveLLMRefinement(JSON.stringify(res));
        }, function (e) {
          if (typeof setStatus === "function") setStatus("LLM 精细化失败：" + _err(e, 120));
        });
      } catch (e) {
        if (typeof setStatus === "function") setStatus("LLM 精细化请求被拒绝：" + _err(e, 120));
      }
    },

    /* 偏好 / 服务端基址 */
    uiPrefsGet: function (cb) {
      var p = {};
      try { p = JSON.parse(localStorage.getItem("dermagent.ui-prefs") || "{}"); } catch (e) {}
      _cb(cb, p);
    },
    uiPrefsSet: function (payload) {
      try { localStorage.setItem("dermagent.ui-prefs", payload); } catch (e) {}
    },
    serverBase: function (cb) { _cb(cb, { ok: true, mode: "server", base: window.location.origin }); },

    /* LLM 连接管理（与桌面端同一 llm_config 数据，经服务端 REST） */
    llmProbe: function (cb) {
      _req("/api/llm/status?check=1").then(function (d) {
        var available = !!d.configured && d.reachable !== false;
        var out = { available: available, mode: "server", model: d.model || "",
          configured: !!d.configured, reachable: d.reachable };
        if (!available) out.error = d.error || "LLM 未配置或服务不可达";
        _cb(cb, out);
      }, function (e) { _cb(cb, { available: false, mode: "server", error: _err(e, 120) }); });
    },
    llmConnectionsList: function (cb) {
      _req("/api/llm/connections").then(function (d) { _cb(cb, d); },
        function (e) { _cb(cb, { ok: false, error: _err(e, 120) }); });
    },
    llmConnectionSave: function (payload, cb) {
      var d;
      try { d = JSON.parse(payload) || {}; } catch (e) { _cb(cb, { ok: false, reason: "bad_payload" }); return; }
      var id = String(d.id || "");
      delete d.id;
      var base = String(d.base_url || "");
      if (base && !/^https?:\/\//i.test(base)) { _cb(cb, { ok: false, reason: "base_url_must_be_http" }); return; }
      _req(id ? "/api/llm/connections/" + encodeURIComponent(id) : "/api/llm/connections", d, id ? "PUT" : "POST")
        .then(function (r) { _cb(cb, r); }, function (e) { _cb(cb, { ok: false, reason: _err(e, 80) }); });
    },
    llmConnectionActivate: function (id, cb) {
      _req("/api/llm/connections/" + encodeURIComponent(id) + "/activate", undefined, "POST")
        .then(function (r) { _cb(cb, r); }, function (e) { _cb(cb, { ok: false, reason: _err(e, 80) }); });
    },
    llmConnectionDelete: function (id, cb) {
      _req("/api/llm/connections/" + encodeURIComponent(id), undefined, "DELETE")
        .then(function (r) { _cb(cb, r); }, function (e) { _cb(cb, { ok: false, reason: _err(e, 80) }); });
    },
    agentSettingsGet: function (cb) { _cb(cb, { mode: "server", server_url: window.location.origin }); },
    agentSettingsSet: function (payload, cb) {
      var d;
      try { d = JSON.parse(payload) || {}; } catch (e) { d = {}; }
      if (d.mode !== "server") { _cb(cb, { ok: false, reason: "浏览器端仅支持服务端接入" }); return; }
      _cb(cb, { ok: true, mode: "server", server_url: window.location.origin });
    },

    /* 知识库管理 */
    kbList: function (cb) {
      _req("/api/kb/books").then(function (d) {
        var out = (d && typeof d === "object") ? d : {};
        var payload = { ok: true, books: out.books || [] };
        for (var k in out) { if (Object.prototype.hasOwnProperty.call(out, k)) payload[k] = out[k]; }
        _cb(cb, payload);
      }, function (e) { _cb(cb, { ok: false, reason: "kb_list_failed", books: [] }); });
    },
    kbHealth: function (cb) {
      _req("/api/kb/health").then(function (d) { _cb(cb, d); },
        function (e) { _cb(cb, { ok: false, reason: _err(e, 120) }); });
    },
    kbSearch: function (query, topk, cb) {
      _req("/api/kb/search?q=" + encodeURIComponent(String(query || "")) +
           "&top_k=" + (parseInt(topk, 10) || 5)).then(function (d) { _cb(cb, d); },
        function (e) { _cb(cb, { ok: false, reason: _err(e, 120) }); });
    },
    kbImport: function (payload, cb) {
      var d;
      try { d = JSON.parse(payload) || {}; } catch (e) { _cb(cb, { ok: false, reason: "kb_import_failed" }); return; }
      _req("/api/kb/books", { filename: d.filename || "book.txt",
        content_base64: d.data_b64 || "", meta_note: d.meta_note || "" })
        .then(function (r) { _cb(cb, r); }, function (e) { _cb(cb, { ok: false, reason: _err(e, 80) }); });
    },
    kbDelete: function (id, cb) {
      _req("/api/kb/books/" + encodeURIComponent(id), undefined, "DELETE")
        .then(function (r) { _cb(cb, r); }, function (e) { _cb(cb, { ok: false, reason: _err(e, 80) }); });
    },
    kbDeleteHard: function (id, cb) {
      _req("/api/kb/books/" + encodeURIComponent(id) + "/hard", undefined, "DELETE")
        .then(function (r) { _cb(cb, r); }, function (e) { _cb(cb, { ok: false, reason: _err(e, 80) }); });
    },
    kbRestore: function (id, cb) {
      _req("/api/kb/books/" + encodeURIComponent(id) + "/restore", {})
        .then(function (r) { _cb(cb, r); }, function (e) { _cb(cb, { ok: false, reason: _err(e, 80) }); });
    },
    kbBackup: function (cb) {
      _req("/api/kb/backup").then(function (r) {
        if (r && r.ok === false) { _cb(cb, { ok: false, reason: r.reason || "backup_failed" }); return; }
        try {
          _download(new Blob([JSON.stringify(r, null, 2)], { type: "application/json" }),
            "kb-backup-" + new Date().toISOString().slice(0, 10) + ".json");
        } catch (e) { /* 下载被拦截时仍返回数据，调用方自行提示 */ }
        _cb(cb, { ok: true, download: true, kb_snapshot: (r && r.kb_snapshot) || null });
      }, function (e) { _cb(cb, { ok: false, reason: _err(e, 80) }); });
    },
    kbRestoreBackup: function (cb) {   /* 浏览器端无本地备份文件句柄，如实降级 */
      _cb(cb, { ok: false, reason: "browser_no_local_backup" });
    },
    embeddingInfo: function (cb) {
      _req("/api/kb/embedding").then(function (d) { _cb(cb, d); },
        function (e) { _cb(cb, { ok: false, reason: "rag_embed_unavailable" }); });
    },
    embeddingInstall: function (id, cb) {
      _req("/api/kb/embedding/install", { model: id }).then(function (d) { _cb(cb, d); },
        function (e) { _cb(cb, { ok: false, reason: _err(e, 80) }); });
    },
    embeddingSetActive: function (id, cb) {
      _req("/api/kb/embedding/activate", { model: id }).then(function (d) { _cb(cb, d); },
        function (e) { _cb(cb, { ok: false, reason: _err(e, 80) }); });
    }
  };

  window.__browserBridge = function () { return B; };
  window.__browserBootstrap = function () { setTimeout(_bootstrap, 400); };
})();
