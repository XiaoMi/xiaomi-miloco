/**
 * humanizeRulesInText 渲染层单测。
 *
 * 当前格式的「所属任务 / 对应规则」由后端构造成住户可读形态（无 [task_id]），
 * 前端原样保留、不 strip；只清理同 header 旧行残留的 [task_id] 前缀（触发规则 / 触发条件）。
 */

import { describe, it, expect } from "vitest";
import { humanizeRulesInText } from "@/lib/eventText";

describe("humanizeRulesInText", () => {
  it("新格式：任务 + 规则[短名] 原样保留（方括号短名不被误 strip）", () => {
    const text =
      "[感知引擎]规则提醒：\n" +
      "任务：书房安防\n" +
      "规则：[书桌前有人] 有人坐在书桌前面向屏幕\n" +
      "触发原因：画面中有人";
    const out = humanizeRulesInText(text);
    expect(out).toContain("任务：书房安防");
    expect(out).toContain("规则：[书桌前有人] 有人坐在书桌前面向屏幕");
  });

  it("旧数据 v3：触发规则行 strip [task_id] 前缀", () => {
    const text =
      "[感知引擎]规则提醒：\n触发规则：[kitchen_safety] 厨房安全\n触发原因：x";
    const out = humanizeRulesInText(text);
    expect(out).toContain("触发规则：厨房安全");
    expect(out).not.toContain("[kitchen_safety]");
  });

  it("旧数据：触发条件兜底成 [task_id] 名时 strip 前缀", () => {
    const text =
      "[感知引擎]规则提醒：\n触发条件：[kitchen_safety] 厨房安全\n触发原因：x";
    const out = humanizeRulesInText(text);
    expect(out).toContain("触发条件：厨房安全");
    expect(out).not.toContain("[kitchen_safety]");
  });

  it("触发条件里以中文方括号 token 开头的合法 query 不被误 strip", () => {
    const text =
      "[感知引擎]规则提醒：\n触发条件：[夜间]是否有人闯入\n触发原因：x";
    expect(humanizeRulesInText(text)).toContain("触发条件：[夜间]是否有人闯入");
  });
});
