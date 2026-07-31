/**
 * 投喂开关「不可开」原因 key —— 仅未启用时给理由,已启用随时可关返回 undefined。
 *
 * 原因由开启硬门(云端在线 / 镜头开关)+ 满额直接判,顺序与后端 `toggle_camera` 的
 * hard-reject 一致(云端离线 > 镜头关 > 满额),
 * 让开关提示与后端拒绝口径一致、不会「面板说能开、点了被后端拒」。`awake===null`
 * (机型无开关 / 未知)不算不可用,与后端放行口径一致。
 * `lanReachable` 只用于状态诊断：OT 未发现时仍需允许 direct-IP/PPCS 发起握手。
 *
 * 抽成纯函数便于单测,并让开关的点击 toast 与桌面悬停气泡取同一文案。
 */
import type { ScopeCamera } from "./types";

export function switchBlockedReasonKey(
  cam: Pick<ScopeCamera, "cloudOnline" | "awake">,
  opts: { inUse: boolean; atCapacity: boolean },
): string | undefined {
  if (opts.inUse) return undefined; // 已启用 → 随时可关,无理由
  if (!cam.cloudOnline) return "hero.disabledOfflineHint";
  if (cam.awake === false) return "hero.disabledLensHint";
  if (opts.atCapacity) return "hero.disabledCapacityHint";
  return undefined; // 可用且有名额 → 直接可开
}
