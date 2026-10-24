#!/usr/bin/env python3
"""肤智（DermAgent）Windows 客户端 —— 与 Web/Android 三端一致：
左：历史对话记录 ｜ 中：3D 真实人体（区域点击回填）｜ 右：症状显示/采集 + 医生在环。

安全边界（参赛计划 §7.4 / §11.2）：
- 客户端不得直连模型供应商 API（llm 直连已按新计划移除）：server 模式接入可信服务层（Key 只在服务端），
  mock 模式为离线演示资产（确定性规则管线，shared/derm_core 单一实现）；
- 照片白名单 + EXIF 清理 + 仅本地脱敏副本；操作审计 JSONL；断网明确提示可切 mock。
仅供医生决策支持演示，不构成诊疗建议或处方。
"""

import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html import escape as esc   # v22 修复：esc 被引用但从未定义（提交病例时 NameError）

from PySide6.QtCore import QObject, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QColor, QPalette
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDockWidget, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QTextBrowser, QToolBar, QVBoxLayout, QWidget,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))
from agent_client import MockAgentClient, ServerAgentClient  # noqa: E402
from derm_core import DEMO_CASES  # noqa: E402
from llm_config import (CONFIG_FILE as LLM_CONFIG_FILE, active_connection, activate as activate_llm_connection,
                        delete as delete_llm_connection, list_public as list_llm_connections,
                        save_connection as save_llm_connection)  # noqa: E402

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
AUDIT_FILE = os.path.join(DATA_DIR, "audit.jsonl")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
ALLOWED_IMG = {".png", ".jpg", ".jpeg", ".webp"}
MAX_IMG_BYTES = 10 * 1024 * 1024

try:
    from PySide6.QtWebChannel import QWebChannel
    from PySide6.QtWebEngineWidgets import QWebEngineView
    HAS_WEBENGINE = True
except Exception:  # noqa: BLE001
    HAS_WEBENGINE = False

REGION_FALLBACK = {
    "头面部": "head_face", "颈部": "neck", "胸部": "chest", "上背": "back_upper",
    "腹部": "abdomen", "腰部（下背）": "waist", "生殖/隐私部位": "groin",
    "上臂（左）": "arm_upper_l", "上臂（右）": "arm_upper_r",
    "前臂（左）": "forearm_l", "前臂（右）": "forearm_r",
    "手部（左）": "hand_l", "手部（右）": "hand_r",
    "大腿（左）": "thigh_l", "大腿（右）": "thigh_r",
    "小腿（左）": "calf_l", "小腿（右）": "calf_r",
    "足部（左）": "foot_l", "足部（右）": "foot_r",
}
DISCLAIMER_SHORT = "仅供医生决策支持 · 不构成诊断或处方 · 合成/脱敏数据"
STATUS_LABEL = {"present": "有", "absent": "无（医生确认）",
                "unknown": "未知（≠没有）", "not_asked": "未询问（≠没有）"}
# v59：任务类型中文名（doc 卡标题 / 状态栏文案与 server TASK_LABELS 同表）
TASK_TYPE_LABELS = {
    "case_write": "病例写作", "case_edit": "病例编辑", "history": "现病史整理",
    "lesion_desc": "皮损描述", "differential": "鉴别分析", "treatment_ref": "治疗参考",
    "medication_review": "用药核对", "patient_edu": "患者健康教育", "referral": "转诊摘要",
    "followup": "随访记录", "summary": "病例摘要", "report_export": "报告导出",
}


def audit(event: str, detail="") -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    detail = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    with open(AUDIT_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "event": event,
                             "detail": detail[:500]}, ensure_ascii=False) + "\n")


# —— v53：本地服务端自动托管（server 模式打开程序即自动带起 server/app.py） ——
# 幂等：健康探测通过（/api/health status=ok）就复用既有服务端，绝不重复拉起；
# 只对 127.0.0.1/localhost/::1 生效，远程地址不托管；只 terminate 自己 spawn 的子进程。

SERVER_APP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server", "app.py")
LOCAL_SERVER_PROC = None          # 本客户端 spawn 的服务端子进程（退出时收掉）


def _read_agent_settings() -> dict:
    """v54：接入设置（mode/server_url）唯一读取入口——顶栏控件移除后由设置页经 bridge 读写。
    无配置/配置损坏时默认 server + 本机地址（v53 自动托管下打开即用；坏值不伪造，回落默认）。"""
    cfg = {}
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            cfg = data
    except (OSError, json.JSONDecodeError):
        pass
    mode = cfg.get("mode") if cfg.get("mode") in ("mock", "server") else "server"
    url = str(cfg.get("server_url") or "").strip() or "http://127.0.0.1:8765"
    return {"mode": mode, "server_url": url}


def _server_health_ok(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url + "/api/health", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return isinstance(data, dict) and data.get("status") == "ok"
    except Exception:  # noqa: BLE001 — 未启动/端口被占/非本程序服务端，一律按不可用处理
        return False


def _ensure_local_server(url: str, wait_seconds: float = 30.0):
    """server 模式 + 本机地址时自动拉起本机服务端。返回 (ok, reason)：
    already_running（复用）/ started（本次拉起）/ remote（远程地址不托管）/
    missing（找不到 server/app.py）/ invalid_url（无端口无法托管）/ timeout（拉起失败）。"""
    global LOCAL_SERVER_PROC
    try:
        parts = urllib.parse.urlsplit(url if "://" in url else "http://" + url)
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        return False, "invalid_url"
    if host not in ("127.0.0.1", "localhost", "::1") or not port:
        return False, "remote"
    if _server_health_ok(url):
        return True, "already_running"
    if not os.path.exists(SERVER_APP_PATH):
        return False, "missing"
    os.makedirs(DATA_DIR, exist_ok=True)
    log = open(os.path.join(DATA_DIR, "server.log"), "ab")   # 子进程日志可排查启动失败
    proc = subprocess.Popen([sys.executable, SERVER_APP_PATH],
                            cwd=os.path.dirname(SERVER_APP_PATH),
                            stdout=log, stderr=subprocess.STDOUT,
                            env=dict(os.environ, PORT=str(port)))
    LOCAL_SERVER_PROC = proc
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if _server_health_ok(url, timeout=0.8):
            audit("server_autostart", {"port": port, "pid": proc.pid})
            return True, "started"
        if proc.poll() is not None:   # 子进程已退出（端口被占/依赖缺失），不空等
            break
        time.sleep(0.4)
    audit("server_autostart_failed", {"port": port, "log": "client/data/server.log"})
    return False, "timeout"


# —— §16.1：本地书籍知识库（RAG）共享访问 ——
# Web 左下角齿轮设置面板（bridge slots）与原生「⚙ 设置」对话框共用以下助手，
# 直接操作 server/rag.py 同一套落盘（server/data/rag/），不经 HTTP，mock/server 模式均可用。

KB_STATUS_LABELS = {
    "queued": "排队中", "parsing": "解析中", "chunking": "切分中", "indexing": "索引中",
    "ready": "检索中", "failed": "导入失败", "quarantined": "已停止检索",
}


def load_rag_module():
    """导入 server/rag.py（本地知识库核心）；不可用返回 None（调用方明确降级，不伪造数据）。"""
    srv = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server")
    if srv not in sys.path:
        sys.path.insert(0, srv)
    try:
        import rag  # noqa: PLC0415
        return rag
    except Exception:  # noqa: BLE001
        return None


def load_rag_embed_module():
    """导入 server/rag_embed.py（v49 向量模型自选安装）；不可用返回 None。"""
    srv = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server")
    if srv not in sys.path:
        sys.path.insert(0, srv)
    try:
        import rag_embed  # noqa: PLC0415
        return rag_embed
    except Exception:  # noqa: BLE001
        return None


def kb_list_payload() -> dict:
    """书籍列表 + RAG 运行状态（kb_snapshot / 检索模式 / 向量模型状态），供 Web 与原生面板共用。"""
    rag = load_rag_module()
    if rag is None:
        return {"ok": False, "reason": "rag_unavailable", "books": []}
    try:
        db = rag.load_books()
        books = []
        for b in db["books"].values():
            meta = b.get("meta") or {}
            version = meta.get("version")
            books.append({
                "book_id": b["book_id"], "filename": b.get("filename"),
                "title": meta.get("title"), "format_incomplete": bool(meta.get("format_incomplete")),
                "status": b.get("status"),
                "status_label": KB_STATUS_LABELS.get(b.get("status"), b.get("status")),
                "quarantined": b.get("status") == "quarantined",
                "version_label": version if version and version != "（未提取）" else "",
                "section_count": len(b.get("chunks") or []),
                "updated_label": (b.get("updated_at") or "")[:19].replace("T", " "),
                "error": b.get("error"),
            })
        st = rag.embedding_status()          # v50：惰性同步真实状态（此前只在检索后同步，启动即显示"未安装"）
        return {"ok": True, "books": books, "kb_snapshot": rag.kb_snapshot(db),
                "embedding_state": st["state"], "embedding_detail": st.get("detail", ""),
                "retrieval_mode": "hybrid" if st["state"] == "ready" else "bm25"}
    except Exception as exc:  # noqa: BLE001
        audit("kb_list_error", str(exc))
        return {"ok": False, "reason": "kb_list_failed", "books": []}


def kb_backup_to_disk() -> dict:
    """完整知识库备份落盘到 server/data/rag/backups/（书籍/文本/索引/元数据一体）。"""
    rag = load_rag_module()
    if rag is None:
        return {"ok": False, "reason": "rag_unavailable"}
    try:
        res = rag.backup_kb()
        out_dir = os.path.join(rag.RAG_DIR, "backups")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "kb-backup-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False)
        audit("kb_backup", path)
        return {"ok": True, "path": path, "kb_snapshot": res.get("kb_snapshot")}
    except Exception as exc:  # noqa: BLE001
        audit("kb_backup_error", str(exc))
        return {"ok": False, "reason": "kb_backup_failed"}


def kb_restore_latest_backup() -> dict:
    rag = load_rag_module()
    if rag is None:
        return {"ok": False, "reason": "rag_unavailable"}
    try:
        out_dir = os.path.join(rag.RAG_DIR, "backups")
        files = sorted(g for g in os.listdir(out_dir) if g.endswith(".json")) if os.path.isdir(out_dir) else []
        if not files:
            return {"ok": False, "reason": "no_backup"}
        with open(os.path.join(out_dir, files[-1]), encoding="utf-8") as fh:
            res = rag.restore_kb(json.load(fh).get("data"))
        audit("kb_restore_backup", res)
        return res
    except Exception as exc:  # noqa: BLE001
        audit("kb_restore_backup_error", str(exc))
        return {"ok": False, "reason": "kb_restore_backup_failed"}


# —— v43：界面语言偏好（双齿轮共享）与 LLM 连接配置（仅落盘服务端） ——

UI_PREFS_FILE = os.path.join(DATA_DIR, "ui-prefs.json")
SERVER_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server", "data")


def load_ui_prefs() -> dict:
    try:
        with open(UI_PREFS_FILE, encoding="utf-8") as fh:
            prefs = json.load(fh)
        return prefs if isinstance(prefs, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_ui_prefs(prefs: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    merged = load_ui_prefs()
    merged.update({k: v for k, v in prefs.items() if v is not None})
    tmp = UI_PREFS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, ensure_ascii=False)
    os.replace(tmp, UI_PREFS_FILE)


def read_llm_config() -> dict:
    """兼容原生旧表单：返回当前活动连接；不经过 Web bridge 返回 Key。"""
    c = active_connection()
    if not c:
        return {"base_url": "", "api_key": "", "model": "", "thinking_level": "", "id": ""}
    return dict(c)


def write_llm_config(base_url: str, model: str, api_key: str, keep_existing_key: bool,
                     thinking_level: str = "") -> dict:
    """旧表单保存入口代理到活动连接，避免覆盖多连接文件。"""
    current = active_connection()
    payload = {"name": (current or {}).get("name") or "默认连接",
               "provider": (current or {}).get("provider") or "custom",
               "base_url": base_url, "model": model, "thinking_level": thinking_level}
    if api_key.strip():
        payload["api_key"] = api_key.strip()
    res = save_llm_connection(payload, (current or {}).get("id"))
    c = res["connection"]
    audit("llm_config_updated", {"id": c["id"], "base_url": c["base_url"],
                                 "model": c["model"], "has_key": c["has_key"]})
    return {"ok": True, "base_url": c["base_url"], "model": c["model"],
            "thinking_level": c["thinking_level"], "has_key": c["has_key"], "key_tail": c["key_tail"]}


class WebBridge(QObject):
    def __init__(self, window):
        super().__init__()
        self._window = window

    @Slot(str)
    def onRegionSelected(self, payload: str):
        region_id, _, label = payload.partition("|")
        audit("region_select", payload)
        self._window.on_region_selected(label or region_id)

    @Slot(str, result=str)
    def regionsCommitted(self, payload: str) -> str:
        """v69：医生在 3D 面板完成选点 → 立即写入当前病例草稿（不依赖下一条消息）。

        服务端只做合并 + record 同步（不调 LLM），返回导航决策；本槽同步返回 JSON，
        web 侧据此把「身体部位」更新到病例面板，并由 __nav 回到对话工作区。"""
        win = self._window
        try:
            regions = (json.loads(payload) or {}).get("regions") or []
        except (ValueError, TypeError):
            regions = []
        if not regions:
            return json.dumps({"ok": False, "error": "未选择部位"}, ensure_ascii=False)
        if not win.case_id:
            # 尚未建立病例：不做隐式新建，如实告知（3D 选点仍随下一条消息作为上下文提交）
            return json.dumps({"ok": False, "no_case": True,
                               "message": "当前对话尚未创建病例，已选部位会随下一条消息一并提交。"},
                              ensure_ascii=False)
        try:
            win._ensure_client()
            res = dict(win.client.set_regions(regions) or {})
        except Exception as exc:  # noqa: BLE001
            audit("regions_commit_failed", {"error": str(exc)[:160]})
            return json.dumps({"ok": False, "error": str(exc)[:200]}, ensure_ascii=False)
        audit("regions_commit", {"case_id": win.case_id, "count": len(regions)})
        if isinstance(res.get("record"), dict):
            # 3D 选点已由服务端即时写入病历单；推送完整 record 快照，
            # 无论病例面板当前是否打开，下一次打开都能看到身体部位。
            win._on_case_done({"ok": True, "__op": "get", "record": res["record"]})   # 病例面板即时刷新
        if isinstance(res.get("nav"), dict) and res["nav"]:
            win._navigate_to_web({"nav": res["nav"]}, trigger="regions_committed")
        return json.dumps({"ok": True, "record": res.get("record") or {}}, ensure_ascii=False)

    @Slot()
    @Slot(str)
    def navLog(self, payload: str):
        """v69：web 侧执行结果回传（导航前/后工作区、聚焦与滚动是否成功、是否被去重跳过）。"""
        try:
            d = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            d = {"raw": str(payload)[:200]}
        if isinstance(d, dict):
            audit("nav_apply", d)

    def openCaseWindow(self):
        """v65/v66：web doc 卡「在病例窗口打开」按钮 → 打开右侧病例面板。"""
        self._window.open_case_window()

    # —— v66：右侧病例面板（与 Agent 对话同页三栏联动；数据经 agent_client 走服务端）——
    # 快操作（get/save/confirm/reject/history）同步返回 JSON 字符串（与 kbList 同模式，
    # 本机/局域网 <1s）；regen 是 LLM 调用可能 30s+，必须后台线程（caseRecordRegen）。

    @Slot(str)
    def casePanelToggle(self, action: str):
        """面板开/关由 web 侧自行完成（不遮挡对话由 CSS 保证）；open 时 Python 推最新快照。"""
        win = self._window
        if action == "open":
            win.push_case_to_window()

    @Slot(result=str)
    def caseRecordGet(self) -> str:
        win = self._window
        if not win.case_id:
            return json.dumps({"ok": False, "no_case": True,
                               "message": "当前对话尚未创建病例。在对话中描述病情（如「患者男，35岁，右前臂红斑伴瘙痒7天」）即可生成病历单。"},
                              ensure_ascii=False)
        try:
            win._ensure_client()
            return json.dumps(dict(win.client.get_record() or {}), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)[:200]}, ensure_ascii=False)

    @Slot(str, result=str)
    def caseRecordSave(self, payload: str) -> str:
        try:
            fields = (json.loads(payload) or {}).get("fields") or {}
        except (ValueError, TypeError):
            return json.dumps({"ok": False, "error": "保存数据解析失败"}, ensure_ascii=False)
        win = self._window
        try:
            win._ensure_client()
            return json.dumps(dict(win.client.save_record(fields) or {}), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)[:200]}, ensure_ascii=False)

    @Slot(str, result=str)
    def caseRecordConfirm(self, payload: str) -> str:
        try:
            body = json.loads(payload) or {}
        except (ValueError, TypeError):
            body = {}
        win = self._window
        try:
            win._ensure_client()
            return json.dumps(dict(win.client.confirm_record(fields=body.get("fields"),
                                                             confirm_all=bool(body.get("all"))) or {}),
                              ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)[:200]}, ensure_ascii=False)

    @Slot(result=str)
    def caseRecordReject(self) -> str:
        win = self._window
        try:
            win._ensure_client()
            return json.dumps(dict(win.client.reject_record() or {}), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)[:200]}, ensure_ascii=False)

    @Slot(result=str)
    def caseHistoryGet(self) -> str:
        """v66：字段修改历史（医生/AI 前后值），供「查看历史版本」弹层。"""
        win = self._window
        if not win.case_id:
            return json.dumps({"ok": False, "no_case": True, "history": []}, ensure_ascii=False)
        try:
            win._ensure_client()
            rec = (win.client.get_record() or {}).get("record") or {}
            return json.dumps({"ok": True, "history": rec.get("history") or []}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)[:200], "history": []}, ensure_ascii=False)

    @Slot(str)
    def caseRecordRegen(self, field: str):
        """重新生成字段：LLM 调用走后台线程，完成后 case_done → _on_case_done → 推回面板。"""
        if field:
            self._window._case_record_work("regen", field=field)

    @Slot()
    def caseRecordExport(self):
        """导出：后台取 md（服务端确认门禁 403 时报错）→ 主线程 QFileDialog 保存。"""
        self._window._case_export_work()

    @Slot(str)
    def exportConsultNote(self, text: str):
        """v73：导出问诊单——内容即对话输出的分析结果，存为 Markdown 文件。"""
        win = self._window
        try:
            path, _ = QFileDialog.getSaveFileName(win, "导出问诊单", "问诊单.md", "Markdown (*.md)")
            if path:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(str(text or ""))
                audit("consult_note_exported", path)
                win._update_status(f"问诊单已导出：{path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(win, "导出失败", str(exc)[:160])

    @Slot()
    def onNewSession(self):
        """v59：web「新对话」→ 重置当前病例句柄（下一条消息 start 新病例而非 chat 进旧病例）。

        win.case_id 与 client.case_id 必须同步清空：发送分支只看 win.case_id，
        若 client 残留旧句柄会在服务端会话已失效时打出 /api/cases/None/chat 之类的脏请求。
        v64-P1 修复：此前误写 self.win（未定义属性）→ slot 静默 AttributeError，
        句柄从未真正清空（对话流清了但下一条消息仍 chat 进旧病例）。"""
        win = self._window
        win.case_id = None
        win.draft = None
        win._chat_epoch = getattr(win, "_chat_epoch", 0) + 1   # v73-fix：新对话 → 在途旧结果作废
        if win.client is not None and hasattr(win.client, "case_id"):
            win.client.case_id = None
        audit("new_session")
        win.push_case_to_window()   # v65：新对话后病例窗口若开着 → 推空模板提示（不残留旧病例数据）

    @Slot(str)
    def onChatSubmitted(self, payload: str):
        """v22 统一提交：web 输入框把「文字+全部位置标签+附件」合并为一条结构化请求。
        Qt 端只做：记录历史（左抽屉）+ mock/server 管线调用，不再有第二套输入框。"""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            audit("chat_submit_invalid", payload[:200])
            return
        clinic = data.get("clinic") if isinstance(data.get("clinic"), dict) else None
        audit("chat_submit", {"text": (data.get("text") or "")[:120],
                              "regions": len(data.get("regions", [])),
                              "files": len(data.get("files", [])),
                              "clinic": bool(clinic),
                              "clinic_labs": len((clinic or {}).get("labs") or []),
                              "clinic_imaging": len((clinic or {}).get("imaging") or [])})
        self._window.on_chat_submitted(data)

    @Slot()
    def stopChat(self):
        """v74：中止生成——会话纪元 +1 使在途请求作废；工作线程返回时按「已作废」收口
        （不杀线程：urllib 阻塞中无法安全强杀，作废后结果丢弃，界面立即可继续提问）。"""
        win = self._window
        if not getattr(win, "_chat_busy", False):
            return
        win._chat_epoch = getattr(win, "_chat_epoch", 0) + 1
        audit("chat_aborted_by_user", {})
        win.chat_done.emit({"__stale": True, "__aborted": True})

    @Slot(str)
    def onScreenshot(self, data_url: str):
        self._window.on_screenshot(data_url)

    @Slot(str)
    def onManualRegionsSaved(self, payload: str):
        """Persist versioned manual regions separately from legacy point calibrations."""
        try:
            data = json.loads(payload)
            if not isinstance(data, dict) or not isinstance(data.get("regions", data.get("manual_regions", {})), dict):
                raise ValueError("manual region document must contain an object of regions")
            os.makedirs(DATA_DIR, exist_ok=True)
            path = os.path.join(DATA_DIR, "manual-regions.saved.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            audit("manual_regions_saved", {"regions": len(data.get("regions", data.get("manual_regions", {}))),
                                            "history": len(data.get("history", []))})
        except Exception as exc:  # noqa: BLE001
            audit("manual_regions_error", str(exc))

    @Slot(str)
    def onManualRegionSaved(self, payload: str):
        """Compatibility slot for a single-region notification; the versioned document is authoritative."""
        audit("manual_region_saved", payload[:300])

    @Slot(str)
    def onManualRegionsExported(self, payload: str):
        """医生主动导出人工区域文档到自选路径。纯写盘：失败不影响客户端已有数据。"""
        try:
            data = json.loads(payload)
            if not isinstance(data, dict) or not isinstance(data.get("regions", data.get("manual_regions", {})), dict):
                raise ValueError("manual region document must contain an object of regions")
            path, _ = QFileDialog.getSaveFileName(
                self._window, "导出人工区域", "manual-regions.json", "JSON (*.json)")
            if not path:
                audit("manual_regions_export_cancelled", {})
                return
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            audit("manual_regions_exported", {"regions": len(data.get("regions", data.get("manual_regions", {}))),
                                              "history": len(data.get("history", [])), "path": path})
        except Exception as exc:  # noqa: BLE001
            audit("manual_regions_export_error", str(exc))

    @Slot(str)
    def onLLMRefinementRequested(self, payload: str):
        """Receive geometry-only batch request; never accept screenshots or GLB bytes."""
        try:
            data = json.loads(payload)
            if not isinstance(data, dict) or "regions" not in data:
                raise ValueError("invalid refinement request")
            if any(k in data for k in ("screenshot", "image", "glb", "model_data")):
                raise ValueError("refinement request may contain geometry summary only")
            audit("llm_refinement_requested", {"regions": len(data["regions"]), "model_key": data.get("model_key")})
            if self.agent_mode == "server" and getattr(self, "client", None) and hasattr(self.client, "refine_regions"):
                self._send_manual_llm_result(self.client.refine_regions(data))
            else:
                self._update_status("已记录批量精细化请求；请在服务模式接入 LLM 后生成审核草稿")
        except Exception as exc:  # noqa: BLE001
            audit("llm_refinement_error", str(exc))
            self._update_status("LLM 精细化请求被拒绝：" + str(exc))

    def _send_manual_llm_result(self, result: dict):
        """Return service output to the Web page through a Qt JavaScript callback."""
        if not self.web or not isinstance(result, dict):
            return
        payload = json.dumps(result, ensure_ascii=False).replace("\\", "\\\\").replace("'", "\\'")
        self.web.page().runJavaScript("window.receiveLLMRefinement && window.receiveLLMRefinement('" + payload + "');")

    @Slot(str)
    def onCalibrationSaved(self, payload: str):
        """校准标注落盘（真实命中 + 人工标签）：JSON 存 data/，供跨会话复用与导出。"""
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            path = os.path.join(DATA_DIR, "anatomy-calibrations.saved.json")
            data = json.loads(payload)
            merged = {}
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as fh:
                        merged = json.load(fh).get("calibrations", {})
                except Exception:
                    merged = {}
            for mk, entries in data.items():
                merged.setdefault(mk, {}).update(entries)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"version": "1.0", "calibrations": merged}, fh, ensure_ascii=False, indent=2)
            audit("calibration_saved", f"{sum(len(v) for v in data.values())} label(s)")
        except Exception as exc:  # noqa: BLE001
            audit("calibration_error", str(exc))

    @Slot(str, result=str)
    def promptLabel(self, question: str) -> str:
        """标注模式取标签：GUI 内 QInputDialog（浏览器 window.prompt 在 QtWebEngine 中不可靠）。"""
        from PySide6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(self._window, "标注部位", question)
        return text if ok and text.strip() else ""

    # —— §16.1：设置面板（Web 左下角齿轮）——知识库管理 / RAG 运行 / 版本与安全 ——

    @Slot(result=str)
    def kbList(self) -> str:
        return json.dumps(kb_list_payload(), ensure_ascii=False)

    @Slot(str, result=str)
    def kbImport(self, payload: str) -> str:
        rag = load_rag_module()
        if rag is None:
            return json.dumps({"ok": False, "reason": "rag_unavailable"})
        try:
            data = json.loads(payload)
            raw = base64.b64decode(data.get("data_b64") or "")
            res = rag.import_book(data.get("filename") or "book.txt", raw, data.get("meta_note") or "")
            audit("kb_import_web" if res.get("ok") else "kb_import_failed",
                  {"book_id": res.get("book_id"), "reason": res.get("reason")})
            return json.dumps(res, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            audit("kb_import_error", str(exc))
            return json.dumps({"ok": False, "reason": "kb_import_failed"})

    @Slot(str, result=str)
    def kbDelete(self, book_id: str) -> str:
        """可恢复删除：立即停止检索（quarantined），数据保留。"""
        rag = load_rag_module()
        res = rag.soft_delete_book(book_id) if rag else {"ok": False, "reason": "rag_unavailable"}
        if res.get("ok"):
            audit("kb_delete_web", book_id)
        return json.dumps(res, ensure_ascii=False)

    @Slot(str, result=str)
    def kbDeleteHard(self, book_id: str) -> str:
        """v71：物理删除（不可恢复）——web 端大文件走 HTTP 通道，此处为降级路径。"""
        rag = load_rag_module()
        res = rag.delete_book(book_id) if rag else {"ok": False, "reason": "rag_unavailable"}
        if res.get("ok"):
            audit("kb_hard_delete_web", book_id)
        return json.dumps(res, ensure_ascii=False)

    @Slot(result=str)
    def serverBase(self) -> str:
        """v71：本机服务端 HTTP 基址——web 端大书导入/删除直连 HTTP（绕开 QWebChannel
        大消息丢失），由 _read_agent_settings 提供与客户端一致的地址。"""
        try:
            cfg = _read_agent_settings()
            return json.dumps({"ok": True, "mode": cfg.get("mode"),
                               "base": cfg.get("server_url") or "http://127.0.0.1:8765"},
                              ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)[:120]}, ensure_ascii=False)

    @Slot(str, result=str)
    def kbRestore(self, book_id: str) -> str:
        rag = load_rag_module()
        res = rag.restore_book(book_id) if rag else {"ok": False, "reason": "rag_unavailable"}
        if res.get("ok"):
            audit("kb_restore_web", book_id)
        return json.dumps(res, ensure_ascii=False)

    @Slot(result=str)
    def kbBackup(self) -> str:
        return json.dumps(kb_backup_to_disk(), ensure_ascii=False)

    @Slot(result=str)
    def kbRestoreBackup(self) -> str:
        return json.dumps(kb_restore_latest_backup(), ensure_ascii=False)

    # —— v49：本地向量模型自选安装（设置页·知识库 pane） ——

    @Slot(result=str)
    def embeddingInfo(self) -> str:
        """清单 + 状态 + 下载进度（供前端轮询）。"""
        emb = load_rag_embed_module()
        if emb is None:
            return json.dumps({"ok": False, "reason": "rag_embed_unavailable"})
        return json.dumps({"ok": True, "catalog": emb.catalog(),
                           "status": emb.status(), "active": emb.active_model(),
                           "last_error": emb.last_error()}, ensure_ascii=False)

    @Slot(str, result=str)
    def embeddingInstall(self, model_id: str) -> str:
        emb = load_rag_embed_module()
        res = emb.install(model_id) if emb else {"ok": False, "reason": "rag_embed_unavailable"}
        audit("kb_embedding_install" if res.get("ok") else "kb_embedding_install_rejected",
              {"model": model_id, "reason": res.get("reason")})
        return json.dumps(res, ensure_ascii=False)

    @Slot(str, result=str)
    def embeddingSetActive(self, model_id: str) -> str:
        emb = load_rag_embed_module()
        res = emb.set_active(model_id) if emb else {"ok": False, "reason": "rag_embed_unavailable"}
        if res.get("ok"):
            audit("kb_embedding_activate", model_id)
        return json.dumps(res, ensure_ascii=False)

    # —— v43：语言偏好与 LLM 连接（Web 齿轮 ⇄ 原生设置共享） ——

    @Slot(result=str)
    def uiPrefsGet(self) -> str:
        return json.dumps(load_ui_prefs(), ensure_ascii=False)

    @Slot(str)
    def uiPrefsSet(self, payload: str) -> None:
        try:
            prefs = json.loads(payload)
            if isinstance(prefs, dict):
                save_ui_prefs(prefs)
                if isinstance(prefs.get("language"), str):
                    self._window.set_app_language(prefs["language"], from_web=True)
        except Exception as exc:  # noqa: BLE001
            audit("ui_prefs_error", str(exc))

    @Slot(result=str)
    def llmConfigGet(self) -> str:
        """活动连接的掩码摘要；绝不返回 Key 本体。"""
        data = list_llm_connections()
        active = next((c for c in data["connections"] if c["id"] == data["active_id"]), {})
        return json.dumps({"ok": True, "configured": bool(active.get("base_url") and active.get("model")),
                           **active}, ensure_ascii=False)

    @Slot(str, result=str)
    def llmConfigSet(self, payload: str) -> str:
        try:
            data = json.loads(payload)
            base_url = str(data.get("base_url") or "")
            if base_url and not base_url.lower().startswith(("http://", "https://")):
                return json.dumps({"ok": False, "reason": "base_url_must_be_http"})
            res = write_llm_config(base_url, str(data.get("model") or ""), str(data.get("api_key") or ""),
                                   keep_existing_key=not str(data.get("api_key") or "").strip(),
                                   thinking_level=str(data.get("thinking_level") or ""))
            return json.dumps(res, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            audit("llm_config_error", str(exc))
            return json.dumps({"ok": False, "reason": "llm_config_failed"})

    @Slot(result=str)
    def llmConnectionsList(self) -> str:
        return json.dumps(list_llm_connections(), ensure_ascii=False)

    @Slot(str, result=str)
    def llmConnectionSave(self, payload: str) -> str:
        try:
            data = json.loads(payload)
            base = str(data.get("base_url") or "")
            if base and not base.lower().startswith(("http://", "https://")):
                return json.dumps({"ok": False, "reason": "base_url_must_be_http"})
            cid = str(data.pop("id", "") or "")
            clear_key = bool(data.pop("clear_key", False))
            return json.dumps(save_llm_connection(data, cid or None, clear_key=clear_key), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            audit("llm_connection_save_error", str(exc))
            return json.dumps({"ok": False, "reason": str(exc)[:80]}, ensure_ascii=False)

    @Slot(str, result=str)
    def llmConnectionActivate(self, connection_id: str) -> str:
        try:
            return json.dumps(activate_llm_connection(connection_id), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "reason": str(exc)[:80]}, ensure_ascii=False)

    @Slot(str, result=str)
    def llmConnectionDelete(self, connection_id: str) -> str:
        try:
            return json.dumps(delete_llm_connection(connection_id), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "reason": str(exc)[:80]}, ensure_ascii=False)

    # —— v54：接入方式（顶栏控件移除后，设置页「通用」pane 读写同一 settings.json） ——

    @Slot(result=str)
    def agentSettingsGet(self) -> str:
        return json.dumps(_read_agent_settings(), ensure_ascii=False)

    @Slot(str, result=str)
    def agentSettingsSet(self, payload: str) -> str:
        try:
            data = json.loads(payload)
            mode = data.get("mode")
            url = str(data.get("server_url") or "").strip()
            if mode not in ("mock", "server"):
                return json.dumps({"ok": False, "reason": "bad_mode"})
            if mode == "server" and not url.lower().startswith(("http://", "https://")):
                return json.dumps({"ok": False, "reason": "url_must_be_http"})
            win = self._window
            win.agent_mode = mode
            win.server_url_cfg = url
            win._save_settings()
            if mode == "server":
                win._autostart_local_server()   # 切到 server：立即探测/带起本机服务端
            else:
                win._update_status("mock · 离线演示模式")
            audit("agent_settings_saved", {"mode": mode})
            return json.dumps({"ok": True, "mode": mode, "server_url": url}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            audit("agent_settings_error", str(exc))
            return json.dumps({"ok": False, "reason": "agent_settings_failed"})

    # —— v58：LLM 真实性防线（发送前探测 / 历史病例列表 / 续办） ——

    @Slot(result=str)
    def llmProbe(self) -> str:
        """发送前 LLM 探测：mock=明确标注的离线演示（结果卡带演示横幅）；server=调服务端
        /api/llm/status?check=1（任何 HTTP 状态码算网关可达）。Key 只在服务端，此处只回
        布尔与模型名，绝不回值、不落日志。"""
        try:
            win = self._window
            if win.agent_mode == "mock":
                return json.dumps({"available": True, "mode": "mock", "note": "offline_demo",
                                   "model": ""}, ensure_ascii=False)
            base = win._server_url().rstrip("/")
            if not base:
                return json.dumps({"available": False, "mode": "server",
                                   "error": "未配置服务地址（设置 → 通用）"}, ensure_ascii=False)
            with urllib.request.urlopen(f"{base}/api/llm/status?check=1", timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            available = bool(data.get("configured")) and data.get("reachable") is not False
            out = {"available": available, "mode": "server", "model": data.get("model", ""),
                   "configured": bool(data.get("configured")), "reachable": data.get("reachable")}
            if not available:
                out["error"] = data.get("error") or "LLM 未配置或服务不可达"
            return json.dumps(out, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"available": False, "mode": "server",
                               "error": str(exc)[:120]}, ensure_ascii=False)

    @Slot(result=str)
    def casesList(self) -> str:
        """左栏「历史病例」：服务端已保存病例列表；失败/空由前端显示空状态，不造假。"""
        try:
            win = self._window
            if win.agent_mode != "server":
                return json.dumps({"ok": True, "cases": []}, ensure_ascii=False)
            client = ServerAgentClient(win._server_url())
            return json.dumps({"ok": True, "cases": client.list_cases()}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "cases": [], "error": str(exc)[:120]}, ensure_ascii=False)

    @Slot(result=str)
    def recordList(self) -> str:
        """v73 病历库：服务端全部已产生病历名录（对话/诊断/手动创建）。"""
        try:
            win = self._window
            if win.agent_mode != "server":
                return json.dumps({"ok": True, "records": []}, ensure_ascii=False)
            client = ServerAgentClient(win._server_url())
            return json.dumps({"ok": True, "records": client.list_records()}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "records": [], "error": str(exc)[:120]}, ensure_ascii=False)

    @Slot(str, result=str)
    def caseResume(self, case_id: str) -> str:
        """左栏「历史病例 → 续办」：复用主窗口既有 resume 链路（_show_result 全套联动）。"""
        try:
            win = self._window
            win._ensure_client()
            win._chat_epoch = getattr(win, "_chat_epoch", 0) + 1   # v73-fix：切换病例 → 在途旧结果作废
            if not isinstance(win.client, ServerAgentClient):
                raise ValueError("续办病例需 server 接入模式")
            result = win.client.resume(case_id)
            win.case_id = case_id
            win.append_history("user", f"续办病例 {case_id}")
            win._show_result(result, trigger="resume")
            return json.dumps({"ok": True}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)[:160]}, ensure_ascii=False)

    @Slot(str, result=str)
    def caseDelete(self, case_id: str) -> str:
        """v60 左栏「历史病例 → 删除」：服务端硬删该病例（列表/续办立即失效）。

        删除的是被删病例自己的会话；若正续办着它，主窗口句柄一并清空，
        避免后续发送打到已删病例（/api/cases/{id}/chat 404）。"""
        try:
            win = self._window
            win._ensure_client()
            if not isinstance(win.client, ServerAgentClient):
                raise ValueError("删除病例需 server 接入模式")
            client = win.client
            client.delete_case(case_id)
            if win.case_id == case_id:      # 删的是当前打开的病例 → 句柄同步失效
                win.case_id = None
                win.draft = None
                if hasattr(client, "case_id"):
                    client.case_id = None
            audit("case_delete_bridge", {"case_id": case_id})
            return json.dumps({"ok": True}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)[:160]}, ensure_ascii=False)

    @Slot(result=str)
    def caseDeleteAll(self) -> str:
        """v62 面板底部「删除全部病例」：服务端批量硬删全部病例（逐条审计）。

        全删后当前病例句柄一并失效——无论删前正续办着哪个病例。"""
        try:
            win = self._window
            win._ensure_client()
            if not isinstance(win.client, ServerAgentClient):
                raise ValueError("删除病例需 server 接入模式")
            r = win.client.delete_all_cases()
            win.case_id = None
            win.draft = None
            if hasattr(win.client, "case_id"):
                win.client.case_id = None
            audit("case_delete_all_bridge", {"deleted": r.get("deleted", 0)})
            return json.dumps({"ok": True, "deleted": r.get("deleted", 0)}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)[:160]}, ensure_ascii=False)


class SettingsDialog(QDialog):
    """§16.1：原生「⚙ 设置」对话框——与 Web 左下角齿轮共享同一套本地 RAG 落盘数据。

    包含知识库管理（批量导入/列表状态/可恢复删除/备份恢复）、RAG 运行设置（检索模式与
    向量模型状态）与版本安全（kb_snapshot）。不伪造：rag 模块不可用时降级为只读提示。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚙ 设置 — 知识库 / RAG 运行 / 版本与安全")
        self.resize(720, 480)
        self._rag = load_rag_module()

        lay = QVBoxLayout(self)
        hint = QLabel("本地医学书籍知识库（RAG）：书籍、切分文本、索引、元数据与审计全部落盘本地，"
                      "不依赖云端服务。与 3D 界面最左下角齿轮设置面板共享同一套数据。")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        # —— v43：界面语言（双齿轮共享，切换即生效并同步 Web 端） ——
        lang_row = QHBoxLayout()
        lang_row.addWidget(QLabel("Language / 界面语言："))
        self.lang_combo = QComboBox()
        self.lang_combo.addItem("简体中文", "zh")
        self.lang_combo.addItem("English", "en")
        idx = self.lang_combo.findData(getattr(self._window, "app_language", "zh"))
        if idx >= 0:
            self.lang_combo.setCurrentIndex(idx)
        self.lang_combo.currentIndexChanged.connect(self._on_lang_changed)
        lang_row.addWidget(self.lang_combo)
        lang_row.addStretch(1)
        lay.addLayout(lang_row)

        # —— v43：LLM 连接（写入服务端 data/llm_config.json，优先于环境变量；Key 仅落盘服务端） ——
        self.llm_group = QGroupBox("🔌 LLM 连接（服务端同机配置；密钥仅保存到 server/data/，界面只显示尾号掩码）")
        form = QFormLayout(self.llm_group)
        self.llm_base = QLineEdit()
        self.llm_base.setPlaceholderText("https://api.example.com/v1")
        self.llm_model = QLineEdit()
        self.llm_model.setPlaceholderText("gpt-4o / qwen-max / …")
        self.llm_key = QLineEdit()
        self.llm_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.llm_key.setPlaceholderText("留空 = 保持已保存的 Key 不变（Leave blank to keep the saved key）")
        self.llm_thinking = QComboBox()          # v47：思考等级（与 Web 发送旁下拉共享同一配置）
        for label, val in (("标准（不发思考参数）", ""), ("低", "low"), ("中", "medium"), ("高", "high")):
            self.llm_thinking.addItem(label, val)
        form.addRow("Base URL", self.llm_base)
        form.addRow("Model", self.llm_model)
        form.addRow("API Key", self.llm_key)
        form.addRow("思考等级", self.llm_thinking)
        llm_btn_row = QHBoxLayout()
        llm_save = QPushButton("💾 保存 LLM 连接")
        llm_save.clicked.connect(self._save_llm)
        self.llm_status = QLabel("")
        self.llm_status.setWordWrap(True)
        llm_btn_row.addWidget(llm_save)
        llm_btn_row.addWidget(self.llm_status, 1)
        form.addRow(llm_btn_row)
        lay.addWidget(self.llm_group)
        self._load_llm_fields()

        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(["书籍", "状态", "片段数", "最近更新", "文件 / 失败原因"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 96)
        self.table.setColumnWidth(2, 64)
        self.table.setColumnWidth(3, 150)
        lay.addWidget(self.table, 1)

        self.rag_line = QLabel("")
        self.rag_line.setWordWrap(True)
        lay.addWidget(self.rag_line)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        row = QHBoxLayout()
        for text, fn in (("📂 批量导入书籍", self._import), ("⏸ 停止检索", self._delete),
                         ("↩ 恢复检索", self._restore), ("💾 备份知识库", self._backup),
                         ("⟳ 恢复最近备份", self._restore_backup), ("关闭", self.reject)):
            btn = QPushButton(text)
            btn.clicked.connect(fn)
            row.addWidget(btn)
        row.addStretch(1)
        lay.addLayout(row)
        self._reload()

    def _say(self, msg: str) -> None:
        self.status.setText(msg)

    def _load_llm_fields(self) -> None:
        cfg = read_llm_config()
        self.llm_base.setText(cfg["base_url"])
        self.llm_model.setText(cfg["model"])
        idx = self.llm_thinking.findData(cfg.get("thinking_level", ""))
        self.llm_thinking.setCurrentIndex(max(0, idx))
        self.llm_key.setText("")   # 明文 Key 不回填；只显示掩码摘要
        if cfg["base_url"] or cfg["model"]:
            self.llm_status.setText(f"已保存：{cfg['base_url'] or '—'} · {cfg['model'] or '—'} · "
                                    f"Key 尾号 {cfg['api_key'][-4:] if cfg['api_key'] else '无'}")
        else:
            self.llm_status.setText("未配置（当前由服务端规则降级稿生成；保存后由所选模型生成草稿）")

    def _on_lang_changed(self, idx: int) -> None:
        lang = self.lang_combo.itemData(idx)
        if lang and lang != getattr(self._window, "app_language", "zh"):
            self._window.set_app_language(lang)   # 保存偏好 + 通知 Web + 原生工具栏/标题重译

    def _save_llm(self) -> None:
        base_url = self.llm_base.text().strip()
        if base_url and not base_url.lower().startswith(("http://", "https://")):
            self.llm_status.setText("Base URL 须以 http:// 或 https:// 开头")
            return
        res = write_llm_config(base_url, self.llm_model.text(), self.llm_key.text(),
                               keep_existing_key=not self.llm_key.text().strip(),
                               thinking_level=self.llm_thinking.currentData() or "")
        self.llm_key.setText("")   # 保存后立即清空明文输入
        if res.get("ok"):
            self.llm_status.setText(f"已保存（重启服务端后生效）：{res['base_url'] or '—'} · {res['model'] or '—'} · "
                                    f"Key 尾号 {res['key_tail'] or '无'}")
        else:
            self.llm_status.setText(f"保存失败：{res.get('reason')}")

    def _reload(self) -> None:
        self.table.setRowCount(0)
        if self._rag is None:
            self._say("本地知识库模块不可用（缺少 server/rag.py），设置面板降级为只读提示。")
            return
        try:
            db = self._rag.load_books()
        except Exception as exc:  # noqa: BLE001
            self._say(f"知识库读取失败：{exc}")
            return
        for b in sorted(db["books"].values(), key=lambda x: x.get("created_at") or ""):
            meta = b.get("meta") or {}
            title = meta.get("title") or b.get("filename") or b["book_id"]
            if meta.get("format_incomplete"):
                title += "（元数据不完整）"
            note = b.get("error") and ("失败原因：" + b["error"]) or (b.get("filename") or "")
            r = self.table.rowCount()
            self.table.insertRow(r)
            for col, val in enumerate([title, KB_STATUS_LABELS.get(b.get("status"), b.get("status")),
                                       str(len(b.get("chunks") or [])),
                                       (b.get("updated_at") or "")[:19].replace("T", " "), note]):
                item = QTableWidgetItem(val)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, b["book_id"])
                self.table.setItem(r, col, item)
        state = self._rag.embedding_status()["state"]   # v50：惰性同步，不再依赖"检索过才更新"
        detail = self._rag.EMBEDDING_STATUS.get("detail", "")
        emb_txt = {"ready": "已加载",
                   "error": "加载失败（检索仅在关键词模式下运行）" + (f"：{detail}" if detail else ""),
                   }.get(state,
                         "未安装（检索仅在关键词模式下运行，引用中如实标注）")
        self.rag_line.setText(
            "检索模式：%s ｜ 本地向量模型：%s ｜ 知识库快照版本：%s" % (
                "混合（关键词+本地向量）" if state == "ready" else "关键词（BM25）",
                emb_txt,
                self._rag.kb_snapshot(db)))

    def _selected_book(self):
        r = self.table.currentRow()
        if r < 0:
            self._say("请先在列表中选择一本书籍。")
            return None
        return self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)

    def _import(self) -> None:
        if self._rag is None:
            self._say("本地知识库模块不可用，无法导入。")
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "批量导入医学书籍", "", "医学书籍 (*.txt *.md *.markdown *.html *.htm *.pdf *.docx *.epub)")
        if not paths:
            return
        ok, failed = 0, []
        for p in paths:
            try:
                with open(p, "rb") as fh:
                    raw = fh.read()
                res = self._rag.import_book(os.path.basename(p), raw, "native-settings")
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{os.path.basename(p)}: {exc}")
                continue
            if res.get("ok"):
                ok += 1
            else:
                failed.append(f"{os.path.basename(p)}: {res.get('reason')}")
        audit("kb_import_native", {"ok": ok, "failed": len(failed)})
        self._say(f"导入完成：成功 {ok} 本" + (f"；失败 {len(failed)} 本（{'；'.join(failed[:3])}）" if failed else "。"))
        self._reload()

    def _mutate(self, method: str, okmsg: str) -> None:
        if self._rag is None:
            self._say("本地知识库模块不可用。")
            return
        book_id = self._selected_book()
        if not book_id:
            return
        res = getattr(self._rag, method)(book_id)
        self._say((f"{okmsg}：{book_id}") if res.get("ok") else f"操作失败：{res.get('reason')}")
        self._reload()

    def _delete(self) -> None:
        self._mutate("soft_delete_book", "已停止检索（数据保留，可随时恢复）")

    def _restore(self) -> None:
        self._mutate("restore_book", "已恢复检索")

    def _backup(self) -> None:
        res = kb_backup_to_disk()
        self._say(f"已备份：{res['path']}" if res.get("ok") else f"备份失败：{res.get('reason')}")

    def _restore_backup(self) -> None:
        res = kb_restore_latest_backup()
        if res.get("ok"):
            self._say(f"已从最近备份恢复（{res.get('books', 0)} 本书）")
            self._reload()
        else:
            self._say(f"恢复失败：{res.get('reason')}")


# —— v43：原生窗口界面语言（与 Web 齿轮共享 ui-prefs.json） ——

NATIVE_I18N = {
    "zh": {
        "title": "肤智（DermAgent）— 皮肤科循证辅助智能体（医生决策支持演示原型）",
        "history": "🕘 历史", "history_tip": "打开/关闭历史对话与跨端续办（左侧抽屉）",
        "symptom": "📋 分析结果", "symptom_tip": "打开/关闭分析结果与医生审核（右侧抽屉）",
        "shot": "📸 截图", "shot_tip": "3D 视图截图（存入报告附件缓存）",
        "case_win": "📋 病例窗口", "case_win_tip": "打开/关闭右侧「皮肤科门诊病历」面板（与对话同屏三栏，不遮挡输入框；Agent 也能按指令自动打开）",
        "settings": "⚙ 设置", "settings_tip": "语言 / LLM 连接 / 知识库 / RAG 运行 / 版本与安全（与 3D 界面最左下角齿轮共享同一套数据）",
    },
    "en": {
        "title": "DermAgent — Dermatology Evidence-Assist Agent (Physician Decision-Support Prototype)",
        "history": "🕘 History", "history_tip": "Open/close conversation history (left dock)",
        "symptom": "📋 Analysis Result", "symptom_tip": "Open/close analysis result and physician review (right dock)",
        "shot": "📸 Screenshot", "shot_tip": "Capture the 3D view (stored in report attachments)",
        "case_win": "📋 Case Record", "case_win_tip": "Toggle the right-side outpatient record panel (three-column layout; the Agent can also open it on request)",
        "settings": "⚙ Settings", "settings_tip": "Language / LLM connection / library / RAG / security (shared with the web gear)",
    },
}


class MainWindow(QMainWindow):
    server_ready = Signal(str)   # v53：后台托管线程 → 主线程状态栏提示
    server_online = Signal()     # v62：本机服务端就绪 → 刷新 web 侧栏（首渲染可能早于 server 可用）
    chat_done = Signal(object)   # v64：LLM 请求线程 → 主线程（结果 dict；错误 {"__error": str, "__retry_payload": dict}）
    restore_done = Signal(object)   # v64-P1：会话恢复线程 → 主线程（最近病例快照；失败/无历史 None）
    case_done = Signal(object)   # v65：病例窗口 record 操作线程 → 主线程（get/save/confirm/regen 结果 dict）
    case_export = Signal(object)   # v65：病例导出线程 → 主线程（{"ok": bool, "markdown"/"error"})

    def __init__(self):
        super().__init__()
        self.setWindowTitle("肤智（DermAgent）— 皮肤科循证辅助智能体（医生决策支持演示原型）")
        scr = QApplication.primaryScreen().availableGeometry()   # 启动窗口≈屏幕 76%×85%（居中自适应）
        self.resize(int(scr.width() * 0.76), int(scr.height() * 0.85))
        self.client = None
        self.agent_mode = "mock"
        self.server_url_cfg = ""   # v54：接入设置改为数据字段（顶栏控件已移入 Web 设置页）
        self.app_language = load_ui_prefs().get("language") or "zh"
        if self.app_language not in NATIVE_I18N:
            self.app_language = "zh"
        self.draft = None
        self.pending_demo = ""
        self.photo_path = ""
        self.screenshot_data = ""
        self.case_id = None
        self._history_details = []
        self._selected_regions = []   # v22：3D 已选位置（结构化，供合并提交与附件标签）
        self._chat_busy = False       # v64：同一时刻只允许一条在途 LLM 请求（防并发竞态）
        self._chat_epoch = 0          # v73-fix：会话纪元——新对话/续办切换后，在途旧请求结果一律丢弃
        self._case_busy = False         # v65：病例窗口 record 操作防重入（保存/确认/重新生成）

        # —— v22 Codex 工作台：中央 3D + 底部输入框（web 内统一），病例/分析/历史为隐藏抽屉 ——
        self.setCentralWidget(self._build_3d_panel())

        self.history_dock = QDockWidget("🕘 历史对话 / 跨端续办", self)
        self.history_dock.setWidget(self._build_history_panel())
        self.history_dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.history_dock.hide()
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.history_dock)

        self.symptom_dock = QDockWidget("📋 分析结果 / 医生审核", self)
        self.symptom_dock.setWidget(self._build_symptom_panel())
        self.symptom_dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.symptom_dock.hide()
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.symptom_dock)

        self._build_toolbar()
        self._load_settings()   # v54：接入设置改读数据字段（默认 server + 本机地址，坏值回落）
        self._update_status("服务端模式（正在检查本机服务端…）" if self.agent_mode == "server"
                            else "mock · 离线演示模式")
        audit("app_start", {"webengine": HAS_WEBENGINE})
        self.server_ready.connect(self._update_status)
        self.server_online.connect(self._refresh_rail_after_server)
        self.chat_done.connect(self._on_chat_done)
        self.restore_done.connect(self._on_restore_done)
        self.case_done.connect(self._on_case_done)      # v65
        self.case_export.connect(self._on_case_export)  # v65
        self._autostart_local_server()   # v53：server 模式打开程序即自动带起本机服务端

    def _refresh_rail_after_server(self):
        """v62：服务端就绪后刷新侧栏「历史病例」分组——bridge 就绪回调可能早于 server 可用，
        首次 renderRailHist 拉列表失败会停在空态；就绪信号到来时主线程重拉一次（幂等，mock 模式 web 函数自行空态）。"""
        if self.web:
            self.web.page().runJavaScript(
                "try { if (typeof renderRailHist === 'function') renderRailHist(); } catch (e) {}")

    # —— v53：本地服务端自动托管 ——

    def _autostart_local_server(self):
        """server 模式 + 本机地址：后台探测/拉起 server/app.py（不阻塞界面，就绪后状态栏提示）。"""
        if self.agent_mode != "server":
            return
        url = os.environ.get("SERVER_URL", self.server_url_cfg).rstrip("/")
        if not url:
            return
        self._update_status("正在启动本地服务端…")

        def _work():
            ok, reason = _ensure_local_server(url)
            if reason == "started":
                self.server_ready.emit("✅ 本地服务端已自动启动（无需手动运行 server/app.py）")
                self.server_online.emit()
            elif reason == "already_running":
                self.server_ready.emit("✅ 已连接本地服务端（检测到服务端已在运行）")
                self.server_online.emit()
            elif reason == "timeout":
                self.server_ready.emit("⚠️ 本地服务端自动启动失败——详见 client/data/server.log；"
                                       "可在设置 → 接入方式 切为「mock · 离线演示」继续")

        threading.Thread(target=_work, daemon=True, name="server-autostart").start()

    def closeEvent(self, event):
        proc = LOCAL_SERVER_PROC
        if proc is not None and proc.poll() is None:   # 只收掉自己 spawn 的服务端
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            audit("server_autostop", {"pid": proc.pid})
        super().closeEvent(event)

    # —— v22：顶部紧凑工具栏（抽屉只由用户主动打开） ——

    def _build_toolbar(self) -> None:
        # 顶部工具栏已按需求移除（功能入口保留在 Web 侧栏）；仍创建 QAction
        # 供 _apply_native_language 等处引用，不加入任何可见栏位。
        def _act(text, tip, fn, checkable=False):
            a = QAction(text, self)
            a.setToolTip(tip)
            a.setCheckable(checkable)
            a.triggered.connect(fn)
            return a

        texts = NATIVE_I18N.get(self.app_language, NATIVE_I18N["zh"])
        self.act_history = _act(texts["history"], texts["history_tip"],
                                lambda: self._toggle_dock(self.history_dock), checkable=True)
        self.act_symptom = _act(texts["symptom"], texts["symptom_tip"],
                                lambda: self._toggle_dock(self.symptom_dock), checkable=True)
        self.act_shot = _act(texts["shot"], texts["shot_tip"], self.on_screenshot_request)
        self.act_case_win = _act(texts.get("case_win", "📋 病例窗口"),
                                 texts.get("case_win_tip", "打开独立病例窗口"), self.open_case_window)
        self.act_settings = _act(texts["settings"], texts["settings_tip"], self._open_settings)

    def set_app_language(self, lang: str, from_web: bool = False) -> None:
        """v43：切换界面语言（双齿轮共享；Web 侧由 uiPrefsSet 已落盘，原生侧在这里保存）。"""
        lang = lang if lang in NATIVE_I18N else "zh"
        self.app_language = lang
        if not from_web:
            save_ui_prefs({"language": lang})
            try:
                self.web.page().runJavaScript(
                    "window.__applyLanguage && window.__applyLanguage(" + json.dumps(json.dumps(lang)) + ");")
            except Exception:  # noqa: BLE001
                pass
        self._apply_native_language()

    def _apply_native_language(self) -> None:
        texts = NATIVE_I18N.get(self.app_language, NATIVE_I18N["zh"])
        self.setWindowTitle(texts["title"])
        for act, key in ((getattr(self, "act_history", None), "history"),
                         (getattr(self, "act_symptom", None), "symptom"),
                         (getattr(self, "act_shot", None), "shot"),
                         (getattr(self, "act_settings", None), "settings")):
            if act is not None:
                act.setText(texts[key])
                act.setToolTip(texts[key + "_tip"])

    def _open_settings(self) -> None:
        """§16.1：原生设置对话框（与 Web 最左下角齿轮共享同一套本地数据）。"""
        dlg = SettingsDialog(self)
        dlg.exec()

    def _toggle_dock(self, dock: QDockWidget) -> None:
        """抽屉只由用户操作开关；不复制模型、不加遮罩，3D 视图始终可交互。"""
        dock.setVisible(not dock.isVisible())

    def on_screenshot_request(self) -> None:
        """用户主动截图：经 web 页 runJavaScript 调 screenshot()，走 bridge 回传。"""
        if self.web:
            self.web.page().runJavaScript("screenshot()")
            self._update_status("已请求 3D 截图（完成后加入报告附件缓存）")

    # —— 左：历史对话记录 ——

    def _build_history_panel(self) -> QWidget:
        box = QGroupBox("🕘 历史对话记录")
        layout = QVBoxLayout(box)
        resume_row = QHBoxLayout()
        self.resume_combo = QComboBox()
        self.resume_combo.addItem("跨端续办：选择病例…", "")
        refresh_btn = QPushButton("⟳")
        refresh_btn.setMaximumWidth(34)
        refresh_btn.clicked.connect(self._refresh_case_list)
        resume_row.addWidget(self.resume_combo)
        resume_row.addWidget(refresh_btn)
        layout.addLayout(resume_row)
        resume_btn = QPushButton("继续该病例（另一端创建的会话）")
        resume_btn.clicked.connect(self.on_resume_case)
        layout.addWidget(resume_btn)
        self.history_list = QListWidget()
        self.history_list.itemClicked.connect(self._show_history_detail)
        layout.addWidget(self.history_list, 2)
        self.history_detail = QTextBrowser()
        self.history_detail.setOpenExternalLinks(True)
        self.history_detail.setMinimumHeight(160)
        layout.addWidget(self.history_detail, 1)
        # v60：历史对话清空——只清本机 Qt 侧记录（_history_details），不触服务端病例数据
        clear_btn = QPushButton("🗑 清空历史记录")
        clear_btn.clicked.connect(self._clear_history)
        layout.addWidget(clear_btn)
        wrap = QWidget()
        wrap.setLayout(layout)
        return wrap

    def _clear_history(self) -> None:
        self.history_list.clear()
        self._history_details = []
        self.history_detail.setHtml("<span style='color:#8a919f'>（已清空）</span>")
        audit("history_cleared")

    def append_history(self, who: str, title: str, detail: str = "") -> None:
        """历史对话记录：左栏时间线条目 + 点击查看详情。"""
        who_label = {"user": "🧑‍⚕️ 医生", "agent": "🤖 肤智", "system": "⚙️ 系统"}.get(who, who)
        ts = datetime.now().strftime("%H:%M:%S")
        item = QListWidgetItem(f"[{ts}] {who_label} · {title}")
        self._history_details.append(detail)
        self.history_list.addItem(item)
        self.history_list.setCurrentItem(item)
        self._show_history_detail(item)
        audit("history", {"who": who, "title": title[:120]})

    def _show_history_detail(self, item: QListWidgetItem):
        row = self.history_list.row(item)
        detail = self._history_details[row] if 0 <= row < len(self._history_details) else ""
        self.history_detail.setHtml(detail or "<span style='color:#8a919f'>（无详情）</span>")

    def _refresh_case_list(self):
        if not isinstance(self.client, ServerAgentClient):
            self.client = ServerAgentClient(self._server_url())
        try:
            cases = self.client.list_cases()
        except Exception as exc:  # noqa: BLE001
            self._update_status(f"⚠️ 服务不可达：{exc}")
            return
        self.resume_combo.clear()
        self.resume_combo.addItem("跨端续办：选择病例…", "")
        for c in cases:
            if c["case_id"] == self.case_id:
                continue
            self.resume_combo.addItem(f"{c['case_id']}（{c.get('label') or '病例'}｜{c.get('workflow_state')}）", c["case_id"])

    def on_resume_case(self):
        case_id = self.resume_combo.currentData()
        if not case_id:
            return
        try:
            self._ensure_client()
            if not isinstance(self.client, ServerAgentClient):
                raise ValueError("跨端续办需 server 模式")
            result = self.client.resume(case_id)
            self.case_id = case_id
            self.append_history("user", f"跨端续办病例 {case_id}")
            self._show_result(result, trigger="resume")
            self.push_case_to_window()   # v65：续办切换病例 → 病例窗口同步新 case_id 内容
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "续办失败", str(exc))

    # —— 中：3D 模型 ——

    def _build_3d_panel(self) -> QWidget:
        """v22 Codex 工作台中央区：只放 3D 视图（单一实例）。输入框、位置标签、
        ＋菜单、附件与合并提交全部在 web 页内完成（web/index.html v24），
        桌面端不再叠加第二套输入区/按钮区/重复信息框。"""
        box = QGroupBox("🧍 3D 人体工作台（点击选点 → 底部输入框位置标签；＋菜单添加附件/工具）")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(4, 4, 4, 4)
        if HAS_WEBENGINE:
            self.web = QWebEngineView()
            # v71：允许 file:// 页面 fetch 本机服务端（大书导入/彻底删除走 HTTP 直连，
            # 绕开 QWebChannel 单消息 >~1MB 丢数据且冻结界面的问题）。
            from PySide6.QtWebEngineCore import QWebEngineSettings
            self.web.settings().setAttribute(
                QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
            self.bridge = WebBridge(self)
            self.channel = QWebChannel()
            self.channel.registerObject("bridge", self.bridge)
            self.web.page().setWebChannel(self.channel)
            index = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
            self.web.load(QUrl.fromLocalFile(index))
            self.web.loadFinished.connect(self._on_web_loaded)   # v64-P1：页面就绪后恢复最近会话
            layout.addWidget(self.web)
        else:
            self.web = None
            self.region_combo = QComboBox()
            self.region_combo.addItem("— 未选择区域 —", "")
            for label in REGION_FALLBACK:
                self.region_combo.addItem(label, label)
            self.region_combo.currentTextChanged.connect(
                lambda text: self.on_region_selected(text) if text in REGION_FALLBACK else None)
            tip = QLabel("（当前环境无 QtWebEngine，已降级为下拉选择）")
            tip.setWordWrap(True)
            layout.addWidget(self.region_combo)
            layout.addWidget(tip)
            self.case_input = QPlainTextEdit()
            self.case_input.setPlaceholderText("在此输入脱敏病例描述（无 WebEngine 降级输入）")
            self.case_input.setMaximumHeight(90)
            layout.addWidget(self.case_input)
            submit_btn = QPushButton("提交给智能体 →")
            submit_btn.clicked.connect(self.on_start)
            layout.addWidget(submit_btn)
        wrap = QWidget()
        wrap.setLayout(layout)
        return wrap

    # —— 右：症状显示区域（提交后运行出来） ——

    def _build_symptom_panel(self) -> QWidget:
        box = QGroupBox("📋 症状显示区域（提交病例后，智能体分析结果在此展示）")
        layout = QVBoxLayout(box)
        self.result_view = QTextBrowser()
        self.result_view.setHtml("<div class='empty'>提交病例后，这里显示症状结构化结果、红旗、鉴别诊断与引用</div>")
        layout.addWidget(self.result_view, 3)
        self.raw_draft_json = QPlainTextEdit()
        self.raw_draft_json.setMaximumHeight(90)
        self.raw_draft_json.setPlaceholderText("草稿 JSON（§6.3 v1.1 契约，供核对/教学）")
        layout.addWidget(self.raw_draft_json, 1)

        review_box = QGroupBox("医生审核（确认 / 修改 / 拒绝）")
        review_layout = QVBoxLayout(review_box)
        self.review_input = QLineEdit()
        self.review_input.setPlaceholderText("确认；或：修改：请补充…；拒绝：理由")
        review_layout.addWidget(self.review_input)
        btn_row = QHBoxLayout()
        confirm_btn = QPushButton("✔ 确认生成辅助报告")
        modify_btn = QPushButton("✎ 修订")
        regen_btn = QPushButton("⟳ 重新生成")
        reject_btn = QPushButton("✖ 拒绝")
        confirm_btn.clicked.connect(lambda: self.on_review("确认"))
        modify_btn.clicked.connect(lambda: self.on_review(f"修改：{self.review_input.text().strip() or '请综合完善'}"))
        regen_btn.clicked.connect(lambda: self.on_review("重新生成：请重新分析本病例", action="regenerate"))
        reject_btn.clicked.connect(lambda: self.on_review(f"拒绝：{self.review_input.text().strip() or '医生拒绝'}"))
        for btn in (confirm_btn, modify_btn, regen_btn, reject_btn):
            btn_row.addWidget(btn)
        review_layout.addLayout(btn_row)
        export_row = QHBoxLayout()
        export_report_btn = QPushButton("导出辅助报告")
        export_draft_btn = QPushButton("导出草稿 JSON")
        export_report_btn.clicked.connect(self.on_export_report)
        export_draft_btn.clicked.connect(self.on_export_draft)
        export_row.addWidget(export_report_btn)
        export_row.addWidget(export_draft_btn)
        review_layout.addLayout(export_row)
        layout.addWidget(review_box)
        wrap = QWidget()
        wrap.setLayout(layout)
        return wrap

    # —— 交互逻辑 ——

    def _server_url(self) -> str:
        """跨端续办用的服务地址：环境变量优先，其次设置页保存的接入地址（v54 前为顶栏输入框）。"""
        return os.environ.get("SERVER_URL", self.server_url_cfg).rstrip("/")

    def _ensure_client(self):
        """v59：client 与接入设置一致时复用实例——不重建。

        发送分支按 win.case_id 判断 chat/start，但请求由 self.client 发出；若这里
        每次无条件重建，会话句柄（client.case_id）清零而 win.case_id 残留，
        chat 延续就会打出 /api/cases/None/chat 之类的脏请求。"""
        if self.agent_mode == "server":
            url = self._server_url()
            if not url:
                raise ValueError("server 模式需填写 DermAgent 服务端地址（server/app.py，如 http://127.0.0.1:8765）；"
                             "注意这不是 LLM 网关地址——大模型连接在「⚙ 设置 → LLM 与模型」里配置，Key 只在服务端")
            if not isinstance(self.client, ServerAgentClient) or self.client.base_url != url.rstrip("/"):
                self.client = ServerAgentClient(url)
                self._save_settings()
        elif not isinstance(self.client, MockAgentClient):
            self.client = MockAgentClient()

    def _load_settings(self):
        """v54：接入设置读唯一入口 _read_agent_settings（坏值/无文件回落 server + 本机地址）。"""
        cfg = _read_agent_settings()
        self.agent_mode = cfg["mode"]
        self.server_url_cfg = cfg["server_url"]

    def _save_settings(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        cfg = {"mode": self.agent_mode, "server_url": self.server_url_cfg.strip()}
        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        audit("settings_saved", cfg)

    def on_region_selected(self, label: str):
        """3D 点击回执：web 端已在底部输入框生成位置标签（chips）。
        Qt 端只记状态与结构化列表（供合并提交），不再改写输入框文字。"""
        if not label or label == "multi":   # v22：初始化空上报不进列表
            return
        if label not in self._selected_regions:
            self._selected_regions.append(label)
        self._update_status(f"3D 已选：{label}（已加入底部输入框位置标签）")

    def on_chat_submitted(self, data: dict):
        """v22：web 统一提交的一条结构化请求 → 历史抽屉 + 智能体管线。"""
        text = (data.get("text") or "").strip()
        regions = data.get("regions", [])
        files = data.get("files", [])
        clinic = data.get("clinic") if isinstance(data.get("clinic"), dict) else None
        summary_parts = []
        if text:
            summary_parts.append(text)
        if regions:
            summary_parts.append("部位：" + "；".join(regions))
        if files:
            summary_parts.append("附件：" + "、".join(f.get("name", "?") for f in files))
        if clinic:
            imaging = clinic.get("imaging") or []
            parts = []
            if clinic.get("hx"):
                parts.append("病史")
            if clinic.get("labs"):
                parts.append(f"检验{len(clinic['labs'])}项")
            if clinic.get("report"):
                parts.append("报告")
            if any(clinic.get(k) for k in ("meds", "allergy", "past", "tx")):
                parts.append("用药/史")
            if imaging:
                ready = sum(1 for m in imaging if m.get("status") == "ready")
                parts.append(f"影像{ready}张")
            if parts:
                summary_parts.append("资料：" + "/".join(parts))
        summary = "；".join(summary_parts) or "（空请求）"
        self.append_history("user", "发送对话（含 3D 选点/附件）" if regions or files else "发送对话", esc(summary))
        # 展开历史抽屉展示回执？——不。用户要求面板只由用户主动打开，仅在状态栏提示。
        self._update_status("已发送（历史对话可在「🕘 历史」抽屉查看）")
        # v59 Agent 化：已有当前病例 → chat 延续上下文（任务识别在服务端）；无病例 → start 新建。
        # 3D 选点是可选上下文：有则并入文本，没有照常发送，绝不阻断。
        # v64：LLM 请求移出 Qt 主线程（旧实现在 slot 内同步 HTTP，网关慢时窗口冻结数十秒，
        # 正是「发送后一直没回复」体感的根因）——先推「正在思考」占位，结果经 chat_done 回主线程。
        if self._chat_busy:
            self._push_chat_error_to_web({"message": "上一条消息仍在处理中，请稍候再发送",
                                          "retry_payload": None})
            self._update_status("⚠️ 上一条消息仍在处理中")
            return
        self._chat_busy = True
        self._last_chat_payload = data   # v64：llm_error（已配置但请求失败）时错误卡重试用
        self._push_thinking_to_web()
        self._update_status("正在思考…（Agent 工作中，通常 1-2 分钟；超时会显示错误并可重试）")
        threading.Thread(target=self._chat_work,
                         args=(summary, text, list(regions), clinic, data, self._chat_epoch),
                         daemon=True, name="chat-request").start()

    def _push_thinking_to_web(self):
        """v64：对话流「正在思考」占位——发送即出现，结果/错误到达时移除，绝不静默等待。"""
        if self.web:
            self.web.page().runJavaScript(
                "window.__pushThinking && window.__pushThinking();")

    def _push_chat_error_to_web(self, body: dict):
        """v64：请求失败进对话流——清晰错误 + 重试按钮（重试原样重发 payload），绝不静默失败。"""
        if self.web:
            self.web.page().runJavaScript(
                "window.__pushChatError && window.__pushChatError("
                + json.dumps(body, ensure_ascii=True) + ");")

    def _chat_work(self, summary: str, text: str, regions: list, clinic: dict | None, data: dict, epoch: int = 0):
        """v64：工作线程内的 LLM 请求。异常一律归一成 __error 回主线程，绝不冒泡冻结 UI。
        v73-fix：请求期间若发生新对话/续办切换（epoch 变化），结果直接作废——
        旧病例的回复绝不允许落进当前会话的对话流。"""
        try:
            self._ensure_client()
            if self.case_id and hasattr(self.client, "chat"):
                chat_text = text
                if regions:
                    chat_text = (chat_text + "\n身体区域：" + "；".join(regions)).strip()
                if not chat_text and clinic:
                    chat_text = summary
                result = self.client.chat(chat_text, clinic=clinic,
                                          regions=regions,
                                          attachments=data.get("files") or [])
            else:
                result = self.client.start(summary, clinic=clinic,
                                           regions=regions,
                                           attachments=data.get("files") or [])
        except Exception as exc:  # noqa: BLE001
            if epoch != self._chat_epoch:
                audit("chat_result_dropped_stale", {"epoch": epoch, "now": self._chat_epoch})
                self.chat_done.emit({"__stale": True})
                return
            self.chat_done.emit({"__error": str(exc), "__retry_payload": data})
            return
        if epoch != self._chat_epoch:   # v73-fix：会话已切换 → 丢弃旧结果，不进对话流
            audit("chat_result_dropped_stale", {"epoch": epoch, "now": self._chat_epoch})
            self.chat_done.emit({"__stale": True})
            return
        self.chat_done.emit(result)

    def _on_chat_done(self, result: dict):
        """v64：主线程渲染——成功走 _show_result；失败推送对话流错误卡（含重试），不再只弹窗。"""
        self._chat_busy = False
        if self.web:   # v74：无论成功/失败/中止，一律恢复发送按钮（生成中它是停止按钮）
            self.web.page().runJavaScript(
                "window.__setChatBusy && window.__setChatBusy(false);")
        if result.get("__stale"):   # v73-fix：会话切换/中止后的旧回复——只解除占用，绝不渲染
            if self.web:
                self.web.page().runJavaScript(
                    "window.removeConvThinking && window.removeConvThinking();")
            self._update_status("已中止生成，可继续提问" if result.get("__aborted")
                                else "已丢弃切换会话前的旧回复")
            return
        if result.get("__error"):
            err = str(result["__error"])
            audit("chat_request_failed", {"error": err[:200]})
            self._push_chat_error_to_web({"message": err, "retry_payload": result.get("__retry_payload")})
            # v69：失败/超时一律留在当前工作区显示真实错误，明确下发 stay（绝不跳空白页）
            self._navigate_to_web({"__error": True}, trigger="chat_error")
            self._update_status("⚠️ 请求失败：已保留输入，可点击对话中的「重试」")
            return
        if self.web:
            self.web.page().runJavaScript(
                "window.removeConvThinking && window.removeConvThinking();")
        self.case_id = getattr(self.client, "case_id", None)
        self._show_result(result, trigger="chat_done")   # v69：渲染 + 服务端导航决策
        self.push_case_to_window()   # v65：对话产出后病例窗口开着则实时刷新（内部判断可见性）
        # “进行诊断”进入病例采集阶段后，病历单就是当前工作窗口，应立即打开。
        # 这不是普通 LLM 回复的自动跳转，普通对话仍停留在对话工作区。
        if result.get("workflow_state") == "case_intake":
            self.open_case_window()
        # v65-S3：Agent 判定「新建一份病例/打开病例窗口」意图（server 路由 open_case 标志）→ 自动开窗
        elif result.get("open_case"):
            self.open_case_window()

    # —— v65：独立病例窗口（皮肤科门诊病历单）——

    def open_case_window(self):
        """v66：打开右侧病例面板（不再用独立 QDialog 弹窗——面板占 workstage 右侧 ~38%，
        对话区自动让位不遮挡；数据即开即拉）。无 WebEngine 时降级提示。"""
        if not self.web:
            QMessageBox.information(self, "提示", "当前环境无 QtWebEngine，病例面板不可用")
            return
        audit("case_panel_open", {"case_id": self.case_id or ""})
        # v73：对话触发的「打开病历单」改为门诊病历单抽屉（旧病例面板不再由对话拉起）
        self.web.page().runJavaScript("window.__openClinicForm && window.__openClinicForm();")

    def push_case_to_window(self, banner: dict | None = None):
        """v66 对话↔病例联动入口：case_id 已建立时后台拉取最新 record 推给 web 面板
        （面板未开由 web 侧忽略）；case_id 为空（新对话）推空态提示。"""
        if not self.web or not self.case_id:
            if self.web:
                payload = {"no_case": True,
                           "message": "当前对话尚未创建病例。在对话中描述病情（如「患者男，35岁，右前臂红斑伴瘙痒7天」）即可生成病历单。"}
                try:
                    body = json.dumps(payload, ensure_ascii=True)
                    self.web.page().runJavaScript(
                        "window.__pushCaseRecord && window.__pushCaseRecord("
                        + json.dumps(body, ensure_ascii=True) + ");")
                except (TypeError, ValueError):
                    pass
            return
        if self._case_busy:
            return
        self._case_busy = True

        def _work():
            try:
                self._ensure_client()
                res = dict(self.client.get_record() or {})
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "error": str(exc)[:200]}
            self._case_busy = False
            self.case_done.emit(res)

        threading.Thread(target=_work, daemon=True, name="case-record-get").start()

    def _case_llm_off(self, rec) -> bool:
        """v66：服务端 llm_meta.ok=False 且 source=none → LLM 未连接（面板红横幅，零伪造）。"""
        lm = (rec or {}).get("llm_meta") or {}
        return lm.get("ok") is False and lm.get("source") == "none"

    def _on_case_done(self, result: dict):
        """v66 主线程：record 操作完成 → __pushCaseRecord(双层 dumps 字符串契约) 推给 web 面板；
        面板横幅文案在 web 侧按 ok/op 组织。"""
        if not self.web:
            return
        payload = {"ok": result.get("ok", False), "op": result.get("__op", "get"),
                   "record": result.get("record") or {},
                   "workflow_state": result.get("workflow_state") or "",
                   "llm_connected": not (self._case_llm_off(result.get("record"))
                                         or result.get("llm_connected") is False),
                   "message": result.get("message") or result.get("error") or ""}
        try:
            body = json.dumps(payload, ensure_ascii=True)
        except (TypeError, ValueError):
            return
        self.web.page().runJavaScript(
            "window.__pushCaseRecord && window.__pushCaseRecord("
            + json.dumps(body, ensure_ascii=True) + ");")
        # v69：病例面板写操作后执行服务端导航决策——
        # 保存 → doctor_modified（保持病例工作区 + 高亮医生修改项）；
        # 确认 → confirmed（解锁导出）；拒绝 → rejected。get 不导航（防刷新时反复切窗）。
        op = str(result.get("__op") or "get")
        if op in ("save", "confirm", "reject") and isinstance(result.get("nav"), dict) \
                and result["nav"]:
            self._navigate_to_web({"nav": result["nav"]}, trigger="case_panel_" + op)

    def _case_record_work(self, op: str, fields=None, field: str = ""):
        """病例面板写操作统一后台线程（v66 仅 regen 走这里；regen 是 LLM 调用可能 30s+）。
        服务端纪律：AI 更新只覆盖非医生确认字段；LLM 未连接 regen 返回提示而非伪造。"""
        if self._case_busy:
            return
        self._case_busy = True

        def _work():
            try:
                self._ensure_client()
                res = dict(self.client.regen_field(field) or {}) if op == "regen" else {}
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "error": str(exc)[:200]}
            self._case_busy = False
            res["__op"] = op
            self.case_done.emit(res)

        threading.Thread(target=_work, daemon=True, name=f"case-record-{op}").start()

    def _case_export_work(self):
        def _work():
            try:
                self._ensure_client()
                md = self.client.export_record_md()
                self.case_export.emit({"ok": True, "markdown": md or "", "case_id": self.case_id or ""})
            except Exception as exc:  # noqa: BLE001
                self.case_export.emit({"ok": False, "error": str(exc)[:200]})
        threading.Thread(target=_work, daemon=True, name="case-record-export").start()

    def _on_case_export(self, result: dict):
        if not result.get("ok"):
            QMessageBox.warning(self, "无法导出", result.get("error", "未知错误")
                                + "\n\n（导出正式病例前需医生确认：病例面板 → 医生确认）")
            return
        suggested = f"dermagent_record_{result.get('case_id') or 'draft'}.md"
        path, _ = QFileDialog.getSaveFileName(self, "导出病历单（Markdown）", suggested, "Markdown (*.md)")
        if not path:
            self._update_status("已取消导出")
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(result["markdown"])
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        audit("case_record_export", {"path": os.path.basename(path), "case_id": result.get("case_id") or ""})
        self._update_status(f"✅ 病历单已导出：{path}")

    def _on_web_loaded(self, ok: bool):
        if getattr(self, "_restore_started", False):
            return
        self._restore_started = True
        if not ok or not self.web:
            return
        if getattr(self, "agent_mode", "") != "server":
            return   # mock/离线演示：无跨重启会话，不恢复
        threading.Thread(target=self._restore_work, daemon=True,
                         name="session-restore").start()

    def _restore_work(self):
        try:
            self._ensure_client()
            cases = self.client.list_cases()
            if not cases:
                self.restore_done.emit(None)
                return
            latest = cases[0].get("case_id")   # list_cases 按 updated_at 倒序，首个=最近
            result = self.client.resume(latest)
            self.restore_done.emit(result)
        except Exception as exc:  # noqa: BLE001
            audit("session_restore_failed", {"error": str(exc)[:160]})
            self.restore_done.emit(None)

    def _on_restore_done(self, result):
        if not result:
            return
        # 竞态防护：用户已在恢复完成前开始新对话（case_id 已被 onNewSession/发送重置）则不打断
        if self.case_id or self._chat_busy:
            audit("session_restore_skipped_busy")
            return
        self.case_id = getattr(self.client, "case_id", None)
        # 顺序：先恢复历史对话（内部清空对话流），再渲染当前病例结果卡（append 到尾部）
        msgs = result.get("messages") or []
        if msgs and self.web:
            try:
                body = json.dumps(msgs, ensure_ascii=True)
            except (TypeError, ValueError):
                body = "[]"
            # v70：先注入消息级产物存档（近 12 条），历史卡各自渲染自己的工作结果按钮
            try:
                ma_body = json.dumps(result.get("msg_artifacts") or {}, ensure_ascii=True)
            except (TypeError, ValueError):
                ma_body = "{}"
            self.web.page().runJavaScript(
                "window.__setMsgArtifacts && window.__setMsgArtifacts(" + json.dumps(ma_body, ensure_ascii=True) + ");")
            self.web.page().runJavaScript(
                "window.__restoreConversation && window.__restoreConversation(" + body + ");")
        self._show_result(result, trigger="restore")   # v69：重启恢复也走同一导航决策
        self._update_status("已恢复最近会话（输入即延续该病例上下文；「新对话」可另起）")
        self.push_case_to_window()   # v65：重启恢复后病例窗口若开着 → 同步最近病例内容

    def on_screenshot(self, data_url: str):
        self.screenshot_data = data_url
        audit("screenshot_taken")
        self._update_status("3D 视图截图已加入报告附件缓存")

    def on_pick_photo(self):
        """v22：桌面端不再单独放照片按钮；附件走 web 输入框 ＋菜单（图片白名单/≤10MB
        校验在 web 端 addChatFile）。保留脱敏副本方法供降级分支与后续复用。"""
        path, _ = QFileDialog.getOpenFileName(self, "选择脱敏/合成皮肤照片", "", "图片 (*.png *.jpg *.jpeg *.webp)")
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        if ext not in ALLOWED_IMG:
            QMessageBox.warning(self, "格式不允许", "仅支持 png/jpg/jpeg/webp")
            return
        if os.path.getsize(path) > MAX_IMG_BYTES:
            QMessageBox.warning(self, "文件过大", "照片不得超过 10MB")
            return
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        img = QImage(path)
        clean_path = os.path.join(UPLOAD_DIR, f"{datetime.now():%Y%m%d_%H%M%S}_clean{ext}")
        if not img.save(clean_path):
            QMessageBox.warning(self, "保存失败", "脱敏副本写入失败")
            return
        self.photo_path = clean_path
        audit("photo_upload", {"src": os.path.basename(path), "clean": os.path.basename(clean_path)})
        self._update_status(f"照片已脱敏（EXIF 清除）保存本地副本 {os.path.basename(clean_path)}")

    def on_load_demo(self, cid: str):
        """演示案例：web 统一提交框架下不再依赖 case_input，直接走 on_start 同路径。"""
        self.pending_demo = cid
        demo = DEMO_CASES[cid]
        audit("demo_loaded", f"案例{cid}")
        self.on_start_with_text(cid)
        self._update_status(f"已选择案例{cid}（{demo['label']}）")

    def on_start_with_text(self, text: str):
        """以给定文本走智能体管线（案例快捷键 / 降级输入共用）。"""
        try:
            self._ensure_client()
            self.append_history("user", "提交病例资料", esc(text))
            result = self.client.start(text)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "智能体服务不可用", f"{exc}\n\n可在设置 → 接入方式 切为「mock · 离线演示」继续。")
            self._update_status("⚠️ 服务不可用（AI/RAG 置灰）；已保留本地资料")
            return
        self.case_id = getattr(self.client, "case_id", None)
        self._show_result(result)

    def on_start(self):
        text = self.case_input.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "提示", "请先在输入框输入病例描述或选择案例快捷键")
            return
        self.on_start_with_text(text)

    def on_review(self, feedback: str, action: str = ""):
        self.append_history("user", "医生审核：" + ("重新生成｜" if action == "regenerate" else "") + feedback[:30])
        try:
            result = self.client.review(feedback, action=action)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "服务不可用", str(exc))
            return
        self._show_result(result)

    def on_supplement(self):
        reply = self.review_input.text().strip()
        if not reply:
            QMessageBox.information(self, "提示", "请先输入补充内容")
            return
        self.append_history("user", "补充：" + reply[:40])
        result = self.client.supplement(reply)
        self._show_result(result)

    # —— 结果 → 历史时间线 ——

    def _cite_ids_html(self, ids) -> str:
        return " ".join(f"<cite style='color:#b45309'>{i}</cite>" for i in (ids or []))

    def _v70_fields(self, result: dict) -> dict:
        """v70：工作结果按钮区字段——available_actions/result_summary 只来自服务端
        artifacts 派生层，前端按清单渲染按钮，绝不根据回复文字猜测该打开哪个窗口。"""
        return {"case_id": result.get("case_id") or self.case_id or "",
                "message_id": result.get("message_id") or "",
                "available_actions": result.get("available_actions") or [],
                "result_summary": result.get("result_summary") or [],
                "artifacts": result.get("artifacts") or {},
                "authenticity": result.get("authenticity") or "",
                "msg_artifacts": result.get("msg_artifacts") or {}}

    def _push_assist_to_web(self, draft: dict, history=None, llm=None, tool_history=None, result=None):
        """阶段5/6：把服务端待审核草稿（可附审核历史）推给 web 辅助面板渲染。
        ensure_ascii=True 把非 ASCII 全部转义为反斜杠 u 序列，作为 JS 字符串字面量无注入风险；
        载荷仅存 web 会话内存，不落 localStorage。
        v58b：必须随载荷携带 llm 真实性元信息（result["llm"]）——前端结果卡只认
        llm.source（llm=绿横幅 / demo=离线演示 / 其余=未连接），缺了会把真实成功结果误标为未连接。"""
        if not self.web or not isinstance(draft, dict):
            return
        body = dict(draft)
        if isinstance(history, list):
            body["review_history"] = history
        body["llm"] = llm if isinstance(llm, dict) else {}
        # v68（计划 §八）：评估结果卡同样携带工具执行记录——输入/输出/来源/确认可追踪
        body["tool_history"] = tool_history or []
        # v70：结果卡底部「工作结果」按钮区数据（服务端派生，医生按需打开）
        if isinstance(result, dict):
            body.update(self._v70_fields(result))
        try:
            payload = json.dumps(body, ensure_ascii=True)
        except (TypeError, ValueError):
            return
        self.web.page().runJavaScript(
            "window.__pushAssist && window.__pushAssist(" + json.dumps(payload, ensure_ascii=True) + ");")

    def _push_doc_to_web(self, result: dict):
        """v59：把文书/医学判断任务产物推给 web 对话区（doc 卡渲染在 main.js appendDocCard）。
        与 _push_assist_to_web 同纪律：llm 真实性元信息必须随载荷携带，前端横幅只认 source。"""
        if not self.web:
            return
        body = {"task_type": result.get("task_type", "case_write"),
                "workflow_state": result.get("workflow_state", ""),
                "doc": result.get("doc"),
                "llm": result.get("llm") if isinstance(result.get("llm"), dict) else {},
                "message": result.get("message", ""),
                "missing": result.get("missing") or [],
                # v68（计划 §八）：doc 产物同样携带工具执行记录——输入/输出/来源/确认可追踪
                "tool_history": result.get("tool_history") or []}
        # v70：doc 卡底部「工作结果」按钮区（服务端派生，医生按需打开）
        body.update(self._v70_fields(result))
        # v64-P4 修复：__pushDoc(jsonStr) 契约要求 JSON 字符串（内部 JSON.parse），
        # 之前传对象字面量 → JSON.parse("[object Object]") 静默抛错 → doc 卡永远不渲染。
        # 与 _push_assist_to_web 同一模式：先 dumps 成字符串，再 dumps 一层做 JS 字符串字面量。
        try:
            payload = json.dumps(body, ensure_ascii=True)
        except (TypeError, ValueError):
            return
        self.web.page().runJavaScript(
            "window.__pushDoc && window.__pushDoc(" + json.dumps(payload, ensure_ascii=True) + ");")

    def _push_flow_to_web(self, result: dict):
        """v52：把工作流状态推给 web 状态行/指引弹窗（mock/server 两模式同一契约）。
        ask_more → web 自动弹出「下一步指引」弹窗；review → 自动展开辅助判断面板。
        v58：透传 llm 真实性元信息与接入模式，前端据此管理 llm_connected/llm_unavailable/offline_demo 状态。"""
        if not self.web:
            return
        payload = {"action": result.get("action", ""),
                   "workflow_state": result.get("workflow_state", ""),
                   "question": result.get("question", ""),
                   "message": result.get("message", ""),
                   "missing": result.get("missing") or [],
                   "review_status": result.get("review_status", ""),
                   "llm": result.get("llm") or {},
                   "mode": getattr(self.client, "mode", "")}
        try:
            body = json.dumps(payload, ensure_ascii=True)
        except (TypeError, ValueError):
            return
        self.web.page().runJavaScript(
            "window.__pushFlow && window.__pushFlow(" + json.dumps(body, ensure_ascii=True) + ");")

    def _show_result(self, result: dict, trigger: str = "show_result"):
        """v69：渲染 + 导航一体——先如实渲染本轮产物（卡片/面板/历史），
        再执行服务端权威导航决策。这是「Agent 已回复但界面不跳转」的修复入口：
        渲染与导航分离，保证两个关注点各自可测，也不会因某分支提前 return 而漏掉导航。"""
        self._render_result(result)
        self._navigate_to_web(result, trigger=trigger)

    def _navigate_to_web(self, result: dict, trigger: str = "chat_done"):
        """v69：把导航决策下发给 web 侧 window.__nav 执行（工作区切换/聚焦/滚动/高亮）。

        Python 侧只做「如实下发」，不做界面推断：决策来自服务端 nav 模块（worker 场景无服务端
        决策时，明确下发 stay —— 空响应/超时/失败一律留在当前工作区，绝不跳空白页）。
        导航日志（导航前/后工作区、是否成功聚焦与滚动）由 web 侧补齐后回传 audit。"""
        if not self.web:
            return
        nav = result.get("nav") if isinstance(result.get("nav"), dict) else {}
        if not nav:
            nav = {"nav_state": "error" if result.get("__error") else "idle",
                   "next_action": "stay", "workspace": None,
                   "reason": "无服务端导航决策：保持当前工作区（不跳转）"}
        payload = dict(nav)
        payload["trigger"] = trigger
        # v69：单调递增序号——web 侧据此丢弃乱序到达的旧决策（否则会跳回旧任务/旧病例）
        seq = int(getattr(self, "_nav_seq", 0) or 0) + 1
        self._nav_seq = seq
        payload["seq"] = seq
        if not payload.get("case_id"):
            payload["case_id"] = self.case_id or ""
        try:
            body = json.dumps(payload, ensure_ascii=True)
        except (TypeError, ValueError):
            return
        audit("navigate", {"trigger": trigger, "nav_state": payload.get("nav_state"),
                           "next_action": payload.get("next_action"),
                           "workspace": payload.get("workspace"),
                           "case_id": payload.get("case_id")})
        self.web.page().runJavaScript(
            "window.__nav && window.__nav(" + json.dumps(body, ensure_ascii=True) + ");")

    def _render_result(self, result: dict):
        self._push_flow_to_web(result)   # v52：每次服务端交互后同步流程指引
        # v68（计划 §十一.5）：上下文压缩提示——自动摘要生成后在对话流如实告知医生
        if result.get("context_compressed") and self.web:
            self.web.page().runJavaScript(
                "window.__pushAgentReply && window.__pushAgentReply("
                + json.dumps({"content": "🧹 上下文已压缩：已生成本病例结构化摘要"
                                       "（含已确认事实、待确认项、过敏史与危险信号、医生修改记录——"
                                       "这些内容永不裁剪），较早的普通对话将按摘要参与后续分析。",
                              "generated_by": "agent-cmd", "llm": {"source": "agent", "model": ""},
                              "workflow_state": result.get("workflow_state", ""), "tool_history": []},
                             ensure_ascii=True) + ");")
        action = result.get("action")
        wf = result.get("workflow_state", "")
        if action == "reply":
            # v64：Agent 自然语言回复（闲聊/澄清）——进对话流渲染，不动病例结果面板。
            # 真实性随 llm 元信息透传（source=llm 为真实回复；agent=本地澄清清单，非 AI 生成）。
            reply = result.get("reply") or {}
            llm = result.get("llm") or {}
            content = str(reply.get("content") or "")
            self.append_history("agent", "✦ Agent 回复", esc(content)[:800])
            if self.web:
                reply_body = {"content": content,
                              "generated_by": reply.get("generated_by") or "",
                              "llm": {"source": llm.get("source"), "model": llm.get("model")},
                              "workflow_state": result.get("workflow_state", ""),
                              "reply_card": reply.get("card"),   # v68：图片分析特征勾选卡等结构化卡片
                              "tool_history": result.get("tool_history") or []}
                # v70：回复卡底部「工作结果」按钮区——只按服务端 available_actions 渲染
                reply_body.update(self._v70_fields(result))
                if result.get("ui_action"):   # v73：Agent 改界面类设置（如语言）→ 随卡下发执行
                    reply_body["ui_action"] = result.get("ui_action")
                self.web.page().runJavaScript(
                    "window.__pushAgentReply && window.__pushAgentReply("
                    + json.dumps(reply_body, ensure_ascii=True) + ");")
            if result.get("workflow_state") == "planning":
                self._update_status("已说明理解与准备动作，等待医生确认或补充")
            elif result.get("workflow_state") == "chat_reply":
                self._update_status("Agent 已回复")
            else:
                self._update_status("已列出需要的信息，等待医生补充")
            return
        if action == "llm_error":
            # v64：已配置但请求失败/超时（网关抖动常见）——对话流错误卡 + 重试按钮，
            # 重试原样重发上一次 payload；零医疗内容，绝不伪装。
            if self.web:
                self.web.page().runJavaScript(
                    "window.__pushChatError && window.__pushChatError("
                    + json.dumps({"message": result.get("message") or "LLM 请求失败，请重试",
                                  "unavailable": False,
                                  "retry_payload": getattr(self, "_last_chat_payload", None)},
                                 ensure_ascii=True) + ");")
            llm = result.get("llm") or {}
            self.append_history("agent", "⚠️ LLM 请求失败（可重试）",
                                esc(result.get("message", "")) +
                                (f"<p class='meta'>失败原因：{esc(str(llm.get('error', ''))[:160])}</p>" if llm.get("error") else ""))
            self.result_view.setHtml(
                "<div class='flag'>⚠️ LLM 请求失败（网关不可达或超时）</div>"
                "<p>%s</p><p style='color:#8a919f'>LLM 连接已配置；请点击对话中的「重试」再次发送，"
                "或稍后在设置页检查网关状态。</p>" % esc(result.get("message", "")))
            self._update_status("LLM 请求失败：已保留输入，可点击对话中的「重试」")
            return
        if action == "llm_unavailable":
            # v64：服务端判定的未连接（前端 probe 通过但请求时不可用）也要进对话流——
            # 只说「当前未连接 LLM，无法回复」，零医疗内容；不给重试按钮（须先在设置页配置连接）。
            if self.web:
                self.web.page().runJavaScript(
                    "window.__pushChatError && window.__pushChatError("
                    + json.dumps({"message": result.get("message") or "当前未连接 LLM，无法回复",
                                  "unavailable": True, "retry_payload": None},
                                 ensure_ascii=True) + ");")
            # v58：LLM 未配置/请求失败/返回非法——服务端不生成任何医疗内容，这里如实呈现，绝不伪装
            llm = result.get("llm") or {}
            self.append_history("agent", "⛔ LLM 不可用（不生成医疗内容）",
                                esc(result.get("message", "")) +
                                (f"<p class='meta'>失败原因：{esc(str(llm.get('error', ''))[:160])}</p>" if llm.get("error") else ""))
            self.result_view.setHtml(
                "<div class='flag'>⛔ 当前未连接 LLM，暂不能生成真实 AI 医疗建议</div>"
                "<p>%s</p><p style='color:#8a919f'>可在 Web 界面「检查连接 / 打开模型设置 / 切换离线演示」。</p>"
                % esc(result.get("message", "")))
            self._update_status("当前未连接 LLM（未生成任何医疗内容）")
        elif action == "ask_more":
            self.append_history("agent", f"📋 资料不足（workflow_state={wf}）", esc(result.get("question", "")))
            self.result_view.setHtml(
                "<div class='flag'>📋 资料不足（workflow_state=%s）</div><p>%s</p>"
                "<p style='color:#8a919f'>请在底部输入框补充描述后重新发送。</p>"
                % (wf or "?", esc(result.get("question", ""))))
            self._update_status("待补充资料")
        elif action == "doc":
            # v59：文书/医学判断任务产物（现病史/摘要/鉴别/宣教/导出等，task_type 区分）。
            # 渲染守卫与 v58 一致：真实来源只认 llm.source（llm/template/none），缺失一律按未连接处理。
            doc = result.get("doc") or {}
            llm = result.get("llm") or {}
            task_type = result.get("task_type") or (doc.get("task_type") or "case_write")
            title = TASK_TYPE_LABELS.get(task_type, "文书整理")
            if not doc and result.get("message"):
                self.append_history("agent", f"ℹ️ {title}", esc(result.get("message", "")))
                self._update_status(result.get("message", ""))
                return
            self.append_history("agent", f"📝 {title}（来源：{llm.get('source', '未知')}）",
                                "<pre>" + esc(str(doc.get("content", ""))) + "</pre>"
                                + (f"<p class='meta'>{esc(str(doc.get('disclaimer', '')))}</p>" if doc.get("disclaimer") else ""))
            self._update_status(f"{title}已生成（来源：{llm.get('source', '未知')}）；医疗判断须由医生完成")
            self._push_doc_to_web(result)
        elif action == "review":
            self.draft = result.get("draft") or self.draft
            d = self.draft
            if d is None:
                # 病例尚无草稿（如病历填写阶段恢复会话）：如实渲染引导信息，不伪造草稿面板
                self.append_history("agent", "ℹ️ 暂无可展示的草稿",
                                    esc(result.get("question") or result.get("message")
                                        or "当前阶段还没有草稿。"))
                self._update_status(result.get("question") or "等待医生输入")
                return
            self.raw_draft_json.setPlainText(json.dumps(d, ensure_ascii=False, indent=2))
            red = d.get("red_flag_escalation", {})
            flags = "".join(f"<p class='flag'>🚩 {r}</p>" for r in red.get("reasons", []))
            blocked = (f"<p class='flag'>⛔ workflow_state={d.get('workflow_state')}｜{d.get('blocking_reason') or ''}</p>"
                       if red.get("is_blocking") else "")
            no_ev = "<p class='flag'>ℹ️ 知识库未覆盖：不给鉴别诊断（不足以判断），不编造引用</p>" if not d.get("citations") else ""
            dx_rows = "".join(
                "<tr><td>" + c.get("name", "") + "</td><td>" + c.get("confidence", "") + "</td><td>" +
                "；".join(e.get("statement", "") for e in c.get("supporting_evidence", [])) + "</td></tr>"
                for c in d.get("suspected_conditions", [])) or "<tr><td colspan=3>知识库未覆盖（不编造）</td></tr>"
            status_label = STATUS_LABEL
            assess = "".join(
                "<li>" + x.get("symptom", "") + "｜状态：" + status_label.get(x.get("status"), "") +
                "｜依据：" + x.get("basis", "") + " " + self._cite_ids_html(x.get("citation_ids", [])) + "</li>"
                for x in d.get("symptom_assessment", []))
            meds = "".join(
                "<li>" + x.get("generic_name", "") + "：核对【" + "、".join(x.get("items_to_verify", [])) + "】</li>"
                for x in d.get("medication_checks", [])) or "<li>无用药信息</li>"
            tr = "".join("<li>" + x.get("approach", "治疗方向待核实") + "：" +
                          (x.get("indication_or_goal") or "目标待核实") + "（依据：" +
                          (x.get("basis") or "待核实") + "）</li>"
                          for x in d.get("treatment_reference", [])) or "<li>无治疗方向参考</li>"
            med_recs = "".join("<li>" + x.get("generic_name", "药物名称待核实") + "：" +
                               (x.get("purpose") or "用途待核实") + "；参考使用时长：" +
                               (x.get("reference_duration") or "待核实") + "；需医生确认</li>"
                               for x in d.get("medication_recommendations", [])) or "<li>无用药参考</li>"
            cites = "".join(
                "<li><b style='color:#b45309'>" + c.get("citation_id", "") + "</b>｜" + c.get("document_title", "") +
                "（" + c.get("document_version", "") + "）§" + c.get("page_or_section", "") +
                "<br><span style='color:#57606a'>「" + c.get("quoted_text", "") + "」</span></li>"
                for c in d.get("citations", [])) or "<li>知识库未覆盖</li>"
            detail = (
                f"<p><span class='pill'>workflow: {d.get('workflow_state')}</span>"
                f"<span class='pill'>review: {d.get('review_status')}</span></p>"
                f"{flags}{blocked}{no_ev}"
                f"<p><b>病例摘要</b>：{d.get('case_summary', {}).get('symptom_summary', '')}</p>"
                f"<table width='100%' cellpadding=3><tr style='color:#8a919f'><td>鉴别诊断</td><td>置信度</td><td>依据</td></tr>{dx_rows}</table>"
                f"<p><b>症状评估（unknown/未询问 ≠ 没有）</b></p><ul>{assess}</ul>"
                f"<p><b>治疗方向（非处方，待医生核定）</b></p><ul>{tr}</ul>"
                f"<p><b>用药参考（药名与参考时长均须医生确认，不得直接执行）</b></p><ul>{med_recs}</ul>"
                f"<p><b>用药安全核对</b></p><ul>{meds}</ul>"
                f"<p><b>引用</b></p><ul>{cites}</ul>"
                f"<p class='meta'>模型 {d.get('model_version')}｜生成方式 {d.get('generated_by')}｜"
                f"安全策略 {d.get('safety_policy_version')}｜{d.get('disclaimer')}</p>")
            covered = d.get("case_summary", {}).get("data_completeness", "-")
            self.append_history("agent",
                                f"📄 待审核草稿（workflow_state={d.get('workflow_state')}｜完备度 {covered}）", detail)
            last_err = getattr(self.client, "last_error", "")
            self._update_status("草稿待医生审核（workflow_state=" + str(d.get("workflow_state")) + "）" +
                                (f"（⚠️ 降级：{last_err[:80]}）" if last_err else ""))
            self._push_assist_to_web(self.draft, history=result.get("review_history"),
                                     llm=result.get("llm"),
                                     tool_history=result.get("tool_history") or [],
                                     result=result)   # v68 工具追踪 + v70 按钮区
        elif action == "report":
            self.draft = result.get("draft") or (self.client.state.get("draft") if hasattr(self.client, "state") else self.draft)
            report = result["report"]
            if self.screenshot_data:
                report += "\n\n## 附：3D 定位截图\n\n![3D](screenshot.png)\n"
                self._pending_screenshot_in_report = True
            self.append_history("agent", "✅ 辅助报告已生成（医生已确认）", f"<pre>{esc(report)}</pre>")
            self._last_report = report
            self._update_status("✅ 待签署草稿已生成——可导出 Markdown（最终签署须在授权目标系统完成）")
            if self.draft:
                self._push_assist_to_web(self.draft, history=result.get("review_history"),
                                         llm=result.get("llm"), result=result)   # 审核状态联动 web
        elif action == "closed":
            self.append_history("agent", "❌ 医生已拒绝，不生成报告", esc(result.get("message", "")))
            self._update_status("病例已关闭（医生拒绝）")
            if self.draft:
                self._push_assist_to_web(self.draft, history=result.get("review_history"),
                                         llm=result.get("llm"), result=result)

    def on_export_report(self):
        report = getattr(self, "_last_report", "")
        if not report:
            QMessageBox.information(self, "提示", "尚无已确认的辅助报告（需先「确认」）")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出辅助报告", "dermagent_report.md", "Markdown (*.md)")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(report)
            if getattr(self, "_pending_screenshot_in_report", False) and self.screenshot_data:
                img_path = os.path.join(os.path.dirname(path), "screenshot.png")
                self._save_data_url(self.screenshot_data, img_path)
            audit("report_exported", path)
            self._update_status(f"报告已导出：{path}")

    def _save_data_url(self, data_url: str, path: str):
        import base64
        raw = base64.b64decode(data_url.split(",", 1)[1])
        with open(path, "wb") as fh:
            fh.write(raw)

    def on_export_draft(self):
        if not self.draft:
            QMessageBox.information(self, "提示", "尚无草稿")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出草稿 JSON", "dermagent_draft.json", "JSON (*.json)")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self.draft, ensure_ascii=False, indent=2))
            audit("draft_exported", path)

    def _update_status(self, text: str):
        self.statusBar().showMessage(
            f"[{DISCLAIMER_SHORT}]  接入：{self.agent_mode}｜知识快照 synthetic-kb@2026-09-04｜{text}")


def _apply_light_theme(app: QApplication) -> None:
    """全局浅色主题（与 web/index.html 同 tokens：白底 + 浅灰侧栏 + 蓝色强调）。"""
    app.setStyle("Fusion")
    p = QPalette()
    p.setColor(QPalette.Window, QColor(0xf7f8fa))
    p.setColor(QPalette.WindowText, QColor(0x1f2329))
    p.setColor(QPalette.Base, QColor(0xffffff))
    p.setColor(QPalette.AlternateBase, QColor(0xf7f8fa))
    p.setColor(QPalette.Text, QColor(0x1f2329))
    p.setColor(QPalette.PlaceholderText, QColor(0x8a919f))
    p.setColor(QPalette.Button, QColor(0xffffff))
    p.setColor(QPalette.ButtonText, QColor(0x1f2329))
    p.setColor(QPalette.Highlight, QColor(0x1f6feb))
    p.setColor(QPalette.HighlightedText, QColor(0xffffff))
    p.setColor(QPalette.ToolTipBase, QColor(0xffffff))
    p.setColor(QPalette.ToolTipText, QColor(0x1f2329))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor(0xb8bec9))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(0xb8bec9))
    app.setPalette(p)
    app.setStyleSheet("""
      QToolBar { background: #f7f8fa; border-bottom: 1px solid #e8eaed; padding: 4px 6px; spacing: 2px; }
      QToolBar QToolButton { background: transparent; color: #4b5563; border: none;
                             border-radius: 8px; padding: 5px 10px; }
      QToolBar QToolButton:hover { background: #eceef1; color: #1f2329; }
      QToolBar QToolButton:checked { background: #e8f1ff; color: #1f6feb; }
      QDockWidget::title { background: #f7f8fa; border-bottom: 1px solid #e8eaed;
                           padding: 6px 10px; color: #1f2329; }
      QGroupBox { border: 1px solid #e8eaed; border-radius: 8px; margin-top: 10px;
                  color: #1f2329; font-weight: bold; background: #ffffff; }
      QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #b45309; }
      QPushButton { background: #ffffff; color: #1f2329; border: 1px solid #d9dce1;
                    border-radius: 6px; padding: 5px 10px; }
      QPushButton:hover { background: #f3f4f6; border-color: #c9ddf7; }
      QLineEdit, QTextEdit, QPlainTextEdit, QComboBox {
        background: #ffffff; color: #1f2329; border: 1px solid #e0e3e8; border-radius: 6px;
        selection-background-color: #cfe0f7; }
      QComboBox QAbstractItemView { background: #ffffff; color: #1f2329; }
      QListWidget { background: #ffffff; color: #1f2329; border: 1px solid #e8eaed; }
      QTextBrowser { background: #ffffff; color: #1f2329; border: 1px solid #e8eaed; }
      QStatusBar { background: #f7f8fa; color: #8a919f; border-top: 1px solid #e8eaed; }
      QSplitter::handle { background: #e8eaed; width: 2px; }
      QLabel { color: #1f2329; }
    """)


def main() -> int:
    os.environ.setdefault("QT_LOGGING_RULES", "qt.webenginecontext.*=false")
    app = QApplication(sys.argv)
    app.setApplicationName("肤智 DermAgent")
    _apply_light_theme(app)
    window = MainWindow()
    window.show()

    def _warm_embedding():               # v50：后台预热向量模型，状态行/检索就绪前不阻塞界面
        rag = load_rag_module()
        if rag is not None:
            try:
                rag.embedding_status()
            except Exception:  # noqa: BLE001 — 预热失败不影响启动，状态如实降级
                pass
    threading.Thread(target=_warm_embedding, daemon=True, name="emb-warmup").start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
