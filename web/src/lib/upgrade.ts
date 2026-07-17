/**
 * 升级 banner 可见性 / 步骤映射 / 完成判定 —— 纯函数，单一事实源。
 *
 * 抽成纯函数便于单测（web 测试跑 node 环境、无 jsdom；且本文件刻意不 import api/客户端，
 * 保持零副作用依赖）。banner 门控：有可一键升级的 release 新版、当前 idle、且用户没按这个
 * 版本 dismiss 过时才显示。关掉即对该版本**永久**不再提示，仅当出现更新的版本才再现
 * （或用户主动点侧栏底部的升级入口）——不设时间限制。
 */

import type { UpgradeCheck } from "@/lib/types";

// checking = 用户手动检查更新、正在现查（弹窗显 loading）；checked = 查完但无可升级新版
// （弹窗显"已是最新 / 无法检查"结果）。其余同前。
export type UpgradePhase =
  | "idle"
  | "checking"
  | "checked"
  | "confirm"
  | "upgrading"
  | "timeout"
  | "failed";

// 升级步骤（下载 → 安装 → 重启）。「完成」不单列成步——重启完即完成、届时直接刷新页面。
export const UPGRADE_STEPS = ["download", "install", "restart"] as const;
export type UpgradeStepKey = (typeof UPGRADE_STEPS)[number];

// 后端 /upgrade/status 的 phase → 步骤下标。restart 阶段不来自 status（后端那时已重启、
// 端点消失），由前端「轮询 /upgrade/status 连不上（throw）」判定，故这里只映射 下载 / 安装。
const PHASE_TO_STEP: Record<string, number> = {
  idle: 0,
  starting: 0,
  downloading: 0,
  installing: 1,
};

export function phaseToStep(phase: string): number {
  return PHASE_TO_STEP[phase] ?? 0;
}

/**
 * 是否"有可一键升级的新版本"——用于侧栏底部版本号旁那个常驻提示点。
 * 与 banner 不同：不受 dismiss 影响（dot 是常驻低调入口，关掉 banner 后仍在）。
 */
export function updateAvailable(info: UpgradeCheck | null): boolean {
  return (
    !!info && info.has_update && info.deploy_kind === "release" && !!info.latest
  );
}

/**
 * 侧栏底部提示点直接用 updateAvailable 判定（有可升级新版即常驻显示，不受 dismiss 影响）。
 * 可关闭的是下面的 banner——按版本 dismiss，**dismiss 状态由后端返回（info.dismissed），
 * 不再放浏览器**。
 *
 * 是否显示"有新版本"窄 banner：有可升级新版 + idle + 该版本未被 dismiss（latest !== dismissed）。
 *
 * @param info  后端 /upgrade/check 结果（含 dismissed；null=尚未查到/查询失败）
 * @param phase 当前交互阶段（confirm/upgrading/… 时不显示 banner）
 */
export function shouldShowUpgradeBanner(
  info: UpgradeCheck | null,
  phase: UpgradePhase,
): boolean {
  return (
    updateAvailable(info) &&
    phase === "idle" &&
    info!.dismissed !== info!.latest
  );
}
