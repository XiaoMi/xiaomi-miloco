// 新建场景规则的默认值（住户反馈：进入/退出确认都不等，冷却 1 分钟）。
// 这几个数字直接决定规则灵敏度，改回去会静默改变「进入/退出」的手感，故锁住。
import { describe, expect, it } from "vitest";

import { buildInput, emptyDraft } from "../src/components/SceneTasksPage";

describe("场景规则新建表单默认值", () => {
  it("进入确认时间默认 0 秒（立即触发）", () => {
    expect(emptyDraft().enterDebounceSeconds).toBe("0");
  });

  it("退出确认时间默认 0 秒（条件不满足即退出）", () => {
    expect(emptyDraft().exitDebounceSeconds).toBe("0");
  });

  it("场景冷却默认 1 分钟（后端要求 >=1）", () => {
    expect(emptyDraft().cooldownMinutes).toBe("1");
  });

  it("提交时把空值兜底成同一组默认值，而不是旧值 5 / 60", () => {
    const input = buildInput({ ...emptyDraft(), cooldownMinutes: "", exitDebounceSeconds: "" });
    expect(input.enterDebounceSeconds).toBe(0);
    expect(input.exitDebounceSeconds).toBe(0);
    expect(input.cooldownMinutes).toBe(1);
  });
});

// 动态 TTS：一个方向可以只播报不触发自动化；没选音响 = 该方向不播报。
describe("场景规则表单 · 动态播报", () => {
  it("新建默认不播报（两个方向都没有音响）", () => {
    const d = emptyDraft();
    expect(d.enterTts.deviceId).toBe("");
    expect(d.exitTts.deviceId).toBe("");
    expect(d.enterTts.content).toBe(true); // 默认按画面动态生成
  });

  it("只开播报、不选自动化也能提交（音响 did 与播报配置成对带上）", () => {
    const d = emptyDraft();
    d.description = "出门夸夸";
    d.query = "准备出门";
    d.cameraDids = ["cam-1"];
    d.enterTts = {
      deviceId: "spk-1",
      content: true,
      prompt: "结合我的穿搭夸一句",
      text: "兜底一句",
    };
    const input = buildInput(d);
    expect(input.enterSceneId).toBeNull();
    expect(input.enterTtsDeviceId).toBe("spk-1");
    expect(input.enterTts).toEqual({
      ttsContent: true,
      ttsPrompt: "结合我的穿搭夸一句",
      ttsText: "兜底一句",
    });
    expect(input.exitTtsDeviceId).toBeNull();
    expect(input.exitTts).toBeNull();
  });
});
