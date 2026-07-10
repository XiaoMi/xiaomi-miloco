/**
 * 极简 useAsync —— 第一版不引 TanStack Query。
 * 提供 loading/error/data + reload 即可覆盖所有拉取场景。
 *
 * 错误处理:error 进入 state 同时**自动 toast** 一条住户友好的提示。
 * 这样调用方不必逐个检查 error;只在需要重试时才取 error 字段。
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
        }
      })
      .catch((e) => {
        if (cancelled) return;
        const err = e instanceof Error ? e : new Error(String(e));
        setError(err);
        if (options.errorLabel) {
          toast(i18n.t("common.errorToast", { label: options.errorLabel }), "warn");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
        // 本轮拉取 settle:唤醒所有等待「重拉落地」的 reload() 调用方。即使 cancelled 也
        // resolve,避免 await reload() 永挂(deps 变会另起新一轮拉取)。
        const resolvers = pendingResolvers.current;
        pendingResolvers.current = [];
        resolvers.forEach((r) => r());
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  return { data, loading, error, reload };
}
