/**
 * 发行版本（full / slim）能力单测。
 *
 * 覆盖：默认 full（不误砍功能）、setEditionInfo 写入与 null 回退、slim 下
 * visibleTabs 只留概览/场景联动/日志/模型、SLIM_TAB_KEYS 与 Sidebar 真实 TABS 一致
 * （防止有人加了 tab 却漏进白名单 —— 这条是本次改动最容易腐化的地方）。
 */

import { afterEach, describe, expect, it } from "vitest";
import {
  FULL_EDITION,
  SLIM_TAB_KEYS,
  getEdition,
  isSlimEdition,
  setEditionInfo,
  visibleTabs,
} from "@/lib/edition";
import { TABS } from "@/components/Sidebar";
import type { EditionInfo } from "@/lib/types";

const slimInfo: EditionInfo = {
  edition: "slim",
  slim: true,
  capabilities: {
    identity: false,
    pet: false,
    home_profile: false,
    tasks: false,
    schedule: false,
    observability: false,
    one_click_upgrade: false,
    rule_only: true,
  },
};

afterEach(() => setEditionInfo(FULL_EDITION));

describe("edition 能力缓存", () => {
  it("默认是 full —— 后端取不到时绝不砍功能", () => {
    expect(getEdition()).toEqual(FULL_EDITION);
    expect(isSlimEdition()).toBe(false);
    expect(getEdition().capabilities.identity).toBe(true);
  });

  it("setEditionInfo 写入 slim 后可读", () => {
    setEditionInfo(slimInfo);
    expect(isSlimEdition()).toBe(true);
    expect(getEdition().capabilities.one_click_upgrade).toBe(false);
  });

  it("setEditionInfo(null) 回退 full（请求失败/超时路径）", () => {
    setEditionInfo(slimInfo);
    setEditionInfo(null);
    expect(getEdition()).toEqual(FULL_EDITION);
  });
});

describe("visibleTabs", () => {
  it("full 原样返回全部 tab（同一引用，零开销）", () => {
    expect(visibleTabs(TABS)).toBe(TABS);
  });

  it("slim 只留概览/场景联动/日志/模型", () => {
    setEditionInfo(slimInfo);
    expect(visibleTabs(TABS).map((tab) => tab.key)).toEqual([
      "now",
      "scenes",
      "activity",
      "usage",
    ]);
  });

  it("SLIM_TAB_KEYS 里的 key 必须真实存在于 TABS", () => {
    const known = new Set(TABS.map((tab) => tab.key));
    for (const key of SLIM_TAB_KEYS) {
      expect(known.has(key as (typeof TABS)[number]["key"])).toBe(true);
    }
  });

  it("slim 白名单不含设备/家庭/任务这些后端未注册的 tab", () => {
    for (const key of ["devices", "family", "tasks"]) {
      expect(SLIM_TAB_KEYS.includes(key)).toBe(false);
    }
  });
});
