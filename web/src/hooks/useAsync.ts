/**
 * 极简 useAsync —— 第一版不引 TanStack Query。
 * 提供 loading/error/data + reload 即可覆盖所有拉取场景。
 *
 * 错误处理:error 进入 state 同时 toast 一条住户友好的提示,但**只在「转入错误」那一刻**发。
 * 这样调用方不必逐个检查 error;只在需要重试时才取 error 字段。
 *
 * 为什么不是每次失败都发:轮询驱动的拉取在后端宕机期间每个周期都会失败一次,
 * 而 Toast 的去重是为「同一操作连点」设的短窗口、按设计短于轮询间隔,挡不住这种重复。
 * 于是每次都弹就成了一条不会停的瀑布,而同一个失败界面上本来就有内联错误条(还带重试
 * 入口)。这里只在「转入错误」那一刻发一次;恢复成功后重新置位,下次断开会再提醒。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "@/components/Toast";
import i18n from "@/i18n";

export interface AsyncState<T> {
  data: T | undefined;
  loading: boolean;
  error: Error | undefined;
  reload: () => Promise<void>;
}

export interface UseAsyncOptions {
  /** 错误时 toast 显示的描述（"加载家人信息失败"等）。空 = 不 toast */
  errorLabel?: string;
}

/**
 * 「转入错误才提醒」的闸门。抽成独立函数是为了能回归——它的两条语义都容易被后来人改掉：
 * 只在从非错误态进入错误态时放行；成功一次后重新置位，于是下一次断开还会再提醒一次。
 */
export function createErrorToastGate(): { ok: () => void; fail: () => boolean } {
  let wasError = false;
  return {
    ok: () => {
      wasError = false;
    },
    fail: () => {
      const first = !wasError;
      wasError = true;
      return first;
    },
  };
}

export function useAsync<T>(
  fn: () => Promise<T>,
  deps: unknown[] = [],
  options: UseAsyncOptions = {},
): AsyncState<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<Error | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);

  // reload 返回 Promise:在「本次触发的重拉」settle(成功 / 失败)后 resolve,让调用方能
  // await 到数据真落地(如手动刷新按钮转圈覆盖到列表更新到位)。不 await 的现有调用照常
  // 工作(忽略返回的 Promise),向后兼容。
  /** 把 toast 收成「转入错误时一次」（原因见文件头）。 */
  const gateRef = useRef(createErrorToastGate());

  const pendingResolvers = useRef<Array<() => void>>([]);
  const reload = useCallback(
    () =>
      new Promise<void>((resolve) => {
        pendingResolvers.current.push(resolve);
        setTick((x) => x + 1);
      }),
    [],
  );

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fn()
      .then((d) => {
        if (!cancelled) {
          setData(d);
          setError(undefined);
          gateRef.current.ok();
        }
      })
      .catch((e) => {
        if (cancelled) return;
        const err = e instanceof Error ? e : new Error(String(e));
        setError(err);
        // 只在「转入错误」时提醒一次（原因见文件头）。闸门描述的是错误态的进入与退出，
        // 与「要不要显示」分开：显示由调用方是否给了 errorLabel 决定，故先无条件推进闸门。
        const first = gateRef.current.fail();
        if (options.errorLabel && first) {
          toast(i18n.t("common.errorToast", { label: options.errorLabel }), "warn");
        }
      })
      .finally(() => {
        // 被新一轮取代(cancelled)的旧拉取:setData 已被跳过、数据丢弃,不算「落地」,
        // **不**唤醒等待者——resolver 留给接棒的新一轮,否则并发窗口里(如切开关的
        // fire-and-forget reload 正 in-flight 时点手动刷新)旧拉取先 settle 会把刷新的
        // resolver 提前 resolve,转圈早于列表更新一小拍停。
        if (cancelled) return;
        setLoading(false);
        // 本轮(未被取消)拉取 settle = 数据真落地:唤醒所有等待「重拉落地」的 reload()。
        const resolvers = pendingResolvers.current;
        pendingResolvers.current = [];
        resolvers.forEach((r) => r());
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  // unmount 兜底:卸载后不再有接棒的新一轮拉取,把仍在等待的 reload() 全部唤醒,防调用方
  // await 永挂。(主 effect 的 cleanup 无法区分「卸载」与「deps/tick 变化重跑」,故单独用
  // 空 deps effect——它的 cleanup 只在卸载时执行。)
  useEffect(
    () => () => {
      const resolvers = pendingResolvers.current;
      pendingResolvers.current = [];
      resolvers.forEach((r) => r());
    },
    [],
  );

  return { data, loading, error, reload };
}
