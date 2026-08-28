/**
 * 刷新周期的夹取规则。
 *
 * 抽成纯函数是为了让输入框与数据侧共用同一份规则——输入框不能只靠「把值交上去、
 * 等回流的新值改变显示」：值已经在下限上时，再输一个更小的数夹完还是下限，
 * 上层 state 不变、React 跳过重渲染、回显也就不会发生，框里会留着那个不生效的数。
 */

import { describe, it, expect } from "vitest";
import { afterEach, beforeEach } from "vitest";
import {
  clampRefreshSec,
  readStoredRefreshSec,
  REFRESH_DEFAULT_SEC,
  REFRESH_KEY,
  REFRESH_MAX_SEC,
  REFRESH_MIN_SEC,
} from "@/hooks/useRefreshInterval";

describe("clampRefreshSec", () => {
  it("低于下限一律抬到下限", () => {
    expect(clampRefreshSec(1)).toBe(REFRESH_MIN_SEC);
    expect(clampRefreshSec(0)).toBe(REFRESH_MIN_SEC);
    expect(clampRefreshSec(-30)).toBe(REFRESH_MIN_SEC);
  });

  it("已经在下限上时再夹仍是下限——这正是输入框回显失效的那一步", () => {
    expect(clampRefreshSec(REFRESH_MIN_SEC)).toBe(REFRESH_MIN_SEC);
    expect(clampRefreshSec(clampRefreshSec(1))).toBe(REFRESH_MIN_SEC);
  });

  it("下限之上原样保留，小数向下取整", () => {
    expect(clampRefreshSec(30)).toBe(30);
    expect(clampRefreshSec(7.9)).toBe(7);
    expect(clampRefreshSec(3600)).toBe(3600);
  });

  it("上限 1 天：到这一档已经等于关掉自动刷新", () => {
    expect(clampRefreshSec(REFRESH_MAX_SEC)).toBe(REFRESH_MAX_SEC);
    expect(clampRefreshSec(REFRESH_MAX_SEC - 1)).toBe(REFRESH_MAX_SEC - 1);
  });

  it("超过上限一律压到上限——再大会让 setInterval 的 delay 回绕成负数", () => {
    // delay 在 WebIDL 里是 32 位有符号整数：2147484 秒 × 1000 就越界，
    // 按 ToInt32 回绕成负数、被规范夹到 0，实测 300ms 内触发 79 次。
    // 「填很大等于关掉」会因此变成每 4 毫秒重取一次，方向正好反了。
    expect(clampRefreshSec(REFRESH_MAX_SEC + 1)).toBe(REFRESH_MAX_SEC);
    expect(clampRefreshSec(2_147_484)).toBe(REFRESH_MAX_SEC);
    expect(clampRefreshSec(999_999_999)).toBe(REFRESH_MAX_SEC);
    // 夹完的值乘 1000 必须仍在 32 位有符号范围内，否则前一条断言就是白写的
    expect(clampRefreshSec(999_999_999) * 1000).toBeLessThanOrEqual(2_147_483_647);
  });
});


describe("readStoredRefreshSec（存量值也要过上下限）", () => {
  const store = new Map<string, string>();
  beforeEach(() => {
    store.clear();
    (globalThis as { localStorage?: unknown }).localStorage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    };
  });
  afterEach(() => {
    delete (globalThis as { localStorage?: unknown }).localStorage;
  });

  it("上限收紧之前存进去的大数要被夹回来——否则刷新页面救不回卡死的页面", () => {
    store.set(REFRESH_KEY, "999999999");
    expect(readStoredRefreshSec()).toBe(REFRESH_MAX_SEC);
    expect(readStoredRefreshSec() * 1000).toBeLessThanOrEqual(2_147_483_647);
  });

  it("手改存储塞进的小数同样抬回下限", () => {
    store.set(REFRESH_KEY, "1");
    expect(readStoredRefreshSec()).toBe(REFRESH_MIN_SEC);
  });

  it("没存过 / 存了非数字 → 出厂值", () => {
    expect(readStoredRefreshSec()).toBe(REFRESH_DEFAULT_SEC);
    store.set(REFRESH_KEY, "abc");
    expect(readStoredRefreshSec()).toBe(REFRESH_DEFAULT_SEC);
  });

  it("范围内的值原样读回", () => {
    store.set(REFRESH_KEY, "45");
    expect(readStoredRefreshSec()).toBe(45);
  });
});
