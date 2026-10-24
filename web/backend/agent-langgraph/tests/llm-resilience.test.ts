/**
 * LLM 韧性原语单测（超时 / 重试 / 并发闸门 / config 解析）
 */

import { describe, it, expect, vi } from "vitest";
import { HumanMessage } from "@langchain/core/messages";
import { AppConfigSchema } from "../src/runtime/index.js";
import {
  withTimeout,
  withRetry,
  createConcurrencyLimiter,
  resolveLlmResilience,
  invokeWithResilience,
} from "../src/runtime/services/llm-resilience.js";

describe("resolveLlmResilience", () => {
  it("默认短/长超时与并发", () => {
    const r = resolveLlmResilience();
    expect(r.shortTimeoutMs).toBe(120_000);
    expect(r.longTimeoutMs).toBe(180_000);
    expect(r.maxConcurrent).toBe(2);
  });

  it("config.model.settings 覆盖默认值", () => {
    const config = AppConfigSchema.parse({
      model: {
        settings: {
          invokeTimeoutMs: 90_000,
          invokeLongTimeoutMs: 200_000,
          maxConcurrentInvokes: 3,
        },
      },
    });
    const r = resolveLlmResilience(config);
    expect(r.shortTimeoutMs).toBe(90_000);
    expect(r.longTimeoutMs).toBe(200_000);
    expect(r.maxConcurrent).toBe(3);
  });
});

describe("withTimeout", () => {
  it("未超时则返回结果", async () => {
    await expect(withTimeout(Promise.resolve(42), 500, "测试")).resolves.toBe(42);
  });

  it("超时则 reject", async () => {
    await expect(
      withTimeout(new Promise(() => undefined), 30, "测试")
    ).rejects.toThrow("测试超时（30ms）");
  });

  it("signal 已 abort → 立即 reject AbortError（不等超时）", async () => {
    const c = new AbortController();
    c.abort();
    await expect(
      withTimeout(new Promise(() => undefined), 10_000, "测试", c.signal)
    ).rejects.toThrow("测试已取消");
  });

  it("signal 中途 abort → 即时 reject（耗时远小于超时）", async () => {
    const c = new AbortController();
    const p = withTimeout(new Promise(() => undefined), 10_000, "测试", c.signal);
    setTimeout(() => c.abort(), 20);
    const start = Date.now();
    await expect(p).rejects.toThrow("测试已取消");
    expect(Date.now() - start).toBeLessThan(1000);
  });
});

describe("withRetry", () => {
  it("第二次成功则返回", async () => {
    let n = 0;
    const v = await withRetry(
      async () => {
        n++;
        if (n < 2) throw new Error("flaky");
        return "ok";
      },
      { attempts: 3, baseDelayMs: 1, label: "t" }
    );
    expect(v).toBe("ok");
    expect(n).toBe(2);
  });

  it("AbortError 不重试，立即抛出（用户已取消）", async () => {
    let n = 0;
    await expect(
      withRetry(
        async () => {
          n++;
          const err = new Error("aborted");
          err.name = "AbortError";
          throw err;
        },
        { attempts: 3, baseDelayMs: 1, label: "t" }
      )
    ).rejects.toThrow("aborted");
    expect(n).toBe(1);
  });

  it("signal 已 abort → 不调用 fn，直接抛 AbortError", async () => {
    const c = new AbortController();
    c.abort();
    const fn = vi.fn();
    await expect(
      withRetry(fn, { attempts: 3, baseDelayMs: 1, label: "t", signal: c.signal })
    ).rejects.toThrow("t已取消");
    expect(fn).not.toHaveBeenCalled();
  });
});

describe("createConcurrencyLimiter", () => {
  it("限制最大并发", async () => {
    const limit = createConcurrencyLimiter(2);
    let peak = 0;
    let active = 0;
    const task = () =>
      limit(async () => {
        active++;
        peak = Math.max(peak, active);
        await new Promise((r) => setTimeout(r, 20));
        active--;
      });
    await Promise.all([task(), task(), task(), task()]);
    expect(peak).toBeLessThanOrEqual(2);
  });
});

describe("invokeWithResilience", () => {
  it("经 model.invoke 返回内容", async () => {
    const model = {
      invoke: vi.fn().mockResolvedValue({ content: "hi" }),
    };
    const res = await invokeWithResilience(model, [new HumanMessage("x")], {
      timeoutMs: 1000,
      attempts: 1,
    });
    expect(res.content).toBe("hi");
    expect(model.invoke).toHaveBeenCalledOnce();
  });

  it("signal abort → 即时中断挂起的调用且不重试", async () => {
    let calls = 0;
    const model = {
      invoke: vi.fn().mockImplementation(() => {
        calls++;
        return new Promise<{ content: unknown }>(() => undefined); // 永不 resolve，只能被 abort 中断
      }),
    };
    const c = new AbortController();
    const p = invokeWithResilience(model, [new HumanMessage("x")], {
      timeoutMs: 10_000,
      attempts: 3,
      baseDelayMs: 1,
      signal: c.signal,
    });
    setTimeout(() => c.abort(), 20);
    const start = Date.now();
    await expect(p).rejects.toThrow(/已取消/);
    expect(Date.now() - start).toBeLessThan(1000);
    expect(calls).toBe(1); // 取消不重试
  });
});
