/**
 * cameraStatus 派生态单测 — 四原子字段(isOnline / lanOnline / inUse / connected)
 * + grace 窗口 → 展示态(kind / canEnable / benchPrimaryKey / benchHintKey)。
 *
 * 纯函数、now/enabledAt 注入,不读时钟,故可精确断言 grace 边界。
 */

import { describe, it, expect } from "vitest";
import { cameraStatus, CAMERA_GRACE_MS } from "@/lib/cameraStatus";

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
  it("云端离线 → offline，不可开，标签「已离线」", () => {
    const s = cameraStatus(cam({ isOnline: false }), { now: T0 });
    expect(s.kind).toBe("offline");
    expect(s.canEnable).toBe(false);
    expect(s.benchPrimaryKey).toBe("hero.benchOffline");
    expect(s.benchHintKey).toBeUndefined();
  });

  it("云端离线优先于 connected 残留(切断过渡期也判离线,不误显在感知)", () => {
    const s = cameraStatus(cam({ isOnline: false, inUse: true, connected: true }), {
      now: T0,
    });
    expect(s.kind).toBe("offline");
    expect(s.canEnable).toBe(false);
  });

  it("在线未启用 → online，可开，无标签/诊断", () => {
    const s = cameraStatus(cam({ inUse: false }), { now: T0 });
    expect(s.kind).toBe("online");
    expect(s.canEnable).toBe(true);
    expect(s.benchPrimaryKey).toBeUndefined();
    expect(s.benchHintKey).toBeUndefined();
  });

  it("在线已启用已订阅 → perceiving(上区 live)", () => {
    const s = cameraStatus(cam({ inUse: true, connected: true }), { now: T0 });
    expect(s.kind).toBe("perceiving");
    expect(s.canEnable).toBe(true);
  });

  it("已启用未出流 + grace 内 → 接入中(不甩锅跨 LAN)", () => {
    const s = cameraStatus(cam({ inUse: true, connected: false, lanOnline: false }), {
      now: T0,
      enabledAt: T0 - (CAMERA_GRACE_MS - 1),
    });
    expect(s.kind).toBe("noStream");
    expect(s.benchPrimaryKey).toBe("hero.benchNoStream");
    expect(s.benchHintKey).toBe("hero.noStreamHintConnecting");
  });

  it("已启用未出流 + 超 grace + LAN 不可见 → 跨 LAN 诊断", () => {
    const s = cameraStatus(cam({ inUse: true, connected: false, lanOnline: false }), {
      now: T0,
      enabledAt: T0 - (CAMERA_GRACE_MS + 1),
    });
    expect(s.kind).toBe("noStream");
    expect(s.benchHintKey).toBe("hero.noStreamHintCrossLan");
  });

  it("grace 精确边界(now - enabledAt === CAMERA_GRACE_MS)算超 grace,给诊断而非接入中", () => {
    // 代码用严格 `< graceMs`:恰等于 graceMs 即已出 grace(钉住边界,防 < 被误改成 <=)。
    const s = cameraStatus(cam({ inUse: true, connected: false, lanOnline: false }), {
      now: T0,
      enabledAt: T0 - CAMERA_GRACE_MS,
    });
    expect(s.kind).toBe("noStream");
    expect(s.benchHintKey).toBe("hero.noStreamHintCrossLan");
  });

  it("已启用未出流 + 超 grace + LAN 可见 → 未开 LAN 模式诊断", () => {
    const s = cameraStatus(cam({ inUse: true, connected: false, lanOnline: true }), {
      now: T0,
      enabledAt: T0 - (CAMERA_GRACE_MS + 1),
    });
    expect(s.kind).toBe("noStream");
    expect(s.benchHintKey).toBe("hero.noStreamHintNoLanMode");
  });

  it("已启用未出流 + 无 enabledAt(重启后残留态) → 直接给诊断,不算接入中", () => {
    const s = cameraStatus(cam({ inUse: true, connected: false, lanOnline: false }), {
      now: T0,
    });
    expect(s.kind).toBe("noStream");
    expect(s.benchHintKey).toBe("hero.noStreamHintCrossLan");
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
