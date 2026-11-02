/**
 * HTTP Request Tool
 *
 * Generic HTTP client — built with @langchain/core/tools `tool()` helper
 * so it's fully compatible with deepagents' tool system.
 *
 * 安全默认（防 SSRF / OOM，LLM 给的 url 与响应体均为不可信输入）：
 *  - 仅允许 http/https；
 *  - 默认拒绝解析到私有/内网/loopback/链路本地地址（RFC1918、127/8、169.254/16、
 *    IPv6 ULA/link-local 等）——阻断云元数据端点（169.254.169.254）窃取 IAM 凭证、
 *    内网探测；需要访问内网时用 `createHttpRequestTool({ allowPrivateNetwork: true })`；
 *  - 重定向手动 follow，最多 3 跳，**每跳重新校验目标**（防 302 引入内网）；
 *  - 响应体流式读取并设字节上限（200KB），超限即停 + cancel——防巨型响应先整读入内存 OOM
 *    （旧实现 `await response.text()` 先全读再 slice，2GB 响应会 OOM）。
 *
 * 局限：DNS 在请求前解析一次校验，不防 TTL=0 的 DNS rebinding（需 connect 时校验，超出模板范围）。
 */

import { tool } from "@langchain/core/tools";
import { z } from "zod";
import { lookup } from "node:dns/promises";

/** OOM 字节上限：流式读取累计到此后即停 + cancel reader。 */
const MAX_RESPONSE_BYTES = 200_000;
/** 返回给 LLM 的字符截断（上下文保护，独立于上面的 OOM 字节上限）。 */
const MAX_RETURN_CHARS = 10_000;

export interface HttpRequestToolOptions {
  /**
   * 允许请求私有/内网/loopback 地址（默认 false 拦截，防 SSRF）。
   * 仅在确需访问内网服务时显式开启。
   */
  allowPrivateNetwork?: boolean;
}

/** 点分 IPv4 → 无符号 32 位整数；非法格式返回 null。 */
function ipv4ToInt(ip: string): number | null {
  const m = ip.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (!m) return null;
  const parts = m.slice(1).map(Number);
  if (parts.some((p) => Number.isNaN(p) || p < 0 || p > 255)) return null;
  return (((parts[0]! << 24) | (parts[1]! << 16) | (parts[2]! << 8) | parts[3]!) >>> 0);
}

/** IPv4 私有/保留段（RFC1918 + loopback + link-local + CGNAT + multicast + reserved + 0/8）。 */
const PRIVATE_IPV4_RANGES: Array<{ start: number; end: number }> = [
  { start: 0x00000000, end: 0x00ffffff }, // 0.0.0.0/8
  { start: 0x0a000000, end: 0x0fffffff }, // 10.0.0.0/8
  { start: 0x64400000, end: 0x647fffff }, // 100.64.0.0/10 (CGNAT)
  { start: 0x7f000000, end: 0x7fffffff }, // 127.0.0.0/8 (loopback)
  { start: 0xa9fe0000, end: 0xa9feffff }, // 169.254.0.0/16 (link-local / 元数据)
  { start: 0xac100000, end: 0xac1fffff }, // 172.16.0.0/12
  { start: 0xc0a80000, end: 0xc0a8ffff }, // 192.168.0.0/16
  { start: 0xe0000000, end: 0xefffffff }, // 224.0.0.0/4 (multicast)
  { start: 0xf0000000, end: 0xffffffff }, // 240.0.0.0/4 (reserved)
];

function isPrivateIpv4(ip: string): boolean {
  const n = ipv4ToInt(ip);
  if (n === null) return false;
  return PRIVATE_IPV4_RANGES.some((r) => n >= r.start && n <= r.end);
}

/** 判定一个 IP 地址是否私有/loopback/链路本地（IPv4 + IPv6，含 IPv4-mapped）。 */
function isPrivateAddress(ip: string): boolean {
  if (ip.includes(":")) {
    // IPv6（含 IPv4-mapped ::ffff:a.b.c.d → 剥前缀后按 IPv4 判）。
    const v = ip.toLowerCase().replace(/^::ffff:/, "");
    if (v.includes(":")) {
      return (
        v === "::1" || // loopback
        v === "::" || // unspecified
        v.startsWith("fe80") || // link-local
        v.startsWith("fec0") || // site-local（deprecated fec0::/10，部署中仍存在）
        v.startsWith("fc") || // ULA fc00::/7
        v.startsWith("fd")
      );
    }
    return isPrivateIpv4(v);
  }
  return isPrivateIpv4(ip);
}

/** 解析 hostname 为 IP 地址列表（IP 字面量原样返回，域名走 DNS）。解析失败抛错。 */
async function resolveHosts(hostname: string): Promise<string[]> {
  // IP 字面量（IPv4 点分 / 含冒号的 IPv6）直接返回，避免依赖 DNS。
  if (/^[\d.]+$/.test(hostname) || hostname.includes(":")) return [hostname];
  try {
    const records = await lookup(hostname, { all: true });
    return records.map((r) => r.address);
  } catch {
    throw new Error(`cannot resolve host: ${hostname}`);
  }
}

/** 校验单个 URL：仅 http/https；非 allowPrivateNetwork 时拒绝解析到私有地址的目标。 */
async function assertSafeUrl(rawUrl: string, allowPrivateNetwork: boolean): Promise<void> {
  let url: URL;
  try {
    url = new URL(rawUrl);
  } catch {
    throw new Error(`invalid url: ${rawUrl}`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error(`unsupported protocol: ${url.protocol} (only http/https allowed)`);
  }
  if (!allowPrivateNetwork) {
    const addrs = await resolveHosts(url.hostname);
    for (const a of addrs) {
      if (isPrivateAddress(a)) {
        throw new Error(
          `blocked: ${url.hostname} resolves to private/loopback address ${a} (set allowPrivateNetwork to override)`
        );
      }
    }
  }
}

/**
 * fetch 包装：redirect 手动 follow（最多 maxRedirects 跳），**每跳重新 assertSafeUrl**，
 * 防「公网域名 302 跳转到内网」绕过初始校验。method/body 在重定向时保持不变（简化处理）。
 */
async function safeFetch(
  rawUrl: string,
  init: RequestInit & { signal?: AbortSignal },
  opts: { allowPrivateNetwork: boolean; maxRedirects: number }
): Promise<Response> {
  // 首请求单独发出；重定向最多 follow maxRedirects 跳，每跳重新 assertSafeUrl（防 302 引入内网）。
  let current = rawUrl;
  await assertSafeUrl(current, opts.allowPrivateNetwork);
  let res = await fetch(current, { ...init, redirect: "manual" });
  for (let hop = 0; hop < opts.maxRedirects && res.status >= 300 && res.status < 400; hop++) {
    const loc = res.headers.get("location");
    if (!loc) break;
    current = new URL(loc, current).toString(); // 相对 Location 按当前 URL 解析
    await assertSafeUrl(current, opts.allowPrivateNetwork);
    res = await fetch(current, { ...init, redirect: "manual" });
  }
  if (res.status >= 300 && res.status < 400) {
    throw new Error(`too many redirects (>${opts.maxRedirects})`);
  }
  return res;
}

/** 流式读取响应体，累计到 maxBytes 即停 + cancel reader，防巨型响应整读 OOM。 */
async function readBodyCapped(res: Response, maxBytes: number): Promise<string> {
  if (!res.body) {
    // 无 body 流（极少见）——退化为一次性读取，仍受 maxBytes 截断保护。
    const text = await res.text();
    return text.length > maxBytes ? text.slice(0, maxBytes) + `\n... [truncated at ${maxBytes} bytes]` : text;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let received = 0;
  let out = "";
  let truncated = false;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!value) continue;
      received += value.byteLength;
      if (received > maxBytes) {
        const keep = value.byteLength - (received - maxBytes);
        out += decoder.decode(value.subarray(0, keep > 0 ? keep : 0), { stream: true });
        truncated = true;
        break;
      }
      out += decoder.decode(value, { stream: true });
    }
    out += decoder.decode(); // flush 尾部
  } finally {
    await reader.cancel().catch(() => {});
  }
  if (truncated) out += `\n... [truncated at ${maxBytes} bytes]`;
  return out;
}

export function createHttpRequestTool(opts: HttpRequestToolOptions = {}) {
  const allowPrivateNetwork = opts.allowPrivateNetwork ?? false;
  return tool(
    async ({ url, method, headers, body, timeout }) => {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), timeout);

      try {
        let response: Response;
        try {
          response = await safeFetch(
            url,
            {
              method,
              headers: {
                "Content-Type": "application/json",
                ...headers,
              },
              body: body || undefined,
              signal: controller.signal,
            },
            { allowPrivateNetwork, maxRedirects: 3 }
          );
        } catch (err) {
          // SSRF 拦截 / DNS 失败 / 过多重定向 / 网络错误统一转成工具结果（不抛）。
          return `HTTP request failed: ${err instanceof Error ? err.message : String(err)}`;
        }

        try {
          const contentType = response.headers.get("content-type") || "";
          const raw = await readBodyCapped(response, MAX_RESPONSE_BYTES);
          let responseBody: string;
          if (contentType.includes("application/json")) {
            try {
              responseBody = JSON.stringify(JSON.parse(raw), null, 2);
            } catch {
              responseBody = raw; // 非合法 JSON：原样返回（截断后的 body 常如此）。
            }
          } else {
            responseBody = raw;
          }

          // 给 LLM 的上下文截断（独立于 OOM 字节上限）。
          if (responseBody.length > MAX_RETURN_CHARS) {
            responseBody = responseBody.slice(0, MAX_RETURN_CHARS) + "\n... [truncated]";
          }

          return JSON.stringify({
            status: response.status,
            statusText: response.statusText,
            body: responseBody,
          });
        } catch (err) {
          // 读流阶段异常（超时 abort 打在 body 中段、socket reset）也降级为结果字符串，不抛出 tool。
          return `HTTP request failed: ${err instanceof Error ? err.message : String(err)}`;
        }
      } finally {
        clearTimeout(timeoutId);
      }
    },
    {
      name: "http_request",
      description: `Make HTTP requests to external APIs (GET, POST, PUT, DELETE, PATCH).
Use this for API calls that don't have a dedicated platform tool or MCP tool.
Security: only http/https; private/loopback/link-local destinations are rejected by default (anti-SSRF); redirects capped at 3 hops; response body capped to ${MAX_RETURN_CHARS} chars.
Before using this tool, check if a platform plugin provides the needed functionality.`,
      schema: z.object({
        url: z.string().describe("The URL to request"),
        method: z
          .enum(["GET", "POST", "PUT", "DELETE", "PATCH"])
          .default("GET")
          .describe("HTTP method"),
        headers: z
          .record(z.string())
          .optional()
          .describe("Request headers as key-value pairs"),
        body: z.string().optional().describe("Request body (JSON string)"),
        timeout: z
          .number()
          .default(30000)
          .describe("Timeout in milliseconds"),
      }),
    }
  );
}

/** 默认实例：安全默认（拦截私有网络）。需访问内网时改用 createHttpRequestTool({ allowPrivateNetwork: true })。 */
export const httpRequestTool = createHttpRequestTool();
