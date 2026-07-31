/**
 * switchBlockedReasonKey / cameraAvailable — 单开与批量开启共用的硬门。
 * 顺序与后端 toggle_camera 一致:云端离线 > 镜头关 > 满额；OT/LAN 只作诊断。
 */

import { describe, it, expect } from "vitest";
import { switchBlockedReasonKey } from "@/lib/cameraSwitch";
import { cameraAvailable } from "@/lib/types";

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

  it("单开：OT/局域网未发现仍放行，让 direct-IP/PPCS 有机会握手", () => {
    expect(
      switchBlockedReasonKey(cam({ lanReachable: false }), {
        inUse: false,
        atCapacity: false,
      }),
    ).toBeUndefined();
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

  it("OT/局域网未发现不遮蔽镜头关闭硬门", () => {
    expect(
      switchBlockedReasonKey(cam({ lanReachable: false, awake: false }), {
        inUse: false,
        atCapacity: false,
      }),
    ).toBe("hero.disabledLensHint");
  });
});

describe("cameraAvailable — 一键全开候选", () => {
  it("批量开启：OT/局域网未发现仍进入候选", () => {
    expect(cameraAvailable(cam({ lanReachable: false }))).toBe(true);
  });

  it("云端离线或镜头关闭仍排除", () => {
    expect(cameraAvailable(cam({ cloudOnline: false }))).toBe(false);
    expect(cameraAvailable(cam({ awake: false }))).toBe(false);
  });
});
