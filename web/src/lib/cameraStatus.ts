/**
 * 摄像头派生状态——把「云端在线 / LAN 发现 / 启用意图 / 已订阅」四个原子字段
 * 收敛成一个前端展示态，替代原先散在 HeroNow 三处的二元 online/offline 判断。
 *
 * 纯函数：不读时钟（`now` / `enabledAt` 由调用方传入），便于单测。
 *
 * 四态 → 下区呈现（短状态词 + 按严重度染色 + 接入转圈 + 独立诊断框）：
 *  - offline    云端离线（真·不可用）——「已离线」红字，开关禁
 *  - online     云端在线、未启用——「已关闭感知」灰字，可开启（正常可用，只是没开）
 *  - perceiving 云端在线、已启用、已订阅出流——上区 live（下区不出现、无标签）
 *  - noStream   云端在线、已启用、未订阅出流：
 *      · grace 内  「接入中」灰字 + 转圈（进行中，不报警、不甩锅跨 LAN）
 *      · 超 grace  「未出流」黄字 + 诊断框（按 lanOnline 分流：跨网段 / LAN 可达但无画面）
 *
 * 在线判定只看云端 `isOnline`，不看 `lanOnline`：后者只表示 backend 主机能否在
 * 局域网直接发现相机（跨网段 / NAT / WSL 恒 false），不代表云端离线。`lanOnline`
 * 仅用于给 noStream 超 grace 的诊断文案分流。
 */
import type { ScopeCamera } from "./types";

export type CameraStatusKind = "offline" | "online" | "perceiving" | "noStream";

/** 主标签染色档：error 红（离线）/ warning 黄（未出流异常）/ muted 灰（正常态 / 接入中过程）。 */
export type CameraTone = "error" | "warning" | "muted";

export interface CameraStatus {
  kind: CameraStatusKind;
  /** 未启用时能否开启投喂（= 云端在线）。已启用的相机永远可关，与此无关。 */
  canEnable: boolean;
  /** 下区行主标签的 i18n key（perceiving 无标签、在上区）。 */
  labelKey?: string;
  /** 主标签染色档（前端映射到 text-error / text-warning / text-text-secondary）。 */
  tone?: CameraTone;
  /** grace 内接入中：前端据此在主标签后显示转圈动画，且不出诊断框。 */
  connecting?: boolean;
  /** 诊断框文案的 i18n key（仅超 grace 未出流：跨网段 / LAN 可达但无画面）。 */
  diagKey?: string;
}

/** 「已启用但未出流」判定为跨 LAN 之前给的宽限窗口：刚点开的几秒接入中，别甩锅。 */
export const CAMERA_GRACE_MS = 15_000;

export function cameraStatus(
  cam: Pick<ScopeCamera, "isOnline" | "lanOnline" | "inUse" | "connected">,
  opts: { now: number; enabledAt?: number; graceMs?: number },
): CameraStatus {
  const graceMs = opts.graceMs ?? CAMERA_GRACE_MS;

  // 真·云端离线：不可开，「已离线」红字。
  if (!cam.isOnline) {
    return {
      kind: "offline",
      canEnable: false,
      labelKey: "hero.stOffline",
      tone: "error",
    };
  }
  // 云端在线、未启用：可开启，「已关闭感知」灰字（正常可用，只是没开感知）。
  if (!cam.inUse) {
    return {
      kind: "online",
      canEnable: true,
      labelKey: "hero.stPerceptionOff",
      tone: "muted",
    };
  }
  // 云端在线、已启用、已订阅：正在投喂（上区 live），下区不出现、无标签。
  if (cam.connected) {
    return { kind: "perceiving", canEnable: true };
  }
  // 云端在线、已启用、未订阅：已启用但没出流。
  //  - grace 内（刚点开）：「接入中」灰字 + 转圈，进行中不报警、不甩锅跨 LAN。
  //  - 超 grace：「未出流」黄字 + 诊断框，按 lanOnline 分流：
  //      lan 不可见 → 多半跨网段 / NAT / WSL；
  //      lan 可见   → LAN 可发现却仍不出流（异常态），给中性提示、不预设归因
  //                   （lanOnline=true 恰说明 LAN 发现在工作，断言「未开 LAN 模式」自相矛盾）。
  const inGrace =
    opts.enabledAt !== undefined && opts.now - opts.enabledAt < graceMs;
  if (inGrace) {
    return {
      kind: "noStream",
      canEnable: true,
      labelKey: "hero.stConnecting",
      tone: "muted",
      connecting: true,
    };
  }
  return {
    kind: "noStream",
    canEnable: true,
    labelKey: "hero.stNoStream",
    tone: "warning",
    diagKey: cam.lanOnline ? "hero.diagLanReachable" : "hero.diagCrossLan",
  };
}

/**
 * 开关「不可开」的理由文案 key——仅未启用时给理由:离线 / 满额。已启用的相机随时可关,
 * 返回 undefined。瞬态忙(busy)是操作 in-flight、走原生禁用,不属此列、也不在此判。
 *
 * 抽成纯函数便于单测「文案随原因切换」,并让开关的点击 toast 与 hover 气泡取同一文案。
 */
export function switchBlockedReasonKey(opts: {
  inUse: boolean;
  canEnable: boolean;
  atCapacity: boolean;
}): string | undefined {
  if (opts.inUse) return undefined;
  if (!opts.canEnable) return "hero.disabledOfflineHint";
  if (opts.atCapacity) return "hero.disabledCapacityHint";
  return undefined;
}
