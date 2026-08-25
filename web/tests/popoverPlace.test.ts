/**
 * fixed 浮层落点。抽成纯函数就是为了让这几条能回归——明细表同一个单元格里并排着
 * URL 气泡与清理菜单，此前各写一份，结果一个会在视口底部翻转、另一个不会。
 */

import { describe, it, expect } from "vitest";
import { placePopover } from "@/lib/popoverPlace";

const VP = { width: 1440, height: 900 };
const box = { width: 216, height: 212 };
const anchorAt = (top: number, left = 1000, w = 30, h = 30) => ({
  top,
  bottom: top + h,
  left,
  right: left + w,
});

describe("placePopover", () => {
  it("下方装得下就贴下方", () => {
    const p = placePopover(anchorAt(400), box, VP, { gap: 8, edge: 8, align: "right" });
    expect(p.top).toBe(438); // 400 + 30 + 8
  });

  it("下方装不下就翻到上方——这是行内浮层最常见的处境", () => {
    // 按钮底 830，下方只剩 70px，放不下 212 高的浮层
    const p = placePopover(anchorAt(800), box, VP, { gap: 8, edge: 8, align: "right" });
    expect(p.top).toBe(800 - 8 - 212); // 580，翻到按钮上方
    expect(p.top).toBeGreaterThanOrEqual(8);
    expect(p.top + box.height).toBeLessThanOrEqual(VP.height - 8);
  });

  it("上下都装不下时贴下方并夹住，不翻上——翻上会把标题推出视口", () => {
    const tall = { width: 216, height: 880 };
    const p = placePopover(anchorAt(400), tall, VP, { gap: 8, edge: 8, align: "right" });
    expect(p.top).toBe(VP.height - 8 - 880); // 12，夹在视口内
    expect(p.top).toBeGreaterThanOrEqual(8);
  });

  it("align=right 贴锚点右缘，align=left 贴左缘", () => {
    const a = anchorAt(100, 1000, 30);
    const r = placePopover(a, box, VP, { gap: 8, edge: 8, align: "right" });
    const l = placePopover(a, box, VP, { gap: 8, edge: 8, align: "left" });
    expect(r.left).toBe(1030 - 216); // 右缘对齐
    expect(l.left).toBe(1000); // 左缘对齐
  });

  it("右侧溢出时往左收", () => {
    const p = placePopover(anchorAt(100, 1400), box, VP, { gap: 8, edge: 8, align: "left" });
    expect(p.left).toBe(VP.width - 216 - 8);
  });

  it("窄屏也不许把左侧切掉——只有上界没有下界时这里会是负数", () => {
    // 320px 老机型 / 分屏窗口：视口比浮层还窄
    const p = placePopover(anchorAt(100, 0), box, { width: 320, height: 640 }, {
      gap: 6,
      edge: 8,
      align: "left",
    });
    expect(p.left).toBeGreaterThanOrEqual(8);
  });
});
