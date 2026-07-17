/**
 * 升级纯函数单测（node 环境，无 DOM）：banner 可见性、更新可用性、完成判定、阶段→步骤映射。
 *
 * 覆盖：有新版才出、dev 部署不出、按版本 dismiss（同版本永久不出 / 更新版本再出）、
 * 弹窗/升级中态占屏时 banner 让位、info 缺失/不可达时不出；updateAvailable（侧栏红点门控：
 * 有可升级新版即常驻显示，与 dismiss 无关）；phaseToStep（阶段→步骤映射）。
 */

import { describe, it, expect } from "vitest";
import {
  shouldShowUpgradeBanner,
  updateAvailable,
  phaseToStep,
} from "@/lib/upgrade";
import type { UpgradeCheck } from "@/lib/types";

function mk(over: Partial<UpgradeCheck> = {}): UpgradeCheck {
  return {
    current: "2026.7.2",
    latest: "2026.7.3",
    has_update: true,
    deploy_kind: "release",
    release_url: "https://gh/rel/2026.7.3",
    reachable: true,
    checked_at: 0,
    dismissed: null, // 已确认版本来自后端 /upgrade/check（不再是浏览器 localStorage）
    ...over,
  };
}

describe("shouldShowUpgradeBanner（dismissed 取自 info，即后端返回）", () => {
  it("release + 有新版 + 未 dismiss + idle → 显示", () => {
    expect(shouldShowUpgradeBanner(mk(), "idle")).toBe(true);
  });

  it("info 为 null（尚未查到/查询失败）→ 不显示", () => {
    expect(shouldShowUpgradeBanner(null, "idle")).toBe(false);
  });

  it("无新版（has_update=false）→ 不显示", () => {
    expect(shouldShowUpgradeBanner(mk({ has_update: false }), "idle")).toBe(
      false,
    );
  });

  it("dev(git) 部署即使有新版也不显示（只提示 git pull）", () => {
    expect(shouldShowUpgradeBanner(mk({ deploy_kind: "dev" }), "idle")).toBe(
      false,
    );
  });

  it("GitHub 不可达（latest=null）→ 不显示", () => {
    expect(
      shouldShowUpgradeBanner(
        mk({ reachable: false, latest: null, has_update: false }),
        "idle",
      ),
    ).toBe(false);
  });

  it("后端已记录该版本 dismissed（latest===dismissed）→ 不显示", () => {
    expect(
      shouldShowUpgradeBanner(
        mk({ latest: "2026.7.3", dismissed: "2026.7.3" }),
        "idle",
      ),
    ).toBe(false);
  });

  it("dismissed 的是旧版本、又出更新版本 → 重新显示", () => {
    expect(
      shouldShowUpgradeBanner(
        mk({ latest: "2026.7.4", dismissed: "2026.7.3" }),
        "idle",
      ),
    ).toBe(true);
  });

  it.each(["confirm", "upgrading", "timeout", "failed"] as const)(
    "phase=%s（弹窗/升级中态占屏）→ banner 让位不显示",
    (phase) => {
      expect(shouldShowUpgradeBanner(mk(), phase)).toBe(false);
    },
  );
});

describe("updateAvailable（侧栏红点门控：有可升级新版即显示，与 dismiss 无关）", () => {
  it("release + 有新版 → true", () => {
    expect(updateAvailable(mk())).toBe(true);
  });
  it("dev 部署 → false", () => {
    expect(updateAvailable(mk({ deploy_kind: "dev" }))).toBe(false);
  });
  it("无新版 → false", () => {
    expect(updateAvailable(mk({ has_update: false }))).toBe(false);
  });
  it("null → false", () => {
    expect(updateAvailable(null)).toBe(false);
  });
});

describe("phaseToStep（/upgrade/status phase → 步骤下标）", () => {
  it("下载阶段 → 0", () => {
    expect(phaseToStep("starting")).toBe(0);
    expect(phaseToStep("downloading")).toBe(0);
    expect(phaseToStep("idle")).toBe(0);
  });
  it("安装阶段 → 1", () => {
    expect(phaseToStep("installing")).toBe(1);
  });
  it("未知 phase（含 done/failed，由轮询单独处理）→ 兜底 0", () => {
    expect(phaseToStep("whatever")).toBe(0);
    expect(phaseToStep("done")).toBe(0);
    expect(phaseToStep("failed")).toBe(0);
  });
});
