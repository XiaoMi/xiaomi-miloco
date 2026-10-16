/**
 * miotTone — 米家连接项的三态判据。
 *
 * 关键不变量：失效档独立于 bound。若被 bound 门控，续期被拒 + access_token 也
 * 过期时（问题最严重的那一刻）状态条反而从红退回黄「未连」，失效原因一并消失，
 * 而命令行体检此刻正确报不通过——两个面口径劈叉。
 */

import { describe, it, expect } from "vitest";
import { miotTone } from "@/lib/miotTone";

describe("miotTone", () => {
  it("授权失效且 access_token 尚可用（bound 仍为真）→ 失效", () => {
    expect(miotTone({ bound: true, authDegraded: true })).toBe("degraded");
  });

  it("授权失效且 access_token 也过期（bound 翻假）→ 仍是失效，不是「未连」", () => {
    // 这一条正是被 bound 门控时会退化的场景
    expect(miotTone({ bound: false, authDegraded: true })).toBe("degraded");
  });

  it("已绑定且授权正常 → 已连", () => {
    expect(miotTone({ bound: true, authDegraded: false })).toBe("connected");
  });

  it("从未绑定 → 未连", () => {
    expect(miotTone({ bound: false, authDegraded: false })).toBe("disconnected");
  });

  it("老后端不返回该字段时按「未失效」处理", () => {
    expect(miotTone({ bound: true })).toBe("connected");
    expect(miotTone({ bound: false })).toBe("disconnected");
  });
});
