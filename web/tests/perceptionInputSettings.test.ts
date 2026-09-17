/**
 * 「设置 → 感知输入」的契约测试。
 *
 * 这一块只改了三个地方，任何一处漂移都会让住户"拨了开关没反应"：
 *   1. 默认值必须与后端 settings.yaml 一致：rule_only_input=image、last_frame_only=true
 *      （图片输入 + 只送末帧）。默认值写反了，新装机的行为就跟 UI 上显示的对不上。
 *   2. payload 的字段名必须是后端 PerceptionConfigBody 认的
 *      perception_input / image_last_frame_only（写错会被 pydantic 静默忽略，
 *      保存看起来成功、实际没生效）。
 *   3. 恢复默认要回到"图片 + 只送末帧"，而不是把开关带到某个中间态。
 *
 * 只测纯逻辑（vitest 是 node 环境，没有 jsdom）：组件里的默认常量与 payload 组装函数
 * 都从 SettingsDrawer 导出，测试直接引用，不复制一份字面量 —— 那样测试只会自己跟自己一致。
 */

import { describe, expect, it } from "vitest";
import {
  buildPerceptionInputPayload,
  DEFAULT_IMAGE_LAST_FRAME_ONLY,
  DEFAULT_PERCEPTION_INPUT,
  normalizePerceptionInput,
  PERCEPTION_INPUT_MODES,
} from "@/lib/perceptionInput";

describe("感知输入默认值", () => {
  it("默认是图片输入 + 只送末帧（与 settings.yaml 对齐）", () => {
    expect(PERCEPTION_INPUT_MODES).toEqual(["image", "video"]);
    expect(DEFAULT_PERCEPTION_INPUT).toBe("image");
    // 图片按张计费，多帧纯属多花 token；场景规则判的多是"此刻状态"，末帧够用
    expect(DEFAULT_IMAGE_LAST_FRAME_ONLY).toBe(true);
  });
});

describe("buildPerceptionInputPayload", () => {
  it("图片 + 多帧：显式两个字段都带上", () => {
    expect(buildPerceptionInputPayload("image", false)).toEqual({
      perception_input: "image",
      image_last_frame_only: false,
    });
  });

  it("图片 + 只送末帧", () => {
    expect(buildPerceptionInputPayload("image", true)).toEqual({
      perception_input: "image",
      image_last_frame_only: true,
    });
  });

  it("视频模式原样上报（末帧开关值保留,切回图片时还是住户上次的选择）", () => {
    expect(buildPerceptionInputPayload("video", true)).toEqual({
      perception_input: "video",
      image_last_frame_only: true,
    });
  });

  it("字段名与后端 PerceptionConfigBody 一致", () => {
    const keys = Object.keys(buildPerceptionInputPayload("image", false)).sort();
    expect(keys).toEqual(["image_last_frame_only", "perception_input"]);
  });

  it("恢复默认 = 图片 + 只送末帧", () => {
    expect(
      buildPerceptionInputPayload(
        DEFAULT_PERCEPTION_INPUT,
        DEFAULT_IMAGE_LAST_FRAME_ONLY,
      ),
    ).toEqual({ perception_input: "image", image_last_frame_only: true });
  });
});

describe("normalizePerceptionInput（老后端不返字段时的收敛）", () => {
  it("只有明确 video 才是视频，其余一律回默认图片", () => {
    expect(normalizePerceptionInput("video")).toBe("video");
    expect(normalizePerceptionInput("image")).toBe("image");
    expect(normalizePerceptionInput(undefined)).toBe("image");
    expect(normalizePerceptionInput(null)).toBe("image");
    expect(normalizePerceptionInput("VIDEO")).toBe("image");
    expect(normalizePerceptionInput("")).toBe("image");
  });
});
