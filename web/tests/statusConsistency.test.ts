/**
 * 授权失效时，状态条各项的口径要一致。
 *
 * 截图时发现的不一致：一边说「已停止工作」，另一边「看家」项仍是绿点「在看家」。
 * 那一项原先只判引擎进程在不在跑——授权废了之后引擎还活着，只是拉不到相机列表、
 * 没有相机可喂，所以显示绿。两个相邻的指示灯给出相反结论，住户无从判断。
 *
 * 这里钉的是判据本身：只要判据说「失效」，看家项就该走停止那一档。
 */

import { describe, it, expect } from "vitest";
import { watchTier } from "@/lib/watchTier";

describe("授权失效时看家项的档位", () => {
  it("引擎还在跑、但授权已失效 → 停止，不能显示「在看家」", () => {
    expect(
      watchTier({
        miot: { bound: true, authDegraded: true },
        perception: { running: true, ready: true },
      }),
    ).toBe("auth-stopped");
  });

  it("访问令牌也过期（绑定状态翻假）→ 仍是停止", () => {
    expect(
      watchTier({
        miot: { bound: false, authDegraded: true },
        perception: { running: true, ready: true },
      }),
    ).toBe("auth-stopped");
  });

  it("授权正常、引擎在跑 → 在看家", () => {
    expect(
      watchTier({
        miot: { bound: true, authDegraded: false },
        perception: { running: true, ready: true },
      }),
    ).toBe("watching");
  });

  it("授权正常、引擎被暂停 → 休息中（授权档不该抢占它）", () => {
    expect(
      watchTier({
        miot: { bound: true, authDegraded: false },
        perception: { running: false, ready: false },
      }),
    ).toBe("resting");
  });

  it("授权正常、引擎没准备好 → 还没准备好", () => {
    expect(
      watchTier({
        miot: { bound: true, authDegraded: false },
        perception: { running: true, ready: false },
      }),
    ).toBe("not-ready");
  });
});
