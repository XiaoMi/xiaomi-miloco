/**
 * cropBoxGeometry — Smart Crop 参考帧上那个框的坐标换算。
 *
 * 关键不变量:**不做任何缩放**。viewBox 用全景帧原始尺寸、rect 用原始像素坐标,
 * letterbox 由浏览器的 preserveAspectRatio 负责 —— 所以这里只需断言
 * 「原样搬过去 + 宽高相减」以及坏数据一律返 null(不画框,而不是吐 NaN 花屏)。
 */

import { describe, it, expect } from "vitest";
import { cropBoxGeometry } from "@/components/ActivityFeed";
import type { EventCropMeta } from "@/lib/types";

function crop(
  region: [number, number, number, number],
  frame: [number, number],
): EventCropMeta {
  return { region_xyxy: region, frame_size_wh: frame, crop_short_edge: 360 };
}

describe("cropBoxGeometry", () => {
  it("viewBox = 全景帧原始尺寸,rect = 原始像素坐标(不缩放)", () => {
    const geo = cropBoxGeometry(crop([430, 122, 590, 318], [640, 360]));
    expect(geo).toMatchObject({
      viewBox: "0 0 640 360",
      x: 430,
      y: 122,
      width: 160,
      height: 196,
    });
  });

  it("strokeWidth 按帧宽比例给:1920 宽的帧上是 12,而不是固定 2", () => {
    expect(cropBoxGeometry(crop([0, 0, 100, 100], [1920, 1080]))!.strokeWidth).toBe(12);
  });

  it("小帧下 strokeWidth 有下限 2(320/160=2,不会退化成 1px 看不见)", () => {
    expect(cropBoxGeometry(crop([0, 0, 50, 50], [320, 180]))!.strokeWidth).toBe(2);
    expect(cropBoxGeometry(crop([0, 0, 20, 20], [160, 90]))!.strokeWidth).toBe(2);
  });

  it("退化 region(x2<=x1 或 y2<=y1)→ null", () => {
    expect(cropBoxGeometry(crop([100, 50, 100, 200], [640, 360]))).toBeNull();
    expect(cropBoxGeometry(crop([100, 200, 300, 200], [640, 360]))).toBeNull();
    expect(cropBoxGeometry(crop([300, 200, 100, 50], [640, 360]))).toBeNull();
  });

  it("帧尺寸非正 → null(viewBox 会变成 0 宽,svg 直接不可渲染)", () => {
    expect(cropBoxGeometry(crop([10, 10, 20, 20], [0, 360]))).toBeNull();
    expect(cropBoxGeometry(crop([10, 10, 20, 20], [640, 0]))).toBeNull();
  });

  it("字段缺失 / 非数字(历史 trace、被截断的数组)→ null,不吐 NaN", () => {
    const missing = { crop_short_edge: 360 } as unknown as EventCropMeta;
    expect(cropBoxGeometry(missing)).toBeNull();

    const truncated = {
      region_xyxy: [10, 10],
      frame_size_wh: [640, 360],
      crop_short_edge: 360,
    } as unknown as EventCropMeta;
    expect(cropBoxGeometry(truncated)).toBeNull();

    const nonNumeric = {
      region_xyxy: ["10", 10, 20, 20],
      frame_size_wh: [640, 360],
      crop_short_edge: 360,
    } as unknown as EventCropMeta;
    expect(cropBoxGeometry(nonNumeric)).toBeNull();
  });
});
