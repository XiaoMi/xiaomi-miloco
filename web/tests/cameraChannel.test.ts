import { describe, it, expect } from "vitest";
import {
  channelHasMic,
  feedDid,
  lensLabelKey,
  multiChannelDidSet,
  splitChannelDid,
} from "@/lib/cameraChannel";

describe("splitChannelDid", () => {
  it("裸 did 直通、channel 0", () => {
    expect(splitChannelDid("cam1")).toEqual({ physicalDid: "cam1", channel: 0 });
  });
  it("合成 did 拆物理 + 通道", () => {
    expect(splitChannelDid("cam1:ch0")).toEqual({ physicalDid: "cam1", channel: 0 });
    expect(splitChannelDid("cam1:ch1")).toEqual({ physicalDid: "cam1", channel: 1 });
    expect(splitChannelDid("cam1:ch10")).toEqual({ physicalDid: "cam1", channel: 10 });
  });
  it("只认末尾 :ch{n}，物理 did 含冒号不误伤", () => {
    expect(splitChannelDid("a:b:ch2")).toEqual({ physicalDid: "a:b", channel: 2 });
  });
  it("非数字后缀不当通道，整体作物理 did", () => {
    expect(splitChannelDid("cam:chX")).toEqual({ physicalDid: "cam:chX", channel: 0 });
  });
});

describe("multiChannelDidSet", () => {
  it("出现 >1 次的 did 判为多通道；单条不算", () => {
    const set = multiChannelDidSet([
      { did: "dual" },
      { did: "dual" },
      { did: "solo" },
    ]);
    expect(set.has("dual")).toBe(true);
    expect(set.has("solo")).toBe(false);
  });
  it("空输入 → 空集", () => {
    expect(multiChannelDidSet([]).size).toBe(0);
  });
});

describe("feedDid", () => {
  it("多通道 → 合成 did", () => {
    expect(feedDid("cam", 0, true)).toBe("cam:ch0");
    expect(feedDid("cam", 1, true)).toBe("cam:ch1");
  });
  it("单通道 → 裸 did", () => {
    expect(feedDid("cam", 0, false)).toBe("cam");
  });
});

describe("lensLabelKey", () => {
  it("ch0=移动画面 / ch1=固定画面 / 其它 null", () => {
    expect(lensLabelKey(0)).toBe("hero.lensMoving");
    expect(lensLabelKey(1)).toBe("hero.lensFixed");
    expect(lensLabelKey(2)).toBeNull();
  });
});

describe("channelHasMic", () => {
  it("只有 ch0(球机)有 mic", () => {
    expect(channelHasMic(0)).toBe(true);
    expect(channelHasMic(1)).toBe(false);
    expect(channelHasMic(2)).toBe(false);
  });
});
