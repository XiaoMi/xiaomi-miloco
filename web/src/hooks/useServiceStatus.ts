/**
 * 后端服务状态轮询。
 *
 * 内置网页打开着的时候，住户可能在菜单栏点了「停止服务」（或后端崩了、被巡检自动
 * 重启）：页面要能自己发现这个变化 —— 顶部状态条变成「服务已停止」+ 一条横幅，
 * 服务回来后**自动刷新**（各 tab 的 SSE、相机流、分页游标都是按老进程建立的，
 * 只有整页重载才保证一致）。
 *
 * 轮询间隔取 2s：菜单里点下「停止服务」到页面显示出来不超过 2s，一次本机
 * /health 请求开销可忽略；服务不可用时页面本就没有别的请求在跑。
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  nextServiceState,
  probeService,
  setServiceDown,
  type ServiceState,
} from "@/lib/serviceStatus";

export interface ServiceStatus {
  state: ServiceState;
  /** 服务停止的起始时刻(ms)；运行中为 null。 */
  downSince: number | null;
  /** 立刻重探一次（横幅 / 状态条上的「重试」） */
  recheck: () => void;
}

export interface UseServiceStatusOptions {
  /** 轮询间隔(ms)，默认 2000。 */
  intervalMs?: number;
  /**
   * 由「已停止」翻回「运行」时调用一次（页面在这里自动刷新）。
   * 首帧成功（checking → up）不算 —— 那是正常冷启动，不该刷新。
   */
  onRecover?: () => void;
}

export function useServiceStatus(options: UseServiceStatusOptions = {}): ServiceStatus {
  const { intervalMs = 2000, onRecover } = options;
  const [state, setState] = useState<ServiceState>("checking");
  const [downSince, setDownSince] = useState<number | null>(null);
  // 手动重探：只用来重启定时器（下一次 tick 立刻跑一轮）。
  const [tick, setTick] = useState(0);

  const stateRef = useRef<ServiceState>("checking");
  // onRecover 一般是内联箭头函数：放进 deps 会让定时器每渲染一次就重建，
  // 用一个每次渲染都刷新的 ref 拿最新实现。
  const recoverRef = useRef(onRecover);
  recoverRef.current = onRecover;

  const recheck = useCallback(() => setTick((x) => x + 1), []);

  useEffect(() => {
    let cancelled = false;

    const run = async () => {
      const ok = await probeService();
      if (cancelled) return;
      const { state: next, recovered } = nextServiceState(stateRef.current, ok);
      stateRef.current = next;
      // 全局标记先落：useAsync 的失败 toast 靠它静音，要让本轮探测结果立刻生效。
      setServiceDown(!ok);
      setState(next);
      if (next === "up") {
        setDownSince(null);
        // 只有"确认停止过 → 恢复"才自动刷新；冷启动首帧成功不算。
        if (recovered) recoverRef.current?.();
      } else {
        // 保留首次停止的时刻（持续不可用时不要被后续轮询不断刷新）。
        setDownSince((d) => d ?? Date.now());
      }
    };

    void run();
    const id = setInterval(() => void run(), intervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [intervalMs, tick]);

  return { state, downSince, recheck };
}
