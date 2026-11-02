# -*- coding: utf-8 -*-
"""身份信息识别与替换：只保留临床事实，姓名/证件/电话/门诊号一律占位。

边界（计划 §9）：身份字段只允许存在 MySQL patients 表与病例窗口展示；
进入 LLM、Milvus、审计与日志的文本必须先过本模块。
"""
import re

ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
LABELED_NAME_RE = re.compile(r"((?:患者)?姓名)\s*[:：]\s*([\u4e00-\u9fa5A-Za-z·]{1,20})")
CLINIC_NO_RE = re.compile(r"(门诊号|病历号|住院号)\s*[:：]?\s*([A-Za-z0-9\-]{3,24})")

_PLACEHOLDER_ID = "[身份证]"
_PLACEHOLDER_PHONE = "[手机号]"
_PLACEHOLDER_NAME = "[患者]"


def identity_values(patient: dict | None) -> list:
    """从患者档案收集需要抹除的字面值（姓名/电话/证件）。"""
    p = patient if isinstance(patient, dict) else {}
    out = []
    for key in ("display_name", "name", "phone", "id_no", "id_number"):
        val = str(p.get(key) or "").strip()
        if val and val not in out:
            out.append(val)
    return out


def redact_identity(text: str, names=None) -> str:
    """替换直接身份字段；不触碰药物剂量、病程等临床数字。"""
    out = str(text or "")
    if not out:
        return out
    out = ID_RE.sub(_PLACEHOLDER_ID, out)
    out = PHONE_RE.sub(_PLACEHOLDER_PHONE, out)
    out = LABELED_NAME_RE.sub(lambda m: f"{m.group(1)}：{_PLACEHOLDER_NAME}", out)
    out = CLINIC_NO_RE.sub(lambda m: f"{m.group(1)}：[门诊号]", out)
    for name in sorted({str(n).strip() for n in (names or []) if str(n or "").strip()},
                       key=len, reverse=True):
        if len(name) >= 2:
            out = out.replace(name, _PLACEHOLDER_NAME)
    return out


def has_identity(text: str, names=None) -> bool:
    """测试与写入校验用：文本是否仍含直接身份特征。"""
    if not text:
        return False
    if ID_RE.search(text) or PHONE_RE.search(text):
        return True
    if LABELED_NAME_RE.search(text) or CLINIC_NO_RE.search(text):
        return True
    return any(len(str(n)) >= 2 and str(n) in text for n in (names or []))
