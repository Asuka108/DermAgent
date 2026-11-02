/**
 * 身份信息替换：与 server/memory/redact.py 同口径。
 *
 * 进入 LLM / 情景文本 / 日志前必须调用；只替换直接身份字段，不触碰剂量与病程数字。
 */

const ID_RE = /(?<!\d)\d{17}[\dXx](?!\d)/g;
const PHONE_RE = /(?<!\d)1[3-9]\d{9}(?!\d)/g;
const LABELED_NAME_RE = /((?:患者)?姓名)\s*[:：]\s*([\u4e00-\u9fa5A-Za-z·]{1,20})/g;
const CLINIC_NO_RE = /(门诊号|病历号|住院号)\s*[:：]?\s*([A-Za-z0-9-]{3,24})/g;

export function redactIdentity(text: string, names: string[] = []): string {
  let out = String(text ?? "");
  if (!out) return out;
  out = out.replace(ID_RE, "[身份证]").replace(PHONE_RE, "[手机号]");
  out = out.replace(LABELED_NAME_RE, (_m, label: string) => `${label}：[患者]`);
  out = out.replace(CLINIC_NO_RE, (_m, label: string) => `${label}：[门诊号]`);
  const unique = [...new Set(names.map((n) => String(n ?? "").trim()).filter(Boolean))].sort(
    (a, b) => b.length - a.length
  );
  for (const name of unique) {
    if (name.length >= 2) out = out.split(name).join("[患者]");
  }
  return out;
}

export function hasIdentity(text: string, names: string[] = []): boolean {
  if (!text) return false;
  if (ID_RE.test(text) || PHONE_RE.test(text) || LABELED_NAME_RE.test(text) || CLINIC_NO_RE.test(text)) {
    // 带 g 的正则会保留 lastIndex，重置后再返回，避免下次误判
    ID_RE.lastIndex = PHONE_RE.lastIndex = LABELED_NAME_RE.lastIndex = CLINIC_NO_RE.lastIndex = 0;
    return true;
  }
  return names.some((n) => String(n ?? "").trim().length >= 2 && text.includes(n));
}
