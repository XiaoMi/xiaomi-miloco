/**
 * 「家里发生了什么」的两个折叠偏好：**折叠开不开**（useFoldEnabled）与**强度**
 * （useFoldStrength）。两个键各存各的，理由在下面「关掉不删档位」那条。
 *
 * 强度是弱档「各行其时」/ 强档「并入事件」，两档之间没有中间态。
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

/* ── 两个偏好共用的持久化骨架 ──────────────────────────────────
   读回并校验 → useState → 同页自定义事件 + storage 双监听 → 写入成功后广播：两个偏好
   逐字同构，差别只有存储键、事件名、和「值怎么变成存储里的那个字符串」。

   各写一份的代价不是今天的重复，是明天的漏改：广播时机、隐私模式回退这类修法都得动两遍，
   而只改了一处时，症状是「这个偏好跨标签页同步、那个不同步」——极难一眼看出的不一致。
   骨架共用，两个键仍各存各的（见文件头）：共用的是骨架，不是状态。 */
function useStoredPref<T>(
  key: string,
  eventName: string,
  /** 读回并校验。认不出的值退回默认，别让第三种状态流进渲染层（见文件头）。 */
  read: () => T,
  /** 值 → 存储里的字符串。 */
  serialize: (v: T) => string,
): { value: T; setValue: (v: T) => void } {
  const [value, setLocal] = useState(read);

  useEffect(() => {
    const onSync = () => setLocal(read());
    window.addEventListener(eventName, onSync);
    window.addEventListener("storage", onSync);
    return () => {
      window.removeEventListener(eventName, onSync);
      window.removeEventListener("storage", onSync);
    };
  }, [eventName, read]);

  const setValue = useCallback(
    (v: T) => {
      setLocal(v);
      try {
        localStorage.setItem(key, serialize(v));
        // 广播只在写成功后发：同步处理器是回读存储的，写失败还广播会让本实例把刚设的
        // 值打回默认。
        window.dispatchEvent(new Event(eventName));
      } catch {
        // 存储不可用（隐私模式）→ 不广播，本实例内存里的新值仍生效
      }
    },
    // read / serialize 都是模块层的稳定引用，所以 setValue 的身份恒定——与它被抽出来之前
    // 一样。写成内联箭头就会每次渲染换一个身份，传染给一切拿它当依赖的 effect。
    [key, eventName, serialize],
  );

  return { value, setValue };
}

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

/** 档位原样存：`"weak"` / `"strong"` 本身就是存储格式。 */
const serializeStrength = (s: FoldStrength): string => s;

export function useFoldStrength(): {
  strength: FoldStrength;
  setStrength: (s: FoldStrength) => void;
} {
  const { value, setValue } = useStoredPref(
    FOLD_STRENGTH_KEY,
    EVENT,
    readStoredFoldStrength,
    serializeStrength,
  );
  return { strength: value, setStrength: setValue };
}

/* ── 折叠开不开 ────────────────────────────────────────────────
   关掉 = 未折叠态：动作不分组，每条自己一行、锚在自己的时刻上。这是**逃生口**——把动作
   折进事件是这一页默认面貌的一次改动，得留一条「今天我想按原样翻一遍」的路。

   默认开：折叠的收益在最需要它的地方最大（见上），而它是这一版要交付的形态；未折叠是主动
   选的那个，不该是默认。

   **关掉不删档位。**两个键分开存，就是为了这件事：平时读强档的人关掉折叠翻一遍再打开，
   回来还是强档，不必重选一次。所以这里只管开不开，不去动 FOLD_STRENGTH_KEY。 */
export const FOLD_ENABLED_KEY = "web:activity:folded";
export const FOLD_ENABLED_DEFAULT = true;

/** 跨组件 / 跨标签页同步。与强度分用两个事件名：同步处理器是回读存储的，共用一个名字会
 *  让改档位也触发一次开关的回读（结果一样，但白跑一次，且两件事的日志混在一起）。 */
const ENABLED_EVENT = "miloco:fold-enabled";

/** 从本机存储读回开关。**只认 "1" / "0"**：其余（含 null）一律当没存过，退回默认。
 *  与档位同一条理由——列是自由文本、键也可能被手改，认不出的值原样落进 state 就会变成
 *  「既不是开了也不是关了」，两处分支都不命中。 */
export function readStoredFoldEnabled(): boolean {
  if (typeof localStorage === "undefined") return FOLD_ENABLED_DEFAULT;
  const raw = localStorage.getItem(FOLD_ENABLED_KEY);
  return raw === "1" ? true : raw === "0" ? false : FOLD_ENABLED_DEFAULT;
}

/** 开关存 `"1"` / `"0"`。 */
const serializeEnabled = (v: boolean): string => (v ? "1" : "0");

export function useFoldEnabled(): {
  folded: boolean;
  setFolded: (v: boolean) => void;
} {
  const { value, setValue } = useStoredPref(
    FOLD_ENABLED_KEY,
    ENABLED_EVENT,
    readStoredFoldEnabled,
    serializeEnabled,
  );
  return { folded: value, setFolded: setValue };
}
