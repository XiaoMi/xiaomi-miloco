/**
 * 「家里发生了什么」的折叠强度——弱档「各行其时」/ 强档「并入事件」，两档之间没有中间态。
 *
 * 跟主题、语言、自动刷新周期同一套路持久化到 localStorage。**它存在本机、不跟随账号**：
 * 这个选择没有对错，只有代价——换浏览器、换设备要重选一次，换来的是「这台机器上我想怎么看
 * 这份日志」不必向服务端解释，也不需要为它多一张表、一个接口和一个同步时机。
 *
 * 默认强档：折叠的收益（一个事件永远只占一行）在最需要它的地方最大——事件多、动作更多，
 * 而那时用户正在扫「出了什么事」，还没到要逐条读动作的地步。弱档是给「把日志当审计记录看」
 * 的人留的，那种看法是**主动选的**，不该是默认。
 *
 * 存进去的值是要认的：读回来的字符串必须是这两个字面量之一，其余一律当没存过。列是自由文本、
 * 键也可能被手改，而一个认不出的档位若原样落进 state，渲染层拿到的是第三种值——两处分支都
 * 不命中，界面会卡在一个既不是弱也不是强的画法上。
 */

import { useCallback, useEffect, useState } from "react";
import type { FoldStrength } from "@/lib/feedFold";

export const FOLD_STRENGTH_KEY = "web:activity:foldStrength";
export const FOLD_STRENGTH_DEFAULT: FoldStrength = "strong";

/** 跨组件 / 跨标签页同步：同页只有一处开关，但另一标签页改了要跟上。 */
const EVENT = "miloco:fold-strength";

/** 从本机存储读回档位。认不出的值退回默认，不让第三种状态流进渲染层（见文件头）。 */
export function readStoredFoldStrength(): FoldStrength {
  if (typeof localStorage === "undefined") return FOLD_STRENGTH_DEFAULT;
  const raw = localStorage.getItem(FOLD_STRENGTH_KEY);
  return raw === "weak" || raw === "strong" ? raw : FOLD_STRENGTH_DEFAULT;
}

export function useFoldStrength(): {
  strength: FoldStrength;
  setStrength: (s: FoldStrength) => void;
} {
  const [strength, setLocal] = useState(readStoredFoldStrength);

  useEffect(() => {
    const onSync = () => setLocal(readStoredFoldStrength());
    window.addEventListener(EVENT, onSync);
    window.addEventListener("storage", onSync);
    return () => {
      window.removeEventListener(EVENT, onSync);
      window.removeEventListener("storage", onSync);
    };
  }, []);

  const setStrength = useCallback((s: FoldStrength) => {
    setLocal(s);
    try {
      localStorage.setItem(FOLD_STRENGTH_KEY, s);
      // 广播只在写成功后发：同步处理器是回读存储的，写失败还广播会让本实例把刚设的
      // 档位打回默认值。
      window.dispatchEvent(new Event(EVENT));
    } catch {
      // 存储不可用（隐私模式）→ 不广播，本实例内存里的新档位仍生效
    }
  }, []);

  return { strength, setStrength };
}
