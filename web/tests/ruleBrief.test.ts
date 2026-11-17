/**
 * 触发条件能不能编辑 —— 按触发源判；以及按方向分组。
 *
 * node 环境无 jsdom，测导出的纯函数，不渲 DOM。
 */
import { describe, it, expect } from "vitest";
import { groupRuleBriefs, ruleConditionIsEditable } from "@/lib/ruleBrief";
import type { TaskRuleBrief } from "@/lib/types";

function brief(overrides: Partial<TaskRuleBrief> = {}): TaskRuleBrief {
  return {
    ruleId: "r1",
    name: "[r1] 有人在门口",
    enabled: true,
    query: "有人在门口",
    direction: "enter",
    sourceType: "omni",
    actionsDesc: [],
    ...overrides,
  };
}

describe("ruleConditionIsEditable", () => {
  it("omni 规则的条件可编辑", () => {
    expect(ruleConditionIsEditable(brief())).toBe(true);
  });

  // 与上一条方向相反：判据写反时只有一条会红。
  it("iot 规则的条件只读", () => {
    expect(ruleConditionIsEditable(brief({ sourceType: "iot" }))).toBe(false);
  });

  it("record 规则的条件只读", () => {
    expect(ruleConditionIsEditable(brief({ sourceType: "record" }))).toBe(false);
  });
});

describe("groupRuleBriefs", () => {
  // 这条是回归护栏：后端不限制同方向条数（只强制 session 独占），运行期把它们
  // OR 起来。分组若退化成「取第一条」，第二条规则会在界面上静默消失。
  it("同方向多条规则全部保留", () => {
    const groups = groupRuleBriefs([
      brief({ ruleId: "e1", direction: "enter", query: "有人进客厅" }),
      brief({ ruleId: "e2", direction: "enter", query: "门磁被打开" }),
    ]);
    expect(groups.entry.map((r) => r.ruleId)).toEqual(["e1", "e2"]);
  });

  it("会话与进入归入同一组；退出、前提各自成组", () => {
    const groups = groupRuleBriefs([
      brief({ ruleId: "s1", direction: "session" }),
      brief({ ruleId: "x1", direction: "exit" }),
      brief({ ruleId: "g1", direction: "guard" }),
      brief({ ruleId: "g2", direction: "guard" }),
    ]);
    expect(groups.entry.map((r) => r.ruleId)).toEqual(["s1"]);
    expect(groups.exit.map((r) => r.ruleId)).toEqual(["x1"]);
    expect(groups.guard.map((r) => r.ruleId)).toEqual(["g1", "g2"]);
  });

  it("停用的规则不进任何一组 —— 它们不参与运行期判定", () => {
    const groups = groupRuleBriefs([
      brief({ ruleId: "e1", direction: "enter" }),
      brief({ ruleId: "e2", direction: "enter", enabled: false }),
      brief({ ruleId: "g1", direction: "guard", enabled: false }),
    ]);
    expect(groups.entry.map((r) => r.ruleId)).toEqual(["e1"]);
    expect(groups.guard).toEqual([]);
  });

  it("没有规则时三组都是空数组，不是 undefined", () => {
    const groups = groupRuleBriefs([]);
    expect(groups).toEqual({ entry: [], exit: [], guard: [] });
  });

  it("组内保持传入顺序 —— 后端 SQL 无 ORDER BY，顺序只能由调用方透传", () => {
    const groups = groupRuleBriefs([
      brief({ ruleId: "e2", direction: "enter" }),
      brief({ ruleId: "e1", direction: "enter" }),
    ]);
    expect(groups.entry.map((r) => r.ruleId)).toEqual(["e2", "e1"]);
  });
});
