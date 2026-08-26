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

/**
 * 夹取规则只此一处，输入框与 setSec 共用。
 *
 * 输入框不能只靠「把值交上去、等 sec 变了再回显」：值已经在下限上时，再输一个更小的数
 * 夹完还是下限，state 没变、React 跳过重渲染、回显的副作用也就不触发——框里会留着那个
 * 不生效的数。所以提交时自己先夹一次。
 */
export function clampRefreshSec(n: number): number {
  return Math.max(REFRESH_MIN_SEC, Math.floor(n));
}

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
    const v = clampRefreshSec(n);
    setLocal(v);
    try {
      localStorage.setItem(REFRESH_KEY, String(v));
      // 广播只在写成功后发：同步处理器是回读存储的，写失败还广播会让本实例把刚设的值
      // 打回默认值，与下面「本次会话内仍生效」正好相反。
      window.dispatchEvent(new Event(EVENT));
    } catch {
      // 存储不可用（隐私模式）→ 不广播，本实例内存里的新值仍生效，另一张卡保持原值
    }
  }, []);

  return { sec, setSec };
}
