/**
 * 状态条「看家」项的分档判据。
 *
 * 抽成纯函数是为了能被单测钉住——组件本身不在测试范围内（仓库没装渲染库），
 * 判据留在组件里就只能靠复刻一份来测，而复刻件与实现漂移之后测试照样绿。
 *
 * **授权失效优先于引擎自身状态**：授权一废，拉相机列表就会被拒，感知没有相机
 * 可跑——引擎进程还活着，但它已经不在看家了。只判引擎状态会显示「在看家」，与
 * 旁边那一项的「已停止工作」自相矛盾，两个相邻的指示灯给出相反结论。
 */

import { miotTone } from "./miotTone";

export type WatchTier = "auth-stopped" | "watching" | "standby" | "not-ready" | "resting";

export function watchTier(input: {
  miot: { bound: boolean; authDegraded?: boolean };
  perception: { running: boolean; ready: boolean };
  allCamerasOff?: boolean;
}): WatchTier {
  if (miotTone(input.miot) === "degraded") return "auth-stopped";
  if (!input.perception.running) return "resting";
  if (!input.perception.ready) return "not-ready";
  return input.allCamerasOff ? "standby" : "watching";
}
