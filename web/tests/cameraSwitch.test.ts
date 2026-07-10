/**
 * switchBlockedReasonKey — 投喂开关「不可开」原因(基于 #403 三态 + 满额)。
 * 顺序与后端 toggle_camera 的 hard-reject 一致:云端离线 > 局域网不可达 > 镜头关 > 满额。
 */

import { describe, it, expect } from "vitest";
import { switchBlockedReasonKey } from "@/lib/cameraSwitch";

type Cam = { cloudOnline: boolean; lanReachable: boolean; awake: boolean | null };
const cam = (o: Partial<Cam> = {}): Cam => ({
  cloudOnline: true,
  lanReachable: true,
  awake: true,
  ...o,
});

describe("switchBlockedReasonKey", () => {
  it("已启用 → undefined(随时可关,任意状态/满额)", () => {
    expect(
      switchBlockedReasonKey(cam({ cloudOnline: false }), {
        inUse: true,
        atCapacity: true,
      }),
    ).toBeUndefined();
  });

  it("云端离线 → disabledOfflineHint", () => {
    expect(
      switchBlockedReasonKey(cam({ cloudOnline: false }), {
        inUse: false,
        atCapacity: false,
      }),
    ).toBe("hero.disabledOfflineHint");
  });

  it("局域网不可达 → disabledLanHint", () => {
    expect(
      switchBlockedReasonKey(cam({ lanReachable: false }), {
        inUse: false,
        atCapacity: false,
      }),
    ).toBe("hero.disabledLanHint");
  });

  it("镜头关 → disabledLensHint", () => {
    expect(
      switchBlockedReasonKey(cam({ awake: false }), {
        inUse: false,
        atCapacity: false,
      }),
    ).toBe("hero.disabledLensHint");
  });

  it("awake=null(未知)不算镜头关、放行", () => {
    expect(
      switchBlockedReasonKey(cam({ awake: null }), {
        inUse: false,
        atCapacity: false,
      }),
    ).toBeUndefined();
  });

  it("可用 + 满额 → disabledCapacityHint", () => {
    expect(
      switchBlockedReasonKey(cam(), { inUse: false, atCapacity: true }),
    ).toBe("hero.disabledCapacityHint");
  });

  it("可用 + 有名额 → undefined(可开)", () => {
    expect(
      switchBlockedReasonKey(cam(), { inUse: false, atCapacity: false }),
    ).toBeUndefined();
  });

  it("优先级:云端离线 > 满额", () => {
    expect(
      switchBlockedReasonKey(cam({ cloudOnline: false }), {
        inUse: false,
        atCapacity: true,
      }),
    ).toBe("hero.disabledOfflineHint");
  });

  it("优先级:局域网不可达 > 镜头关(与后端拒绝顺序一致)", () => {
    expect(
      switchBlockedReasonKey(cam({ lanReachable: false, awake: false }), {
        inUse: false,
        atCapacity: false,
      }),
    ).toBe("hero.disabledLanHint");
  });
});
