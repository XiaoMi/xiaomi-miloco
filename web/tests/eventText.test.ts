/**
 * humanizeRulesInText 渲染层单测。
 *
 * 后端 meaningful_events.text 里规则提醒块新增「任务名称：[task_id] 规则名」行，
 * DB 存带前缀全名（spec B2: DB==webhook），UI 渲染时 strip 掉 [task_id] 前缀。
 */

import { describe, it, expect } from "vitest";
import { humanizeRulesInText } from "@/lib/eventText";

describe("humanizeRulesInText 任务名称前缀 strip", () => {
  it("strip 任务名称行的 [task_id] 前缀，只留中文名", () => {
    const text =
      "[感知引擎]规则提醒：\n" +
      "任务名称：[kitchen_safety] 厨房安全\n" +
      "触发条件：厨房是否有明火\n" +
      "触发原因：检测到明火";
    const out = humanizeRulesInText(text);
    expect(out).toContain("任务名称：厨房安全");
    expect(out).not.toContain("[kitchen_safety]");
    // 其它行保持不变
    expect(out).toContain("触发条件：厨房是否有明火");
    expect(out).toContain("触发原因：检测到明火");
  });

  it("任务名称不带前缀时原样保留", () => {
    const text = "[感知引擎]规则提醒：\n任务名称：厨房安全\n触发原因：x";
    expect(humanizeRulesInText(text)).toContain("任务名称：厨房安全");
  });

  it("显示名退化成仅前缀时，不吞换行、不粘连下一行", () => {
    // [^\S\n]* 而非 \s*：前缀后无中文名时不能把换行一起吃掉
    const text =
      "[感知引擎]规则提醒：\n" +
      "任务名称：[kitchen_safety]\n" +
      "触发原因：检测到明火";
    const out = humanizeRulesInText(text);
    expect(out).toContain("任务名称：\n触发原因：检测到明火");
    expect(out).not.toContain("任务名称：触发原因"); // 不粘连
  });
});
