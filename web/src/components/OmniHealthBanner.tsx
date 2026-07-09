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


// 立即重试冷却期:点击后本地 disabled 这么久,后端 /omni-config/retry 端点也有
// 同款冷却拦截(admin/router.py::_OMNI_RETRY_COOLDOWN_SEC),双层保护——前端防止
// 误触,后端防止绕过 UI 的脚本狂调。两端值需保持一致。
const RETRY_COOLDOWN_SEC = 5;


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
  const [cooldownUntil, setCooldownUntil] = useState<number>(0);

  useEffect(() => {
    return subscribeOmniHealth(setHealth, () => {
      // SSE 重连:backend 可能刚重启,广播事件让「模型」页 refetch config。
      window.dispatchEvent(new Event(OMNI_CONFIG_STALE_EVENT));
    });
  }, []);

  const nextSec = useCountdownSeconds(health?.next_probe_at_ms ?? null);
  const cooldownRemaining = useCountdownSeconds(
    cooldownUntil > 0 ? cooldownUntil : null,
  );
  const inCooldown = cooldownRemaining != null && cooldownRemaining > 0;

  if (!health || health.state === "ok") return null;

  const isConfig = health.state === "error";
  const cls = isConfig
    ? "bg-error-bg text-error border-b border-error"
    : "bg-warning-bg text-warning border-b border-warning";

  async function onRetry() {
    setRetrying(true);
    // 无论成功/失败都进入本地冷却:成功时后端已发 probe 不该立刻再触发;失败时
    // (含后端返 code=1 冷却拦截)也要阻止用户狂点。用户看到按钮变倒计时即知冷却中。
    setCooldownUntil(Date.now() + RETRY_COOLDOWN_SEC * 1000);
    try {
      await retryOmniProbe();
      // SSE 会推新 health,不需要手动 setHealth
    } catch (e) {
      toast(e instanceof Error ? e.message : t("omniHealth.retryFailed"), "danger");
    } finally {
      setRetrying(false);
    }
  }

  // 优先用 code 查本地化文案(backend message 是硬编码中文,直接注入会污染英文界面);
  // code 为空或未在 omniHealth.codes 里定义时回退到 backend message,保留 http_error
  // 附带的 HTTP 状态码等动态细节。
  const localizedMsg = health.code
    ? t(`omniHealth.codes.${health.code}`, { defaultValue: health.message })
    : health.message;
  const message = isConfig
    ? t("omniHealth.configInvalid", { message: localizedMsg })
    : nextSec != null
      ? t("omniHealth.retrying", { message: localizedMsg, seconds: nextSec })
      : t("omniHealth.retryingNoTime", { message: localizedMsg });

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
          disabled={retrying || inCooldown}
          className="text-caption px-3 py-1 rounded border border-current hover:opacity-80 disabled:opacity-60"
        >
          {retrying
            ? t("omniHealth.retryingBtn")
            : inCooldown
              ? t("omniHealth.cooldownBtn", { seconds: cooldownRemaining })
              : t("omniHealth.retryNow")}
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
