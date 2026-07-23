/**
 * 升级检测的共享入口 —— 让侧栏底部提示点（Sidebar）与升级 banner/弹窗（UpgradeNotice）
 * 共用同一次 /upgrade/check 网络请求，并用一个 window 自定义事件解耦"点提示点 → 打开升级
 * 确认弹窗"的跨组件意图（沿用本仓 useTheme 的 hook + window 事件模式，仓内不用 React Context）。
 *
 * 为什么 fetch 放这里而不放 lib/upgrade.ts：lib/upgrade.ts 刻意保持纯、零副作用依赖
 * （node 单测直接 import），不能牵入 api/网络层。
 */

import { useEffect, useState } from "react";
import { upgradeCheck, dismissUpgrade as apiDismissUpgrade } from "@/api";
import type { UpgradeCheck } from "@/lib/types";

/** 点侧栏提示点 → 请求打开升级确认弹窗的意图事件。 */
export const OPEN_UPGRADE_EVENT = "miloco-open-upgrade";

// 模块级共享存储：整个页面生命周期打开时查一次（走服务端缓存），结果广播给所有挂
// useUpgradeInfo 的组件（Sidebar + UpgradeNotice 共用同一结果，不重复打网络）。用户手动
// 「检查更新」时经 refreshUpgradeInfo 强制现查一次并同样广播。查询异常 → null（静默）。
let _info: UpgradeCheck | null = null;
let _started = false;
const INFO_EVENT = "miloco-upgrade-info";

async function loadUpgradeInfo(force = false): Promise<UpgradeCheck | null> {
  const r = await upgradeCheck(force).catch(() => null);
  _info = r;
  window.dispatchEvent(new Event(INFO_EVENT));
  return r;
}

/** 手动「检查更新」：强制跳过缓存现查一次并广播（供侧栏版本号入口点击时用）。 */
export function refreshUpgradeInfo(): Promise<UpgradeCheck | null> {
  return loadUpgradeInfo(true);
}

/** 订阅共享的升级信息；首个挂载者触发"打开页面查一次"（走缓存），后续订阅广播实时同步。 */
export function useUpgradeInfo(): UpgradeCheck | null {
  const [info, setInfo] = useState<UpgradeCheck | null>(_info);
  useEffect(() => {
    const h = () => setInfo(_info);
    window.addEventListener(INFO_EVENT, h);
    if (!_started) {
      _started = true;
      loadUpgradeInfo(false);
    } else {
      setInfo(_info);
    }
    return () => window.removeEventListener(INFO_EVENT, h);
  }, []);
  return info;
}

/** 请求打开升级确认弹窗（由 UpgradeNotice 监听 OPEN_UPGRADE_EVENT 响应）。 */
export function requestOpenUpgrade(): void {
  window.dispatchEvent(new Event(OPEN_UPGRADE_EVENT));
}

// ── "已确认到某版本"（dismiss）——存后端、不放浏览器 ──────────────────────────
// 关闭 banner 时调用：POST 记到后端，并就地把共享 info.dismissed 更新为该版本 + 广播，
// 让 banner 立即隐藏（无需再打 GitHub 重查）。已确认版本随 /upgrade/check 一起返回（info.dismissed），
// 语义：latest === dismissed 时 banner 不显，直到出现更新版本；红点不看它（有更新就显）。
export async function dismissUpgrade(version: string): Promise<void> {
  await apiDismissUpgrade(version);
  if (_info) {
    _info = { ..._info, dismissed: version };
    window.dispatchEvent(new Event(INFO_EVENT));
  }
}
