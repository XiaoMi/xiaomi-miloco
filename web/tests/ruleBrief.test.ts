/**
 * 触发条件能不能编辑 —— 按触发源判。
 *
 * node 环境无 jsdom，测导出的纯函数，不渲 DOM。
 */
import { describe, it, expect } from "vitest";
import { ruleConditionIsEditable } from "@/lib/ruleBrief";
import type { TaskRuleBrief } from "@/lib/types";

function brief(overrides: Partial<TaskRuleBrief> = {}): TaskRuleBrief {
  return {
    ruleId: "r1",
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
