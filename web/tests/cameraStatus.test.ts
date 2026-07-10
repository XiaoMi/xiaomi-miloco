/**
 * cameraStatus 派生态单测 — 四原子字段(isOnline / lanOnline / inUse / connected)
 * + grace 窗口 → 展示态(kind / canEnable / labelKey / tone / connecting / diagKey)。
 *
 * 纯函数、now/enabledAt 注入,不读时钟,故可精确断言 grace 边界。
 */

import { describe, it, expect } from "vitest";
import {
  cameraStatus,
  CAMERA_GRACE_MS,
  switchBlockedReasonKey,
} from "@/lib/cameraStatus";

// 只需 cameraStatus 读的四个字段;默认「云端在线 + LAN 可见 + 未启用 + 未订阅」。
function cam(
  over: Partial<{
    isOnline: boolean;
    lanOnline: boolean;
    inUse: boolean;
    connected: boolean;
  }> = {},
) {
  return { isOnline: true, lanOnline: true, inUse: false, connected: false, ...over };
}

const T0 = 1_000_000;

describe("cameraStatus", () => {
  it("云端离线 → offline，红色「已离线」，不可开", () => {
    const s = cameraStatus(cam({ isOnline: false }), { now: T0 });
    expect(s.kind).toBe("offline");
    expect(s.canEnable).toBe(false);
    expect(s.labelKey).toBe("hero.stOffline");
    expect(s.tone).toBe("error");
    expect(s.diagKey).toBeUndefined();
  });

  it("云端离线优先于 connected 残留(切断过渡期也判离线,不误显在感知)", () => {
    const s = cameraStatus(cam({ isOnline: false, inUse: true, connected: true }), {
      now: T0,
    });
    expect(s.kind).toBe("offline");
    expect(s.canEnable).toBe(false);
  });

  it("在线未启用 → online，灰色「已关闭感知」，可开", () => {
    const s = cameraStatus(cam({ inUse: false }), { now: T0 });
    expect(s.kind).toBe("online");
    expect(s.canEnable).toBe(true);
    expect(s.labelKey).toBe("hero.stPerceptionOff");
    expect(s.tone).toBe("muted");
    expect(s.diagKey).toBeUndefined();
  });

  it("在线已启用已订阅 → perceiving(上区 live,下区无标签)", () => {
    const s = cameraStatus(cam({ inUse: true, connected: true }), { now: T0 });
    expect(s.kind).toBe("perceiving");
    expect(s.canEnable).toBe(true);
    expect(s.labelKey).toBeUndefined();
  });

  it("已启用未出流 + grace 内 → 「接入中」灰色 + 转圈,不甩锅、不出诊断框", () => {
    const s = cameraStatus(cam({ inUse: true, connected: false, lanOnline: false }), {
      now: T0,
      enabledAt: T0 - (CAMERA_GRACE_MS - 1),
    });
    expect(s.kind).toBe("noStream");
    expect(s.labelKey).toBe("hero.stConnecting");
    expect(s.tone).toBe("muted");
    expect(s.connecting).toBe(true);
    expect(s.diagKey).toBeUndefined();
  });

  it("已启用未出流 + 超 grace + LAN 不可见 → 「未出流」黄色 + 跨 LAN 诊断框", () => {
    const s = cameraStatus(cam({ inUse: true, connected: false, lanOnline: false }), {
      now: T0,
      enabledAt: T0 - (CAMERA_GRACE_MS + 1),
    });
    expect(s.kind).toBe("noStream");
    expect(s.labelKey).toBe("hero.stNoStream");
    expect(s.tone).toBe("warning");
    expect(s.connecting).toBeFalsy();
    expect(s.diagKey).toBe("hero.diagCrossLan");
  });

  it("grace 精确边界(now - enabledAt === CAMERA_GRACE_MS)算超 grace,给诊断而非接入中", () => {
    // 代码用严格 `< graceMs`:恰等于 graceMs 即已出 grace(钉住边界,防 < 被误改成 <=)。
    const s = cameraStatus(cam({ inUse: true, connected: false, lanOnline: false }), {
      now: T0,
      enabledAt: T0 - CAMERA_GRACE_MS,
    });
    expect(s.connecting).toBeFalsy();
    expect(s.diagKey).toBe("hero.diagCrossLan");
  });

  it("已启用未出流 + 超 grace + LAN 可见 → 中性诊断(不预设未开 LAN 模式)", () => {
    const s = cameraStatus(cam({ inUse: true, connected: false, lanOnline: true }), {
      now: T0,
      enabledAt: T0 - (CAMERA_GRACE_MS + 1),
    });
    expect(s.kind).toBe("noStream");
    expect(s.labelKey).toBe("hero.stNoStream");
    expect(s.diagKey).toBe("hero.diagLanReachable");
  });

  it("已启用未出流 + 无 enabledAt(重启后残留态) → 直接给诊断,不算接入中", () => {
    const s = cameraStatus(cam({ inUse: true, connected: false, lanOnline: false }), {
      now: T0,
    });
    expect(s.kind).toBe("noStream");
    expect(s.connecting).toBeFalsy();
    expect(s.diagKey).toBe("hero.diagCrossLan");
  });

  it("canEnable 仅 offline 为 false，其余三态为 true", () => {
    expect(cameraStatus(cam({ isOnline: false }), { now: T0 }).canEnable).toBe(false);
    expect(cameraStatus(cam({ inUse: false }), { now: T0 }).canEnable).toBe(true);
    expect(
      cameraStatus(cam({ inUse: true, connected: true }), { now: T0 }).canEnable,
    ).toBe(true);
    expect(
      cameraStatus(cam({ inUse: true, connected: false }), { now: T0 }).canEnable,
    ).toBe(true);
  });
});

describe("switchBlockedReasonKey", () => {
  it("已启用 → 无理由(随时可关,即便离线/满额)", () => {
    expect(
      switchBlockedReasonKey({ inUse: true, canEnable: false, atCapacity: true }),
    ).toBeUndefined();
  });

  it("未启用 + 不可开(离线) → 离线提示", () => {
    expect(
      switchBlockedReasonKey({ inUse: false, canEnable: false, atCapacity: false }),
    ).toBe("hero.disabledOfflineHint");
  });

  it("未启用 + 可开 + 满额 → 满额提示", () => {
    expect(
      switchBlockedReasonKey({ inUse: false, canEnable: true, atCapacity: true }),
    ).toBe("hero.disabledCapacityHint");
  });

  it("未启用 + 可开 + 未满额 → 无理由(可开)", () => {
    expect(
      switchBlockedReasonKey({ inUse: false, canEnable: true, atCapacity: false }),
    ).toBeUndefined();
  });

  it("离线优先于满额(未启用 + 不可开 + 满额)→ 离线提示", () => {
    expect(
      switchBlockedReasonKey({ inUse: false, canEnable: false, atCapacity: true }),
    ).toBe("hero.disabledOfflineHint");
  });
});
