/**
 * 刷新周期的夹取规则。
 *
 * 抽成纯函数是为了让输入框与数据侧共用同一份规则——输入框不能只靠「把值交上去、
 * 等回流的新值改变显示」：值已经在下限上时，再输一个更小的数夹完还是下限，
 * 上层 state 不变、React 跳过重渲染、回显也就不会发生，框里会留着那个不生效的数。
 */

import { describe, it, expect } from "vitest";
import { clampRefreshSec, REFRESH_MIN_SEC } from "@/hooks/useRefreshInterval";

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

  it("上限不设——填很大等于自己关掉自动刷新", () => {
    expect(clampRefreshSec(86_400)).toBe(86_400);
  });
});
