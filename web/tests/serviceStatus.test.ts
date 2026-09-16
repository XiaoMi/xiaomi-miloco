// 后端服务状态探测：内置网页靠它判断"服务还在不在"（菜单栏点了停止服务 /
// 进程崩了之后页面要自己看出来，恢复后自动刷新）。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  isServiceDown,
  nextServiceState,
  probeService,
  setServiceDown,
} from "../src/lib/serviceStatus";

const realFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = realFetch;
  setServiceDown(false);
  vi.restoreAllMocks();
});

describe("probeService", () => {
  it("2xx 视为服务在跑，并打的是免鉴权的 /health", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    globalThis.fetch = (async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      return new Response("{}", { status: 200 });
    }) as unknown as typeof fetch;

    await expect(probeService()).resolves.toBe(true);
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/health");
    // 不能带缓存：服务刚重启时读到旧的 200 会误判为"已恢复"
    expect(calls[0].init?.cache).toBe("no-store");
  });

  it("5xx 视为不可用（服务在但没就绪，同样不能当运行中）", async () => {
    globalThis.fetch = (async () =>
      new Response("boom", { status: 503 })) as unknown as typeof fetch;
    await expect(probeService()).resolves.toBe(false);
  });

  it("连接被拒（服务已停）不抛异常，直接返回 false", async () => {
    globalThis.fetch = (async () => {
      throw new TypeError("Failed to fetch");
    }) as unknown as typeof fetch;
    await expect(probeService()).resolves.toBe(false);
  });

  it("超时按 AbortSignal 中断，返回 false 而不是挂住页面", async () => {
    globalThis.fetch = ((_url: string, init?: RequestInit) =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        );
      })) as unknown as typeof fetch;

    await expect(probeService(5)).resolves.toBe(false);
  });
});

describe("nextServiceState", () => {
  it("冷启动首帧成功不算「恢复」——否则每次开页面都自我重载", () => {
    expect(nextServiceState("checking", true)).toEqual({ state: "up", recovered: false });
  });

  it("确认停止过再恢复才算「恢复」——页面据此自动刷新", () => {
    expect(nextServiceState("down", true)).toEqual({ state: "up", recovered: true });
  });

  it("运行中探测失败 → 已停止，不触发刷新", () => {
    expect(nextServiceState("up", false)).toEqual({ state: "down", recovered: false });
  });

  it("持续不可用不会反复触发「恢复」", () => {
    expect(nextServiceState("down", false)).toEqual({ state: "down", recovered: false });
  });
});

describe("setServiceDown 全局标记", () => {
  beforeEach(() => setServiceDown(false));

  it("默认未标记（页面正常时不该静音任何 toast）", () => {
    expect(isServiceDown()).toBe(false);
  });

  it("探测失败后置位，供 useAsync 静音「加载失败」toast；恢复后清掉", () => {
    setServiceDown(true);
    expect(isServiceDown()).toBe(true);
    setServiceDown(false);
    expect(isServiceDown()).toBe(false);
  });
});
