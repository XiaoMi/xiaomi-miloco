/**
 * humanizeRulesInText 渲染层单测。
 *
 * 当前格式的「所属任务 / 对应规则」由后端构造成住户可读形态（无 [task_id]），
 * 前端原样保留、不 strip；只清理同 header 旧行残留的 [task_id] 前缀（触发规则 / 触发条件）。
 */

import { describe, it, expect } from "vitest";
import { humanizeRulesInText, splitHumanizedSections } from "@/lib/eventText";

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

  it("旧数据 v3：触发规则中文方括号 token 不被误 strip（ascii 口径）", () => {
    const text = "[感知引擎]规则提醒：\n触发规则：[夜间]有人闯入\n触发原因：x";
    expect(humanizeRulesInText(text)).toContain("触发规则：[夜间]有人闯入");
  });

  it("旧格式 v2：检测到 ascii [task_id] 前缀被 strip、中文方括号保留", () => {
    const ascii = "[感知引擎] 提醒:\n检测到：[kitchen] 厨房安全";
    expect(humanizeRulesInText(ascii)).toContain("检测到：厨房安全");
    const cn = "[感知引擎] 提醒:\n检测到：[夜间]有人闯入";
    expect(humanizeRulesInText(cn)).toContain("检测到：[夜间]有人闯入");
  });

  it("旧格式 v1：rule_name ascii [task_id] 前缀被 strip、中文方括号保留", () => {
    const asciiPrefix =
      '[感知引擎] 命中以下规则:\n1. {"rule_id":"r1","rule_name":"[kitchen] 厨房安全","reason":"x"}';
    const outAscii = humanizeRulesInText(asciiPrefix);
    expect(outAscii).toContain("厨房安全");
    expect(outAscii).not.toContain("kitchen");

    const cnBracket =
      '[感知引擎] 命中以下规则:\n1. {"rule_id":"r1","rule_name":"[夜间]有人闯入","reason":"x"}';
    expect(humanizeRulesInText(cnBracket)).toContain("夜间");
  });
});

describe("splitHumanizedSections（折叠态「触发状态」→ badge）", () => {
  const block = (status: string) =>
    "[感知引擎]规则提醒：\n" +
    "任务：书房安防\n" +
    "规则：[书桌前有人] 有人坐在书桌前面向屏幕\n" +
    `触发状态：${status}\n` +
    "时间：16:50:08\n" +
    "触发原因：画面中有人";

  it("五种取值都能识别，且「触发状态」行从折叠态正文里摘掉", () => {
    const cases: Array<[string, string]> = [
      ["已触发", "fired"],
      ["未触发（持续中）", "stillIn"],
      ["未触发（计时中）", "counting"],
      ["未触发", "notFired"],
      ["未知", "unknown"],
    ];
    for (const [label, kind] of cases) {
      const [sec] = splitHumanizedSections(block(label));
      expect(sec.status, label).toBe(kind);
      // 该行改由 badge 承载 → 正文不再含它，但其余字段都在
      expect(sec.text, label).not.toContain("触发状态");
      expect(sec.text, label).toContain("任务：书房安防");
      expect(sec.text, label).toContain("触发原因：画面中有人");
    }
  });

  it("整值精确匹配：「未触发」不能把「未触发（持续中/计时中）」误判成 notFired", () => {
    // 「未触发」是后两者的真前缀。注：单靠这三条**不足以**证明是精确匹配——键的字面
    // 顺序恰好把长值排在前，前缀匹配也能通过；真正的判别力在下一条用例。
    expect(splitHumanizedSections(block("未触发（持续中）"))[0].status).toBe("stillIn");
    expect(splitHumanizedSections(block("未触发（计时中）"))[0].status).toBe("counting");
    expect(splitHumanizedSections(block("未触发"))[0].status).toBe("notFired");
  });

  it("以已知值为前缀、但整值不认识 → 不猜（这条才真正钉住「精确匹配」）", () => {
    // 「未触发（冷却中）」以「未触发」开头：前缀匹配会误判成 notFired（把一个我们并不
    // 理解的新状态当成已知状态展示）；精确匹配则老实不出 badge、原行保留。
    const [sec] = splitHumanizedSections(block("未触发（冷却中）"));
    expect(sec.status).toBeUndefined();
    expect(sec.text).toContain("触发状态：未触发（冷却中）");
  });

  it("无「触发状态」行（老数据）→ 无 badge、正文原样", () => {
    const text = "[感知引擎]规则提醒：\n任务：书房安防\n触发原因：画面中有人";
    const [sec] = splitHumanizedSections(text);
    expect(sec.status).toBeUndefined();
    expect(sec.text).toBe(text);
  });

  it("认不出的值（后端将来加新状态）→ 不猜：无 badge，原行保留", () => {
    const [sec] = splitHumanizedSections(block("已派发（冷却中）"));
    expect(sec.status).toBeUndefined();
    expect(sec.text).toContain("触发状态：已派发（冷却中）");
  });

  it("一条 event 多个规则块 → 各段各自的状态（per 规则块，不是 per 事件）", () => {
    const text = block("已触发") + "\n\n═══\n\n" + block("未触发（持续中）").replace("[感知引擎]规则提醒：\n", "");
    const secs = splitHumanizedSections(text);
    const statuses = secs.map((s) => s.status).filter(Boolean);
    expect(statuses).toEqual(["fired", "stillIn"]);
  });
});

describe("splitHumanizedSections：老数据兜底", () => {
  it("非规则块里的伪造「触发状态」行不升级成 badge（只需一个 \\n 就能造出来）", () => {
    // fold 之前写入的老数据：事件提醒块的「建议」字段带未折叠换行
    const text =
      "[感知引擎]事件提醒：\n" +
      "时间：03:00:00\n" +
      "检测到：门口有动静\n" +
      "建议：留意门口\n触发状态：已触发";
    const [sec] = splitHumanizedSections(text);
    expect(sec.status).toBeUndefined(); // 不给伪造行发 badge
    expect(sec.text).toContain("触发状态：已触发"); // 原样留在正文里，不隐藏
  });

  it("真规则块（同段带 任务/规则/触发原因）仍正常出 badge", () => {
    const text =
      "[感知引擎]规则提醒：\n任务：书房安防\n触发状态：已触发\n触发原因：画面中有人";
    const [sec] = splitHumanizedSections(text);
    expect(sec.status).toBe("fired");
  });
});
