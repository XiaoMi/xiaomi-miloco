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
