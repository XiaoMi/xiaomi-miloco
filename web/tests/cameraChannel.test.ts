import { describe, it, expect } from "vitest";
import {
  channelHasMic,
  feedDid,
  isChannelDid,
  lensLabelKey,
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
  it("严格 :ch{非负整数}：空/小数/负数/十六进制等后端会拒绝的畸形不当通道", () => {
    // 旧 Number() 会把这些悄悄解释成通道；严格正则一律退化成整串裸 did、channel 0。
    for (const bad of ["cam:ch", "cam:ch1.5", "cam:ch-1", "cam:ch0x1", "cam:ch 1"]) {
      expect(splitChannelDid(bad)).toEqual({ physicalDid: bad, channel: 0 });
    }
  });
  it("多个 :chN 只取末尾（贪婪 last-wins）", () => {
    expect(splitChannelDid("a:ch1:ch2")).toEqual({ physicalDid: "a:ch1", channel: 2 });
  });
  it("无冒号的 chN 不当通道", () => {
    expect(splitChannelDid("camch3")).toEqual({ physicalDid: "camch3", channel: 0 });
  });
  it("前导零通道号归一为整数（与后端 int() 同）", () => {
    expect(splitChannelDid("cam:ch01")).toEqual({ physicalDid: "cam", channel: 1 });
  });
  it("空串 → 裸 did、channel 0", () => {
    expect(splitChannelDid("")).toEqual({ physicalDid: "", channel: 0 });
  });
});

describe("split/feed 往返一致", () => {
  it("多通道：split(feed(did, ch)) 还原 {did, ch}", () => {
    for (const did of ["cam", "a:b", "did-with-dash"]) {
      for (const ch of [0, 1, 2, 10]) {
        expect(splitChannelDid(feedDid(did, ch, true))).toEqual({
          physicalDid: did,
          channel: ch,
        });
      }
    }
  });
  it("单通道：feed 裸 did，split 还原 channel 0", () => {
    expect(splitChannelDid(feedDid("cam", 0, false))).toEqual({
      physicalDid: "cam",
      channel: 0,
    });
  });
});

describe("isChannelDid", () => {
  it("合成 did(:chN) → true", () => {
    expect(isChannelDid("cam:ch0")).toBe(true);
    expect(isChannelDid("cam:ch1")).toBe(true);
    expect(isChannelDid("cam:ch10")).toBe(true);
    expect(isChannelDid("a:b:ch2")).toBe(true);
  });
  it("裸 did → false", () => {
    expect(isChannelDid("cam")).toBe(false);
    expect(isChannelDid("1213460650")).toBe(false);
    expect(isChannelDid("")).toBe(false);
  });
  it("畸形 :ch 后缀不算通道（与 splitChannelDid 同口径）", () => {
    for (const bad of ["cam:ch", "cam:ch1.5", "cam:ch-1", "cam:ch0x1", "cam:chX"]) {
      expect(isChannelDid(bad)).toBe(false);
    }
  });
  it("单摄单路在列表里也能凭 did 形态识别（不再依赖行数）", () => {
    // 只有一路时行数代理会漏判；合成 did 形态是每行独立的权威信号。
    expect(isChannelDid("dual:ch0")).toBe(true);
    expect(isChannelDid("solo")).toBe(false);
  });
});

describe("feedDid", () => {
  it("多通道 → 合成 did", () => {
    expect(feedDid("cam", 0, true)).toBe("cam:ch0");
    expect(feedDid("cam", 1, true)).toBe("cam:ch1");
  });
  it("单通道 → 裸 did（忽略传入的 channel）", () => {
    expect(feedDid("cam", 0, false)).toBe("cam");
    expect(feedDid("cam", 3, false)).toBe("cam");
  });
});

describe("lensLabelKey", () => {
  it("ch0=移动画面 / ch1=固定画面 / 其它 null", () => {
    expect(lensLabelKey(0)).toBe("hero.lensMoving");
    expect(lensLabelKey(1)).toBe("hero.lensFixed");
    expect(lensLabelKey(2)).toBeNull();
    expect(lensLabelKey(-1)).toBeNull();
  });
});

describe("channelHasMic", () => {
  it("只有 ch0(球机)有 mic", () => {
    expect(channelHasMic(0)).toBe(true);
    expect(channelHasMic(1)).toBe(false);
    expect(channelHasMic(2)).toBe(false);
    expect(channelHasMic(-1)).toBe(false);
  });
});
