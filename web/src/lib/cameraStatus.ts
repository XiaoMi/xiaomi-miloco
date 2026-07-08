/**
 * 摄像头派生状态——把「云端在线 / LAN 发现 / 启用意图 / 已订阅」四个原子字段
 * 收敛成一个前端展示态，替代原先散在 HeroNow 三处的二元 online/offline 判断。
 *
 * 纯函数：不读时钟（`now` / `enabledAt` 由调用方传入），便于单测。
 *
 * 四态：
 *  - offline    云端离线（真·不可用）——开关禁、标签「已离线」
 *  - online     云端在线、未启用——可开启
 *  - perceiving 云端在线、已启用、已订阅出流——上区 live
 *  - noStream   云端在线、已启用、但没订阅出流——下区「已启用·未出流」+ 诊断
 *
 * 在线判定只看云端 `isOnline`，不看 `lanOnline`：后者只表示 backend 主机能否在
 * 局域网直接发现相机（跨网段 / NAT / WSL 恒 false），不代表云端离线。`lanOnline`
 * 仅用于给 noStream 的诊断文案分流。
 */
import type { ScopeCamera } from "./types";

export type CameraStatusKind = "offline" | "online" | "perceiving" | "noStream";

export interface CameraStatus {
  kind: CameraStatusKind;
  /** 未启用时能否开启投喂（= 云端在线）。已启用的相机永远可关，与此无关。 */
  canEnable: boolean;
  /** 下区行主标签的 i18n key（offline / noStream 才有；online 无标签）。 */
  benchPrimaryKey?: string;
  /** noStream 诊断文案的 i18n key（接入中 / 跨 LAN / 未开 LAN 模式）。 */
  benchHintKey?: string;
}

/** 「已启用但未出流」判定为跨 LAN 之前给的宽限窗口：刚点开的几秒接入中，别甩锅。 */
export const CAMERA_GRACE_MS = 15_000;

export function cameraStatus(
  cam: Pick<ScopeCamera, "isOnline" | "lanOnline" | "inUse" | "connected">,
  opts: { now: number; enabledAt?: number; graceMs?: number },
): CameraStatus {
  const graceMs = opts.graceMs ?? CAMERA_GRACE_MS;

  // 真·云端离线：不可开，标签「已离线」。
  if (!cam.isOnline) {
    return { kind: "offline", canEnable: false, benchPrimaryKey: "hero.benchOffline" };
  }
  // 云端在线、未启用：可开启，下区无特殊标签。
  if (!cam.inUse) {
    return { kind: "online", canEnable: true };
  }
  // 云端在线、已启用、已订阅：正在投喂（上区 live）。
  if (cam.connected) {
    return { kind: "perceiving", canEnable: true };
  }
  // 云端在线、已启用、未订阅：已启用·未出流。按 grace + lanOnline 分流诊断。
  //  - grace 内（刚点开）：接入中，不提跨 LAN，避免误报失败。
  //  - 超 grace + lan 不可见：多半跨网段 / NAT / WSL。
  //  - 超 grace + lan 可见：相机可能没开 LAN 模式。
  const inGrace =
    opts.enabledAt !== undefined && opts.now - opts.enabledAt < graceMs;
  const benchHintKey = inGrace
    ? "hero.noStreamHintConnecting"
    : cam.lanOnline
      ? "hero.noStreamHintNoLanMode"
      : "hero.noStreamHintCrossLan";
  return {
    kind: "noStream",
    canEnable: true,
    benchPrimaryKey: "hero.benchNoStream",
    benchHintKey,
  };
}
