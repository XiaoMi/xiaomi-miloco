/**
 * PerceptionDeviceTable 的纯函数 helper —— 抽出来便于 vitest 单测。
 *
 * 不导出 React hook、不依赖 i18n，组件和测试都用同一个函数避免实现飘移。
 */

import type { ScopeCamera } from "@/lib/types";
import { cameraAvailable } from "@/lib/types";

export function sortCamerasByDid(cameras: ScopeCamera[]): ScopeCamera[] {
  return [...cameras].sort((a, b) =>
    a.did < b.did ? -1 : a.did > b.did ? 1 : 0,
  );
}

export function onlineCameras(cameras: ScopeCamera[]): ScopeCamera[] {
  return cameras.filter(cameraAvailable);
}