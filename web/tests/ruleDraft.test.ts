import { describe, expect, it } from "vitest";

import { makeRuleDraft, ruleDiff, ruleNamePrefix } from "@/lib/ruleDraft";
import type { TaskRuleBrief } from "@/lib/types";

// 后端只校验最终的名字串，验不出「前缀被悄悄丢掉」；这一层的分支只有纯函数测试接得住。
const brief = (over: Partial<TaskRuleBrief> = {}): TaskRuleBrief => ({
  ruleId: "r1",
  name: "[piano_practice] 孩子坐在钢琴前",
  mode: "event",
  query: "孩子在弹琴",
  actionsDesc: [],
  actionDescriptions: ["提醒一句"],
  onEnterDesc: null,
  onExitDesc: null,
  onTargetDesc: null,
  deviceActions: [],
  editableDescSlots: ["action_descriptions"],
  ...over,
});

describe("ruleNamePrefix", () => {
  it("只认 skill 约定的 `[task_id] ` 形态", () => {
    expect(ruleNamePrefix("t1", "[t1] 有人进门")).toBe("[t1] ");
    // 人工建的老规则 / 别的任务的前缀 → 空串，整条名字交给住户改，不硬套
    expect(ruleNamePrefix("t1", "客厅有人走动")).toBe("");
    expect(ruleNamePrefix("t1", "[t2] 有人进门")).toBe("");
  });
});

describe("ruleDiff", () => {
  it("带前缀规则名只发前缀 + 改过的后半段", () => {
    const rule = brief();
    const draft = {
      ...makeRuleDraft("piano_practice", rule),
      nameSuffix: "孩子在弹钢琴",
    };
    expect(ruleDiff("piano_practice", rule, draft)).toEqual({
      name: "[piano_practice] 孩子在弹钢琴",
    });
  });

  it("无前缀老规则不硬套前缀", () => {
    const rule = brief({ name: "客厅有人走动", editableDescSlots: [] });
    const draft = { ...makeRuleDraft("t1", rule), nameSuffix: "客厅有人跑动" };
    expect(ruleDiff("t1", rule, draft)).toEqual({ name: "客厅有人跑动" });
  });

  it("原封不动 → null，不发空 PATCH", () => {
    const rule = brief();
    expect(ruleDiff("piano_practice", rule, makeRuleDraft("piano_practice", rule))).toBeNull();
  });

  it("清空槽位视作放弃修改，不下发（避免撞后端「至少配一个」校验）", () => {
    const rule = brief();
    const draft = { ...makeRuleDraft("t1", rule), action_descriptions: "" };
    expect(ruleDiff("t1", rule, draft)).toBeNull();
  });

  it("存量值带首尾空格时，仅改一处不捎带另一处的规范化 PATCH", () => {
    const rule = brief({ query: "  孩子在弹琴  ", actionDescriptions: [" 提醒一句 "] });
    const draft = { ...makeRuleDraft("t1", rule), nameSuffix: "孩子在弹钢琴" };
    // query 与 action_descriptions 只是首尾空格差异 → 不出现在 patch 里
    expect(ruleDiff("t1", rule, draft)).toEqual({ name: "孩子在弹钢琴" });
  });

  it("前缀与描述之间的多余空格不算改动", () => {
    // 前缀含一个空格，`]` 后多打的空格会落进 nameSuffix；两侧不同口径的话住户
    // 一个字没改点保存也会被静默改名（老规则名不合规时还会被新校验挡在保存外）。
    const rule = brief({ name: "[t1]  客厅有人" });
    expect(ruleDiff("t1", rule, makeRuleDraft("t1", rule))).toBeNull();
  });

  it("多条文案按行拆分，空行丢掉", () => {
    const rule = brief();
    const draft = {
      ...makeRuleDraft("t1", rule),
      action_descriptions: "提醒一句\n\n  再提醒一句  \n",
    };
    expect(ruleDiff("t1", rule, draft)).toEqual({
      actionDescriptions: ["提醒一句", "再提醒一句"],
    });
  });

  it("只发白名单里的槽位：不在 editableDescSlots 里的改动不下发", () => {
    // state 模式 + 进入方向已接设备直控 → 只开 on_exit_desc
    const rule = brief({
      mode: "state",
      actionDescriptions: [],
      onEnterDesc: null,
      onExitDesc: "离开时提醒",
      deviceActions: ["on_enter:prop.2.1"],
      editableDescSlots: ["on_exit_desc"],
    });
    const draft = {
      ...makeRuleDraft("t1", rule),
      on_enter_desc: "住户手改也不该发",
      on_exit_desc: "离开时关灯",
    };
    expect(ruleDiff("t1", rule, draft)).toEqual({ onExitDesc: "离开时关灯" });
  });
});
