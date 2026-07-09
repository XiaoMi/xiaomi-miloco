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


// 后端 SSE 断连时用的兜底冷却秒数:正常情况下前端从 health.retry_cooldown_sec 读,
// 拿不到再回退这里。值与后端 circuit_breaker.RETRY_COOLDOWN_SEC 对齐即可,不再要求
// 精确同步——单源在后端。
const RETRY_COOLDOWN_SEC_FALLBACK = 5;


/**
 * SSE 相对秒数倒计时:每秒 -1,归零后返 0。
 * 入参是「初始剩余秒数」而不是绝对时刻,因为剩余秒数由服务端按 monotonic 差算好推来,
 * 不依赖两端时钟一致(NAS/容器场景常见几十秒时钟偏差),前端只做单调递减即可。
 */
function useCountdownSeconds(initial_seconds: number | null): number | null {
  const [remaining, setRemaining] = useState<number | null>(initial_seconds);
  useEffect(() => {
    setRemaining(initial_seconds);
    if (initial_seconds == null || initial_seconds <= 0) return;
    const id = setInterval(() => {
      setRemaining((prev) => (prev == null ? null : Math.max(0, prev - 1)));
    }, 1000);
    return () => clearInterval(id);
  }, [initial_seconds]);
  return remaining;
}


/**
 * 本地按钮冷却倒计时:入参是本地时钟系的绝对 ms deadline。
 * 必须与 useCountdownSeconds 分开,因为本地冷却每次点击都需触发新一轮 effect,而冷却
 * 值恒为 5 时 initial_seconds 依赖不变 → effect 不重跑 → 按钮再点击不置灰(CB-N5)。
 * 本地时钟自己与自身对齐,不涉及跨机器偏差,可以放心用 Date.now()。
 */
function useCountdownToDeadline(deadline_ms: number | null): number | null {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (deadline_ms == null) return;
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, [deadline_ms]);
  if (deadline_ms == null) return null;
  return Math.max(0, Math.round((deadline_ms - Date.now()) / 1000));
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
  // 点击「立即重试」后本地按钮冷却截止时刻(本地时钟 ms)。每次点击都是新的 Date.now(),
  // deadline 必然变化,useCountdownToDeadline 的 effect 一定重跑。
  const [cooldownDeadline, setCooldownDeadline] = useState<number | null>(null);

  useEffect(() => {
    return subscribeOmniHealth(setHealth, () => {
      // SSE 重连:backend 可能刚重启,广播事件让「模型」页 refetch config。
      window.dispatchEvent(new Event(OMNI_CONFIG_STALE_EVENT));
    });
  }, []);

  const nextSec = useCountdownSeconds(health?.next_probe_in_seconds ?? null);
  const cooldownRemaining = useCountdownToDeadline(cooldownDeadline);
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
    // 冷却秒数从 health 里读(后端 circuit_breaker.RETRY_COOLDOWN_SEC 单源),缺省回退。
    const cooldownSec = health?.retry_cooldown_sec ?? RETRY_COOLDOWN_SEC_FALLBACK;
    setCooldownDeadline(Date.now() + cooldownSec * 1000);
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
