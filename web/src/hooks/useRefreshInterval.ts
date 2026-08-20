/**
 * 「模型」页的自动刷新周期，Token 用量与性能监测**共用同一个值**。
 *
 * 为什么共用而不是各自一份：两张卡在同一页、看的是同一段时间里同一次感知循环的两面。
 * 各自一个周期会让「用量说 3,270 次调用、性能说 623 轮」这种对不上的瞬态更难解释，
 * 而这类对不上本来就够难解释了。两处都露出同一个输入框，改哪边都是改这一个值。
 *
 * 下限 5 秒是硬的：这是台一边跑推理一边被轮询的家用盒子，两张卡合计 4 个接口，
 * 5 秒就是每分钟 48 次请求，再往下只会把 CPU 花在应答上。上限不设——填得很大等于
 * 实际关掉自动刷新，那是使用者的选择。
 */

import { useCallback, useEffect, useState } from "react";

export const REFRESH_KEY = "web:usage:refreshSec";
export const REFRESH_MIN_SEC = 5;
export const REFRESH_DEFAULT_SEC = 30;

/** 跨组件同步：同一页两个输入框，改一个另一个要跟着变。 */
const EVENT = "miloco:refresh-interval";

function readStored(): number {
  if (typeof localStorage === "undefined") return REFRESH_DEFAULT_SEC;
  const raw = localStorage.getItem(REFRESH_KEY);
  const n = Number.parseInt(raw ?? "", 10);
  // 存量值也要过一遍下限：老版本或手改存储都可能塞进 1
  return Number.isFinite(n) && n >= REFRESH_MIN_SEC ? n : REFRESH_DEFAULT_SEC;
}

export function useRefreshInterval(): {
  sec: number;
  setSec: (n: number) => void;
} {
  const [sec, setLocal] = useState(readStored);

  useEffect(() => {
    const onSync = () => setLocal(readStored());
    window.addEventListener(EVENT, onSync);
    // 另一个标签页改了也跟上
    window.addEventListener("storage", onSync);
    return () => {
      window.removeEventListener(EVENT, onSync);
      window.removeEventListener("storage", onSync);
    };
  }, []);

  const setSec = useCallback((n: number) => {
    const v = Math.max(REFRESH_MIN_SEC, Math.floor(n));
    setLocal(v);
    try {
      localStorage.setItem(REFRESH_KEY, String(v));
    } catch {
      // 存储不可用（隐私模式）→ 本次会话内仍生效
    }
    window.dispatchEvent(new Event(EVENT));
  }, []);

  return { sec, setSec };
}
