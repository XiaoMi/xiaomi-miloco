/**
 * 全局顶部横条:反映 omni 熔断器实时健康度。
 *
 * - state=ok → 不渲染
 * - state=warn(可恢复错) → 黄色横条 + 「立即重试」按钮
 * - state=error(配置错) → 红色横条 + 「立即重试」+ 「到「模型」页修改」
 *
 * 数据来源:GET /api/admin/omni-config 首次拉取 + SSE /api/admin/omni-config/stream 实时更新。
 * SSE 首连即推当前状态,所以初次挂载不需要手动 GET。
 */

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { OMNI_CONFIG_STALE_EVENT, retryOmniProbe, subscribeOmniHealth } from "@/api";
import type { OmniHealth } from "@/lib/types";
import { toast } from "./Toast";


function useCountdownSeconds(target_ms: number | null): number | null {
  // 不缓存 now：如果缓存挂载时的 Date.now(),target_ms 稍后才从 SSE 到达时,首次 render
  // 会用挂载时刻的老 now 与将来的 target 作差,显示"等待时长"而非"到期剩余秒数"。
  // 每秒 setTick 触发重渲染,渲染时直接读 Date.now() 保证首帧就正确。
  const [, setTick] = useState(0);
  useEffect(() => {
    if (target_ms == null) return;
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, [target_ms]);
  if (target_ms == null) return null;
  return Math.max(0, Math.round((target_ms - Date.now()) / 1000));
}


export function OmniHealthBanner({
  onGoToConfig,
}: {
  /** 「到模型页」跳转回调;上层负责切 tab。 */
  onGoToConfig?: () => void;
}) {
  const { t } = useTranslation();
  const [health, setHealth] = useState<OmniHealth | null>(null);
  const [retrying, setRetrying] = useState(false);

  useEffect(() => {
    return subscribeOmniHealth(setHealth, () => {
      // SSE 重连:backend 可能刚重启,广播事件让「模型」页 refetch config。
      window.dispatchEvent(new Event(OMNI_CONFIG_STALE_EVENT));
    });
  }, []);

  const nextSec = useCountdownSeconds(health?.next_probe_at_ms ?? null);

  if (!health || health.state === "ok") return null;

  const isConfig = health.state === "error";
  const cls = isConfig
    ? "bg-error-bg text-error border-b border-error"
    : "bg-warning-bg text-warning border-b border-warning";

  async function onRetry() {
    setRetrying(true);
    try {
      await retryOmniProbe();
      // SSE 会推新 health,不需要手动 setHealth
    } catch (e) {
      toast(e instanceof Error ? e.message : t("omniHealth.retryFailed"), "danger");
    } finally {
      setRetrying(false);
    }
  }

  const message = isConfig
    ? t("omniHealth.configInvalid", { message: health.message })
    : nextSec != null
      ? t("omniHealth.retrying", { message: health.message, seconds: nextSec })
      : t("omniHealth.retryingNoTime", { message: health.message });

  return (
    <div className={`w-full px-4 py-2 flex items-center justify-between gap-3 shrink-0 ${cls}`}>
      <div className="text-caption flex items-baseline gap-2 flex-wrap">
        <span>{message}</span>
        {health.consecutive_failures > 3 && (
          <span className="num">
            · {t("omniHealth.failuresCount", { n: health.consecutive_failures })}
          </span>
        )}
      </div>
      <div className="flex gap-2 shrink-0">
        <button
          type="button"
          onClick={onRetry}
          disabled={retrying}
          className="text-caption px-3 py-1 rounded border border-current hover:opacity-80 disabled:opacity-60"
        >
          {retrying ? t("omniHealth.retryingBtn") : t("omniHealth.retryNow")}
        </button>
        {isConfig && onGoToConfig && (
          <button
            type="button"
            onClick={onGoToConfig}
            className="text-caption px-3 py-1 rounded border border-current hover:opacity-80"
          >
            {t("omniHealth.goToConfig")}
          </button>
        )}
      </div>
    </div>
  );
}
